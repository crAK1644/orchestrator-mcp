"""Consulting an Antigravity (`agy`) CLI.

Written against `agy` 1.1.10 and the spike in `docs/antigravity-spike.md`, which is
where the reasons for the three unusual things in this module are recorded in full.

**The prompt travels in argv, not on stdin.** That breaks the transport invariant
every other adapter here relies on: `agy` does not read stdin (`-p ""` is an "empty
prompt" error, `-p -` answers the dash), and it has no `--prompt-file`. Linux caps a
single argv value at 128 KiB (`MAX_ARG_STRLEN`, not raisable), which is far below this
project's 1 MB context limit. Rather than silently shrink that limit, a prompt too
large for one argument is split across turns of one `--conversation` and reassembled
by the model. Measured at 6/6 verbatim canary recall with canaries buried mid-fragment,
which is evidence the mechanism works and not a guarantee -- hence the explicit
reassembly instruction on every part.

**`--output-format stream-json`, never `json`.** In `json` mode `--json-schema`
produces prose *followed by* the object, which `parse_content` refuses. The stream
carries `result.structured_output` -- clean, schema-conformant, and separate from the
prose-contaminated `response` -- plus `init.model`, which is the only place this
runtime names the model that answered.

**Terminal status is not trustworthy.** A tool call that headless mode auto-denies
ends the run with exit 0, `status: "SUCCESS"`, an empty `response` and a null
`structured_output`. So the answer is validated on its own, not on the status beside
it. Failures arrive the same way -- a `result` event on *stdout* with exit 1 and empty
stderr -- so nothing here reads `stderr` to decide an outcome.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from ...contract import Usage
from ..config import AgentConfig
from ..contract import RequiredAction, SourceMode, consultation_content_schema
from ..errors import ConsultErrorCode
from ..prompts import CompiledPrompt
from .base import (
    AdapterError,
    AdapterResult,
    AgentStatus,
    ProcessResult,
    check_model,
    child_env,
    parse_content,
    resolve_command,
    run_streaming,
)

# The most this adapter will put in a single `-p` value. Linux's per-argument ceiling
# is 131_072 bytes; the remainder is headroom for the fragment wrapper, which is added
# on top of the body.
MAX_ARG_BYTES = 100_000

# Fragments of one oversized message. The instruction is repeated on every part rather
# than stated once, because part 1 is the only one the model has read when it arrives
# and a later part must not read as a fresh task.
FRAGMENT_TEMPLATE = """\
[CONSULT FRAGMENT {index} OF {total}]
This is part {index} of a single message too large to send at once. Store it verbatim
and do not answer yet: the protocol contract, the task, and any source material are
split across all {total} parts.
Reply with exactly: ACK {index}
--- BEGIN PART {index} ---
{body}
--- END PART {index} ---"""

FINAL_TEMPLATE = """\
All {total} parts have now been delivered. Reassemble them in order into the single
message they form, then answer that message under the Consult Protocol v1 contract
stated in part 1. Return only the required structured response."""

# Substrings that mark a failure as being about credentials. Matched so the runtime's
# own text can be dropped instead of recorded: this server never reads, stores, or
# parses this runtime's credential, and an error quoting one back would do all three
# by accident. Deliberately narrow -- `token` is absent because every usage line
# contains it.
AUTH_MARKERS = (
    "unauthenticated",
    "unauthorized",
    "not logged in",
    "log in again",
    "login required",
    "please log in",
    "re-authenticate",
    "reauthenticate",
    "credential",
    "oauth",
    "401",
)


class AntigravityCliAdapter:
    runtime = "antigravity"

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def connect_command(self, agent: AgentConfig) -> str:
        # There is no `login` verb: `agy` has no login/logout/auth subcommand at all,
        # and authenticates on its first interactive launch. Logging out is the
        # in-session `/logout`. So the command to hand a user is the bare binary.
        return agent.command

    async def preflight(self, agent: AgentConfig) -> AgentStatus:
        try:
            resolve_command(agent)
        except AdapterError:
            return AgentStatus(agent.agent_id, installed=False, authenticated=False,
                               detail=f"`{agent.command}` is not on PATH")

        # Installed is all that can honestly be reported. There is no auth probe to
        # run -- no `login status` equivalent, and `agy models` exits 0 either way --
        # so this claims readiness it has not checked, and says so. The alternative,
        # reporting `authenticated=False`, would make every working agent look broken.
        # The first consultation is what surfaces a logged-out account.
        return AgentStatus(
            agent.agent_id,
            installed=True,
            authenticated=True,
            detail="authentication is not verifiable for this runtime; it has no auth probe",
        )

    async def start(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        session_id: str | None = None,
    ) -> AdapterResult:
        # Our id is ignored rather than passed: whether `--conversation` accepts an id
        # for a conversation that does not exist yet is unverified, and guessing wrong
        # would either fail every first turn or silently start an unbound session. The
        # id `agy` reports back is what binds the consultation.
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
        if source_mode is SourceMode.WEB:
            # Unconditionally, not `if not agent.web_search`: this adapter does not
            # offer web mode at all, so an operator who sets `web_search: true` on an
            # antigravity agent must still be refused rather than served a model-mode
            # answer under a web-mode contract.
            raise AdapterError(
                ConsultErrorCode.WEB_SEARCH_UNAVAILABLE,
                f"agent `{agent.agent_id}` runs on the antigravity runtime, which this "
                "server does not offer web mode on",
            )

        command = resolve_command(agent)
        # One deadline for the whole consultation, not one per subprocess: a chunked
        # prompt runs several children, and giving each the full `timeout_s` would
        # multiply the caller's timeout by the number of fragments.
        deadline = time.monotonic() + self.timeout_s

        # A scratch cwd for the same reason as Codex -- nothing to read -- though it is
        # not what contains this runtime: `agy` announces a cwd reset to the user's
        # repository on every run, and what actually stops it acting is the headless
        # permission default. See the spike; that is a risk this adapter documents
        # rather than a guarantee it enforces.
        with tempfile.TemporaryDirectory(prefix="consult-agy-") as scratch:
            schema = Path(scratch) / "consult-schema.json"
            schema.write_text(json.dumps(consultation_content_schema()))

            conversation, reported, usage = resume, None, Usage()
            raw: list[str] = []
            envelope: dict = {}

            for text, answering in _turns(prompt.full_text):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterError(
                        ConsultErrorCode.TIMEOUT,
                        f"the agent did not finish reading the prompt within {self.timeout_s:g}s",
                    )

                argv = [
                    command,
                    "--output-format",
                    "stream-json",
                    "--model",
                    agent.model,
                    "--sandbox",
                    # Stops slash and skill expansion, so nothing in a task or a
                    # document can invoke one by starting a line with `/`.
                    "--disable-slash-commands",
                    # The CLI's own bound, kept under ours so it cannot outlive the
                    # deadline. `asyncio.timeout` stays authoritative.
                    "--print-timeout",
                    f"{max(1, int(remaining))}s",
                ]
                # Never `--effort`: every model slug this runtime offers already carries
                # its reasoning level, and passing both is a hard error from the CLI.
                # Never `--dangerously-skip-permissions`, which is the one flag that
                # would let a consultation run commands against the user's machine.
                if conversation:
                    argv += ["--conversation", conversation]
                if answering:
                    argv += ["--json-schema", str(schema)]
                argv += ["-p", text]

                events, result = await self._turn(argv, remaining, scratch)
                raw.append(result.stdout)

                envelope = _result_event(events, result)
                _check_status(agent, envelope, result)

                reported = _reported_model(events) or reported
                # Every turn, not just the last: a chunked consultation that silently
                # switched model partway has still been answered by a model nobody
                # chose, and only the turn it happened on reports it.
                check_model(agent, reported)

                conversation = _conversation_id(events, envelope) or conversation
                usage = _add(usage, _usage(envelope))

                if not answering and not str(envelope.get("response") or "").strip():
                    # A fragment turn is supposed to acknowledge. Silence here is what
                    # an auto-denied tool call looks like -- exit 0, SUCCESS, empty --
                    # and continuing would build an answer on a part that never landed.
                    raise AdapterError(
                        ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                        "the agent acknowledged none of a fragment of the prompt, so the "
                        "consultation it would answer is incomplete",
                    )

        structured = envelope.get("structured_output")
        if not structured:
            # Checked independently of `status`, which reports SUCCESS for a run that
            # produced nothing at all. `response` is deliberately not the fallback: it
            # carries prose around the JSON, which is what makes it unparseable.
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"the agent returned no structured output (status "
                f"{str(envelope.get('status'))[:40]!r}), so there is no answer to record",
            )
        if not conversation:
            raise AdapterError(
                ConsultErrorCode.TRANSPORT_ERROR, "the agent returned no conversation id to resume"
            )

        return AdapterResult(
            content=parse_content(
                structured if isinstance(structured, str) else json.dumps(structured)
            ),
            native_session_id=conversation,
            model_used=check_model(agent, reported),
            model_verified=reported is not None,
            raw_output="".join(raw),
            usage=usage,
        )

    async def _turn(
        self, argv: list[str], timeout_s: float, cwd: str
    ) -> tuple[list[dict], ProcessResult]:
        events: list[dict] = []

        def on_line(line: str) -> bool:
            line = line.strip()
            if not line:
                return True
            try:
                event = json.loads(line)
            except ValueError:
                return True  # a diagnostic on stdout is not a protocol failure
            if isinstance(event, dict):
                events.append(event)
                # Raising from here is what kills the child: `run_streaming` terminates
                # the group on any exception out of this callback, so a run that starts
                # acting is stopped mid-tool rather than after it finishes.
                _check_event(event)
            return True

        # No stdin text: this runtime does not read it, and the prompt is already in
        # argv. Closing it immediately is what `run_streaming` does with `None`.
        result = await run_streaming(argv, None, timeout_s, on_line, env=child_env(), cwd=cwd)
        return events, result


# --- transport --------------------------------------------------------------


def _turns(text: str) -> list[tuple[str, bool]]:
    """The message as (argv text, is the answering turn) pairs.

    One turn when it fits, which is the ordinary case. Otherwise every fragment is
    delivered on its own turn and a final turn asks for the answer, so the schema is
    applied once -- to the turn that is meant to produce JSON, and not to the ones
    that are meant to produce an ACK.
    """
    parts = _fragments(text, MAX_ARG_BYTES)
    if len(parts) == 1:
        return [(parts[0], True)]
    total = len(parts)
    turns = [
        (FRAGMENT_TEMPLATE.format(index=index, total=total, body=body), False)
        for index, body in enumerate(parts, start=1)
    ]
    return turns + [(FINAL_TEMPLATE.format(total=total), True)]


def _fragments(text: str, limit: int) -> list[str]:
    """Split on encoded size, never mid-codepoint.

    The ceiling is a byte count, so the split has to be one too -- a character count
    would have to assume 4 bytes each to stay safe and would quadruple the number of
    turns for the ASCII that most prompts are.
    """
    raw = text.encode()
    if len(raw) <= limit:
        return [text]

    parts, start = [], 0
    while start < len(raw):
        end = min(start + limit, len(raw))
        # Continuation bytes are `10xxxxxx`; back off until `end` starts a character.
        while end < len(raw) and raw[end] & 0xC0 == 0x80:
            end -= 1
        parts.append(raw[start:end].decode())
        start = end
    return parts


# --- parsing ----------------------------------------------------------------


def _check_event(event: dict) -> None:
    """Fail closed on the agent acting rather than answering.

    Keyed on a tool step actually happening, not on what the runtime says it can do:
    `init.tools` always advertises the full builtin surface -- `run_command`,
    `call_mcp_tool`, `write_to_file` -- so a check on availability would refuse every
    run, including the ones that never touch any of it.
    """
    if event.get("event") != "step_update":
        return
    step = event.get("step_update")
    if not isinstance(step, dict):
        return
    if str(step.get("step_type", "")).lower() == "tool":
        name = step.get("tool_name")
        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            "the agent tried to act rather than answer (tool "
            f"`{name if isinstance(name, str) and name else '<unnamed>'}`)",
        )


def _result_event(events: list[dict], result: ProcessResult) -> dict:
    for event in reversed(events):
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            return event["result"]
    detail = (result.stdout or result.stderr).strip()[:400]
    raise AdapterError(
        ConsultErrorCode.TRANSPORT_ERROR,
        f"the agent produced no result event (exit {result.returncode}): {detail}",
    )


def _check_status(agent: AgentConfig, envelope: dict, result: ProcessResult) -> None:
    """Refuse a turn the runtime itself called failed.

    An authentication failure is turned into a code and a command for the user rather
    than a quoted message: this server does not read this runtime's credential, and
    text that came from a failed login is the one place a fragment of one could arrive
    and be stored verbatim alongside every other consultation.
    """
    status = str(envelope.get("status") or "").upper()
    if status == "SUCCESS" and result.returncode == 0:
        return

    message = str(envelope.get("error") or envelope.get("response") or "")
    if any(marker in message.lower() for marker in AUTH_MARKERS):
        raise AdapterError(
            ConsultErrorCode.CONNECTION_REQUIRED,
            f"agent `{agent.agent_id}` is not authenticated. The runtime's own message is "
            "not recorded, because a credential error is the one place a credential can "
            "appear in it",
            RequiredAction(command=f"{agent.command}"),
        )
    raise AdapterError(
        ConsultErrorCode.AGENT_UNAVAILABLE,
        f"the agent reported {status or 'no status'} (exit {result.returncode}): "
        f"{message.strip()[:400]}",
    )


def _conversation_id(events: list[dict], envelope: dict) -> str | None:
    """The id to resume on, from wherever this release puts it.

    Both the `init` event and the `result` carry one; taking either means a build that
    stops emitting one of them still leaves a resumable consultation.
    """
    for holder in (envelope, *reversed(events)):
        value = holder.get("conversation_id")
        if isinstance(value, str) and value:
            return value
    return None


def _reported_model(events: list[dict]) -> str | None:
    for event in events:
        init = event.get("init")
        model = init.get("model") if isinstance(init, dict) else None
        if isinstance(model, str) and model:
            return model
    return None


def _usage(envelope: dict) -> Usage:
    raw: Any = envelope.get("usage")
    raw = raw if isinstance(raw, dict) else {}
    prompt_tokens = int(raw.get("input_tokens") or 0)
    # Thinking is generated and billed, so it belongs on the completion side rather
    # than in a field this envelope does not have.
    completion_tokens = int(raw.get("output_tokens") or 0) + int(raw.get("thinking_tokens") or 0)
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=int(raw.get("total_tokens") or 0) or prompt_tokens + completion_tokens,
        # No cost anywhere in this runtime's output, and no quota surface either.
    )


def _add(left: Usage, right: Usage) -> Usage:
    """Totals across the turns of one chunked consultation.

    Summed rather than taking the last turn's: every turn is a separately billed
    request, and a run that re-sent its history four times cost four times, however
    much of it was served from cache.
    """
    return Usage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )
