"""The cost estimate shown beside a plan.

It decides nothing, so what is worth proving is that it never shows a price it
cannot back: no history is no estimate, an unpriced agent is tokens without money,
and one unpriced reviewer makes the total `None` rather than a floor.
"""

from __future__ import annotations

from orchestrator_mcp.estimate import TOKENS_PER_CHAR, ceiling_warning, estimate, total

from .test_review_service import REVIEWERS, StubAdapter, build, planned  # noqa: F401


def test_no_history_is_no_estimate():
    assert estimate("a", 1000, []) is None


def test_a_seeded_history_gives_the_expected_numbers():
    # A fixed 1000 tokens plus 0.25 a char; $0.01 plus $0.00001 a char.
    history = [(1000, 1250, 20, 0.02), (2000, 1500, 30, 0.03), (3000, 1750, 40, 0.04)]

    got = estimate("a", 5000, history)

    assert (got.input_tokens, got.output_tokens, got.basis_turns) == (2250, 30, 3)
    assert got.cost_usd == 0.06


def test_a_fixed_overhead_is_not_scaled_away():
    """A ratio would price a 100-char prompt at a fraction of the CLI's own prompt."""
    history = [(10_000, 16_500, 1, None), (100_000, 39_000, 1, None)]

    assert estimate("a", 100, history).input_tokens == 14_025


def test_an_unpriced_agent_gives_tokens_and_no_money():
    got = estimate("a", 2000, [(400, 100, 20, None)] * 5)

    assert got.input_tokens == 100 and got.cost_usd is None


def test_too_few_priced_turns_give_no_money():
    assert estimate("a", 2000, [(400, 100, 20, 0.01)] * 2).cost_usd is None


def test_no_stored_prompts_falls_back_to_the_ratio():
    """`store_full_content: false` keeps no prompt to calibrate against."""
    got = estimate("a", 2000, [(None, 100, 20, None)] * 3)

    assert got.input_tokens == round(2000 * TOKENS_PER_CHAR)


def test_one_unpriced_part_makes_the_total_none():
    priced = estimate("a", 10, [(10, 10, 1, 0.01)] * 3)
    unpriced = estimate("b", 10, [(10, 10, 1, None)] * 3)

    assert total([priced, priced]) == round(2 * priced.cost_usd, 4)
    assert total([priced, unpriced]) is None
    assert total([priced, None]) is None


def test_the_warning_is_only_at_or_past_the_ceiling():
    assert ceiling_warning(0.5, 0.4, 1.0, "x") is None
    assert "ceiling" in ceiling_warning(0.5, 0.5, 1.0, "x")
    assert ceiling_warning(0.5, None, 1.0, "x") is None
    assert ceiling_warning(0.5, 9.0, None, "x") is None


# --- in a review plan -------------------------------------------------------


async def _history(service, turns: int = 3):
    for _ in range(turns):
        plan = await planned(service)
        assert (await service.run(plan.review_id, plan.plan.confirm_token)).error is None


async def test_a_review_plan_carries_the_estimate_from_past_turns(build):
    service = await build({aid: StubAdapter(cost_usd=0.012) for aid in REVIEWERS})
    first = await planned(service)
    assert first.plan.estimates == [] and first.plan.estimated_cost_usd is None

    await _history(service)
    plan = (await planned(service)).plan

    [only] = plan.estimates
    assert only.agent_id == "codex-sol" and only.basis_turns == 3
    assert only.output_tokens == 2 and only.cost_usd is not None
    assert plan.estimated_cost_usd == only.cost_usd


async def test_an_unpriced_reviewer_leaves_the_plan_total_empty(build):
    service = await build()
    await _history(service)

    plan = (await planned(service)).plan

    assert plan.estimates[0].basis_turns == 3 and plan.estimates[0].cost_usd is None
    assert plan.estimated_cost_usd is None


async def test_an_estimate_over_the_review_ceiling_is_flagged(build):
    service = await build(
        {aid: StubAdapter(cost_usd=0.012) for aid in REVIEWERS},
        spend={"max_cost_usd_per_review": 0.000001},
    )
    await _history(service)

    plan = (await planned(service)).plan

    assert plan.ceiling_warning and "ceiling" in plan.ceiling_warning
