"""Consulting a Codex CLI.

Unlike the Claude adapter, none of this was checked against a binary -- codex is
not installed on the machine this was written on. The flags and event names come
from the `codex exec` documentation, the parser is deliberately tolerant about
where in an event the text lives, and `smoke_consult_live.py` is what actually
confirms them.

What is not tolerant is the event *kind*: a command, a file change, an MCP call
or a subagent means the CLI is doing something a consultation never asked for,
and that fails closed rather than being ignored.
"""

from __future__ import annotations

import json
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
    check_model,
    child_env,
    parse_content,
    resolve_command,
    run_process,
)

PREFLIGHT_TIMEOUT_S = 30.0

# Substrings that mark an event as an action rather than a message. Matched against
# the event type so a renamed-but-recognisable event still fails closed.
FORBIDDEN_EVENT_MARKERS = ("command", "exec_command", "patch", "file_change", "mcp", "subagent")

# Substrings that mark the run as having failed. Matched the same tolerant way, so
# `turn.failed`, `error`, and whatever the next release calls it all stop the turn.
FAILURE_EVENT_MARKERS = ("failed", "failure", "error", "aborted", "cancelled", "canceled")


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
                "--sandbox",
                "read-only",
                "--model",
                agent.model,
                "--json",
                "--output-schema",
                str(schema),
                # As a config key and not `--ask-for-approval`, which a 0.146 build
                # rejects as an unexpected argument on `exec`. Config keys outlive
                # flags, and this one is what the flag set anyway.
                "-c",
                'approval_policy="never"',
                "-c",
                "agents.enabled=false",
                "-c",
                "features.shell_tool=false",
                "-c",
                f"web_search={'live' if source_mode is SourceMode.WEB else 'disabled'}",
                # Read the prompt from stdin: no argv ceiling, and no shell to quote for.
                "-",
            ]

            # Codex has no `--system-prompt`, so the protocol contract travels with the
            # payload. It stays first, which is what keeps a task from reading as one.
            result = await run_process(
                argv, prompt.full_text, self.timeout_s, env=child_env(), cwd=scratch
            )

        events = _events(result)
        for event in events:
            _check_event(event)

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

        return AdapterResult(
            content=parse_content(text),
            native_session_id=thread_id,
            model_used=check_model(agent, _reported_model(events)),
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


def _check_event(event: dict) -> None:
    kind = _kind(event)
    hit = [marker for marker in FORBIDDEN_EVENT_MARKERS if marker in kind]
    if hit:
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


def _usage(events: list[dict]) -> Usage:
    for event in reversed(events):
        raw: Any = event.get("usage")
        if not isinstance(raw, dict):
            continue
        prompt_tokens = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
        completion_tokens = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=int(raw.get("total_tokens") or prompt_tokens + completion_tokens),
        )
    return Usage()
