"""Recording what was done about the findings.

The whole of `apply_fixes` is bookkeeping, and that is the thing worth proving:
the server hands back the findings and the steps, takes the host's account of
what happened, and touches no file either way. What it does enforce is that a
round points at findings some reviewer actually raised, that a selection quietly
missing a Critical is named, and that a redaction failure here is no different
from one anywhere else in the review path.
"""

from __future__ import annotations

from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.review.contract import MAX_FIX_ROUNDS

from .test_review_service import (  # noqa: F401
    FINDINGS,
    REVIEWERS,
    StubAdapter,
    build,
    planned,
)

SECRET = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"

TWO_FINDINGS = (
    'Two things.\n\n```json\n{"findings": ['
    '{"location": "a.py:1", "severity": "critical", "why": "unbounded read", '
    '"example": "a 2GB file", "fix": "stream it"}, '
    '{"location": "a.py:9", "severity": "minor", "why": "stale comment", '
    '"example": "line 9", "fix": "delete it"}]}\n```'
)


async def reviewed(service, **overrides):
    """A review with findings on it, sitting at `awaiting_synthesis`."""
    plan = await planned(service, **overrides)
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    assert run.status == "awaiting_synthesis", run.error
    return run


def critical_id(run) -> str:
    return next(f.finding_id for r in run.results for f in r.findings if f.severity == "critical")


# --- the plan side ----------------------------------------------------------


async def test_the_fix_plan_returns_the_selected_findings_and_the_steps(build):
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    run = await reviewed(service)
    before = {aid: len(a.prompts) for aid, a in adapters.items()}

    response = await service.fix_plan(run.review_id, [critical_id(run)])

    assert response.error is None, response.error
    assert [f.finding_id for f in response.fix_plan.findings] == [critical_id(run)]
    assert response.fix_plan.findings[0].fix == "stream it"
    assert any("safety point" in step for step in response.fix_plan.steps)
    # Nothing was sent anywhere: planning a fix is reading rows back.
    assert {aid: len(a.prompts) for aid, a in adapters.items()} == before


async def test_a_finding_id_no_reviewer_raised_is_refused(build):
    """A round pointing at nobody's finding is worse than no record at all."""
    service = await build()
    run = await reviewed(service)

    response = await service.fix_plan(run.review_id, ["codex-sol-99"])

    assert response.error.code == ConsultErrorCode.INVALID_REQUEST
    assert "codex-sol-99" in response.error.message


async def test_a_selection_that_leaves_out_a_critical_says_so(build):
    """The same failure the synthesis check exists to prevent, one stage later."""
    service = await build({aid: StubAdapter(TWO_FINDINGS) for aid in REVIEWERS})
    run = await reviewed(service)
    minor = next(f.finding_id for r in run.results for f in r.findings if f.severity == "minor")

    response = await service.fix_plan(run.review_id, [minor])

    assert response.fix_plan.criticals_omitted == [critical_id(run)]


async def test_the_fix_plan_of_a_finished_review_still_carries_its_synthesis(build):
    """Fixing normally happens after the synthesis, so `complete` is the common case
    here -- and a `complete` envelope without the summary is one the invariants
    refuse."""
    from .test_review_service import _synthesis

    service = await build()
    run = await reviewed(service)
    await service.finalize(run.review_id, _synthesis(run))

    response = await service.fix_plan(run.review_id, [critical_id(run)])

    assert response.error is None, response.error
    assert response.status == "complete" and response.summary is not None
    assert [f.finding_id for f in response.fix_plan.findings] == [critical_id(run)]


async def test_fixing_a_review_that_has_not_run_is_refused(build):
    service = await build()
    plan = await planned(service)

    response = await service.fix_plan(plan.review_id, [])

    assert response.error.code == ConsultErrorCode.INVALID_REQUEST
    assert "has findings" in response.error.message


# --- recording --------------------------------------------------------------


async def test_a_recorded_round_comes_back_with_the_review(build):
    service = await build()
    run = await reviewed(service)

    await service.record_fix_round(
        run.review_id, [critical_id(run)], "applied", notes="streamed the read"
    )
    response = await service.get(run.review_id)

    assert len(response.fix_rounds) == 1
    round_ = response.fix_rounds[0]
    assert round_.outcome == "applied" and round_.notes == "streamed the read"
    assert round_.finding_ids == [critical_id(run)] and round_.recorded_at


async def test_rounds_accumulate_in_the_order_they_were_recorded(build):
    """A revert after an apply is the history worth keeping: a later round must not
    read as though the first one never happened."""
    service = await build()
    run = await reviewed(service)

    await service.record_fix_round(run.review_id, [critical_id(run)], "applied")
    response = await service.record_fix_round(run.review_id, [critical_id(run)], "reverted")

    assert [r.outcome for r in response.fix_rounds] == ["applied", "reverted"]


async def test_a_round_can_be_recorded_after_the_review_is_complete(build):
    """Fixing happens after the synthesis, which is exactly when a review is
    `complete`. Refusing there would refuse the normal case."""
    from .test_review_service import _synthesis

    service = await build()
    run = await reviewed(service)
    done = await service.finalize(run.review_id, _synthesis(run))
    assert done.status == "complete"

    response = await service.record_fix_round(run.review_id, [critical_id(run)], "applied")

    assert response.error is None and len(response.fix_rounds) == 1


async def test_an_unknown_outcome_is_refused(build):
    service = await build()
    run = await reviewed(service)

    response = await service.record_fix_round(run.review_id, [], "mostly-fixed")

    assert response.error.code == ConsultErrorCode.INVALID_REQUEST


async def test_the_round_cap_points_at_a_recheck_instead(build):
    service = await build()
    run = await reviewed(service)
    for _ in range(MAX_FIX_ROUNDS):
        assert (await service.record_fix_round(run.review_id, [], "applied")).error is None

    response = await service.record_fix_round(run.review_id, [], "applied")

    assert response.error.code == ConsultErrorCode.INVALID_REQUEST
    assert "parent_review_id" in response.error.message


# --- the promises that hold everywhere else ---------------------------------


async def test_a_secret_in_the_notes_is_redacted_before_it_is_stored(build, tmp_path):
    """Notes are host-written prose reaching the same database as everything else.
    A field added later is exactly how a redaction rule stops being true."""
    import sqlite3

    service = await build()
    run = await reviewed(service)

    await service.record_fix_round(
        run.review_id, [critical_id(run)], "applied", notes=f"rotated {SECRET}"
    )
    await service.close()

    database = sqlite3.connect(tmp_path / "c.sqlite3")
    rows = database.execute("SELECT fix_rounds_json FROM reviews").fetchall()
    database.close()
    assert rows and "[redacted]" in rows[0][0]
    assert SECRET not in rows[0][0]


async def test_not_keeping_content_keeps_the_shape_and_drops_the_notes(build):
    """`store_full_content: false` means prose does not go on disk. The ids and the
    outcome are shape, and losing those would make the log unreadable rather than
    private."""
    service = await build(store_full_content=False)
    run = await reviewed(service)

    response = await service.record_fix_round(
        run.review_id, [], "applied", notes="a paragraph about the repository"
    )

    assert response.fix_rounds[0].outcome == "applied"
    assert response.fix_rounds[0].notes == ""


async def test_a_recheck_shows_up_on_the_review_it_rechecks(build):
    """The link lives on the child's `parent_review_id`, so the chain is read rather
    than recorded twice in two places that could disagree."""
    service = await build()
    run = await reviewed(service)

    child = await planned(service, parent_review_id=run.review_id, context="the diff")
    response = await service.get(run.review_id)

    assert response.rechecks == [str(child.review_id)]


async def test_deleting_a_review_takes_its_rechecks_with_it(build):
    service = await build()
    run = await reviewed(service)
    await planned(service, parent_review_id=run.review_id, context="the diff")

    assert await service.delete(run.review_id) == 2
    assert (await service.list()) == []
