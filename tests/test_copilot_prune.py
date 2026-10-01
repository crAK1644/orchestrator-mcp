"""Deleting a consultation removes the Copilot session it left behind.

The directory and the lock the CLI keeps under Orchestrator's own `COPILOT_HOME` hold each
prompt and answer unmasked, so a delete that left them would leave what it was asked to
erase. The sessions are named in the rows about to go, read before they go and removed only
once the delete has committed. The store the CLI keeps beside them, `session-store.db`, holds
a second copy that none of this reaches, and the docs have to say so.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator_mcp.consult.adapters import copilot_cli
from orchestrator_mcp.consult.contract import ConsultRoute
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.store import ConsultStore, StoreError
from orchestrator_mcp.review.store import ReviewStore
from orchestrator_mcp.workflow.store import WorkflowStore


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    """The `session-state` of a Copilot home of the test's own."""
    monkeypatch.setattr(copilot_cli, "_root", lambda: tmp_path / "copilot")
    return tmp_path / "copilot" / "home" / "session-state"


@pytest.fixture
async def store(tmp_path):
    consult = await ConsultStore(tmp_path / "consultations.sqlite3").open()
    yield consult
    await consult.close()


def keep(state: Path, session: str) -> None:
    """What the CLI leaves for a session: a directory with a prompt in it, and a lock."""
    (state / session).mkdir(parents=True)
    (state / session / "events.jsonl").write_text("the prompt, unmasked\n")
    locks = state / ".session-operation-locks"
    locks.mkdir(exist_ok=True)
    (locks / f"{session}.lock").touch()


def left(state: Path, session: str) -> list[str]:
    """What of one session is still there."""
    lock = state / ".session-operation-locks" / f"{session}.lock"
    return [p.name for p in (state / session, lock) if p.exists()]


async def consult(store, state, runtime="copilot", *, bound=True, **owner) -> str:
    """A consultation, and the session its first turn made: named for the consultation.

    `bound=False` is a first turn that failed before the session id was recorded."""
    consultation_id = uuid4()
    await store.create_consultation(
        consultation_id=consultation_id,
        origin_runtime="claude",
        route=ConsultRoute(
            agent_id=f"{runtime}-x", runtime=runtime, model="m", capability_score=90,
            priority=10, explicitly_selected=True,
        ),
        capability="research",
        protocol_version="consult-v1",
        config_hash="abc123",
        **owner,
    )
    session = str(consultation_id)
    keep(state, session)
    if bound:
        await store.bind_native_session(consultation_id, session)
    return session


async def a_review(store, state):
    """A review that owns one reviewer's consultation, and the session that left."""
    reviews = ReviewStore(store)
    review_id = uuid4()
    await reviews.create_review(
        review_id=review_id, mode="standard", goal="look", context=None, material=[],
        material_sha256="a" * 64, raw_sha256="b" * 64, reviewer_snapshot=[{"agent_id": "rev"}],
        confirm_token="token", secret_hits=[], web_requested=False, parent_review_id=None,
    )
    owned = await consult(store, state)
    await reviews.record_reviewer_result(
        str(review_id), "rev", status="ok", consultation_id=owned
    )
    return reviews, review_id, owned


async def a_workflow(tmp_path, store, state):
    """A finished workflow that owns one step's consultation, and the session that left."""
    workflows = WorkflowStore(store)
    workflow_id = str(uuid4())
    await workflows.create_workflow(
        workflow_id, "goal", str(tmp_path), "claude", None, {}, {}, "hash", None
    )
    step = await consult(store, state, workflow_id=workflow_id)
    await store._run(lambda: store._db.execute("UPDATE workflow_runs SET status = 'completed'"))
    return workflows, workflow_id, step


async def hold(store, consultation_id: str, expires_in: float = 600) -> None:
    """The lease a turn holds on its consultation while it runs. A negative `expires_in`
    is one a crashed process left behind."""
    await store._run(
        lambda: store._db.execute(
            "INSERT INTO consultation_leases VALUES (?, 'x', ?)",
            (consultation_id, time.time() + expires_in),
        )
    )


# --- the consultation tools -------------------------------------------------


async def test_deleting_a_consultation_removes_its_session_and_lock_and_no_ones_else(store, state):
    gone, other = await consult(store, state), await consult(store, state)

    assert await store.delete_consultation(gone) == 1

    assert left(state, gone) == []
    assert len(left(state, other)) == 2


async def test_a_first_turn_that_failed_before_its_session_was_bound_is_removed_too(store, state):
    """The adapter names a first turn's session after the consultation, so the directory
    is there under that id whether or not the store ever heard back."""
    failed = await consult(store, state, bound=False)

    assert await store.delete_consultation(failed) == 1

    assert left(state, failed) == []


async def test_only_a_copilot_consultation_names_a_copilot_session(store, state):
    codex = await consult(store, state, "codex")

    assert await store.delete_consultation(codex) == 1

    assert len(left(state, codex)) == 2


async def test_a_refused_delete_keeps_the_session(store, state):
    busy = await consult(store, state)
    await hold(store, busy)

    with pytest.raises(StoreError) as refused:
        await store.delete_consultation(busy)

    assert refused.value.code is ConsultErrorCode.SESSION_BUSY
    assert len(left(state, busy)) == 2


async def test_delete_all_removes_every_ordinary_consultations_session(store, state):
    first, second = await consult(store, state), await consult(store, state)
    token, count = await store.request_delete_all_consultations()

    assert (count, await store.delete_all_consultations(token)) == (2, 2)

    assert left(state, first) == left(state, second) == []


@pytest.fixture
def logged(caplog, monkeypatch):
    """The store's warnings, whether or not a server was built first.

    `log.configure` sets `propagate = False` on the package logger and `caplog` listens on
    the root, so propagation is switched back on for the test."""
    monkeypatch.setattr(logging.getLogger("orchestrator_mcp"), "propagate", True)
    caplog.set_level(logging.WARNING, logger="orchestrator_mcp.consult.store")
    return caplog


async def test_a_session_that_cannot_be_removed_does_not_undo_the_delete(
    store, state, monkeypatch, logged
):
    def refuse(home, session):
        raise RuntimeError("cannot")

    monkeypatch.setattr(copilot_cli, "_forget", refuse)
    doomed = await consult(store, state)

    assert await store.delete_consultation(doomed) == 1

    with pytest.raises(StoreError):
        await store.get_consultation(doomed)
    (warning,) = [r for r in logged.records if "Copilot session" in r.getMessage()]
    assert warning.exc_info[0] is RuntimeError


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes a directory it has no bit for")
async def test_a_directory_the_user_cannot_empty_is_left_and_the_delete_still_succeeds(store, state):
    """Not a patched failure: a session directory with no write bit, so its files cannot be
    unlinked. `rmtree` is told to ignore that, so the directory stays and nothing is raised."""
    stuck = await consult(store, state)
    (state / stuck).chmod(0o500)
    try:
        assert await store.delete_consultation(stuck) == 1

        with pytest.raises(StoreError):
            await store.get_consultation(stuck)
        assert (state / stuck / "events.jsonl").exists()
    finally:
        (state / stuck).chmod(0o700)


# --- what owns consultations ------------------------------------------------


async def test_deleting_a_review_removes_its_reviewers_sessions(store, state):
    reviews, review_id, owned = await a_review(store, state)
    other = await consult(store, state)

    assert await reviews.delete_review(review_id) == 1

    assert left(state, owned) == []
    assert len(left(state, other)) == 2


async def test_deleting_a_workflow_removes_its_steps_sessions(tmp_path, store, state):
    workflows, workflow_id, step = await a_workflow(tmp_path, store, state)
    other = await consult(store, state)

    assert await workflows.delete_workflow(workflow_id) == 1

    assert left(state, step) == []
    assert len(left(state, other)) == 2


# --- a turn in flight -------------------------------------------------------
#
# A reviewer's consultation can be resumed on its own through `orchestrator_consult`, and
# that turn takes a lease on the consultation and none on the review. The owner's delete
# has to honor it as the consultation's own delete does, or the CLI is left writing to a
# session directory that has just been removed.


async def test_a_review_is_not_deleted_under_a_reviewers_turn_in_flight(store, state):
    reviews, review_id, owned = await a_review(store, state)
    await hold(store, owned)

    with pytest.raises(StoreError) as refused:
        await reviews.delete_review(review_id)

    assert refused.value.code is ConsultErrorCode.SESSION_BUSY
    assert len(left(state, owned)) == 2
    await reviews.get_review(review_id)
    await store.get_consultation(owned)

    await store._run(lambda: store._db.execute("DELETE FROM consultation_leases"))
    assert await reviews.delete_review(review_id) == 1
    assert left(state, owned) == []


async def test_a_workflow_is_not_deleted_under_a_steps_turn_in_flight(tmp_path, store, state):
    workflows, workflow_id, step = await a_workflow(tmp_path, store, state)
    await hold(store, step)

    with pytest.raises(StoreError) as refused:
        await workflows.delete_workflow(workflow_id)

    assert refused.value.code is ConsultErrorCode.SESSION_BUSY
    assert len(left(state, step)) == 2
    await workflows.get_workflow(workflow_id)
    await store.get_consultation(step)

    await store._run(lambda: store._db.execute("DELETE FROM consultation_leases"))
    assert await workflows.delete_workflow(workflow_id) == 1
    assert left(state, step) == []


async def test_a_lease_that_lapsed_does_not_hold_a_delete(store, state):
    """A process that died mid-turn leaves its lease row behind, and it expires rather than
    being released. It must not keep a review undeletable."""
    reviews, review_id, owned = await a_review(store, state)
    await hold(store, owned, expires_in=-1)

    assert await reviews.delete_review(review_id) == 1

    assert left(state, owned) == []


# --- the retention sweep ----------------------------------------------------


async def test_the_retention_sweep_removes_the_sessions_of_what_it_deletes(tmp_path, state):
    database = tmp_path / "consultations.sqlite3"
    first = await ConsultStore(database).open()
    stale, fresh = await consult(first, state), await consult(first, state)
    await first._run(
        lambda: first._db.execute(
            "UPDATE consultations SET updated_at = '2026-01-01T00:00:00Z' WHERE id = ?", (stale,)
        )
    )
    await first.close()

    swept = await ConsultStore(database, retention_days=30).open()

    assert left(state, stale) == []
    assert len(left(state, fresh)) == 2
    await swept.close()


async def test_the_retention_sweep_leaves_a_review_whose_reviewer_has_a_turn_in_flight(
    tmp_path, state
):
    """Resuming a reviewer's consultation touches the consultation and not the review, so
    the review still reads as untouched for as long as the retention window says."""
    database = tmp_path / "consultations.sqlite3"
    first = await ConsultStore(database).open()
    reviews, review_id, owned = await a_review(first, state)
    await hold(first, owned)
    await first._run(
        lambda: first._db.execute(
            "UPDATE reviews SET updated_at = '2026-01-01T00:00:00Z' WHERE id = ?",
            (str(review_id),),
        )
    )
    await first.close()

    swept = await ConsultStore(database, retention_days=30).open()

    await ReviewStore(swept).get_review(review_id)
    assert len(left(state, owned)) == 2
    await swept.close()


# --- the removal itself -----------------------------------------------------


def test_a_name_that_is_not_a_uuid_is_never_turned_into_a_path(state):
    """The names come out of a database row. One that climbs out of `session-state`, or
    names `session-state` itself, must not take what it reaches. `session-state` has to
    exist for the first to climb at all."""
    session = str(uuid4())
    keep(state, session)
    outside = state.parent / "outside"
    outside.mkdir()

    copilot_cli.forget_sessions(["../outside", "", ".", "not-a-uuid"])

    assert outside.is_dir()
    assert len(left(state, session)) == 2


def test_forgetting_in_a_home_that_is_not_there_creates_nothing(tmp_path, state):
    copilot_cli.forget_sessions([str(uuid4())])

    assert not (tmp_path / "copilot").exists()


# --- what the docs say ------------------------------------------------------


@pytest.mark.parametrize("name", ["README.md", "PRIVACY.md"])
def test_the_docs_say_the_clis_own_store_keeps_a_second_copy(name):
    """A session that "goes with" a delete, and a doc that stops there, would promise an
    erasure the CLI's own `session-store.db` outlives. The name alone would survive a
    rewrite that claimed the opposite, so the claim is pinned with it."""
    doc = (Path(__file__).resolve().parents[1] / name).read_text()

    assert "`session-store.db`" in doc
    assert "second copy of each prompt and answer" in doc
