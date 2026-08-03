"""Consulting a Claude Code CLI.

Every flag here was checked against the installed binary (2.1.220) rather than
taken from documentation, and two of the spec's assumptions did not survive that:
`--json-schema` takes inline JSON, not a path, and `--output-format stream-json`
is rejected without `--verbose`. The third is the one that shapes this module --
there is no `--max-turns`, so a web-mode consultation is bounded by counting
assistant turns in the event stream and killing the child, not by a flag.

The session id is ours: `--session-id` accepts a UUID we assign, so the
consultation id *is* the native session id and there is no id to parse out of a
stream or map in a table.
"""

from __future__ import annotations

import json
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
    run_streaming,
)

# What web mode is allowed to reach for. Anything else in the init event's tool
# list means the CLI is more capable than we asked for, and that fails closed.
WEB_TOOLS = ("WebSearch", "WebFetch")

# How long a preflight gets. It is one local command; a slow one is a broken one.
PREFLIGHT_TIMEOUT_S = 30.0


class ClaudeCliAdapter:
    runtime = "claude"

    def __init__(self, timeout_s: float, web_turn_limit: int) -> None:
        self.timeout_s = timeout_s
        self.web_turn_limit = web_turn_limit

    async def preflight(self, agent: AgentConfig) -> AgentStatus:
        try:
            command = resolve_command(agent)
        except AdapterError:
            return AgentStatus(agent.agent_id, installed=False, authenticated=False,
                               detail=f"`{agent.command}` is not on PATH")

        result = await run_process(
            [command, "auth", "status", "--json"], None, timeout_s=PREFLIGHT_TIMEOUT_S
        )
        # The exit code is 0 either way, so the JSON is the answer. Only the boolean
        # is read: nothing else in that payload is ours to look at, let alone store.
        try:
            logged_in = bool(json.loads(result.stdout).get("loggedIn"))
        except ValueError:
            return AgentStatus(agent.agent_id, installed=True, authenticated=False,
                               detail="`claude auth status --json` did not return JSON")

        return AgentStatus(
            agent.agent_id,
            installed=True,
            authenticated=logged_in,
            detail=None if logged_in else "not logged in",
        )

    async def start(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        session_id: str | None = None,
    ) -> AdapterResult:
        argv = self._argv(agent, prompt, source_mode)
        if session_id:
            argv += ["--session-id", session_id]
        return await self._run(agent, argv, prompt, source_mode, session_id)

    async def resume(
        self,
        agent: AgentConfig,
        native_session_id: str,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
    ) -> AdapterResult:
        argv = self._argv(agent, prompt, source_mode) + ["--resume", native_session_id]
        return await self._run(agent, argv, prompt, source_mode, native_session_id)

    # --- invocation ---------------------------------------------------------

    def _argv(self, agent: AgentConfig, prompt: CompiledPrompt, source_mode: SourceMode) -> list[str]:
        argv = [
            resolve_command(agent),
            "--print",
            "--safe-mode",
            # No MCP config of the user's, ever: this server is itself reachable over
            # MCP, and a consulted CLI that could load it could consult back.
            "--strict-mcp-config",
            "--no-chrome",
            "--model",
            agent.model,
            "--system-prompt",
            prompt.system,
            # Inline JSON, not a path -- checked against the binary.
            "--json-schema",
            json.dumps(consultation_content_schema()),
        ]

        if source_mode is SourceMode.WEB:
            if not agent.web_search:
                raise AdapterError(
                    ConsultErrorCode.WEB_SEARCH_UNAVAILABLE,
                    f"agent `{agent.agent_id}` is not configured for web search",
                )
            argv += [
                "--tools",
                ",".join(WEB_TOOLS),
                "--disallowedTools",
                "mcp__*",
                "--output-format",
                "stream-json",
                # Rejected without this, and stream-json is the only way to see the
                # turns we have to count.
                "--verbose",
            ]
        else:
            # Documented as disabling every tool, and confirmed by `"tools":[]` in the
            # init event.
            argv += ["--tools", "", "--output-format", "json"]

        return argv

    async def _run(
        self,
        agent: AgentConfig,
        argv: list[str],
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        session_id: str | None,
    ) -> AdapterResult:
        if source_mode is SourceMode.WEB:
            result, envelope = await self._run_web(argv, prompt)
        else:
            result = await run_process(argv, prompt.payload_json, self.timeout_s, env=child_env())
            envelope = _envelope(result)

        return self._result(agent, envelope, result, session_id)

    async def _run_web(self, argv: list[str], prompt: CompiledPrompt) -> tuple[ProcessResult, dict]:
        """Stream, count assistant turns, and stop the child at the budget.

        The counter is the whole reason web mode streams: with no `--max-turns`, an
        unbounded search loop would otherwise run until `timeout_s`, spending the
        user's own account the entire time.
        """
        events: list[dict] = []
        state = {"turns": 0}

        def on_line(line: str) -> bool:
            line = line.strip()
            if not line:
                return True
            try:
                event = json.loads(line)
            except ValueError:
                # Not every line of a stream is an event; a diagnostic that happens to
                # land on stdout is not a protocol failure.
                return True
            events.append(event)
            _check_tools(event)
            if event.get("type") == "assistant":
                state["turns"] += 1
                return state["turns"] < self.web_turn_limit
            return True

        result = await run_streaming(argv, prompt.payload_json, self.timeout_s, on_line, env=child_env())

        for event in reversed(events):
            if event.get("type") == "result":
                return result, event

        # Killed at the budget before it produced a result envelope: the consultation
        # has no answer, and inventing one from the partial turns is exactly the
        # ghostwriting the envelope invariants exist to prevent.
        if state["turns"] >= self.web_turn_limit:
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"the agent used its {self.web_turn_limit}-turn web budget without "
                "returning a structured answer",
            )
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent produced no result event (exit {result.returncode}): "
            f"{result.stderr.strip()[:400]}",
        )

    def _result(
        self,
        agent: AgentConfig,
        envelope: dict,
        result: ProcessResult,
        session_id: str | None,
    ) -> AdapterResult:
        if envelope.get("is_error"):
            raise AdapterError(
                ConsultErrorCode.AGENT_UNAVAILABLE,
                f"the agent reported a failure: {str(envelope.get('result'))[:400]}",
            )

        model_used = check_model(agent, _reported_model(envelope))
        content = parse_content(str(envelope.get("result", "")))
        native = str(envelope.get("session_id") or session_id or "")
        if not native:
            raise AdapterError(
                ConsultErrorCode.TRANSPORT_ERROR, "the agent returned no session id to resume"
            )

        return AdapterResult(
            content=content,
            native_session_id=native,
            model_used=model_used,
            raw_output=result.stdout,
            usage=_usage(envelope),
        )


# --- parsing ----------------------------------------------------------------


def _envelope(result: ProcessResult) -> dict:
    try:
        envelope = json.loads(result.stdout)
    except ValueError as exc:
        detail = (result.stderr or result.stdout).strip()[:400]
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent returned no JSON envelope (exit {result.returncode}): {detail}",
        ) from exc
    if not isinstance(envelope, dict):
        raise AdapterError(ConsultErrorCode.TRANSPORT_ERROR, "the agent's envelope is not an object")
    return envelope


def _check_tools(event: dict) -> None:
    """Fail closed on a tool we did not ask for.

    The init event lists what the CLI actually enabled, which is the only place a
    disagreement between our flags and its behaviour is visible before it acts.
    """
    if event.get("type") != "system" or event.get("subtype") != "init":
        return
    unexpected = [t for t in event.get("tools") or [] if t not in WEB_TOOLS]
    if unexpected:
        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            f"the agent started with tools we did not enable: {', '.join(sorted(unexpected))}",
        )


def _reported_model(envelope: dict) -> str | None:
    usage: Any = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        # One key in practice; sorted so a hypothetical second one still reports the
        # same model every run rather than whichever hashed first.
        return sorted(usage)[0]
    model = envelope.get("model")
    return model if isinstance(model, str) else None


def _usage(envelope: dict) -> Usage:
    raw = envelope.get("usage")
    raw = raw if isinstance(raw, dict) else {}
    prompt_tokens = int(raw.get("input_tokens") or 0)
    completion_tokens = int(raw.get("output_tokens") or 0)
    cost = envelope.get("total_cost_usd")
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )
