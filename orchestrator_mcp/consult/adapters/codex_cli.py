"""Consulting a Codex CLI.

The flags and event names here were written from documentation and have since been
run against codex-cli 0.146: every argument below is one the binary accepts on both
`exec` and `exec resume`, which is not the same set -- see the sandbox comment. The
parser stays deliberately tolerant about where in an event the text lives, because
that is the part a release can move without telling anyone.

What is not tolerant is the event *kind*: a command, a file change, an MCP call
or a subagent means the CLI is doing something a consultation never asked for,
and that fails closed rather than being ignored.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from ...contract import Usage
from ..config import AgentConfig
from ..contract import SourceMode, consultation_content_schema
from ..errors import ConsultErrorCode
from ..prompts import CompiledPrompt
from .base import (
    AdapterError,
    AdapterResult,
    AgentStatus,
    ProcessResult,
    accounted,
    check_cache_is_a_breakdown,
    check_model,
    check_reported_total,
    child_env,
    parse_content,
    resolve_command,
    run_process,
    usage_any,
)

PREFLIGHT_TIMEOUT_S = 30.0

# Substrings that mark an event as an action rather than a message. Matched against
# the event type so a renamed-but-recognisable event still fails closed.
FORBIDDEN_EVENT_MARKERS = ("command", "exec_command", "patch", "file_change", "mcp", "subagent")

# Reaching the network is an action too, but a legitimate one in web mode -- so these
# are checked only in the modes that pass `web_search=disabled`. The config key is what
# prevents the search; this is what notices if it ever stops working, which is the
# whole reason a deny-list exists behind a setting.
NETWORK_EVENT_MARKERS = ("web", "search", "fetch", "browse")

# Substrings that mark the run as having failed. Matched the same tolerant way, so
# `turn.failed`, `error`, and whatever the next release calls it all stop the turn.
FAILURE_EVENT_MARKERS = ("failed", "failure", "error", "aborted", "cancelled", "canceled")

# A thread id, checked before it is put in a glob pattern. `*` and `?` in an id we did
# not generate would otherwise match some other session's file.
_SESSION_ID = re.compile(r"[0-9a-fA-F-]{16,64}")


class CodexCliAdapter:
    runtime = "codex"

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def connect_command(self, agent: AgentConfig) -> str:
        return f"{agent.command} login"

    async def preflight(self, agent: AgentConfig) -> AgentStatus:
        try:
            command = resolve_command(agent)
        except AdapterError:
            return AgentStatus(agent.agent_id, installed=False, authenticated=False,
                               detail=f"`{agent.command}` is not on PATH")

        # The exit code is the whole answer. `capture=False` sends both streams to
        # /dev/null, so whatever an auth command prints about the account never
        # reaches this process at all -- not to be parsed, and not to sit in memory
        # waiting for a traceback to carry it somewhere.
        result = await run_process(
            [command, "login", "status"], None, timeout_s=PREFLIGHT_TIMEOUT_S, capture=False
        )
        ok = result.returncode == 0
        return AgentStatus(
            agent.agent_id, installed=True, authenticated=ok, detail=None if ok else "not logged in"
        )

    async def start(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        session_id: str | None = None,
    ) -> AdapterResult:
        # Codex assigns its own thread id, so ours is ignored here rather than passed:
        # what binds the consultation is the id it reports back in `thread.started`.
        return await self._run(agent, prompt, source_mode, resume=None)

    async def resume(
        self,
        agent: AgentConfig,
        native_session_id: str,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
    ) -> AdapterResult:
        return await self._run(agent, prompt, source_mode, resume=native_session_id)

    async def _run(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        resume: str | None,
    ) -> AdapterResult:
        if source_mode is SourceMode.WEB and not agent.web_search:
            raise AdapterError(
                ConsultErrorCode.WEB_SEARCH_UNAVAILABLE,
                f"agent `{agent.agent_id}` is not configured for web search",
            )

        command = resolve_command(agent)
        # A scratch cwd, not the user's repository: `--sandbox read-only` still reads,
        # and an empty directory is nothing to read. `CODEX_HOME` stays where it is --
        # relocating it would take the saved credentials out of scope and turn every
        # consultation into a login prompt.
        with tempfile.TemporaryDirectory(prefix="consult-codex-") as scratch:
            schema = Path(scratch) / "consult-schema.json"
            schema.write_text(json.dumps(consultation_content_schema()))

            argv = [command, "exec"]
            if resume:
                argv += ["resume", resume]
            argv += [
                "--strict-config",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--model",
                agent.model,
                "--json",
                "--output-schema",
                str(schema),
                # Both as config keys rather than `--sandbox` / `--ask-for-approval`.
                # `codex exec resume` accepts neither flag -- it takes a much smaller
                # set than `codex exec` does -- and passing them makes every follow-up
                # turn die on argument parsing. `-c` it takes, on both forms, so one
                # argv serves both.
                "-c",
                'sandbox_mode="read-only"',
                "-c",
                'approval_policy="never"',
                "-c",
                "agents.enabled=false",
                "-c",
                "features.shell_tool=false",
                "-c",
                f"web_search={'live' if source_mode is SourceMode.WEB else 'disabled'}",
            ]
            if agent.reasoning_effort:
                # Not inherited from the user's config.toml, which `--ignore-user-config`
                # is there to exclude. Unset means the model's own default.
                argv += ["-c", f'model_reasoning_effort="{agent.reasoning_effort}"']
            # Read the prompt from stdin: no argv ceiling, and no shell to quote for.
            argv.append("-")

            # Where the session log ends *before* this turn runs, so what the turn
            # appends can be told apart from what an earlier one left behind. Taken
            # here rather than after the run, which would be after the append.
            offsets = _rollout_offsets(resume)

            # Codex has no `--system-prompt`, so the protocol contract travels with the
            # payload. It stays first, which is what keeps a task from reading as one.
            result = await run_process(
                argv, prompt.full_text, self.timeout_s, env=child_env(), cwd=scratch
            )

        events = _events(result)
        for event in events:
            _check_event(event, source_mode)

        # Checked before any message is extracted, and both together: a run that
        # reported a failed turn or exited nonzero did not answer, however
        # well-formed something earlier in the stream looks. Reading the message
        # anyway would return a partial or abandoned answer as a successful one.
        _check_outcome(events, result)

        thread_id = _thread_id(events) or resume
        if not thread_id:
            raise AdapterError(
                ConsultErrorCode.TRANSPORT_ERROR, "the agent returned no thread id to resume"
            )

        text = _final_message(events)
        if text is None:
            detail = (result.stderr or result.stdout).strip()[:400]
            raise AdapterError(
                ConsultErrorCode.TRANSPORT_ERROR,
                f"the agent returned no final message (exit {result.returncode}): {detail}",
            )

        # The stream first, the session log only if it said nothing. Kept in a name so
        # the envelope can say whether anything was checked at all, rather than
        # reporting the configured model and an unverified one identically.
        reported = _reported_model(events) or _rollout_model(thread_id, offsets)
        return AdapterResult(
            content=parse_content(text),
            native_session_id=thread_id,
            model_used=check_model(agent, reported),
            model_verified=reported is not None,
            raw_output=result.stdout,
            usage=_usage(events),
        )


# --- JSONL parsing ----------------------------------------------------------


def _events(result: ProcessResult) -> list[dict]:
    events = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a diagnostic on stdout is not a protocol failure
        if isinstance(event, dict):
            events.append(event)
    if not events:
        detail = (result.stderr or result.stdout).strip()[:400]
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent produced no JSONL events (exit {result.returncode}): {detail}",
        )
    return events


def _kind(event: dict) -> str:
    """The event's type, plus the item's if it carries one, lowercased."""
    item = event.get("item")
    item_type = item.get("type", "") if isinstance(item, dict) else ""
    return f"{event.get('type', '')}.{item_type}".lower()


def _check_event(event: dict, source_mode: SourceMode) -> None:
    forbidden = FORBIDDEN_EVENT_MARKERS
    if source_mode is not SourceMode.WEB:
        forbidden += NETWORK_EVENT_MARKERS

    kind = _kind(event)
    if any(marker in kind for marker in forbidden):
        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            f"the agent tried to act rather than answer (event `{event.get('type')}`)",
        )


def _check_outcome(events: list[dict], result: ProcessResult) -> None:
    """Refuse a run the CLI itself called failed."""
    for event in events:
        kind = _kind(event)
        if any(marker in kind for marker in FAILURE_EVENT_MARKERS):
            detail = str(event.get("error") or event.get("message") or event.get("type"))[:400]
            raise AdapterError(
                ConsultErrorCode.AGENT_UNAVAILABLE,
                f"the agent reported a failed turn: {detail}",
            )
    if result.returncode != 0:
        raise AdapterError(
            ConsultErrorCode.AGENT_UNAVAILABLE,
            f"the agent exited {result.returncode}: {result.stderr.strip()[:400]}",
        )


def _thread_id(events: list[dict]) -> str | None:
    for event in events:
        if "thread" in str(event.get("type", "")).lower():
            for key in ("thread_id", "session_id", "id"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _final_message(events: list[dict]) -> str | None:
    """The last agent message, wherever the event happens to keep its text."""
    for event in reversed(events):
        if "agent_message" not in _kind(event):
            continue
        item = event.get("item")
        for holder in (item if isinstance(item, dict) else {}, event):
            for key in ("text", "message", "content"):
                value = holder.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return None


def _reported_model(events: list[dict]) -> str | None:
    for event in events:
        for holder in (event, event.get("item") if isinstance(event.get("item"), dict) else {}):
            model = holder.get("model")
            if isinstance(model, str) and model:
                return model
    return None


def _rollout_paths(thread_id: str | None) -> list[Path]:
    """The CLI's own session logs for this thread, if we can name them safely.

    Dated directories, three deep -- bounded rather than `rglob`, which would walk every
    session ever recorded to find one file.
    """
    # `CODEX_HOME` is not in `PASSTHROUGH_ENV`, so the child used its default, which is
    # anchored to the `HOME` we did pass. Asking `os.environ` here would be asking about
    # this process instead of the one that wrote the file.
    home = child_env().get("HOME")
    if not home or not thread_id or not _SESSION_ID.fullmatch(thread_id):
        # A thread id is a UUID. Anything else came from a CLI we do not recognise, and
        # it is about to be pasted into a glob pattern.
        return []
    try:
        return list(Path(home).glob(f".codex/sessions/*/*/*/rollout-*-{thread_id}.jsonl"))
    except OSError:
        return []


def _rollout_offsets(thread_id: str | None) -> dict[Path, int]:
    """Where each session log ends before a turn runs.

    Only a resumed thread has one. A new thread's file does not exist yet, so everything
    that ends up in it belongs to the turn about to run and the empty mapping is right.
    """
    offsets = {}
    for path in _rollout_paths(thread_id):
        try:
            offsets[path] = path.stat().st_size
        except OSError:
            continue
    return offsets


def _rollout_model(thread_id: str, offsets: dict[Path, int]) -> str | None:
    """The model this thread actually ran on, read from the CLI's own session log.

    `--json` on codex-cli 0.146 names the model nowhere: every event it prints is a
    `thread.started`, a `turn.started`, an `item.completed` or a `turn.completed`, and
    none of them carries one. So the substitution check had nothing to check and passed
    every answer through -- a guard that cannot fire is not a guard, it is a comment.

    The CLI does record it, in the rollout file it writes for its own resume support:
    `turn_context.model`, one per turn. Reading it back is the only way this process can
    say which model answered rather than which model it asked for.

    Only past `offsets`, which is what makes the answer belong to the turn that just ran.
    A resumed consultation appends to a file that already names a model, and reading the
    whole file would let turn N vouch for turn N+1 -- so a build that renames the record,
    moves the field, or writes somewhere else would go on reporting the previous turn's
    model as verified rather than reporting nothing.

    Nothing found is `None`, which lands on the same "no metadata is not evidence" path
    as before: this makes the check work where it can, it does not invent a failure where
    it cannot.
    """
    model = None
    for path in _rollout_paths(thread_id):
        try:
            # Bytes, not text: text mode decodes eagerly, and one bad byte anywhere in a
            # log we do not own would raise mid-iteration rather than skipping the line
            # it is on. It also makes the offset a plain byte count rather than a
            # `tell()` cookie, which is the only thing a text stream may be seeked to.
            with path.open("rb") as handle:
                handle.seek(offsets.get(path, 0))
                for raw in handle:
                    # Streamed, not read whole: these files hold the entire conversation
                    # and the reasoning behind it, and only one field is wanted.
                    if b'"turn_context"' not in raw:
                        continue
                    try:
                        # Strict, not `errors="replace"`: a line we cannot decode is a
                        # line we do not understand, and replacement characters would
                        # turn it into a model name that merely looks like one.
                        # `UnicodeDecodeError` and `JSONDecodeError` are both
                        # `ValueError`, so one clause covers reading and parsing.
                        record = json.loads(raw.decode())
                    except ValueError:
                        continue
                    # Every hop checked: a line that is a JSON array, or a `payload` that
                    # is a string, is a format we do not recognise -- which is the case
                    # this exists to survive, not a case to raise `AttributeError` on.
                    payload = record.get("payload") if isinstance(record, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    found = payload.get("model")
                    if isinstance(found, str) and found:
                        model = found
        except OSError:
            continue  # one unreadable file is not a reason to ignore the others
    return model


def rate_limit() -> dict | None:
    """How much of the Codex subscription window this account has spent.

    Not from an API -- there is no key here and there is not going to be one. The CLI
    records what the service told it in the same session log the model check reads, as
    the `rate_limits` of a `token_count` event, and the newest file is the newest answer.

    Worth surfacing because the question people ask before starting a 300-second review
    is whether they can afford one, and this is the only place on the machine that knows.
    Stale by definition -- it is whatever the newest Codex CLI session was told --
    and `None` when no session has reported it, which is not an error.
    """
    home = child_env().get("HOME")
    if not home:
        return None
    try:
        paths = list(Path(home).glob(".codex/sessions/*/*/*/rollout-*.jsonl"))
        newest = max(paths, key=lambda path: path.stat().st_mtime, default=None)
    except (OSError, ValueError):
        return None
    if newest is None:
        return None

    limits = None
    try:
        with newest.open("rb") as handle:
            for raw in handle:
                if b'"rate_limits"' not in raw:
                    continue
                try:
                    record = json.loads(raw.decode())
                except ValueError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                found = payload.get("rate_limits") if isinstance(payload, dict) else None
                # The last one in the file: a session that ran several turns was told
                # the number several times, and only the final one is current.
                if isinstance(found, dict):
                    limits = found
    except OSError:
        return None
    if not isinstance(limits, dict):
        return None

    # Only the window that is actually enforced, flattened. `secondary` exists on some
    # plans and is null on this one, so it is read the same tolerant way as everything
    # else here: present and well-shaped, or absent.
    primary = limits.get("primary")
    if not isinstance(primary, dict) or not isinstance(primary.get("used_percent"), (int, float)):
        return None
    return {
        "used_percent": float(primary["used_percent"]),
        "window_minutes": primary.get("window_minutes"),
        "resets_at": primary.get("resets_at"),
        "plan_type": limits.get("plan_type"),
    }


@accounted
def _usage(events: list[dict]) -> Usage:
    for event in reversed(events):
        raw: Any = event.get("usage")
        if not isinstance(raw, dict):
            continue
        # `cached_input_tokens` and `reasoning_output_tokens` ride alongside these two
        # as breakdowns *of* them, not additions to them -- adding either would count
        # the same tokens twice. The other adapters have to add their cache and
        # thinking figures because those runtimes report them disjoint; this one does
        # not, and the difference is the whole reason `Usage` states a meaning.
        #
        # Measured, not assumed. A consultation is a fresh single-shot invocation and
        # never hits cache, so every `cached_input_tokens` this server has ever stored
        # is 0 and none of them can tell the two readings apart. 21,164 usage envelopes
        # from this machine's own interactive Codex rollouts can: all 21,164 report a
        # `total_tokens` equal to `input_tokens + output_tokens`, none equal to
        # `input_tokens + cached_input_tokens + output_tokens`, and `cached_input_tokens`
        # never once exceeds `input_tokens` -- it reaches exactly it and stops.
        prompt_tokens = usage_any(raw.get("input_tokens"), raw.get("prompt_tokens"))
        completion_tokens = usage_any(raw.get("output_tokens"), raw.get("completion_tokens"))
        # Checked rather than used: `input + output` is what those 21,164 samples say a
        # Codex total covers. Conditional, though -- the `exec` event this adapter reads
        # carries no total today, only the rollout envelopes do, so this fires only if
        # one appears. What the measurement settles is the versions installed on one
        # machine when it was taken, and a later CLI that moved the cache out of the
        # input while still reporting no total is exactly what it cannot speak to.
        check_reported_total(raw.get("total_tokens"), prompt_tokens + completion_tokens, "codex")
        # The part of the same claim that holds without a total to check against,
        # which on this event is every turn.
        check_cache_is_a_breakdown(
            raw.get("cached_input_tokens"), raw.get("input_tokens"), raw.get("prompt_tokens")
        )
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            # Derived rather than read, like every other adapter: a CLI total counts
            # whatever that CLI counts, and the rollups have to add these across agents.
            total_tokens=prompt_tokens + completion_tokens,
        )
    return Usage()
