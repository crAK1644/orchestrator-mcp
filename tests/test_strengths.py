"""`strengths`: how each agent has answered each kind of question, and what that can say.

Every database here is written through the real store, so a column is whatever the store
writes. Three things carry the weight: a rate is never printed from a handful of asks,
advice names only an agent the config holds now and never touches the config, and the
report says out loud what it cannot tell -- whose answers were better.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from orchestrator_mcp.consult.config import HOST_RUNTIME_ENV
from orchestrator_mcp.consult.contract import ConsultRoute, SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.store import ConsultStore
from orchestrator_mcp.reports import (
    MIN_ASKED,
    NOT_MIGRATED,
    _top_code,
    render_strengths,
    scorecard,
    strengths,
)

from .conftest import agent
from .test_consult_dashboard import config  # noqa: F401 -- fixture
from .test_reports import (
    answer_with,
    cli,  # noqa: F401 -- fixture
    decided,
    digest,
    report,
    reviewer,
    write,
)
from .test_review_dashboard import (
    SECRET,
    make_review,
    old_database,
    review_config,  # noqa: F401 -- fixture
)

TIMEOUT = ConsultErrorCode.TIMEOUT


def answered(latency: int, count: int = MIN_ASKED) -> list[tuple[int, ConsultErrorCode | None]]:
    return [(latency, None)] * count


def failed(
    code: ConsultErrorCode, count: int = MIN_ASKED
) -> list[tuple[int, ConsultErrorCode | None]]:
    # A failed turn's clock is a timeout or a crash, so it is a long one on purpose: the
    # average must leave it out.
    return [(90_000, code)] * count


async def seed(consult_config, agent_id, capability, outcomes, *, model=None, runtime=None):
    """One real consultation per outcome, each with one turn: `(latency_ms, error code)`,
    plus a third item, its `cost_usd`, for a turn that reported a price.

    Straight through the store, so every column is what the store writes. An agent the
    config no longer holds takes its `runtime` and `model` from the caller.
    """
    known = consult_config.agents.get(agent_id)
    route = ConsultRoute(
        agent_id=agent_id,
        runtime=runtime or known.runtime,
        model=model or known.model,
        capability_score=90,
        priority=10,
        explicitly_selected=False,
    )
    store = await ConsultStore(consult_config.database_path).open()
    try:
        for latency, code, *price in outcomes:
            consultation_id = uuid4()
            await store.create_consultation(
                consultation_id=consultation_id,
                origin_runtime="claude",
                route=route,
                capability=capability,
                protocol_version="consult-v1",
                config_hash="abc123",
            )
            await store.record_turn(
                consultation_id,
                1,
                SourceMode.MODEL,
                "q",
                None,
                "compiled",
                latency_ms=latency,
                error_code=code,
                cost_usd=price[0] if price else None,
            )
    finally:
        await store.close()


def advice_for(consult_config, host="", **kwargs):
    return report(consult_config, strengths, 30, consult_config, host, **kwargs)["advice"]


# --- the table ----------------------------------------------------------------


async def test_a_row_per_kind_agent_and_model_counts_asks_answers_and_errors(config):
    consult_config = config()
    await seed(
        consult_config, "codex-sol", "coding", answered(1000, 2) + answered(3000, 2) + failed(TIMEOUT, 2)
    )
    await seed(consult_config, "codex-sol", "research", answered(500, 1))
    await seed(consult_config, "claude-opus", "coding", answered(200, 1))

    result = report(consult_config, strengths)

    assert [(g["capability"], g["agent_id"], g["model"]) for g in result["groups"]] == [
        ("coding", "claude-opus", "opus"),
        ("coding", "codex-sol", "gpt-5.6-sol"),
        ("research", "codex-sol", "gpt-5.6-sol"),
    ]
    busy = result["groups"][1]
    assert (busy["asked"], busy["answered"], busy["errored"]) == (6, 4, 2)
    assert busy["error_rate"] == 2 / 6 and busy["top_error"] == "timeout"
    # Over the four answers: the two timeouts' minute and a half is not how long an answer takes.
    assert busy["avg_latency_ms"] == 2000
    assert busy["decided"] is None and busy["precision"] is None
    text = render_strengths(result)
    assert "33%" in text and "2000 ms" in text and "Last 30 days" in text


async def test_cost_is_a_price_only_when_every_turn_reported_one(config):
    consult_config = config()
    store = await ConsultStore(consult_config.database_path).open()
    try:
        for agent_id, cost in (("codex-sol", 0.0125), ("claude-opus", None)):
            known = consult_config.agents[agent_id]
            route = ConsultRoute(
                agent_id=agent_id,
                runtime=known.runtime,
                model=known.model,
                capability_score=90,
                priority=10,
                explicitly_selected=False,
            )
            consultation_id = uuid4()
            await store.create_consultation(
                consultation_id=consultation_id,
                origin_runtime="claude",
                route=route,
                capability="coding",
                protocol_version="consult-v1",
                config_hash="abc123",
            )
            await store.record_turn(
                consultation_id, 1, SourceMode.MODEL, "q", None, "compiled", cost_usd=cost
            )
    finally:
        await store.close()

    text = render_strengths(report(consult_config, strengths))

    assert "$0.0125" in text and "unknown" in text and "$0.0000" not in text


async def test_a_group_with_one_unpriced_turn_shows_no_total(config):
    consult_config = config()
    # One turn reported a price and one did not, in the same kind, agent and model.
    await seed(consult_config, "codex-sol", "coding", [(100, None, 0.0125), (100, None)])

    result = report(consult_config, strengths)

    (group,) = result["groups"]
    assert group["asked"] == 2 and group["cost_usd"] is None
    assert group["known_cost_usd"] == 0.0125
    assert "unknown (>= $0.0125)" in render_strengths(result)


async def test_a_database_with_no_turns_says_so(config):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", [])

    result = report(consult_config, strengths)

    assert result["groups"] == [] and render_strengths(result) == "No turns in the last 30 days."


async def test_only_the_window_is_counted(config):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", answered(1000, 5))
    write(consult_config, "UPDATE consultation_turns SET created_at = ?", "2020-01-01T00:00:00+00:00")
    await seed(consult_config, "codex-sol", "coding", answered(1000, 2))

    assert report(consult_config, strengths, 30)["groups"][0]["asked"] == 2
    assert report(consult_config, strengths, 3650)["groups"][0]["asked"] == 7


async def test_turns_nobody_sent_are_not_asks(config):
    consult_config = config()
    await seed(
        consult_config,
        "codex-sol",
        "coding",
        answered(1000)
        + failed(ConsultErrorCode.NOT_STARTED, 4)
        + failed(ConsultErrorCode.SPEND_LIMIT_REACHED, 4),
    )
    await seed(consult_config, "claude-opus", "coding", failed(ConsultErrorCode.NOT_STARTED))

    result = report(consult_config, strengths)

    # An agent that was never tried has no row at all, and one that was is not failing.
    (row,) = result["groups"]
    assert (row["agent_id"], row["asked"], row["errored"], row["error_rate"]) == (
        "codex-sol",
        5,
        0,
        0.0,
    )


# --- a rate needs asks ----------------------------------------------------------


async def test_fewer_than_five_asks_print_no_rate_and_earn_no_advice(config):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", failed(TIMEOUT, MIN_ASKED - 1))

    result = report(consult_config, strengths, 30, consult_config)

    assert result["groups"][0]["error_rate"] is None and result["advice"] == []
    assert "n<5" in render_strengths(result)

    await seed(consult_config, "codex-sol", "coding", failed(TIMEOUT, 1))
    result = report(consult_config, strengths, 30, consult_config)

    assert result["groups"][0]["error_rate"] == 1.0 and len(result["advice"]) == 1


@pytest.mark.parametrize(
    ("asks", "errors", "advised"),
    [(10, 3, True), (10, 2, False), (5, 2, True), (5, 1, False)],
)
async def test_failure_advice_starts_at_thirty_percent_of_five_or_more_asks(
    config, asks, errors, advised
):
    consult_config = config()
    await seed(
        consult_config,
        "codex-sol",
        "coding",
        answered(1000, asks - errors) + failed(TIMEOUT, errors),
    )

    advice = advice_for(consult_config)

    assert bool(advice) is advised
    if advised:
        assert (
            f"codex-sol (gpt-5.6-sol) failed {errors} of {asks} asks for coding, "
            "most often `timeout`." in advice[0]
        )


async def test_a_setup_code_is_called_setup_and_points_at_doctor(config):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", failed(ConsultErrorCode.CONNECTION_REQUIRED))
    await seed(consult_config, "claude-opus", "coding", failed(TIMEOUT))

    advice = advice_for(consult_config)

    setup = next(line for line in advice if line.startswith("codex-sol"))
    slow = next(line for line in advice if line.startswith("claude-opus"))
    assert "setup, not a weakness" in setup and "orchestrator-mcp-server doctor" in setup
    assert "doctor" not in slow


def test_the_commonest_error_is_named_and_a_tie_goes_to_the_first_alphabetically():
    assert _top_code("timeout\x1ftransport_error\x1ftransport_error") == "transport_error"
    assert _top_code("transport_error\x1ftimeout") == "timeout"
    assert _top_code("") is None and _top_code(None) is None


# --- speed advice ---------------------------------------------------------------


async def test_advice_names_the_much_quicker_agent_at_no_higher_error_rate(config):
    consult_config = config()  # codex-sol has the lower priority number, so it gets `coding`
    await seed(consult_config, "codex-sol", "coding", answered(4000))
    await seed(consult_config, "claude-opus", "coding", answered(2000))

    (line,) = advice_for(consult_config)

    assert line.startswith("For coding, claude-opus (opus) averaged 2,000 ms over 5 answers")
    assert "codex-sol (gpt-5.6-sol), where the config routes it, 4,000 ms over 5" in line
    assert "claude-opus failed no more often" in line


@pytest.mark.parametrize(
    ("routed", "other", "advised"),
    [
        ((4000, 5, 0), (2000, 5, 0), True),  # exactly twice as quick
        ((4000, 5, 0), (2001, 5, 0), False),  # a hair short of it
        ((4000, 5, 0), (2000, 4, 0), False),  # too few answers to know
        ((4000, 4, 0), (2000, 5, 0), False),  # the routed one is too thin to compare with
        ((4000, 5, 0), (2000, 5, 1), False),  # quicker, but it fails more often
        ((4000, 5, 1), (2000, 5, 1), True),  # the same error rate is no higher
    ],
)
async def test_speed_advice_thresholds(config, routed, other, advised):
    consult_config = config()
    for agent_id, (latency, answers, errors) in (("codex-sol", routed), ("claude-opus", other)):
        await seed(
            consult_config,
            agent_id,
            "coding",
            answered(latency, answers) + failed(TIMEOUT, errors),
        )

    assert bool(advice_for(consult_config)) is advised


@pytest.mark.parametrize(
    ("routed", "other", "advised"),
    [
        # 3999.6 ms against 2000: under twice, though the table prints 3999.6 as 4000.
        ([3999, 3999, 4000, 4000, 4000], [2000] * 5, False),
        # 4000.2 against 2000.2: twice is 4000.4, though both print as a clean 2 to 1.
        ([4000] * 4 + [4001], [2000] * 4 + [2001], False),
        # 4000.4 against 2000.2: exactly twice, with nothing whole about either.
        ([4000, 4000, 4000, 4001, 4001], [2000] * 4 + [2001], True),
    ],
)
async def test_speed_advice_compares_the_averages_and_not_the_milliseconds_they_print_as(
    config, routed, other, advised
):
    consult_config = config()
    for agent_id, latencies in (("codex-sol", routed), ("claude-opus", other)):
        await seed(consult_config, agent_id, "coding", [(ms, None) for ms in latencies])

    assert bool(advice_for(consult_config)) is advised


async def test_the_quickest_is_picked_on_exact_averages_when_two_print_the_same(config):
    consult_config = config(
        agents={
            "codex-sol": agent("codex", "gpt-5.6-sol", 10),
            "oc-a": agent("opencode", "a-1", 20),
            "oc-b": agent("opencode", "b-1", 30),
        }
    )
    await seed(consult_config, "codex-sol", "coding", answered(5000))
    # 2000.4 and 1999.6 both print as 2,000 ms. Rounded first, they tie and the name picks oc-a.
    await seed(consult_config, "oc-a", "coding", [(ms, None) for ms in (2000, 2000, 2000, 2000, 2002)])
    await seed(consult_config, "oc-b", "coding", [(ms, None) for ms in (1999, 1999, 2000, 2000, 2000)])

    (line,) = advice_for(consult_config)

    assert line.startswith("For coding, oc-b (b-1) averaged 2,000 ms")


async def test_the_fastest_of_several_is_the_one_named(config):
    consult_config = config(
        agents={
            "codex-sol": agent("codex", "gpt-5.6-sol", 10),
            "claude-opus": agent("claude", "opus", 20),
            "oc-fast": agent("opencode", "fast-1", 30),
            "oc-faster": agent("opencode", "faster-1", 40),
        }
    )
    await seed(consult_config, "codex-sol", "coding", answered(9000))
    await seed(consult_config, "claude-opus", "coding", answered(4000))
    await seed(consult_config, "oc-fast", "coding", answered(3000))
    await seed(consult_config, "oc-faster", "coding", answered(1000))

    (line,) = advice_for(consult_config)

    assert line.startswith("For coding, oc-faster (faster-1) averaged 1,000 ms")


async def test_advice_follows_what_the_router_would_pick_and_never_covers_review(config):
    consult_config = config()
    for capability in ("coding", "review"):
        await seed(consult_config, "codex-sol", capability, answered(4000))
        await seed(consult_config, "claude-opus", capability, answered(2000))

    lines = advice_for(consult_config)
    assert len(lines) == 1 and lines[0].startswith("For coding,")  # reviewers are named, not routed
    # The host's own runtime is left out of routing, so it is neither where `coding` goes
    # nor something to suggest instead.
    assert advice_for(consult_config, "claude") == []
    assert advice_for(consult_config, "codex") == []
    disabled = config(
        agents={
            "codex-sol": agent("codex", "gpt-5.6-sol", 10),
            "claude-opus": agent("claude", "opus", 20, enabled=False),
        }
    )
    assert advice_for(disabled) == []


async def test_advice_never_names_an_agent_or_model_the_config_no_longer_holds(config):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", answered(4000))
    # Quicker, but not configured any more.
    await seed(
        consult_config, "gemini-flash", "coding", answered(100), model="flash", runtime="antigravity"
    )
    # Configured, but on an older model than the config names now.
    await seed(consult_config, "claude-opus", "coding", answered(100), model="opus-3")

    result = report(consult_config, strengths, 30, consult_config)

    assert result["advice"] == []
    assert {g["agent_id"] for g in result["groups"]} == {"codex-sol", "gemini-flash", "claude-opus"}

    await seed(consult_config, "claude-opus", "coding", answered(100))
    (line,) = advice_for(consult_config)
    assert line.startswith("For coding, claude-opus (opus)") and "gemini-flash" not in line


async def test_a_retired_agent_that_failed_is_history_and_not_advice(config):
    consult_config = config()
    await seed(
        consult_config, "gemini-flash", "coding", failed(TIMEOUT), model="flash", runtime="antigravity"
    )

    assert advice_for(consult_config) == []
    # With no config to say what is retired, all there is to go on is what the rows say.
    assert len(report(consult_config, strengths)["advice"]) == 1


async def test_without_a_config_there_is_no_routing_advice_and_the_config_is_left_alone(config):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", answered(4000))
    await seed(consult_config, "claude-opus", "coding", answered(2000))
    before = consult_config.model_dump_json()

    assert report(consult_config, strengths)["advice"] == []
    assert len(advice_for(consult_config)) == 1
    assert consult_config.model_dump_json() == before


# --- review rows ----------------------------------------------------------------


async def test_review_rows_carry_the_scorecards_hit_rate_and_others_carry_none(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        answer=answer_with(12),
        combined_findings=decided(*["fixed"] * 9, *["rejected"] * 3),
    )
    await seed(consult_config, "codex-sol", "coding", answered(1000, 1))

    board = reviewer(report(consult_config, scorecard), "codex-sol")
    result = report(consult_config, strengths)

    row = next(g for g in result["groups"] if g["capability"] == "review" and g["agent_id"] == "codex-sol")
    assert (row["decided"], row["precision"]) == (board["decided"], board["precision"]) == (12, 0.75)
    assert "75%" in render_strengths(result)
    ordinary = next(g for g in result["groups"] if g["capability"] == "coding")
    assert ordinary["decided"] is None and ordinary["precision"] is None


async def test_a_review_row_with_too_few_decisions_prints_no_hit_rate(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        answer=answer_with(3),
        combined_findings=decided("fixed", "fixed", "rejected"),
    )

    result = report(consult_config, strengths)

    row = next(g for g in result["groups"] if g["capability"] == "review" and g["agent_id"] == "codex-sol")
    assert row["decided"] == 3 and row["precision"] is None
    assert "n<10" in render_strengths(result)


async def test_the_report_says_what_it_cannot_tell(config):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", answered(1000))

    text = " ".join(render_strengths(report(consult_config, strengths)).split())

    assert "It does not say whose answers were better" in text
    assert "only `review` rows carry a signal of that" in text


# --- masking and writing --------------------------------------------------------


async def test_a_credential_in_an_old_row_never_reaches_the_output(config):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", failed(TIMEOUT))
    # As rows from before the masking existed would have them: raw.
    write(
        consult_config,
        "UPDATE consultations SET target_agent_id = ?, target_model = ?",
        "\x1b[31mred " + SECRET,
        SECRET,
    )
    write(consult_config, "UPDATE consultation_turns SET error_code = ?", SECRET)

    result = report(consult_config, strengths, 30, consult_config)
    text = render_strengths(result) + json.dumps(result)

    assert SECRET not in text and "\x1b" not in text


async def test_the_report_writes_nothing(config, cli):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", answered(4000) + failed(TIMEOUT, 2))
    await seed(consult_config, "claude-opus", "coding", answered(1000))
    before = digest(consult_config)

    report(consult_config, strengths, 30, consult_config)
    assert cli("strengths", consult_config=consult_config)[0] == 0

    assert digest(consult_config) == before


# --- the command line -----------------------------------------------------------


async def test_the_command_prints_a_table_or_json_and_routes_as_a_plain_terminal(
    config, cli, monkeypatch
):
    consult_config = config()
    await seed(consult_config, "codex-sol", "coding", answered(4000))
    await seed(consult_config, "claude-opus", "coding", answered(2000))
    # Not inside any agent: nothing is left out of routing.
    monkeypatch.delenv(HOST_RUNTIME_ENV, raising=False)

    code, out, err = cli("strengths", consult_config=consult_config)
    assert (code, err) == (0, "")
    assert "capability" in out and "codex-sol" in out and "Worth a look" in out
    code, out, _ = cli("strengths", "--days", "7", "--json", consult_config=consult_config)
    data = json.loads(out)
    assert code == 0 and data["days"] == 7 and len(data["groups"]) == 2 and len(data["advice"]) == 1

    # Inside Claude Code, claude-opus is the host: it cannot be routed to, so it is not advice.
    monkeypatch.setenv(HOST_RUNTIME_ENV, "claude")
    code, out, _ = cli("strengths", "--json", consult_config=consult_config)
    assert code == 0 and json.loads(out)["advice"] == []


async def test_an_unmigrated_database_and_a_missing_one_are_sentences(config, cli, tmp_path):
    consult_config = config()
    old_database(consult_config.database_path)

    code, _, _ = cli("strengths", consult_config=consult_config)
    assert NOT_MIGRATED in str(code)
    code, _, _ = cli("strengths")
    assert "no database" in str(code)


def test_a_bad_option_is_a_usage_error_and_help_is_not_one(cli):
    assert cli("strengths", "--days", "0")[0] == 2
    assert cli("strengths", "--days", "3651")[0] == 2
    assert cli("strengths", "--limit", "3")[0] == 2
    assert cli("strengths", "--help")[0] == 0


def test_the_usage_text_lists_the_command(capsys):
    from orchestrator_mcp.server import main

    main(["--help"])

    assert "strengths [--days N] [--json]" in capsys.readouterr().out
