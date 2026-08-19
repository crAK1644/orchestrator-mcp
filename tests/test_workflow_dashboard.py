"""The workflow pages, driven over real HTTP like the rest of the dashboard.

0.5.0 shipped the workflow layer and the only window onto the database could not see
it. Three properties carry the weight here: a database that predates the workflow
tables renders a sentence about restarting rather than a traceback, the step timeline
comes back in the order it happened rather than the order it was written, and the page
stays read-only -- deletion lives on the MCP tools, where the confirmation token is.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from orchestrator_mcp.consult.store import MIGRATIONS

from .test_consult_dashboard import _Client, config, serve  # noqa: F401 -- fixtures
from .test_workflow_service import (  # noqa: F401 -- fixtures
    AGENTS,
    BRIEF,
    FINDINGS,
    HOST_BINDINGS,
    PATCH,
    PLAN,
    RESEARCH,
    StubAdapter,
    StubWorkflow,
    finding_ids,
    host_step,
    repo,
    step,
    summary_with,
)


@pytest.fixture
def workflow_config(config, repo, host_claude):
    """The dashboard's config, with a workflow configured.

    `store_full_content` because `workflow:` refuses without it: a workflow *is* its
    stored plans, briefs, patches and findings.
    """

    def build(**overrides):
        return config(
            agents=dict(AGENTS),
            store_full_content=True,
            host={"runtime": "claude", "model": "claude-opus-5"},
            review={"reviewers": ["codex-sol"], "deep_reviewers": list(AGENTS)},
            workflow={
                "roots": [str(repo.parent)],
                # `research` is bound to an agent rather than the host so the workflow
                # owns a consultation: a review's reviewers get plain consultations,
                # and a step's link to one is a thing this page has to render.
                "bindings": {
                    **HOST_BINDINGS,
                    "research": {"agent": "codex-sol"},
                    "review": {"agents": ["codex-sol"]},
                },
            },
            **overrides,
        )

    return build


async def make_workflow(consult_config, repo, *, goal="split the scanner out", finish=True):
    """One real workflow in the store the dashboard reads. Returns its id.

    The whole loop rather than hand-written rows, because the page is being asked to
    render what the service actually writes -- including the review and the
    consultation a step links to.
    """
    adapters = {agent_id: StubAdapter() for agent_id in consult_config.agents}
    adapters["codex-sol"].answers = [RESEARCH, FINDINGS]
    service = await StubWorkflow(consult_config, "claude", adapters=adapters).open()
    try:
        response = await service.start(goal=goal, workdir=str(repo))
        assert response.error is None, response.error
        workflow_id = response.workflow_id
        if not finish:
            return workflow_id
        step_id, token = await step(service, workflow_id, "research")
        assert (await service.run_step(workflow_id, step_id, token)).error is None
        await host_step(service, workflow_id, "plan", json.loads(PLAN))
        await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
        await host_step(
            service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH}
        )
        await host_step(
            service, workflow_id, "test",
            {"command": "pytest -q", "workdir": str(repo), "exit_code": 0, "status": "passed"},
        )
        step_id, token = await step(service, workflow_id, "review")
        assert (await service.run_step(workflow_id, step_id, token)).error is None
        ids = await finding_ids(service, workflow_id)
        final = await host_step(service, workflow_id, "synthesize", summary_with("fixed", ids))
        assert final.status == "completed"
        return workflow_id
    finally:
        await service.close()


def old_database(path) -> None:
    """The store as an earlier version left it: every migration but the workflow one.

    The real ledger rather than a hand-built table, because what this stands in for is
    a running server that simply has not been restarted -- everything else on the page
    still has to work.
    """
    database = sqlite3.connect(path)
    with database:
        for statement in MIGRATIONS:
            if "CREATE TABLE workflow_runs" in statement:
                break
            database.executescript(statement)
    database.close()


# --- an un-migrated database ------------------------------------------------


async def test_the_workflows_page_asks_for_a_restart_rather_than_raising(serve, workflow_config):
    """The read-only connection cannot create the table, so the page has to say so."""
    consult_config = workflow_config()
    old_database(consult_config.database_path)
    get, _ = serve(consult_config)

    status, body = get("/workflows")

    assert status == 200
    assert "restart the MCP server" in body
    assert "no such table" not in body


async def test_a_workflow_page_on_an_un_migrated_database_is_a_sentence_not_a_500(
    serve, workflow_config
):
    consult_config = workflow_config()
    old_database(consult_config.database_path)
    get, _ = serve(consult_config)

    status, body = get("/workflows/6f1c9d20-0000-0000-0000-000000000000")

    assert status == 404
    assert "restart the MCP server" in body


async def test_the_index_carries_the_notice_when_workflows_are_configured(
    serve, workflow_config
):
    consult_config = workflow_config()
    old_database(consult_config.database_path)
    get, _ = serve(consult_config)

    _, body = get("/")

    assert "<h2>Workflows</h2>" in body
    assert "restart the MCP server" in body


async def test_a_database_that_does_not_exist_yet_is_not_a_restart(serve, workflow_config):
    get, _ = serve(workflow_config())

    status, body = get("/workflows")

    assert status == 200
    assert "No workflows recorded yet." in body
    assert "restart" not in body


async def test_an_install_that_runs_no_workflows_gets_no_workflows_section(serve):
    """No `workflow:` block and no history: the index does not advertise a feature
    nobody configured."""
    get, _ = serve()

    _, body = get("/")

    assert "<h2>Workflows</h2>" not in body
    assert "Workflows open" not in body


# --- history and detail -----------------------------------------------------


async def test_a_workflow_appears_on_the_index_with_its_state(serve, workflow_config, repo):
    get, consult_config = serve(workflow_config())
    workflow_id = await make_workflow(consult_config, repo)

    status, body = get("/")

    assert status == 200
    assert "<h2>Workflows</h2>" in body
    assert workflow_id[:8] in body
    assert "split the scanner out" in body
    assert "completed" in body


async def test_the_monitor_strip_counts_the_workflows_that_have_not_finished(
    serve, workflow_config, repo
):
    get, consult_config = serve(workflow_config())
    await make_workflow(consult_config, repo)

    _, body = get("/")
    assert "<dt>Workflows open</dt><dd>0</dd>" in body

    await make_workflow(consult_config, repo, goal="still going", finish=False)

    _, body = get("/")
    assert "<dt>Workflows open</dt><dd>1</dd>" in body


async def test_the_detail_page_shows_the_steps_in_the_order_they_happened(
    serve, workflow_config, repo
):
    """`round_index, attempt, sequence` is what those three columns were added for."""
    get, consult_config = serve(workflow_config())
    workflow_id = await make_workflow(consult_config, repo)

    status, body = get(f"/workflows/{workflow_id}")

    assert status == 200
    order = [
        body.index(f"<strong>{name}</strong>")
        for name in ("research", "plan", "implement", "test", "review", "synthesize")
    ]
    assert order == sorted(order)


async def test_the_detail_page_shows_the_frozen_bindings_and_the_fix_round_cap(
    serve, workflow_config, repo
):
    """The snapshot, not `config.workflow` as it reads now -- storing it was the point."""
    get, consult_config = serve(workflow_config())
    workflow_id = await make_workflow(consult_config, repo)

    _, body = get(f"/workflows/{workflow_id}")

    assert "<h2>Bindings</h2>" in body
    assert "codex-sol" in body
    assert "fix rounds 0 of" in body


async def test_a_step_links_to_its_consultation_and_its_review(serve, workflow_config, repo):
    """A workflow's consultations are unreachable from the consultation list, so
    without these links there is no way to read what a reviewer actually said."""
    get, consult_config = serve(workflow_config())
    workflow_id = await make_workflow(consult_config, repo)
    database = sqlite3.connect(consult_config.database_path)
    try:
        consultation_id = database.execute(
            "SELECT id FROM consultations WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()[0]
        review_id = database.execute(
            "SELECT id FROM reviews WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()[0]
    finally:
        database.close()

    _, body = get(f"/workflows/{workflow_id}")
    assert f"/consultation/{consultation_id}" in body
    assert f"/reviews/{review_id}" in body

    # And both resolve rather than 404.
    assert get(f"/consultation/{consultation_id}")[0] == 200
    assert get(f"/reviews/{review_id}")[0] == 200


async def test_a_workflow_consultation_links_back_to_its_workflow(serve, workflow_config, repo):
    get, consult_config = serve(workflow_config())
    workflow_id = await make_workflow(consult_config, repo)
    database = sqlite3.connect(consult_config.database_path)
    try:
        consultation_id = database.execute(
            "SELECT id FROM consultations WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()[0]
    finally:
        database.close()

    _, body = get(f"/consultation/{consultation_id}")

    assert f"/workflows/{workflow_id}" in body


async def test_a_standalone_consultation_says_nothing_about_workflows(
    serve, workflow_config, repo
):
    """Most consultations are nobody's step, and an empty row would be noise."""
    get, consult_config = serve(workflow_config())
    adapters = {agent_id: StubAdapter() for agent_id in consult_config.agents}
    service = await StubWorkflow(consult_config, "claude", adapters=adapters).open()
    try:
        response = await service.consult.consult(capability="research", prompt="unrelated")
        assert response.error is None, response.error
    finally:
        await service.close()

    _, body = get(f"/consultation/{response.consultation_id}")

    assert "<dt>Workflow</dt>" not in body


async def test_a_workflow_that_is_not_there_is_a_404(serve, workflow_config, repo):
    get, consult_config = serve(workflow_config())
    await make_workflow(consult_config, repo)

    status, body = get("/workflows/6f1c9d20-0000-0000-0000-000000000000")

    assert status == 404
    assert "No such workflow." in body


# --- read-only --------------------------------------------------------------


async def test_the_workflow_pages_offer_no_way_to_delete_anything(
    serve, workflow_config, repo
):
    """Deletion stays on the MCP tools, where the confirmation token lives. A GET that
    could erase a workflow would also be a GET a stray link could fire."""
    get, consult_config = serve(workflow_config(dashboard={"editable": True}))
    workflow_id = await make_workflow(consult_config, repo)

    for path in ("/workflows", f"/workflows/{workflow_id}"):
        _, body = get(path)
        assert "<form" not in body
        assert "Delete" not in body


# --- spend -------------------------------------------------------------------


async def test_the_workflow_pages_report_what_was_spent(serve, workflow_config, repo):
    """Both the delegated step and the reviewer, whose consultation hangs off the
    review rather than off the workflow."""
    consult_config = workflow_config()
    workflow_id = await make_workflow(consult_config, repo)
    get, _ = serve(consult_config)

    _, listing = get("/workflows")
    _, page = get(f"/workflows/{workflow_id}")

    assert "<th>Spend" in listing and "<th>Spend" in page
    # Two consulted steps at 12 tokens each, and the reviewer is one of them.
    assert "24 tokens" in listing
    assert page.count("12 tokens") == 2


async def test_an_unpriced_workflow_says_unknown_rather_than_zero(
    serve, workflow_config, repo
):
    """A free tier reports no price. `$0.0000` there would read as free."""
    consult_config = workflow_config()
    workflow_id = await make_workflow(consult_config, repo)
    get, _ = serve(consult_config)

    _, listing = get("/workflows")
    _, page = get(f"/workflows/{workflow_id}")

    assert "cost unknown" in listing and "cost unknown" in page
    assert "$0.00" not in listing and "$0.00" not in page


async def test_a_host_only_workflow_shows_no_spend_at_all(serve, workflow_config, repo):
    """Nothing was consulted, so there is nothing to report -- not a row of zeroes."""
    consult_config = workflow_config()
    await make_workflow(consult_config, repo, finish=False)
    get, _ = serve(consult_config)

    _, listing = get("/workflows")

    assert "tokens" not in listing
    assert "cost unknown" not in listing
