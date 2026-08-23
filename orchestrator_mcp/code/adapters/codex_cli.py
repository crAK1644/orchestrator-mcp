"""Codex, run inside its own sandbox, with permission to edit one directory.

Every flag here was run against codex-cli 0.147 before it was written down, and the
spike checked the thing that actually matters: a raw shell command aimed outside the
worktree comes back `Operation not permitted`. That is the kernel refusing, not the
model declining, which is the whole reason this runtime is the only one with a write
adapter.

Two findings from that spike shape the code:

  * **The event stream under-reports.** The denied command produced no event at all
    -- three commands ran, two were reported. So `commands` here is evidence about
    what a run tried, never the record of what it did. The record is the diff the
    service reads from git afterwards.
  * **`.git` of a worktree lives outside the worktree**, so the agent cannot commit
    or even `git add`. It leaves the work in the tree; the host records it.

The consult adapter's `FORBIDDEN_EVENT_MARKERS` is deliberately not reused: commands,
patches and file changes are the point here. What is kept is the rest of it -- a
failed turn, a nonzero exit, and the network markers, because web is off.

The JSONL shape is one CLI's, not one mode's, so the small parsing helpers are
imported from the consult adapter rather than copied. That direction of dependency
carries no capability: reading someone else's stdout is not a way to write anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...consult.adapters.base import (
    AdapterError,
    ProcessResult,
    check_model,
    check_reported_total,
    child_env,
    resolve_command,
    run_process,
    usage_any,
)
from ...consult.adapters.codex_cli import (
    FAILURE_EVENT_MARKERS,
    NETWORK_EVENT_MARKERS,
    _kind,
    _reported_model,
    _thread_id,
)
from ...consult.config import AgentConfig
from ...consult.errors import ConsultErrorCode
from ...consult.prompts import CompiledPrompt
from ...contract import Usage
from .base import AdapterRun, ObservedCommand

# How much of a command's output is kept per command. Enough to see what a failing
# test said, not enough for one chatty build to fill the row.
OUTPUT_TAIL_CHARS = 2000

# The containment itself. Written out as one list because it only means anything
# whole: `workspace-write` with approvals still on would stop and wait forever, and
# with network on it would be a sandbox around the filesystem only.
SANDBOX_CONFIG = (
    "-c", 'sandbox_mode="workspace-write"',
    # Nobody is at the terminal to approve an escalation, and a run that blocks on
    # one would sit there until the timeout killed it.
    "-c", 'approval_policy="never"',
    "-c", "agents.enabled=false",
    # On, unlike a consultation: an execution step is expected to run the tests it
    # writes. The sandbox is what bounds that, not the absence of a shell.
    "-c", "features.shell_tool=true",
    "-c", "web_search=disabled",
    "-c", "sandbox_workspace_write.network_access=false",
    # The default writable set includes /tmp and $TMPDIR. Excluded so "writable"
    # means the worktree and nothing else -- a step that leaves state in /tmp
    # between rounds is a step whose result is not reproducible from the diff.
    "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
    "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
)


class CodexWriteAdapter:
    """The one contained executor. No resume: a worktree does not outlive its step."""

    runtime = "codex"

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    async def execute(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        worktree: Path,
        timeout_s: float | None = None,
    ) -> AdapterRun:
        command = resolve_command(agent)
        argv = [
            command,
            "exec",
            "--strict-config",
            "--ignore-user-config",
            "--ignore-rules",
            # The worktree *is* a repository, but its `.git` is a file pointing at the
            # parent's, which the CLI's own check reads as unusual. The sandbox is what
            # bounds this run; the check is about advising a human at a terminal.
            "--skip-git-repo-check",
            "--model",
            agent.model,
            "--json",
            "-C",
            str(worktree),
            *SANDBOX_CONFIG,
        ]
        if agent.reasoning_effort:
            argv += ["-c", f'model_reasoning_effort="{agent.reasoning_effort}"']
        argv.append("-")

        result = await run_process(
            argv,
            prompt.full_text,
            timeout_s or self.timeout_s,
            env=child_env(),
            cwd=worktree,
        )

        events = _events(result)
        for event in events:
            _check_network(event)
        _check_outcome(events, result)

        reported = _reported_model(events)
        return AdapterRun(
            summary=_final_message(events) or "",
            commands=_commands(events),
            claimed_paths=_claimed_paths(events),
            native_session_id=_thread_id(events),
            model_used=check_model(agent, reported),
            model_verified=reported is not None,
            raw_output=result.stdout,
            usage=_usage(events),
        )


# --- reading the stream -----------------------------------------------------
#
# Tolerant in the same way the consult parser is, and for the same reason: where a
# release puts the text of an event is the part that moves.


def _events(result: ProcessResult) -> list[dict]:
    events = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    if not events:
        detail = (result.stderr or result.stdout).strip()[:400]
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent produced no JSONL events (exit {result.returncode}): {detail}",
        )
    return events


def _check_network(event: dict) -> None:
    """Notice the network coming back on.

    `network_access=false` is what prevents it; this is what says so if that key
    ever stops meaning what it means. Kept even though a run's own commands are
    allowed here -- reaching the internet is not one of the things being allowed.
    """
    if any(marker in _kind(event) for marker in NETWORK_EVENT_MARKERS):
        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            f"the agent reached the network with it disabled (event `{event.get('type')}`)",
        )


def _check_outcome(events: list[dict], result: ProcessResult) -> None:
    """A run the CLI called failed did not do the work, whatever the tree looks like.

    Raised before the service reads git, so a half-finished edit is never captured as
    a completed step. The worktree is thrown away either way.
    """
    for event in events:
        if any(marker in _kind(event) for marker in FAILURE_EVENT_MARKERS):
            detail = str(event.get("error") or event.get("message") or event.get("type"))[:400]
            raise AdapterError(
                ConsultErrorCode.AGENT_UNAVAILABLE, f"the agent reported a failed turn: {detail}"
            )
    if result.returncode != 0:
        raise AdapterError(
            ConsultErrorCode.AGENT_UNAVAILABLE,
            f"the agent exited {result.returncode}: {result.stderr.strip()[:400]}",
        )


def _final_message(events: list[dict]) -> str | None:
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


def _commands(events: list[dict]) -> list[ObservedCommand]:
    """What the stream said ran. A partial account -- see this module's docstring."""
    seen: list[ObservedCommand] = []
    for event in events:
        if "command" not in _kind(event):
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        holder: dict[str, Any] = {**event, **item}
        text = holder.get("command") or holder.get("cmd")
        if isinstance(text, list):
            text = " ".join(str(part) for part in text)
        if not isinstance(text, str) or not text.strip():
            continue
        exit_code = holder.get("exit_code")
        output = holder.get("aggregated_output") or holder.get("output") or ""
        seen.append(
            ObservedCommand(
                command=text.strip(),
                exit_code=exit_code if isinstance(exit_code, int) else None,
                output_tail=str(output)[-OUTPUT_TAIL_CHARS:],
            )
        )
    return seen


def _claimed_paths(events: list[dict]) -> list[str]:
    """Files the stream says were touched. Kept for reading beside the real diff."""
    paths: list[str] = []
    for event in events:
        kind = _kind(event)
        if "patch" not in kind and "file_change" not in kind:
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        changes = item.get("changes") or event.get("changes")
        if isinstance(changes, dict):
            paths.extend(str(key) for key in changes)
        elif isinstance(changes, list):
            for change in changes:
                path = change.get("path") if isinstance(change, dict) else change
                if isinstance(path, str):
                    paths.append(path)
    return sorted(set(paths))


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
        # The event this adapter reads carries no total, but the rollout envelopes above
        # do, so one may appear here. Checked rather than used: `input + output` is what
        # those 21,164 samples say a Codex total covers.
        check_reported_total(raw.get("total_tokens"), prompt_tokens + completion_tokens, "codex")
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            # Derived rather than read, like every other adapter: a CLI total counts
            # whatever that CLI counts, and the rollups have to add these across agents.
            total_tokens=prompt_tokens + completion_tokens,
        )
    return Usage()
