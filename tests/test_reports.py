"""`usage`, `history`, `scorecard` and `export`: read-only reports over real history.

Every database here is written by the real services, so a report is checked against
what the code actually stores rather than against rows shaped to please it. Three
properties carry the weight: a price nobody reported is never printed as zero, a
credential in stored history never reaches the output, and nothing here writes -- not
even a migration.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
import yaml

from orchestrator_mcp.consult.store import MIGRATIONS
from orchestrator_mcp.reports import (
    NOT_MIGRATED,
    SCORECARD_HEADERS,
    _parser,
    connect,
    export,
    history,
    render_history,
    render_scorecard,
    render_usage,
    run,
    scorecard,
    scorecard_cells,
    usage,
)
from orchestrator_mcp.server import main

from .conftest import consult_block
from .test_consult_dashboard import config, consult  # noqa: F401 -- fixtures
from .test_review_dashboard import (
    SECRET,
    make_review,
    old_database,
    review_config,  # noqa: F401 -- fixture
)
from .test_review_service import FINDINGS, REVIEWERS, StubAdapter


def answer_with(count: int, severity: str = "minor") -> str:
    """A reviewer's reply carrying `count` findings, in the block the parser reads."""
    findings = [
        {
            "location": f"a.py:{n}",
            "severity": severity,
            "why": f"why {n}",
            "example": "e",
            "fix": "f",
        }
        for n in range(count)
    ]
    return "Reviewed.\n\n```json\n" + json.dumps({"findings": findings}) + "\n```"


def decided(*dispositions: str, agents=("codex-sol",)):
    """One combined finding per disposition, each citing the same-numbered finding of
    every agent named -- so `("fixed", "rejected")` is two decisions."""

    def build(results):
        found = {result.agent_id: result.findings for result in results}
        return [
            {
                "problem": f"problem {n}",
                "severity": "minor",
                "agreed_by": list(agents),
                "source_finding_ids": [found[agent][n].finding_id for agent in agents],
                "disposition": disposition,
            }
            for n, disposition in enumerate(dispositions)
        ]

    return build


def read(consult_config, query, *params):
    with closing(sqlite3.connect(consult_config.database_path)) as db:
        return db.execute(query, params).fetchall()


def write(consult_config, statement, *params):
    with closing(sqlite3.connect(consult_config.database_path)) as db, db:
        db.execute(statement, params)


def report(consult_config, function, *args, **kwargs):
    with closing(connect(consult_config.database_path)) as db:
        return function(db, *args, **kwargs)


def digest(consult_config) -> str:
    return hashlib.sha256(consult_config.database_path.read_bytes()).hexdigest()


def reviewer(result, agent_id):
    return next(r for r in result["reviewers"] if r["agent_id"] == agent_id)


# --- usage -------------------------------------------------------------------


async def test_a_price_nobody_reported_is_unknown_and_never_zero(review_config):
    consult_config = review_config()
    await make_review(consult_config)

    result = report(consult_config, usage)
    text = render_usage(result)

    assert {group["agent_id"] for group in result["groups"]} == set(REVIEWERS)
    assert all(group["cost_usd"] is None for group in result["groups"])
    assert result["total"]["cost_usd"] is None
    assert "unknown" in text and "$0" not in text


async def test_one_unpriced_group_leaves_the_total_a_floor(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        adapters={
            "codex-sol": StubAdapter(FINDINGS, cost_usd=0.25),
            "gemini-x": StubAdapter(FINDINGS),
        },
    )

    result = report(consult_config, usage)
    by_agent = {group["agent_id"]: group for group in result["groups"]}

    assert by_agent["codex-sol"]["cost_usd"] == 0.25
    assert by_agent["gemini-x"]["cost_usd"] is None
    assert result["total"]["cost_usd"] is None
    assert result["total"]["known_cost_usd"] == 0.25
    assert "unknown (>= $0.2500)" in render_usage(result)


async def test_every_turn_is_counted_once_whichever_tool_asked_for_it(review_config):
    consult_config = review_config()
    await make_review(consult_config)
    await consult(consult_config, capability="coding", prompt="q")

    result = report(consult_config, usage)
    stored = read(consult_config, "SELECT COUNT(*), SUM(total_tokens) FROM consultation_turns")[0]

    assert (result["total"]["turns"], result["total"]["total_tokens"]) == tuple(stored)


async def test_a_turn_older_than_the_window_is_left_out(review_config):
    consult_config = review_config()
    await consult(consult_config, capability="coding", prompt="q")
    write(consult_config, "UPDATE consultation_turns SET created_at = '2020-01-01T00:00:00+00:00'")

    assert report(consult_config, usage, 30)["groups"] == []
    assert report(consult_config, usage, 10_000)["total"]["turns"] == 1
    assert "No turns" in render_usage(report(consult_config, usage, 30))


async def test_a_window_with_no_turns_has_no_price_rather_than_a_free_one(review_config):
    consult_config = review_config()
    await consult(consult_config, capability="coding", prompt="q")
    write(consult_config, "UPDATE consultation_turns SET created_at = '2020-01-01T00:00:00+00:00'")

    total = report(consult_config, usage, 30)["total"]

    assert total["cost_usd"] is None and total["known_cost_usd"] == 0


async def test_a_credential_in_a_stored_caveat_or_model_name_reaches_no_output(review_config):
    """Rows this version wrote hold neither, but usage and the scorecard print stored
    strings, and a row from before the masking existed is still on disk."""
    consult_config = review_config()
    await make_review(consult_config)
    write(consult_config, "UPDATE consultation_turns SET counts_incomplete = ?",
          json.dumps(["odd " + SECRET]))
    write(consult_config, "UPDATE consultations SET target_model = ?", "\x1b[31m " + SECRET)

    usage_result = report(consult_config, usage)
    card = report(consult_config, scorecard)
    texts = [
        render_usage(usage_result), json.dumps(usage_result),
        render_scorecard(card), json.dumps(card),
    ]

    assert "odd" in texts[0] and "odd" in texts[2]  # the planted field did reach the report
    assert all(SECRET not in text and "\x1b" not in text for text in texts)


async def test_a_legacy_turn_says_its_totals_are_not_comparable(review_config):
    consult_config = review_config()
    await consult(consult_config, capability="coding", prompt="q")
    write(consult_config, "UPDATE consultation_turns SET usage_semantics = 0")

    result = report(consult_config, usage)

    assert any("usage_semantics" in caveat for caveat in result["caveats"])
    assert "usage_semantics" in render_usage(result)


# --- history -----------------------------------------------------------------


async def test_history_lists_owners_newest_first_and_not_what_they_own(review_config):
    consult_config = review_config()
    review_id = await make_review(consult_config, goal="review the parser")
    await consult(consult_config, capability="coding", prompt="q")

    items = report(consult_config, history)

    # Two reviewer consultations exist underneath; only their owner is listed.
    assert sorted(item["kind"] for item in items) == ["consultation", "review"]
    assert items == sorted(items, key=lambda item: item["updated_at"], reverse=True)
    assert str(review_id) in {item["id"] for item in items}
    assert len(read(consult_config, "SELECT id FROM consultations")) == 3


async def test_history_can_be_narrowed_by_kind_and_count(review_config):
    consult_config = review_config()
    await make_review(consult_config)
    await consult(consult_config, capability="coding", prompt="q")
    await consult(consult_config, capability="coding", prompt="q")

    kinds = {item["kind"] for item in report(consult_config, history, 20, "consultation")}

    assert kinds == {"consultation"}
    assert len(report(consult_config, history, 1)) == 1
    assert report(consult_config, history, 20, "workflow") == []
    assert render_history([]) == "Nothing recorded yet."


async def test_a_credential_and_a_terminal_escape_in_a_goal_never_reach_the_table(review_config):
    consult_config = review_config()
    await make_review(consult_config, finalize=False)
    # As a row from before the masking existed would have it: raw.
    write(consult_config, "UPDATE reviews SET goal = ?", "\x1b[31mred " + SECRET)

    text = render_history(report(consult_config, history))

    assert SECRET not in text and "\x1b" not in text


# --- scorecard ---------------------------------------------------------------


async def test_twelve_decided_findings_earn_a_rate(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        answer=answer_with(12),
        combined_findings=decided(*["fixed"] * 9, *["rejected"] * 3),
    )

    result = report(consult_config, scorecard)
    codex = reviewer(result, "codex-sol")

    assert (codex["kept"], codex["rejected"], codex["decided"]) == (9, 3, 12)
    assert codex["precision"] == 0.75
    assert "75%" in render_scorecard(result)


async def test_too_few_decisions_print_no_rate(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        answer=answer_with(3),
        combined_findings=decided("fixed", "fixed", "rejected"),
    )

    result = report(consult_config, scorecard)
    codex = reviewer(result, "codex-sol")

    assert codex["precision"] is None and codex["decided"] == 3
    assert "n<10" in render_scorecard(result)


async def test_open_findings_count_in_neither_kept_nor_rejected(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        answer=answer_with(3),
        combined_findings=decided("open", "accepted_risk", "rejected"),
    )

    codex = reviewer(report(consult_config, scorecard), "codex-sol")

    assert (codex["open"], codex["kept"], codex["rejected"]) == (1, 1, 1)


async def test_a_finding_two_reviewers_raised_counts_once_for_each(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        answer=answer_with(1),
        combined_findings=decided("fixed", agents=("codex-sol", "gemini-x")),
    )

    result = report(consult_config, scorecard)

    assert {r["agent_id"]: r["kept"] for r in result["reviewers"]} == {
        "codex-sol": 1,
        "gemini-x": 1,
    }


def serious(*findings):
    """One combined finding per `(severity, disposition, agents)`, citing finding n of
    each agent named, where n is its place in the list."""

    def build(results):
        found = {result.agent_id: result.findings for result in results}
        return [
            {
                "problem": f"problem {n}",
                "severity": severity,
                "agreed_by": list(agents),
                "source_finding_ids": [found[agent][n].finding_id for agent in agents],
                "disposition": disposition,
                "disposition_reason": "decided",
            }
            for n, (severity, disposition, agents) in enumerate(findings)
        ]

    return build


async def test_a_serious_finding_only_one_reviewer_raised_counts_for_it_alone(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        answer=answer_with(5),
        combined_findings=serious(
            ("critical", "open", ("codex-sol",)),
            ("important", "fixed", ("codex-sol",)),
            ("critical", "fixed", ("codex-sol", "gemini-x")),  # shared: neither alone
            ("critical", "rejected", ("gemini-x",)),  # rejected: no catch
            ("minor", "fixed", ("gemini-x",)),  # not serious
        ),
    )

    result = report(consult_config, scorecard)

    assert {r["agent_id"]: r["sole_serious"] for r in result["reviewers"]} == {
        "codex-sol": 2,
        "gemini-x": 0,
    }
    assert "sole serious" in render_scorecard(result)


async def test_a_reviewer_that_never_answered_beside_another_has_no_sole_count(review_config):
    """Alone beside nobody is not a catch, and no chance to make one is not zero."""
    consult_config = review_config()
    await make_review(
        consult_config,
        adapters={
            "codex-sol": StubAdapter(answer_with(1)),
            "gemini-x": StubAdapter(answer_with(1), error=RuntimeError("boom")),
        },
        combined_findings=serious(("critical", "open", ("codex-sol",))),
    )

    codex = reviewer(report(consult_config, scorecard), "codex-sol")

    assert codex["answered"] == 1 and codex["open"] == 1
    assert codex["sole_serious"] is None
    assert scorecard_cells(codex)[SCORECARD_HEADERS.index("sole serious")] == "-"


async def test_an_unreadable_synthesis_has_no_sole_count(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        answer=answer_with(1),
        combined_findings=serious(("critical", "open", ("codex-sol",))),
    )
    write(consult_config, "UPDATE reviews SET summary_json = '{not json'")

    result = report(consult_config, scorecard)

    assert {r["agent_id"]: r["sole_serious"] for r in result["reviewers"]} == {
        "codex-sol": None,
        "gemini-x": None,
    }


async def test_a_reviewer_that_failed_is_told_apart_from_one_never_started(review_config):
    consult_config = review_config()
    await make_review(
        consult_config,
        adapters={
            "codex-sol": StubAdapter(answer_with(1)),
            "gemini-x": StubAdapter(answer_with(1), error=RuntimeError("boom")),
        },
        finalize=False,
    )
    failed = read(
        consult_config, "SELECT status, error_code FROM review_consultations WHERE agent_id = ?",
        "gemini-x",
    )[0]
    assert failed[0] == "failed"

    errored = reviewer(report(consult_config, scorecard), "gemini-x")
    assert (errored["asked"], errored["answered"], errored["errored"]) == (1, 0, 1)

    write(
        consult_config,
        "UPDATE review_consultations SET error_code = 'not_started' WHERE agent_id = 'gemini-x'",
    )
    assert {r["agent_id"] for r in report(consult_config, scorecard)["reviewers"]} == {"codex-sol"}


async def test_a_recheck_does_not_count_the_same_findings_twice(review_config):
    consult_config = review_config()
    parent = await make_review(consult_config, answer=answer_with(1))
    await make_review(consult_config, answer=answer_with(1), parent_review_id=parent)

    result = report(consult_config, scorecard)

    assert {r["agent_id"]: r["asked"] for r in result["reviewers"]} == {
        "codex-sol": 1,
        "gemini-x": 1,
    }


async def test_a_review_with_no_synthesis_is_counted_asked_but_not_judged(review_config):
    """Which is every review under `store_full_content: false`, where finalizing is
    refused and no summary is ever written."""
    consult_config = review_config(store_full_content=False)
    await make_review(consult_config, answer=answer_with(2), finalize=False)

    result = report(consult_config, scorecard)
    codex = reviewer(result, "codex-sol")

    assert codex["asked"] == 1 and codex["answered"] == 1
    assert codex["kept"] is None and codex["precision"] is None
    assert result["unsynthesised_reviews"] == 1
    assert "no synthesis on record" in render_scorecard(result)


@pytest.mark.parametrize("status", ["running", "cancelled"])
async def test_a_reviewer_that_answered_is_scored_though_its_review_never_settled(
    review_config, status
):
    """A cancel keeps the rows of reviewers that had finished, and their turns are in
    `usage`; leaving them out of the scorecard would make the two disagree."""
    consult_config = review_config()
    await make_review(consult_config, finalize=False)
    write(consult_config, "UPDATE reviews SET status = ?", status)
    write(
        consult_config,
        "UPDATE review_consultations SET status = 'failed', error_code = 'not_started' "
        "WHERE agent_id = 'gemini-x'",
    )

    result = report(consult_config, scorecard)

    assert [r["agent_id"] for r in result["reviewers"]] == ["codex-sol"]  # gemini-x never ran
    codex = result["reviewers"][0]
    assert codex["asked"] == 1 and codex["answered"] == 1 and codex["turns"] == 1
    assert codex["kept"] is None  # no synthesis, so nothing to count as kept


async def test_a_review_that_reached_no_reviewer_is_not_scored(review_config):
    consult_config = review_config()
    await make_review(consult_config, finalize=False)
    write(consult_config, "UPDATE reviews SET status = 'running'")
    write(
        consult_config,
        "UPDATE review_consultations SET status = 'failed', error_code = 'not_started'",
    )

    result = report(consult_config, scorecard)

    assert result["reviewers"] == []
    assert "No reviews" in render_scorecard(result)


# --- export ------------------------------------------------------------------


async def test_a_prefix_finds_the_record_and_names_what_it_owns(review_config):
    consult_config = review_config()
    review_id = str(await make_review(consult_config))

    document = report(consult_config, export, review_id[:8])

    assert document["kind"] == "review" and document["review"]["id"] == review_id
    assert {r["agent_id"] for r in document["reviewers"]} == set(REVIEWERS)
    # Their conversations are named, not inlined.
    assert all(r["consultation_id"] for r in document["reviewers"])
    assert isinstance(document["review"]["summary_json"], dict)


async def test_an_export_carries_what_the_newest_recheck_left_open(review_config):
    consult_config = review_config()
    parent = str(await make_review(consult_config))
    cited = '```json\n{"findings": [{"severity": "critical", "previous": "P1"}]}\n```'
    child = await make_review(
        consult_config,
        parent_review_id=parent,
        finalize=False,
        adapters={"codex-sol": StubAdapter(cited), "gemini-x": StubAdapter(answer_with(0))},
    )

    document = report(consult_config, export, parent)

    assert document["finding_status"] == [
        {"index": 0, "recheck_id": str(child), "status": "still_open"}
    ]
    assert report(consult_config, export, str(child))["finding_status"] == []


async def test_a_consultation_exports_its_turns(review_config):
    consult_config = review_config()
    response = await consult(consult_config, capability="coding", prompt="what colour")

    document = report(consult_config, export, str(response.consultation_id))

    assert document["kind"] == "consultation"
    assert [turn["sequence_number"] for turn in document["turns"]] == [1]


@pytest.mark.parametrize("ident", ["abc", "zzzzzzzz", "%%%%%%%%", "________"])
async def test_an_id_that_is_short_unknown_or_all_wildcards_is_refused(review_config, ident):
    consult_config = review_config()
    await make_review(consult_config)

    with pytest.raises(SystemExit) as refusal:
        report(consult_config, export, ident)

    assert str(refusal.value).startswith("orchestrator-mcp-server: ")


async def test_a_prefix_two_records_share_is_refused_rather_than_guessed(review_config):
    consult_config = review_config()
    await consult(consult_config, capability="coding", prompt="q")
    await consult(consult_config, capability="coding", prompt="q")
    write(consult_config, "UPDATE consultations SET id = 'sameprefix-' || rowid")

    with pytest.raises(SystemExit) as refusal:
        report(consult_config, export, "sameprefix")

    assert "more than one" in str(refusal.value)


async def test_a_credential_in_a_json_column_and_in_a_raw_row_comes_out_masked(review_config):
    consult_config = review_config()
    review_id = str(await make_review(consult_config))
    response = await consult(consult_config, capability="coding", prompt="q")
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIabcdef\n-----END RSA PRIVATE KEY-----"
    # Rows as an older version left them: nothing masked on the way in.
    write(
        consult_config,
        "UPDATE reviews SET context = ?, summary_json = ?",
        key,
        json.dumps({"summary": "leaked " + SECRET}),
    )
    write(consult_config, "UPDATE consultation_turns SET user_prompt = ?", "use " + SECRET)

    reviewed = json.dumps(report(consult_config, export, review_id))
    consulted = json.dumps(report(consult_config, export, str(response.consultation_id)))

    assert SECRET not in reviewed and SECRET not in consulted
    assert "MIIabcdef" not in reviewed


async def test_a_report_reads_one_snapshot_even_if_the_record_is_deleted_mid_way(review_config):
    """Another server process may finalize or delete while a report runs. Each SELECT
    on its own would see a different database; the report has to see one."""
    consult_config = review_config()
    review_id = str(await make_review(consult_config))

    with closing(connect(consult_config.database_path)) as db:
        write(consult_config, "DELETE FROM reviews WHERE id = ?", review_id)
        found = export(db, review_id)

    assert found["review"]["id"] == review_id


async def test_a_session_id_and_a_token_hash_are_never_exported(review_config):
    consult_config = review_config()
    review_id = str(await make_review(consult_config))
    response = await consult(consult_config, capability="coding", prompt="q")

    text = json.dumps(report(consult_config, export, review_id)) + json.dumps(
        report(consult_config, export, str(response.consultation_id))
    )

    assert "native_session_id" not in text and "native-1" not in text
    assert "token_sha" not in text


async def test_an_export_without_stored_content_says_so(review_config):
    consult_config = review_config(store_full_content=False)
    response = await consult(consult_config, capability="coding", prompt="q")

    document = report(consult_config, export, str(response.consultation_id))
    review_id = str(await make_review(consult_config, finalize=False))

    assert "store_full_content" in document["note"]
    assert "store_full_content" in report(consult_config, export, review_id)["note"]


# --- refusals ----------------------------------------------------------------


def refused(path) -> str:
    with pytest.raises(SystemExit) as refusal:
        connect(path).close()
    assert refusal.value.code not in (0, None)
    return str(refusal.value)


def test_no_database_yet_is_a_sentence(tmp_path):
    assert "no database" in refused(tmp_path / "missing.sqlite3")


def test_a_file_that_is_not_a_database_is_a_sentence(tmp_path):
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"this is not sqlite" * 100)

    assert NOT_MIGRATED in refused(empty)
    assert "not a readable database" in refused(junk)


def test_a_database_with_no_ledger_is_told_to_migrate(config):
    consult_config = config()
    old_database(consult_config.database_path)

    assert NOT_MIGRATED in refused(consult_config.database_path)


async def test_a_ledger_behind_the_code_is_told_to_migrate(review_config):
    consult_config = review_config()
    await consult(consult_config, capability="coding", prompt="q")
    write(
        consult_config,
        "DELETE FROM schema_migrations WHERE version = ?",
        len(MIGRATIONS) - 1,
    )

    assert NOT_MIGRATED in refused(consult_config.database_path)


# --- the command line --------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys, host_claude):
    """`main` against a config file on disk, which is how the server finds one."""

    def call(*args, consult_config=None):
        database = consult_config.database_path if consult_config else tmp_path / "none.sqlite3"
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"consult": consult_block(database_path=str(database))}))
        monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(path))
        with pytest.raises(SystemExit) as done:
            main(list(args))
        captured = capsys.readouterr()
        return done.value.code, captured.out, captured.err

    return call


async def test_every_subcommand_runs_from_main_and_prints_json_on_request(review_config, cli):
    consult_config = review_config()
    review_id = str(await make_review(consult_config))

    for args in (["usage"], ["history"], ["scorecard"]):
        code, out, err = cli(*args, consult_config=consult_config)
        assert (code, err) == (0, "") and out.strip(), args
        code, out, _ = cli(*args, "--json", consult_config=consult_config)
        assert code == 0 and json.loads(out) is not None, args
    code, out, _ = cli("export", review_id, consult_config=consult_config)
    assert code == 0 and json.loads(out)["review"]["id"] == review_id


async def test_reports_leave_the_database_file_untouched(review_config, cli):
    consult_config = review_config()
    review_id = str(await make_review(consult_config))
    before = digest(consult_config)

    for args in (["usage"], ["history"], ["scorecard"], ["export", review_id]):
        assert cli(*args, consult_config=consult_config)[0] == 0

    assert digest(consult_config) == before


def test_a_config_without_a_consult_block_is_a_sentence():
    with pytest.raises(SystemExit) as done:
        run(["usage"], lambda: {"other": {}})

    assert "consult:" in str(done.value)


def test_a_missing_config_file_is_its_own_sentence(cli, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(tmp_path / "absent.yaml"))

    with pytest.raises(SystemExit) as done:
        main(["history"])

    assert "config not found" in str(done.value)


async def test_a_count_too_big_for_a_date_or_an_integer_is_a_usage_error(cli, review_config):
    """Unbounded, `--days` overflows `datetime` and `--limit` overflows SQLite, but only
    once there is a database to read, so the refusal has to come from the parser."""
    consult_config = review_config()
    await make_review(consult_config)

    def code(*args):
        return cli(*args, consult_config=consult_config)[0]

    assert code("usage", "--days", "1000000000") == 2
    assert code("scorecard", "--days", "3651") == 2
    assert code("history", "--limit", str(10**30)) == 2
    assert _parser().parse_args(["usage", "--days", "3650"]).days == 3650


def test_a_bad_option_is_a_usage_error_and_help_is_not_one(cli):
    assert cli("usage", "--days", "0")[0] == 2
    assert cli("usage", "--nope")[0] == 2
    assert cli("history", "--kind", "nonsense")[0] == 2
    assert cli("export")[0] == 2
    assert cli("scorecard", "--help")[0] == 0
    assert cli("nonsense")[0] == 2
