"""Who may take a step, and what the answer is when nobody may.

The service tests drive whole workflows; these drive the decision underneath a single
step: operator trust intersected with what the code can actually contain, execution
identity against the host's, and the messages that say which side refused. Nothing
here starts a workflow, so a refusal that belongs at configuration time is shown to
land at configuration time rather than several paid steps in.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator_mcp.code.registry import (
    RUNTIME_CAPABILITIES,
    CodeError,
    code_adapter_for,
    runtime_capabilities,
    unsupported_reason,
)
from orchestrator_mcp.consult.config import ConsultConfig, StepBinding
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.workflow.contract import WorkflowError, repository_access_for
from orchestrator_mcp.workflow.identity import host_identity_conflict, same_execution_identity
from orchestrator_mcp.workflow.routing import WorkflowRouter

from .test_workflow_service import WORKFLOW_SCORES, workflow_agent

HOST = {"runtime": "claude", "model": "claude-opus-5"}


def config(agents: dict, host: dict | None = None, **workflow) -> ConsultConfig:
    return ConsultConfig(
        agents=agents,
        host=HOST if host is None else host,
        workflow={"bindings": {}, **workflow},
    )


def router(agents: dict, host: dict | None = None) -> WorkflowRouter:
    return WorkflowRouter(config(agents, host), "claude")


AGENTS = {
    "codex-sol": workflow_agent(
        "codex", "gpt-5.6-sol", 10, execution_modes=["consultation", "patch"]
    ),
    "flash": workflow_agent(
        "opencode", "deepseek-v4-flash-free", 30, execution_modes=["consultation", "patch"]
    ),
}


# --- execution modes: operator trust, intersected with containment ----------


def test_declaring_a_mode_a_runtime_cannot_do_is_refused_at_startup():
    """`execution_modes:` is operator trust; containment is the code's own statement,
    and the two are reconciled at boot rather than at the step that needed it."""
    agents = {
        "flash": workflow_agent(
            "opencode",
            "deepseek-v4-flash-free",
            10,
            execution_modes=["consultation", "patch", "isolated_write"],
        )
    }
    with pytest.raises(ValidationError) as raised:
        config(agents)
    # Which side said no: not "you did not ask for it" but "opencode cannot be held to it".
    assert "`opencode` does not support `isolated_write`" in str(raised.value)
    assert "permissions isolate configuration" in str(raised.value)


def test_effective_modes_is_the_intersection_that_does_it():
    built = config(AGENTS)
    assert built.effective_modes(built.agents["flash"]) == {"consultation", "patch"}
    assert built.effective_modes(built.agents["codex-sol"]) == {"consultation", "patch"}
    # Trust is the binding half: codex *can* be contained, and this one was not asked to be.
    assert "isolated_write" in runtime_capabilities("codex")


def test_a_mode_the_operator_never_granted_refuses_on_the_operator_s_side():
    with pytest.raises(WorkflowError) as raised:
        router(AGENTS).resolve(
            "implement", StepBinding(agent="codex-sol", execution="isolated_write"), want_web=False
        )
    assert "is not in its `execution_modes:`" in str(raised.value)


@pytest.mark.parametrize(
    "runtime,expected",
    [
        ("opencode", "permissions isolate configuration"),
        ("claude", "no contained executor yet"),
        ("antigravity", "--dangerously-skip-permissions"),
    ],
)
def test_each_runtime_refuses_isolated_write_with_its_own_reason(runtime, expected):
    """"Unsupported" reads as "not yet"; for antigravity it is a standing refusal."""
    built = config({"a": workflow_agent(runtime, "some-model-9", 10)})
    with pytest.raises(CodeError) as raised:
        code_adapter_for(built.agents["a"], built)
    assert f"`{runtime}` does not support `isolated_write`" in str(raised.value)
    assert expected in str(raised.value)
    assert raised.value.code == ConsultErrorCode.AGENT_UNAVAILABLE


def test_codex_is_the_one_runtime_with_a_write_adapter():
    """The table allows it and an adapter exists for it -- the only pairing that does.
    What the adapter then relies on is codex's own sandbox; see `test_code_execution`."""
    assert "isolated_write" in RUNTIME_CAPABILITIES["codex"]
    built = config({"a": workflow_agent("codex", "gpt-5.6-sol", 10)})
    adapter = code_adapter_for(built.agents["a"], built)
    assert adapter.runtime == "codex"


def test_a_runtime_outside_the_table_inherits_nothing():
    """No fallthrough default: a mistyped runtime must not pick up a write mode."""
    assert runtime_capabilities("gpt-cli") == frozenset()
    assert "not a runtime this installation implements" in unsupported_reason(
        "gpt-cli", "isolated_write"
    )


# --- execution identity -----------------------------------------------------


def test_the_hosts_own_identity_is_never_routed_back_to_itself():
    agents = {"self": workflow_agent("claude", "claude-opus-5", 10)}
    with pytest.raises(WorkflowError) as raised:
        router(agents).resolve("plan", StepBinding(agent="self"), want_web=False)
    assert "this host's own execution identity" in str(raised.value)


@pytest.mark.parametrize("model", ["opus", "claude-opus"])
def test_an_alias_of_the_host_model_is_the_host(model):
    """`opus` is a spelling of `claude-opus-5`, not a second model."""
    agents = {"alias": workflow_agent("claude", model, 10)}
    with pytest.raises(WorkflowError) as raised:
        router(agents).resolve("plan", StepBinding(agent="alias"), want_web=False)
    assert "this host's own execution identity" in str(raised.value)


def test_an_unversioned_name_that_matches_nothing_is_still_refused():
    """Unprovable means refused: `sonnet` may or may not be what the host is running."""
    agents = {"alias": workflow_agent("claude", "sonnet", 10)}
    with pytest.raises(WorkflowError) as raised:
        router(agents).resolve("plan", StepBinding(agent="alias"), want_web=False)
    assert "cannot be shown to differ from this host" in str(raised.value)
    assert "`sonnet` names no version" in str(raised.value)


def test_a_different_versioned_model_on_the_host_runtime_is_routable():
    """What configuring `consult.host.model:` buys: a sibling model can take a step."""
    agents = {"other": workflow_agent("claude", "claude-opus-4.9", 10)}
    resolved = router(agents).resolve("plan", StepBinding(agent="other"), want_web=False)
    assert [a.agent_id for a in resolved.agents] == ["other"]


def test_without_a_host_model_exclusion_falls_back_to_the_whole_runtime():
    agents = {"other": workflow_agent("claude", "claude-opus-4.9", 10)}
    with pytest.raises(WorkflowError) as raised:
        router(agents, host={"runtime": "claude"}).resolve(
            "plan", StepBinding(agent="other"), want_web=False
        )
    assert "no `consult.host.model:` is configured" in str(raised.value)
    # Another runtime is untouched by that fallback.
    assert host_identity_conflict("codex", "gpt-5.6-sol", "claude", None) is None


def test_identity_is_one_question_with_one_answer():
    assert same_execution_identity("codex", "gpt-5.6", "claude", "gpt-5.6") is None
    assert same_execution_identity("codex", "gpt-5.6", "codex", "gpt-5.6") == (
        "they name the same model"
    )
    # Two pinned versions that differ are two models, in either order.
    assert same_execution_identity("codex", "gpt-5.6-sol", "codex", "gpt-5.6") is None
    assert same_execution_identity("codex", "gpt-5.6", "codex", "gpt-5.6-sol") is None
    assert "names no version" in (same_execution_identity("codex", "sol", "codex", "gpt-5.6") or "")


# --- routing ----------------------------------------------------------------


def test_auto_selects_for_a_step_that_had_no_capability_before():
    """`synthesize` is unroutable without the capability the workflow added."""
    agents = {
        "low": workflow_agent(
            "codex", "gpt-5.6-low", 10, scores={**WORKFLOW_SCORES, "synthesis": 10}
        ),
        "high": workflow_agent(
            "codex", "gpt-5.6-high", 20, scores={**WORKFLOW_SCORES, "synthesis": 95}
        ),
    }
    resolved = router(agents).resolve("synthesize", StepBinding(), want_web=False)
    assert [a.agent_id for a in resolved.agents] == ["high"]


def test_when_nothing_is_eligible_the_refusal_names_every_agent_and_its_reason():
    agents = {
        "codex-sol": workflow_agent(
            "codex", "gpt-5.6-sol", 10, scores={**WORKFLOW_SCORES, "synthesis": 0}
        )
    }
    with pytest.raises(WorkflowError) as raised:
        router(agents).resolve("synthesize", StepBinding(), want_web=False)
    assert raised.value.code == ConsultErrorCode.NO_AGENT_AVAILABLE
    assert "scores 0 for `synthesis`" in str(raised.value)


def test_an_explicit_binding_is_still_compatibility_checked():
    """Naming an agent is a request, not an override."""
    agents = {
        "flash": workflow_agent(
            "opencode", "deepseek-v4-flash-free", 10, scores={**WORKFLOW_SCORES, "review": 0}
        )
    }
    with pytest.raises(WorkflowError) as raised:
        router(agents).resolve("review", StepBinding(agents=["flash"]), want_web=False)
    assert "scores 0 for `review`" in str(raised.value)


def test_a_host_only_step_cannot_be_given_to_an_agent():
    with pytest.raises(WorkflowError) as raised:
        router(AGENTS).resolve("apply_patch", StepBinding(agent="codex-sol"), want_web=False)
    assert "host-only" in str(raised.value)


def test_a_step_needing_the_web_refuses_a_runtime_that_has_none():
    agents = {"flash": workflow_agent("opencode", "deepseek-v4-flash-free", 10, web_search=True)}
    with pytest.raises(WorkflowError) as raised:
        router(agents).resolve("research", StepBinding(agent="flash"), want_web=True)
    assert "offers no web mode" in str(raised.value)
    # The same agent takes the same step when the workflow did not ask for the web.
    assert router(agents).resolve(
        "research", StepBinding(agent="flash"), want_web=False
    ).web is False


def test_only_review_takes_more_than_one_agent():
    with pytest.raises(WorkflowError) as raised:
        router(AGENTS).resolve("plan", StepBinding(agents=["codex-sol", "flash"]), want_web=False)
    assert "takes one" in str(raised.value)
    resolved = router(AGENTS).resolve(
        "review", StepBinding(agents=["codex-sol", "flash"]), want_web=False
    )
    assert [a.agent_id for a in resolved.agents] == ["codex-sol", "flash"]


# --- repository access ------------------------------------------------------


def test_repository_access_follows_the_execution_mode_not_the_step():
    assert repository_access_for("host", None) == "active_tree"
    assert repository_access_for("agent", "consultation") == "context_only"
    assert repository_access_for("agent", "patch") == "context_only"
    assert repository_access_for("agent", "isolated_write") == "worktree"

    # And what the resolved binding carries is that, not something read off the step:
    # `implement` produces a code change and still sees only what it was sent.
    patching = router(AGENTS).resolve(
        "implement", StepBinding(agent="codex-sol", execution="patch"), want_web=False
    )
    asking = router(AGENTS).resolve("plan", StepBinding(agent="codex-sol"), want_web=False)
    assert patching.repository_access == asking.repository_access == "context_only"
    assert patching.as_binding()["repository_access"] == "context_only"

    on_host = router(AGENTS).resolve("test", StepBinding(executor="host"), want_web=False)
    assert on_host.repository_access == "active_tree"
    assert on_host.agents == () and on_host.execution_mode is None


# --- configuration ----------------------------------------------------------


def test_a_workflow_without_retention_refuses_at_startup():
    """A workflow *is* its stored artifacts, and a review cannot finalize without
    them, so the refusal belongs at boot."""
    with pytest.raises(ValidationError) as raised:
        ConsultConfig(agents=AGENTS, store_full_content=False, workflow={"bindings": {}})
    assert "requires `store_full_content: true`" in str(raised.value)
    # Without a workflow block, dropping bodies is still the operator's call.
    assert ConsultConfig(agents=AGENTS, store_full_content=False).workflow is None


def test_a_filesystem_root_is_not_a_workflow_root():
    with pytest.raises(ValidationError) as raised:
        ConsultConfig(agents=AGENTS, workflow={"roots": ["/"]})
    assert "filesystem root" in str(raised.value)


def test_a_binding_naming_an_unconfigured_agent_refuses_at_startup():
    with pytest.raises(ValidationError) as raised:
        ConsultConfig(agents=AGENTS, workflow={"bindings": {"plan": {"agent": "ghost"}}})
    assert "not a configured agent" in str(raised.value)


def test_a_binding_asking_a_step_for_a_mode_it_does_not_allow_refuses_at_startup():
    with pytest.raises(ValidationError) as raised:
        ConsultConfig(
            agents=AGENTS,
            workflow={"bindings": {"plan": {"agent": "codex-sol", "execution": "patch"}}},
        )
    assert "which `plan` does not allow" in str(raised.value)


def test_the_binding_shapes_are_exclusive():
    with pytest.raises(ValidationError):
        StepBinding(executor="host", agent="codex-sol")
    with pytest.raises(ValidationError):
        StepBinding(agent="codex-sol", agents=["flash"])
    with pytest.raises(ValidationError):
        StepBinding(agents=["codex-sol", "codex-sol"])
    with pytest.raises(ValidationError):
        StepBinding(agents=[])
