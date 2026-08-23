"""The transport every consult adapter shares.

Three rules hold for both runtimes, and they are here rather than duplicated in
each adapter because getting any of them wrong in one place would be enough:

`create_subprocess_exec` with an argument list, never a shell. The prompt goes in
over stdin, which removes both the injection surface and the argv length ceiling
-- a document-mode context is routinely longer than `ARG_MAX`.

A curated environment, not `os.environ`. The agent that started this server may
well have provider API keys in its own environment, and a consulted CLI has no
business seeing them; it authenticates with its own saved credentials, which is
what `HOME` is for.

Children run in their own process group, and a timeout kills the group. A CLI
that has spawned its own helpers would otherwise leave them running after we stop
waiting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from ...contract import Usage
from ...log import get_logger
from ..config import AgentConfig
from ..contract import ConsultationContent, RequiredAction, SourceMode
from ..errors import ConsultErrorCode
from ..prompts import CompiledPrompt

log = get_logger(__name__)

# How long a child gets to exit on SIGTERM before the group is killed outright.
GRACE_S = 2.0

# One event line can carry a whole answer, and the default 64 KiB would raise
# `LimitOverrunError` on it rather than returning a truncated line.
STREAM_LINE_LIMIT = 16 * 1024 * 1024

# The ceiling on everything one child may produce. A consultation's answer is a
# small JSON object; a runtime emitting tens of megabytes is looping, not
# answering, and reading all of it into this process is how a runaway CLI takes
# the server down with it.
MAX_OUTPUT_BYTES = 32 * 1024 * 1024

# Read in chunks so the cap is checked before the memory is committed, rather
# than after `communicate()` has already buffered whatever arrived.
_CHUNK = 64 * 1024

# Passed through to a consulted CLI. `HOME` because that is where its own saved
# credentials live, the rest because a process without them misbehaves in ways
# that are tedious to diagnose. Notably absent: every `*_API_KEY` that happened to
# be in the environment this server was started from.
PASSTHROUGH_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "TERM", "USER", "SHELL")


class AdapterError(Exception):
    """A transport failure with a code the envelope can carry."""

    def __init__(
        self,
        code: ConsultErrorCode,
        message: str,
        required_action: RequiredAction | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.required_action = required_action


@dataclass(frozen=True)
class AgentStatus:
    """What a preflight found. Never holds credential material -- `detail` is text
    this server writes, not output copied out of a login command."""

    agent_id: str
    installed: bool
    authenticated: bool
    detail: str | None = None

    @property
    def ready(self) -> bool:
        return self.installed and self.authenticated


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class AdapterResult:
    content: ConsultationContent
    native_session_id: str
    model_used: str
    raw_output: str
    usage: Usage = field(default_factory=Usage)
    # Whether a runtime actually named the model, or `model_used` is the configured name
    # passed through unchecked. Defaults to the honest answer for an adapter that has not
    # thought about it.
    model_verified: bool = False


class ConsultAdapter(Protocol):
    """Narrow on purpose: three verbs, and no way to ask for anything agentic."""

    runtime: str

    def connect_command(self, agent: AgentConfig) -> str:
        """What the *user* runs to log this runtime in. Never run by this server."""
        ...

    async def preflight(self, agent: AgentConfig) -> AgentStatus: ...

    async def start(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        session_id: str | None = None,
    ) -> AdapterResult: ...

    async def resume(
        self,
        agent: AgentConfig,
        native_session_id: str,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
    ) -> AdapterResult: ...


def _parse_count(value: Any) -> int | None:
    """`value` as a token count, or `None` if it does not read as one."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def usage_count(value: Any) -> int:
    """One token count off a runtime's usage envelope, or nothing.

    Never an exception: usage is reporting, and a `"N/A"` where a number was expected
    must not be what loses an answer that already arrived, validated, and was paid for.
    Shared rather than per-adapter because `Usage` derives its total from these, so
    every field it counts is a field that can end a consultation if it is read
    strictly -- and the runtimes disagree about which fields they even fill.

    An absent field is nothing and says so quietly; a field that is *present* and
    unreadable is a reporting failure, and the count returned for it is a guess that
    reads exactly like a measurement. It still returns, because losing a paid answer
    over its receipt is worse -- but it says so first, which is the whole difference
    between an incomplete number and a wrong one nobody can find.
    """
    if value is None:
        return 0
    count = _parse_count(value)
    if count is None:
        log.warning("usage: %r is not a token count; counting it as 0", value)
        return 0
    if count < 0:
        # Not clamped quietly. A negative token count is a runtime reporting a
        # quantity that cannot exist, and the zero substituted for it is this
        # function's invention, not that runtime's measurement.
        log.warning("usage: token count %d is negative; counting it as 0", count)
        return 0
    return count


def usage_any(*values: Any) -> int:
    """The first of these that reads as a count, for a field spelled more than one way.

    Not `usage_count(a or b)`: that resolves the alias by truthiness before anything
    checks whether the winner is a number, so a present-but-malformed first spelling
    takes the slot and the good second one is never reached.
    """
    for value in values:
        if _parse_count(value) is not None:
            return usage_count(value)
    return usage_count(values[-1]) if values else 0


def check_reported_total(reported: Any, expected: int, runtime: str) -> None:
    """Compare a runtime's own total against what that runtime's total should be.

    `Usage.total_tokens` is derived from the two parts and never read from here --
    each CLI totals a different set, which is why deriving it is the only way the
    rollups can add them. But a reported total is still a checksum over the fields
    beside it: it is how the parts were confirmed correct in the first place, and
    discarding it means the day a runtime adds a sixth token category, the derived
    total drifts and nothing anywhere notices.

    `expected` is what *that runtime* documents its total to cover, not the canonical
    one. Antigravity totals input and output alone; Opencode counts all four of its
    disjoint figures. Comparing either against the wrong one would warn on every
    healthy turn, which is the same as not warning at all.
    """
    count = _parse_count(reported)
    if count is not None and count != expected:
        log.warning(
            "usage: %s reported a total of %d where its own fields make %d; "
            "the derived total is unaffected, but a token category may be unread",
            runtime,
            count,
            expected,
        )


def parse_content(text: str) -> ConsultationContent:
    """Validate a reply against the contract, or refuse it.

    Fences are stripped first. Both CLIs are told to emit bare JSON through a schema
    flag, but a model that wraps it in ```json anyway has answered correctly and
    formatted badly, and failing that consultation would be pedantry.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[-1]
        stripped = body.rsplit("```", 1)[0].strip() if "```" in body else body.strip()

    try:
        payload = json.loads(stripped)
    except ValueError as exc:
        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            f"the agent did not return JSON: {exc}",
        ) from exc

    try:
        return ConsultationContent.model_validate(payload)
    except ValidationError as exc:
        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            f"the agent's reply does not match the consultation contract: {exc}",
        ) from exc


# Tokens that name a size, tier, or variant rather than a family. Two names that
# disagree on one of these are different models however much else they share, which
# is what plain containment cannot see: `gpt-5` is a substring of `gpt-5-mini`.
VARIANT_TOKENS = frozenset(
    {
        "mini", "nano", "small", "medium", "large", "lite", "micro",
        "haiku", "sonnet", "opus",
        "turbo", "flash", "pro", "max", "ultra", "thinking", "reasoning",
        "high", "low", "fast", "instant", "preview", "codex",
    }
)


def _tokens(name: str) -> list[str]:
    return [part for part in re.split(r"[^a-z0-9]+", name.strip().lower()) if part]


# A dated snapshot of the same model: `claude-sonnet-4-5-20250929`. Long enough that a
# version number cannot be mistaken for one -- `4` and `5` are versions, `20250929` is
# a date.
_SNAPSHOT = re.compile(r"^\d{6,}$")


def _same_model(configured: list[str], actual: list[str]) -> bool:
    """Whether two model names, tokenized, name the same model.

    Token runs rather than raw substrings, because a substring cannot see where a
    number ends: `gpt-5.1` sits inside `gpt-5.10`, which is a different model.

    What one name may add over the other depends on whether a version was configured.
    An operator who wrote a bare alias -- `opus` -- gave no version to disagree with,
    so anything around it is allowed. An operator who wrote `claude-sonnet-4-5` pinned
    one, and the only thing the runtime may add on the end is a snapshot date;
    `claude-sonnet-4` answering as `claude-sonnet-4-5` is a substitution.
    """
    if configured == actual:
        return True
    longer, shorter = (actual, configured) if len(actual) >= len(configured) else (configured, actual)
    width = len(shorter)
    for start in range(len(longer) - width + 1):
        if longer[start : start + width] != shorter:
            continue
        before, after = longer[:start], longer[start + width :]
        if any(part.isdigit() for part in before):
            continue  # a number in front of the match is part of some other version
        if not any(part.isdigit() for part in shorter):
            return True  # unversioned alias
        if all(_SNAPSHOT.match(part) for part in after):
            return True
    return False


def check_model(agent: AgentConfig, reported: str | None) -> str:
    """Reject a substituted model, but only when the runtime actually said which.

    Not equality: an operator configures `opus` and the CLI reports `claude-opus-5`,
    or pins `claude-sonnet-4-5` and the CLI reports today's snapshot of it. What this
    is for is the runtime's own silent fallback -- an answer from a model nobody
    chose, presented as the one they did.

    Two independent checks, because either alone lets a real fallback through. The
    names have to name the same model under `_same_model`, *and* their variant tokens
    have to agree exactly -- one name claiming `mini` when the other does not is a
    different model, not a longer spelling of the same one.
    """
    if not reported:
        # No metadata is not evidence of substitution. The spec asks for this check
        # "when reliable model metadata is available", and inventing a failure from
        # its absence would make every quiet release of either CLI an outage.
        return agent.model

    configured, actual = _tokens(agent.model), _tokens(reported)
    variants_agree = VARIANT_TOKENS.intersection(configured) == VARIANT_TOKENS.intersection(actual)
    # A name with no tokens in it -- `???` -- is not a model, and `_same_model` would
    # otherwise read the empty list as an unversioned alias and match anything.
    if not actual or not (_same_model(configured, actual) and variants_agree):
        raise AdapterError(
            ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE,
            f"agent `{agent.agent_id}` is configured for model `{agent.model}` but the "
            f"answer came from `{reported}`",
        )
    return reported


def resolve_command(agent: AgentConfig) -> str:
    """The absolute path of the configured executable, or a refusal naming it."""
    path = shutil.which(agent.command)
    if path is None:
        raise AdapterError(
            ConsultErrorCode.AGENT_NOT_INSTALLED,
            f"`{agent.command}` is not on PATH, so agent `{agent.agent_id}` cannot be consulted",
        )
    return path


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {name: os.environ[name] for name in PASSTHROUGH_ENV if name in os.environ}
    return env | (extra or {})


class _OutputTooLarge(Exception):
    """A child wrote past `MAX_OUTPUT_BYTES`."""


class _Budget:
    """One byte allowance shared by every stream of a child.

    Per-stream caps would let a child spend the limit twice, and the limit exists to
    bound this process's memory, which does not care which pipe the bytes arrived on.
    """

    def __init__(self, cap: int) -> None:
        self.left = cap

    def spend(self, count: int) -> None:
        self.left -= count
        if self.left < 0:
            raise _OutputTooLarge


async def _read_capped(stream: asyncio.StreamReader, budget: _Budget) -> bytes:
    chunks: list[bytes] = []
    while chunk := await stream.read(_CHUNK):
        budget.spend(len(chunk))
        chunks.append(chunk)
    return b"".join(chunks)


async def run_process(
    argv: list[str],
    stdin_text: str | None,
    timeout_s: float,
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    capture: bool = True,
) -> ProcessResult:
    """Run one child to completion, or kill its whole group trying.

    `capture=False` sends both streams to `/dev/null` and returns empty text. That
    is for the auth probes: their exit code is the whole answer, and output we do
    not need is output that must not reach this process's memory.
    """
    sink = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=sink,
            stderr=sink,
            env=env if env is not None else child_env(),
            cwd=cwd,
            # Its own process group, so the kill below reaches everything the CLI
            # started and not just the CLI.
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("could not start %s: %s", argv[0], exc)
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR, f"could not start `{argv[0]}`: {exc}"
        ) from exc

    # Only the executable and the flags: the prompt goes in over stdin and never
    # reaches a log line, so a `%s` of the whole argv cannot spill a consultation.
    log.debug("started %s pid=%d timeout=%gs", argv[0], process.pid, timeout_s)

    # Read both streams under a cap rather than `communicate()`, which buffers
    # whatever a child chooses to send. Two tasks, because draining only one of a
    # pair of pipes deadlocks against the buffer nobody is emptying.
    budget = _Budget(MAX_OUTPUT_BYTES)
    readers = [
        asyncio.create_task(_read_capped(stream, budget))
        for stream in (process.stdout, process.stderr)
        if stream is not None
    ]
    try:
        async with asyncio.timeout(timeout_s):
            if process.stdin is not None:
                if stdin_text is not None:
                    process.stdin.write(stdin_text.encode())
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        await process.stdin.drain()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    process.stdin.close()
            captured = await asyncio.gather(*readers)
            await process.wait()
    except BaseException as exc:
        # Cancellation and overflow as well as the timeout: an outer deadline, or a
        # child that will not stop talking, must not leave a CLI running against the
        # user's account with nobody reading it.
        for reader in readers:
            reader.cancel()
        await _terminate(process)
        if isinstance(exc, _OutputTooLarge):
            log.warning(
                "%s pid=%d exceeded the %d MiB output cap and was terminated",
                argv[0],
                process.pid,
                MAX_OUTPUT_BYTES // 1024 // 1024,
            )
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"`{argv[0]}` produced more than {MAX_OUTPUT_BYTES // 1024 // 1024} MiB "
                "instead of an answer, and was terminated",
            ) from exc
        if isinstance(exc, TimeoutError):
            log.warning(
                "%s pid=%d timed out after %gs and was terminated",
                argv[0],
                process.pid,
                timeout_s,
            )
            raise AdapterError(
                ConsultErrorCode.TIMEOUT,
                f"`{argv[0]}` did not answer within {timeout_s:g}s and was terminated",
            ) from exc
        raise

    stdout, stderr = captured if capture else (b"", b"")
    log.debug(
        "exited %s pid=%d rc=%d in %.1fs",
        argv[0],
        process.pid,
        process.returncode or 0,
        time.monotonic() - started,
    )
    return ProcessResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


async def run_streaming(
    argv: list[str],
    stdin_text: str | None,
    timeout_s: float,
    on_line: Callable[[str], bool],
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> ProcessResult:
    """Run a child, handing each stdout line to `on_line` as it arrives.

    `on_line` returning False stops the run and kills the group. That is the only
    way to bound a web-mode consultation from here: Claude Code 2.1.220 has no
    `--max-turns`, so the turn budget is counted in the event stream and enforced
    by ending the process, not by asking it to stop.
    """
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env if env is not None else child_env(),
            cwd=cwd,
            start_new_session=True,
            limit=STREAM_LINE_LIMIT,
        )
    except OSError as exc:
        log.warning("could not start %s: %s", argv[0], exc)
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR, f"could not start `{argv[0]}`: {exc}"
        ) from exc

    log.debug("started %s pid=%d timeout=%gs streaming", argv[0], process.pid, timeout_s)

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    # Drained in the background: a child that fills the stderr pipe while we are
    # reading stdout would deadlock against a buffer nobody is emptying. Against the
    # same budget as stdout, because a plain `read()` here would let a child spend the
    # output cap a second time on a stream nobody is even parsing.
    budget = _Budget(MAX_OUTPUT_BYTES)
    stderr_task = asyncio.create_task(_read_capped(process.stderr, budget))
    lines: list[str] = []

    try:
        async with asyncio.timeout(timeout_s):
            if stdin_text is not None:
                process.stdin.write(stdin_text.encode())
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.drain()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.close()

            stopped = False
            async for raw in process.stdout:
                budget.spend(len(raw))
                if stderr_task.done():
                    await stderr_task  # re-raises an overflow on the other stream
                lines.append(raw.decode(errors="replace"))
                if not on_line(lines[-1]):
                    stopped = True
                    break

            if stopped:
                await _terminate(process)
            else:
                await process.wait()
            # Inside the try, and inside the deadline: an overflow on stderr that the
            # loop above never saw -- because stdout said nothing more -- is still an
            # envelope, not an exception on the way out.
            stderr = await stderr_task
    except BaseException as exc:
        # Everything, not only the timeout: `on_line` rejecting an event is a normal
        # outcome here, and a child left running after it would keep spending the
        # user's account with nobody reading the answer.
        await _terminate(process)
        stderr_task.cancel()
        if isinstance(exc, TimeoutError):
            log.warning(
                "%s pid=%d timed out after %gs and was terminated",
                argv[0],
                process.pid,
                timeout_s,
            )
            raise AdapterError(
                ConsultErrorCode.TIMEOUT,
                f"`{argv[0]}` did not answer within {timeout_s:g}s and was terminated",
            ) from exc
        if isinstance(exc, _OutputTooLarge):
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"`{argv[0]}` produced more than {MAX_OUTPUT_BYTES // 1024 // 1024} MiB "
                "instead of an answer, and was terminated",
            ) from exc
        if isinstance(exc, ValueError):
            # A single line past `STREAM_LINE_LIMIT`. `StreamReader` raises a bare
            # `ValueError` for it, which would otherwise cross the MCP boundary as an
            # exception instead of a code the caller can branch on.
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"`{argv[0]}` emitted a single event larger than "
                f"{STREAM_LINE_LIMIT // 1024 // 1024} MiB, which is not an answer",
            ) from exc
        raise

    log.debug(
        "exited %s pid=%d rc=%d in %.1fs after %d events",
        argv[0],
        process.pid,
        process.returncode or 0,
        time.monotonic() - started,
        len(lines),
    )
    return ProcessResult(
        returncode=process.returncode or 0,
        stdout="".join(lines),
        stderr=stderr.decode(errors="replace"),
    )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """SIGTERM the group, then SIGKILL what is left.

    Term first so a CLI can close its session files; kill after, because "asked
    politely" is not a guarantee and a leaked child holds the account busy.
    """
    _signal_group(process, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(GRACE_S):
            await process.wait()
            return
    # Worth a line at WARNING: a group that outlived SIGTERM had helpers of its own,
    # and that is the case where something is left holding the account.
    log.warning("pid=%d did not exit within %gs of SIGTERM; killing group", process.pid, GRACE_S)
    _signal_group(process, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


def _signal_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    # `process.pid` *is* the group id: `start_new_session=True` makes the child the
    # leader of a new group with that number. Asking `os.getpgid` for it instead would
    # raise `ProcessLookupError` the moment the leader has exited -- which is exactly
    # the case worth killing, because a CLI that forked a worker and returned leaves
    # that worker holding the account with the group still alive.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)
