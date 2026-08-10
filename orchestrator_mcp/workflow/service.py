"""The workflow: three phases, one state machine, and a checkpoint at every step.

What this service owns is the *order* of a job and the record of it. It runs nothing
by itself: every step is planned first and run second, and the second call carries a
token that the first one issued. The host or the user decides when to advance.

Three properties are worth stating plainly, because they are the ones a reader will
want to check rather than take on faith:

  * **Nothing gains write access here.** A delegated step runs over the read-only
    consult path and comes back as text -- a document, or a diff the host applies
    itself. `isolated_write` reaches `code_adapter_for`, which refuses until an
    adapter exists that can actually contain a process.
  * **A coding agent's word is not a test result.** `test` is its own step with its
    own artifact, and `reported_by` is assigned from which tool wrote the row, never
    read out of the payload.
  * **`loop_done` is computed here**, from the review row, the test report and the
    workflow's own state. No reviewer is asked whether the work is finished, because
    a reviewer that says yes is not evidence of anything.

The token proves that what runs is what was previewed -- the agent, the model, the
mode and the prompt hash it was issued against. It is snapshot integrity, not human
approval; nothing here can tell whether a person read the preview.
"""

from __future__ import annotations

import json
import secrets as secrets_mod
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..code.registry import CodeError
from ..code.service import run_contained
from ..contract import MAX_ERROR_CHARS, scrub_json
from ..consult.config import ConsultConfig, StepBinding
from ..consult.contract import ConsultError, Runtime
from ..consult.errors import ConsultErrorCode
from ..consult.prompts import compile_execution_prompt
from ..consult.store import ConsultStore, StoreError
from ..review.contract import SERIOUS, ReviewSummary, open_serious
from ..review.service import ReviewService
from .contract import (
    ARTIFACT_MODELS,
    STEPS,
    TERMINAL_STATES,
    AgentRef,
    ArtifactType,
    CodeChange,
    Step,
    StepPreview,
    StepView,
    SynthesisRecord,
    TestReport,
    WorkflowError,
    WorkflowResponse,
    WorkflowState,
    WorkflowView,
)
from .prompts import INSTRUCTIONS, parse_reply, step_prompt
from .repo import baseline, resolve_workdir
from .routing import WorkflowRouter
from .store import (
    STEP_LEASE_SLACK_S,
    StepRow,
    WorkflowRun,
    WorkflowStore,
    canonical,
)
from .store import _sha256 as _text_sha256


class WorkflowService:
    """Workflows over the review and consult layers, on their store.

    One `ConsultStore` for all three, so a workflow, the reviews its review steps
    ran and the consultations under those delete in one transaction. The
    `ConsultService` is the review layer's own -- the one whose stored text goes
    through `scrub_json` -- rather than a second one with different write habits.
    """

    def __init__(
        self,
        config: ConsultConfig,
        host_runtime: Runtime,
        store: ConsultStore | None = None,
        reviews: ReviewService | None = None,
    ) -> None:
        if config.workflow is None:
            raise ValueError("no `consult.workflow:` block is configured")
        self.config = config
        self.policy = config.workflow
        self.host_runtime = host_runtime
        shared = store or ConsultStore(config.database_path, config.store_full_content)
        self.reviews = reviews or ReviewService(config, host_runtime, store=shared)
        self.consult = self.reviews.consult
        self.store = WorkflowStore(shared)
        self.router = WorkflowRouter(config, host_runtime)

    async def open(self) -> WorkflowService:
        await self.reviews.open()
        return self

    async def close(self) -> None:
        await self.reviews.close()

    # --- start ----------------------------------------------------------------

    async def start(
        self,
        goal: str,
        workdir: str,
        web: bool = False,
        bindings: dict[str, dict[str, Any]] | None = None,
        allow_dirty: bool = False,
    ) -> WorkflowResponse:
        """Create a workflow and freeze its routing. Sends nothing.

        Every step is resolved here, not when it is reached: an operator finding out
        at the review step that no agent can review is finding out after paying for
        research, planning and implementation. The resolved set is stored, so a config
        edited halfway through does not reroute what is already running -- that is
        what `plan_replan` is for.
        """
        started = time.perf_counter()
        workflow_id = str(uuid4())
        try:
            await self.open()
            if not goal.strip():
                raise WorkflowError(
                    ConsultErrorCode.INVALID_REQUEST, "a workflow needs a goal saying what to do"
                )
            path = resolve_workdir(workdir, self.policy.roots)
            head = await baseline(path, allow_dirty)
            resolved = self.router.resolve_all(self._bindings(bindings), want_web=web)
            snapshot = {step: view.as_binding() for step, view in resolved.items()}
            policy = {
                "max_fix_rounds": self.policy.max_fix_rounds,
                "advance_on_failed_test": self.policy.advance_on_failed_test,
                "review_policy": self.policy.review_policy.model_dump(mode="json"),
                "web": bool(web),
                "roots": [str(r) for r in self.policy.roots],
            }
            await self.store.create_workflow(
                workflow_id=workflow_id,
                goal=goal,
                workdir=str(path),
                host_runtime=self.host_runtime,
                host_model=self.config.host.model,
                bindings=snapshot,
                policy=policy,
                # Wider than `config_hash`, which covers the agent table only: two
                # workflows with the same agents and a different round cap are not
                # the same workflow.
                workflow_hash=_text_sha256(canonical({"bindings": snapshot, "policy": policy})),
                baseline_commit=head,
            )
            return await self._view_response(workflow_id, started)
        except (WorkflowError, StoreError) as exc:
            return _failed(workflow_id, "failed", exc.code, str(exc), started)
        except ValueError as exc:
            return _failed(workflow_id, "failed", ConsultErrorCode.INVALID_REQUEST, str(exc), started)

    def _bindings(self, overrides: dict[str, dict[str, Any]] | None) -> dict[Step, StepBinding]:
        """Configured bindings, with this call's overrides on top.

        A step nobody bound falls to the host. That is the conservative default and
        not a placeholder: the host can always do the work itself, and defaulting to
        an agent would mean a config that forgot a step still sent it somewhere.
        """
        merged: dict[Step, StepBinding] = {step: StepBinding(executor="host") for step in STEPS}
        merged.update(self.policy.bindings)
        for name, raw in (overrides or {}).items():
            if name not in STEPS:
                raise WorkflowError(
                    ConsultErrorCode.INVALID_REQUEST, f"`{name}` is not a workflow step"
                )
            merged[name] = StepBinding(**raw)  # type: ignore[index]
        return merged

    # --- planning a step ------------------------------------------------------

    async def plan_step(
        self, workflow_id: str, step: str, context: str = ""
    ) -> WorkflowResponse:
        """Write the `planned` row and hand back its preview and one-time token.

        `context` is the material the host is showing this step. A delegated step
        never sees the repository, so the source it is meant to change has to arrive
        this way or not at all; it is redacted before it is stored *and* before it is
        sent, so the preview, the hash and the send are all the same text.

        For a **review** step this calls `ReviewService.plan` and returns *that*
        plan's token as the step's own. One review, one approval: minting a second
        token here would mean approving the same send twice and calling the second
        one a workflow checkpoint.
        """
        started = time.perf_counter()
        try:
            await self.open()
            workflow = await self._live(workflow_id)
            if step not in STEPS:
                raise WorkflowError(ConsultErrorCode.INVALID_REQUEST, f"`{step}` is not a step")
            return await self._plan_step(started, workflow, step, context)  # type: ignore[arg-type]
        except (WorkflowError, StoreError) as exc:
            return _failed(workflow_id, await self._status(workflow_id), exc.code, str(exc), started)
        except ValueError as exc:
            return _failed(
                workflow_id, await self._status(workflow_id),
                ConsultErrorCode.INVALID_REQUEST, str(exc), started,
            )

    async def _plan_step(
        self, started: float, workflow: WorkflowRun, step: Step, context: str = ""
    ) -> WorkflowResponse:
        definition = STEPS[step]
        if workflow.status not in definition.runs_from:
            allowed = ", ".join(f"`{s}`" for s in definition.runs_from) or "no state"
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"`{step}` runs from {allowed}, and this workflow is `{workflow.status}`",
            )
        binding = json.loads(workflow.bindings_json)[step]
        if step == "review" and binding["executor"] == "host":
            # The review step's product is a review row, and only `ReviewService`
            # writes one. A host-recorded `review_outcome` would be a synthesis
            # written straight into a step, which is exactly the path around
            # `missing_serious` that the review layer exists to close.
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                "the `review` step cannot be `executor: host`; bind it to reviewers so "
                "the findings go on the record and can be checked for survival",
            )

        steps = await self.store.steps(workflow.id)
        round_index = workflow.fix_rounds
        attempt = 1 + sum(1 for s in steps if s.step == step and s.round_index == round_index)
        inputs = await self._step_inputs(step, steps)
        step_id = str(uuid4())
        agents = [AgentRef(**a) for a in binding.get("agents", [])]

        # Redacted here rather than at the store, so the text this preview measures,
        # the hash the token is bound to, the copy on the row and what the agent
        # actually receives are one string. Storing the original and sending it would
        # make the record a lie about what was sent.
        material = scrub_json(context) if context else ""
        prompt = payload = None
        review_id = None
        token = secrets_mod.token_urlsafe(32)
        if binding["executor"] == "agent" and step in INSTRUCTIONS:
            prompt, payload = step_prompt(step, workflow.goal, inputs, material)
        if step == "review":
            # The step's token *is* the review's. One approval, one review.
            review = await self._plan_review(workflow, binding, step_id, inputs, material)
            review_id = str(review.review_id)
            assert review.plan is not None
            token = review.plan.confirm_token

        agent_snapshot: dict[str, Any] = {
            "capability": definition.capability,
            "web": bool(binding.get("web")),
            "agents": binding.get("agents", []),
            "inputs": list(inputs),
            # Bound into the row so `run_step` can prove the prompt it is about to
            # send is the one the preview described. Without it the token would
            # approve the agent and the mode while leaving what they are asked to do
            # free to change.
            "prompt_sha256": _text_sha256(f"{prompt or ''}\n{payload or ''}"),
            # Kept so `run_step` sends what the preview described. Already redacted,
            # and scrubbed once more by the store, which is idempotent.
            "material": material,
        }
        if len(agents) == 1:
            # Where `StepRow.snapshot` reads the execution identity from. A step with
            # several agents is a review, and reviews are bound by the review layer.
            agent_snapshot["runtime"] = agents[0].runtime
            agent_snapshot["model"] = agents[0].model
        row = await self.store.plan_step(
            step_id=step_id,
            workflow_id=workflow.id,
            step=step,
            executor=binding["executor"],
            execution_mode=binding.get("execution"),
            repository_access=binding["repository_access"],
            agent_id=agents[0].agent_id if len(agents) == 1 else None,
            agent_snapshot=agent_snapshot,
            confirm_token=token,
            round_index=round_index,
            attempt=attempt,
            review_id=review_id,
        )
        return WorkflowResponse(
            workflow_id=workflow.id,
            status=workflow.status,  # type: ignore[arg-type]
            preview=StepPreview(
                step_id=row.id,
                step=step,
                executor=binding["executor"],
                execution_mode=binding.get("execution"),
                repository_access=binding["repository_access"],
                capability=definition.capability,
                agents=agents,
                web=bool(binding.get("web")),
                inputs=list(inputs),  # type: ignore[arg-type]
                prompt_chars=len(prompt or "") + len(payload or ""),
                prompt_sha256=agent_snapshot["prompt_sha256"],
                review_id=review_id,
                confirm_token=token,
            ),
            latency_ms=_ms(started),
        ).check_invariants()

    async def _plan_review(
        self, workflow: WorkflowRun, binding: dict, step_id: str, inputs: dict, material: str = ""
    ) -> Any:
        """Plan the review this step will run, under the workflow's review policy.

        The excluded identities are the ones this workflow resolved for planning and
        implementation, not the agent ids that were typed: two ids pointing at one
        model are one reviewer however they are spelled.
        """
        bindings = json.loads(workflow.bindings_json)
        excluded: dict[str, tuple[str, str]] = {}
        policy = self.policy.review_policy
        for role, step_name, wanted in (
            ("implementer", "implement", policy.different_from_implementer),
            ("planner", "plan", policy.different_from_planner),
        ):
            agents = bindings.get(step_name, {}).get("agents") or []
            if wanted and agents:
                excluded[role] = (agents[0]["runtime"], agents[0]["model"])

        payload = dict(inputs)
        if material:
            # The same material the coding steps were shown. A reviewer reading a diff
            # without the file it changes is reviewing half of it.
            payload["host_material"] = material
        context = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        manifest = [
            {"label": name, "kind": "text", "chars": len(json.dumps(value, default=str))}
            for name, value in payload.items()
        ]
        response = await self.reviews.plan(
            mode="standard",
            goal=workflow.goal,
            material=manifest,
            context=context,
            web=False,
            reviewers=[a["agent_id"] for a in binding.get("agents", [])] or None,
            workflow_id=workflow.id,
            step_id=step_id,
            excluded_identities=excluded,
        )
        if response.error is not None:
            raise WorkflowError(response.error.code, response.error.message)
        return response

    # --- running a step -------------------------------------------------------

    async def run_step(self, workflow_id: str, step_id: str, confirm_token: str) -> WorkflowResponse:
        """Spend the token and run one delegated step.

        The lease is on the workflow, not the step: two steps of one workflow running
        at once would each read a state the other is about to change.
        """
        started = time.perf_counter()
        try:
            await self.open()
            workflow = await self._live(workflow_id)
            row = await self._step_of(workflow, step_id)
            if row.executor != "agent":
                raise WorkflowError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"step `{step_id}` is the host's to do; record it with "
                    "`orchestrator_workflow_record_host_step`",
                )
            # A contained run gets the longer lease, because it gets the longer
            # timeout: a lease that expires while its process is still working would
            # let a second caller take the step the first one is running.
            contained = row.snapshot().execution_mode == "isolated_write"
            async with self.store.lease(workflow_id, step_id, ttl_s=self._lease_ttl(contained)):
                await self.store.consume_step_token(step_id, workflow_id, confirm_token)
                return await self._run_step(started, workflow, row, confirm_token)
        except (WorkflowError, StoreError, CodeError) as exc:
            return _failed(workflow_id, await self._status(workflow_id), exc.code, str(exc), started)

    async def _run_step(
        self, started: float, workflow: WorkflowRun, row: StepRow, token: str
    ) -> WorkflowResponse:
        snapshot = row.snapshot()
        step: Step = snapshot.step
        if snapshot.execution_mode == "isolated_write":
            return await self._run_contained(started, workflow, row)

        if step == "review":
            return await self._run_review(started, workflow, row, token)

        prompt, context = await self._planned_prompt(workflow, row, step)

        response = await self.consult.consult_step(snapshot, prompt, context)
        if not response.ok or response.content is None:
            await self.store.finish_step(row.id, "failed")
            error = response.error
            return _failed(
                workflow.id, workflow.status,  # type: ignore[arg-type]
                error.code if error else ConsultErrorCode.TRANSPORT_ERROR,
                error.message if error else "the step produced no answer",
                started,
            )

        answer = response.content.answer
        patch = ""
        try:
            if step in ("implement", "fix"):
                # The raw diff goes back once, here. What gets stored is the scrubbed
                # audit copy plus the hash of the raw text -- a scrubbed patch does
                # not apply, and storing one as though it did would be worse than
                # storing nothing.
                patch = answer
                output = CodeChange(
                    summary=f"{step} produced a patch of {len(answer)} characters",
                    files=_paths(answer),
                    patch=answer,
                    raw_patch_sha256=_text_sha256(answer),
                ).model_dump(mode="json")
            else:
                output = parse_reply(step, answer).model_dump(mode="json")
        except ValueError as exc:
            await self.store.finish_step(row.id, "failed")
            raise WorkflowError(ConsultErrorCode.PROTOCOL_VALIDATION_FAILED, str(exc)) from exc

        await self.store.finish_step(
            row.id, "done", output=output, raw_patch_sha256=output.get("raw_patch_sha256") or None
        )
        if step == "synthesize":
            return await self._synthesize(started, workflow, row, output)
        await self._advance(workflow, row, output)
        return await self._view_response(workflow.id, started, step_id=row.id, patch=patch)

    async def _planned_prompt(
        self, workflow: WorkflowRun, row: StepRow, step: Step
    ) -> tuple[str, str | None]:
        """Rebuild what the preview described, and refuse to send anything else.

        Assembled from the artifacts rather than stored, so the hash is what proves
        they have not moved underneath the approval. Shared by the delegated and the
        contained path so that one cannot drift into sending something the other's
        check would have caught.
        """
        inputs = await self._step_inputs(step, await self.store.steps(workflow.id))
        stored = json.loads(row.agent_snapshot_json)
        prompt, context = step_prompt(step, workflow.goal, inputs, stored.get("material", ""))
        expected = stored.get("prompt_sha256")
        if expected and expected != _text_sha256(f"{prompt}\n{context or ''}"):
            await self.store.finish_step(row.id, "failed")
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"step `{row.id}` would send something other than what its preview "
                "described; the artifacts it reads have changed since. Plan it again",
            )
        return prompt, context

    async def _run_contained(
        self, started: float, workflow: WorkflowRun, row: StepRow
    ) -> WorkflowResponse:
        """Run a step in a worktree, and read the result out of git rather than the reply.

        The agent here really does edit files and run commands -- the one place in this
        server where that is true -- so three things are worth reading closely:

          * it edits a *copy* checked out at the baseline, in a directory outside the
            user's repository, and the CLI's sandbox is what holds it there;
          * what comes back is the diff, captured by us. The model's summary is stored
            beside it as a description, and nothing downstream reads it as the change;
          * the host still applies it. `_advance` sends this to `awaiting_host_apply`
            exactly as it does a delegated patch, because a contained write has not
            touched the branch anyone is working on.
        """
        snapshot = row.snapshot()
        step: Step = snapshot.step
        agent = self.config.agents.get(snapshot.agent_id or "")
        if agent is None:
            await self.store.finish_step(row.id, "failed")
            raise WorkflowError(
                ConsultErrorCode.AGENT_UNAVAILABLE,
                f"agent `{snapshot.agent_id}` is no longer configured, so the step "
                "cannot run as it was approved. Plan it again",
            )

        prompt, context = await self._planned_prompt(workflow, row, step)
        try:
            result = await run_contained(
                self.config,
                agent,
                compile_execution_prompt(prompt, context),
                repo=Path(workflow.workdir),
                # The applied result, not the original baseline. After round one the
                # branch has moved, and a fix checked out at `baseline_commit` would
                # edit pre-implementation source and return a patch computed against a
                # commit the branch has left -- applying it reverts the implementation
                # the round was supposed to build on.
                baseline_commit=workflow.result_commit or workflow.baseline_commit,
                workflow_id=workflow.id,
                step_id=row.id,
                timeout_s=float(self.policy.execution_timeout_s),
            )
        except CodeError:
            await self.store.finish_step(row.id, "failed")
            raise

        if not result.changed:
            # Not an advance. A step that left the tree as it found it has not
            # implemented anything, and moving to `awaiting_host_apply` with an empty
            # patch would ask the host to apply nothing and then test it.
            await self.store.finish_step(row.id, "failed")
            raise WorkflowError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"the contained run changed no files. It reported: "
                f"{result.summary.strip()[:MAX_ERROR_CHARS] or '(nothing)'}",
            )

        output = CodeChange(
            summary=result.summary or f"{step} changed {len(result.files)} file(s)",
            files=result.files,
            patch=result.patch,
            raw_patch_sha256=_text_sha256(result.patch),
            ignored=result.ignored,
        ).model_dump(mode="json")
        await self.store.finish_step(
            row.id, "done", output=output, raw_patch_sha256=output["raw_patch_sha256"]
        )
        await self._advance(workflow, row, output)
        # The raw patch, once, the same way a delegated one comes back: what is stored
        # is scrubbed, and a scrubbed patch does not apply.
        return await self._view_response(
            workflow.id, started, step_id=row.id, patch=result.patch
        )

    async def _run_review(
        self, started: float, workflow: WorkflowRun, row: StepRow, token: str
    ) -> WorkflowResponse:
        """Ask the approved reviewers, through the review layer that owns them.

        The token this step just spent is the review's own, so the same secret opens
        both halves -- there is no second confirmation, and no second chance to change
        what was approved between them.
        """
        assert row.review_id is not None
        response = await self.reviews.run(row.review_id, token)
        if response.error is not None:
            await self.store.finish_step(row.id, "failed", review_id=row.review_id)
            return _failed(
                workflow.id, workflow.status, response.error.code,  # type: ignore[arg-type]
                response.error.message, started,
            )
        await self.store.finish_step(row.id, "done", review_id=row.review_id)
        await self._advance(workflow, row, None)
        return await self._view_response(workflow.id, started, step_id=row.id)

    # --- host-recorded steps --------------------------------------------------

    async def record_host_step(
        self, workflow_id: str, step_id: str, confirm_token: str, result: dict[str, Any]
    ) -> WorkflowResponse:
        """Record work the host did itself, spending the step's token as attestation.

        `reported_by` is assigned here, from the fact that this tool wrote the row. A
        value in `result` is dropped: the weaker claim must not be able to describe
        itself as the stronger one.
        """
        started = time.perf_counter()
        try:
            await self.open()
            workflow = await self._live(workflow_id)
            row = await self._step_of(workflow, step_id)
            if row.executor != "host":
                raise WorkflowError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"step `{step_id}` is delegated; run it with "
                    "`orchestrator_workflow_run_step`",
                )
            async with self.store.lease(workflow_id, step_id, ttl_s=self._lease_ttl()):
                await self.store.consume_step_token(step_id, workflow_id, confirm_token)
                return await self._record_host_step(started, workflow, row, result)
        except (WorkflowError, StoreError) as exc:
            return _failed(workflow_id, await self._status(workflow_id), exc.code, str(exc), started)

    async def _record_host_step(
        self, started: float, workflow: WorkflowRun, row: StepRow, result: dict[str, Any]
    ) -> WorkflowResponse:
        step: Step = row.step  # type: ignore[assignment]
        artifact = STEPS[step].output_artifact
        if step == "synthesize":
            output = _validated(ReviewSummary, result, step).model_dump(mode="json")
            return await self._synthesize(started, workflow, row, output)

        model = ARTIFACT_MODELS.get(artifact)
        if model is None:
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"`{step}` produces `{artifact}`, which has no shape a host can record",
            )
        payload = dict(result)
        if model is TestReport:
            # Not read from the payload, ever. A host-written report is host-attested
            # and that is what it says; only a report this process observed itself may
            # ever say `orchestrator`.
            payload["reported_by"] = "host"
        output = _validated(model, payload, step).model_dump(mode="json")
        await self.store.finish_step(
            row.id, "done", output=output,
            reported_by="host" if model is TestReport else None,
        )
        await self._advance(workflow, row, output)
        return await self._view_response(workflow.id, started, step_id=row.id)

    async def _step_inputs(self, step: Step, steps: list[StepRow]) -> dict[str, Any]:
        """What one step is shown: its artifacts, plus the findings a round is about.

        Read the same way in `plan_step` and `run_step`, because the prompt hash the
        token is bound to is computed from it -- two different assemblies would mean a
        step could never run what its preview described.
        """
        artifacts = _artifacts(steps)
        inputs: dict[str, Any] = {
            name: artifacts[name] for name in STEPS[step].input_artifacts if name in artifacts
        }
        if step in ("fix", "review"):
            findings = await self._open_findings(steps)
            if findings:
                inputs["open_findings"] = findings
        return inputs

    async def _open_findings(self, steps: list[StepRow]) -> list[dict[str, Any]]:
        """The serious findings the last review left open, in full.

        These cannot arrive as an artifact: the review step deliberately writes nothing
        under its own artifact name, so a fix round asking for `review_outcome` was
        handed an empty payload and answered from the goal instead of from what the
        reviewer said. Reading them back through `ReviewService` is what makes the
        round about the findings. `open_serious` returns the problem text alone, which
        is right for a reason line and not enough to fix anything from.
        """
        review_id = _review_id(steps)
        if review_id is None:
            return []
        response = await self.reviews.get(review_id)
        summary = response.summary
        if summary is None:
            return []
        return [
            finding.model_dump(mode="json")
            for finding in summary.combined_findings
            if finding.disposition == "open" and finding.severity in SERIOUS
        ]

    # --- synthesis and the loop ----------------------------------------------

    async def _synthesize(
        self, started: float, workflow: WorkflowRun, row: StepRow, summary: dict[str, Any]
    ) -> WorkflowResponse:
        """Finalize the review, compute `loop_done`, and place the workflow.

        The synthesis goes through `ReviewService.finalize`, which is what checks that
        no serious finding was dropped between the reviewers and the conclusion. The
        step stores a pointer and the computed outcome, never the synthesis itself --
        a synthesis written straight into a step would be exactly the way around that
        check.
        """
        review_id = _review_id(await self.store.steps(workflow.id))
        if review_id is None:
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                "there is no review to synthesize; run the review step first",
            )
        response = await self.reviews.finalize(review_id, summary)
        if response.error is not None:
            await self.store.finish_step(row.id, "failed", review_id=review_id)
            return _failed(
                workflow.id, workflow.status, response.error.code,  # type: ignore[arg-type]
                response.error.message, started,
            )

        assert response.summary is not None
        done, reasons = self._loop_done(
            response.summary, await self.store.steps(workflow.id), response.unparsed_reviewers
        )
        record = SynthesisRecord(
            review_id=review_id,
            loop_done=done,
            open_serious=len(open_serious(response.summary)),
            reasons=reasons,
        )
        await self.store.finish_step(
            row.id, "done", output=record.model_dump(mode="json"), review_id=review_id
        )

        if done:
            await self._transition(workflow.id, "completed", ("synthesizing",))
        elif workflow.fix_rounds >= self.policy.max_fix_rounds:
            await self._transition(
                workflow.id, "needs_attention", ("synthesizing",),
                reason=f"the {self.policy.max_fix_rounds}-round cap was reached with "
                       f"{record.open_serious} serious finding(s) still open",
            )
        else:
            await self.store.bump_fix_rounds(workflow.id)
            await self._transition(workflow.id, "fixing", ("synthesizing",))
        return await self._view_response(workflow.id, started, step_id=row.id)

    def _loop_done(
        self, summary: ReviewSummary, steps: list[StepRow], unparsed: list[str]
    ) -> tuple[bool, list[str]]:
        """Whether this workflow is finished, and every reason it is not.

        Computed, never asked. Each clause is a thing that could be true while the
        work is still broken, so the reasons are returned even when the answer is
        yes -- an empty list is the evidence, not the absence of a check.
        """
        reasons: list[str] = []
        report = _latest(steps, "test_report")
        if report is None:
            reasons.append("no test report has been recorded for this round")
        elif report.get("status") != "passed":
            reasons.append(f"the last test run was `{report.get('status')}`")
        if unparsed:
            # Without a reviewer's findings parsed, "no serious finding survived
            # unaddressed" is unprovable for that reviewer rather than true.
            reasons.append(f"findings could not be read from: {', '.join(unparsed)}")
        still_open = open_serious(summary)
        if still_open:
            reasons.append(f"{len(still_open)} serious finding(s) still open: {still_open[0]}")
        return not reasons, reasons

    async def _advance(self, workflow: WorkflowRun, row: StepRow, output: dict | None) -> None:
        """Move the workflow to where this step's success leaves it.

        Only on success, which is why there is no workflow-level `abandoned`: a step
        that dies leaves the workflow exactly where it was, and the answer is to plan
        that step again.
        """
        step: Step = row.step  # type: ignore[assignment]
        definition = STEPS[step]
        target: WorkflowState = definition.on_success
        commit = None

        if step in ("implement", "fix"):
            # A delegated agent produced a diff; the host owns applying it. Only a
            # host-run implementation lands straight in the tree.
            target = "testing" if row.executor == "host" else "awaiting_host_apply"
        elif step == "test":
            passed = (output or {}).get("status") == "passed"
            target = "reviewing" if passed or self.policy.advance_on_failed_test else "fixing"
        if step in ("implement", "fix", "apply_patch") and output:
            commit = output.get("commit") or None

        await self._transition(
            workflow.id, target, definition.runs_from, result_commit=commit
        )

    # --- replan ---------------------------------------------------------------

    async def plan_replan(
        self, workflow_id: str, bindings: dict[str, dict[str, Any]]
    ) -> WorkflowResponse:
        """Preview a binding change. Changes no routing until it is confirmed."""
        started = time.perf_counter()
        try:
            await self.open()
            workflow = await self._live(workflow_id)
            resolved = self.router.resolve_all(
                self._bindings(bindings), want_web=json.loads(workflow.policy_json).get("web", False)
            )
            snapshot = {step: view.as_binding() for step, view in resolved.items()}
            token = secrets_mod.token_urlsafe(32)
            await self.store.stage_replan(workflow_id, snapshot, token)
            return WorkflowResponse(
                workflow_id=workflow_id,
                status=workflow.status,  # type: ignore[arg-type]
                preview=StepPreview(
                    agents=[
                        AgentRef(**agent)
                        for view in snapshot.values()
                        for agent in view["agents"]
                    ],
                    confirm_token=token,
                    prompt_sha256=_text_sha256(canonical(snapshot)),
                ),
                latency_ms=_ms(started),
            ).check_invariants()
        except (WorkflowError, StoreError) as exc:
            return _failed(workflow_id, await self._status(workflow_id), exc.code, str(exc), started)
        except ValueError as exc:
            return _failed(
                workflow_id, await self._status(workflow_id),
                ConsultErrorCode.INVALID_REQUEST, str(exc), started,
            )

    async def replan(self, workflow_id: str, confirm_token: str) -> WorkflowResponse:
        """Adopt the staged bindings. Steps already run keep the routing they had."""
        started = time.perf_counter()
        try:
            await self.open()
            workflow = await self._live(workflow_id)
            policy = json.loads(workflow.policy_json)
            staged = json.loads(workflow.replan_bindings_json or "{}")
            await self.store.consume_replan(
                workflow_id,
                confirm_token,
                _text_sha256(canonical({"bindings": staged, "policy": policy})),
            )
            return await self._view_response(workflow_id, started)
        except (WorkflowError, StoreError) as exc:
            return _failed(workflow_id, await self._status(workflow_id), exc.code, str(exc), started)

    # --- status and cancel ----------------------------------------------------

    async def status(self, workflow_id: str) -> WorkflowResponse:
        started = time.perf_counter()
        try:
            await self.open()
            # Reaped on read, so a status call is enough to unwedge a workflow whose
            # runner died: the step becomes `abandoned` and can be planned again.
            await self.store.reap_abandoned(workflow_id)
            return await self._view_response(workflow_id, started)
        except StoreError as exc:
            return _failed(workflow_id, "failed", exc.code, str(exc), started)

    async def cancel(self, workflow_id: str) -> WorkflowResponse:
        """Stop a workflow, and this process's children with it.

        A step running under a *second* server process is marked, but its subprocesses
        are not signalled -- nothing here can reach them. The same honest caveat
        `orchestrator_cancel_review` carries, for the same reason.
        """
        started = time.perf_counter()
        try:
            await self.open()
            workflow = await self.store.get_workflow(workflow_id)
            if workflow.status in TERMINAL_STATES:
                raise WorkflowError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"workflow `{workflow_id}` is already `{workflow.status}`",
                )
            for row in await self.store.steps(workflow_id):
                if row.status == "running":
                    if row.review_id:
                        await self.reviews.cancel(row.review_id)
                    await self.store.finish_step(row.id, "cancelled")
            await self._transition(
                workflow_id, "cancelled",
                tuple(s for s in _ALL_STATES if s not in TERMINAL_STATES),
                reason="cancelled by the host",
            )
            return await self._view_response(workflow_id, started)
        except (WorkflowError, StoreError) as exc:
            return _failed(workflow_id, await self._status(workflow_id), exc.code, str(exc), started)

    # --- shared ---------------------------------------------------------------

    async def _live(self, workflow_id: str) -> WorkflowRun:
        await self.store.reap_abandoned(workflow_id)
        workflow = await self.store.get_workflow(workflow_id)
        if workflow.status in TERMINAL_STATES:
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"workflow `{workflow_id}` is `{workflow.status}` and takes no more steps",
            )
        return workflow

    async def _step_of(self, workflow: WorkflowRun, step_id: str) -> StepRow:
        row = await self.store.get_step(step_id)
        if row.workflow_id != workflow.id:
            # Checked rather than assumed: a step id from another workflow would
            # otherwise be run against this one's state and artifacts.
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"step `{step_id}` belongs to workflow `{row.workflow_id}`",
            )
        if row.status != "planned":
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"step `{step_id}` is `{row.status}`; only a planned step can be run",
            )
        runs_from = STEPS[row.step].runs_from  # type: ignore[index]
        if workflow.status not in runs_from:
            # `planned` is not enough. Two steps can be planned from one state, and
            # once the first has run, the second's preview describes a workflow that
            # no longer exists -- but its row is still `planned`, so without this it
            # would run, be paid for, and record an artifact for a state the workflow
            # has left. The transition afterwards is what fails, far too late.
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"step `{step_id}` was planned to run from "
                f"{_or_list(runs_from)}, and this workflow is now "
                f"`{workflow.status}`. Something else moved it on since; plan the "
                "step the status names as next",
            )
        return row

    async def _transition(
        self,
        workflow_id: str,
        to_status: WorkflowState,
        allowed_from: tuple[WorkflowState, ...],
        **kwargs: Any,
    ) -> None:
        """Move the workflow, and refuse to carry on quietly when it has already moved.

        `store.transition` guards the change in SQL and returns False when something
        else got there first. Every caller here used to discard that, which made losing
        the race silent: the step ran, was paid for and stored its artifact, and the
        workflow simply stayed where it was with nothing said about why.
        """
        if not await self.store.transition(workflow_id, to_status, allowed_from, **kwargs):
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"this workflow could not move to `{to_status}`: it is no longer in "
                f"{_or_list(allowed_from)}. Something changed it while this step was "
                "running -- read `orchestrator_workflow_status` before doing anything "
                "else. The step's own result was recorded",
            )

    async def _status(self, workflow_id: str) -> WorkflowState:
        """The workflow's state for an error envelope, or `failed` if it has none."""
        try:
            return (await self.store.get_workflow(workflow_id)).status  # type: ignore[return-value]
        except StoreError:
            return "failed"

    async def _view_response(
        self, workflow_id: str, started: float, step_id: str | None = None, patch: str = ""
    ) -> WorkflowResponse:
        workflow = await self.store.get_workflow(workflow_id)
        rows = await self.store.steps(workflow_id)
        views = [_step_view(row) for row in rows]
        return WorkflowResponse(
            workflow_id=workflow_id,
            status=workflow.status,  # type: ignore[arg-type]
            workflow=WorkflowView(
                workflow_id=workflow_id,
                status=workflow.status,  # type: ignore[arg-type]
                goal=workflow.goal,
                workdir=workflow.workdir,
                fix_rounds=workflow.fix_rounds,
                max_fix_rounds=self.policy.max_fix_rounds,
                baseline_commit=workflow.baseline_commit,
                result_commit=workflow.result_commit,
                reason=workflow.reason,
                bindings=json.loads(workflow.bindings_json),
                steps=views,
                next_steps=[
                    step for step, definition in STEPS.items()
                    if workflow.status in definition.runs_from
                ],
            ),
            step=next((v for v in views if v.step_id == step_id), None),
            patch=patch,
            latency_ms=_ms(started),
        ).check_invariants()

    def _lease_ttl(self, contained: bool = False) -> float:
        seconds = self.policy.execution_timeout_s if contained else self.config.timeout_s
        return float(seconds) + STEP_LEASE_SLACK_S


_ALL_STATES: tuple[WorkflowState, ...] = (
    "created", "researching", "planning", "prompt_ready", "coding",
    "awaiting_host_apply", "testing", "reviewing", "synthesizing", "fixing",
)


def _artifacts(steps: list[StepRow]) -> dict[str, Any]:
    """The latest artifact of each type, from the steps that produced one.

    Latest wins: a second fix round's patch is the patch, and a plan revised after a
    replan is the plan. Order comes from `sequence`, which the store assigns.
    """
    found: dict[str, Any] = {}
    for row in sorted(steps, key=lambda r: r.sequence):
        if row.status == "done" and row.output_json:
            found[STEPS[row.step].output_artifact] = row.output()  # type: ignore[index]
    return found


def _latest(steps: list[StepRow], artifact: ArtifactType) -> dict | None:
    return _artifacts(steps).get(artifact)


def _review_id(steps: list[StepRow]) -> str | None:
    for row in sorted(steps, key=lambda r: r.sequence, reverse=True):
        if row.step == "review" and row.review_id:
            return row.review_id
    return None


def _paths(patch: str) -> list[str]:
    """The files a unified diff claims to touch. A manifest, not a verification."""
    seen: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].strip()
            path = path[2:] if path[:2] in ("a/", "b/") else path
            if path and path not in seen:
                seen.append(path)
    return seen[:200]


def _or_list(states: tuple[str, ...]) -> str:
    """States a caller has to reconcile against, written the way they are read."""
    quoted = [f"`{state}`" for state in states]
    if len(quoted) > 4:
        # Cancellation allows every non-terminal state. Naming a dozen of them tells
        # the reader less than the count does.
        return f"any of the {len(quoted)} states it was allowed to move from"
    if len(quoted) < 3:
        return " or ".join(quoted)
    return f"{', '.join(quoted[:-1])} or {quoted[-1]}"


def _validated(model: type, payload: dict[str, Any], step: str):
    try:
        return model(**payload)
    except Exception as exc:
        raise WorkflowError(
            ConsultErrorCode.INVALID_REQUEST,
            f"the `{step}` result is not a valid {model.__name__}: {exc}",
        ) from exc


def _step_view(row: StepRow) -> StepView:
    return StepView(
        step_id=row.id,
        step=row.step,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        executor=row.executor,  # type: ignore[arg-type]
        execution_mode=row.execution_mode,  # type: ignore[arg-type]
        repository_access=row.repository_access,  # type: ignore[arg-type]
        agent_id=row.agent_id,
        round_index=row.round_index,
        attempt=row.attempt,
        sequence=row.sequence,
        review_id=row.review_id,
        reported_by=row.reported_by,  # type: ignore[arg-type]
        raw_patch_sha256=row.raw_patch_sha256,
        output=row.output(),
    )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _failed(
    workflow_id: str, status: WorkflowState, code: ConsultErrorCode, message: str, started: float
) -> WorkflowResponse:
    return WorkflowResponse(
        workflow_id=workflow_id,
        status=status,
        # The model's own bound, not a larger number that happens to be round: an
        # over-long message here raised out of the one function whose whole job is
        # that a refusal comes back as an envelope rather than as an exception.
        error=ConsultError(code=code, message=message[:MAX_ERROR_CHARS]),
        latency_ms=_ms(started),
    )
