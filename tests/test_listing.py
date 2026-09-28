"""Finding things again: the list tools, the resource templates, and completion.

Seeded straight into the store the server reads, so no agent runs.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import PromptReference, ResourceTemplateReference

from orchestrator_mcp.consult.contract import ConsultRoute
from orchestrator_mcp.consult.store import ConsultStore
from orchestrator_mcp.server import build_server
from orchestrator_mcp.workflow.store import WorkflowStore

from .conftest import agent, consult_block

ROUTE = ConsultRoute(
    agent_id="codex-sol",
    runtime="codex",
    model="gpt-5.6-sol",
    capability_score=90,
    priority=10,
    explicitly_selected=False,
)


@pytest.fixture
async def seeded(tmp_path, host_claude):
    """Two workflows and two consultations of the host's own, a day apart, then a
    newer consultation one of the workflows owns."""
    db = tmp_path / "consultations.sqlite3"
    store = await ConsultStore(db).open()
    workflows = WorkflowStore(store)
    ids: dict[str, list[str]] = {"workflows": [], "consultations": []}

    async def dated(table: str, row_id: str, day: int) -> None:
        await store._run(
            lambda: store._db.execute(
                f"UPDATE {table} SET created_at = ? WHERE id = ?",
                (f"2026-09-0{day}T00:00:00+00:00", row_id),
            )
        )

    async def consultation(day: int, **kwargs) -> str:
        consultation_id = uuid4()
        await store.create_consultation(
            consultation_id=consultation_id,
            origin_runtime="claude",
            route=ROUTE,
            capability="research",
            protocol_version="consult-v1",
            config_hash="abc123",
            **kwargs,
        )
        await dated("consultations", str(consultation_id), day)
        return str(consultation_id)

    for day, goal in [(1, "split the scanner out"), (2, "x" * 300)]:
        workflow_id = str(uuid4())
        await workflows.create_workflow(
            workflow_id, goal, str(tmp_path), "claude", None, {}, {}, "hash", None
        )
        await dated("workflow_runs", workflow_id, day)
        ids["workflows"].append(workflow_id)
        ids["consultations"].append(await consultation(day))
    await consultation(3, workflow_id=ids["workflows"][0])
    await store.close()

    server = build_server(
        {
            "consult": consult_block(
                database_path=str(db),
                agents=consult_block()["agents"]
                | {"codex-off": agent("codex", "gpt-5.6-sol", enabled=False)},
                review={"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol"]},
                workflow={"bindings": {"research": {"agent": "codex-sol"}}},
            )
        }
    )
    return server, ids


async def listed(server, tool: str, **arguments) -> list[dict]:
    return (await server.call_tool(tool, arguments)).structured_content["result"]


async def test_the_lists_are_newest_first_and_leave_owned_consultations_out(seeded):
    server, ids = seeded

    workflows = await listed(server, "orchestrator_list_workflows")
    assert [w["workflow_id"] for w in workflows] == ids["workflows"][::-1]
    assert workflows[0]["goal"] == "x" * 199 + "…"
    consultations = await listed(server, "orchestrator_list_consultations")
    assert [c["consultation_id"] for c in consultations] == ids["consultations"][::-1]
    assert len(await listed(server, "orchestrator_list_workflows", limit=1)) == 1


async def test_a_resource_reads_what_the_get_tool_returns(seeded):
    server, ids = seeded
    consultation_id = ids["consultations"][0]

    [read] = await server.read_resource(f"orchestrator://consultation/{consultation_id}")
    tool = await server.call_tool(
        "orchestrator_get_consultation", {"consultation_id": consultation_id}
    )
    assert json.loads(read.content) == tool.structured_content
    [read] = await server.read_resource(f"orchestrator://workflow/{ids['workflows'][0]}")
    assert json.loads(read.content)["workflow"]["goal"] == "split the scanner out"


@pytest.mark.parametrize("kind", ["review", "workflow", "consultation"])
@pytest.mark.parametrize("unknown", ["0c8ab1d0-5d4c-4bd4-9d71-27c1d5b0e0a1", "not-an-id"])
async def test_an_id_nobody_issued_is_a_missing_resource(seeded, kind, unknown):
    """-32602, not a failure envelope handed back as if it were the record."""
    server, _ = seeded
    with pytest.raises(ResourceNotFoundError):
        await server.read_resource(f"orchestrator://{kind}/{unknown}")


async def test_completion_offers_what_exists_and_matches_the_prefix(seeded):
    server, ids = seeded
    workflow_id = ids["workflows"][1]

    async with Client(server) as client:
        assert client.server_capabilities.completions is not None
        agents = await client.complete(
            PromptReference(type="ref/prompt", name="consult"), {"name": "agent", "value": ""}
        )
        # `claude-opus` is the host's own runtime, which is never consulted, and
        # `codex-off` is disabled.
        assert agents.completion.values == ["codex-sol"]
        matched = await client.complete(
            ResourceTemplateReference(
                type="ref/resource", uri="orchestrator://workflow/{workflow_id}"
            ),
            {"name": "workflow_id", "value": workflow_id[:8]},
        )
        assert matched.completion.values == [workflow_id]
