"""The MCP surface of the consultation path.

`test_existing_contract.py` guards what a consult-free config advertises. This file
guards the other half: that configuring `consult:` adds exactly six tools, that
the calling model reads a usable schema off them, and that it cannot name an agent
nobody configured.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from orchestrator_mcp.contract import ConfigError
from orchestrator_mcp.server import build_server

from .conftest import consult_block


def config(tmp_path, **overrides):
    return {
        "consult": consult_block(
            database_path=str(tmp_path / "consultations.sqlite3"), **overrides
        )
    }


async def tools(server):
    return {t.name: t for t in await server.list_tools()}


async def test_configuring_consult_without_reviewers_adds_exactly_six_tools(
    tmp_path, host_claude
):
    """`review:` is a separate opt-in. A config with agents and no reviewers gets the
    consultation tools and nothing that would refuse every call."""
    assert set(await tools(build_server(config(tmp_path)))) == {
        "orchestrator_consult",
        "orchestrator_list_consult_agents",
        "orchestrator_get_consultation",
        "orchestrator_delete_consultation",
        "orchestrator_request_delete_all_consultations",
        "orchestrator_delete_all_consultations",
    }


async def test_the_consult_schema_offers_only_configured_agents(tmp_path, host_claude):
    schema = (await tools(build_server(config(tmp_path))))["orchestrator_consult"].input_schema
    target = schema["properties"]["target_agent"]

    assert set(target["enum"]) == {"codex-sol", "claude-opus", None}
    # An enum, not a `const` or an `anyOf`: what a function-calling client actually
    # reads when it turns this schema into a tool definition.
    assert "anyOf" not in target and "const" not in target


async def test_the_consult_schema_carries_the_capability_vocabulary(tmp_path, host_claude):
    schema = (await tools(build_server(config(tmp_path))))["orchestrator_consult"].input_schema
    # Spelled out rather than compared against `CONSULT_CAPABILITIES`, so widening the
    # vocabulary stays a deliberate edit in two places. `planning`, `prompt_authoring`,
    # `testing` and `synthesis` exist for the workflow's steps -- without the last two
    # an `auto` binding for `test` or `synthesize` would have nothing to sort on.
    assert set(schema["properties"]["capability"]["enum"]) == {
        "coding", "research", "writing", "reasoning", "review",
        "planning", "prompt_authoring", "testing", "synthesis",
    }
    assert schema["required"] == ["capability", "prompt"] or set(schema["required"]) == {
        "capability", "prompt"
    }


async def test_the_tool_tells_the_host_to_keep_the_consultation_id(tmp_path, host_claude):
    """MCP gives this server no reliable host conversation id, so the returned id is
    the only thing that continues a session -- and the caller has to hold it."""
    doc = (await tools(build_server(config(tmp_path))))["orchestrator_consult"].description
    assert "consultation_id" in doc
    assert "send it back" in doc


async def test_the_tool_description_names_every_supported_runtime(tmp_path, host_claude):
    doc = (await tools(build_server(config(tmp_path))))["orchestrator_consult"].description
    assert all(name in doc for name in ("Codex", "Claude Code", "OpenCode", "Antigravity"))


async def test_bulk_consultation_deletion_requires_the_preview_token(tmp_path, host_claude):
    surface = await tools(build_server(config(tmp_path)))
    schema = surface["orchestrator_delete_all_consultations"].input_schema
    assert schema["required"] == ["confirm_token"]


async def test_consult_answers_with_an_envelope_not_an_exception(tmp_path, host_claude):
    """The agent is not installed here, which is exactly the point: a failure comes
    back as a code the caller can branch on."""
    server = build_server(config(tmp_path, agents={
        "codex-sol": {
            "runtime": "codex",
            "command": "definitely-not-installed-anywhere",
            "model": "m",
            "scores": {"coding": 90},
        }
    }))
    result = await server.call_tool("orchestrator_consult", {"capability": "coding", "prompt": "q"})

    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["code"] == "agent_not_installed"
    assert result.structured_content["content"] is None


async def test_an_unconfigured_agent_never_reaches_the_service(tmp_path, host_claude):
    """The enum is in the advertised schema, so the MCP layer rejects the argument
    before a consultation is created -- no database row for a call that cannot run."""
    server = build_server(config(tmp_path))
    with pytest.raises(Exception):
        await server.call_tool(
            "orchestrator_consult", {"capability": "coding", "prompt": "q", "target_agent": "gpt-9"}
        )
    assert not (tmp_path / "consultations.sqlite3").exists()


async def test_a_direct_caller_naming_an_unconfigured_agent_gets_an_envelope(
    tmp_path, host_claude
):
    """Second line of defence, for the callers that do not go through the schema."""
    from orchestrator_mcp.consult.config import ConsultConfig
    from orchestrator_mcp.consult.service import ConsultService

    service = ConsultService(ConsultConfig(**config(tmp_path)["consult"]), "claude")
    await service.open()
    response = await service.consult(capability="coding", prompt="q", target_agent="gpt-9")

    assert response.ok is False
    assert response.error.code.value == "invalid_request"


async def test_listing_agents_reports_the_host_runtime(tmp_path, host_claude):
    server = build_server(config(tmp_path))
    result = await server.call_tool("orchestrator_list_consult_agents", {})

    assert result.structured_content["host_runtime"] == "claude"
    hosts = {a["agent_id"]: a["excluded_as_host"] for a in result.structured_content["agents"]}
    assert hosts == {"claude-opus": True, "codex-sol": False}


async def test_get_consultation_on_an_unknown_id_is_an_error_not_a_blank_record(
    tmp_path, host_claude
):
    server = build_server(config(tmp_path))
    # `ToolError` specifically, not any exception. It is the SDK's word for a failure
    # the server anticipated, and it is the only kind whose message reaches the model:
    # anything else is treated as a crash, and since mcp 2.1 the caller is told only
    # `Error executing tool <name>`. An unknown id has to be the first kind, or the
    # model cannot tell a bad argument from a broken server.
    with pytest.raises(ToolError) as exc:
        await server.call_tool(
            "orchestrator_get_consultation", {"consultation_id": "11111111-2222-3333-4444-555555555555"}
        )
    assert "no consultation" in str(exc.value)


async def test_consult_without_a_host_runtime_refuses_to_boot(tmp_path, monkeypatch):
    """Without it the server cannot exclude itself from its own routing, and would
    happily delegate the work straight back to the agent that asked."""
    monkeypatch.delenv("ORCHESTRATOR_HOST_RUNTIME", raising=False)
    with pytest.raises(ConfigError) as exc:
        build_server(config(tmp_path))
    assert "ORCHESTRATOR_HOST_RUNTIME" in str(exc.value)


async def test_a_bad_consult_block_refuses_to_boot(tmp_path, host_claude):
    with pytest.raises(ConfigError):
        build_server({"consult": {"agents": {"x": {"runtime": "gemini"}}}})


async def test_the_store_is_not_created_until_a_consultation_happens(tmp_path, host_claude):
    """A configured-but-unused consult path should not leave a database behind."""
    build_server(config(tmp_path))
    assert not (tmp_path / "consultations.sqlite3").exists()
