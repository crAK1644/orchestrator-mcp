"""`retention_days`: what the startup sweep takes, and what it has to leave."""

from __future__ import annotations

import asyncio
import time
from functools import partial
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


async def test_a_record_touched_after_the_sweep_picked_it_stays(tmp_path):
    """The sweep picks its ids before each delete's transaction opens, so the delete
    checks the age again: another server may have resumed the record in between."""
    store = await ConsultStore(tmp_path / "consultations.sqlite3").open()
    workflows, reviews = WorkflowStore(store), ReviewStore(store)
    workflow_id, review_id, consultation_id = str(uuid4()), uuid4(), uuid4()
    await workflows.create_workflow(
        workflow_id, "goal", str(tmp_path), "claude", None, {}, {}, "hash", None
    )
    await store._run(lambda: store._db.execute("UPDATE workflow_runs SET status = 'completed'"))
    await reviews.create_review(
        review_id, "quick", "goal", None, [], "", "", [], "token", [], False
    )
    await store.create_consultation(
        consultation_id=consultation_id,
        origin_runtime="claude",
        route=ROUTE,
        capability="research",
        protocol_version="consult-v1",
        config_hash="abc123",
    )

    async def dated(table: str, row_id: str, updated_at: str) -> None:
        await store._run(
            lambda: store._db.execute(
                f"UPDATE {table} SET updated_at = ? WHERE id = ?", (updated_at, row_id)
            )
        )

    cutoff = "2026-02-01T00:00:00+00:00"
    for table, delete, row_id in [
        ("workflow_runs", workflows._delete, workflow_id),
        ("reviews", reviews._delete, str(review_id)),
        ("consultations", store._delete_consultations, str(consultation_id)),
    ]:
        sweep = partial(delete, [row_id], stale_before=cutoff)
        # Stale when the sweep picked it, then resumed before the delete ran.
        await dated(table, row_id, "2026-03-01T00:00:00+00:00")
        assert await store._run(sweep) == 0
        await dated(table, row_id, "2026-01-01T00:00:00+00:00")
        assert await store._run(sweep) == 1
    await store.close()


def test_retention_is_off_unless_asked_for():
    assert ConsultConfig(**consult_block()).retention_days is None
    with pytest.raises(ValidationError):
        ConsultConfig(**consult_block(retention_days=0))


# --- the daily tick ---------------------------------------------------------


class Ticks:
    """Stands in for `asyncio.sleep`: each `await` waits for the test to release one tick."""

    def __init__(self) -> None:
        self.asked: list[float] = []
        self._release = asyncio.Semaphore(0)
        self.waiting = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)
        self.waiting.set()
        await self._release.acquire()

    async def tick(self) -> None:
        """Release one pause and wait until the loop is paused again."""
        self.waiting.clear()
        self._release.release()
        await asyncio.wait_for(self.waiting.wait(), 5)


async def stale_consultation(store: ConsultStore) -> str:
    consultation_id = uuid4()
    await store.create_consultation(
        consultation_id=consultation_id,
        origin_runtime="claude",
        route=ROUTE,
        capability="research",
        protocol_version="consult-v1",
        config_hash="abc123",
    )
    await store._run(
        lambda: store._db.execute(
            "UPDATE consultations SET updated_at = '2026-01-01T00:00:00Z' WHERE id = ?",
            (str(consultation_id),),
        )
    )
    return str(consultation_id)


async def consultation_ids(store: ConsultStore) -> set[str]:
    return await store._run(
        lambda: {row[0] for row in store._db.execute("SELECT id FROM consultations")}
    )


async def opened_with_ticks(tmp_path, days: int | None = 30) -> tuple[ConsultStore, Ticks]:
    """Opened, with the pause swapped in before the task first reaches it."""
    ticks = Ticks()
    store = ConsultStore(tmp_path / "consultations.sqlite3", retention_days=days)
    store._pause = ticks
    await store.open()
    if days:
        await asyncio.wait_for(ticks.waiting.wait(), 5)
    return store, ticks


async def test_a_tick_sweeps_what_went_stale_while_the_server_ran(tmp_path):
    store, ticks = await opened_with_ticks(tmp_path)
    stale, fresh = await stale_consultation(store), uuid4()
    await store.create_consultation(
        consultation_id=fresh,
        origin_runtime="claude",
        route=ROUTE,
        capability="research",
        protocol_version="consult-v1",
        config_hash="abc123",
    )
    assert stale in await consultation_ids(store)  # the start-up sweep is long past

    await ticks.tick()

    assert await consultation_ids(store) == {str(fresh)}
    assert ticks.asked[0] == 86_400
    await store.close()


async def test_close_cancels_the_task_and_leaves_nothing_pending(tmp_path):
    store, _ = await opened_with_ticks(tmp_path)
    task = store._retention_task

    await store.close()

    assert task.cancelled()
    assert not [t for t in asyncio.all_tasks() if "_retain" in repr(t)]


async def test_no_task_without_retention_days(tmp_path):
    store, _ = await opened_with_ticks(tmp_path, days=None)

    assert store._retention_task is None
    await store.close()


async def test_a_failing_tick_does_not_end_the_loop(tmp_path, monkeypatch):
    store, ticks = await opened_with_ticks(tmp_path)
    calls = []

    async def broken(days: int) -> None:
        calls.append(days)
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "_sweep", broken)

    await ticks.tick()
    await ticks.tick()

    assert calls == [30, 30]
    assert not store._retention_task.done()
    await store.close()


async def test_a_task_that_died_comes_back_on_the_next_open(tmp_path):
    store, _ = await opened_with_ticks(tmp_path)
    store._retention_task.cancel()
    await asyncio.sleep(0)
    dead = store._retention_task
    assert dead.done()

    await store.open()

    assert store._retention_task is not dead
    assert not store._retention_task.done()
    await store.close()


async def test_opening_twice_starts_one_task(tmp_path):
    store, _ = await opened_with_ticks(tmp_path)
    task = store._retention_task

    await asyncio.gather(store.open(), store.open())

    assert store._retention_task is task
    await store.close()


async def test_an_open_that_overlaps_a_close_waits_for_it_and_leaves_a_working_store(tmp_path):
    """`close` yields while the cancelled task unwinds. An `open` in that gap used to
    take the old connection, which `close` then shut, or start a task `close` never saw."""
    store, _ = await opened_with_ticks(tmp_path)
    closing = asyncio.create_task(store.close())
    await asyncio.sleep(0)  # close has cancelled the task and is waiting for it

    await store.open()
    await closing

    assert store._connection is not None
    assert await consultation_ids(store) == set()  # usable, not merely non-None
    assert not store._retention_task.done()
    await store.close()
    assert store._retention_task.done()
