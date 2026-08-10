"""Local record of every consultation.

Stdlib `sqlite3` behind `asyncio.to_thread` rather than `aiosqlite`: a turn writes
a handful of short statements around a subprocess call that takes seconds, so the
driver is never the thing waiting, and this keeps the dependency list where it is.

Two things this file is careful about. Everything a consultation sent and received
is stored in full by default, which makes the database as sensitive as the prompts
themselves -- hence `0700` on the directory and `0600` on the file, set before
anything is written into it. And nothing from a subprocess environment or from an
authentication command has a column to land in; the schema is the enforcement.

Concurrency is cross-process, not just cross-task: two MCP servers can be pointed
at one database, and both could try to advance the same native CLI session. The
lease table is what makes the second one wait, so it is taken in a transaction
SQLite itself serializes rather than in an `asyncio.Lock` that only one process
would honour.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from ..contract import Usage, scrub_json
from .contract import ConsultRoute, SourceMode
from .errors import ConsultErrorCode
from .routing import RoutingDecision

# Applied in order, by index. Append only -- an edit to a shipped migration is a
# database that disagrees with itself depending on when it was created.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE profiles (
        id          TEXT PRIMARY KEY,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE consultations (
        id                 TEXT PRIMARY KEY,
        profile_id         TEXT NOT NULL REFERENCES profiles(id),
        origin_runtime     TEXT NOT NULL,
        target_agent_id    TEXT NOT NULL,
        target_runtime     TEXT NOT NULL,
        target_model       TEXT NOT NULL,
        capability         TEXT NOT NULL,
        native_session_id  TEXT,
        conversation_label TEXT,
        protocol_version   TEXT NOT NULL,
        config_hash        TEXT NOT NULL,
        status             TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    );

    CREATE TABLE consultation_turns (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        consultation_id         TEXT NOT NULL REFERENCES consultations(id),
        sequence_number         INTEGER NOT NULL,
        source_mode             TEXT NOT NULL,
        user_prompt             TEXT,
        context                 TEXT,
        compiled_prompt         TEXT,
        raw_output              TEXT,
        validated_response_json TEXT,
        input_tokens            INTEGER NOT NULL DEFAULT 0,
        output_tokens           INTEGER NOT NULL DEFAULT 0,
        cost_usd                REAL,
        latency_ms              INTEGER NOT NULL DEFAULT 0,
        error_code              TEXT,
        created_at              TEXT NOT NULL,
        UNIQUE (consultation_id, sequence_number)
    );

    CREATE TABLE routing_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        consultation_id TEXT NOT NULL REFERENCES consultations(id),
        capability      TEXT NOT NULL,
        selected_agent  TEXT,
        explicit        INTEGER NOT NULL DEFAULT 0,
        excluded_json   TEXT NOT NULL,
        error_code      TEXT,
        created_at      TEXT NOT NULL
    );

    CREATE TABLE agent_status_checks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id      TEXT NOT NULL,
        installed     INTEGER,
        authenticated INTEGER,
        detail        TEXT,
        checked_at    TEXT NOT NULL
    );

    CREATE TABLE consultation_leases (
        consultation_id TEXT PRIMARY KEY,
        holder          TEXT NOT NULL,
        expires_at      REAL NOT NULL
    );
    """,
    """
    CREATE TABLE reviews (
        id                     TEXT PRIMARY KEY,
        parent_review_id       TEXT REFERENCES reviews(id) DEFERRABLE INITIALLY DEFERRED,
        mode                   TEXT NOT NULL,
        status                 TEXT NOT NULL,
        outcome                TEXT,
        goal                   TEXT,
        context                TEXT,
        material_json          TEXT NOT NULL,
        material_sha256        TEXT NOT NULL,
        raw_sha256             TEXT NOT NULL,
        reviewer_snapshot_json TEXT NOT NULL,
        confirm_token_sha      TEXT,
        secret_hits_json       TEXT NOT NULL,
        web_requested          INTEGER NOT NULL DEFAULT 0,
        host_findings_json     TEXT,
        summary_json           TEXT,
        fix_rounds_json        TEXT,
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL
    );

    CREATE TABLE review_consultations (
        review_id       TEXT NOT NULL REFERENCES reviews(id),
        agent_id        TEXT NOT NULL,
        consultation_id TEXT REFERENCES consultations(id),
        status          TEXT NOT NULL,
        findings_json   TEXT,
        answer          TEXT,
        error_code      TEXT,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (review_id, agent_id)
    );

    CREATE TABLE review_leases (
        review_id  TEXT PRIMARY KEY,
        holder     TEXT NOT NULL,
        expires_at REAL NOT NULL
    );

    CREATE TABLE review_delete_confirmations (
        token_sha       TEXT PRIMARY KEY,
        review_ids_json TEXT NOT NULL,
        displayed_count INTEGER NOT NULL,
        created_at      TEXT NOT NULL,
        expires_at      REAL NOT NULL
    );
    """,
    """
    ALTER TABLE reviews ADD COLUMN secrets_mode TEXT;
    ALTER TABLE review_consultations ADD COLUMN findings_parsed INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE review_consultations ADD COLUMN findings_truncated INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX reviews_parent_review_id_idx ON reviews(parent_review_id);
    CREATE INDEX review_consultations_consultation_id_idx
        ON review_consultations(consultation_id);
    """,
    """
    ALTER TABLE review_consultations ADD COLUMN sources_json TEXT;
    """,
    """
    ALTER TABLE consultation_turns ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0;
    UPDATE consultation_turns SET total_tokens = input_tokens + output_tokens;

    CREATE TABLE IF NOT EXISTS consultation_delete_confirmations (
        token_sha               TEXT PRIMARY KEY,
        consultation_ids_json  TEXT NOT NULL,
        displayed_count         INTEGER NOT NULL,
        created_at              TEXT NOT NULL,
        expires_at              REAL NOT NULL
    );
    """,
    """
    CREATE TABLE workflow_runs (
        id              TEXT PRIMARY KEY,
        goal            TEXT NOT NULL,
        workdir         TEXT NOT NULL,
        host_runtime    TEXT NOT NULL,
        host_model      TEXT,
        status          TEXT NOT NULL,
        bindings_json   TEXT NOT NULL,
        policy_json     TEXT NOT NULL,
        baseline_commit TEXT,
        result_commit   TEXT,
        fix_rounds      INTEGER NOT NULL DEFAULT 0,
        reason          TEXT,
        workflow_hash   TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    CREATE TABLE workflow_steps (
        id                  TEXT PRIMARY KEY,
        workflow_id         TEXT NOT NULL REFERENCES workflow_runs(id),
        step                TEXT NOT NULL,
        executor            TEXT NOT NULL,
        execution_mode      TEXT,
        repository_access   TEXT NOT NULL,
        round_index         INTEGER NOT NULL DEFAULT 0,
        attempt             INTEGER NOT NULL DEFAULT 1,
        sequence            INTEGER NOT NULL,
        parent_step_id      TEXT,
        agent_id            TEXT,
        agent_snapshot_json TEXT NOT NULL,
        status              TEXT NOT NULL,
        confirm_token_sha   TEXT,
        review_id           TEXT,
        output_json         TEXT,
        raw_patch_sha256    TEXT,
        reported_by         TEXT,
        lease_holder        TEXT,
        lease_expires_at    REAL,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    );

    CREATE INDEX workflow_steps_workflow_id_idx ON workflow_steps(workflow_id);

    ALTER TABLE consultations ADD COLUMN workflow_id TEXT;
    ALTER TABLE consultations ADD COLUMN step_id TEXT;
    ALTER TABLE reviews ADD COLUMN workflow_id TEXT;
    ALTER TABLE reviews ADD COLUMN step_id TEXT;
    """,
    """
    ALTER TABLE workflow_runs ADD COLUMN replan_token_sha TEXT;
    ALTER TABLE workflow_runs ADD COLUMN replan_bindings_json TEXT;
    """,
]

DEFAULT_PROFILE = "default"
LEASE_TTL_S = 300.0
DELETE_CONFIRM_TTL_S = 300.0

_T = TypeVar("_T")


class StoreError(Exception):
    """A refusal with a code the caller's envelope can carry."""

    def __init__(self, code: ConsultErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Consultation:
    id: str
    profile_id: str
    origin_runtime: str
    target_agent_id: str
    target_runtime: str
    target_model: str
    capability: str
    native_session_id: str | None
    conversation_label: str | None
    protocol_version: str
    config_hash: str
    status: str
    created_at: str
    updated_at: str
    # Set when a workflow step created this consultation. Its presence is what
    # `ConsultService._bind_public` refuses on: the workflow path relaxes the host
    # exclusion from runtime to execution identity, and a public resume must not be
    # able to reach a row that was bound under the relaxed rule.
    workflow_id: str | None = None
    step_id: str | None = None


@dataclass(frozen=True)
class Turn:
    sequence_number: int
    source_mode: str
    user_prompt: str | None
    context: str | None
    compiled_prompt: str | None
    raw_output: str | None
    validated_response_json: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None
    latency_ms: int
    error_code: str | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _token_sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _set_wal(connection: sqlite3.Connection, deadline_s: float = 5.0) -> None:
    """Switch to WAL, waiting out another process that is mid-switch.

    Retried by hand rather than left to `busy_timeout`: converting the journal mode
    needs a lock SQLite does not run the busy handler for, so two processes opening
    one fresh database at the same moment leave the second with an immediate
    "database is locked" and an exception out of `open()` instead of an envelope.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


class ConsultStore:
    """Every method is async and does its SQLite work in a worker thread."""

    def __init__(self, path: Path, store_full_content: bool = True) -> None:
        self.path = Path(path)
        self.store_full_content = store_full_content
        self._connection: sqlite3.Connection | None = None
        # One connection shared by every worker thread, so one lock decides who is
        # using it. Without this, two `to_thread` workers can interleave inside a
        # `BEGIN IMMEDIATE` -- SQLite raises "cannot start a transaction within a
        # transaction", and worse, an unrelated write lands inside someone else's
        # open transaction and rolls back with it.
        self._lock = threading.RLock()
        self._open_lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------------

    async def open(self) -> ConsultStore:
        # Idempotent *and* concurrency-safe: the MCP tools open on first use, so two
        # simultaneous first calls both arrive here with no connection, and two
        # threads racing to create the schema is how one of them gets
        # "database is locked" instead of an envelope.
        if self._connection is None:
            async with self._open_lock:
                if self._connection is None:
                    # Published here rather than by the worker, because cancelling
                    # this `await` releases the lock without stopping the thread.
                    # A worker that assigned the field itself would land on a store
                    # someone else has since opened -- or closed -- and reopen it
                    # from the outside. Cancelled, the connection it built is simply
                    # never taken, and goes when the discarded result does.
                    self._connection = await asyncio.to_thread(self._open)
        return self

    def _open(self) -> sqlite3.Connection:
        # `0700` before the file exists, so there is no window where a fresh
        # database is world-readable.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.path.parent.stat()
        directory_mode = stat.S_IMODE(directory.st_mode)
        if directory.st_uid != os.getuid() or directory_mode != 0o700:
            raise StoreError(
                ConsultErrorCode.TRANSPORT_ERROR,
                f"database directory `{self.path.parent}` must be owned by this user with "
                f"permissions 0700 (found {directory_mode:04o})",
            )
        connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        # Every open, not only the first: a database that was already there with
        # looser permissions is exactly the one worth tightening.
        os.chmod(self.path, 0o600)
        connection.row_factory = sqlite3.Row
        # First, so that every statement below waits for another process instead of
        # failing on it. Long enough to outlast a short write, short enough that a
        # wedged one surfaces as an error rather than hanging the consultation.
        connection.execute("PRAGMA busy_timeout=5000")
        # WAL so a reader (the dashboard) never blocks the writer mid-consultation.
        _set_wal(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        # Returned rather than assigned, and only once the schema is finished --
        # `_migrate` takes the connection for that reason. Anything visible before
        # this line is a connection every later call runs against a schema that was
        # never finished, since `open()` skips its work whenever `_connection` is set.
        try:
            self._migrate(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _migrate(self, db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        for version, statements in enumerate(MIGRATIONS):
            if version in applied:
                continue
            db.execute("BEGIN IMMEDIATE")
            try:
                # Read again, now that the write lock is held. The check above ran
                # before it, so two processes opening one database at the same
                # moment both see an unapplied migration; the second one waits here,
                # takes the lock after the first commits, and would otherwise run a
                # CREATE TABLE against the schema the first just created.
                if db.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone():
                    db.execute("COMMIT")
                    continue
                # Not `executescript`, which commits any open transaction before it
                # runs: the schema and the row saying it was applied have to land
                # together, or a half-applied migration re-runs into a CREATE that
                # already exists. Splitting on `;` is safe for these -- no statement
                # here contains one inside a literal.
                for statement in filter(str.strip, statements.split(";")):
                    try:
                        db.execute(statement)
                    except sqlite3.OperationalError as error:
                        # A manually repaired or restored database can contain a
                        # column whose migration-ledger row is missing. SQLite has
                        # no portable `ADD COLUMN IF NOT EXISTS`, so make only that
                        # operation idempotent and let every other schema error fail.
                        is_duplicate_add = (
                            statement.lstrip().upper().startswith("ALTER TABLE ")
                            and " ADD COLUMN " in statement.upper()
                            and "duplicate column name" in str(error).lower()
                        )
                        if not is_duplicate_add:
                            raise
                db.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )
                db.execute(
                    "INSERT OR IGNORE INTO profiles (id, created_at) VALUES (?, ?)",
                    (DEFAULT_PROFILE, _now()),
                )
            except Exception:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    async def close(self) -> None:
        if self._connection is not None:
            connection, self._connection = self._connection, None
            await asyncio.to_thread(connection.close)

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StoreError(ConsultErrorCode.TRANSPORT_ERROR, "consultation store is not open")
        return self._connection

    async def _run(self, work: Callable[[], _T]) -> _T:
        """Run one unit of SQLite work in a thread, alone on the connection.

        Every database access in this class goes through here. The lock is held for
        the whole unit rather than per statement, because the units that matter are
        transactions: a `BEGIN IMMEDIATE` that another thread can slip a statement
        into is not a transaction.
        """

        def guarded() -> _T:
            with self._lock:
                return work()

        return await asyncio.to_thread(guarded)

    # --- consultations ------------------------------------------------------

    async def create_consultation(
        self,
        consultation_id: UUID,
        origin_runtime: str,
        route: ConsultRoute,
        capability: str,
        protocol_version: str,
        config_hash: str,
        conversation_label: str | None = None,
        profile_id: str = DEFAULT_PROFILE,
        workflow_id: str | None = None,
        step_id: str | None = None,
    ) -> Consultation:
        def work() -> Consultation:
            now = _now()
            self._db.execute(
                "INSERT INTO consultations (id, profile_id, origin_runtime, target_agent_id, "
                "target_runtime, target_model, capability, native_session_id, conversation_label, "
                "protocol_version, config_hash, status, created_at, updated_at, "
                "workflow_id, step_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(consultation_id),
                    profile_id,
                    origin_runtime,
                    route.agent_id,
                    route.runtime,
                    # Free-form strings from configuration and from the host. Scrubbed
                    # at the insert like every other text column; the resume check in
                    # `ConsultService` compares against the scrubbed copy.
                    scrub_json(route.model),
                    capability,
                    None,
                    scrub_json(conversation_label),
                    protocol_version,
                    config_hash,
                    # The only value this column ever holds. A consultation has no
                    # terminal state -- the row is what makes it resumable, so it is
                    # open for as long as it exists. Anything reading it as liveness
                    # is reading a constant; reviews are where a real lifecycle lives.
                    "open",
                    now,
                    now,
                    workflow_id,
                    step_id,
                ),
            )
            return self._fetch(str(consultation_id))

        return await self._run(work)

    async def get_consultation(self, consultation_id: UUID | str) -> Consultation:
        return await self._run(lambda: self._fetch(str(consultation_id)))

    def _fetch(self, consultation_id: str) -> Consultation:
        row = self._db.execute(
            "SELECT * FROM consultations WHERE id = ?", (consultation_id,)
        ).fetchone()
        if row is None:
            # A consultation id this server never issued, or one from a database the
            # host has since been repointed away from. Either way there is no session
            # behind it, and inventing one would silently start a new conversation.
            raise StoreError(
                ConsultErrorCode.SESSION_NOT_FOUND,
                f"no consultation `{consultation_id}` in this store; start a new one",
            )
        return Consultation(**dict(row))

    async def step_consultation(self, workflow_id: str, step_id: str) -> Consultation | None:
        """The consultation a workflow step already owns, if it has one.

        None rather than a raise: a step's first turn has nothing to resume, and that
        is the ordinary case, not a missing row.
        """

        def work() -> Consultation | None:
            row = self._db.execute(
                "SELECT * FROM consultations WHERE workflow_id = ? AND step_id = ? "
                "ORDER BY created_at LIMIT 1",
                (workflow_id, step_id),
            ).fetchone()
            return None if row is None else Consultation(**dict(row))

        return await self._run(work)

    async def bind_native_session(self, consultation_id: UUID | str, native_session_id: str) -> None:
        """Record the session id the runtime gave us, once."""

        def work() -> None:
            current = self._fetch(str(consultation_id)).native_session_id
            if current is not None and current != native_session_id:
                raise StoreError(
                    ConsultErrorCode.SESSION_TARGET_MISMATCH,
                    f"consultation `{consultation_id}` is already bound to native session "
                    f"`{current}`",
                )
            self._db.execute(
                "UPDATE consultations SET native_session_id = ?, updated_at = ? WHERE id = ?",
                (native_session_id, _now(), str(consultation_id)),
            )

        await self._run(work)

    def check_target(self, consultation: Consultation, target_agent: str | None) -> None:
        """A consultation is bound to the agent it started with, for its whole life.

        Continuing it against a different agent would hand a second vendor's model a
        conversation it never had, and the caller would read the reply as the same
        agent's next turn.
        """
        if target_agent is not None and target_agent != consultation.target_agent_id:
            raise StoreError(
                ConsultErrorCode.SESSION_TARGET_MISMATCH,
                f"consultation `{consultation.id}` is bound to `{consultation.target_agent_id}`; "
                f"start a new consultation to ask `{target_agent}`",
            )

    # --- turns --------------------------------------------------------------

    async def next_sequence(self, consultation_id: UUID | str) -> int:
        def work() -> int:
            row = self._db.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) FROM consultation_turns "
                "WHERE consultation_id = ?",
                (str(consultation_id),),
            ).fetchone()
            return int(row[0]) + 1

        return await self._run(work)

    async def record_turn(
        self,
        consultation_id: UUID | str,
        sequence_number: int,
        source_mode: SourceMode,
        user_prompt: str,
        context: str | None,
        compiled_prompt: str,
        raw_output: str | None = None,
        validated_response: dict[str, Any] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float | None = None,
        latency_ms: int = 0,
        error_code: ConsultErrorCode | None = None,
    ) -> None:
        # `store_full_content: false` drops the bodies and keeps the shape: an
        # operator who cannot keep prompts on disk still gets timings, usage and
        # failures, which is what makes the record worth having at all.
        keep = self.store_full_content

        def work() -> None:
            self._db.execute(
                "INSERT INTO consultation_turns (consultation_id, sequence_number, source_mode, "
                "user_prompt, context, compiled_prompt, raw_output, validated_response_json, "
                "input_tokens, output_tokens, total_tokens, cost_usd, latency_ms, error_code, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(consultation_id),
                    sequence_number,
                    source_mode.value,
                    user_prompt if keep else None,
                    context if keep else None,
                    compiled_prompt if keep else None,
                    raw_output if keep else None,
                    json.dumps(validated_response) if (keep and validated_response) else None,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cost_usd,
                    latency_ms,
                    error_code.value if error_code else None,
                    _now(),
                ),
            )
            self._db.execute(
                "UPDATE consultations SET updated_at = ? WHERE id = ?",
                (_now(), str(consultation_id)),
            )

        await self._run(work)

    async def turns(self, consultation_id: UUID | str) -> list[Turn]:
        def work() -> list[Turn]:
            rows = self._db.execute(
                "SELECT sequence_number, source_mode, user_prompt, context, compiled_prompt, "
                "raw_output, validated_response_json, input_tokens, output_tokens, total_tokens, "
                "cost_usd, latency_ms, error_code, created_at FROM consultation_turns "
                "WHERE consultation_id = ? ORDER BY sequence_number",
                (str(consultation_id),),
            ).fetchall()
            return [Turn(**dict(row)) for row in rows]

        return await self._run(work)

    async def usage(self, consultation_id: UUID | str) -> Usage | None:
        """Cumulative accounting for every recorded turn in one native session."""

        def work() -> Usage | None:
            row = self._db.execute(
                "SELECT COUNT(*) AS turns, COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
                "COUNT(cost_usd) AS priced_turns, SUM(cost_usd) AS cost_usd "
                "FROM consultation_turns WHERE consultation_id = ?",
                (str(consultation_id),),
            ).fetchone()
            if row["turns"] == 0:
                return None
            return Usage(
                prompt_tokens=row["input_tokens"],
                completion_tokens=row["output_tokens"],
                total_tokens=row["total_tokens"],
                cost_usd=row["cost_usd"] if row["priced_turns"] == row["turns"] else None,
            )

        return await self._run(work)

    async def latest_response(self, consultation_id: UUID | str) -> dict[str, Any] | None:
        """The latest retained structured answer, for rebuilding a review result."""

        def work() -> dict[str, Any] | None:
            row = self._db.execute(
                "SELECT validated_response_json FROM consultation_turns "
                "WHERE consultation_id = ? AND validated_response_json IS NOT NULL "
                "ORDER BY sequence_number DESC LIMIT 1",
                (str(consultation_id),),
            ).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row[0])
            except (TypeError, ValueError):
                return None
            return value if isinstance(value, dict) else None

        return await self._run(work)

    # --- deletion ----------------------------------------------------------

    async def delete_consultation(self, consultation_id: UUID | str) -> int:
        """Delete one ordinary consultation; review-owned consultations stay with the review."""
        return await self._run(lambda: self._delete_consultations([str(consultation_id)]))

    async def request_delete_all_consultations(
        self, ttl_s: float = DELETE_CONFIRM_TTL_S
    ) -> tuple[str, int]:
        """Snapshot every consultation not owned by a review and return a one-use token."""

        def work() -> tuple[str, int]:
            self._db.execute(
                "DELETE FROM consultation_delete_confirmations WHERE expires_at <= ?",
                (time.time(),),
            )
            ids = [
                row[0]
                for row in self._db.execute(
                    "SELECT c.id FROM consultations c WHERE NOT EXISTS ("
                    "SELECT 1 FROM review_consultations r WHERE r.consultation_id = c.id"
                    ") ORDER BY c.id"
                )
            ]
            token = secrets.token_urlsafe(32)
            self._db.execute(
                "INSERT INTO consultation_delete_confirmations (token_sha, "
                "consultation_ids_json, displayed_count, created_at, expires_at) "
                "VALUES (?,?,?,?,?)",
                (_token_sha(token), json.dumps(ids, separators=(",", ":")), len(ids),
                 _now(), time.time() + ttl_s),
            )
            return token, len(ids)

        return await self._run(work)

    async def delete_all_consultations(self, token: str) -> int:
        """Delete only the ordinary-consultation snapshot approved by `token`."""
        return await self._run(
            lambda: self._delete_consultations([], confirmation_sha=_token_sha(token))
        )

    def _delete_consultations(
        self, ids: list[str], confirmation_sha: str | None = None
    ) -> int:
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            db.execute("DELETE FROM consultation_leases WHERE expires_at <= ?", (now,))
            db.execute(
                "DELETE FROM consultation_delete_confirmations WHERE expires_at <= ?", (now,)
            )
            if confirmation_sha is not None:
                row = db.execute(
                    "SELECT consultation_ids_json, expires_at FROM "
                    "consultation_delete_confirmations WHERE token_sha = ?",
                    (confirmation_sha,),
                ).fetchone()
                if row is None:
                    raise StoreError(
                        ConsultErrorCode.INVALID_REQUEST,
                        "that confirmation is not outstanding; call "
                        "`orchestrator_request_delete_all_consultations` again",
                    )
                if row["expires_at"] <= now:
                    raise StoreError(
                        ConsultErrorCode.INVALID_REQUEST,
                        "that confirmation has expired; request the count again",
                    )
                ids = json.loads(row["consultation_ids_json"])
                consumed = db.execute(
                    "DELETE FROM consultation_delete_confirmations WHERE token_sha = ?",
                    (confirmation_sha,),
                )
                if consumed.rowcount != 1:
                    raise StoreError(
                        ConsultErrorCode.INVALID_REQUEST,
                        "that confirmation was already spent; request a new count",
                    )

            if not ids:
                db.execute("COMMIT")
                return 0
            marks = ",".join("?" * len(ids))
            linked = db.execute(
                f"SELECT consultation_id FROM review_consultations WHERE "
                f"consultation_id IN ({marks}) LIMIT 1",
                ids,
            ).fetchone()
            if linked is not None:
                raise StoreError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"consultation `{linked[0]}` belongs to a review; delete the review instead",
                )
            busy = db.execute(
                f"SELECT consultation_id FROM consultation_leases WHERE "
                f"consultation_id IN ({marks}) LIMIT 1",
                ids,
            ).fetchone()
            if busy is not None:
                raise StoreError(
                    ConsultErrorCode.SESSION_BUSY,
                    f"consultation `{busy[0]}` has a turn in flight; wait for it to finish",
                )
            existing = [
                row[0]
                for row in db.execute(
                    f"SELECT id FROM consultations WHERE id IN ({marks})", ids
                )
            ]
            if not existing:
                db.execute("COMMIT")
                return 0
            existing_marks = ",".join("?" * len(existing))
            for table in ("consultation_turns", "routing_decisions", "consultation_leases"):
                db.execute(
                    f"DELETE FROM {table} WHERE consultation_id IN ({existing_marks})", existing
                )
            deleted = db.execute(
                f"DELETE FROM consultations WHERE id IN ({existing_marks})", existing
            ).rowcount
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return deleted

    # --- diagnostics --------------------------------------------------------

    async def record_routing(self, consultation_id: UUID | str, decision: RoutingDecision) -> None:
        def work() -> None:
            self._db.execute(
                "INSERT INTO routing_decisions (consultation_id, capability, selected_agent, "
                "explicit, excluded_json, error_code, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    str(consultation_id),
                    decision.capability,
                    decision.route.agent_id if decision.route else None,
                    int(bool(decision.route and decision.route.explicitly_selected)),
                    json.dumps([{"agent_id": e.agent_id, "reason": e.reason} for e in decision.excluded]),
                    decision.error[0].value if decision.error else None,
                    _now(),
                ),
            )

        await self._run(work)

    async def routing_for(self, consultation_id: UUID | str) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT capability, selected_agent, explicit, excluded_json, error_code, created_at "
                "FROM routing_decisions WHERE consultation_id = ? ORDER BY id",
                (str(consultation_id),),
            ).fetchall()
            return [dict(row) | {"excluded": json.loads(row["excluded_json"])} for row in rows]

        return await self._run(work)

    async def record_status_check(
        self,
        agent_id: str,
        installed: bool | None,
        authenticated: bool | None,
        detail: str | None = None,
    ) -> None:
        # `detail` is ours to write -- a short explanation like "not on PATH". But it
        # is built from an `AdapterError`, and what an adapter puts in one is the
        # adapter's decision: one that starts quoting a failing argv or a stderr line
        # would carry a credential here without this call changing at all. Scrubbed at
        # the insert, so the guarantee belongs to the column rather than to whichever
        # adapter wrote the message.
        await self._run(
            lambda: self._db.execute(
                "INSERT INTO agent_status_checks (agent_id, installed, authenticated, detail, "
                "checked_at) VALUES (?,?,?,?,?)",
                (
                    agent_id,
                    None if installed is None else int(installed),
                    None if authenticated is None else int(authenticated),
                    scrub_json(detail),
                    _now(),
                ),
            )
        )

    # --- leases -------------------------------------------------------------

    @asynccontextmanager
    async def lease(self, consultation_id: UUID | str, ttl_s: float = LEASE_TTL_S):
        """Hold the right to advance one consultation's native session.

        Turns on a CLI session are inherently serial: two of them interleaved would
        produce one conversation with two futures. `BEGIN IMMEDIATE` is what makes
        the check-and-take atomic across processes, which an in-process lock cannot
        be.

        `ttl_s` has to outlive the turn it guards, or the lease expires under a
        consultation that is still running and a second caller is let in beside it.
        The service passes its configured timeout; the default here is only for
        callers that have no timeout of their own.
        """
        # A fresh token per acquisition, not a per-process id: after an expiry, the
        # same process can hold the *next* lease on the same consultation, and a
        # release keyed on the process would delete a lease it does not own.
        token = f"pid-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        await self._run(lambda: self._acquire(str(consultation_id), ttl_s, token))
        try:
            yield
        finally:
            await self._run(lambda: self._release(str(consultation_id), token))

    def _acquire(self, consultation_id: str, ttl_s: float, token: str) -> None:
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            # A holder that died mid-turn leaves its row behind; the expiry is what
            # keeps that from wedging the consultation forever.
            db.execute("DELETE FROM consultation_leases WHERE expires_at <= ?", (now,))
            cursor = db.execute(
                "INSERT OR IGNORE INTO consultation_leases (consultation_id, holder, expires_at) "
                "VALUES (?,?,?)",
                (consultation_id, token, now + ttl_s),
            )
            taken = cursor.rowcount == 1
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

        if not taken:
            raise StoreError(
                ConsultErrorCode.SESSION_BUSY,
                f"consultation `{consultation_id}` already has a turn in flight; "
                "wait for it to finish before sending the next one",
            )

    def _release(self, consultation_id: str, token: str) -> None:
        self._db.execute(
            "DELETE FROM consultation_leases WHERE consultation_id = ? AND holder = ?",
            (consultation_id, token),
        )
