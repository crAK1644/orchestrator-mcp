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
    return {
        "consult": consult_block(
            review={"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol", "claude-opus"]}
        )
    }


async def advertised(config: dict) -> dict:
    tools = await build_server(config).list_tools()
    return {
        t.name: {"input_schema": t.input_schema, "output_schema": t.output_schema} for t in tools
    }


async def test_the_advertised_schema_has_not_moved(host_claude):
    assert await advertised(snapshot_config()) == json.loads(SNAPSHOT.read_text())


async def test_a_config_without_reviewers_advertises_only_the_consult_tools(host_claude):
    """Two opt-ins, not one. A server with no reviewers must not offer an
    `orchestrator_review` tool that can do nothing but refuse."""
    assert set(await advertised({"consult": consult_block()})) == {
        "orchestrator_consult",
        "orchestrator_list_consult_agents",
        "orchestrator_get_consultation",
    }


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
