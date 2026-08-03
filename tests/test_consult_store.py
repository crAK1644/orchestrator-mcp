"""Persistence: permissions, session binding, and cross-process leases."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from uuid import uuid4

import pytest

from orchestrator_mcp.consult.contract import ConsultRoute, SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.routing import ExcludedCandidate, RoutingDecision
from orchestrator_mcp.consult.store import ConsultStore, StoreError

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
    """The file holds every prompt and every answer verbatim."""
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700


async def test_opening_twice_is_not_a_second_migration(tmp_path):
    path = tmp_path / "db.sqlite3"
    first = await ConsultStore(path).open()
    await first.close()
    second = await ConsultStore(path).open()
    versions = second._db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    profiles = second._db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    await second.close()
    assert (versions, profiles) == (1, 1)


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
    assert counts == (1, 1)


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
        user_prompt="secret question",
        context="secret document",
        compiled_prompt="secret prompt",
        raw_output="secret answer",
        validated_response={"answer": "secret answer"},
        input_tokens=7,
        latency_ms=99,
    )
    (turn,) = await store.turns(consultation_id)
    await store.close()

    assert (turn.user_prompt, turn.context, turn.compiled_prompt) == (None, None, None)
    assert (turn.raw_output, turn.validated_response_json) == (None, None)
    assert (turn.input_tokens, turn.latency_ms) == (7, 99)
    assert b"secret" not in (tmp_path / "db.sqlite3").read_bytes()


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
