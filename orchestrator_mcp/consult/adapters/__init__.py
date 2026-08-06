"""Adapters, and the one place that maps a runtime to one."""

from __future__ import annotations

from ..config import AgentConfig, ConsultConfig
from .base import AdapterError, AdapterResult, AgentStatus, ConsultAdapter
from .claude_cli import ClaudeCliAdapter
from .codex_cli import CodexCliAdapter
from ..errors import ConsultErrorCode

__all__ = [
    "AdapterError",
    "AdapterResult",
    "AgentStatus",
    "ClaudeCliAdapter",
    "CodexCliAdapter",
    "ConsultAdapter",
    "adapter_for",
]


def adapter_for(agent: AgentConfig, config: ConsultConfig) -> ConsultAdapter:
    # Explicit, with no fallthrough: a runtime this function does not recognise used to
    # land on Codex, which means a mistyped or not-yet-implemented runtime would quietly
    # be consulted through the wrong CLI -- wrong model, wrong flags, plausible answer.
    if agent.runtime == "claude":
        return ClaudeCliAdapter(config.timeout_s, config.web_turn_limit)
    if agent.runtime == "codex":
        return CodexCliAdapter(config.timeout_s)
    raise AdapterError(
        ConsultErrorCode.AGENT_UNAVAILABLE,
        f"no adapter is implemented for runtime `{agent.runtime}`",
    )
