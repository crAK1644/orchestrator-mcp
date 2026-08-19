"""The workflow's vocabulary: states, steps, artifacts, and the resolved snapshot.

Imports nothing from `consult.config` or from any service, on purpose -- the config
validates bindings against `STEPS`, so the dependency has to run one way only. What
this module knows about an agent is its execution identity as three strings, never
an `AgentConfig`.

Two things are load-bearing here:

  * A step says which capability it needs and which execution modes an *agent* may
    use for it. An empty `allowed_execution_modes` means the step is host-only --
    there is no delegated way to do it at all.
  * `repository_access` is not on the step. It follows the resolved binding: the
    same `implement` step touches the active tree when the host does it, sees only
    supplied context when an agent returns a patch, and gets a worktree under
    `isolated_write`. Putting it on the step would state one of those three as if it
    were the rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..contract import MAX_ERROR_CHARS, Usage
from ..consult.contract import Capability, ConsultError, ExecutionMode, Runtime
from ..consult.errors import ConsultErrorCode


class WorkflowError(Exception):
    """A refusal carrying a code the tool's envelope can report.

    The same shape as `StoreError` and `CodeError`, and deliberately not a subclass
    of either: a refusal from routing is not a storage failure, and the service
    catches them separately.
    """

    def __init__(self, code: ConsultErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code

WorkflowState = Literal[
    "created",
    "planning",
    "prompt_ready",
    "coding",
    "awaiting_host_apply",
    "testing",
    "reviewing",
    "synthesizing",
    "fixing",
    "completed",
    # The cap was reached with serious findings still open. Distinct from `failed`:
    # nothing went wrong, the loop simply did not converge, and the difference is
    # what a caller needs in order to decide whether to look at it or retry it.
    "needs_attention",
    "failed",
    "cancelled",
]

# No `abandoned` here, though there is one on `StepStatus`. A workflow's state moves
# only when a step *succeeds*, so a step whose lease expired leaves the workflow
# exactly where it was and the fix is to plan that step again. A workflow-level
# `abandoned` would be a state nothing could enter.
TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {"completed", "needs_attention", "failed", "cancelled"}
)

Step = Literal[
    "research",
    "plan",
    "author_execution_prompt",
    "implement",
    "apply_patch",
    "test",
    "review",
    "synthesize",
    "fix",
]

ArtifactType = Literal[
    "research_brief",
    "implementation_plan",
    "execution_brief",
    "code_change",
    "test_report",
    "review_outcome",
    "synthesis",
    # An input no step produces: the serious findings the last review left open, read
    # back from the review row. A fix round that does not carry these is a second pass
    # at the goal rather than an answer to the review, so the preview names it.
    "open_findings",
]

Executor = Literal["host", "agent"]

# What the step's worker can reach. Derived from the resolved binding, never chosen
# by a caller.
RepositoryAccess = Literal["active_tree", "worktree", "context_only"]

StepStatus = Literal["planned", "running", "done", "failed", "abandoned", "cancelled"]

# Who observed a result. `orchestrator` means this process ran the command and read
# the exit code; `host` means the host says it did. Assigned by the service from
# which tool wrote the row -- never read out of a caller's payload, or the weaker of
# the two could claim to be the stronger.
ReportedBy = Literal["host", "orchestrator"]


class StepDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    # None for a step no agent can take -- applying a patch to the host's own tree is
    # not a capability anyone can be scored for.
    capability: Capability | None
    input_artifacts: tuple[ArtifactType, ...] = ()
    output_artifact: ArtifactType
    needs_web: bool = False
    produces_code_change: bool = False
    # Empty means host-only.
    allowed_execution_modes: tuple[ExecutionMode, ...] = ()
    supports_multiple_agents: bool = False
    # Which states this step may run from, and where success leaves the workflow.
    # `on_success` is advisory for the steps whose next state depends on the outcome
    # (`implement`, `fix`, `test`, `synthesize`); the service decides those.
    runs_from: tuple[WorkflowState, ...] = ()
    on_success: WorkflowState = "failed"


STEPS: dict[Step, StepDefinition] = {
    "research": StepDefinition(
        capability="research",
        output_artifact="research_brief",
        needs_web=True,
        allowed_execution_modes=("consultation",),
        runs_from=("created",),
        on_success="planning",
    ),
    # Research is skippable: a workflow that starts from a known problem has nothing
    # to look up, and forcing a paid step to produce an empty brief helps nobody.
    "plan": StepDefinition(
        capability="planning",
        input_artifacts=("research_brief",),
        output_artifact="implementation_plan",
        allowed_execution_modes=("consultation",),
        runs_from=("created", "planning"),
        on_success="prompt_ready",
    ),
    "author_execution_prompt": StepDefinition(
        capability="prompt_authoring",
        input_artifacts=("implementation_plan",),
        output_artifact="execution_brief",
        allowed_execution_modes=("consultation",),
        runs_from=("prompt_ready",),
        on_success="coding",
    ),
    "implement": StepDefinition(
        capability="coding",
        input_artifacts=("implementation_plan", "execution_brief"),
        output_artifact="code_change",
        produces_code_change=True,
        # Not `consultation`: a step whose output is a code change is either the
        # host's own edit or a delegated patch. Asking for one as a plain answer is
        # how a "here is roughly what I would write" reply becomes a code change.
        allowed_execution_modes=("patch", "isolated_write"),
        runs_from=("prompt_ready", "coding"),
        on_success="testing",
    ),
    "apply_patch": StepDefinition(
        capability=None,
        input_artifacts=("code_change",),
        output_artifact="code_change",
        produces_code_change=True,
        runs_from=("awaiting_host_apply",),
        on_success="testing",
    ),
    "test": StepDefinition(
        capability="testing",
        input_artifacts=("code_change",),
        output_artifact="test_report",
        # An agent can only be trusted to report an exit code it was contained while
        # producing. Everything else is the host's job.
        allowed_execution_modes=("isolated_write",),
        runs_from=("testing",),
        on_success="reviewing",
    ),
    "review": StepDefinition(
        capability="review",
        input_artifacts=("code_change", "test_report"),
        output_artifact="review_outcome",
        allowed_execution_modes=("consultation",),
        supports_multiple_agents=True,
        runs_from=("reviewing",),
        on_success="synthesizing",
    ),
    "synthesize": StepDefinition(
        capability="synthesis",
        input_artifacts=("review_outcome",),
        output_artifact="synthesis",
        allowed_execution_modes=("consultation",),
        runs_from=("synthesizing",),
        on_success="completed",
    ),
    "fix": StepDefinition(
        capability="coding",
        # Not `review_outcome`: a review's product is a review row, and the review
        # step stores nothing under its artifact name on purpose, so naming it here
        # asked for something that never exists. The findings reach this step through
        # `_open_findings` instead; these are the artifacts it reads.
        input_artifacts=("implementation_plan", "test_report"),
        output_artifact="code_change",
        produces_code_change=True,
        allowed_execution_modes=("patch", "isolated_write"),
        runs_from=("fixing",),
        on_success="testing",
    ),
}

# Which steps a host may record rather than run through an agent. Every one of them:
# the host can always do the work itself, and for `apply_patch` it is the only party
# that can.
HOST_RECORDABLE: frozenset[Step] = frozenset(STEPS)

# ...with one exception, and it is not recordable in either direction. The review
# step's product is a review row, and only `ReviewService` writes one. A host-recorded
# `review_outcome` would be a synthesis written straight into a step, which is exactly
# the path around `missing_serious` that the review layer exists to close. Said in one
# place because it is checked in two: when the binding is resolved, and again when the
# step is planned from the stored snapshot.
HOST_REVIEW_REFUSAL = (
    "the `review` step cannot be `executor: host`; bind it to reviewers so the "
    "findings go on the record and can be checked for survival"
)


def repository_access_for(executor: Executor, mode: ExecutionMode | None) -> RepositoryAccess:
    """What this resolved binding can reach.

    The host edits its own checkout; an agent under `isolated_write` gets a worktree;
    everything else -- a consultation, a returned patch -- sees only the context it
    was sent.
    """
    if executor == "host":
        return "active_tree"
    return "worktree" if mode == "isolated_write" else "context_only"


class StepSnapshot(BaseModel):
    """One step, fully resolved, immutable, and the only thing the private
    consultation path accepts.

    It carries an execution identity rather than an agent id because that is what
    exclusion is decided on: two agent ids can name the same `(runtime, model)`, and
    the check that matters is whether this is the host answering itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    step_id: str
    step: Step
    executor: Executor
    execution_mode: ExecutionMode | None = None
    repository_access: RepositoryAccess
    capability: Capability | None = None
    agent_id: str | None = None
    agent_runtime: Runtime | None = None
    agent_model: str | None = None
    round_index: int = Field(default=0, ge=0)
    attempt: int = Field(default=1, ge=1)
    sequence: int = Field(default=0, ge=0)
    parent_step_id: str | None = None
    web: bool = False


# --- artifacts ----------------------------------------------------------------
#
# Validated rather than trusted, and generated into a JSON Schema for the runtimes
# that take one, the way `ConsultationContent` already is. `extra="forbid"` on every
# object because OpenAI's structured outputs reject a schema without it.


class ResearchBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    findings: list[str]
    open_questions: list[str]


class PlannedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    intent: str


class ImplementationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    changes: list[PlannedChange]
    order: list[str]
    validation_strategy: str
    risks: list[str]
    acceptance_criteria: list[str]


class ExecutionBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    constraints: list[str]
    steps: list[str]
    done_when: list[str]


class CodeChange(BaseModel):
    """What one implementation or fix round produced.

    `patch` is the sanitized audit copy. It is never what gets applied: the raw text
    is returned by the run and recoverable through workflow status until application;
    `raw_patch_sha256` ties it to this record. A scrubbed patch is a corrupt patch.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str
    files: list[str]
    patch: str = ""
    raw_patch_sha256: str = ""
    commit: str = ""
    # Written by a contained step, absent from `patch`, and gone with the worktree.
    # Empty for every other mode. Recorded so "the patch is the whole change" is a
    # claim the artifact can contradict rather than one nothing checks.
    ignored: list[str] = []


class TestReport(BaseModel):
    """What one test run did, and the exit code that says so.

    `status` and `exit_code` are cross-checked rather than stored side by side. They
    disagreed freely before, which meant `exit_code: 1` with `status: "passed"` was a
    report the store accepted and `_loop_done` believed -- and `_loop_done` reads
    `status` alone, so the number nobody compared it against was the only evidence
    there was.
    """

    model_config = ConfigDict(extra="forbid")

    command: str
    workdir: str
    # Nullable, because "no exit code was read" is a real outcome and zero is not how
    # to say it. A denied command leaves no event, while a timeout may either kill the
    # process before it reports or come from a wrapper that returns a non-zero code.
    exit_code: int | None
    status: Literal["passed", "failed", "timeout", "skipped"]
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: int = Field(default=0, ge=0)
    commit: str = ""
    # What the run changed while it was supposedly only testing. Normally empty, and
    # a contained test step's worktree is deleted either way, so this is not a patch
    # anyone can apply -- it is how "the tests pass" and "the code was edited to make
    # them pass" stop being the same-looking result.
    changed_files: list[str] = []
    # Set by the service. A value supplied by a caller is dropped, so `orchestrator`
    # here always means this process read the exit code.
    reported_by: ReportedBy = "host"

    @model_validator(mode="after")
    def _status_matches_exit_code(self) -> TestReport:
        if self.status == "passed" and self.exit_code != 0:
            raise ValueError(
                "a report cannot be `passed` with a non-zero or absent exit code; "
                "use `failed`, or `skipped` when no exit code was read"
            )
        if self.status == "failed" and self.exit_code in (None, 0):
            raise ValueError("a report cannot be `failed` with exit code 0 or none at all")
        if self.status == "timeout" and self.exit_code == 0:
            raise ValueError(
                "a report cannot be `timeout` with exit code 0; use a non-zero wrapper "
                "code, or none when the process did not produce a result"
            )
        if self.status == "skipped" and self.exit_code is not None:
            raise ValueError("a report cannot be `skipped` with an exit code")
        return self


class SynthesisRecord(BaseModel):
    """The workflow's side of a review: a pointer and the computed outcome.

    The review itself lives in the reviews table, written by `ReviewService`. Nothing
    here is a substitute for it -- a synthesis written straight into a workflow step
    would route around `missing_serious`.
    """

    model_config = ConfigDict(extra="forbid")

    review_id: str
    loop_done: bool
    open_serious: int = Field(default=0, ge=0)
    reasons: list[str] = Field(default_factory=list)


# --- what the tools return ----------------------------------------------------


class AgentRef(BaseModel):
    """An agent as the workflow recorded it, not as the config reads today."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    runtime: str
    model: str


class StepView(BaseModel):
    """One step, as `status` reports it.

    `output` is the stored artifact, which is the *scrubbed* copy. For a patch that
    makes it an audit record and nothing else; the response-level `patch` carries the
    private recovery copy while application is pending.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str
    step: Step
    status: StepStatus
    executor: Executor
    execution_mode: ExecutionMode | None = None
    repository_access: RepositoryAccess
    agent_id: str | None = None
    round_index: int = 0
    attempt: int = 1
    sequence: int = 0
    review_id: str | None = None
    reported_by: ReportedBy | None = None
    raw_patch_sha256: str | None = None
    output: dict | None = None
    # What this step spent, rebuilt from its consultation's turn ledger at read
    # time. `None` for a host step, which spends nothing here; a delegated step
    # whose agent reports no price has a usage with `cost_usd` unset rather than a
    # zero, because unmeasured is not free.
    usage: Usage | None = None


class StepPreview(BaseModel):
    """What `plan_step` approved: who it goes to and what it would carry.

    `confirm_token` is shown once and spent once, and it proves that what runs is
    what this preview described -- snapshot integrity, not that a human read it.
    """

    model_config = ConfigDict(extra="forbid")

    # Blank and null for a replan, which previews a routing change rather than a
    # step. One preview shape for both, because a token is a token: whatever issued
    # it, spending it is the same handshake.
    step_id: str = ""
    step: Step | None = None
    executor: Executor = "host"
    execution_mode: ExecutionMode | None = None
    repository_access: RepositoryAccess = "active_tree"
    capability: Capability | None = None
    agents: list[AgentRef] = Field(default_factory=list)
    web: bool = False
    inputs: list[ArtifactType] = Field(default_factory=list)
    prompt_chars: int = 0
    prompt_sha256: str = ""
    # A review step's token *is* the review plan's token. One approval, one review;
    # minting a second one here would mean approving the same send twice.
    review_id: str | None = None
    confirm_token: str


class WorkflowView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    status: WorkflowState
    goal: str
    workdir: str
    fix_rounds: int = 0
    max_fix_rounds: int = 0
    baseline_commit: str | None = None
    result_commit: str | None = None
    reason: str | None = None
    bindings: dict = Field(default_factory=dict)
    steps: list[StepView] = Field(default_factory=list)
    # Which steps may be planned from where the workflow is now. Derived from
    # `runs_from`, so it cannot disagree with what `plan_step` will accept.
    next_steps: list[Step] = Field(default_factory=list)
    # The steps' spend added up, reviewers included. `cost_usd` is set only when
    # every step that spent anything reported a price -- one unpriced agent makes
    # the total a floor, and a floor presented as a sum is worse than no number.
    usage: Usage | None = None


class WorkflowResponse(BaseModel):
    """One envelope for every workflow tool, the same as the other two layers.

    `patch` is the exception that proves the storage rule: the raw diff is returned
    here because the stored copy is scrubbed and a scrubbed patch does not apply. It
    may be recovered from a private file while application is pending, never SQLite.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    status: WorkflowState
    workflow: WorkflowView | None = None
    preview: StepPreview | None = None
    step: StepView | None = None
    patch: str = ""
    # Recovery is deliberately weaker than the primary response: a full disk or an
    # unsafe file must not discard a paid patch or make status unreadable.
    recovery_warning: str = Field(default="", max_length=MAX_ERROR_CHARS)
    latency_ms: int = 0
    error: ConsultError | None = None

    def check_invariants(self) -> WorkflowResponse:
        if self.preview is not None and self.preview.confirm_token == "":
            raise AssertionError("a preview that approves nothing is not a preview")
        if self.patch and self.step is None:
            raise AssertionError("a raw patch belongs to the step that produced it")
        return self


class WorkflowDeleteApproval(BaseModel):
    """A count, and a token good for exactly the workflows behind it."""

    model_config = ConfigDict(extra="forbid")

    workflows: int = Field(ge=0)
    confirm_token: str = Field(min_length=1)
    expires_in_s: int = Field(ge=0)


class WorkflowDeletionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: int = Field(
        ge=0, description="Workflows removed, with their steps, consultations and reviews."
    )


ARTIFACT_MODELS: dict[ArtifactType, type[BaseModel]] = {
    "research_brief": ResearchBrief,
    "implementation_plan": ImplementationPlan,
    "execution_brief": ExecutionBrief,
    "code_change": CodeChange,
    "test_report": TestReport,
    "synthesis": SynthesisRecord,
    # `review_outcome` has no model here: the review row is the artifact, and the
    # workflow step stores its id.
}
