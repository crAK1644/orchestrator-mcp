"""`retention_days`: what the startup sweep takes, and what it has to leave."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.store import ConsultStore
from orchestrator_mcp.review.store import ReviewStore
from orchestrator_mcp.workflow.store import WorkflowStore

from .conftest import consult_block
from .test_listing import ROUTE


async def test_the_sweep_takes_finished_history_and_leaves_what_is_live(tmp_path):
    db = tmp_path / "consultations.sqlite3"
    store = await ConsultStore(db).open()
    workflows, reviews = WorkflowStore(store), ReviewStore(store)

    async def sql(statement: str, *args) -> list:
        return await store._run(
            lambda: [tuple(row) for row in store._db.execute(statement, args)]
        )

    async def consultation(**kwargs) -> str:
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
        return str(consultation_id)

    async def workflow(status: str | None = None) -> str:
        workflow_id = str(uuid4())
        await workflows.create_workflow(
            workflow_id, "goal", str(tmp_path), "claude", None, {}, {}, "hash", None
        )
        if status:
            await sql("UPDATE workflow_runs SET status = ? WHERE id = ?", status, workflow_id)
        return workflow_id

    async def review(status: str) -> str:
        review_id = uuid4()
        await reviews.create_review(
            review_id, "quick", "goal", None, [], "", "", [], "token", [], False
        )
        await sql("UPDATE reviews SET status = ? WHERE id = ?", status, str(review_id))
        return str(review_id)

    finished, still_open = await workflow("completed"), await workflow()
    await consultation(workflow_id=finished)
    await review("awaiting_synthesis")
    running = await review("running")
    await consultation()
    leased, fresh = await consultation(), await consultation()
    await sql("INSERT INTO consultation_leases VALUES (?, 'x', ?)", leased, time.time() + 600)
    for table in ("workflow_runs", "reviews", "consultations"):
        await sql(f"UPDATE {table} SET updated_at = '2026-01-01T00:00:00Z' WHERE id != ?", fresh)
    await store.close()

    store = await ConsultStore(db, retention_days=30).open()

    assert await sql("SELECT id FROM workflow_runs") == [(still_open,)]
    assert await sql("SELECT id FROM reviews") == [(running,)]
    # The finished workflow's consultation went with it.
    assert sorted(await sql("SELECT id FROM consultations")) == sorted([(leased,), (fresh,)])
    await store.close()


def test_retention_is_off_unless_asked_for():
    assert ConsultConfig(**consult_block()).retention_days is None
    with pytest.raises(ValidationError):
        ConsultConfig(**consult_block(retention_days=0))
