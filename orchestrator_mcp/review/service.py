"""What composes a review out of the parts.

A review is N consultations over one approved payload. This module owns the
handshake (plan, then run), the approval checks that stand between them, and the
one rule the layer exists for: nothing leaves this machine until a plan has been
shown and a token spent against it.

The same two rules as `ConsultService` shape every method below. Every public
path returns a `ReviewResponse`, because an exception crossing the MCP boundary
leaves the caller with a protocol error and no `review_id` -- the one thing it
needs to retry, cancel or delete. And a failure never carries findings.

The consult path underneath is built with `scrub_json` as its storage sanitizer,
so a credential in the material reaches the reviewer's CLI (a review of a
redacted file is a review of a different file) and never reaches our database.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets as secrets_mod
import time
from typing import Any
from uuid import UUID, uuid4

from ..contract import MAX_ERROR_CHARS, Usage, redact, scrub_json, secret_lines
from ..consult.config import ConsultConfig
from ..consult.contract import (
    ConsultAgentsResponse,
    ConsultError,
    ConsultResponse,
    Runtime,
    SourceMode,
)
from ..consult.errors import ConsultErrorCode
from ..consult.service import ConsultService
from ..consult.store import ConsultStore, StoreError
from .contract import (
    FIX_STEPS,
    MAX_FINDINGS,
    MAX_FIX_ROUNDS,
    MAX_LIST_ITEMS,
    MAX_SECRET_HITS,
    REVIEWER_INSTRUCTIONS,
    SEVERITY_ORDER,
    Finding,
    FixPlan,
    FixRound,
    MaterialItem,
    Outcome,
    RawReviewMaterial,
    ReviewListing,
    ReviewerResult,
    ReviewerSnapshot,
    ReviewMode,
    ReviewPlan,
    ReviewResponse,
    ReviewStatus,
    ReviewSummary,
    SecretHit,
    missing_criticals,
)
from .store import REVIEW_LEASE_SLACK_S, ReviewStore, _now, canonical, sha256

# Statuses a review may be retried from. `failed` is here on purpose: a review
# where every reviewer was offline is exactly the one worth running again.
RETRYABLE = ("failed", "awaiting_synthesis")
CANCELLABLE = ("pending", "running", "awaiting_synthesis", "failed")
# Statuses a fix round can be recorded against: the ones that have findings to fix.
# A `failed` review has none by invariant, and a `pending` one has sent nothing.
FIXABLE = ("awaiting_synthesis", "complete")

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


class ReviewService:
    """Reviews over a `ConsultService` that shares this store.

    One `ConsultStore` for both layers: a review and the consultations under it
    are written by the same serialized worker, and deletion can remove them in one
    transaction rather than hoping two connections agree.
    """

    def __init__(
        self,
        config: ConsultConfig,
        host_runtime: Runtime,
        store: ConsultStore | None = None,
    ) -> None:
        self.config = config
        self.host_runtime = host_runtime
        shared = store or ConsultStore(config.database_path, config.store_full_content)
        self.consult = ConsultService(config, host_runtime, store=shared, store_sanitizer=scrub_json)
        self.store = ReviewStore(shared)
        # Live batches, by review id. `cancel` awaits these so a cancel followed by a
        # delete in the same process is ordered rather than racing; another process's
        # children are out of reach, which is what the execution lease covers instead.
        self._tasks: dict[str, list[asyncio.Task[Any]]] = {}

    async def open(self) -> ReviewService:
        await self.consult.open()
        return self

    async def close(self) -> None:
        await self.consult.close()

    # --- plan ---------------------------------------------------------------

    async def plan(
        self,
        mode: ReviewMode = "standard",
        goal: str = "",
        material: list[dict[str, Any]] | None = None,
        context: str | None = None,
        web: bool = False,
        reviewers: list[str] | None = None,
        parent_review_id: UUID | str | None = None,
        host_model: str | None = None,
    ) -> ReviewResponse:
        """Describe what would be sent. Sends nothing.

        Writes a `pending` row holding the redacted material and the sha256 of a
        one-time token, and hands the token back once. Nothing here spawns a
        subprocess, so a caller can plan freely and abandon it.
        """
        started = time.perf_counter()
        review_id = uuid4()
        try:
            await self.open()
            return await self._plan(
                started, review_id, mode, goal, material or [], context, web,
                reviewers, parent_review_id, host_model,
            )
        except (StoreError, ValueError) as exc:
            code = exc.code if isinstance(exc, StoreError) else ConsultErrorCode.INVALID_REQUEST
            return _failed(review_id, mode, "failed", code, str(exc), started)
        except Exception as exc:
            # The type and nothing else: these messages are quoted back to a caller
            # that may not be the operator, and an operational exception carries
            # paths and occasionally a credential.
            return _failed(
                review_id, mode, "failed", ConsultErrorCode.TRANSPORT_ERROR,
                f"the review failed inside the orchestrator ({type(exc).__name__})", started,
            )

    async def _plan(
        self,
        started: float,
        review_id: UUID,
        mode: ReviewMode,
        goal: str,
        material: list[dict[str, Any]],
        context: str | None,
        web: bool,
        reviewers: list[str] | None,
        parent_review_id: UUID | str | None,
        host_model: str | None,
    ) -> ReviewResponse:
        if not goal.strip():
            raise ValueError("a review needs a goal saying what to look at")

        snapshots = self._reviewer_snapshots(mode, reviewers)
        manifest = [MaterialItem(**item) for item in material]

        hits = [SecretHit(field="goal", line=n) for n in secret_lines(goal)]
        hits += [SecretHit(field="context", line=n) for n in secret_lines(context or "")]

        # Hashed over the *redacted* text, which is what the row stores. Hashing the
        # original would make the check unrecomputable exactly when a secret was
        # present -- the case it most needs to work in.
        safe_goal, safe_context = redact(goal), redact(context) if context else None
        material_sha = sha256(
            canonical(
                {
                    "mode": mode,
                    "goal": safe_goal,
                    "context": safe_context,
                    "material": [m.model_dump(mode="json") for m in manifest],
                    "web": bool(web),
                    "parent": str(parent_review_id) if parent_review_id else None,
                    "reviewers": [s.model_dump(mode="json") for s in snapshots],
                }
            )
        )
        raw_sha = sha256(canonical({"goal": goal, "context": context}))
        token = secrets_mod.token_urlsafe(32)

        await self.store.create_review(
            review_id=review_id,
            mode=mode,
            goal=goal,  # scrubbed by the store, at the insert
            context=context,
            material=[m.model_dump(mode="json") for m in manifest],
            material_sha256=material_sha,
            raw_sha256=raw_sha,
            reviewer_snapshot=[s.model_dump(mode="json") for s in snapshots],
            confirm_token=token,
            secret_hits=[h.model_dump(mode="json") for h in hits[:MAX_SECRET_HITS]],
            web_requested=web,
            parent_review_id=parent_review_id,
        )

        seen: dict[str, int] = {}
        for snapshot in snapshots:
            seen[snapshot.model] = seen.get(snapshot.model, 0) + 1

        return ReviewResponse(
            review_id=review_id,
            mode=mode,
            status="pending",
            plan=ReviewPlan(
                mode=mode,
                reviewers=snapshots,
                material=manifest,
                goal_chars=len(goal),
                context_chars=len(context or ""),
                material_sha256=material_sha,
                web_requested=web,
                expected_requests=len(snapshots),
                secret_hits=hits[:MAX_SECRET_HITS],
                duplicate_models=sorted(m for m, n in seen.items() if n > 1),
                host_model_conflict=next(
                    (s.model for s in snapshots if host_model and s.model == host_model), None
                ),
                confirm_token=token,
            ),
            latency_ms=_ms(started),
        ).check_invariants()

    def _reviewer_snapshots(
        self, mode: ReviewMode, reviewers: list[str] | None
    ) -> list[ReviewerSnapshot]:
        """Who this review would go to, as configured right now.

        A reviewer on the host's own runtime is refused here rather than left to the
        router: approving a send the router is guaranteed to reject is a preview that
        lies. A reviewer on the same *model* but a different runtime stays allowed and
        turns into `host_model_conflict` instead.
        """
        if self.config.review is None and reviewers is None:
            raise ValueError(
                "no `review:` block is configured; add `reviewers:` and `deep_reviewers:` "
                "to the consult config or the dashboard's agents file, or name reviewers "
                "on this call"
            )
        if reviewers is None:
            assert self.config.review is not None
            chosen = (
                self.config.review.deep_reviewers
                if mode == "deep"
                else self.config.review.reviewers
            )
        else:
            chosen = reviewers
        if not chosen:
            raise ValueError(f"`{mode}` review has no reviewers configured")

        snapshots = []
        for agent_id in chosen:
            # Same three checks as boot, so a reviewer refused there cannot arrive
            # later through an explicit list.
            self.config.check_reviewer(agent_id, "reviewers")
            agent = self.config.agents[agent_id]
            if agent.runtime == self.host_runtime:
                raise ValueError(
                    f"`{agent_id}` runs this host's own runtime ({self.host_runtime}) and "
                    "cannot review its own work; name a different agent"
                )
            snapshots.append(
                ReviewerSnapshot(
                    agent_id=agent_id,
                    runtime=agent.runtime,
                    model=agent.model,
                    web_search=agent.web_search,
                )
            )
        return snapshots

    def _verify_snapshot(self, review) -> list[ReviewerSnapshot]:
        """Refuse if a reviewer moved since the plan was approved.

        An agent id is a label, and between plan and run a config edit or a dashboard
        save can point it at another runtime or model. The consultation-level check
        only engages once a consultation exists, which is already too late.
        """
        approved = [ReviewerSnapshot(**s) for s in json.loads(review.reviewer_snapshot_json)]
        for snapshot in approved:
            agent = self.config.agents.get(snapshot.agent_id)
            if agent is None:
                raise ValueError(
                    f"`{snapshot.agent_id}` was approved for this review but is no longer "
                    "configured; plan the review again"
                )
            live = ReviewerSnapshot(
                agent_id=agent.agent_id,
                runtime=agent.runtime,
                model=agent.model,
                web_search=agent.web_search,
            )
            if live != snapshot:
                moved = [
                    field
                    for field in ("runtime", "model", "web_search")
                    if getattr(live, field) != getattr(snapshot, field)
                ]
                raise ValueError(
                    f"`{snapshot.agent_id}` changed since you approved this review "
                    f"({', '.join(moved)}); plan it again so the preview matches what "
                    "would be sent"
                )
        return approved

    # --- run ----------------------------------------------------------------

    async def run(
        self,
        review_id: UUID | str,
        confirm_token: str,
        host_findings: list[str] | None = None,
        secrets: str = "mask",
        raw: dict[str, Any] | None = None,
    ) -> ReviewResponse:
        """Spend the token and ask every approved reviewer, in parallel.

        Stops at `awaiting_synthesis`. External models replying is not a finished
        review -- the host AI's combined conclusion is the product, and
        `orchestrator_finalize_review` is the only path to `complete`.
        """
        started = time.perf_counter()
        return await self._guard(
            review_id, started, lambda review: self._run(started, review, confirm_token,
                                                         host_findings, secrets, raw)
        )

    async def _run(
        self,
        started: float,
        review,
        confirm_token: str,
        host_findings: list[str] | None,
        secrets: str,
        raw: dict[str, Any] | None,
    ) -> ReviewResponse:
        if secrets not in ("mask", "send_as_is"):
            raise ValueError("`secrets` must be `mask` or `send_as_is`")
        approved = self._verify_snapshot(review)
        findings = [f for f in (host_findings or []) if f.strip()][:MAX_LIST_ITEMS]
        if review.mode == "deep" and not findings:
            raise ValueError(
                "a deep review is the host AI's own opinion plus the reviewers'; pass "
                "`host_findings` with what you found before asking anyone else"
            )

        goal, context = review.goal, review.context
        if secrets == "send_as_is":
            material = RawReviewMaterial(**(raw or {}))
            # Verified *before* the token is spent: a typo must not burn a one-time
            # token and force a whole new plan.
            if sha256(canonical({"goal": material.goal, "context": material.context})) != review.raw_sha256:
                raise ValueError(
                    "the `raw` material does not match what was planned; send the exact "
                    "goal and context you planned with, or use `secrets=\"mask\"`"
                )
            goal, context = material.goal, material.context

        await self.store.consume_confirm_token(review.id, confirm_token)
        if findings:
            await self.store.save_host_findings(review.id, findings)

        results = await self._batch(review, [s.agent_id for s in approved], context, goal)
        return await self._settle(started, review.id, results, findings)

    async def retry(
        self, review_id: UUID | str, agent_ids: list[str] | None = None
    ) -> ReviewResponse:
        """Re-run the reviewers that failed. No new approval: the material has not
        changed, and the stored hash is what says so."""
        started = time.perf_counter()
        return await self._guard(
            review_id, started, lambda review: self._retry(started, review, agent_ids)
        )

    async def _retry(self, started: float, review, agent_ids: list[str] | None) -> ReviewResponse:
        self._verify_snapshot(review)
        rows = {row.agent_id: row for row in await self.store.reviewer_rows(review.id)}
        failed = [aid for aid, row in rows.items() if row.status != "ok"]
        wanted = failed if agent_ids is None else [a for a in agent_ids if a in failed]
        if not wanted:
            raise ValueError(
                "nothing to retry: every reviewer on this review already answered"
            )
        if not await self.store.transition(review.id, "running", RETRYABLE):
            raise ValueError(
                f"review `{review.id}` is `{review.status}` and cannot be retried from there"
            )

        host = json.loads(review.host_findings_json) if review.host_findings_json else []
        results = await self._batch(review, wanted, review.context, review.goal, rows)
        return await self._settle(started, review.id, results, host)

    async def _batch(
        self,
        review,
        agent_ids: list[str],
        context: str | None,
        goal: str,
        rows: dict[str, Any] | None = None,
    ) -> dict[str, ReviewerResult]:
        """Ask each reviewer, holding the execution lease for the whole batch.

        The lease is what a delete checks. Status alone cannot carry this: a cancel
        moves the review out of `running` while another process's subprocesses keep
        going, and a delete landing then would leave their consultations behind with
        no review pointing at them.
        """
        prompt = f"{goal}\n\n{REVIEWER_INSTRUCTIONS}"
        source_mode = SourceMode.WEB if review.web_requested else SourceMode.AUTO
        ttl = self.config.timeout_s + REVIEW_LEASE_SLACK_S

        async def one(agent_id: str) -> ReviewerResult:
            prior = (rows or {}).get(agent_id)
            # Reuse the failed attempt's consultation where there was one, so the retry
            # is another turn in the same session rather than an orphan the delete
            # would miss. A failure before any consultation existed has none.
            existing = getattr(prior, "consultation_id", None)
            response = await self.consult.consult(
                capability="review",
                target_agent=agent_id,
                prompt=prompt,
                context=context,
                source_mode=source_mode,
                consultation_id=UUID(existing) if existing else None,
            )
            result = self._to_result(agent_id, response)
            # Persisted before returning, so a cancel or a crash never loses a
            # reviewer that already answered.
            await self.store.record_reviewer_result(
                review.id,
                agent_id,
                status="ok" if result.ok else "failed",
                consultation_id=result.consultation_id,
                findings=[f.model_dump(mode="json") for f in result.findings] or None,
                answer=result.answer,
                error_code=result.error.code.value if result.error else None,
            )
            return result

        async with self.store.lease(review.id, ttl_s=ttl):
            tasks = [asyncio.create_task(one(agent_id)) for agent_id in agent_ids]
            self._tasks[str(review.id)] = tasks
            try:
                done = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._tasks.pop(str(review.id), None)

        out: dict[str, ReviewerResult] = {}
        for agent_id, result in zip(agent_ids, done, strict=True):
            if isinstance(result, ReviewerResult):
                out[agent_id] = result
            elif isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result  # CancelledError: no caller left to hand an envelope to
            else:
                out[agent_id] = ReviewerResult(
                    agent_id=agent_id,
                    ok=False,
                    error=ConsultError(
                        code=ConsultErrorCode.TRANSPORT_ERROR,
                        message=f"the reviewer failed inside the orchestrator "
                        f"({type(result).__name__})",
                        agent_id=agent_id,
                    ),
                )
        return out

    async def _settle(
        self, started: float, review_id: str, fresh: dict[str, ReviewerResult], host: list[str]
    ) -> ReviewResponse:
        """Recompute the outcome over *every* reviewer and move the review, if it
        is still ours to move.

        Over every reviewer, never over the batch that just finished: a retry may run
        one reviewer out of three, and counting only that batch would demote a review
        with two good answers to `none` and lose real findings.
        """
        results = await self._results(review_id, fresh)
        answered = sum(1 for r in results if r.ok)
        outcome: Outcome = "all" if answered == len(results) else "some" if answered else "none"
        status: ReviewStatus = "awaiting_synthesis" if answered else "failed"

        moved = await self.store.transition(review_id, status, ("running",), outcome)
        current = await self.store.get_review(review_id)
        if not moved:
            # A cancel landed while the reviewers were finishing. Their rows are
            # already saved; the cancel stands.
            status, outcome = current.status, current.outcome  # type: ignore[assignment]

        return ReviewResponse(
            review_id=UUID(str(review_id)),
            mode=current.mode,  # type: ignore[arg-type]
            status=status,
            outcome=outcome,
            results=results,
            host_findings=host,
            usage=_total(results),
            latency_ms=_ms(started),
        ).check_invariants()

    async def _results(
        self, review_id: str, fresh: dict[str, ReviewerResult] | None = None
    ) -> list[ReviewerResult]:
        """Every reviewer's latest outcome: this batch's, plus the persisted rest.

        The persisted half is reconstructed from the row, which under
        `store_full_content: false` has no answer and no findings -- the shape
        survives, the bodies do not, which is what that setting means.
        """
        fresh = fresh or {}
        out = []
        for row in await self.store.reviewer_rows(review_id):
            if row.agent_id in fresh:
                out.append(fresh[row.agent_id])
                continue
            findings = json.loads(row.findings_json) if row.findings_json else []
            out.append(
                ReviewerResult(
                    agent_id=row.agent_id,
                    ok=row.status == "ok",
                    consultation_id=UUID(row.consultation_id) if row.consultation_id else None,
                    findings=[Finding(**f) for f in findings],
                    findings_parsed=bool(findings),
                    answer=row.answer,
                    error=(
                        ConsultError(
                            code=ConsultErrorCode(row.error_code),
                            message="recorded on an earlier attempt",
                            agent_id=row.agent_id,
                        )
                        if row.status != "ok" and row.error_code
                        else None
                    ),
                )
            )
        return out

    def _to_result(self, agent_id: str, response: ConsultResponse) -> ReviewerResult:
        if not response.ok or response.content is None:
            return ReviewerResult(
                agent_id=agent_id,
                ok=False,
                consultation_id=response.consultation_id,
                error=response.error,
            )
        content = response.content
        findings, parsed, dropped = _parse_findings(agent_id, content.answer)
        return ReviewerResult(
            agent_id=agent_id,
            ok=True,
            consultation_id=response.consultation_id,
            findings=findings,
            findings_parsed=parsed,
            findings_truncated=dropped,
            answer=content.answer,
            assumptions=content.assumptions[:MAX_LIST_ITEMS],
            uncertainties=content.uncertainties[:MAX_LIST_ITEMS],
            follow_up_questions=content.follow_up_questions[:MAX_LIST_ITEMS],
            sources=content.sources[:MAX_LIST_ITEMS],
            usage=response.usage,
        )

    # --- finish -------------------------------------------------------------

    async def finalize(self, review_id: UUID | str, summary: dict[str, Any]) -> ReviewResponse:
        """Record the host AI's synthesis. The only path to `complete`."""
        started = time.perf_counter()
        return await self._guard(
            review_id, started, lambda review: self._finalize(started, review, summary)
        )

    async def _finalize(self, started: float, review, summary: dict[str, Any]) -> ReviewResponse:
        written = ReviewSummary(**summary)
        results = await self._results(review.id)

        # Checked, not merely asked for: the reason to consult more than one reviewer
        # is that a lone dissenting Critical survives the synthesis, and a rule stated
        # only in a prompt is a rule nothing enforces.
        dropped = missing_criticals(results, written)
        if dropped:
            raise ValueError(
                f"the summary accounts for no Critical finding {', '.join(dropped)}. Every "
                "Critical must be referenced by some combined finding through "
                "`source_finding_ids`, even one only a single reviewer raised and the "
                "others contradict -- disagree with it in the row, do not drop it"
            )

        # Citations carried through rather than lost in synthesis: a web-mode
        # reviewer's sources are most of what made its answer checkable.
        if not written.citations:
            cited = {(s.title, s.locator, s.source_type): s for r in results for s in r.sources}
            written = written.model_copy(update={"citations": list(cited.values())[:MAX_LIST_ITEMS]})

        await self.store.save_summary(review.id, written.model_dump(mode="json"))
        if not await self.store.transition(review.id, "complete", ("awaiting_synthesis",)):
            raise ValueError(
                f"review `{review.id}` is `{review.status}`; only a review waiting on "
                "synthesis can be completed"
            )
        return ReviewResponse(
            review_id=UUID(review.id),
            mode=review.mode,
            status="complete",
            outcome=review.outcome,
            results=results,
            summary=written,
            latency_ms=_ms(started),
        ).check_invariants()

    # --- fixing -------------------------------------------------------------

    async def fix_plan(
        self, review_id: UUID | str, finding_ids: list[str] | None = None
    ) -> ReviewResponse:
        """The selected findings and the steps around editing them. Changes nothing.

        Two calls rather than one that switches on its arguments, for the same
        reason `orchestrator_review` and `orchestrator_review_run` are two: "what am I about to do" and "here
        is what I did" are different moments, and one schema covering both reads to
        a calling model as everything being optional.
        """
        started = time.perf_counter()
        return await self._guard(
            review_id, started, lambda review: self._fix_plan(started, review, finding_ids or [])
        )

    async def _fix_plan(
        self, started: float, review, finding_ids: list[str]
    ) -> ReviewResponse:
        self._fixable(review)
        # Built on the ordinary read rather than assembled here: most fixing happens
        # after the synthesis, and an envelope stamped `complete` that omits the
        # summary is one the invariants refuse -- rightly.
        response = await self._get(started, review)
        chosen = self._selected(response.results, finding_ids)
        picked = {f.finding_id for f in chosen}
        return response.model_copy(
            update={
                "fix_plan": FixPlan(
                    findings=chosen,
                    # Named here rather than found again by the recheck: a selection
                    # that leaves out a Critical is the synthesis failure one stage
                    # later, and the host should be told before it starts editing.
                    criticals_omitted=[
                        f.finding_id
                        for r in response.results
                        for f in r.findings
                        if f.severity == "critical" and f.finding_id not in picked
                    ][:MAX_FINDINGS],
                    steps=FIX_STEPS,
                )
            }
        ).check_invariants()

    async def record_fix_round(
        self,
        review_id: UUID | str,
        finding_ids: list[str],
        outcome: str,
        notes: str = "",
    ) -> ReviewResponse:
        """Log what a round of fixing did. Records a claim; verifies nothing.

        This server does not edit files or run commands, so it cannot know whether
        the fixes landed -- the round is the host AI's own account, kept beside the
        findings it refers to so a later recheck can be read against it.
        """
        started = time.perf_counter()
        return await self._guard(
            review_id,
            started,
            lambda review: self._record_fix_round(
                started, review, finding_ids, outcome, notes
            ),
        )

    async def _record_fix_round(
        self, started: float, review, finding_ids: list[str], outcome: str, notes: str
    ) -> ReviewResponse:
        self._fixable(review)
        self._selected(await self._results(review.id), finding_ids)
        if len(json.loads(review.fix_rounds_json or "[]")) >= MAX_FIX_ROUNDS:
            raise ValueError(
                f"review `{review.id}` already has {MAX_FIX_ROUNDS} fix rounds; plan a "
                "recheck as a new review with `parent_review_id` rather than fixing the "
                "same one again"
            )
        round_ = FixRound(
            finding_ids=finding_ids[:MAX_FINDINGS],
            outcome=outcome,  # type: ignore[arg-type]
            # The one part of a round that is prose. `store_full_content: false` means
            # prose does not go on disk; the ids and the outcome are shape and stay.
            notes=notes if self.store.keeps_content else "",
            recorded_at=_now(),
        )
        await self.store.append_fix_round(review.id, round_.model_dump(mode="json"))
        return await self._get(started, await self.store.get_review(review.id))

    def _fixable(self, review) -> None:
        if review.status not in FIXABLE:
            raise ValueError(
                f"review `{review.id}` is `{review.status}`; fixes are recorded against a "
                "review that has findings, so run it and read the results first"
            )

    def _selected(self, results: list[ReviewerResult], finding_ids: list[str]) -> list[Finding]:
        """Resolve ids to findings, refusing ones no reviewer raised.

        An id that matches nothing is a fix round pointing at nobody's finding, which
        is worse than no record at all. The exception is an operator who does not keep
        content: the findings were never written down, so there is nothing to resolve
        against and nothing to refuse on.
        """
        by_id = {f.finding_id: f for r in results for f in r.findings}
        if not by_id and not self.store.keeps_content:
            return []
        unknown = [fid for fid in finding_ids if fid not in by_id]
        if unknown:
            raise ValueError(
                f"no reviewer raised {', '.join(unknown)} on this review; the ids come "
                "from `results[].findings[].finding_id`"
            )
        return [by_id[fid] for fid in finding_ids]

    async def cancel(self, review_id: UUID | str) -> ReviewResponse:
        """Stop a review, and wait for this process's reviewers to finish.

        A review running under a *second* server process is marked, but its
        subprocesses are not signalled -- nothing here can reach them. What is
        guaranteed is that they cannot write into a deleted review: the execution
        lease blocks the delete until they stop or it expires.
        """
        started = time.perf_counter()
        return await self._guard(review_id, started, lambda review: self._cancel(started, review))

    async def _cancel(self, started: float, review) -> ReviewResponse:
        if not await self.store.transition(review.id, "cancelled", CANCELLABLE):
            raise ValueError(
                f"review `{review.id}` is `{review.status}` and cannot be cancelled"
            )
        # Awaited rather than cancelled: the subprocesses are already running and each
        # task records its reviewer's row before returning. Awaiting is what makes a
        # cancel followed by a delete in this process ordered instead of racing.
        tasks = self._tasks.get(str(review.id), [])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return ReviewResponse(
            review_id=UUID(review.id),
            mode=review.mode,
            status="cancelled",
            outcome=review.outcome,
            results=await self._results(review.id),
            latency_ms=_ms(started),
        ).check_invariants()

    # --- read-only ----------------------------------------------------------

    async def get(self, review_id: UUID | str) -> ReviewResponse:
        started = time.perf_counter()
        return await self._guard(review_id, started, lambda review: self._get(started, review))

    async def _get(self, started: float, review) -> ReviewResponse:
        return ReviewResponse(
            review_id=UUID(review.id),
            mode=review.mode,
            status=review.status,
            outcome=review.outcome,
            results=await self._results(review.id),
            host_findings=json.loads(review.host_findings_json or "[]"),
            summary=(
                ReviewSummary(**json.loads(review.summary_json)) if review.summary_json else None
            ),
            fix_rounds=_fix_rounds(review),
            rechecks=await self.store.recheck_ids(review.id),
            latency_ms=_ms(started),
        )

    async def list(self, limit: int = 20) -> list[ReviewListing]:
        await self.open()
        return [
            ReviewListing(
                review_id=r.id,
                parent_review_id=r.parent_review_id,
                mode=r.mode,  # type: ignore[arg-type]
                status=r.status,  # type: ignore[arg-type]
                outcome=r.outcome,  # type: ignore[arg-type]
                reviewers=[s["agent_id"] for s in json.loads(r.reviewer_snapshot_json)],
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in await self.store.list_reviews(limit)
        ]

    async def test_reviewers(self, mode: ReviewMode | None = None) -> ConsultAgentsResponse:
        """Preflight the configured reviewers. No project material leaves the machine.

        The agent ids are pushed into `list_agents` rather than filtered afterwards,
        because with `check=True` every row costs a subprocess and narrowing after the
        fact would preflight agents nobody asked about.
        """
        await self.open()
        wanted: list[str] = []
        for which in ("standard", "deep") if mode is None else [mode]:
            wanted += [s.agent_id for s in self._reviewer_snapshots(which, None)]  # type: ignore[arg-type]
        return await self.consult.list_agents(check=True, agent_ids=sorted(set(wanted)))

    # --- deletion -----------------------------------------------------------

    async def delete(self, review_id: UUID | str) -> int:
        await self.open()
        return await self.store.delete_review(review_id)

    async def request_delete_all(self) -> tuple[str, int]:
        await self.open()
        return await self.store.request_delete_all()

    async def delete_all(self, confirm_token: str) -> int:
        await self.open()
        return await self.store.delete_all_reviews(confirm_token)

    # --- envelope -----------------------------------------------------------

    async def _guard(self, review_id: UUID | str, started: float, work) -> ReviewResponse:
        """Load the review and run `work` over it, turning every failure into an
        envelope carrying the id the caller needs to act on next."""
        mode: ReviewMode = "standard"
        try:
            await self.open()
            review = await self.store.get_review(review_id)
            mode = review.mode  # type: ignore[assignment]
            return await work(review)
        except (StoreError, ValueError) as exc:
            code = exc.code if isinstance(exc, StoreError) else ConsultErrorCode.INVALID_REQUEST
            status = await self._status_of(review_id)
            return _failed(_uuid(review_id), mode, status, code, str(exc), started)
        except Exception as exc:
            status = await self._status_of(review_id)
            return _failed(
                _uuid(review_id), mode, status, ConsultErrorCode.TRANSPORT_ERROR,
                f"the review failed inside the orchestrator ({type(exc).__name__})", started,
            )

    async def _status_of(self, review_id: UUID | str) -> ReviewStatus:
        """Where the review actually is, for the failure envelope.

        Read back rather than assumed: a refused `orchestrator_finalize_review` leaves the review
        exactly where it was, and reporting `failed` would tell the caller to retry
        something that is still waiting on them.
        """
        try:
            return (await self.store.get_review(review_id)).status  # type: ignore[return-value]
        except Exception:
            return "failed"


def _fix_rounds(review) -> list[FixRound]:
    return [FixRound(**r) for r in json.loads(review.fix_rounds_json or "[]")][:MAX_FIX_ROUNDS]


def _parse_findings(agent_id: str, answer: str) -> tuple[list[Finding], bool, int]:
    """Pull the fenced JSON block out of a reviewer's prose. Never raises.

    Tolerant on purpose: the findings ride inside `answer` because all three
    adapters hardcode one output schema, so a reviewer that writes the block
    slightly wrong should cost its structure, not its whole answer. A failure
    returns `([], False, 0)` and the prose is still shown.
    """
    for chunk in _candidates(answer):
        try:
            payload = json.loads(chunk)
        except ValueError:
            continue
        raw = payload.get("findings") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            continue

        findings = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "")).strip().lower()
            findings.append(
                Finding(
                    finding_id="",  # assigned below, once the order is final
                    agent_id=agent_id,
                    location=str(item.get("location", ""))[:500],
                    severity=severity if severity in SEVERITY_ORDER else "uncertain",
                    why=str(item.get("why", ""))[:5000],
                    example=str(item.get("example", ""))[:5000],
                    fix=str(item.get("fix", ""))[:5000],
                )
            )

        dropped = 0
        if len(findings) > MAX_FINDINGS:
            # Severity-ordered, so the cap can never be what drops a Critical.
            # Stable within a severity, so a reviewer's own ordering survives.
            findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
            dropped = len(findings) - MAX_FINDINGS
            findings = findings[:MAX_FINDINGS]

        return (
            [f.model_copy(update={"finding_id": f"{agent_id}-{n}"})
             for n, f in enumerate(findings, 1)],
            True,
            dropped,
        )
    return [], False, 0


def _candidates(answer: str):
    """Blocks worth trying: every fenced one, then the last `{...}` mentioning
    findings. `rfind` rather than a forward scan because the reviewer is asked to
    end with the block, and a forward scan over a megabyte of prose would spend the
    time on every brace before it."""
    if not answer:
        return
    for match in _FENCE.finditer(answer):
        yield match.group(1)
    marker = answer.rfind('"findings"')
    if marker == -1:
        return
    start = answer.rfind("{", 0, marker)
    if start == -1:
        return
    depth = 0
    for index in range(start, len(answer)):
        if answer[index] == "{":
            depth += 1
        elif answer[index] == "}":
            depth -= 1
            if depth == 0:
                yield answer[start : index + 1]
                return


def _total(results: list[ReviewerResult]) -> Usage:
    """What the whole review cost, summed over the reviewers that reported.

    A reviewer rebuilt from its stored row has no usage, so this undercounts a
    retry rather than inventing a figure for the attempts it did not run.
    """
    used = [r.usage for r in results if r.usage is not None]
    costs = [u.cost_usd for u in used if u.cost_usd is not None]
    return Usage(
        prompt_tokens=sum(u.prompt_tokens for u in used),
        completion_tokens=sum(u.completion_tokens for u in used),
        total_tokens=sum(u.total_tokens for u in used),
        cost_usd=sum(costs) if costs else None,
    )


def _uuid(review_id: UUID | str) -> UUID:
    return review_id if isinstance(review_id, UUID) else UUID(str(review_id))


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _failed(
    review_id: UUID,
    mode: ReviewMode,
    status: ReviewStatus,
    code: ConsultErrorCode,
    message: str,
    started: float,
) -> ReviewResponse:
    return ReviewResponse(
        review_id=review_id,
        mode=mode,
        status=status,
        error=ConsultError(code=code, message=redact(message)[:MAX_ERROR_CHARS]),
        latency_ms=_ms(started),
    ).check_invariants()
