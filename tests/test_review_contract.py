"""The review contract: envelope invariants, size caps, and the two checks that
turn a promise in a docstring into something enforced.

The promises being tested are the ones a reader would otherwise have to trust:
that a lone Critical cannot be dropped during synthesis, that the findings cap
cannot be what drops one, and that ids come from this server rather than from a
model that might reuse one.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from orchestrator_mcp.review.contract import (
    MAX_FINDINGS,
    MAX_LIST_ITEMS,
    REVIEWER_INSTRUCTIONS,
    CombinedFinding,
    DeleteApproval,
    Finding,
    MaterialItem,
    RawReviewMaterial,
    ReviewerResult,
    ReviewPlan,
    ReviewResponse,
    ReviewSummary,
    SecretHit,
    missing_criticals,
)
from orchestrator_mcp.review.service import _parse_findings


def finding(agent="a", severity="critical", n=1) -> Finding:
    return Finding(finding_id=f"{agent}-{n}", agent_id=agent, severity=severity, why="w")


def summary(**overrides) -> ReviewSummary:
    return ReviewSummary(**{"summary": "s", "recommendation": "r", **overrides})


def response(**overrides) -> ReviewResponse:
    return ReviewResponse(**{"review_id": uuid4(), "mode": "standard", **overrides})


# --- envelope invariants ----------------------------------------------------


def test_a_pending_review_carries_no_results():
    """Nothing has been sent yet, so a result would be something this server wrote."""
    with pytest.raises(AssertionError):
        response(status="pending", results=[ReviewerResult(agent_id="a", ok=True)]).check_invariants()


def test_a_pending_review_carries_no_summary():
    with pytest.raises(AssertionError):
        response(status="pending", summary=summary()).check_invariants()


def test_a_failed_review_carries_no_findings():
    with pytest.raises(AssertionError):
        response(
            status="failed",
            results=[ReviewerResult(agent_id="a", ok=False, findings=[finding()])],
        ).check_invariants()


def test_a_complete_review_carries_the_synthesis_that_completed_it():
    with pytest.raises(AssertionError):
        response(status="complete").check_invariants()
    response(status="complete", summary=summary()).check_invariants()


def test_a_refusal_on_a_complete_review_reports_where_it_actually_is():
    """A second `finalize_review` is an error envelope stamped `complete`. Reporting
    `failed` instead would tell the caller to retry something already finished."""
    from orchestrator_mcp.consult.contract import ConsultError

    response(
        status="complete",
        error=ConsultError(code="invalid_request", message="already finalized"),
    ).check_invariants()


def test_a_plan_only_ever_describes_what_has_not_been_sent():
    plan = ReviewPlan(
        mode="standard",
        reviewers=[],
        material=[],
        goal_chars=1,
        context_chars=0,
        material_sha256="0" * 64,
        web_requested=False,
        expected_requests=0,
        confirm_token="t",
    )
    with pytest.raises(AssertionError):
        response(status="running", plan=plan).check_invariants()


# --- bounds -----------------------------------------------------------------


def test_a_sha_field_refuses_anything_that_is_not_one():
    for bad in ("", "nope", "0" * 63, "g" * 64, "0" * 65):
        with pytest.raises(ValidationError):
            ReviewPlan(
                mode="standard", reviewers=[], material=[], goal_chars=0, context_chars=0,
                material_sha256=bad, web_requested=False, expected_requests=0, confirm_token="t",
            )


def test_every_shown_field_is_actually_bounded():
    """Each of these is copied into a prompt, JSON-encoded and stored, so an
    uncapped one is several times its own size on disk before anything refuses it."""
    with pytest.raises(ValidationError):
        Finding(finding_id="a-1", agent_id="a", severity="minor", why="x" * 5_001)
    with pytest.raises(ValidationError):
        ReviewerResult(agent_id="a", ok=True, assumptions=["x"] * (MAX_LIST_ITEMS + 1))
    with pytest.raises(ValidationError):
        ReviewerResult(agent_id="a", ok=True, findings=[finding()] * (MAX_FINDINGS + 1))
    with pytest.raises(ValidationError):
        MaterialItem(label="", kind="file")
    with pytest.raises(ValidationError):
        SecretHit(field="goal", line=0)
    with pytest.raises(ValidationError):
        SecretHit(field="answer", line=1)
    with pytest.raises(ValidationError):
        RawReviewMaterial(goal="")
    with pytest.raises(ValidationError):
        summary(summary="x" * 50_001)
    with pytest.raises(ValidationError):
        DeleteApproval(reviews=1, confirm_token="", expires_in_s=1)


def test_the_models_refuse_fields_nobody_declared():
    with pytest.raises(ValidationError):
        Finding(finding_id="a-1", agent_id="a", severity="minor", note="extra")


def test_a_reviewer_cannot_be_asked_to_claim_agreement():
    """None of them can see the others' answers, so agreement is the host's to
    compute and there is nowhere for a reviewer to assert it."""
    assert "agreed_by" not in ReviewerResult.model_fields
    assert "agreed_by" in CombinedFinding.model_fields
    assert "not seen" in REVIEWER_INSTRUCTIONS or "have not seen" in REVIEWER_INSTRUCTIONS


# --- the minority-Critical rule ---------------------------------------------


def test_a_summary_that_drops_a_lone_critical_is_named_as_missing_it():
    results = [
        ReviewerResult(agent_id="a", ok=True, findings=[finding("a", "critical")]),
        ReviewerResult(agent_id="b", ok=True, findings=[finding("b", "minor")]),
    ]
    assert missing_criticals(results, summary()) == ["a-1"]


def test_disagreeing_with_a_critical_keeps_it_and_dropping_it_does_not():
    """The point of a second reviewer is that one dissenting Critical survives. A
    synthesis may conclude it is wrong; it may not make it disappear."""
    results = [ReviewerResult(agent_id="a", ok=True, findings=[finding("a", "critical")])]
    kept = summary(
        combined_findings=[
            CombinedFinding(
                problem="p", severity="critical", agreed_by=["a"], disagreed_by=["b"],
                source_finding_ids=["a-1"],
            )
        ]
    )
    assert missing_criticals(results, kept) == []


def test_lesser_severities_are_not_held_to_the_same_rule():
    results = [ReviewerResult(agent_id="a", ok=True, findings=[finding("a", "important")])]
    assert missing_criticals(results, summary()) == []


def test_a_summary_cannot_claim_provenance_from_a_finding_nobody_raised():
    results = [ReviewerResult(agent_id="a", ok=True, findings=[finding("a", "minor")])]
    invented = summary(
        combined_findings=[
            CombinedFinding(
                problem="p",
                severity="minor",
                agreed_by=["a"],
                source_finding_ids=["ghost-1"],
            )
        ]
    )
    assert missing_criticals(results, invented) == ["ghost-1"]


# --- parsing a reviewer's answer --------------------------------------------


def block(findings) -> str:
    return "prose review\n\n```json\n" + json.dumps({"findings": findings}) + "\n```"


def test_findings_ride_inside_the_answer_and_get_server_assigned_ids():
    findings, parsed, dropped = _parse_findings(
        "rev", block([{"location": "a.py:1", "severity": "critical", "why": "w",
                       "example": "e", "fix": "f"}])
    )
    assert parsed and dropped == 0
    assert [f.finding_id for f in findings] == ["rev-1"]
    assert findings[0].agent_id == "rev"


def test_an_id_a_model_invented_is_ignored():
    """Two reviewers both numbering from 1 would collide, and the summary check
    resolves references by id."""
    findings, _, _ = _parse_findings(
        "rev", block([{"finding_id": "MINE", "severity": "minor", "why": "w"}])
    )
    assert findings[0].finding_id == "rev-1"


def test_an_unknown_severity_becomes_uncertain_rather_than_a_refusal():
    findings, _, _ = _parse_findings("rev", block([{"severity": "BLOCKER", "why": "w"}]))
    assert findings[0].severity == "uncertain"
    findings, _, _ = _parse_findings("rev", block([{"severity": "Critical", "why": "w"}]))
    assert findings[0].severity == "critical"


def test_malformed_json_keeps_the_prose_and_says_it_did_not_parse():
    findings, parsed, _ = _parse_findings("rev", "a real review\n```json\n{not json\n```")
    assert findings == [] and parsed is False


def test_a_json_recursion_error_is_an_unparsed_answer_not_an_exception(monkeypatch):
    def too_deep(_):
        raise RecursionError

    monkeypatch.setattr("orchestrator_mcp.review.service.json.loads", too_deep)
    findings, parsed, dropped = _parse_findings("rev", '{"findings": []}')
    assert (findings, parsed, dropped) == ([], False, 0)


def test_an_unfenced_block_is_still_found():
    answer = 'the review\n{"findings": [{"severity": "minor", "why": "w"}]}'
    findings, parsed, _ = _parse_findings("rev", answer)
    assert parsed and len(findings) == 1


def test_an_empty_findings_list_is_a_real_answer():
    findings, parsed, _ = _parse_findings("rev", block([]))
    assert findings == [] and parsed is True


def test_the_findings_cap_drops_a_minor_before_a_critical():
    """Truncation is severity-ordered, so the cap can never be what loses a
    Critical -- and how many were dropped is reported rather than left silent."""
    raw = [{"severity": "minor", "why": f"m{n}"} for n in range(MAX_FINDINGS + 5)]
    raw.append({"severity": "critical", "why": "the one that matters"})

    findings, parsed, dropped = _parse_findings("rev", block(raw))

    assert parsed and dropped == 6 and len(findings) == MAX_FINDINGS
    assert findings[0].severity == "critical"
    assert "matters" in findings[0].why


def test_the_reviewer_instructions_pin_the_shape_they_ask_for():
    """A golden check: the adapters hardcode one output schema, so this text is the
    only thing telling a reviewer how to emit findings. Changing it changes what
    every reviewer returns."""
    for required in ('```json', '"findings"', "critical", "important", "minor", "uncertain"):
        assert required in REVIEWER_INSTRUCTIONS
    assert "Do not assign ids" in REVIEWER_INSTRUCTIONS
