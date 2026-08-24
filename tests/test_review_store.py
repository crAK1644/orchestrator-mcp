"""Review persistence: the migration, the one-time token, deletion, and the
execution lease.

Two of these are the reason the file exists. Deletion has to collect the whole
tree before it removes anything, or a recheck's consultations outlive the review
that pointed at them -- exactly the material a user asked to erase. And the lease
has to be visible to a *second* process, because a cancel in one server leaves
subprocesses running in another and status alone cannot see them.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid

import pytest

from orchestrator_mcp.consult.contract import ConsultRoute, SourceMode
from orchestrator_mcp.consult.store import ConsultStore, StoreError
from orchestrator_mcp.review.store import ReviewStore, sha256


@pytest.fixture
async def store(tmp_path):
    consult = ConsultStore(tmp_path / "consultations.sqlite3", store_full_content=True)
    await consult.open()
    yield ReviewStore(consult)
    await consult.close()


async def plan(store, **overrides) -> str:
    review_id = overrides.pop("review_id", uuid.uuid4())
    await store.create_review(
        review_id=review_id,
        mode=overrides.pop("mode", "standard"),
        goal=overrides.pop("goal", "look at this"),
        context=overrides.pop("context", None),
        material=overrides.pop("material", []),
        material_sha256="a" * 64,
        raw_sha256="b" * 64,
        reviewer_snapshot=overrides.pop("reviewer_snapshot", [{"agent_id": "rev"}]),
        confirm_token=overrides.pop("confirm_token", "token"),
        secret_hits=[],
        web_requested=overrides.pop("web_requested", False),
        parent_review_id=overrides.pop("parent_review_id", None),
        **overrides,
    )
    return str(review_id)


async def consultation(store, agent_id="rev") -> str:
    """A real consultation row, so deletion is tested against the foreign keys it
    actually has to satisfy."""
    consultation_id = uuid.uuid4()
    await store.store.create_consultation(
        consultation_id=consultation_id,
        origin_runtime="claude",
        route=ConsultRoute(
            agent_id=agent_id, runtime="codex", model="m", capability_score=90,
            priority=10, explicitly_selected=True,
        ),
        capability="review",
        protocol_version="consult-v1",
        config_hash="deadbeef",
        conversation_label=None,
    )
    await store.store.record_turn(
        consultation_id, 1, SourceMode.DOCUMENT,
        user_prompt="q", context=None, compiled_prompt="q", raw_output="a",
    )
    return str(consultation_id)


# --- the migration ----------------------------------------------------------


async def test_the_migration_applies_to_a_database_created_before_it_existed(tmp_path):
    """Migrations are applied by index, so an installation that ran the 0.1 schema
    picks up the review tables on the next boot rather than needing a fresh file."""
    path = tmp_path / "old.sqlite3"
    old = ConsultStore(path, store_full_content=True)
    await old.open()
    before = await consultation(ReviewStore(old))
    await old.close()

    # Rewind the ledger to before the review migration, the way a database written
    # by the previous release looks.
    raw = sqlite3.connect(path)
    # Every table any migration after index 0 created, so re-running them all from a
    # rewound ledger is what this asserts. The `ADD COLUMN` statements in those
    # migrations do re-run against columns that are still there, and the loop's
    # `duplicate column name` tolerance is exactly what covers that.
    raw.executescript(
        "DROP TABLE reviews; DROP TABLE review_consultations; DROP TABLE review_leases; "
        "DROP TABLE review_delete_confirmations; "
        "DROP TABLE workflow_runs; DROP TABLE workflow_steps; "
        "DELETE FROM schema_migrations WHERE version >= 1;"
    )
    raw.commit()
    raw.close()

    new = ConsultStore(path, store_full_content=True)
    await new.open()
    store = ReviewStore(new)
    review_id = await plan(store)
    assert (await store.get_review(review_id)).status == "pending"
    # And the consultation written under the old schema is still there.
    assert await new.get_consultation(uuid.UUID(before)) is not None
    await new.close()


# --- redaction at the insert ------------------------------------------------


async def test_the_goal_and_context_are_redacted_on_their_way_in(store):
    secret = "sk-ant-api03-AAAABBBBCCCCDDDDEEEE"
    review_id = await plan(store, goal=f"review {secret}", context=f"key={secret}")

    review = await store.get_review(review_id)
    assert secret not in review.goal and secret not in review.context
    assert "[redacted]" in review.goal


async def test_the_material_manifest_is_scrubbed_too(store):
    """Its labels are host-supplied strings, and a path can carry a token."""
    secret = "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGG"
    review_id = await plan(
        store, material=[{"label": f"/tmp/{secret}/a.py", "kind": "file", "locator": "", "chars": 1}]
    )
    assert secret not in (await store.get_review(review_id)).material_json


async def test_turning_off_content_storage_still_leaves_a_runnable_review(store, tmp_path):
    """`store_full_content: false` drops model output. It cannot drop the goal: the
    second half of the handshake reads it back to send, so nulling it would make
    every review impossible to run rather than merely unlogged."""
    consult = ConsultStore(tmp_path / "quiet.sqlite3", store_full_content=False)
    await consult.open()
    quiet = ReviewStore(consult)
    review_id = await plan(quiet, goal="the goal")
    await quiet.record_reviewer_result(
        review_id, "rev", status="ok", findings=[{"finding_id": "rev-1"}], answer="the answer"
    )

    assert (await quiet.get_review(review_id)).goal == "the goal"
    row = (await quiet.reviewer_rows(review_id))[0]
    assert row.answer is None and row.findings_json is None
    assert row.status == "ok"  # the shape survives; the bodies do not
    await consult.close()


# --- the one-time token -----------------------------------------------------


async def test_the_token_starts_the_review_and_cannot_be_spent_twice(store):
    review_id = await plan(store, confirm_token="tok")
    await store.consume_confirm_token(review_id, "tok")

    assert (await store.get_review(review_id)).status == "running"
    with pytest.raises(StoreError):
        await store.consume_confirm_token(review_id, "tok")


async def test_only_the_hash_of_the_token_is_stored(store):
    review_id = await plan(store, confirm_token="tok")
    row = await store.get_review(review_id)
    assert row.confirm_token_sha == sha256("tok")
    assert "tok" != row.confirm_token_sha


async def test_a_wrong_token_does_not_start_the_review(store):
    review_id = await plan(store, confirm_token="tok")
    with pytest.raises(StoreError):
        await store.consume_confirm_token(review_id, "guess")
    assert (await store.get_review(review_id)).status == "pending"


# --- guarded transitions ----------------------------------------------------


async def test_a_transition_from_the_wrong_state_changes_nothing(store):
    review_id = await plan(store)
    assert await store.transition(review_id, "complete", ("awaiting_synthesis",)) is False
    assert (await store.get_review(review_id)).status == "pending"


async def test_a_cancel_is_not_overwritten_by_a_batch_finishing_afterwards(store):
    review_id = await plan(store, confirm_token="t")
    await store.consume_confirm_token(review_id, "t")
    assert await store.transition(review_id, "cancelled", ("running",)) is True

    # The `gather` lands a moment later and finds the door closed.
    assert await store.transition(review_id, "awaiting_synthesis", ("running",), "all") is False
    assert (await store.get_review(review_id)).status == "cancelled"


# --- reviewer rows ----------------------------------------------------------


async def test_a_retry_updates_the_reviewer_in_place(store):
    """One row per `(review, agent)`, so no consultation is left dangling where the
    delete cannot find it."""
    review_id = await plan(store)
    first = await consultation(store)
    await store.record_reviewer_result(review_id, "rev", status="failed", error_code="timeout")
    await store.record_reviewer_result(review_id, "rev", status="ok", consultation_id=first)

    rows = await store.reviewer_rows(review_id)
    assert len(rows) == 1
    assert rows[0].status == "ok" and rows[0].consultation_id == first


async def test_parser_metadata_survives_the_round_trip(store):
    review_id = await plan(store)
    await store.record_reviewer_result(
        review_id,
        "rev",
        status="ok",
        findings=[],
        findings_parsed=True,
        findings_truncated=3,
    )

    row = (await store.reviewer_rows(review_id))[0]
    assert bool(row.findings_parsed) is True
    assert row.findings_truncated == 3


# --- deletion ---------------------------------------------------------------


async def test_deleting_a_parent_removes_its_rechecks_consultations_too(store):
    parent = await plan(store)
    child = await plan(store, parent_review_id=parent)
    kept = await consultation(store, "unrelated")
    parent_c = await consultation(store)
    child_c = await consultation(store)
    await store.record_reviewer_result(parent, "rev", status="ok", consultation_id=parent_c)
    await store.record_reviewer_result(child, "rev", status="ok", consultation_id=child_c)

    assert await store.delete_review(parent) == 2

    with pytest.raises(StoreError):
        await store.get_review(child)
    for gone in (parent_c, child_c):
        with pytest.raises(StoreError):
            await store.store.get_consultation(uuid.UUID(gone))
    # And an unrelated consultation is untouched.
    assert await store.store.get_consultation(uuid.UUID(kept)) is not None


async def test_a_workflow_owned_review_is_not_deleted_from_the_review_tool(store):
    """`workflow_steps.review_id` has no `REFERENCES` clause, so nothing else would
    say a word -- and the workflow would not merely look intact, it would run wrong.

    `WorkflowService._open_findings` reads a step's findings back through
    `ReviewService.get`, which answers a missing review with an error envelope rather
    than raising, so the next fix round would be handed `[]` and would answer from the
    goal instead of from the review.
    """
    review_id = await plan(store, workflow_id="wf-1", step_id="st-1")

    with pytest.raises(StoreError, match="is a step of workflow `wf-1`"):
        await store.delete_review(review_id)
    assert (await store.get_review(review_id)).status == "pending"


async def test_a_recheck_of_a_workflow_review_goes_but_its_parent_stays(store):
    """The guard runs over the expanded tree, and a recheck is not itself a step.

    `orchestrator_review` takes `parent_review_id` but no `workflow_id`, so a recheck
    somebody ran against a workflow's review is their own review and deleting it costs
    the workflow nothing. Deleting the *parent* would take that recheck with it, which
    is why the guard reads the tree rather than the roots.
    """
    parent = await plan(store, workflow_id="wf-1", step_id="st-1")
    child = await plan(store, parent_review_id=parent)

    with pytest.raises(StoreError, match="is a step of workflow"):
        await store.delete_review(parent)
    assert (await store.get_review(child)).parent_review_id is not None

    assert await store.delete_review(child) == 1
    assert (await store.get_review(parent)).status == "pending"


async def test_the_delete_all_count_leaves_workflow_reviews_out(store):
    """Snapshotted and then refused would mean approving a number that never happens.

    The count is the contract: what the user was shown is what gets deleted.
    """
    standalone = await plan(store)
    owned = await plan(store, workflow_id="wf-1", step_id="st-1")

    token, count = await store.request_delete_all()

    assert count == 1
    assert await store.delete_all_reviews(token) == 1
    with pytest.raises(StoreError):
        await store.get_review(standalone)
    assert (await store.get_review(owned)).status == "pending"


async def test_deleting_a_running_review_is_refused(store):
    review_id = await plan(store, confirm_token="t")
    await store.consume_confirm_token(review_id, "t")
    with pytest.raises(StoreError, match="still running"):
        await store.delete_review(review_id)


# --- the execution lease ----------------------------------------------------


async def test_a_cancelled_review_cannot_be_deleted_while_its_reviewers_run(store, tmp_path):
    """The blocker this design exists for. One process cancels and deletes; another
    still has subprocesses that are about to write their rows. Status says the review
    is cancellable; only the lease knows the work is live.

    A second `ConsultStore` on the same file is a second process for this purpose --
    an `asyncio.Lock` would not have been visible across it.
    """
    review_id = await plan(store, confirm_token="t")
    await store.consume_confirm_token(review_id, "t")

    other = ConsultStore(store.store.path, store_full_content=True)
    await other.open()
    second = ReviewStore(other)
    try:
        async with store.lease(review_id, ttl_s=60):
            await second.transition(review_id, "cancelled", ("running",))
            with pytest.raises(StoreError, match="in flight"):
                await second.delete_review(review_id)
        # Released, and now it goes.
        assert await second.delete_review(review_id) == 1
    finally:
        await other.close()


async def test_a_crashed_holder_does_not_wedge_the_review_forever(store):
    review_id = await plan(store)
    await store._run(
        lambda: store._db.execute(
            "INSERT INTO review_leases (review_id, holder, expires_at) VALUES (?,?,?)",
            (review_id, "pid-dead", time.time() - 1),
        )
    )
    # Expired rows are purged inside the same transaction as the check.
    assert await store.delete_review(review_id) == 1


async def test_two_batches_cannot_hold_one_review_at_once(store):
    review_id = await plan(store)
    async with store.lease(review_id, ttl_s=60):
        with pytest.raises(StoreError, match="in flight"):
            async with store.lease(review_id, ttl_s=60):
                pass


async def test_a_live_review_lease_is_renewed_past_its_initial_expiry(store):
    """The renew loop moves the deadline forward while the holder is still working.

    Asserted against the deadline this lease was granted, never against the clock. A
    renewal sets `now + ttl`, and this ttl is 60ms across a thread pool, so whether the
    stored deadline is still ahead of `time.time()` at any moment a test happens to
    look depends on how the scheduler treated the renew task -- which is a fact about
    the machine and not about the lease. Comparing the two observations instead is the
    same property with nothing timing-dependent left in it.
    """
    review_id = await plan(store)
    async with store.lease(review_id, ttl_s=0.06):
        initial = (
            await store._run(
                lambda: store._db.execute(
                    "SELECT expires_at FROM review_leases WHERE review_id = ?", (review_id,)
                ).fetchone()[0]
            )
        )
        await asyncio.sleep(0.08)
        renewed = (
            await store._run(
                lambda: store._db.execute(
                    "SELECT expires_at FROM review_leases WHERE review_id = ?", (review_id,)
                ).fetchone()[0]
            )
        )
        assert renewed > initial


# --- delete-all -------------------------------------------------------------


async def test_a_review_created_after_the_count_survives_the_confirmation(store):
    """An approval is for the reviews the user was shown. One that arrived since was
    never shown and was never approved."""
    old = await plan(store)
    token, count = await store.request_delete_all()
    new = await plan(store, parent_review_id=old)

    assert count == 1
    assert await store.delete_all_reviews(token) == 1
    with pytest.raises(StoreError):
        await store.get_review(old)
    assert (await store.get_review(new)).status == "pending"
    assert (await store.get_review(new)).parent_review_id is None


async def test_an_expired_confirmation_is_refused_rather_than_widened(store):
    await plan(store)
    token, _ = await store.request_delete_all(ttl_s=-1)
    with pytest.raises(StoreError, match="expired"):
        await store.delete_all_reviews(token)
    assert len(await store.list_reviews()) == 1


async def test_a_confirmation_is_spent_once(store):
    await plan(store)
    token, _ = await store.request_delete_all()
    assert await store.delete_all_reviews(token) == 1
    with pytest.raises(StoreError, match="not outstanding"):
        await store.delete_all_reviews(token)


async def test_a_refused_delete_does_not_burn_the_confirmation(store):
    review_id = await plan(store, confirm_token="run")
    token, _ = await store.request_delete_all()
    await store.consume_confirm_token(review_id, "run")

    with pytest.raises(StoreError, match="still running"):
        await store.delete_all_reviews(token)
    await store.transition(review_id, "cancelled", ("running",))
    assert await store.delete_all_reviews(token) == 1


async def test_delete_confirmation_storage_contains_only_the_hash(store):
    token, _ = await store.request_delete_all()
    rows = await store._run(
        lambda: store._db.execute(
            "SELECT token_sha FROM review_delete_confirmations"
        ).fetchall()
    )
    assert [row[0] for row in rows] == [sha256(token)]
    assert token not in rows[0][0]


async def test_fix_round_updates_are_serialized_across_connections(store):
    review_id = await plan(store)
    other_store = ConsultStore(store.store.path, store_full_content=True)
    await other_store.open()
    other = ReviewStore(other_store)
    try:
        await asyncio.gather(
            store.append_fix_round(review_id, {"finding_ids": ["a"], "outcome": "applied"}),
            other.append_fix_round(review_id, {"finding_ids": ["b"], "outcome": "skipped"}),
        )
        saved = await store.get_review(review_id)
        assert len(json.loads(saved.fix_rounds_json)) == 2
    finally:
        await other_store.close()


async def test_an_unknown_confirmation_deletes_nothing(store):
    await plan(store)
    with pytest.raises(StoreError):
        await store.delete_all_reviews("made-up")
    assert len(await store.list_reviews()) == 1
