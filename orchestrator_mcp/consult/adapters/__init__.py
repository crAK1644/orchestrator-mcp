"""Adapters, and the one place that maps a runtime to one."""

from __future__ import annotations

from ..config import AgentConfig, ConsultConfig
from .base import AdapterError, AdapterResult, AgentStatus, ConsultAdapter
from .claude_cli import ClaudeCliAdapter
from .codex_cli import CodexCliAdapter

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
    if agent.runtime == "claude":
        return ClaudeCliAdapter(config.timeout_s, config.web_turn_limit)
    return CodexCliAdapter(config.timeout_s)
