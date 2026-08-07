"""Persistence for reviews, on the connection `ConsultStore` already holds.

One connection and one lock for both layers, so a review and the consultations
under it are written by the same serialized worker rather than by two drivers
racing over one file. That is why this wraps a `ConsultStore` instead of opening
its own.

Two things this file is responsible for and no caller is trusted to remember:

* **Every text column is scrubbed on its way in.** The sanitizer runs here, at the
  insert, not in a cleanup pass afterwards -- a cleanup pass is only needed by a
  design that writes the secret first, and a crash in the window between the two
  would leave it there.
* **Deletion collects the whole tree before removing anything**, and refuses while
  a review is leased. A review cancelled in one process can still have subprocesses
  writing into it from another, and status alone cannot see them.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ..contract import scrub_json
from ..consult.errors import ConsultErrorCode
from ..consult.store import ConsultStore, StoreError

# Long enough to outlive a whole reviewer batch. They run in parallel, so the bound
# is the slowest one plus its preflight, not the sum of all of them.
REVIEW_LEASE_SLACK_S = 60.0
DELETE_CONFIRM_TTL_S = 300.0


@dataclass(frozen=True)
class Review:
    id: str
    parent_review_id: str | None
    mode: str
    status: str
    outcome: str | None
    goal: str | None
    context: str | None
    material_json: str
    material_sha256: str
    raw_sha256: str
    reviewer_snapshot_json: str
    confirm_token_sha: str | None
    secrets_mode: str | None
    secret_hits_json: str
    web_requested: int
    host_findings_json: str | None
    summary_json: str | None
    fix_rounds_json: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewerRow:
    review_id: str
    agent_id: str
    consultation_id: str | None
    status: str
    findings_json: str | None
    findings_parsed: int
    findings_truncated: int
    answer: str | None
    error_code: str | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical(payload: Any) -> str:
    """Deterministic JSON, so a hash recomputed later matches the one stored.

    Sorted keys and no incidental whitespace: the same payload has to serialize
    identically across processes and Python versions, or the approval check fails
    for a reason that has nothing to do with the material changing.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ReviewStore:
    """Review rows on a `ConsultStore`'s connection.

    Reaches into that store's `_run` and `_db` deliberately: sharing the worker and
    the `RLock` is the point, and a second connection to the same file would be two
    writers where the design assumes one.
    """

    def __init__(self, store: ConsultStore) -> None:
        self.store = store

    @property
    def _db(self):
        return self.store._db

    async def _run(self, work):
        return await self.store._run(work)

    def _keep(self, value: Any) -> Any:
        """Scrub, then drop the body if the operator does not keep content.

        `store_full_content: false` means model output does not go on disk. Findings
        and summaries are model output, so they go too -- what survives is the shape:
        statuses, agent ids, timings, error codes, hashes. The results still reach the
        caller in the response; they are simply not kept.
        """
        cleaned = scrub_json(value)
        return cleaned if self.store.store_full_content else None

    # --- reviews ------------------------------------------------------------

    async def create_review(
        self,
        review_id: UUID,
        mode: str,
        goal: str,
        context: str | None,
        material: list[dict[str, Any]],
        material_sha256: str,
        raw_sha256: str,
        reviewer_snapshot: list[dict[str, Any]],
        confirm_token: str,
        secret_hits: list[dict[str, Any]],
        web_requested: bool,
        parent_review_id: UUID | str | None = None,
    ) -> str:
        """Write the `pending` row. Returns nothing the caller does not already have.

        `goal` and `context` are stored redacted and are *not* subject to
        `store_full_content`: the second half of the handshake reads them back to
        send, so nulling them here would make every review impossible to run rather
        than merely unlogged. `material` is a manifest of labels, and is scrubbed
        like everything else -- a file path can carry a token.
        """

        def work() -> None:
            now = _now()
            self._db.execute(
                "INSERT INTO reviews (id, parent_review_id, mode, status, outcome, goal, context, "
                "material_json, material_sha256, raw_sha256, reviewer_snapshot_json, "
                "confirm_token_sha, secret_hits_json, web_requested, created_at, updated_at) "
                "VALUES (?,?,?,'pending',NULL,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(review_id),
                    str(parent_review_id) if parent_review_id is not None else None,
                    mode,
                    scrub_json(goal),
                    scrub_json(context),
                    canonical(scrub_json(material)),
                    material_sha256,
                    raw_sha256,
                    canonical(reviewer_snapshot),
                    sha256(confirm_token),
                    canonical(secret_hits),
                    int(bool(web_requested)),
                    now,
                    now,
                ),
            )

        await self._run(work)

    async def get_review(self, review_id: UUID | str) -> Review:
        def work() -> Review:
            row = self._db.execute(
                "SELECT * FROM reviews WHERE id = ?", (str(review_id),)
            ).fetchone()
            if row is None:
                raise StoreError(
                    ConsultErrorCode.SESSION_NOT_FOUND,
                    f"no review `{review_id}` in this store",
                )
            return Review(**dict(row))

        return await self._run(work)

    async def list_reviews(self, limit: int = 20) -> list[Review]:
        def work() -> list[Review]:
            rows = self._db.execute(
                "SELECT * FROM reviews ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [Review(**dict(row)) for row in rows]

        return await self._run(work)

    async def consume_confirm_token(
        self,
        review_id: UUID | str,
        token: str,
        *,
        host_findings: list[str] | None = None,
        secrets_mode: str = "mask",
    ) -> None:
        """Spend the token, record run metadata, and start, in one statement.

        The status predicate is what makes two simultaneous `orchestrator_review_run` calls launch
        one review instead of two paid ones; nulling the hash is what stops a third
        call replaying the same token later.
        """

        def work() -> None:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute(
                    "UPDATE reviews SET status = 'running', confirm_token_sha = NULL, "
                    "host_findings_json = COALESCE(?, host_findings_json), secrets_mode = ?, "
                    "updated_at = ? WHERE id = ? AND status = 'pending' AND confirm_token_sha = ?",
                    (
                        _json_or_none(self._keep(host_findings)) if host_findings else None,
                        secrets_mode,
                        _now(),
                        str(review_id),
                        sha256(token),
                    ),
                )
                taken = cursor.rowcount == 1
            except Exception:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

            if not taken:
                raise StoreError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"review `{review_id}` is not waiting on that confirmation; the token was "
                    "already used, or the review has moved on. Plan a new review to send again",
                )

        await self._run(work)

    async def transition(
        self,
        review_id: UUID | str,
        to_status: str,
        allowed_from: tuple[str, ...],
        outcome: str | None = None,
    ) -> bool:
        """Move a review, but only from where it is allowed to move.

        False means somebody got there first -- a cancel that landed while the
        reviewers were finishing, or a second retry. The caller decides what that
        means; what it must not do is overwrite the state that won.

        `outcome` uses `COALESCE` deliberately: transitions that do not recompute an
        outcome preserve the value derived from the persisted reviewer rows.
        """
        placeholders = ",".join("?" * len(allowed_from))

        def work() -> bool:
            cursor = self._db.execute(
                f"UPDATE reviews SET status = ?, outcome = COALESCE(?, outcome), updated_at = ? "
                f"WHERE id = ? AND status IN ({placeholders})",
                (to_status, outcome, _now(), str(review_id), *allowed_from),
            )
            return cursor.rowcount == 1

        return await self._run(work)

    async def save_host_findings(self, review_id: UUID | str, findings: Any) -> None:
        await self._run(
            lambda: self._db.execute(
                "UPDATE reviews SET host_findings_json = ?, updated_at = ? WHERE id = ?",
                (_json_or_none(self._keep(findings)), _now(), str(review_id)),
            )
        )

    @property
    def keeps_content(self) -> bool:
        return self.store.store_full_content

    async def append_fix_round(self, review_id: UUID | str, round_: dict[str, Any]) -> None:
        """Add one round to the review's log.

        Read and write under `BEGIN IMMEDIATE`, so two server processes cannot both
        read the same list and let the second write the first round out of existence.

        Not `_keep`: dropping the whole log under `store_full_content: false` would
        lose the shape too, and the shape here -- which findings a round took on and
        how it went -- is metadata. Only `notes` is prose, and the caller drops that.
        """

        def work() -> None:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT fix_rounds_json FROM reviews WHERE id = ?", (str(review_id),)
                ).fetchone()
                if row is None:
                    raise StoreError(
                        ConsultErrorCode.SESSION_NOT_FOUND,
                        f"no review `{review_id}` in this store",
                    )
                rounds = json.loads(row[0]) if row[0] else []
                rounds.append(scrub_json(round_))
                db.execute(
                    "UPDATE reviews SET fix_rounds_json = ?, updated_at = ? WHERE id = ?",
                    (canonical(rounds), _now(), str(review_id)),
                )
            except Exception:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

        await self._run(work)

    async def recheck_ids(self, review_id: UUID | str) -> list[str]:
        """The reviews planned against this one, oldest first."""

        def work() -> list[str]:
            return [
                row[0]
                for row in self._db.execute(
                    "SELECT id FROM reviews WHERE parent_review_id = ? "
                    "ORDER BY created_at, id",
                    (str(review_id),),
                )
            ]

        return await self._run(work)

    async def save_summary(self, review_id: UUID | str, summary: Any) -> None:
        await self._run(
            lambda: self._db.execute(
                "UPDATE reviews SET summary_json = ?, updated_at = ? WHERE id = ?",
                (_json_or_none(self._keep(summary)), _now(), str(review_id)),
            )
        )

    async def complete_review(self, review_id: UUID | str, summary: Any) -> bool:
        """Store the synthesis only if it atomically wins the completion transition."""

        def work() -> bool:
            cursor = self._db.execute(
                "UPDATE reviews SET summary_json = ?, status = 'complete', updated_at = ? "
                "WHERE id = ? AND status = 'awaiting_synthesis'",
                (_json_or_none(self._keep(summary)), _now(), str(review_id)),
            )
            return cursor.rowcount == 1

        return await self._run(work)

    # --- reviewer rows ------------------------------------------------------

    async def record_reviewer_result(
        self,
        review_id: UUID | str,
        agent_id: str,
        status: str,
        consultation_id: UUID | str | None = None,
        findings: Any = None,
        findings_parsed: bool = False,
        findings_truncated: int = 0,
        answer: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Upsert one reviewer's outcome.

        Upsert rather than insert: a retry is another attempt by the same reviewer on
        the same review, and `(review_id, agent_id)` stays one row so no consultation
        is left dangling where deletion cannot find it.
        """

        def work() -> None:
            self._db.execute(
                "INSERT INTO review_consultations (review_id, agent_id, consultation_id, status, "
                "findings_json, findings_parsed, findings_truncated, answer, error_code, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (review_id, agent_id) DO UPDATE SET "
                "consultation_id = excluded.consultation_id, status = excluded.status, "
                "findings_json = excluded.findings_json, "
                "findings_parsed = excluded.findings_parsed, "
                "findings_truncated = excluded.findings_truncated, answer = excluded.answer, "
                "error_code = excluded.error_code, created_at = excluded.created_at",
                (
                    str(review_id),
                    agent_id,
                    str(consultation_id) if consultation_id is not None else None,
                    status,
                    _json_or_none(self._keep(findings)),
                    int(findings_parsed),
                    findings_truncated,
                    self._keep(answer),
                    error_code,
                    _now(),
                ),
            )
            self._db.execute(
                "UPDATE reviews SET updated_at = ? WHERE id = ?", (_now(), str(review_id))
            )

        await self._run(work)

    async def reviewer_rows(self, review_id: UUID | str) -> list[ReviewerRow]:
        def work() -> list[ReviewerRow]:
            rows = self._db.execute(
                "SELECT * FROM review_consultations WHERE review_id = ? ORDER BY agent_id",
                (str(review_id),),
            ).fetchall()
            return [ReviewerRow(**dict(row)) for row in rows]

        return await self._run(work)

    # --- execution lease ----------------------------------------------------

    @asynccontextmanager
    async def lease(self, review_id: UUID | str, ttl_s: float):
        """Hold a review while its reviewers are actually running.

        Status cannot carry this. `orchestrator_cancel_review` moves a review out of `running`
        while another process's subprocesses keep going, and a delete that only
        consulted the status would remove the parent out from under a reviewer that
        is about to write its row -- leaving history no later delete can find.

        Same mechanism as `ConsultStore.lease` and for the same reason: `BEGIN
        IMMEDIATE` is serialized by SQLite across processes, an `asyncio.Lock` is not.
        A fresh token per acquisition, so a release can never delete a lease taken by
        the next holder after an expiry.
        """
        token = f"pid-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        await self._run(lambda: self._acquire(str(review_id), ttl_s, token))
        try:
            yield
        finally:
            await self._run(
                lambda: self._db.execute(
                    "DELETE FROM review_leases WHERE review_id = ? AND holder = ?",
                    (str(review_id), token),
                )
            )

    def _acquire(self, review_id: str, ttl_s: float, token: str) -> None:
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            # A holder that died mid-run leaves its row behind; the expiry is what
            # keeps that from wedging the review against every future delete.
            db.execute("DELETE FROM review_leases WHERE expires_at <= ?", (now,))
            cursor = db.execute(
                "INSERT OR IGNORE INTO review_leases (review_id, holder, expires_at) "
                "VALUES (?,?,?)",
                (review_id, token, now + ttl_s),
            )
            taken = cursor.rowcount == 1
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

        if not taken:
            raise StoreError(
                ConsultErrorCode.SESSION_BUSY,
                f"review `{review_id}` already has reviewers in flight; wait for them to "
                "finish before running it again",
            )

    # --- deletion -----------------------------------------------------------

    async def delete_review(self, review_id: UUID | str) -> int:
        """Delete a review, its rechecks, and every consultation under either."""
        return await self._run(lambda: self._delete([str(review_id)]))

    async def request_delete_all(self, ttl_s: float = DELETE_CONFIRM_TTL_S) -> tuple[str, int]:
        """Snapshot what would be deleted, and hand back a token for exactly that.

        The ids are recorded, not just the count. Confirming against a fresh `SELECT`
        would delete a review created in the meantime -- one the user was never shown
        and never approved.
        """

        def work() -> tuple[str, int]:
            self._db.execute(
                "DELETE FROM review_delete_confirmations WHERE expires_at <= ?", (time.time(),)
            )
            ids = [row[0] for row in self._db.execute("SELECT id FROM reviews ORDER BY id")]
            token = secrets.token_urlsafe(32)
            self._db.execute(
                "INSERT INTO review_delete_confirmations (token_sha, review_ids_json, "
                "displayed_count, created_at, expires_at) VALUES (?,?,?,?,?)",
                (sha256(token), canonical(ids), len(ids), _now(), time.time() + ttl_s),
            )
            return token, len(ids)

        return await self._run(work)

    async def delete_all_reviews(self, token: str) -> int:
        """Delete the approved snapshot, and nothing that arrived after it."""

        def work() -> int:
            return self._delete([], expand=False, confirmation_sha=sha256(token))

        return await self._run(work)

    def _delete(
        self,
        roots: list[str],
        *,
        expand: bool = True,
        confirmation_sha: str | None = None,
    ) -> int:
        """Collect the tree, refuse if any of it is busy, then remove it in order.

        Order matters because `PRAGMA foreign_keys=ON`: `routing_decisions` and
        `consultation_turns` both reference `consultations`, so the consultations go
        last. `reviews.parent_review_id` is deferred, which is what lets one
        statement remove a parent and its rechecks together.
        """
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM review_leases WHERE expires_at <= ?", (time.time(),))

            if confirmation_sha is not None:
                row = db.execute(
                    "SELECT review_ids_json, expires_at FROM review_delete_confirmations "
                    "WHERE token_sha = ?",
                    (confirmation_sha,),
                ).fetchone()
                if row is None:
                    raise StoreError(
                        ConsultErrorCode.INVALID_REQUEST,
                        "that confirmation is not outstanding; call "
                        "`orchestrator_request_delete_all` and confirm the count it reports",
                    )
                if row["expires_at"] <= time.time():
                    raise StoreError(
                        ConsultErrorCode.INVALID_REQUEST,
                        "that confirmation has expired; request it again so the count you "
                        "approve is the count that gets deleted",
                    )
                roots = json.loads(row["review_ids_json"])
                consumed = db.execute(
                    "DELETE FROM review_delete_confirmations WHERE token_sha = ?",
                    (confirmation_sha,),
                )
                if consumed.rowcount != 1:
                    raise StoreError(
                        ConsultErrorCode.INVALID_REQUEST,
                        "that confirmation was already spent; request a new count",
                    )

            if expand:
                tree = self._descendants(roots)
            elif roots:
                approved_marks = ",".join("?" * len(roots))
                tree = [
                    row[0]
                    for row in db.execute(
                        f"SELECT id FROM reviews WHERE id IN ({approved_marks})", roots
                    )
                ]
            else:
                tree = []
            if not tree:
                db.execute("COMMIT")
                return 0
            marks = ",".join("?" * len(tree))

            busy = db.execute(
                f"SELECT id FROM reviews WHERE id IN ({marks}) AND status = 'running'", tree
            ).fetchone()
            if busy is not None:
                raise StoreError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"review `{busy[0]}` is still running; cancel it first",
                )
            leased = db.execute(
                f"SELECT review_id, expires_at FROM review_leases WHERE review_id IN ({marks})",
                tree,
            ).fetchone()
            if leased is not None:
                # Cancelling is not enough on its own: another process's reviewers can
                # still be mid-flight, and deleting now would leave their consultations
                # behind with no review pointing at them.
                raise StoreError(
                    ConsultErrorCode.SESSION_BUSY,
                    f"review `{leased[0]}` still has reviewers in flight, possibly in another "
                    f"server process; it can be deleted once they stop or the lease expires "
                    f"(in {max(0, int(leased[1] - time.time()))}s)",
                )

            consultations = [
                row[0]
                for row in db.execute(
                    f"SELECT DISTINCT consultation_id FROM review_consultations "
                    f"WHERE review_id IN ({marks}) AND consultation_id IS NOT NULL",
                    tree,
                )
            ]

            db.execute(f"DELETE FROM review_consultations WHERE review_id IN ({marks})", tree)
            if consultations:
                held = ",".join("?" * len(consultations))
                for table in (
                    "consultation_turns",
                    "routing_decisions",
                    "consultation_leases",
                ):
                    column = "consultation_id"
                    db.execute(f"DELETE FROM {table} WHERE {column} IN ({held})", consultations)
                db.execute(f"DELETE FROM consultations WHERE id IN ({held})", consultations)
            if not expand:
                # A recheck created after the snapshot is not approved for deletion.
                # Detach it before removing its snapshotted parent so the deferred
                # foreign key can commit while the new review survives as a root.
                db.execute(
                    f"UPDATE reviews SET parent_review_id = NULL WHERE parent_review_id "
                    f"IN ({marks}) AND id NOT IN ({marks})",
                    [*tree, *tree],
                )
            db.execute(f"DELETE FROM reviews WHERE id IN ({marks})", tree)
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return len(tree)

    def _descendants(self, roots: list[str]) -> list[str]:
        """Every review reachable from these, through `parent_review_id`.

        `UNION` rather than `UNION ALL`, so a cycle in a hand-edited database
        terminates instead of running until the process dies.
        """
        marks = ",".join("?" * len(roots))
        rows = self._db.execute(
            f"WITH RECURSIVE tree(id) AS ("
            f"  SELECT id FROM reviews WHERE id IN ({marks})"
            f"  UNION"
            f"  SELECT r.id FROM reviews r JOIN tree t ON r.parent_review_id = t.id"
            f") SELECT id FROM tree",
            roots,
        ).fetchall()
        return [row[0] for row in rows]


def _json_or_none(value: Any) -> str | None:
    return None if value is None else canonical(value)
