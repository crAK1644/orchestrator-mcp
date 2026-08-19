"""Adapters, and the one place that maps a runtime to one."""

from __future__ import annotations

from ..config import AgentConfig, ConsultConfig
from .antigravity_cli import AntigravityCliAdapter
from .base import AdapterError, AdapterResult, AgentStatus, ConsultAdapter
from .claude_cli import ClaudeCliAdapter
from .codex_cli import CodexCliAdapter
from .opencode_cli import OpenCodeCliAdapter
from ..errors import ConsultErrorCode

__all__ = [
    "AdapterError",
    "AdapterResult",
    "AgentStatus",
    "AntigravityCliAdapter",
    "ClaudeCliAdapter",
    "CodexCliAdapter",
    "ConsultAdapter",
    "OpenCodeCliAdapter",
    "adapter_for",
]


def adapter_for(agent: AgentConfig, config: ConsultConfig) -> ConsultAdapter:
    # Explicit, with no fallthrough: a runtime this function does not recognise used to
    # land on Codex, which means a mistyped or not-yet-implemented runtime would quietly
    # be consulted through the wrong CLI -- wrong model, wrong flags, plausible answer.
    # One agent's own limit where it has one. Adapters take their timeout at
    # construction and this is the only place they are constructed, so nothing below
    # here has to know the value can differ per agent.
    # `is not None` rather than truthiness: `0` falling back to the global would be
    # a silent override of an explicit setting, and this must not depend on
    # `AgentConfig.timeout_s` keeping its `ge=1`.
    timeout_s = agent.timeout_s if agent.timeout_s is not None else config.timeout_s
    if agent.runtime == "claude":
        return ClaudeCliAdapter(timeout_s, config.web_turn_limit)
    if agent.runtime == "codex":
        return CodexCliAdapter(timeout_s)
    if agent.runtime == "antigravity":
        return AntigravityCliAdapter(timeout_s)
    if agent.runtime == "opencode":
        return OpenCodeCliAdapter(timeout_s)
    raise AdapterError(
        ConsultErrorCode.AGENT_UNAVAILABLE,
        f"no adapter is implemented for runtime `{agent.runtime}`",
    )
