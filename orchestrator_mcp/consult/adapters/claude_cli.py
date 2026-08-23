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
    usage_count,
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

    def connect_command(self, agent: AgentConfig) -> str:
        return f"{agent.command} auth login"

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
            # Before the envelope is even parsed. A CLI that exited nonzero said the
            # run failed, and a well-formed envelope on stdout does not overrule it --
            # that answer was abandoned, not delivered. Web mode is exempt because we
            # are the ones who kill it, and a killed child never exits zero.
            if result.returncode != 0:
                raise AdapterError(
                    ConsultErrorCode.AGENT_UNAVAILABLE,
                    f"the agent exited {result.returncode}: {result.stderr.strip()[:400]}",
                )
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
            _check_event(event)
            if event.get("type") == "assistant":
                state["turns"] += 1
                # `<=`, so the turn that spends the last of the budget is allowed to
                # finish and emit its result event. Stopping *at* the limit would kill
                # the child one event before the answer it had already produced, and
                # make `web_turn_limit: 1` mean "no web consultation is possible".
                return state["turns"] <= self.web_turn_limit
            return True

        result = await run_streaming(argv, prompt.payload_json, self.timeout_s, on_line, env=child_env())

        for event in reversed(events):
            if event.get("type") == "result":
                return result, event

        # Killed at the budget before it produced a result envelope: the consultation
        # has no answer, and inventing one from the partial turns is exactly the
        # ghostwriting the envelope invariants exist to prevent.
        if state["turns"] > self.web_turn_limit:
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

        reported = _reported_model(envelope)
        model_used = check_model(agent, reported)
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
            model_verified=reported is not None,
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


def _check_event(event: dict) -> None:
    """Fail closed on anything that is not the two web tools.

    Two checks, because the init event and the turns can disagree. The init event
    lists what the CLI says it enabled, which is where a mismatch between our flags
    and its configuration shows up before it acts. Every later event is then
    searched for the act itself: a stream that declared `tools: []` and then used
    `Bash` has done the thing the declaration promised it would not.
    """
    if event.get("type") == "system" and event.get("subtype") == "init":
        unexpected = [t for t in event.get("tools") or [] if t not in WEB_TOOLS]
        if unexpected:
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"the agent started with tools we did not enable: {', '.join(sorted(unexpected))}",
            )

    if used := sorted(_tools_used(event) - set(WEB_TOOLS)):
        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            f"the agent tried to act rather than answer (used {', '.join(used)})",
        )


def _tools_used(node: Any) -> set[str]:
    """Every tool name anywhere in an event.

    Recursive and shape-agnostic on purpose: a `tool_use` block can sit inside
    `message.content`, inside a nested block, or somewhere a future release moves
    it, and a check that only knows today's path fails open when it moves.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        # `tool_result` is deliberately not here: it is the reply to a `tool_use` we
        # have already seen and judged, and it carries no name of its own.
        if node.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
            name = node.get("name") or node.get("tool_name")
            # An unnamed tool block is still a tool block, and cannot be permitted by
            # a set membership test it does not appear in.
            found.add(name if isinstance(name, str) and name else "<unnamed>")
        for value in node.values():
            found |= _tools_used(value)
    elif isinstance(node, list):
        for item in node:
            found |= _tools_used(item)
    return found


def _reported_model(envelope: dict) -> str | None:
    usage: Any = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        if len(usage) == 1:
            return next(iter(usage))

        # Claude Code 2.1.220 can report helper-model usage beside the model that
        # answered. The top-level usage is the primary answer's usage, so match it
        # back to the one model entry rather than treating an internal Haiku call as
        # a silent fallback from the requested Opus model.
        primary: Any = envelope.get("usage")
        if isinstance(primary, dict):
            matches = [
                model
                for model, model_usage in usage.items()
                if isinstance(model_usage, dict)
                and model_usage.get("inputTokens") == primary.get("input_tokens")
                and model_usage.get("outputTokens") == primary.get("output_tokens")
            ]
            if len(matches) == 1:
                return matches[0]

        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            "the agent returned ambiguous model metadata for the primary answer",
        )
    model = envelope.get("model")
    return model if isinstance(model, str) else None


def _usage(envelope: dict) -> Usage:
    raw = envelope.get("usage")
    raw = raw if isinstance(raw, dict) else {}
    # `input_tokens` counts only what was not served from cache, and on a consultation
    # that is almost nothing: a live resumed turn reported 2 against 1334 read from
    # cache and 1437 written to it. Both cached figures are prompt that was sent and
    # billed, so `Usage` wants them here -- reporting 2 as the prompt described a turn
    # three orders of magnitude smaller than the one that ran.
    prompt_tokens = (
        usage_count(raw.get("input_tokens"))
        + usage_count(raw.get("cache_read_input_tokens"))
        + usage_count(raw.get("cache_creation_input_tokens"))
    )
    completion_tokens = usage_count(raw.get("output_tokens"))
    # Whole-invocation, so it covers any internal helper model too, while the token
    # counts above are the answering model's alone. Not an inconsistency to reconcile:
    # what a consultation spent and how large the answer was are different questions.
    cost = envelope.get("total_cost_usd")
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=(
            float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None
        ),
    )
