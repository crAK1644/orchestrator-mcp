"""The compatibility guard for the pre-consult product.

`test_orchestrator.py` asserts what the `ask` path *does*. This file asserts what
it *advertises*, byte for byte against a checked-in snapshot, so that adding a
second path cannot quietly reshape the first one's tool schema. Regenerate the
snapshot only when the change to it is the point of the commit:

    uv run python -m tests.snapshot
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator_mcp.contract import ConfigError
from orchestrator_mcp.server import build_server

from .conftest import base_config, consult_block, deployment

SNAPSHOT = Path(__file__).parent / "golden" / "ask_tool_schema.json"


def snapshot_config() -> dict:
    return base_config(deployment("coding"), deployment("research"), deployment("fast"))


async def advertised(config: dict) -> dict:
    tools = await build_server(config).list_tools()
    return {
        t.name: {"input_schema": t.input_schema, "output_schema": t.output_schema} for t in tools
    }


async def test_the_advertised_ask_schema_has_not_moved():
    assert await advertised(snapshot_config()) == json.loads(SNAPSHOT.read_text())


async def test_a_config_without_consult_exposes_only_the_original_tools():
    assert set(await advertised(base_config())) == {"ask", "list_capabilities"}


async def test_a_config_with_only_consult_exposes_no_ask(host_claude):
    """The key-free shape: `ask` talks to a provider endpoint and so needs a key,
    `consult` spawns a CLI that carries its own account. A config that wants the
    second and not the first must not be forced to configure the first."""
    advertised_tools = await advertised({"consult": consult_block()})
    assert set(advertised_tools) == {"consult", "list_consult_agents", "get_consultation"}


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({}, id="nothing at all"),
        pytest.param({"limits": {"max_prompt_chars": 10}}, id="only unrelated blocks"),
    ],
)
def test_a_config_that_asks_for_nothing_refuses_to_boot(config):
    """Half a config is still an error -- but so is none of one. A server with no
    tools should say why at boot rather than start and advertise an empty list."""
    with pytest.raises(ConfigError, match="nothing is configured"):
        build_server(config)


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({"capabilities": {"fast": "x"}}, id="capabilities without deployments"),
        pytest.param({"model_list": [deployment("fast")]}, id="deployments without capabilities"),
    ],
)
def test_half_an_ask_config_is_still_a_mistake(config):
    """Only *both* blocks missing means "no ask path". One without the other is an
    operator who forgot something, and stays a boot error."""
    with pytest.raises(ConfigError):
        build_server(config)


async def test_adding_consult_does_not_touch_the_ask_schema(host_claude):
    """The whole point of the second path: the first one does not move under it."""
    before = await advertised(snapshot_config())
    after = await advertised(snapshot_config() | {"consult": consult_block()})

    assert after["ask"] == before["ask"]
    assert after["list_capabilities"] == before["list_capabilities"]


async def test_the_ask_envelope_is_unchanged_with_consult_configured(host_claude):
    server = build_server(snapshot_config() | {"consult": consult_block()})
    result = await server.call_tool("ask", {"capability": "fast", "prompt": "q"})
    assert result.structured_content["ok"] is True
    assert result.structured_content["content"] == "hi"
