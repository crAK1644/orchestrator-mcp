"""Persistence: permissions, session binding, and cross-process leases."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from uuid import uuid4

import pytest

from orchestrator_mcp.consult.contract import ConsultRoute, SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.routing import ExcludedCandidate, RoutingDecision
from orchestrator_mcp.consult.store import (
    MIGRATIONS,
    USAGE_SEMANTICS,
    ConsultStore,
    StoreError,
)
from orchestrator_mcp.review.store import ReviewStore

ROUTE = ConsultRoute(
    agent_id="codex-sol",
    runtime="codex",
    model="gpt-5.6-sol",
    capability_score=90,
    priority=10,
    explicitly_selected=False,
)


@pytest.fixture
async def store(tmp_path):
    opened = await ConsultStore(tmp_path / "nested" / "consultations.sqlite3").open()
    yield opened
    await opened.close()


async def new_consultation(store, **kwargs):
    consultation_id = uuid4()
    await store.create_consultation(
        consultation_id=consultation_id,
        origin_runtime="claude",
        route=kwargs.pop("route", ROUTE),
        capability=kwargs.pop("capability", "research"),
        protocol_version="consult-v1",
        config_hash="abc123",
        **kwargs,
    )
    return consultation_id


# --- storage on disk --------------------------------------------------------


async def test_the_database_and_its_directory_are_user_only(store):
    """The file holds the scrubbed copy of every retained prompt and answer."""
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700


async def test_an_existing_shared_database_directory_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)

    with pytest.raises(StoreError, match="permissions 0700"):
        await ConsultStore(shared / "consultations.sqlite3").open()

    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    assert not (shared / "consultations.sqlite3").exists()


def test_a_migration_carries_no_sql_comment():
    """Prose about a migration is a Python comment above the string, not a `--` comment
    inside it, and the reason is that two things here read the statement text as SQL and
    nothing else. The runner splits on `;`, so a semicolon written as ordinary
    punctuation ends the statement early. The `ADD COLUMN` tolerance checks the text
    *starts* with `ALTER TABLE`, so a statement wearing a comment stops being idempotent
    and fails the next time a rewound ledger replays it. Both surface as a syntax error
    or a duplicate column, and neither reads as a comment problem."""
    for version, statements in enumerate(MIGRATIONS):
        for line in statements.splitlines():
            assert not line.strip().startswith("--"), f"migration {version}: {line.strip()}"


async def test_opening_twice_is_not_a_second_migration(tmp_path):
    path = tmp_path / "db.sqlite3"
    first = await ConsultStore(path).open()
    await first.close()
    second = await ConsultStore(path).open()
    versions = second._db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    profiles = second._db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    await second.close()
    # Against `len(MIGRATIONS)` rather than a literal: the ledger grows by design,
    # and what this test is about is that a second open applies none of them again.
    assert (versions, profiles) == (len(MIGRATIONS), 1)


async def test_a_database_that_lost_its_profile_row_gets_it_back_on_open(tmp_path):
    """Found by a live review that died in 10ms with `IntegrityError`.

    Every consultation references the default profile, and the row was created only
    inside the migration loop -- so a database already at the current version never
    recreated it. Emptying the tables (a purge, a restore, a manual repair) left an
    installation that could never consult again, and said only `IntegrityError`.
    """
    path = tmp_path / "db.sqlite3"
    first = await ConsultStore(path).open()
    await first.close()

    scratch = sqlite3.connect(path)
    scratch.execute("DELETE FROM profiles")
    scratch.commit()
    scratch.close()

    second = await ConsultStore(path).open()
    try:
        # The insert, not the row count: this has to fail the way the live one did,
        # on the foreign key, rather than pass because some other row was restored.
        await new_consultation(second)
    finally:
        await second.close()


async def test_usage_is_rebuilt_from_every_recorded_turn(store):
    consultation_id = await new_consultation(store)
    # Totals that are their own parts. Every turn the service records derives it that
    # way, and three numbers here that did not add up would describe a ledger this
    # server cannot write -- which the rollup now says so about.
    await store.record_turn(
        consultation_id,
        1,
        SourceMode.MODEL,
        "q1",
        None,
        "compiled",
        input_tokens=10,
        output_tokens=2,
        cost_usd=0.2,
    )
    await store.record_turn(
        consultation_id,
        2,
        SourceMode.MODEL,
        "q2",
        None,
        "compiled",
        input_tokens=20,
        output_tokens=4,
        cost_usd=0.3,
    )

    usage = await store.usage(consultation_id)
    assert usage.model_dump() == {
        "prompt_tokens": 30,
        "completion_tokens": 6,
        "total_tokens": 36,
        "cost_usd": 0.5,
        # Both turns counted under one rule, and their total is their parts: nothing
        # about this sum needs saying.
        "counts_incomplete": [],
    }


# --- consultation deletion -------------------------------------------------


async def test_delete_consultation_removes_its_local_history(store):
    consultation_id = await new_consultation(store)
    await store.record_turn(
        consultation_id, 1, SourceMode.MODEL, "q", None, "compiled"
    )

    assert await store.delete_consultation(consultation_id) == 1
    with pytest.raises(StoreError, match="no consultation"):
        await store.get_consultation(consultation_id)


async def test_delete_all_consultations_is_bound_to_the_previewed_snapshot(store):
    first = await new_consultation(store)
    second = await new_consultation(store)
    token, count = await store.request_delete_all_consultations()
    later = await new_consultation(store)

    assert count == 2
    assert await store.delete_all_consultations(token) == 2
    assert (await store.get_consultation(later)).id == str(later)
    for removed in (first, second):
        with pytest.raises(StoreError, match="no consultation"):
            await store.get_consultation(removed)
    with pytest.raises(StoreError, match="not outstanding"):
        await store.delete_all_consultations(token)


async def test_a_consultation_with_a_turn_in_flight_cannot_be_deleted(store):
    consultation_id = await new_consultation(store)
    async with store.lease(consultation_id):
        with pytest.raises(StoreError) as error:
            await store.delete_consultation(consultation_id)

    assert error.value.code is ConsultErrorCode.SESSION_BUSY
    assert (await store.get_consultation(consultation_id)).id == str(consultation_id)


async def test_a_review_owned_consultation_can_only_be_deleted_with_its_review(store):
    consultation_id = await new_consultation(store)
    review_id = uuid4()
    reviews = ReviewStore(store)
    await reviews.create_review(
        review_id=review_id,
        mode="standard",
        goal="g",
        context=None,
        material=[],
        material_sha256="a" * 64,
        raw_sha256="b" * 64,
        reviewer_snapshot=[],
        confirm_token="token",
        secret_hits=[],
        web_requested=False,
    )
    await reviews.record_reviewer_result(
        review_id, "codex-sol", "ok", consultation_id=consultation_id
    )

    with pytest.raises(StoreError, match="delete the review instead"):
        await store.delete_consultation(consultation_id)
    _, count = await store.request_delete_all_consultations()
    assert count == 0


async def test_a_migration_that_fails_does_not_leave_a_store_that_looks_open(tmp_path, monkeypatch):
    """`open()` does its work only while `_connection` is None, so a connection left
    behind by a migration that raised would be used by every later call -- against a
    schema that was never finished, and with the exception long since swallowed by
    whoever caught the first open."""
    store = ConsultStore(tmp_path / "db.sqlite3")

    published = []

    def no(self, db):
        published.append(self._connection)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ConsultStore, "_migrate", no)
    with pytest.raises(sqlite3.OperationalError):
        await store.open()

    # Nothing was visible while the schema was still being built. That is the same
    # invariant from the other direction: `_open` runs in a worker thread, and a
    # caller cancelling its `await` releases `_open_lock` without stopping it, so a
    # connection published before `_migrate` returns is reachable by the next opener
    # even when nothing raised at all.
    assert published == [None]
    assert store._connection is None
    # And it says so rather than handing back a connection to an unmigrated file.
    with pytest.raises(StoreError):
        store._db

    # A later open, with the fault gone, still migrates.
    monkeypatch.undo()
    await store.open()
    assert store._db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(MIGRATIONS)
    await store.close()


async def test_a_cancelled_open_never_publishes_the_connection_it_built(tmp_path, monkeypatch):
    """`asyncio.to_thread` does not stop a worker when its awaiting task is cancelled,
    and cancellation releases `_open_lock` on the way out. A worker that assigned
    `_connection` itself would therefore land on a store somebody else has since
    opened -- or closed -- reopening it from the outside, after the close returned."""
    real = ConsultStore._migrate
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow(self, db):
        started.set()
        release.wait(5)
        real(self, db)
        finished.set()

    monkeypatch.setattr(ConsultStore, "_migrate", slow)
    store = ConsultStore(tmp_path / "db.sqlite3")
    opening = asyncio.create_task(store.open())
    await asyncio.to_thread(started.wait, 5)

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    release.set()
    await asyncio.to_thread(finished.wait, 5)
    assert store._connection is None

    # And the store is still openable -- the cancelled attempt left nothing behind
    # that the next one has to work around.
    monkeypatch.undo()
    await store.open()
    assert store._db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(MIGRATIONS)
    await store.close()


OPENER = """\
import asyncio, sys, time
from orchestrator_mcp.consult.store import ConsultStore

async def main(path, start_at):
    time.sleep(max(0.0, start_at - time.time()))  # all of us hit an empty file together
    store = await ConsultStore(path).open()
    print("opened")
    await store.close()

asyncio.run(main(sys.argv[1], float(sys.argv[2])))
"""


async def test_processes_opening_one_new_database_together_all_migrate_it(tmp_path):
    """The version check before `BEGIN IMMEDIATE` is not the one that decides.

    Every process reads an empty `schema_migrations`, then SQLite serializes them on
    the write lock. Without the recheck inside the transaction, the losers run
    `CREATE TABLE` against the schema the winner just committed and `open()` raises.
    """
    path = tmp_path / "raced.sqlite3"
    start_at = time.time() + 1.0
    args = [sys.executable, "-c", textwrap.dedent(OPENER), str(path), str(start_at)]
    openers = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for _ in range(6)]

    for opener in openers:
        stdout, stderr = opener.communicate(timeout=60)
        assert opener.returncode == 0, stderr
        assert stdout.strip() == "opened"

    store = await ConsultStore(path).open()
    counts = tuple(store._db.execute(
        "SELECT (SELECT COUNT(*) FROM schema_migrations), (SELECT COUNT(*) FROM profiles)"
    ).fetchone())
    await store.close()
    assert counts == (len(MIGRATIONS), 1)


async def test_wal_is_on(store):
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# --- consultations and turns ------------------------------------------------


async def test_a_consultation_records_what_it_is_bound_to(store):
    consultation_id = await new_consultation(store, conversation_label="planning")
    consultation = await store.get_consultation(consultation_id)

    assert consultation.target_agent_id == "codex-sol"
    assert consultation.target_model == "gpt-5.6-sol"
    assert consultation.native_session_id is None
    assert consultation.conversation_label == "planning"
    assert consultation.status == "open"


async def test_an_unknown_consultation_id_is_session_not_found(store):
    """Never silently a new conversation: the caller asked to continue one."""
    with pytest.raises(StoreError) as exc:
        await store.get_consultation(uuid4())
    assert exc.value.code is ConsultErrorCode.SESSION_NOT_FOUND


async def test_a_session_cannot_switch_agent(store):
    consultation_id = await new_consultation(store)
    consultation = await store.get_consultation(consultation_id)

    store.check_target(consultation, "codex-sol")  # the one it started with
    store.check_target(consultation, None)  # not naming one is fine

    with pytest.raises(StoreError) as exc:
        store.check_target(consultation, "claude-opus")
    assert exc.value.code is ConsultErrorCode.SESSION_TARGET_MISMATCH


async def test_the_native_session_binds_once(store):
    consultation_id = await new_consultation(store)
    await store.bind_native_session(consultation_id, "thread-1")
    await store.bind_native_session(consultation_id, "thread-1")  # idempotent

    with pytest.raises(StoreError) as exc:
        await store.bind_native_session(consultation_id, "thread-2")
    assert exc.value.code is ConsultErrorCode.SESSION_TARGET_MISMATCH
    assert (await store.get_consultation(consultation_id)).native_session_id == "thread-1"


async def test_turns_are_stored_in_full_and_numbered(store):
    consultation_id = await new_consultation(store)
    assert await store.next_sequence(consultation_id) == 1

    await store.record_turn(
        consultation_id,
        1,
        SourceMode.DOCUMENT,
        user_prompt="what colour is the sky",
        context="the sky is blue",
        compiled_prompt="SYSTEM...\n{...}",
        raw_output='{"answer": "blue"}',
        validated_response={"answer": "blue"},
        input_tokens=11,
        output_tokens=3,
        cost_usd=0.0004,
        latency_ms=1234,
    )

    assert await store.next_sequence(consultation_id) == 2
    (turn,) = await store.turns(consultation_id)
    assert turn.user_prompt == "what colour is the sky"
    assert turn.context == "the sky is blue"
    assert turn.compiled_prompt == "SYSTEM...\n{...}"
    assert turn.raw_output == '{"answer": "blue"}'
    assert json.loads(turn.validated_response_json) == {"answer": "blue"}
    assert (turn.input_tokens, turn.output_tokens, turn.latency_ms) == (11, 3, 1234)



async def test_a_new_turn_records_which_definition_its_tokens_were_counted_by(store, tmp_path):
    """The three token columns changed meaning, and a row cannot say which one it used.

    Every row written before `Usage` fixed its fields carries whatever its own CLI
    reported: `input_tokens` is the uncached remainder on a Claude row and the whole
    prompt on a Codex one, and `total_tokens` exceeds its parts on the first while
    falling short on the second. Added beside a row written since, they produce a
    number in no unit at all -- and the rollups do add them, across agents, into one
    column of the dashboard.
    """
    consultation_id = await new_consultation(store)
    await store.record_turn(
        consultation_id,
        1,
        SourceMode.DOCUMENT,
        user_prompt="what colour is the sky",
        context=None,
        compiled_prompt="SYSTEM...",
        input_tokens=1800,
        output_tokens=200,
    )

    assert usage_semantics_rows(tmp_path) == [USAGE_SEMANTICS]
    # 0 is reserved for what came before, so the definition in force can never be it.
    assert USAGE_SEMANTICS > 0


async def test_a_turn_written_before_the_column_existed_reads_as_legacy(store, tmp_path):
    """Not a backfill, which is the point of the default.

    What a turn reported is what was measured at the time, and the fields needed to
    restate an old row live in `raw_output` when they are anywhere at all -- absent
    entirely under `store_full_content: false`. Recording which rule was in force costs
    one column and cannot be recovered later; inventing the numbers would be a claim
    about data nobody counted.
    """
    consultation_id = await new_consultation(store)
    legacy_turn(tmp_path, consultation_id, sequence_number=1)

    assert usage_semantics_rows(tmp_path) == [0]


async def test_a_rollup_that_adds_two_definitions_says_so(store, tmp_path):
    """What the marker was recorded for, and the only thing that reads it.

    A turn from before the column and a turn from after are in different units, and
    added they make a number in neither. There is no repair: the fields that would
    restate the old row live in `raw_output` where they survive at all. So the sum is
    returned and labelled -- suppressing it would decide for the caller that an
    approximate figure is worse than none, which depends on what they are doing.
    """
    consultation_id = await new_consultation(store)
    legacy_turn(tmp_path, consultation_id, sequence_number=1)
    await store.record_turn(
        consultation_id,
        2,
        SourceMode.DOCUMENT,
        user_prompt="and now",
        context=None,
        compiled_prompt="SYSTEM...",
        input_tokens=1800,
        output_tokens=200,
    )

    usage = await store.usage(consultation_id)
    assert usage.total_tokens == 1744808 + 2000
    legacy, mixed, arithmetic = usage.counts_incomplete
    # Three notes over two rows, and each answers a question the others do not: one
    # row understates its prompt, the two cannot be added, one contradicts itself.
    assert legacy.startswith("at least one turn here was counted under the definition")
    assert "more than one definition" in mixed
    assert "usage_semantics 0 through 1" in mixed
    # And the same group fails the other test too, which is not redundant: one says
    # the units differ, the other says the numbers returned contradict their own
    # definition. A reader can act on the second without knowing what caused it.
    # The whole note, so it says that and nothing after it: naming legacy rows as the
    # cause would be reading the `usage_semantics` columns this note is not computed
    # from, and banning that one phrase leaves every other wording of it. Pinning the
    # count too is what proves it reads `contradicting_turns` rather than `turns` --
    # one of these two rows contradicts itself and the other does not.
    assert arithmetic == (
        "1 of these 2 turns total something other than their own prompt and completion "
        "columns added, so the three numbers here are not one measurement"
    )


async def test_a_ledger_written_entirely_before_the_rule_still_contradicts_it(store, tmp_path):
    """Why the legacy note was not the whole test either.

    These rows are legacy *and* they disagree with their own columns, and the second
    fact is not the first: a row written under the current marker can contradict
    itself too, and a legacy row can be perfectly self-consistent -- the backfill made
    most of them so. Two notes because a reader can act on the arithmetic one without
    knowing what caused it.
    """
    consultation_id = await new_consultation(store)
    legacy_turn(tmp_path, consultation_id, sequence_number=1)
    legacy_turn(tmp_path, consultation_id, sequence_number=2)

    usage = await store.usage(consultation_id)
    legacy, note = usage.counts_incomplete
    assert legacy.startswith("every turn here was counted under the definition")
    assert "2 of these 2 turns total something other than their own prompt and " in note


async def test_a_ledger_the_backfill_made_consistent_still_says_it_is_legacy(store, tmp_path):
    """The rows both detectors used to miss, which is most of the rows on disk.

    The migration that added `total_tokens` set it to the two columns added, so a row
    written before that migration agrees with itself by construction and the
    arithmetic note has nothing to say about it. Every row in a ledger like this one
    is legacy, so `MIN == MAX` and the semantics note used to have nothing to say
    either. What came back was a real Claude ledger reporting its prompts in the tens
    of tokens where the prompts actually sent ran to hundreds of thousands, and an
    empty caveat list beside it -- the failure the marker column was added to catch,
    silent on exactly the rows a reader is likeliest to be looking at.
    """
    consultation_id = await new_consultation(store)
    for sequence in (1, 2):
        legacy_turn(tmp_path, consultation_id, sequence, tokens=(22, 305704, 22 + 305704))

    usage = await store.usage(consultation_id)
    # Nothing here looks wrong, which is the whole difficulty: the columns add, and 44
    # prompt tokens reads as a number rather than as half a million gone missing.
    assert usage.prompt_tokens + usage.completion_tokens == usage.total_tokens
    # The whole note, because a caveat that is wrong about its own subject is worse
    # than none: an earlier wording said a cached turn counted only the missed share,
    # which is claude's behaviour asserted over a group that may be entirely codex.
    assert usage.counts_incomplete == [
        "every turn here was counted under the definition that predates usage_semantics "
        "1, where a prompt figure was whatever its runtime called input -- claude "
        "counted only the share that missed cache, codex the whole prompt -- so these "
        "totals are not comparable across runtimes and can sit far below what was sent "
        "and paid for"
    ]


async def test_the_legacy_note_counts_no_turns_because_it_cannot(store, tmp_path):
    """A group of one, which the mixed note can never be and this one often is.

    Every other caveat here is free to say how many turns it covers. This one is not:
    a mixed group needs two rows by definition, an all-legacy group needs one, and
    "these 1 turns" is how a caveat stops being read.
    """
    consultation_id = await new_consultation(store)
    legacy_turn(tmp_path, consultation_id, 1, tokens=(22, 305704, 22 + 305704))

    (note,) = (await store.usage(consultation_id)).counts_incomplete
    assert note.startswith("every turn here was counted under")


async def test_two_turns_wrong_in_opposite_directions_do_not_make_a_right_one(
    store, tmp_path
):
    """A residual carries a sign, and the caveat must not be read off the sums.

    Both rows here contradict the definition -- one totals three over its own parts,
    the other three under -- and the group's three columns add up perfectly, because
    those two errors are what cancelled. Counting the turns is what survives that:
    asked how many rows disagree with themselves, SQL answers two, and the arithmetic
    that hid it never enters the question.
    """
    consultation_id = await new_consultation(store)
    legacy_turn(tmp_path, consultation_id, sequence_number=1, tokens=(100, 100, 203))
    legacy_turn(tmp_path, consultation_id, sequence_number=2, tokens=(100, 100, 197))

    usage = await store.usage(consultation_id)
    # The sums agree, which is the trap: 200 + 200 == 400 and every row is still wrong.
    assert usage.prompt_tokens + usage.completion_tokens == usage.total_tokens
    _legacy, note = usage.counts_incomplete
    assert note == (
        "2 of these 2 turns total something other than their own prompt and completion "
        "columns added, so the three numbers here are not one measurement"
    )


async def test_one_turn_that_contradicts_itself_names_both_of_its_numbers(store, tmp_path):
    """The other branch of the same caveat, which a group of one takes.

    Where several turns cancel, the two sums are the arithmetic that hid the problem
    and quoting them would hand it to the reader as though it were evidence. A group
    of one has no such sums: the figures are the row, so it says them.
    """
    consultation_id = await new_consultation(store)
    legacy_turn(tmp_path, consultation_id, sequence_number=1, tokens=(100, 100, 203))

    usage = await store.usage(consultation_id)
    _legacy, note = usage.counts_incomplete
    # Legacy is the likeliest reason and is still not what this note was computed
    # from, so it is not what the note says -- and the whole note is asserted, because
    # what it must not say has more spellings than any list of banned ones.
    assert note == (
        "this turn totals 203 where its own prompt and completion columns add to 200, "
        "so the three numbers here are not one measurement"
    )


async def test_the_review_rollup_counts_turns_the_same_way(store, tmp_path):
    """The same cancelling pair, through the query that keys spend by reviewer.

    Three queries carry this expression and the fixture above proves one of them. This
    is where a reader compares one reviewer against another, so a revert here to
    comparing the summed columns would print a pair of totals that agree, over two rows
    that both disagree with themselves.
    """
    consultation_id = await new_consultation(store)
    review_id = uuid4()
    reviews = ReviewStore(store)
    await reviews.create_review(
        review_id=review_id,
        mode="standard",
        goal="g",
        context=None,
        material=[],
        material_sha256="a" * 64,
        raw_sha256="b" * 64,
        reviewer_snapshot=[],
        confirm_token="token",
        secret_hits=[],
        web_requested=False,
    )
    await reviews.record_reviewer_result(
        review_id, "codex-sol", "ok", consultation_id=consultation_id
    )
    legacy_turn(tmp_path, consultation_id, sequence_number=1, tokens=(100, 100, 203))
    legacy_turn(tmp_path, consultation_id, sequence_number=2, tokens=(100, 100, 197))

    usage = (await store.review_usage(str(review_id)))["codex-sol"].usage
    assert usage.prompt_tokens + usage.completion_tokens == usage.total_tokens
    _legacy, note = usage.counts_incomplete
    assert "2 of these 2 turns total something other than their own prompt and " in note


async def test_the_workflow_rollup_counts_turns_the_same_way(store, tmp_path):
    """And through the third, which keys the same ledger by step.

    Reached by the direct link -- a delegated step owning its consultation -- because
    the reviewer path into this query is the one the test above already covers, and
    what is being pinned here is the expression, not which join found the row.
    """
    consultation_id = await new_consultation(store, workflow_id="wf-1", step_id="step-1")
    legacy_turn(tmp_path, consultation_id, sequence_number=1, tokens=(100, 100, 203))
    legacy_turn(tmp_path, consultation_id, sequence_number=2, tokens=(100, 100, 197))

    usage = (await store.workflow_usage("wf-1"))["step-1"].usage
    assert usage.prompt_tokens + usage.completion_tokens == usage.total_tokens
    _legacy, note = usage.counts_incomplete
    assert "2 of these 2 turns total something other than their own prompt and " in note


async def test_a_review_never_lends_this_workflow_another_ones_step(store):
    """The step key is read from whichever link put the row in *this* workflow.

    A consultation can reach a workflow two ways, and a workflow can borrow a
    reviewer's consultation that belongs elsewhere. Here `c` is workflow A's, at step
    `a-7`, and it lands in workflow B's rollup through a review that names no step of
    its own. Falling back to `c.step_id` there charges half of A's step to B -- and
    this number is money, so it is charged to nobody instead. A's own rollup is
    unaffected: there the direct link is the qualifying one.
    """
    consultation_id = await new_consultation(store, workflow_id="wf-a", step_id="a-7")
    review_id = uuid4()
    reviews = ReviewStore(store)
    await reviews.create_review(
        review_id=review_id,
        mode="standard",
        goal="g",
        context=None,
        material=[],
        material_sha256="a" * 64,
        raw_sha256="b" * 64,
        reviewer_snapshot=[],
        confirm_token="token",
        secret_hits=[],
        web_requested=False,
        workflow_id="wf-b",
    )
    await reviews.record_reviewer_result(
        review_id, "codex-sol", "ok", consultation_id=consultation_id
    )
    await store.record_turn(
        consultation_id, 1, SourceMode.DOCUMENT,
        user_prompt="q", context=None, compiled_prompt="p",
        input_tokens=1800, output_tokens=200,
    )

    # Kept, because B did spend it -- just not at a step B ever ran.
    assert list(await store.workflow_usage("wf-b")) == ["_unattributed"]
    assert (await store.workflow_usage("wf-b"))["_unattributed"].usage.total_tokens == 2000
    assert list(await store.workflow_usage("wf-a")) == ["a-7"]


async def test_the_ledger_refuses_a_count_it_would_have_to_invent(store):
    """The one writer of the three token columns, so the only place to stop this.

    The annotations enforce nothing. `"1"` and `"2"` derive `"12"` in Python, SQLite
    coerces all three to integers, and what lands on disk is a row contradicting its
    own columns under the marker asserting it was counted the current way -- which the
    rollup can only report as the ledger's fault, sending whoever reads it to the
    wrong place entirely. Refused rather than coerced: a repaired count is one nobody
    measured, and this column is the record of what nobody measured.
    """
    consultation_id = await new_consultation(store)
    body = dict(user_prompt="q", context=None, compiled_prompt="p")

    with pytest.raises(TypeError):
        await store.record_turn(
            consultation_id, 1, SourceMode.DOCUMENT,
            input_tokens="1", output_tokens="2", **body,
        )
    # `bool` is an `int` and `True + True` is a 2 that reads like a measurement, which
    # is why the check is on the exact type rather than on `isinstance`.
    with pytest.raises(TypeError):
        await store.record_turn(
            consultation_id, 1, SourceMode.DOCUMENT,
            input_tokens=True, output_tokens=True, **body,
        )
    with pytest.raises(ValueError):
        await store.record_turn(
            consultation_id, 1, SourceMode.DOCUMENT,
            input_tokens=-1, output_tokens=0, **body,
        )

    # Nothing partial written by any of the three: `usage` is None at zero turns.
    assert await store.usage(consultation_id) is None


async def test_the_unattributed_bucket_holds_a_null_step_and_nothing_else(store):
    """Two rows, two keys, because they are two different answers.

    A falsy test folds together everything SQLite can return that is not a step: NULL,
    which is a row that named none, and an empty string, which is a row that named one
    badly. Under one dictionary key the second `Spend` replaces the first and that
    money leaves the page. Unreachable while every step id is a server-generated UUID
    -- and the line still has to ask the question it means, which is whether the
    database returned NULL.
    """
    blank = await new_consultation(store, workflow_id="wf", step_id="")
    stepless = await new_consultation(store, workflow_id="wf")
    for consultation_id, tokens in ((blank, 1000), (stepless, 2000)):
        await store.record_turn(
            consultation_id, 1, SourceMode.DOCUMENT,
            user_prompt="q", context=None, compiled_prompt="p",
            input_tokens=tokens, output_tokens=0,
        )

    spend = await store.workflow_usage("wf")
    assert spend[""].usage.total_tokens == 1000
    assert spend["_unattributed"].usage.total_tokens == 2000


async def test_the_caveat_list_does_not_depend_on_what_order_sqlite_felt_like(store):
    """`GROUP_CONCAT` has no defined input order and `tallied` keeps the order it gets.

    Between those two, the same unchanged rows can return the same caveats in a
    different order on two reads -- the kind of wrong that looks like a page glitch and
    gets refreshed away rather than reported. Sorted where the concatenation is
    consumed, so there is one order and it is a property of the notes rather than of
    the scan. First-appearance order across turns was never meaningful; the rows are
    inserted here in the order that makes the two disagree.
    """
    consultation_id = await new_consultation(store)
    for sequence, note in ((1, "zebra is not a token count; counting it as 0"),
                           (2, "alpha is not a token count; counting it as 0")):
        await store.record_turn(
            consultation_id, sequence, SourceMode.DOCUMENT,
            user_prompt="q", context=None, compiled_prompt="p",
            counts_incomplete=[note],
        )

    assert (await store.usage(consultation_id)).counts_incomplete == [
        "alpha is not a token count; counting it as 0",
        "zebra is not a token count; counting it as 0",
    ]


async def test_a_rollup_counted_one_way_carries_no_caveat(store):
    """The healthy ledger, which is every ledger written since. Each turn derives its
    total from its parts, so a sum of them cannot drift, and nothing is said."""
    consultation_id = await new_consultation(store)
    for sequence in (1, 2):
        await store.record_turn(
            consultation_id, sequence, SourceMode.DOCUMENT,
            user_prompt="q", context=None, compiled_prompt="p",
            input_tokens=1800, output_tokens=200,
        )

    usage = await store.usage(consultation_id)
    assert usage.counts_incomplete == []


async def test_a_substituted_count_survives_the_restart_that_rebuilds_it(store):
    """The half of the field that had to become durable.

    A live parse tells whoever received the answer. A review reopened tomorrow is
    rebuilt from these rows instead, and there an invented zero is indistinguishable
    from a measured one -- the exact failure the caveat exists to stop, arriving by a
    different door. So the reason is written beside the turn and gathered back up.
    """
    consultation_id = await new_consultation(store)
    note = "'N/A' is not a token count; counting it as 0"
    for sequence in (1, 2):
        await store.record_turn(
            consultation_id, sequence, SourceMode.DOCUMENT,
            user_prompt="q", context=None, compiled_prompt="p",
            counts_incomplete=[note],
        )

    usage = await store.usage(consultation_id)
    # Counted, not merely collapsed: both turns hit it, and that is the difference
    # between one runtime hiccup and a runtime that has stopped reporting.
    assert usage.counts_incomplete == [f"{note} (x2)"]


def legacy_turn(tmp_path, consultation_id, sequence_number, tokens=(22, 305704, 1744808)):
    """One turn inserted the way the write path did before `usage_semantics` existed.

    The column list is the point: every row already on disk was written by a statement
    that did not name it, which is what the `DEFAULT 0` is for. The default triple is a
    real Claude turn off the measured ledger; a caller passes its own when the residual
    itself is what the test is about.
    """
    scratch = sqlite3.connect(tmp_path / "nested" / "consultations.sqlite3")
    try:
        scratch.execute(
            "INSERT INTO consultation_turns (consultation_id, sequence_number, source_mode, "
            "user_prompt, compiled_prompt, input_tokens, output_tokens, total_tokens, "
            "latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(consultation_id), sequence_number, SourceMode.DOCUMENT.value,
             "q", "p", *tokens, 0, "x"),
        )
        scratch.commit()
    finally:
        scratch.close()


def usage_semantics_rows(tmp_path):
    """The column for every stored turn, read past the store's own reader.

    `turns()` does not select it: nothing in the server reads the marker yet, and a
    field added to `Turn` for a test to assert on would be a consumer this change does
    not have. The column has to be on disk from the first row regardless -- it is the
    one thing about an old row that cannot be reconstructed after the fact.
    """
    scratch = sqlite3.connect(tmp_path / "nested" / "consultations.sqlite3")
    try:
        return [row[0] for row in scratch.execute(
            "SELECT usage_semantics FROM consultation_turns ORDER BY sequence_number"
        )]
    finally:
        scratch.close()


async def test_a_failed_turn_keeps_its_code_and_no_answer(store):
    consultation_id = await new_consultation(store)
    await store.record_turn(
        consultation_id,
        1,
        SourceMode.MODEL,
        user_prompt="q",
        context=None,
        compiled_prompt="...",
        error_code=ConsultErrorCode.TIMEOUT,
    )
    (turn,) = await store.turns(consultation_id)
    assert turn.error_code == "timeout"
    assert turn.validated_response_json is None


async def test_store_full_content_false_keeps_the_shape_and_drops_the_bodies(tmp_path):
    store = await ConsultStore(tmp_path / "db.sqlite3", store_full_content=False).open()
    consultation_id = await new_consultation(store)
    await store.record_turn(
        consultation_id,
        1,
        SourceMode.DOCUMENT,
        # A marker no schema identifier could ever contain: the scan below reads the
        # whole file, and `sqlite_master` holds the DDL, so a word like "secret" would
        # match a *column name* and pass or fail for the wrong reason.
        user_prompt="pelican question",
        context="pelican document",
        compiled_prompt="pelican prompt",
        raw_output="pelican answer",
        validated_response={"answer": "pelican answer"},
        input_tokens=7,
        latency_ms=99,
    )
    (turn,) = await store.turns(consultation_id)
    await store.close()

    assert (turn.user_prompt, turn.context, turn.compiled_prompt) == (None, None, None)
    assert (turn.raw_output, turn.validated_response_json) == (None, None)
    assert (turn.input_tokens, turn.latency_ms) == (7, 99)
    assert b"pelican" not in (tmp_path / "db.sqlite3").read_bytes()


async def test_the_second_turn_cannot_reuse_a_sequence_number(store):
    consultation_id = await new_consultation(store)
    args = (consultation_id, 1, SourceMode.MODEL)
    kwargs = {"user_prompt": "q", "context": None, "compiled_prompt": "p"}
    await store.record_turn(*args, **kwargs)
    with pytest.raises(Exception):
        await store.record_turn(*args, **kwargs)


# --- diagnostics ------------------------------------------------------------


async def test_the_routing_decision_records_the_losers(store):
    consultation_id = await new_consultation(store)
    await store.record_routing(
        consultation_id,
        RoutingDecision(
            capability="research",
            route=ROUTE,
            excluded=[ExcludedCandidate("claude-opus", "host runtime"), ExcludedCandidate("x", "disabled")],
        ),
    )
    (decision,) = await store.routing_for(consultation_id)
    assert decision["selected_agent"] == "codex-sol"
    assert decision["explicit"] == 0
    assert {e["agent_id"] for e in decision["excluded"]} == {"claude-opus", "x"}


async def test_a_failed_route_is_recorded_too(store):
    consultation_id = await new_consultation(store)
    await store.record_routing(
        consultation_id,
        RoutingDecision(
            capability="coding",
            excluded=[ExcludedCandidate("codex-sol", "disabled")],
            error=(ConsultErrorCode.NO_AGENT_AVAILABLE, "nothing eligible"),
        ),
    )
    (decision,) = await store.routing_for(consultation_id)
    assert (decision["selected_agent"], decision["error_code"]) == (None, "no_agent_available")


async def test_a_status_check_stores_a_verdict_not_an_auth_transcript(store):
    """`detail` is ours to write. No login output has an argument to arrive through."""
    await store.record_status_check("codex-sol", installed=True, authenticated=False, detail="not on PATH")
    row = store._db.execute("SELECT * FROM agent_status_checks").fetchone()
    assert (row["installed"], row["authenticated"], row["detail"]) == (1, 0, "not on PATH")

    columns = {c[1] for c in store._db.execute("PRAGMA table_info(agent_status_checks)")}
    assert not columns & {"env", "environment", "token", "credentials", "output"}


async def test_a_status_detail_that_quotes_a_credential_is_scrubbed_at_the_insert(store):
    """No adapter quotes stderr into an `AdapterError` today. One that starts to
    should not be the thing standing between a credential and this column."""
    secret = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"
    await store.record_status_check(
        "codex-sol", installed=False, authenticated=False,
        detail=f"could not start `codex --key {secret}`",
    )
    detail = store._db.execute("SELECT detail FROM agent_status_checks").fetchone()["detail"]
    assert secret not in detail and "[redacted]" in detail


# --- leases -----------------------------------------------------------------


async def test_a_lease_is_released_even_when_the_turn_fails(store):
    consultation_id = await new_consultation(store)
    with pytest.raises(RuntimeError):
        async with store.lease(consultation_id):
            raise RuntimeError("the CLI blew up")

    async with store.lease(consultation_id):  # would raise SESSION_BUSY if leaked
        pass


async def test_an_expired_lease_does_not_wedge_the_consultation(store):
    consultation_id = await new_consultation(store)
    store._db.execute(
        "INSERT INTO consultation_leases (consultation_id, holder, expires_at) VALUES (?,?,?)",
        (str(consultation_id), "pid-dead", 1.0),  # 1970, and the holder is gone
    )
    async with store.lease(consultation_id):
        pass


async def test_a_live_consultation_lease_is_renewed_past_its_initial_expiry(store):
    consultation_id = await new_consultation(store)
    async with store.lease(consultation_id, ttl_s=0.06):
        initial = store._db.execute(
            "SELECT expires_at FROM consultation_leases WHERE consultation_id = ?",
            (str(consultation_id),),
        ).fetchone()[0]
        await asyncio.sleep(0.08)
        renewed = store._db.execute(
            "SELECT expires_at FROM consultation_leases WHERE consultation_id = ?",
            (str(consultation_id),),
        ).fetchone()[0]
        assert renewed > initial and renewed > time.time()


async def test_losing_a_consultation_lease_interrupts_the_guarded_turn(
    store, monkeypatch
):
    consultation_id = await new_consultation(store)

    def lost(*_args):
        raise StoreError(ConsultErrorCode.SESSION_BUSY, "lease was stolen")

    monkeypatch.setattr(store, "_renew", lost)
    started = time.perf_counter()
    with pytest.raises(StoreError, match="lease was stolen"):
        async with store.lease(consultation_id, ttl_s=0.03):
            await asyncio.sleep(1)
    assert time.perf_counter() - started < 0.5


async def test_a_body_error_is_not_replaced_by_the_heartbeat(store, monkeypatch):
    consultation_id = await new_consultation(store)

    def lost(*_args):
        raise StoreError(ConsultErrorCode.SESSION_BUSY, "lease was stolen")

    monkeypatch.setattr(store, "_renew", lost)
    with pytest.raises(RuntimeError, match="body failed"):
        async with store.lease(consultation_id, ttl_s=0.03):
            raise RuntimeError("body failed")


async def test_a_body_error_is_not_replaced_by_a_cleanup_failure(store, monkeypatch):
    consultation_id = await new_consultation(store)

    def release_failed(*_args):
        raise OSError("database stayed locked")

    monkeypatch.setattr(store, "_release", release_failed)

    with pytest.raises(RuntimeError, match="body failed"):
        async with store.lease(consultation_id):
            raise RuntimeError("body failed")


async def test_a_cleanup_failure_after_success_is_still_reported(store, monkeypatch):
    consultation_id = await new_consultation(store)

    def release_failed(*_args):
        raise OSError("database stayed locked")

    monkeypatch.setattr(store, "_release", release_failed)

    with pytest.raises(OSError, match="database stayed locked"):
        async with store.lease(consultation_id):
            pass


async def test_heartbeat_cancellation_is_removed_from_the_owner_count(store, monkeypatch):
    consultation_id = await new_consultation(store)

    def lost(*_args):
        raise StoreError(ConsultErrorCode.SESSION_BUSY, "lease was stolen")

    monkeypatch.setattr(store, "_renew", lost)

    async def operation() -> int:
        owner = asyncio.current_task()
        assert owner is not None
        before = owner.cancelling()
        with pytest.raises(StoreError, match="lease was stolen"):
            async with store.lease(consultation_id, ttl_s=0.03):
                await asyncio.sleep(1)
        return owner.cancelling() - before

    assert await asyncio.create_task(operation()) == 0


async def test_swallowed_heartbeat_cancellation_is_removed_from_the_owner_count(
    store, monkeypatch
):
    consultation_id = await new_consultation(store)

    def lost(*_args):
        raise StoreError(ConsultErrorCode.SESSION_BUSY, "lease was stolen")

    monkeypatch.setattr(store, "_renew", lost)

    async def operation() -> int:
        owner = asyncio.current_task()
        assert owner is not None
        before = owner.cancelling()
        with pytest.raises(StoreError, match="lease was stolen"):
            async with store.lease(consultation_id, ttl_s=0.03):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    pass
        return owner.cancelling() - before

    assert await asyncio.create_task(operation()) == 0


async def test_a_renewal_failure_at_body_completion_preserves_success_and_releases(
    store, monkeypatch
):
    consultation_id = await new_consultation(store)
    renewal_entered = threading.Event()
    finish_renewal = threading.Event()
    body_returned = asyncio.Event()

    def lost_after_body(*_args):
        renewal_entered.set()
        assert finish_renewal.wait(timeout=1)
        raise StoreError(ConsultErrorCode.SESSION_BUSY, "lease was stolen")

    monkeypatch.setattr(store, "_renew", lost_after_body)

    async def operation():
        async with store.lease(consultation_id, ttl_s=0.03):
            assert await asyncio.to_thread(renewal_entered.wait, 1)
            body_returned.set()
        return "completed"

    task = asyncio.create_task(operation())
    await body_returned.wait()
    # Let the context manager observe the completed body and begin its shielded
    # cleanup before the in-flight renewal reports the stale owner.
    await asyncio.sleep(0)
    finish_renewal.set()

    assert await task == "completed"
    row = store._db.execute(
        "SELECT 1 FROM consultation_leases WHERE consultation_id = ?",
        (str(consultation_id),),
    ).fetchone()
    assert row is None


async def test_external_cancellation_propagates_after_releasing_the_lease(store):
    consultation_id = await new_consultation(store)
    entered = asyncio.Event()

    async def operation():
        async with store.lease(consultation_id):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(operation())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = store._db.execute(
        "SELECT 1 FROM consultation_leases WHERE consultation_id = ?",
        (str(consultation_id),),
    ).fetchone()
    assert row is None


async def test_external_cancellation_during_cleanup_replaces_a_body_error(
    store, monkeypatch
):
    consultation_id = await new_consultation(store)
    release_entered = threading.Event()
    finish_release = threading.Event()

    def blocked_release(*_args):
        release_entered.set()
        assert finish_release.wait(timeout=1)

    monkeypatch.setattr(store, "_release", blocked_release)

    async def operation():
        async with store.lease(consultation_id):
            raise RuntimeError("body failed")

    task = asyncio.create_task(operation())
    assert await asyncio.to_thread(release_entered.wait, 1)
    task.cancel()
    finish_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_later_external_cancellation_is_not_mistaken_for_heartbeat_cancel(
    store, monkeypatch
):
    consultation_id = await new_consultation(store)
    release_entered = threading.Event()
    finish_release = threading.Event()

    def lost(*_args):
        raise StoreError(ConsultErrorCode.SESSION_BUSY, "lease was stolen")

    def blocked_release(*_args):
        release_entered.set()
        assert finish_release.wait(timeout=1)

    monkeypatch.setattr(store, "_renew", lost)
    monkeypatch.setattr(store, "_release", blocked_release)

    async def operation():
        async with store.lease(consultation_id, ttl_s=0.03):
            await asyncio.sleep(1)

    task = asyncio.create_task(operation())
    assert await asyncio.to_thread(release_entered.wait, 1)
    task.cancel()
    finish_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


CONTENDER = """\
import asyncio, sys
from orchestrator_mcp.consult.store import ConsultStore, StoreError

async def main(path, consultation_id):
    store = await ConsultStore(path).open()
    try:
        async with store.lease(consultation_id):
            print("acquired")
    except StoreError as exc:
        print(exc.code.value)
    await store.close()

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


async def contend(store, consultation_id) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(CONTENDER), str(store.path), str(consultation_id)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


async def test_two_processes_cannot_advance_one_session(store):
    """Two OS processes, not two tasks: an in-process lock would not catch this."""
    consultation_id = await new_consultation(store)
    async with store.lease(consultation_id):
        assert await contend(store, consultation_id) == "session_busy"

    assert await contend(store, consultation_id) == "acquired"


async def test_a_second_process_may_hold_a_different_consultation(store):
    held = await new_consultation(store)
    other = await new_consultation(store)
    async with store.lease(held):
        assert await contend(store, other) == "acquired"
