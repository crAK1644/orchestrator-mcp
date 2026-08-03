"""Routing: deterministic, host-excluding, and never falling through."""

from __future__ import annotations

import pytest

from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.contract import SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.routing import ConsultRouter, SourceModeError, resolve_source_mode

from .conftest import agent, consult_block


def router(host: str = "claude", **agents) -> ConsultRouter:
    block = consult_block() if not agents else {"agents": agents}
    return ConsultRouter(ConsultConfig(**block), host)


# --- selection --------------------------------------------------------------


def test_the_host_runtime_is_never_selected():
    """Self-delegation is the failure this whole env var exists to prevent."""
    assert router("claude").select("coding").route.agent_id == "codex-sol"
    assert router("codex").select("coding").route.agent_id == "claude-opus"


def test_the_highest_score_wins():
    decision = router(
        "claude",
        low=agent("codex", "a", scores={"coding": 40}),
        high=agent("codex", "b", scores={"coding": 90}),
    ).select("coding")
    assert decision.route.agent_id == "high"
    assert decision.route.capability_score == 90


def test_a_lower_priority_number_wins_a_score_tie():
    decision = router(
        "claude",
        second=agent("codex", "a", priority=20, scores={"coding": 90}),
        first=agent("codex", "b", priority=5, scores={"coding": 90}),
    ).select("coding")
    assert decision.route.agent_id == "first"


def test_the_agent_id_breaks_a_full_tie():
    """Two identically configured agents must still resolve the same way twice."""
    decision = router(
        "claude",
        zebra=agent("codex", "a", priority=10, scores={"coding": 90}),
        alpha=agent("codex", "b", priority=10, scores={"coding": 90}),
    ).select("coding")
    assert decision.route.agent_id == "alpha"


def test_scoring_zero_makes_an_agent_ineligible():
    decision = router(
        "claude",
        only=agent("codex", "a", scores={"coding": 0, "research": 80}),
    ).select("coding")
    assert decision.selected is None
    assert decision.error[0] is ConsultErrorCode.NO_AGENT_AVAILABLE
    assert any(e.agent_id == "only" and "scores 0" in e.reason for e in decision.excluded)


def test_no_eligible_agent_is_an_error_not_a_second_choice():
    decision = router("claude", solo=agent("claude", "opus")).select("coding")
    assert decision.route is None
    assert decision.error[0] is ConsultErrorCode.NO_AGENT_AVAILABLE
    assert "claude" in decision.error[1]


def test_a_disabled_agent_is_excluded_with_a_reason():
    decision = router("claude", off=agent("codex", "a", enabled=False)).select("coding")
    assert [(e.agent_id, e.reason) for e in decision.excluded] == [("off", "disabled")]


def test_the_losers_are_recorded_with_why():
    decision = router(
        "claude",
        winner=agent("codex", "a", scores={"coding": 90}),
        muted=agent("codex", "b", scores={"coding": 0}),
        myself=agent("claude", "opus"),
    ).select("coding")
    assert decision.route.agent_id == "winner"
    assert dict((e.agent_id, e.reason) for e in decision.excluded).keys() == {"muted", "myself"}


# --- explicit target --------------------------------------------------------


def test_an_explicit_target_overrides_the_scores():
    decision = router(
        "claude",
        preferred=agent("codex", "a", scores={"coding": 10}),
        stronger=agent("codex", "b", scores={"coding": 99}),
    ).select("coding", target_agent="preferred")
    assert decision.route.agent_id == "preferred"
    assert decision.route.explicitly_selected is True


def test_an_unconfigured_target_is_a_request_error():
    decision = router("claude").select("coding", target_agent="gpt-9")
    assert decision.error[0] is ConsultErrorCode.INVALID_REQUEST
    assert decision.selected is None


def test_an_explicit_target_cannot_be_the_host_runtime():
    decision = router("claude").select("coding", target_agent="claude-opus")
    assert decision.error[0] is ConsultErrorCode.NO_AGENT_AVAILABLE
    assert "host runtime" in decision.error[1]


def test_an_explicit_target_still_needs_a_score():
    decision = router(
        "claude", pick=agent("codex", "a", scores={"coding": 0})
    ).select("coding", target_agent="pick")
    assert decision.error[0] is ConsultErrorCode.NO_AGENT_AVAILABLE


def test_scored_selection_is_not_marked_explicit():
    assert router("claude").select("coding").route.explicitly_selected is False


# --- source mode ------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, context, expected",
    [
        (SourceMode.AUTO, "some doc", SourceMode.DOCUMENT),
        (SourceMode.AUTO, None, SourceMode.MODEL),
        (SourceMode.AUTO, "   ", SourceMode.MODEL),
        (SourceMode.DOCUMENT, "some doc", SourceMode.DOCUMENT),
        (SourceMode.WEB, None, SourceMode.WEB),
        (SourceMode.WEB, "seed", SourceMode.WEB),
        (SourceMode.MODEL, "ignored", SourceMode.MODEL),
    ],
)
def test_source_mode_resolution(mode, context, expected):
    assert resolve_source_mode(mode, context) is expected


@pytest.mark.parametrize("context", [None, "", "  \n "])
def test_document_mode_without_context_is_refused(context):
    with pytest.raises(SourceModeError):
        resolve_source_mode(SourceMode.DOCUMENT, context)
