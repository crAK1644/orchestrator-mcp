"""The MCP surface.

Three paths hang off `build_server`, all of them other agents' CLIs rather than an
API: `consult` asks one agent a question, `review` asks several and makes the host
synthesize them, and `workflow` runs a whole job through research, implementation
and review with the host deciding when to advance. This module owns the config
contract and the tool definitions; the protocols themselves live in `.consult`,
`.review` and `.workflow`.

No provider SDK, no API key, no network of our own -- every model reached from
here is reached through a CLI the user has already logged into.
"""

from __future__ import annotations

import inspect
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import yaml
from mcp.server import MCPServer

from .commands import add_commands
from .contract import ConfigError
from .consult.config import StepBinding, host_runtime, load_consult_config
from .consult.contract import (
    ConsultAgentsResponse,
    ConsultationDeleteApproval,
    ConsultationDeletionResult,
    ConsultationRecord,
    ConsultResponse,
)
from .consult.service import ConsultService
from .consult.store import DELETE_CONFIRM_TTL_S as CONSULT_DELETE_CONFIRM_TTL_S
from .consult.store import ConsultStore
from .review.contract import (
    CombinedFinding,
    DeleteApproval,
    DeletionResult,
    FixOutcome,
    MaterialItem,
    RawReviewMaterial,
    ReviewListing,
    ReviewMode,
    ReviewResponse,
    ReviewSummary,
)
from .review.service import ReviewService
from .review.store import DELETE_CONFIRM_TTL_S
from .workflow.contract import (
    Step,
    WorkflowDeleteApproval,
    WorkflowDeletionResult,
    WorkflowResponse,
)
from .workflow.service import WorkflowService
from .workflow.store import DELETE_CONFIRM_TTL_S as WORKFLOW_DELETE_CONFIRM_TTL_S

__all__ = ["ConfigError", "build_server", "load_config", "main", "validate_config"]

CONFIG_ENV = "ORCHESTRATOR_CONFIG"
DEFAULT_CONFIG = "config.yaml"

def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get(CONFIG_ENV, DEFAULT_CONFIG))
    if not path.exists():
        raise ConfigError(f"config not found: {path} (set {CONFIG_ENV} or create {DEFAULT_CONFIG})")
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict):
        raise ConfigError(f"config must be a YAML mapping: {path}")
    return config


# Everything the direct-routing path read. Named rather than ignored: a config
# written for 0.1 still parses as valid YAML, and a server that quietly started
# without the tools those blocks configured would look like a bug in the client.
_ROUTING_KEYS = ("capabilities", "model_list", "router_settings", "limits")


def validate_config(config: dict[str, Any]) -> None:
    if routing := [key for key in _ROUTING_KEYS if config.get(key)]:
        raise ConfigError(
            f"`{'`, `'.join(routing)}:` configured the `orchestrator_ask` tool, which was "
            "removed in 0.4 along with the LiteLLM dependency. This server now reaches "
            "models only through CLIs you have logged into yourself. Delete those blocks; "
            "if you need direct API routing, pin `orchestrator-mcp-server<0.4`"
        )
    if not config.get("consult"):
        raise ConfigError("nothing is configured: give the server a `consult:` block")


def _tool_signature(request_model: type, return_annotation: type) -> inspect.Signature:
    """Expose the request model's fields as flat keyword arguments.

    Nesting the model under one `request` parameter is what the SDK does by default;
    flat arguments give the calling agent the enum and the caps in the tool schema
    where it will actually read them. Derived from the model so there is one source
    of truth for the contract.
    """
    parameters = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[field.annotation, field],
            default=inspect.Parameter.empty if field.is_required() else field.default,
        )
        for name, field in request_model.model_fields.items()
    ]
    return inspect.Signature(parameters, return_annotation=return_annotation)


def _version() -> str:
    """The installed distribution's version, for the `initialize` handshake."""
    try:
        return version("orchestrator-mcp-server")
    except PackageNotFoundError:
        # Running from a source tree that was never installed. A missing version is
        # not worth refusing to start over.
        return "0+unknown"


def build_server(config: dict[str, Any] | None = None) -> MCPServer:
    config = config if config is not None else load_config()
    validate_config(config)
    server = MCPServer("orchestrator", version=_version())

    # The whole surface hangs off this one branch, because `consult:` is now the
    # only thing there is to configure. `validate_config` has already refused a
    # config without it, so `None` here means a block that parsed to nothing.
    consult_config = load_consult_config(config)
    if consult_config is not None:
        runtime = host_runtime()
        # A `consult.host.runtime:` that disagrees with the environment is a config
        # that has drifted, and every exclusion downstream would be computed against
        # the wrong identity. Checked here because this is the first place that holds
        # both answers.
        consult_config.check_host_runtime(runtime)
        # One store for both layers, not one each: a review and the consultations
        # under it are then written by the same serialized worker, and a deletion can
        # remove them in one transaction instead of hoping two connections agree.
        store = ConsultStore(consult_config.database_path, consult_config.store_full_content)
        _add_consult_tools(server, ConsultService(consult_config, runtime, store=store))
        # Advertised only when reviewers are configured. A server with none should
        # not offer an `orchestrator_review` tool that can do nothing but refuse.
        reviews = (
            ReviewService(consult_config, runtime, store=store)
            if consult_config.review is not None
            else None
        )
        if reviews is not None:
            _add_review_tools(server, reviews)
        # Same rule for the workflow, and the same `ReviewService` rather than a
        # second one: a workflow's review step and `orchestrator_cancel_review` have
        # to be talking about the same in-flight children, or cancelling from one
        # side would leave the other waiting on processes it cannot see.
        if consult_config.workflow is not None:
            _add_workflow_tools(
                server, WorkflowService(consult_config, runtime, store=store, reviews=reviews)
            )
        # The slash commands, gated on the same two answers as the tools they drive.
        # Last, so the `if`s above have already decided what exists.
        add_commands(
            server,
            reviews=reviews is not None,
            workflow=consult_config.workflow is not None,
            review_roots=tuple(
                str(root) for root in (consult_config.review.roots if reviews else [])
            ),
        )

    return server


def _add_consult_tools(server: MCPServer, service: ConsultService) -> None:
    async def consult(**kwargs: Any) -> ConsultResponse:
        # `service.consult` opens the store itself, on first use rather than at boot:
        # a server whose consult path is configured but never called should not create
        # a database for it, and an open that fails is an envelope like any other.
        return await service.consult(**kwargs)

    consult.__name__ = "consult"
    consult.__signature__ = _tool_signature(service.request_model, ConsultResponse)  # type: ignore[attr-defined]
    consult.__annotations__ = {
        p.name: p.annotation for p in consult.__signature__.parameters.values()
    } | {"return": ConsultResponse}
    consult.__doc__ = (
        "Consult another vendor's coding agent -- Codex, Claude Code, OpenCode, or "
        "experimental Antigravity -- running under its own account, and get back a "
        "structured second opinion.\n\n"
        "Keep the `consultation_id` from the response and send it back on every later "
        "call about the same topic: that is what continues the same conversation on "
        "the other side, and without it each call starts a new one that remembers "
        "nothing.\n\n"
        "Returns an envelope: `ok`, `content` (answer, assumptions, uncertainties, "
        "follow_up_questions, sources), `route`, `usage`, and `error` (a code from a "
        "closed set). A failed call never carries answer text -- check `ok` first. An "
        "error with `required_action` means the agent needs the user to run that "
        "command; nothing else will make it available."
    )
    server.add_tool(consult, name="orchestrator_consult")

    @server.tool(name="orchestrator_list_consult_agents")
    async def list_consult_agents() -> ConsultAgentsResponse:
        """List consultable agents: runtime, model, capability scores, and whether each
        is installed and logged in. The host's own runtime is listed but never routed
        to."""
        await service.open()
        return await service.list_agents()

    @server.tool(name="orchestrator_get_consultation")
    async def get_consultation(consultation_id: UUID) -> ConsultationRecord:
        """Retrieve a stored consultation: its turns, usage, and why this agent was
        chosen."""
        await service.open()
        return await service.get_consultation(consultation_id)

    @server.tool(name="orchestrator_delete_consultation")
    async def delete_consultation(consultation_id: UUID) -> ConsultationDeletionResult:
        """Delete one ordinary consultation and all of its locally stored turns.

        A consultation owned by a review must be deleted through the review tool so
        the findings and audit trail cannot be separated from their source. Vendor
        CLI history is outside this database and is not removed.
        """
        return ConsultationDeletionResult(
            deleted=await service.delete_consultation(consultation_id)
        )

    @server.tool(name="orchestrator_request_delete_all_consultations")
    async def request_delete_all_consultations() -> ConsultationDeleteApproval:
        """Preview deletion of all ordinary consultation history. Deletes nothing.

        The returned token is bound to the exact consultations counted here; review
        history and consultations created afterwards are not included.
        """
        token, count = await service.request_delete_all_consultations()
        return ConsultationDeleteApproval(
            consultations=count,
            confirm_token=token,
            expires_in_s=int(CONSULT_DELETE_CONFIRM_TTL_S),
        )

    @server.tool(name="orchestrator_delete_all_consultations")
    async def delete_all_consultations(confirm_token: str) -> ConsultationDeletionResult:
        """Delete the ordinary consultations in the approved snapshot, and only those."""
        return ConsultationDeletionResult(
            deleted=await service.delete_all_consultations(confirm_token)
        )


def _add_review_tools(server: MCPServer, service: ReviewService) -> None:
    """The review surface: a two-call handshake, then synthesis.

    Two tools rather than one that switches on its arguments. A single `orchestrator_review`
    taking either `(mode, goal, context)` or `(review_id, confirm_token)` is two
    mutually exclusive argument sets in one schema, which a calling model reads as
    "everything is optional" -- the same reasoning as `_agent_enum`.

    The handshake is advisory and the docstrings say so: an MCP server has no
    channel to a human and cannot verify anyone saw the plan. What it does buy is
    an explicit sequence, a `review_id` that exists before anything is sent, and a
    stored record of what was approved against what went out.
    """

    @server.tool(name="orchestrator_review")
    async def review(
        goal: str,
        mode: ReviewMode = "standard",
        material: list[MaterialItem] | None = None,
        context: str | None = None,
        context_paths: list[str] | None = None,
        web: bool = False,
        reviewers: list[str] | None = None,
        parent_review_id: UUID | None = None,
        host_model: str | None = None,
    ) -> ReviewResponse:
        """Plan a review and show what would be sent. **Sends nothing.**

        Show the returned `plan` to the user before calling `orchestrator_review_run`: which
        reviewers, how much material, whether web access is requested, how many
        requests it will cost, and `secret_hits` -- lines where something
        credential-shaped was found (positions only, never values). Secret detection
        is best-effort pattern matching; a credential with no recognizable shape
        survives it, so the plan raises the odds the user notices and guarantees
        nothing. This checkpoint is advisory: MCP gives this server no separate
        human channel, so it binds and audits the scope but cannot prove which human
        saw or approved it.

        Give the material as **either** `context` (a string you write) **or**
        `context_paths` (files this server reads for you). Both together is refused.
        `context_paths` is how a large review fits in one call: a whole-branch diff
        runs to hundreds of kilobytes, which you cannot type into a tool argument but
        can write to a file and name here. The reviewer never sees a path either way
        -- it runs with no filesystem at all -- so what reaches it is the bytes.

        `material` is your own manifest of what went into `context`. When you wrote
        `context` yourself this server cannot check the two agree: it is a disclosure
        aid that puts your sources on the record. When you passed `context_paths` and
        declared no manifest of your own, the server builds one from the bytes it
        actually read and the plan comes back `material_verified: true`. Either way
        the manifest is inside the approval hash. The run reads the same stored plan
        this call created; the returned hash identifies that plan but is not a
        separate payload verifier.

        `mode="deep"` asks up to five reviewers and requires your own findings first,
        passed to `orchestrator_review_run` as `host_findings`. `web=False` unless the user asked
        for web access: reviewers get none by default.

        Returns `confirm_token`, which is shown once and can be spent once. Note that
        material sent to a reviewer also lands in that reviewer's own CLI history,
        which nothing here can reach.
        """
        return await service.plan(
            mode=mode,
            goal=goal,
            material=[m.model_dump(mode="json") for m in (material or [])],
            context=context,
            context_paths=context_paths,
            web=web,
            reviewers=reviewers,
            parent_review_id=parent_review_id,
            host_model=host_model,
        )

    @server.tool(name="orchestrator_review_run")
    async def review_run(
        review_id: UUID,
        confirm_token: str,
        host_findings: list[str] | None = None,
        secrets: Literal["mask", "send_as_is"] = "mask",
        raw: RawReviewMaterial | None = None,
    ) -> ReviewResponse:
        """Send the approved material to every reviewer, in parallel. Call this only
        after the user has seen the plan and agreed.

        This checkpoint is advisory: the server verifies the one-time token and
        unchanged scope, but cannot prove which human supplied it or approved the
        plan. Human approval depends on the MCP client's tool-confirmation experience
        or another external gate.

        `secrets="mask"` sends the redacted copy. `secrets="send_as_is"` sends the
        original and requires `raw` to repeat the exact goal and context you planned
        with -- for when the user has looked at a flagged line and decided it is a
        variable named `password` or a fixture's fake key. Either way the stored copy
        is redacted.

        `host_findings` is your own independent opinion, formed before you read
        anyone else's. Required for `mode="deep"`, and never shown to any reviewer.

        Stops at `awaiting_synthesis`: reviewers replying is not a finished review.
        Read every `results[]` entry, write the comparison yourself, and call
        `orchestrator_finalize_review`. A reviewer that failed leaves the others' answers intact --
        `orchestrator_retry_review` re-runs only the failures.
        """
        return await service.run(
            review_id,
            confirm_token,
            host_findings=host_findings,
            secrets=secrets,
            raw=raw.model_dump(mode="json") if raw else None,
        )

    @server.tool(name="orchestrator_retry_review")
    async def retry_review(
        review_id: UUID,
        agent_ids: list[str] | None = None,
        raw: RawReviewMaterial | None = None,
    ) -> ReviewResponse:
        """Re-run the reviewers that failed. Reviewers that already answered are not
        asked again and their answers are kept.

        A review first run with `secrets="send_as_is"` requires the exact original
        goal and context in `raw` again. Its hash is checked against the original
        plan so a retry cannot silently send the
        stored redacted copy as though it were the same payload."""
        return await service.retry(
            review_id, agent_ids, raw.model_dump(mode="json") if raw else None
        )

    @server.tool(name="orchestrator_finalize_review")
    async def finalize_review(
        review_id: UUID,
        summary: str,
        recommendation: str,
        # Required in the schema, not merely asked for in the prose below: a scope
        # that is optional is a scope that gets left out.
        checked: list[str],
        not_checked: list[str],
        combined_findings: list[CombinedFinding] | None = None,
        agreements: list[str] | None = None,
        disagreements: list[str] | None = None,
        options: list[str] | None = None,
    ) -> ReviewResponse:
        """Record your synthesis. The only path to `complete`.

        Every Critical finding any reviewer raised must be referenced by some
        combined finding through `source_finding_ids`, **including one only a single
        reviewer raised while the others disagree**. That is the point of asking more
        than one: disagree with it in `disagreed_by`, never drop it. The call verifies
        this only when every reviewer produced parseable, retained findings; otherwise
        it refuses finalization instead of treating an empty finding set as proof.

        `checked` and `not_checked` are both required, so the scope of the review is
        stated rather than inferred from silence.
        """
        return await service.finalize(
            review_id,
            {
                "summary": summary,
                "recommendation": recommendation,
                "combined_findings": [f.model_dump(mode="json") for f in (combined_findings or [])],
                "agreements": agreements or [],
                "disagreements": disagreements or [],
                "options": options or [],
                "checked": checked,
                "not_checked": not_checked,
            },
        )

    @server.tool(name="orchestrator_apply_fixes")
    async def apply_fixes(review_id: UUID, finding_ids: list[str]) -> ReviewResponse:
        """Pull up the findings you are about to fix, with the steps around them.
        **Changes nothing** -- no file is edited and no command is run here.

        Fixing is yours to do: make a safety point first, apply the changes, run the
        tests, and keep or undo. Then record what happened with `orchestrator_record_fix_round`.

        `fix_plan.criticals_omitted` lists Critical findings your selection leaves
        out. Show them to the user rather than skipping past them -- a Critical
        dropped here is one the recheck will simply find again.

        To re-review, plan a new review with `parent_review_id` set to this one and
        the diff as `context`. A recheck gets the same preview and the same approval
        as any other review; it is not exempt from either.
        """
        return await service.fix_plan(review_id, finding_ids)

    @server.tool(name="orchestrator_record_fix_round")
    async def record_fix_round(
        review_id: UUID,
        finding_ids: list[str],
        outcome: FixOutcome,
        notes: str = "",
    ) -> ReviewResponse:
        """Record what a round of fixing did, after you did it.

        A log entry, not an action: this server takes your account of the round and
        stores it beside the findings it names. It cannot check the claim, because it
        never sees your repository.

        `outcome` is `applied`, `partial`, `reverted` (the fix was undone), or
        `skipped`. `finding_ids` come from `results[].findings[].finding_id`; an id
        no reviewer raised is refused rather than recorded against nobody.
        """
        return await service.record_fix_round(review_id, finding_ids, outcome, notes)

    @server.tool(name="orchestrator_cancel_review")
    async def cancel_review(review_id: UUID) -> ReviewResponse:
        """Stop a review. Reviewers that already answered keep their answers.

        A review launched by *this* server process is waited out before this returns.
        One launched by a second process is marked cancelled, but its subprocesses
        cannot be signalled from here -- they will finish on their own. Deleting the
        review is refused until they do.
        """
        return await service.cancel(review_id)

    @server.tool(name="orchestrator_test_reviewers")
    async def test_reviewers(mode: ReviewMode | None = None) -> ConsultAgentsResponse:
        """Check that the configured reviewers are installed and logged in.

        A preflight and nothing more: **no project material leaves this machine.**
        Omit `mode` to check the reviewers for both standard and deep.
        """
        return await service.test_reviewers(mode)

    @server.tool(name="orchestrator_get_review")
    async def get_review(review_id: UUID) -> ReviewResponse:
        """Retrieve a review: its status, every reviewer's result, and the synthesis
        if one was recorded. Answers are absent when the operator has turned off
        `store_full_content`."""
        return await service.get(review_id)

    @server.tool(name="orchestrator_list_reviews")
    async def list_reviews(limit: int = 20) -> list[ReviewListing]:
        """List recent reviews, newest first. Metadata only -- no material."""
        return await service.list(limit)

    @server.tool(name="orchestrator_delete_review")
    async def delete_review(review_id: UUID) -> DeletionResult:
        """Delete a review, its rechecks, and every consultation under either.

        Refused while a review is running, or while its reviewers are still in flight
        in any server process. Cancel it and try again."""
        return DeletionResult(deleted=await service.delete(review_id))

    @server.tool(name="orchestrator_request_delete_all")
    async def request_delete_all() -> DeleteApproval:
        """Ask what deleting all review history would remove.

        Deletes nothing. Show the count to the user and pass the token to
        `orchestrator_delete_all_reviews` only if they agree. The token is bound to exactly the
        reviews counted here, so one created in the meantime is not caught by an
        approval that never mentioned it."""
        token, count = await service.request_delete_all()
        return DeleteApproval(
            reviews=count, confirm_token=token, expires_in_s=int(DELETE_CONFIRM_TTL_S)
        )

    @server.tool(name="orchestrator_delete_all_reviews")
    async def delete_all_reviews(confirm_token: str) -> DeletionResult:
        """Delete the reviews `orchestrator_request_delete_all` counted, and only those. Requires
        the token from that call; an expired one is refused rather than silently
        widened to whatever exists now."""
        return DeletionResult(deleted=await service.delete_all(confirm_token))


def _add_workflow_tools(server: MCPServer, service: WorkflowService) -> None:
    """The workflow surface: plan a step, approve it, run or record it, repeat.

    Every side-effecting step is two calls, for the same reason the review layer is:
    a preview that exists before anything is sent, and a token bound to that exact
    preview. There is no one-call form and no workflow-level token -- one approval
    covering research, implementation, every fix round and every review would be an
    approval of nothing in particular.

    What the tokens prove is snapshot integrity: that what runs is what the preview
    described, by the agent it named, over the artifacts it read. They cannot prove a
    human saw anything, because MCP gives this server no channel to one.

    This server never edits a file or runs a command. A delegated step returns a
    patch; applying it, running the tests and reading the exit code stay the host's,
    and `record_host_step` is how that work comes back onto the record.
    """

    @server.tool(name="orchestrator_workflow_start")
    async def workflow_start(
        goal: str,
        workdir: str,
        web: bool = False,
        bindings: dict[Step, StepBinding] | None = None,
        allow_dirty: bool = False,
    ) -> WorkflowResponse:
        """Create a workflow and freeze its routing. **Sends nothing.**

        Every step is resolved now rather than when it is reached: finding out at the
        review step that no agent can review is finding out after paying for research,
        planning and implementation. The resolved set is stored, so editing the config
        afterwards does not reroute a running workflow -- `orchestrator_workflow_plan_replan` does.

        `workdir` must resolve beneath a configured `workflow.roots:` entry and be a
        git repository; its HEAD is recorded as the baseline. A dirty tree is refused
        unless `allow_dirty=True`, so a later "what changed" is answerable.

        `bindings` overrides the configured ones for this workflow only, and goes
        through the same compatibility checks: a step whose agent scores 0 for it, or
        whose runtime cannot be held to the execution mode asked for, is refused here
        rather than at the step. **A step nobody binds falls to the host.**
        """
        return await service.start(
            goal=goal,
            workdir=workdir,
            web=web,
            bindings={k: v.model_dump(mode="json") for k, v in (bindings or {}).items()},
            allow_dirty=allow_dirty,
        )

    @server.tool(name="orchestrator_workflow_plan_step")
    async def workflow_plan_step(
        workflow_id: str, step: Step, context: str = ""
    ) -> WorkflowResponse:
        """Prepare one step and show what it would do. **Sends nothing.**

        Returns the step's id, the agents it would use, and a `confirm_token` that is
        shown once and spent once. Show the preview to the user before spending it.

        `context` is the material this step gets to see -- the current source of the
        files it is meant to change, most usefully. A delegated agent never sees your
        repository, so a `plan`, `implement` or `fix` step sent without it is being
        asked to work from a description alone, and a good model will say so rather
        than guess. Credential-shaped values are redacted before it is stored *and*
        before it is sent, so the preview, the token's hash and the send are the same
        text.

        For a **review** step this plans the review as well, and the token you get
        back *is* that review's token -- one review, one approval. The reviewers are
        excluded by execution identity from whoever planned or implemented the change,
        under `workflow.review_policy:`, so two agent ids pointing at one model cannot
        review each other's work.
        """
        return await service.plan_step(workflow_id, step, context)

    @server.tool(name="orchestrator_workflow_run_step")
    async def workflow_run_step(
        workflow_id: str, step_id: str, confirm_token: str
    ) -> WorkflowResponse:
        """Spend the token and run a delegated step. Call this after the user has seen
        the preview.

        The prompt is checked against the one the preview described: if the artifacts
        it reads changed in between, the step is refused rather than sent, and you plan
        it again.

        For `implement` and `fix` the agent replies with a unified diff, returned here
        in `patch`. SQLite stores a scrubbed audit copy plus the hash of the raw text,
        because a scrubbed patch does not apply. A private recovery copy is returned
        by workflow status until application. If `recovery_warning` is non-empty, keep
        the returned patch yourself because the private copy was unavailable or unsafe.
        Apply it yourself, then record the result with
        `orchestrator_workflow_record_host_step` on the `apply_patch` step.

        A step whose executor is the host is refused here; record it instead.
        """
        return await service.run_step(workflow_id, step_id, confirm_token)

    @server.tool(name="orchestrator_workflow_record_host_step")
    async def workflow_record_host_step(
        workflow_id: str,
        step_id: str,
        confirm_token: str,
        result: dict[str, Any],
    ) -> WorkflowResponse:
        """Record work you did yourself, spending the step's token as your attestation.

        `result` must match the artifact the step produces, and is validated: a
        `test` step takes a `TestReport` (the exact command, working directory, exit
        code, bounded output, and the commit tested), `synthesize` takes the same
        synthesis `orchestrator_finalize_review` takes, and so on. Prose where a
        report belongs is refused rather than stored under an artifact's name.

        `reported_by` is assigned by this server from the fact that you wrote the row,
        and a value in `result` is dropped. A host-written report is host-attested and
        says so -- this server never sees your repository. What it is authoritative
        over is a coding agent's *claim* to have run the tests, which is a sentence,
        not a result.
        """
        return await service.record_host_step(workflow_id, step_id, confirm_token, result)

    @server.tool(name="orchestrator_workflow_status")
    async def workflow_status(workflow_id: str) -> WorkflowResponse:
        """Where the workflow is: state, artifacts so far, the agents each step
        resolved to, fix rounds used against the cap, and `next_steps` -- the steps
        that can be planned from here.

        Also reaps steps whose runner died: a `running` step whose lease expired
        becomes `abandoned` and can be planned again, so a crash cannot leave a
        workflow wedged in a status that outlived its process.
        """
        return await service.status(workflow_id)

    @server.tool(name="orchestrator_workflow_plan_replan")
    async def workflow_plan_replan(
        workflow_id: str, bindings: dict[Step, StepBinding]
    ) -> WorkflowResponse:
        """Preview a routing change for a running workflow. **Changes nothing.**

        Rerouting is the same handshake as anything else here, because a binding swap
        mid-workflow decides who sees the rest of the material. Returns a token for
        `orchestrator_workflow_replan`.
        """
        return await service.plan_replan(
            workflow_id, {k: v.model_dump(mode="json") for k, v in bindings.items()}
        )

    @server.tool(name="orchestrator_workflow_replan")
    async def workflow_replan(workflow_id: str, confirm_token: str) -> WorkflowResponse:
        """Adopt the staged bindings. Steps already run keep the routing they had --
        this changes where the *remaining* steps go, not the record of what happened."""
        return await service.replan(workflow_id, confirm_token)

    @server.tool(name="orchestrator_workflow_cancel")
    async def workflow_cancel(workflow_id: str) -> WorkflowResponse:
        """Stop a workflow. Completed steps keep their artifacts.

        A step running under *this* server process is waited out. One launched by a
        second process is marked cancelled, but its subprocesses cannot be signalled
        from here -- they will finish on their own. The same caveat
        `orchestrator_cancel_review` carries, for the same reason.
        """
        return await service.cancel(workflow_id)

    @server.tool(name="orchestrator_delete_workflow")
    async def delete_workflow(workflow_id: str) -> WorkflowDeletionResult:
        """Delete a workflow with its steps, its consultations and its reviews.

        All of it or none of it. A workflow's consultations are refused by the
        consultation delete tools and its reviews by `orchestrator_delete_review`,
        because a step pointing at a row that has been removed still reads as intact
        -- and a fix round reading its findings back from a deleted review is handed
        an empty list rather than an error.

        Refused while the workflow is still open: cancel it first. Also refused while
        a step holds a live lease, which may be an agent running in another server
        process."""
        return WorkflowDeletionResult(deleted=await service.delete(workflow_id))

    @server.tool(name="orchestrator_request_delete_all_workflows")
    async def request_delete_all_workflows() -> WorkflowDeleteApproval:
        """Ask what deleting all workflow history would remove. **Deletes nothing.**

        Show the count to the user and pass the token to
        `orchestrator_delete_all_workflows` only if they agree. The token is bound to
        exactly the workflows counted here, so one started in the meantime is not
        caught by an approval that never mentioned it. An open workflow is counted and
        then refused at confirmation rather than quietly left out of a number
        presented as the whole history."""
        token, count = await service.request_delete_all()
        return WorkflowDeleteApproval(
            workflows=count, confirm_token=token, expires_in_s=int(WORKFLOW_DELETE_CONFIRM_TTL_S)
        )

    @server.tool(name="orchestrator_delete_all_workflows")
    async def delete_all_workflows(confirm_token: str) -> WorkflowDeletionResult:
        """Delete the workflows `orchestrator_request_delete_all_workflows` counted,
        and only those. Requires the token from that call; an expired one is refused
        rather than silently widened to whatever exists now."""
        return WorkflowDeletionResult(deleted=await service.delete_all(confirm_token))


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
