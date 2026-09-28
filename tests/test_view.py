"""The MCP Apps view: which tools point at it, and that it is what gets served."""

from __future__ import annotations

from orchestrator_mcp.server import VIEW_URI, build_server

from .conftest import consult_block


async def test_every_tool_that_names_the_view_can_read_it(host_claude):
    server = build_server(
        {
            "consult": consult_block(
                review={"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol"]},
                workflow={"bindings": {"research": {"agent": "codex-sol"}}},
            )
        }
    )

    pointed = {t.name: t.meta["ui"]["resourceUri"] for t in await server.list_tools() if t.meta}
    assert pointed == dict.fromkeys(
        ["orchestrator_finalize_review", "orchestrator_get_review", "orchestrator_workflow_status"],
        VIEW_URI,
    )
    [resource] = await server.list_resources()
    assert (resource.uri, resource.mime_type) == (VIEW_URI, "text/html;profile=mcp-app")
    [read] = await server.read_resource(VIEW_URI)
    assert "ui/initialize" in read.content
    # Reviewer text is another model's output: the page never parses it as markup.
    assert "innerHTML" not in read.content and "insertAdjacentHTML" not in read.content


async def test_a_consult_only_server_offers_no_view(host_claude):
    assert await build_server({"consult": consult_block()}).list_resources() == []
