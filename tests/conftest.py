"""Shared helpers for the consult tests."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator_mcp.consult.config import HOST_RUNTIME_ENV


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
    # `model_fields` is not enough either: pydantic compiles the default into the
    # validator, so the FieldInfo has to be patched *and* the validator rebuilt.
    # Without the rebuild this fixture ran green for months while the suite wrote the
    # developer's own `~/.orchestrator-mcp/agents.yaml`.
    monkeypatch.setattr(ConsultConfig.model_fields["managed_agents_path"], "default", path)
    ConsultConfig.model_rebuild(force=True)
    # Not decoration: the two lines above are the only thing standing between a test
    # run and a real config file, and both of them are pydantic internals that a
    # version bump can quietly turn into no-ops.
    assert ConsultConfig(agents={"probe": agent()}).managed_agents_path == path
    yield
    monkeypatch.undo()
    ConsultConfig.model_rebuild(force=True)


@pytest.fixture(autouse=True)
def _no_real_worktrees(tmp_path, monkeypatch):
    """Contained runs check out under the test's own directory, never `~`.

    Autouse for the same reason as the fixture above: the root is a module constant
    read at call time rather than a parameter, so any test that reaches the code
    service would otherwise create git worktrees in the developer's home directory
    and register them inside whatever repository it was pointed at."""
    monkeypatch.setattr(
        "orchestrator_mcp.code.service.WORKTREE_ROOT", tmp_path / "worktrees"
    )


@pytest.fixture
def host_claude(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(HOST_RUNTIME_ENV, "claude")
    return "claude"


@pytest.fixture
def host_codex(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(HOST_RUNTIME_ENV, "codex")
    return "codex"
