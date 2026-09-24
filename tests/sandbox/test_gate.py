"""What `isolated_write` for claude and opencode is allowed to rest on.

Three conditions, all asked of this process rather than read from config: a write
adapter exists, the sandbox *holds* (the escape was run and denied, not merely a
binary found), and it can grant the network a hosted-model worker needs. Taking any
one away must take the capability with it, and the refusal must name which.
"""

from __future__ import annotations

import logging

import pytest

from orchestrator_mcp.code import registry, sandbox

from .conftest import confined

GATED = ("claude", "opencode")


def test_a_mechanism_that_stopped_denying_does_not_hold(monkeypatch, caplog) -> None:
    """Why the gate is `holds()` and not `mechanism()`: installed and not denying
    looks exactly like working to everything except this."""

    def escaped(worktree=None):
        raise sandbox.SandboxEscape("sandbox-exec did not deny a write to /tmp/x")

    monkeypatch.setattr(sandbox, "verify", escaped)
    with caplog.at_level(logging.ERROR):
        assert sandbox.holds() is False
    assert "did not hold" in caplog.text


def test_the_answer_is_reached_once_per_process(monkeypatch) -> None:
    """Every capability lookup asks. Running the escape each time would be a boot
    that spawns processes in a loop."""
    calls = []

    def counted(worktree=None):
        calls.append(1)
        return {"mechanism": "counted"}

    monkeypatch.setattr(sandbox, "verify", counted)
    assert sandbox.holds() is True
    assert sandbox.holds() is True
    assert calls == [1]


def _all_three(monkeypatch) -> None:
    """Every condition answered, so a test can take exactly one of them away again."""
    monkeypatch.setattr(sandbox, "holds", lambda: True)
    monkeypatch.setattr(sandbox, "internet_unavailable_reason", lambda: None)
    monkeypatch.setattr(registry, "_WRITE_ADAPTERS", frozenset({"codex", *GATED}))


@pytest.mark.parametrize("runtime", GATED)
def test_all_three_conditions_together_open_the_gate(monkeypatch, runtime) -> None:
    _all_three(monkeypatch)
    assert "isolated_write" in registry.runtime_capabilities(runtime)


@pytest.mark.parametrize("runtime", GATED)
@pytest.mark.parametrize(
    ("withheld", "named"),
    [
        ("sandbox", "OS-level sandbox"),
        ("internet", "network the containment cannot grant"),
        ("adapter", "no write adapter"),
    ],
)
def test_withholding_any_one_condition_closes_the_gate(
    monkeypatch, runtime, withheld, named
) -> None:
    _all_three(monkeypatch)
    if withheld == "sandbox":
        monkeypatch.setattr(sandbox, "holds", lambda: False)
    elif withheld == "internet":
        monkeypatch.setattr(sandbox, "internet_unavailable_reason", lambda: "no network")
    else:
        monkeypatch.setattr(registry, "_WRITE_ADAPTERS", frozenset({"codex"}))
    assert "isolated_write" not in registry.runtime_capabilities(runtime)
    assert named in registry.unsupported_reason(runtime, "isolated_write")


def test_a_config_naming_a_gated_agent_runs_no_probe_while_it_has_no_adapter(
    monkeypatch,
) -> None:
    """`_agents_can_execute` asks for every agent at boot, including ones that only
    consult. The escape probe spawns processes, so the free condition goes first."""

    def probed():
        raise AssertionError("holds() was consulted for a runtime with no adapter")

    monkeypatch.setattr(sandbox, "holds", probed)
    monkeypatch.setattr(registry, "_WRITE_ADAPTERS", frozenset({"codex"}))
    for runtime in GATED:
        assert "isolated_write" not in registry.runtime_capabilities(runtime)


def test_codex_is_unaffected_by_a_host_that_cannot_confine(monkeypatch) -> None:
    # Its CLI brings its own boundary, so this gate is not the one that answers for it.
    monkeypatch.setattr(sandbox, "holds", lambda: False)
    assert "isolated_write" in registry.runtime_capabilities("codex")


def test_antigravity_stays_refused_however_well_the_host_confines(monkeypatch) -> None:
    _all_three(monkeypatch)
    assert "isolated_write" not in registry.runtime_capabilities("antigravity")
    assert "dangerously-skip-permissions" in registry.unsupported_reason(
        "antigravity", "isolated_write"
    )


@confined
@pytest.mark.parametrize("runtime", GATED)
def test_this_host_answers_the_gate_the_way_its_own_conditions_do(runtime) -> None:
    """The integration half: the only place the three conditions are real rather
    than set."""
    if "isolated_write" in registry.runtime_capabilities(runtime):
        assert sandbox.holds()
        assert sandbox.internet_unavailable_reason() is None
        assert runtime in registry._WRITE_ADAPTERS
    else:
        reason = registry.unsupported_reason(runtime, "isolated_write")
        assert len(reason) > len(f"`{runtime}` does not support `isolated_write`")
