"""Workflow rows, on the connection `ConsultStore` already holds.

Same arrangement and the same reason as `ReviewStore`: one connection and one lock
for every layer, so a workflow, the consultations its steps created and the reviews
they ran are written by one serialized worker and delete in one transaction.

Two invariants live here rather than in the service, because a service can be called
twice at once and a `WHERE` clause cannot:

  * **A workflow moves only from where it is allowed to move.** `transition` puts the
    guard in the statement, so two processes cannot both take one transition.
  * **A step's token is spent in the statement that starts it.** Not checked, then
    spent -- the gap between those two is exactly one duplicate paid run wide.

A step's state is separate from its workflow's: the workflow advances only when a
step succeeds, so a step that dies mid-run leaves the workflow where it was and the
answer is to plan that step again.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ..code.service import remove_workflow_patches_now
from ..consult.errors import ConsultErrorCode
from ..consult.store import (
    DELETE_CONFIRM_TTL_S,
    ConsultStore,
    StoreError,
    _renewing_lease,
)
from ..contract import scrub_json
from ..review.store import delete_tree
from .contract import (
    TERMINAL_STATES,
    Executor,
    ReportedBy,
    RepositoryAccess,
    Step,
    StepSnapshot,
    StepStatus,
    WorkflowState,
)

# Long enough to outlive one step, which is one agent call plus its preflight. The
# service passes its configured timeout plus slack; this is only for callers with no
# timeout of their own.
STEP_LEASE_SLACK_S = 60.0

# Built from the one definition rather than typed out again: a state added to
# `TERMINAL_STATES` and forgotten here would be a state a step could still start under.
_TERMINAL_SQL = ", ".join(f"'{state}'" for state in sorted(TERMINAL_STATES))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical(payload: Any) -> str:
    """Deterministic JSON, so a hash recomputed later matches the one stored."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _approved(db: sqlite3.Connection, confirmation_sha: str) -> list[str]:
    """The ids a confirmation covers, spent in the transaction that reads them."""
    row = db.execute(
        "SELECT workflow_ids_json, expires_at FROM workflow_delete_confirmations "
        "WHERE token_sha = ?",
        (confirmation_sha,),
    ).fetchone()
    if row is None:
        raise StoreError(
            ConsultErrorCode.INVALID_REQUEST,
            "that confirmation is not outstanding; call "
            "`orchestrator_request_delete_all_workflows` and confirm the count it reports",
        )
    if row["expires_at"] <= time.time():
        raise StoreError(
            ConsultErrorCode.INVALID_REQUEST,
            "that confirmation has expired; request it again so the count you approve is "
            "the count that gets deleted",
        )
    consumed = db.execute(
        "DELETE FROM workflow_delete_confirmations WHERE token_sha = ?", (confirmation_sha,)
    )
    if consumed.rowcount != 1:
        raise StoreError(
            ConsultErrorCode.INVALID_REQUEST,
            "that confirmation was already spent; request a new count",
        )
    return json.loads(row["workflow_ids_json"])


def _refuse_live(db: sqlite3.Connection, tree: list[str], marks: str) -> None:
    """Refuse a workflow that is still going, and one whose step is still running.

    Two separate refusals because they are two separate facts. A workflow sitting in
    `coding` is waiting on its host and can be finished or cancelled; a step holding
    an unexpired lease may be a live agent process in *another* server, and deleting
    its rows now would leave that process writing into a workflow that is gone.

    An expired lease is not swept first, only compared against: the row it sits on is
    `reap_abandoned`'s to resolve, and a delete has no business quietly rewriting the
    status of a step in a workflow it may be about to refuse.
    """
    live = db.execute(
        f"SELECT id, status FROM workflow_runs WHERE id IN ({marks}) "
        f"AND status NOT IN ({_TERMINAL_SQL}) LIMIT 1",
        tree,
    ).fetchone()
    if live is not None:
        raise StoreError(
            ConsultErrorCode.INVALID_REQUEST,
            f"workflow `{live[0]}` is still open (`{live[1]}`); finish it or call "
            "`orchestrator_workflow_cancel` first",
        )
    leased = db.execute(
        f"SELECT id, workflow_id, lease_expires_at FROM workflow_steps "
        f"WHERE workflow_id IN ({marks}) AND lease_expires_at > ? LIMIT 1",
        [*tree, time.time()],
    ).fetchone()
    if leased is not None:
        raise StoreError(
            ConsultErrorCode.SESSION_BUSY,
            f"workflow `{leased[1]}` still has step `{leased[0]}` in flight, possibly in "
            f"another server process; it can be deleted once that stops or the lease "
            f"expires (in {max(0, int(leased[2] - time.time()))}s)",
        )


@dataclass(frozen=True)
class WorkflowRun:
    id: str
    goal: str
    workdir: str
    host_runtime: str
    host_model: str | None
    status: str
    bindings_json: str
    policy_json: str
    baseline_commit: str | None
    result_commit: str | None
    fix_rounds: int
    reason: str | None
    workflow_hash: str
    created_at: str
    updated_at: str
    replan_token_sha: str | None = None
    replan_bindings_json: str | None = None


@dataclass(frozen=True)
class StepRow:
    id: str
    workflow_id: str
    step: str
    executor: str
    execution_mode: str | None
    repository_access: str
    round_index: int
    attempt: int
    sequence: int
    parent_step_id: str | None
    agent_id: str | None
    agent_snapshot_json: str
    status: str
    confirm_token_sha: str | None
    review_id: str | None
    output_json: str | None
    raw_patch_sha256: str | None
    recovery_written: int | None
    reported_by: str | None
    lease_holder: str | None
    lease_expires_at: float | None
    created_at: str
    updated_at: str

    def snapshot(self) -> StepSnapshot:
        """The resolved step, as the private consultation path requires it.

        Rebuilt from the row rather than kept in memory, so a run that crosses a
        process boundary is bound to what was approved and not to what today's config
        would resolve to.
        """
        agent = json.loads(self.agent_snapshot_json)
        return StepSnapshot(
            workflow_id=self.workflow_id,
            step=self.step,  # type: ignore[arg-type]
            step_id=self.id,
            executor=self.executor,  # type: ignore[arg-type]
            execution_mode=self.execution_mode,  # type: ignore[arg-type]
            repository_access=self.repository_access,  # type: ignore[arg-type]
            capability=agent.get("capability"),
            agent_id=self.agent_id,
            agent_runtime=agent.get("runtime"),
            agent_model=agent.get("model"),
            round_index=self.round_index,
            attempt=self.attempt,
            sequence=self.sequence,
            parent_step_id=self.parent_step_id,
            web=bool(agent.get("web", False)),
        )

    def output(self) -> dict[str, Any] | None:
        return json.loads(self.output_json) if self.output_json else None


LEASE_HELD: ContextVar[str | None] = ContextVar("workflow_lease_held", default=None)
"""The workflow this task already holds the lease on, or `None`.

Set by `lease` itself rather than passed down, so it is true of whoever is inside
one without a caller having to remember to say so. What reads it is the review
layer: a review step's token is its review's own token, so `ReviewService.run` is
reachable both from inside a step and directly from a host, and it has to tell
those apart. `_acquire` has no same-holder exemption -- deliberately -- so a
nested acquire would refuse the very step it belongs to.
"""


class WorkflowStore:
    """Workflow rows on a `ConsultStore`'s connection."""

    def __init__(self, store: ConsultStore) -> None:
        self.store = store

    @property
    def _db(self):
        return self.store._db

    async def _run(self, work):
        return await self.store._run(work)

    # --- runs ---------------------------------------------------------------

    async def create_workflow(
        self,
        workflow_id: UUID | str,
        goal: str,
        workdir: str,
        host_runtime: str,
        host_model: str | None,
        bindings: dict[str, Any],
        policy: dict[str, Any],
        workflow_hash: str,
        baseline_commit: str | None,
    ) -> None:
        """Write the `created` row, with its bindings already resolved.

        The binding snapshot is stored, not recomputed: a config edited halfway
        through a workflow must not silently reroute the rest of it. Changing them
        afterwards is `plan_replan`, with its own preview and token.
        """

        def work() -> None:
            now = _now()
            self._db.execute(
                "INSERT INTO workflow_runs (id, goal, workdir, host_runtime, host_model, status, "
                "bindings_json, policy_json, baseline_commit, result_commit, fix_rounds, reason, "
                "workflow_hash, created_at, updated_at) "
                "VALUES (?,?,?,?,?,'created',?,?,?,NULL,0,NULL,?,?,?)",
                (
                    str(workflow_id),
                    scrub_json(goal),
                    scrub_json(workdir),
                    host_runtime,
                    scrub_json(host_model),
                    canonical(scrub_json(bindings)),
                    canonical(scrub_json(policy)),
                    baseline_commit,
                    workflow_hash,
                    now,
                    now,
                ),
            )

        await self._run(work)

    async def get_workflow(self, workflow_id: UUID | str) -> WorkflowRun:
        def work() -> WorkflowRun:
            row = self._db.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (str(workflow_id),)
            ).fetchone()
            if row is None:
                raise StoreError(
                    ConsultErrorCode.SESSION_NOT_FOUND,
                    f"no workflow `{workflow_id}` in this store",
                )
            return WorkflowRun(**dict(row))

        return await self._run(work)

    async def transition(
        self,
        workflow_id: UUID | str,
        to_status: WorkflowState,
        allowed_from: tuple[WorkflowState, ...],
        *,
        reason: str | None = None,
        result_commit: str | None = None,
    ) -> bool:
        """Move a workflow, but only from where it is allowed to move.

        False means somebody got there first. The caller decides what that means;
        what it must not do is overwrite the state that won.
        """
        placeholders = ",".join("?" * len(allowed_from))

        def work() -> bool:
            cursor = self._db.execute(
                f"UPDATE workflow_runs SET status = ?, reason = COALESCE(?, reason), "
                f"result_commit = COALESCE(?, result_commit), updated_at = ? "
                f"WHERE id = ? AND status IN ({placeholders})",
                (
                    to_status,
                    scrub_json(reason),
                    result_commit,
                    _now(),
                    str(workflow_id),
                    *allowed_from,
                ),
            )
            return cursor.rowcount == 1

        return await self._run(work)

    async def bump_fix_rounds(self, workflow_id: UUID | str) -> int:
        """Count one more round, and return the new total.

        Incremented in the database rather than in the service, because the cap it
        feeds is the only thing standing between a non-converging review loop and an
        unbounded number of paid rounds.
        """

        def work() -> int:
            self._db.execute(
                "UPDATE workflow_runs SET fix_rounds = fix_rounds + 1, updated_at = ? WHERE id = ?",
                (_now(), str(workflow_id)),
            )
            row = self._db.execute(
                "SELECT fix_rounds FROM workflow_runs WHERE id = ?", (str(workflow_id),)
            ).fetchone()
            return int(row[0]) if row else 0

        return await self._run(work)

    async def stage_replan(
        self, workflow_id: UUID | str, bindings: dict[str, Any], confirm_token: str
    ) -> None:
        """Park a proposed binding set behind a token. Changes no routing yet.

        Stored rather than held in memory so the two halves of the handshake survive
        a restart, and so the bindings that get applied are the ones the preview
        described rather than whatever the config says by the time it is confirmed.
        """

        def work() -> None:
            self._db.execute(
                "UPDATE workflow_runs SET replan_bindings_json = ?, replan_token_sha = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    canonical(scrub_json(bindings)),
                    _sha256(confirm_token),
                    _now(),
                    str(workflow_id),
                ),
            )

        await self._run(work)

    async def consume_replan(self, workflow_id: UUID | str, token: str, workflow_hash: str) -> str:
        """Spend the token and adopt the staged bindings, in one statement.

        Returns the bindings now in force. Same shape as `consume_step_token`: the
        token hash is cleared by the update that uses it, so a replay finds nothing
        to redeem rather than reapplying a binding change nobody approved twice.
        """

        def work() -> str:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute(
                    "UPDATE workflow_runs SET bindings_json = replan_bindings_json, "
                    "workflow_hash = ?, replan_bindings_json = NULL, replan_token_sha = NULL, "
                    "updated_at = ? WHERE id = ? AND replan_token_sha = ? "
                    "AND replan_bindings_json IS NOT NULL",
                    (workflow_hash, _now(), str(workflow_id), _sha256(token)),
                )
                taken = cursor.rowcount == 1
            except Exception:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

            if not taken:
                raise StoreError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"workflow `{workflow_id}` is not waiting on that confirmation; the token "
                    "was already used, or no replan is staged. Plan the replan again",
                )
            row = self._db.execute(
                "SELECT bindings_json FROM workflow_runs WHERE id = ?", (str(workflow_id),)
            ).fetchone()
            return str(row[0])

        return await self._run(work)

    # --- steps --------------------------------------------------------------

    async def plan_step(
        self,
        step_id: UUID | str,
        workflow_id: UUID | str,
        step: Step,
        executor: Executor,
        execution_mode: str | None,
        repository_access: RepositoryAccess,
        agent_id: str | None,
        agent_snapshot: dict[str, Any],
        confirm_token: str,
        *,
        round_index: int = 0,
        attempt: int = 1,
        parent_step_id: str | None = None,
        review_id: str | None = None,
    ) -> StepRow:
        """Write the `planned` row and its token hash. Only the hash is stored."""

        def work() -> StepRow:
            now = _now()
            row = self._db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM workflow_steps WHERE workflow_id = ?",
                (str(workflow_id),),
            ).fetchone()
            sequence = int(row[0]) + 1
            self._db.execute(
                "INSERT INTO workflow_steps (id, workflow_id, step, executor, execution_mode, "
                "repository_access, round_index, attempt, sequence, parent_step_id, agent_id, "
                "agent_snapshot_json, status, confirm_token_sha, review_id, output_json, "
                "raw_patch_sha256, reported_by, lease_holder, lease_expires_at, created_at, "
                "updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'planned',?,?,NULL,NULL,NULL,NULL,NULL,?,?)",
                (
                    str(step_id),
                    str(workflow_id),
                    step,
                    executor,
                    execution_mode,
                    repository_access,
                    round_index,
                    attempt,
                    sequence,
                    parent_step_id,
                    agent_id,
                    canonical(scrub_json(agent_snapshot)),
                    _sha256(confirm_token),
                    review_id,
                    now,
                    now,
                ),
            )
            return self._fetch_step(str(step_id))

        return await self._run(work)

    async def supersede_planned_review_steps(
        self, workflow_id: UUID | str, round_index: int
    ) -> int:
        """Cancel earlier previews and erase their still-pending review material.

        A review plan's token is shown once, so replanning cannot reuse it. Keeping the
        old pending review, however, retained another undeletable copy of the material.
        Only planned steps whose review is still pending are eligible here.
        """

        def work() -> int:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT s.id, s.review_id FROM workflow_steps s JOIN reviews r "
                    "ON r.id = s.review_id WHERE s.workflow_id = ? AND s.step = 'review' "
                    "AND s.round_index = ? AND s.status = 'planned' AND r.status = 'pending'",
                    (str(workflow_id), round_index),
                ).fetchall()
                for row in rows:
                    db.execute(
                        "UPDATE workflow_steps SET status = 'cancelled', review_id = NULL, "
                        "confirm_token_sha = NULL, updated_at = ? WHERE id = ?",
                        (_now(), row[0]),
                    )
                    db.execute("DELETE FROM review_consultations WHERE review_id = ?", (row[1],))
                    db.execute("DELETE FROM reviews WHERE id = ? AND status = 'pending'", (row[1],))
            except Exception:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            return len(rows)

        return await self._run(work)

    async def get_step(self, step_id: UUID | str) -> StepRow:
        return await self._run(lambda: self._fetch_step(str(step_id)))

    def _fetch_step(self, step_id: str) -> StepRow:
        row = self._db.execute(
            "SELECT * FROM workflow_steps WHERE id = ?", (step_id,)
        ).fetchone()
        if row is None:
            raise StoreError(
                ConsultErrorCode.SESSION_NOT_FOUND, f"no workflow step `{step_id}` in this store"
            )
        return StepRow(**dict(row))

    async def steps(self, workflow_id: UUID | str) -> list[StepRow]:
        def work() -> list[StepRow]:
            rows = self._db.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY sequence",
                (str(workflow_id),),
            ).fetchall()
            return [StepRow(**dict(row)) for row in rows]

        return await self._run(work)

    async def consume_step_token(
        self, step_id: UUID | str, workflow_id: UUID | str, token: str
    ) -> None:
        """Spend the token and start the step, in one statement.

        The status predicate is what makes two simultaneous runs launch one step
        instead of two paid ones; nulling the hash is what stops a third call
        replaying the same token later. `workflow_id` is in the `WHERE` clause so a
        token cannot be redeemed against a step of some other workflow.

        The workflow's own status is checked here too, in the same statement. The
        service reads it earlier, but between that read and this write a `cancel` can
        land -- and the step would start anyway, spending money and a token on a
        workflow that is already `cancelled`, with nothing left to record it against.
        """

        def work() -> None:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute(
                    "UPDATE workflow_steps SET status = 'running', confirm_token_sha = NULL, "
                    "updated_at = ? WHERE id = ? AND workflow_id = ? AND status = 'planned' "
                    "AND confirm_token_sha = ? AND EXISTS (SELECT 1 FROM workflow_runs w "
                    f"WHERE w.id = workflow_steps.workflow_id AND w.status NOT IN ({_TERMINAL_SQL})"
                    ")",
                    (_now(), str(step_id), str(workflow_id), _sha256(token)),
                )
                taken = cursor.rowcount == 1
            except Exception:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

            if not taken:
                raise StoreError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"step `{step_id}` is not waiting on that confirmation; the token was "
                    "already used, the step has moved on, or the workflow itself has "
                    "ended. Read `orchestrator_workflow_status` before planning it again",
                )

        await self._run(work)

    async def finish_step(
        self,
        step_id: UUID | str,
        status: StepStatus,
        *,
        output: dict[str, Any] | None = None,
        review_id: str | None = None,
        raw_patch_sha256: str | None = None,
        recovery_written: bool | None = None,
        reported_by: ReportedBy | None = None,
    ) -> None:
        """Record what the step produced.

        `output` is scrubbed on the way in like every other stored text. For a patch
        that means the stored copy is an audit copy and nothing else. The raw text is
        kept in a private recovery file until application, and `raw_patch_sha256` ties
        the two together. Never reapply what comes back out of this column.

        `status = 'running'` in the WHERE clause is what stops a writer that has lost
        the right to write. `consume_step_token` is the only thing that makes a step
        `running`, so every legitimate caller passes -- but a step that was reaped as
        `abandoned` or cancelled underneath a slow run is no longer this caller's to
        finish, and without the guard the late write would resurrect it as `done`,
        lease columns cleared, with nothing said.
        """

        def work() -> None:
            cursor = self._db.execute(
                "UPDATE workflow_steps SET status = ?, output_json = COALESCE(?, output_json), "
                "review_id = COALESCE(?, review_id), "
                "raw_patch_sha256 = COALESCE(?, raw_patch_sha256), "
                "recovery_written = COALESCE(?, recovery_written), "
                "reported_by = COALESCE(?, reported_by), lease_holder = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? AND status = 'running'",
                (
                    status,
                    canonical(scrub_json(output)) if output is not None else None,
                    review_id,
                    raw_patch_sha256,
                    recovery_written,
                    reported_by,
                    _now(),
                    str(step_id),
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"step `{step_id}` is no longer running, so its result was not "
                    "recorded. It was cancelled or timed out while the work was still "
                    "in flight; read `orchestrator_workflow_status` and plan it again",
                )

        await self._run(work)

    async def reap_abandoned(self, workflow_id: UUID | str) -> int:
        """Resolve steps whose lease outlived the process that took it.

        A `running` row with an expired lease is a step whose process is gone. Left
        alone it would wedge the workflow behind a lease nobody holds; marked
        `abandoned`, the step can simply be planned again. The workflow's own status
        is untouched, because it never advanced.
        """

        def work() -> int:
            cursor = self._db.execute(
                "UPDATE workflow_steps SET status = 'abandoned', lease_holder = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE workflow_id = ? AND status = 'running' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at <= ?",
                (_now(), str(workflow_id), time.time()),
            )
            return cursor.rowcount

        return await self._run(work)

    # --- leases -------------------------------------------------------------

    @asynccontextmanager
    async def lease(self, workflow_id: UUID | str, step_id: UUID | str, ttl_s: float):
        """Hold the right to run one workflow's next step.

        Taken on the *workflow*, not the step: two different steps of one workflow
        running at once would each read a state the other is about to change. The
        lease columns live on the step row so an expired one names what was in
        flight, which is what `reap_abandoned` needs.
        """
        token = f"pid-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        await self._run(lambda: self._acquire(str(workflow_id), str(step_id), ttl_s, token))
        held = LEASE_HELD.set(str(workflow_id))
        try:
            async with _renewing_lease(
                self._run,
                lambda: self._renew(str(step_id), ttl_s, token),
                lambda: self._release(str(step_id), token),
                ttl_s,
            ):
                yield
        finally:
            LEASE_HELD.reset(held)

    def _acquire(self, workflow_id: str, step_id: str, ttl_s: float, token: str) -> None:
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            # No exemption for `step_id` itself. Exempting it meant a second caller on
            # the same step overwrote the first one's `lease_holder`, then failed on the
            # spent token, and on the way out `_release` matched its own token and
            # cleared the lease -- leaving the first still running with no lease at all,
            # so a *different* step of the same workflow could then start beside it.
            held = db.execute(
                "SELECT id FROM workflow_steps WHERE workflow_id = ? AND lease_expires_at > ?",
                (workflow_id, now),
            ).fetchone()
            if held is None:
                db.execute(
                    "UPDATE workflow_steps SET lease_holder = ?, lease_expires_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (token, now + ttl_s, _now(), step_id),
                )
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

        if held is not None:
            raise StoreError(
                ConsultErrorCode.SESSION_BUSY,
                f"workflow `{workflow_id}` already has step `{held[0]}` in flight; wait for "
                "it to finish before starting the next one",
            )

    def _release(self, step_id: str, token: str) -> None:
        self._db.execute(
            "UPDATE workflow_steps SET lease_holder = NULL, lease_expires_at = NULL, "
            "updated_at = ? WHERE id = ? AND lease_holder = ?",
            (_now(), step_id, token),
        )

    def _renew(self, step_id: str, ttl_s: float, token: str) -> bool:
        cursor = self._db.execute(
            "UPDATE workflow_steps SET lease_expires_at = ?, updated_at = ? "
            "WHERE id = ? AND lease_holder = ?",
            (time.time() + ttl_s, _now(), step_id, token),
        )
        if cursor.rowcount == 1:
            return True
        row = self._db.execute(
            "SELECT status, lease_holder FROM workflow_steps WHERE id = ?", (step_id,)
        ).fetchone()
        if row is not None and row["status"] != "running" and row["lease_holder"] is None:
            # finish_step clears ownership before the service returns through the
            # lease context. That is completion, not ownership loss.
            return False
        raise StoreError(
            ConsultErrorCode.SESSION_BUSY,
            f"workflow step `{step_id}` lost its execution lease while it was running",
        )

    # --- deletion -----------------------------------------------------------
    #
    # The only way a workflow's rows leave this database. Its consultations are
    # excluded from every consultation delete path and its reviews are refused by
    # `refuse_workflow_owned`, both for the same reason: half a workflow is worse than
    # all of it, because a step pointing at a row that is gone still reads as intact.
    # So the whole thing goes together or none of it does.

    async def delete_workflow(self, workflow_id: UUID | str) -> int:
        """Delete one workflow, its steps, its consultations and its reviews."""
        return await self._run(lambda: self._delete([str(workflow_id)]))

    async def request_delete_all_workflows(
        self, ttl_s: float = DELETE_CONFIRM_TTL_S
    ) -> tuple[str, int]:
        """Snapshot every workflow, and hand back a token for exactly those.

        The ids are recorded, not just the count, for the reason
        `ReviewStore.request_delete_all` gives: confirming against a fresh `SELECT`
        would take a workflow started in the meantime, which nobody was shown.

        Running workflows are snapshotted rather than filtered out. A review is
        refused for being `running` and that is a state it leaves on its own; a
        workflow sits in `coding` until somebody advances it, so quietly dropping
        those would report a number that stayed wrong until the user cancelled them.
        The refusal below names the workflow and what to do about it.
        """

        def work() -> tuple[str, int]:
            db = self._db
            db.execute(
                "DELETE FROM workflow_delete_confirmations WHERE expires_at <= ?", (time.time(),)
            )
            ids = [row[0] for row in db.execute("SELECT id FROM workflow_runs ORDER BY id")]
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO workflow_delete_confirmations (token_sha, workflow_ids_json, "
                "displayed_count, created_at, expires_at) VALUES (?,?,?,?,?)",
                (_sha256(token), canonical(ids), len(ids), _now(), time.time() + ttl_s),
            )
            return token, len(ids)

        return await self._run(work)

    async def delete_all_workflows(self, token: str) -> int:
        """Delete the approved snapshot, and nothing that arrived after it."""
        return await self._run(lambda: self._delete([], confirmation_sha=_sha256(token)))

    def _delete(self, roots: list[str], *, confirmation_sha: str | None = None) -> int:
        """Refuse what is still moving, then remove the tree in one transaction.

        Order is `PRAGMA foreign_keys=ON` order, outside in: the reviews and their
        consultations through `delete_tree`, then the workflow's own consultations,
        then the steps that reference `workflow_runs`, then the runs.

        What goes is what the workflow owns and only that. Nothing here widens the
        set beyond the snapshot the caller approved, which is the whole point of
        approving ids rather than a count: a row that did not exist when the token
        was issued cannot be swept up when it is spent.
        """
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            if confirmation_sha is not None:
                roots = _approved(db, confirmation_sha)
            if not roots:
                db.execute("COMMIT")
                return 0
            marks = ",".join("?" * len(roots))
            tree = [
                row[0]
                for row in db.execute(
                    f"SELECT id FROM workflow_runs WHERE id IN ({marks})", roots
                )
            ]
            if not tree:
                db.execute("COMMIT")
                return 0
            marks = ",".join("?" * len(tree))

            _refuse_live(db, tree, marks)

            reviews = [
                row[0]
                for row in db.execute(
                    f"SELECT id FROM reviews WHERE workflow_id IN ({marks})", tree
                )
            ]
            if reviews:
                # Through the review layer's own teardown rather than a second copy of
                # it here: `review_consultations` and the reviewers' consultations are
                # its business, not this table's.
                #
                # These reviews and no others. A recheck a caller made of one carries
                # `workflow_id = NULL` -- `orchestrator_review` has no `workflow_id`
                # argument and nothing stamps one on -- so it is the caller's review,
                # not the workflow's, and deleting a workflow must not take it.
                # `detach_unapproved` is what makes that safe: it clears the child's
                # `parent_review_id` before the parent goes, so the deferred foreign
                # key commits and the recheck survives as a root of its own.
                delete_tree(db, reviews, detach_unapproved=True)

            consultations = [
                row[0]
                for row in db.execute(
                    f"SELECT id FROM consultations WHERE workflow_id IN ({marks})", tree
                )
            ]
            if consultations:
                held = ",".join("?" * len(consultations))
                for table in ("consultation_turns", "routing_decisions", "consultation_leases"):
                    db.execute(
                        f"DELETE FROM {table} WHERE consultation_id IN ({held})", consultations
                    )
                db.execute(f"DELETE FROM consultations WHERE id IN ({held})", consultations)

            db.execute(f"DELETE FROM workflow_steps WHERE workflow_id IN ({marks})", tree)
            db.execute(f"DELETE FROM workflow_runs WHERE id IN ({marks})", tree)
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        for workflow_id in tree:
            remove_workflow_patches_now(workflow_id)
        return len(tree)
