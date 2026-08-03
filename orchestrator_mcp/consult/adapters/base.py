"""The transport every consult adapter shares.

Three rules hold for both runtimes, and they are here rather than duplicated in
each adapter because getting any of them wrong in one place would be enough:

`create_subprocess_exec` with an argument list, never a shell. The prompt goes in
over stdin, which removes both the injection surface and the argv length ceiling
-- a document-mode context is routinely longer than `ARG_MAX`.

A curated environment, not `os.environ`. This server is configured with API keys
for the LiteLLM path, and a consulted CLI has no business seeing them; it
authenticates with its own saved credentials, which is what `HOME` is for.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from ..config import AgentConfig
from ..contract import ConsultationContent, RequiredAction, SourceMode
from ..errors import ConsultErrorCode
from ..prompts import CompiledPrompt
from ...contract import Usage

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
# that are tedious to diagnose. Notably absent: every `*_API_KEY` this server
# holds for the LiteLLM path.
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


def check_model(agent: AgentConfig, reported: str | None) -> str:
    """Reject a substituted model, but only when the runtime actually said which.

    Containment, not equality, and in both directions: an operator configures `opus`
    and the CLI reports `claude-opus-5`. What this is for is the runtime's own silent
    fallback -- an answer from a model nobody chose, presented as the one they did.

    Containment alone is not enough, because a fallback is usually a *smaller sibling*
    and its name usually contains the bigger one's: `gpt-5` sits inside `gpt-5-mini`.
    So the variant tokens have to agree exactly as well -- one name claiming `mini`
    when the other does not is a different model, not a longer spelling of the same
    one.
    """
    if not reported:
        # No metadata is not evidence of substitution. The spec asks for this check
        # "when reliable model metadata is available", and inventing a failure from
        # its absence would make every quiet release of either CLI an outage.
        return agent.model

    configured, actual = agent.model.strip().lower(), reported.strip().lower()
    family_agrees = configured in actual or actual in configured
    variants_agree = VARIANT_TOKENS.intersection(_tokens(configured)) == VARIANT_TOKENS.intersection(
        _tokens(actual)
    )
    if not (family_agrees and variants_agree):
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


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await stream.read(_CHUNK):
        total += len(chunk)
        if total > cap:
            raise _OutputTooLarge
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
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR, f"could not start `{argv[0]}`: {exc}"
        ) from exc

    # Read both streams under a cap rather than `communicate()`, which buffers
    # whatever a child chooses to send. Two tasks, because draining only one of a
    # pair of pipes deadlocks against the buffer nobody is emptying.
    readers = [
        asyncio.create_task(_read_capped(stream, MAX_OUTPUT_BYTES))
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
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"`{argv[0]}` produced more than {MAX_OUTPUT_BYTES // 1024 // 1024} MiB "
                "instead of an answer, and was terminated",
            ) from exc
        if isinstance(exc, TimeoutError):
            raise AdapterError(
                ConsultErrorCode.TIMEOUT,
                f"`{argv[0]}` did not answer within {timeout_s:g}s and was terminated",
            ) from exc
        raise

    stdout, stderr = captured if capture else (b"", b"")
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
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR, f"could not start `{argv[0]}`: {exc}"
        ) from exc

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    # Drained in the background: a child that fills the stderr pipe while we are
    # reading stdout would deadlock against a buffer nobody is emptying.
    stderr_task = asyncio.create_task(process.stderr.read())
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
            total = 0
            async for raw in process.stdout:
                total += len(raw)
                if total > MAX_OUTPUT_BYTES:
                    raise _OutputTooLarge
                lines.append(raw.decode(errors="replace"))
                if not on_line(lines[-1]):
                    stopped = True
                    break

            if stopped:
                await _terminate(process)
            else:
                await process.wait()
    except BaseException as exc:
        # Everything, not only the timeout: `on_line` rejecting an event is a normal
        # outcome here, and a child left running after it would keep spending the
        # user's account with nobody reading the answer.
        await _terminate(process)
        stderr_task.cancel()
        if isinstance(exc, TimeoutError):
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

    stderr = await stderr_task
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
    _signal_group(process, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


def _signal_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), sig)
