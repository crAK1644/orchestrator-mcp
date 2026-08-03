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
import os
import shutil
import signal
from dataclasses import dataclass, field
from typing import Protocol

from ..config import AgentConfig
from ..contract import ConsultationContent, RequiredAction, SourceMode
from ..errors import ConsultErrorCode
from ..prompts import CompiledPrompt
from ...contract import Usage

# How long a child gets to exit on SIGTERM before the group is killed outright.
GRACE_S = 2.0

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

    async def preflight(self, agent: AgentConfig) -> AgentStatus: ...

    async def start(
        self, agent: AgentConfig, prompt: CompiledPrompt, source_mode: SourceMode
    ) -> AdapterResult: ...

    async def resume(
        self,
        agent: AgentConfig,
        native_session_id: str,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
    ) -> AdapterResult: ...


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


async def run_process(
    argv: list[str],
    stdin_text: str | None,
    timeout_s: float,
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> ProcessResult:
    """Run one child to completion, or kill its whole group trying."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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

    payload = stdin_text.encode() if stdin_text is not None else None
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await process.communicate(payload)
    except (TimeoutError, asyncio.CancelledError) as exc:
        # Cancellation too: an outer deadline cancelling this coroutine must not
        # leave a CLI running against the user's account with nobody reading it.
        await _terminate(process)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AdapterError(
            ConsultErrorCode.TIMEOUT,
            f"`{argv[0]}` did not answer within {timeout_s:g}s and was terminated",
        ) from exc

    return ProcessResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode(errors="replace"),
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
