"""Deleting a workflow: the whole tree or none of it.

A workflow's consultations are refused by every consultation delete path and its
reviews by `refuse_workflow_owned`, so this is the only way any of those rows leave
the database. That makes two properties worth the file: everything the workflow owns
goes together, and nothing that is still moving goes at all.

Offline, on the fixtures `test_workflow_service.py` already builds.
"""

from __future__ import annotations

import json
import time

import pytest

from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.store import StoreError
from orchestrator_mcp.workflow.store import _sha256

from .test_workflow_service import (  # noqa: F401 -- fixtures
    FINDINGS,
    PATCH,
    PLAN,
    BRIEF,
    build,
    finding_ids,
    host_step,
    repo,
    sql,
    started,
    step,
    summary_with,
)


async def finished(service, repo, goal="split the scanner out") -> str:
    """One workflow run to `completed`, with a real review and its consultations.

    The whole loop rather than a hand-written row, because what the delete has to
    satisfy is the foreign keys the run actually created: `consultation_turns` and
    `routing_decisions` under the reviewer's consultation, and `review_consultations`
    under the review.
    """
    service.adapters["codex-sol"].answers = [FINDINGS]
    workflow_id = await service.start(goal=goal, workdir=str(repo))
    assert workflow_id.error is None, workflow_id.error
    workflow_id = workflow_id.workflow_id

    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH})
    await host_step(
        service, workflow_id, "test",
        {"command": "pytest -q", "workdir": str(repo), "exit_code": 0, "status": "passed"},
    )
    step_id, token = await step(service, workflow_id, "review")
    assert (await service.run_step(workflow_id, step_id, token)).error is None
    ids = await finding_ids(service, workflow_id)
    final = await host_step(service, workflow_id, "synthesize", summary_with("fixed", ids))
    assert final.status == "completed", final.workflow.reason if final.workflow else None
    return workflow_id


async def count(service, table: str, where: str = "1", *params) -> int:
    rows = await sql(service, f"SELECT COUNT(*) FROM {table} WHERE {where}", *params)
    return rows.fetchone()[0]


# --- one workflow -----------------------------------------------------------


async def test_deleting_a_workflow_takes_every_row_it_owns(build, repo):  # noqa: F811
    """Seven tables, because half a workflow is worse than all of it."""
    service = await build()
    workflow_id = await finished(service, repo)
    assert await count(service, "reviews") == 1
    assert await count(service, "consultation_turns") > 0

    assert await service.delete(workflow_id) == 1

    for table in (
        "workflow_runs", "workflow_steps", "consultations", "consultation_turns",
        "routing_decisions", "reviews", "review_consultations",
    ):
        assert await count(service, table) == 0, table


async def test_a_second_workflow_and_a_standalone_consultation_are_untouched(build, repo):  # noqa: F811
    """The delete is scoped by id, not by table."""
    service = await build()
    kept = await finished(service, repo, goal="keep this one")
    doomed = await finished(service, repo, goal="delete this one")
    plain = await service.consult.consult(capability="research", prompt="unrelated")
    assert plain.error is None, plain.error

    assert await service.delete(doomed) == 1

    assert await count(service, "workflow_runs", "id = ?", kept) == 1
    assert await count(service, "workflow_steps", "workflow_id = ?", kept) > 0
    assert await count(service, "reviews", "workflow_id = ?", kept) == 1
    assert await count(service, "consultations", "id = ?", str(plain.consultation_id)) == 1


async def test_deleting_a_workflow_that_is_not_there_is_not_an_error(build, repo):  # noqa: F811
    """Nothing was removed and nothing was wrong; the count says which."""
    service = await build()
    assert await service.delete("2f0d5e10-0000-0000-0000-000000000000") == 0


# --- what is still moving ---------------------------------------------------


async def test_an_open_workflow_is_refused_and_told_how_to_close_it(build, repo):  # noqa: F811
    service = await build()
    workflow_id = await started(service, repo)

    with pytest.raises(StoreError, match="orchestrator_workflow_cancel") as caught:
        await service.delete(workflow_id)
    assert caught.value.code == ConsultErrorCode.INVALID_REQUEST
    assert await count(service, "workflow_runs") == 1

    assert (await service.cancel(workflow_id)).error is None
    assert await service.delete(workflow_id) == 1


async def test_a_step_holding_a_live_lease_refuses_the_delete(build, repo):  # noqa: F811
    """A lease may belong to an agent process in another server.

    Deleting the rows now would leave that process writing into a workflow that is
    gone, which is why status alone is not enough to decide this.
    """
    service = await build()
    workflow_id = await finished(service, repo)
    step_id = (
        await sql(service, "SELECT id FROM workflow_steps WHERE workflow_id = ? LIMIT 1", workflow_id)
    ).fetchone()[0]
    await sql(
        service,
        "UPDATE workflow_steps SET lease_holder = 'other-process', lease_expires_at = ? "
        "WHERE id = ?",
        time.time() + 60,
        step_id,
    )

    with pytest.raises(StoreError, match="in flight") as caught:
        await service.delete(workflow_id)
    assert caught.value.code == ConsultErrorCode.SESSION_BUSY
    assert await count(service, "workflow_runs") == 1


async def test_an_expired_lease_does_not_wedge_the_workflow_forever(build, repo):  # noqa: F811
    """The holder crashed. The row it left behind is `reap_abandoned`'s to resolve,
    and the delete only compares against it rather than rewriting it."""
    service = await build()
    workflow_id = await finished(service, repo)
    await sql(
        service,
        "UPDATE workflow_steps SET lease_holder = 'pid-dead', lease_expires_at = ? "
        "WHERE workflow_id = ?",
        time.time() - 1,
        workflow_id,
    )

    assert await service.delete(workflow_id) == 1


# --- delete-all -------------------------------------------------------------


async def test_a_workflow_created_after_the_count_survives_the_confirmation(build, repo):  # noqa: F811
    """An approval is for the workflows the user was shown."""
    service = await build()
    old = await finished(service, repo)
    token, shown = await service.request_delete_all()
    new = await finished(service, repo, goal="started since")

    assert shown == 1
    assert await service.delete_all(token) == 1
    assert await count(service, "workflow_runs", "id = ?", old) == 0
    assert await count(service, "workflow_runs", "id = ?", new) == 1


async def test_an_open_workflow_is_counted_and_then_refused(build, repo):  # noqa: F811
    """Not quietly dropped from the snapshot.

    A review leaves `running` on its own, so filtering one out is temporary. A
    workflow sits in `coding` until somebody advances it, so a count that silently
    skipped it would stay wrong until the user thought to cancel.
    """
    service = await build()
    open_workflow = await started(service, repo)
    token, shown = await service.request_delete_all()

    assert shown == 1
    with pytest.raises(StoreError, match="still open"):
        await service.delete_all(token)
    assert await count(service, "workflow_runs", "id = ?", open_workflow) == 1


async def test_a_refused_delete_does_not_burn_the_confirmation(build, repo):  # noqa: F811
    service = await build()
    workflow_id = await started(service, repo)
    token, _ = await service.request_delete_all()

    with pytest.raises(StoreError, match="still open"):
        await service.delete_all(token)
    assert (await service.cancel(workflow_id)).error is None
    assert await service.delete_all(token) == 1


async def test_an_expired_confirmation_is_refused_rather_than_widened(build, repo):  # noqa: F811
    service = await build()
    await finished(service, repo)
    token, _ = await service.store.request_delete_all_workflows(ttl_s=-1)

    with pytest.raises(StoreError, match="expired"):
        await service.delete_all(token)
    assert await count(service, "workflow_runs") == 1


async def test_a_confirmation_is_spent_once(build, repo):  # noqa: F811
    service = await build()
    await finished(service, repo)
    token, _ = await service.request_delete_all()

    assert await service.delete_all(token) == 1
    with pytest.raises(StoreError, match="not outstanding"):
        await service.delete_all(token)


async def test_the_confirmation_row_holds_only_the_hash(build, repo):  # noqa: F811
    service = await build()
    token, _ = await service.request_delete_all()

    rows = (await sql(service, "SELECT token_sha FROM workflow_delete_confirmations")).fetchall()
    assert [row[0] for row in rows] == [_sha256(token)]


# --- what the workflow does not own -----------------------------------------


async def recheck_of(service, review_id: str):
    """A caller's own recheck of a workflow's review.

    It carries `workflow_id = NULL`: `orchestrator_review` has no `workflow_id`
    argument and nothing stamps one on, so this review belongs to whoever asked for
    it and not to the workflow whose review it re-examines.
    """
    response = await service.reviews.plan(
        goal="look at the same code again", context="the fix", parent_review_id=review_id
    )
    assert response.error is None, response.error
    assert (
        await service.reviews.run(response.review_id, response.plan.confirm_token)
    ).error is None
    return str(response.review_id)


async def review_of(service, workflow_id: str) -> str:
    return (
        await sql(service, "SELECT id FROM reviews WHERE workflow_id = ?", workflow_id)
    ).fetchone()[0]


async def test_a_recheck_of_a_workflow_review_outlives_the_workflow(build, repo):  # noqa: F811
    """Deleting a workflow must not take a review the workflow never owned.

    `parent_review_id` is a link, not ownership. Walking it downward would sweep up
    the caller's own recheck and the consultations under it, which no approval ever
    described. `delete_tree`'s `detach_unapproved` is what makes leaving it behind
    legal: the child's parent link is cleared before the parent goes, so the deferred
    foreign key still commits.
    """
    service = await build()
    workflow_id = await finished(service, repo)
    review_id = await review_of(service, workflow_id)
    recheck = await recheck_of(service, review_id)
    kept = [
        row[0]
        for row in (
            await sql(
                service,
                "SELECT consultation_id FROM review_consultations WHERE review_id = ?",
                recheck,
            )
        ).fetchall()
    ]
    assert kept

    assert await service.delete(workflow_id) == 1

    assert await count(service, "reviews", "id = ?", recheck) == 1
    # As a root of its own now: the parent it named is gone.
    assert await count(service, "reviews", "id = ? AND parent_review_id IS NULL", recheck) == 1
    assert await count(service, "reviews", "id = ?", review_id) == 0
    for consultation_id in kept:
        assert await count(service, "consultations", "id = ?", consultation_id) == 1


async def test_a_recheck_made_after_the_count_survives_the_confirmation(build, repo):  # noqa: F811
    """The approval names workflow ids, and the reviews are read from those ids.

    Expanding a review tree at confirmation time would reach rows that did not exist
    when the count was shown, which is exactly what snapshotting ids rather than a
    count is meant to prevent.
    """
    service = await build()
    workflow_id = await finished(service, repo)
    review_id = await review_of(service, workflow_id)
    token, shown = await service.request_delete_all()
    recheck = await recheck_of(service, review_id)

    assert shown == 1
    assert await service.delete_all(token) == 1
    assert await count(service, "reviews", "id = ?", recheck) == 1


# --- the two columns that say who owns a review ------------------------------


async def test_a_step_and_its_review_agree_on_which_workflow_owns_it(build, repo):  # noqa: F811
    """`workflow_steps.review_id` and `reviews.workflow_id` name the same workflow.

    Both delete paths read ownership off `reviews.workflow_id`, while what they are
    protecting is the step pointer -- and that column carries no `REFERENCES` clause,
    so nothing in the schema makes the two agree. They agree by construction instead:
    the only value the pointer ever receives comes from `WorkflowService._plan_review`,
    which creates the review with `workflow_id` already set, and no path attaches an
    existing review to a step. This asserts the construction rather than assuming it,
    so a future write path that stamps one column without the other fails here rather
    than in a delete that quietly takes the wrong rows.
    """
    service = await build()
    workflow_id = await finished(service, repo)

    # Read from the pointer, not from `workflow_id`: a review the step names but that
    # no workflow claims is exactly the row this asserts cannot exist, and looking it
    # up by owner would miss it by construction.
    pointed = (
        await sql(
            service,
            "SELECT s.id, s.review_id, r.workflow_id FROM workflow_steps s "
            "JOIN reviews r ON r.id = s.review_id WHERE s.review_id IS NOT NULL",
        )
    ).fetchall()
    assert pointed, "the run produced no step pointing at a review"
    for step_id, review_id, owner in pointed:
        assert owner == workflow_id, f"step `{step_id}` names review `{review_id}` of `{owner}`"

    recheck = await recheck_of(service, await review_of(service, workflow_id))
    # Nothing points at a review the workflow does not own: a step whose pointer
    # named the recheck would be deleted while the recheck stayed, which is the
    # disagreement this test exists to catch.
    assert await count(service, "workflow_steps", "review_id = ?", recheck) == 0
    dangling = (
        await sql(
            service,
            "SELECT s.id FROM workflow_steps s LEFT JOIN reviews r ON r.id = s.review_id "
            "WHERE s.review_id IS NOT NULL AND r.id IS NULL",
        )
    ).fetchall()
    assert dangling == []


# --- the defect this update names -------------------------------------------


async def test_a_fix_round_is_never_handed_an_empty_review(build, repo):  # noqa: F811
    """The regression the review guard exists to prevent.

    `ReviewService.get` answers a missing review with an error envelope rather than
    raising, so a deleted review would leave `_open_findings` returning `[]` and the
    next fix round answering from the goal instead of from the review. The delete is
    refused, so the round still sees the finding.
    """
    service = await build()
    service.adapters["codex-sol"].answers = [FINDINGS]
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH})
    await host_step(
        service, workflow_id, "test",
        {"command": "pytest -q", "workdir": str(repo), "exit_code": 0, "status": "passed"},
    )
    step_id, token = await step(service, workflow_id, "review")
    assert (await service.run_step(workflow_id, step_id, token)).error is None
    review_id = (
        await sql(service, "SELECT id FROM reviews WHERE workflow_id = ?", workflow_id)
    ).fetchone()[0]

    with pytest.raises(StoreError, match="is a step of workflow"):
        await service.reviews.store.delete_review(review_id)

    # And the review is still there for the round that reads it.
    ids = await finding_ids(service, workflow_id)
    assert ids
