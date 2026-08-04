"""Shared helpers for the consult-era tests.

`test_orchestrator.py` at the repo root keeps its own copies untouched -- it is the
compatibility suite, and a shared helper it did not have before is a change to it.
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator_mcp.consult.config import HOST_RUNTIME_ENV


def deployment(capability: str, mock: str = "hi", model: str = "openai/gpt-4o") -> dict[str, Any]:
    return {
        "model_name": capability,
        "litellm_params": {"model": model, "api_key": "sk-test", "mock_response": mock},
    }


def base_config(*deployments: dict[str, Any]) -> dict[str, Any]:
    """A 0.1.2-shaped config: no `consult:` block anywhere in it."""
    deployments = deployments or (deployment("fast"),)
    return {
        "capabilities": {d["model_name"]: f"{d['model_name']} work" for d in deployments},
        "model_list": list(deployments),
        "router_settings": {"num_retries": 0},
    }


def agent(
    runtime: str = "codex",
    model: str = "gpt-5.6-sol",
    priority: int = 10,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "runtime": runtime,
        "command": runtime,
        "model": model,
        "priority": priority,
        "scores": {"coding": 90, "research": 90, "writing": 90, "reasoning": 90, "review": 90},
        **overrides,
    }


def consult_block(**overrides: Any) -> dict[str, Any]:
    return {
        "agents": {
            "codex-sol": agent("codex", "gpt-5.6-sol", 10),
            "claude-opus": agent("claude", "opus", 20),
        },
        **overrides,
    }


@pytest.fixture(autouse=True)
def _no_real_managed_file(tmp_path, monkeypatch):
    """Point the dashboard's agents file somewhere disposable, everywhere.

    Its default is `~/.orchestrator-mcp/agents.yaml`, so without this a developer who
    has ever used the dashboard runs a different suite from CI -- and the failure would
    surface as an agent count nobody put there. Autouse, because the file is read by
    `load_consult_config` rather than passed in, so any test can reach it."""
    from orchestrator_mcp.consult.config import ConsultConfig

    path = tmp_path / "agents.yaml"
    monkeypatch.setattr("orchestrator_mcp.consult.managed.DEFAULT_MANAGED_PATH", path)
    # The pydantic default is bound at class definition, so patching the module name
    # alone would leave a directly-constructed `ConsultConfig` pointed at the real one.
    monkeypatch.setattr(ConsultConfig.model_fields["managed_agents_path"], "default", path)


@pytest.fixture
def host_claude(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(HOST_RUNTIME_ENV, "claude")
    return "claude"


@pytest.fixture
def host_codex(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(HOST_RUNTIME_ENV, "codex")
    return "codex"
