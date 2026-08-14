"""The compatibility guard for the advertised surface.

Every other file asserts what the tools *do*. This one asserts what they
*advertise*, byte for byte against a checked-in snapshot, so that a change to a
request model cannot quietly reshape a schema that clients have already been
written against. Regenerate the snapshot only when the change to it is the point
of the commit:

    uv run python -m tests.snapshot
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator_mcp.contract import ConfigError
from orchestrator_mcp.server import build_server

from .conftest import consult_block

SNAPSHOT = Path(__file__).parent / "golden" / "tool_schema.json"


def snapshot_config() -> dict:
    """Everything switched on, so the snapshot covers all three surfaces.

    A block left out here is a surface whose schema can drift unwatched, which is
    the one thing this file exists to prevent.
    """
    return {
        "consult": consult_block(
            review={"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol", "claude-opus"]},
            workflow={"bindings": {"research": {"agent": "codex-sol"}}},
        )
    }


async def advertised(config: dict) -> dict:
    tools = await build_server(config).list_tools()
    return {
        t.name: {"input_schema": t.input_schema, "output_schema": t.output_schema} for t in tools
    }


async def test_the_advertised_schema_has_not_moved(host_claude):
    assert await advertised(snapshot_config()) == json.loads(SNAPSHOT.read_text())


CONSULT_TOOLS = {
    "orchestrator_consult",
    "orchestrator_list_consult_agents",
    "orchestrator_get_consultation",
    "orchestrator_delete_consultation",
    "orchestrator_request_delete_all_consultations",
    "orchestrator_delete_all_consultations",
}


async def test_a_config_without_reviewers_advertises_only_the_consult_tools(host_claude):
    """Three opt-ins, not one. A server with no reviewers must not offer an
    `orchestrator_review` tool that can do nothing but refuse, and the same goes for
    a workflow nobody configured."""
    assert set(await advertised({"consult": consult_block()})) == CONSULT_TOOLS


# Gated on `workflow:` like the rest, and matched by name rather than by prefix: the
# three deletion tools follow the house naming (`orchestrator_delete_consultation`,
# `orchestrator_delete_review`) rather than the `orchestrator_workflow_` prefix, so a
# prefix check alone would have let them be advertised to a server with no workflow.
WORKFLOW_TOOLS = {
    "orchestrator_workflow_start",
    "orchestrator_workflow_plan_step",
    "orchestrator_workflow_run_step",
    "orchestrator_workflow_record_host_step",
    "orchestrator_workflow_status",
    "orchestrator_workflow_plan_replan",
    "orchestrator_workflow_replan",
    "orchestrator_workflow_cancel",
    "orchestrator_delete_workflow",
    "orchestrator_request_delete_all_workflows",
    "orchestrator_delete_all_workflows",
}


async def test_reviewers_without_a_workflow_advertise_no_workflow_tools(host_claude):
    """The two blocks are independent: reviewers are not a workflow."""
    review = snapshot_config()["consult"]["review"]
    tools = set(await advertised({"consult": consult_block(review=review)}))
    assert not tools & WORKFLOW_TOOLS
    assert "orchestrator_review" in tools


async def test_a_workflow_block_advertises_the_workflow_tools(host_claude):
    tools = set(await advertised(snapshot_config()))
    assert tools & WORKFLOW_TOOLS == WORKFLOW_TOOLS
    # Nothing else prefixed `orchestrator_workflow_` slipped in unlisted.
    assert {name for name in tools if name.startswith("orchestrator_workflow")} <= WORKFLOW_TOOLS


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({}, id="nothing at all"),
        pytest.param({"dashboard": {"enabled": True}}, id="only unrelated blocks"),
    ],
)
def test_a_config_that_asks_for_nothing_refuses_to_boot(config):
    """A server with no tools should say why at boot rather than start and advertise
    an empty list."""
    with pytest.raises(ConfigError, match="nothing is configured"):
        build_server(config)


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({"capabilities": {"fast": "x"}}, id="capabilities"),
        pytest.param({"model_list": [{"model_name": "fast"}]}, id="model_list"),
        pytest.param({"router_settings": {"num_retries": 0}}, id="router_settings"),
        pytest.param({"limits": {"max_prompt_chars": 10}}, id="limits"),
    ],
)
def test_a_config_written_for_the_removed_ask_path_names_what_it_lost(config):
    """A 0.3 config still parses as valid YAML. Starting on it without the tools those
    blocks configured would surface as a bug in the client, so it is a boot error that
    names the block and the version that still has it."""
    with pytest.raises(ConfigError, match="orchestrator_ask"):
        build_server(config)
