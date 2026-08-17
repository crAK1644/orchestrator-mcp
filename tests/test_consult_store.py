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
from orchestrator_mcp.consult.store import MIGRATIONS, ConsultStore, StoreError
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
    await store.record_turn(
        consultation_id,
        1,
        SourceMode.MODEL,
        "q1",
        None,
        "compiled",
        input_tokens=10,
        output_tokens=2,
        total_tokens=15,
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
        total_tokens=30,
        cost_usd=0.3,
    )

    usage = await store.usage(consultation_id)
    assert usage.model_dump() == {
        "prompt_tokens": 30,
        "completion_tokens": 6,
        "total_tokens": 45,
        "cost_usd": 0.5,
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
