"""Request/response contract for the review layer.

Same discipline as `consult/contract.py`: `extra="forbid"` everywhere, one
envelope for every outcome, and an invariant check that raises rather than
asserts so `python -O` cannot strip it.

Every field here is bounded. Not as tidiness -- each of these is copied into a
compiled prompt, JSON-encoded, hashed and stored, so an uncapped list is several
times its own size in memory and on disk before anything refuses it. The caps are
the contract; service-side truncation is a convenience on top of them.

Two fields exist to stop a whole class of quiet failure:

* `Finding.finding_id` is assigned by this server, never by a model. Ids that a
  reviewer invents can collide across reviewers, and the summary check below
  resolves references by id.
* `CombinedFinding.source_finding_ids` is what makes "a lone Critical survives"
  checkable instead of merely requested. `missing_criticals` refuses a synthesis
  that references no combined finding for some reviewer's Critical.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from ..contract import Usage
from ..consult.contract import MAX_CONTEXT_CHARS as CONSULT_MAX_CONTEXT_CHARS
from ..consult.contract import ConsultError, ConsultSource, Runtime

ReviewMode = Literal["standard", "deep"]
ReviewStatus = Literal[
    "pending", "running", "awaiting_synthesis", "complete", "failed", "cancelled"
]
Outcome = Literal["all", "some", "none"]
Severity = Literal["critical", "important", "minor", "uncertain"]
# What became of the findings a round took on. `reverted` is named rather than
# folded into `skipped` because undoing a fix that made things worse is a real
# result worth reading back, and one a later round should not repeat.
FixOutcome = Literal["applied", "partial", "reverted", "skipped"]

# Ordered worst-first, so truncation is severity-ordered and the cap can never be
# what drops a Critical.
SEVERITY_ORDER: dict[str, int] = {"critical": 0, "important": 1, "minor": 2, "uncertain": 3}

MAX_GOAL_CHARS = 20_000
MAX_LABEL_CHARS = 500
MAX_TEXT_CHARS = 5_000
MAX_MATERIAL_ITEMS = 200
MAX_SUMMARY_CHARS = 50_000
MAX_FINDINGS = 200
MAX_LIST_ITEMS = 100
MAX_SECRET_HITS = 500
MAX_REVIEWERS = 5
# Fix rounds live in one JSON column on the review row, so the column grows with
# every round recorded against it. Fifty is far past any real fix-and-recheck
# cycle and still small enough that the row stays a row.
MAX_FIX_ROUNDS = 50

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MaterialItem(BaseModel):
    """One thing that went into `context`.

    Two provenances, told apart by `ReviewPlan.material_verified`. When the host
    passed `context` as a string this is host-declared and unverifiable -- the
    server did not read a file, so it cannot check that the manifest describes the
    material, and the entry is a disclosure aid that puts the host's sources on the
    record. When the host passed `context_paths` the server built these entries
    from the bytes it actually read, and they are measurements.

    Either way, being inside the approval hash, the manifest cannot be rewritten
    between the preview and the send.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS, description="e.g. `src/auth.py`")
    kind: Literal["file", "text"]
    locator: str = Field(default="", max_length=MAX_LABEL_CHARS, description="e.g. `lines 1-180`")
    chars: int = Field(default=0, ge=0, le=CONSULT_MAX_CONTEXT_CHARS)


class SecretHit(BaseModel):
    """Where something credential-shaped was found. A position, never a value."""

    model_config = ConfigDict(extra="forbid")

    field: Literal["goal", "context"]
    line: int = Field(ge=1)


class RawReviewMaterial(BaseModel):
    """The original text, re-supplied by a caller choosing `secrets="send_as_is"`.

    Two named fields rather than one string: one blob could not reconstruct which
    half of the material a redaction touched, and the raw hash is computed over
    both. This exists because a user may knowingly approve what the regex flagged
    -- a variable *named* `password`, a fake key in a fixture.
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=MAX_GOAL_CHARS)
    context: str | None = Field(default=None, max_length=CONSULT_MAX_CONTEXT_CHARS)


class ReviewerSnapshot(BaseModel):
    """What the user approved sending to, not just what it was called.

    An agent id is a label. Between plan and run -- a config edit, a restart, a
    dashboard save -- an id can move to another runtime or model, and the material
    would go somewhere nobody approved. This is hashed into the approval and
    re-compared before the first run and before every retry.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(max_length=MAX_LABEL_CHARS)
    runtime: Runtime
    model: str = Field(max_length=MAX_LABEL_CHARS)
    web_search: bool = False


class Finding(BaseModel):
    """One reviewer's single finding, after parsing."""

    model_config = ConfigDict(extra="forbid")

    # Server-assigned (`{agent_id}-{n}`). A model's own ids collide across
    # reviewers, and the summary check resolves references by this.
    finding_id: str = Field(max_length=MAX_LABEL_CHARS)
    agent_id: str = Field(max_length=MAX_LABEL_CHARS)
    location: str = Field(default="", max_length=MAX_LABEL_CHARS)
    severity: Severity
    why: str = Field(default="", max_length=MAX_TEXT_CHARS)
    example: str = Field(default="", max_length=MAX_TEXT_CHARS)
    fix: str = Field(default="", max_length=MAX_TEXT_CHARS)


class ReviewerResult(BaseModel):
    """One reviewer's outcome, whichever way it went.

    `ok=False` carries an error and no findings -- the same never-ghostwrite rule
    as `ConsultResponse`. `findings_parsed=False` with `ok=True` is the common
    partial case: the reviewer answered in prose the parser could not read, and
    the prose is still worth showing.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(max_length=MAX_LABEL_CHARS)
    ok: bool
    consultation_id: UUID | None = None
    findings: list[Finding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    findings_parsed: bool = False
    findings_truncated: int = Field(
        default=0, ge=0, description="Findings dropped by the cap, lowest severity first."
    )
    answer: str | None = Field(default=None, max_length=CONSULT_MAX_CONTEXT_CHARS)
    # The rest of `ConsultationContent`, kept rather than discarded: a web-mode
    # reviewer's citations live in `sources`, and dropping them here would mean the
    # synthesis could not carry them however carefully it was asked to.
    assumptions: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    uncertainties: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    follow_up_questions: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    sources: list[ConsultSource] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    # Null on a reviewer rebuilt from its stored row: the cost was recorded against
    # the consultation, not copied into the review table.
    usage: Usage | None = None
    error: ConsultError | None = None


class ReviewPlan(BaseModel):
    """What would be sent, shown before anything is.

    Returned by `orchestrator_review` and by nothing else. `confirm_token` is handed back once
    and only its sha256 is stored, so it cannot be replayed and cannot be read out
    of the database.
    """

    model_config = ConfigDict(extra="forbid")

    mode: ReviewMode
    reviewers: list[ReviewerSnapshot] = Field(max_length=MAX_REVIEWERS)
    material: list[MaterialItem] = Field(default_factory=list, max_length=MAX_MATERIAL_ITEMS)
    # True only when the server assembled `context` itself from `context_paths`, so
    # the manifest is a measurement rather than the host's claim. See `MaterialItem`.
    material_verified: bool = False
    goal_chars: int = Field(ge=0)
    context_chars: int = Field(ge=0)
    material_sha256: Sha
    web_requested: bool
    expected_requests: int = Field(ge=0)
    secret_hits: list[SecretHit] = Field(default_factory=list, max_length=MAX_SECRET_HITS)
    # Two reviewers on one model agree cheaply and mean nothing; worth saying before
    # the requests are paid for rather than after.
    duplicate_models: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    # Set when a reviewer runs the model the host declared as its own. Advisory:
    # `host_model` is host-declared and unverifiable, like the manifest.
    host_model_conflict: str | None = Field(default=None, max_length=MAX_LABEL_CHARS)
    confirm_token: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)


class CombinedFinding(BaseModel):
    """One row of the host AI's synthesis.

    `source_finding_ids` is load-bearing, not bookkeeping: it is how
    `missing_criticals` proves that no reviewer's Critical was dropped during
    synthesis, and how a reader gets from a combined row back to who said it.
    """

    model_config = ConfigDict(extra="forbid")

    problem: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    severity: Severity
    location: str = Field(default="", max_length=MAX_LABEL_CHARS)
    # Which reviewers raised it. Agreement is computed by the host, never asked of a
    # reviewer -- none of them can see the others' answers.
    agreed_by: list[str] = Field(default_factory=list, max_length=MAX_REVIEWERS)
    disagreed_by: list[str] = Field(default_factory=list, max_length=MAX_REVIEWERS)
    source_finding_ids: list[str] = Field(default_factory=list, max_length=MAX_FINDINGS)
    proposed_action: str = Field(default="", max_length=MAX_TEXT_CHARS)


class ReviewSummary(BaseModel):
    """The host AI's conclusion. The product of a review, and the only path to
    `complete` -- external models replying is not a finished review.

    `checked` and `not_checked` are both required so scope is stated rather than
    inferred from silence, and `citations` so a web-mode reviewer's sources reach
    the reader instead of stopping at the synthesis.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    combined_findings: list[CombinedFinding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    agreements: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    disagreements: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    recommendation: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    options: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    checked: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    not_checked: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    citations: list[ConsultSource] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class FixRound(BaseModel):
    """One pass of fixing, after the fact. A log entry, not an instruction.

    The server records what the host says it did; it does not edit a file, run a
    command, or check the claim. That is the whole of `orchestrator_apply_fixes` by design --
    a reviewer's finding is advice, and acting on it stays with the host AI and
    the user watching it.
    """

    model_config = ConfigDict(extra="forbid")

    finding_ids: list[str] = Field(default_factory=list, max_length=MAX_FINDINGS)
    outcome: FixOutcome
    notes: str = Field(default="", max_length=MAX_TEXT_CHARS)
    recorded_at: str = ""


class FixPlan(BaseModel):
    """What `orchestrator_apply_fixes` hands back: the selected findings, and the steps around
    editing them.

    `criticals_omitted` is the field that earns this a tool of its own. A
    selection that quietly leaves out a Critical is the same failure the synthesis
    check exists to prevent, one stage later -- so it is named here rather than
    discovered when the recheck finds it again.
    """

    model_config = ConfigDict(extra="forbid")

    findings: list[Finding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    criticals_omitted: list[str] = Field(default_factory=list, max_length=MAX_FINDINGS)
    steps: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


FIX_STEPS = [
    "Make a safety point first -- a commit or a stash -- so every change below can "
    "be undone in one move.",
    "Apply the fixes yourself. This server never edits a file and never runs a "
    "command; a reviewer never sees your repository at all.",
    "Run the project's tests, and say what they did.",
    "Keep or undo, and record which with `orchestrator_record_fix_round`.",
    "To re-review, plan a new review with `parent_review_id` set to this one and "
    "the diff as `context`. It gets the same preview and the same approval as any "
    "other review -- a recheck is not exempt from them.",
]


class ReviewResponse(BaseModel):
    """One envelope for every outcome, the same as `ConsultResponse`.

    Invariants (raised by `check_invariants`, covered by tests):
      * `pending` carries no results -- nothing has been sent
      * `failed` carries no findings
      * `complete` always carries a summary
      * a plan is only ever returned while `pending`
    """

    model_config = ConfigDict(extra="forbid")

    review_id: UUID
    mode: ReviewMode
    status: ReviewStatus
    outcome: Outcome | None = None
    plan: ReviewPlan | None = None
    results: list[ReviewerResult] = Field(default_factory=list, max_length=MAX_REVIEWERS)
    host_findings: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    summary: ReviewSummary | None = None
    fix_plan: FixPlan | None = None
    fix_rounds: list[FixRound] = Field(default_factory=list, max_length=MAX_FIX_ROUNDS)
    # Child reviews planned against this one, oldest first. The link lives on the
    # child's `parent_review_id`, so this is a read of the chain rather than a
    # second record of it that could disagree.
    rechecks: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    usage: Usage | None = None
    latency_ms: int = 0
    error: ConsultError | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unparsed_reviewers(self) -> list[str]:
        """Reviewers that answered in prose their findings block could not be read out of.

        Derived from `results` rather than stored, so it cannot drift from them.

        Load-bearing, not diagnostic. `missing_criticals` proves that no reviewer's
        Critical was dropped by checking the summary against the parsed findings --
        so for a reviewer with none, it proves nothing. A non-empty list is carried
        into the finalization refusal so the caller can see exactly which reviewer
        made Critical-survival unverifiable.
        """
        return [r.agent_id for r in self.results if r.ok and not r.findings_parsed]

    def check_invariants(self) -> ReviewResponse:
        # Explicit raises rather than `assert`, for the reason `ConsultResponse`
        # gives: `python -O` strips assertions, and these are exactly the guards
        # that must not vanish there.
        if self.status == "pending" and (self.results or self.summary is not None):
            raise AssertionError("a pending review has sent nothing and carries no results or summary")
        if self.status == "failed" and any(r.findings for r in self.results):
            raise AssertionError("a failed review carries no findings")
        # `error is None` because every refusal reports where the review actually is.
        # An error envelope stamped `complete` carries the refusal rather than
        # re-sending the synthesis that was stored earlier.
        if self.status == "complete" and self.error is None and self.summary is None:
            raise AssertionError("a complete review carries the synthesis that completed it")
        if self.plan is not None and self.status != "pending":
            raise AssertionError("a plan describes what has not been sent yet")
        if (self.fix_plan is not None or self.fix_rounds) and self.status == "pending":
            raise AssertionError("a pending review has no findings, so nothing to fix")
        return self


class ReviewListing(BaseModel):
    """One row of `orchestrator_list_reviews`. Metadata only -- the material is not in it."""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    parent_review_id: str | None = None
    mode: ReviewMode
    status: ReviewStatus
    outcome: Outcome | None = None
    reviewers: list[str] = Field(default_factory=list, max_length=MAX_REVIEWERS)
    created_at: str
    updated_at: str


class DeleteApproval(BaseModel):
    """What `orchestrator_request_delete_all` offers: a count, and a token good for exactly the
    reviews behind it. Confirming deletes that snapshot, not whatever exists by
    then."""

    model_config = ConfigDict(extra="forbid")

    reviews: int = Field(ge=0)
    confirm_token: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    expires_in_s: int = Field(ge=0)


class DeletionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: int = Field(ge=0, description="Reviews removed, rechecks included.")


def missing_criticals(
    results: list[ReviewerResult], summary: ReviewSummary
) -> list[str]:
    """Invalid or incomplete finding provenance, by `finding_id`.

    The whole reason for asking more than one reviewer is that a lone dissenting
    Critical survives. Asking for that in a prompt is unenforceable, so it is
    checked here instead: every Critical a reviewer raised must be referenced by
    some combined finding, and every referenced id must have been raised by a
    reviewer. Referenced, not agreed with -- a synthesis is free to conclude the
    finding is wrong, but not to drop it silently or invent its provenance.
    """
    referenced = {
        finding_id
        for combined in summary.combined_findings
        for finding_id in combined.source_finding_ids
    }
    known = {finding.finding_id for result in results for finding in result.findings}
    missing = [
        finding.finding_id
        for result in results
        for finding in result.findings
        if finding.severity == "critical" and finding.finding_id not in referenced
    ]
    unknown = sorted(referenced - known)
    return [*missing, *unknown]


REVIEWER_INSTRUCTIONS = """\
You are one of several independent reviewers. You cannot see the other reviewers' \
answers, and you must not speculate about them.

Review the material in the context. In your `answer`, first write your review in prose, \
then end with a single fenced JSON block, exactly:

```json
{"findings": [{"location": "src/auth.py:88", "severity": "critical", "why": "...", \
"example": "...", "fix": "..."}]}
```

Rules for that block:
- `severity` is one of `critical`, `important`, `minor`, `uncertain`, and nothing else.
- `critical` means data loss, a security hole, or incorrect results reaching a user.
- `location` names the file and line, or the smallest part of the material you can point at.
- `why` says what goes wrong, `example` gives a concrete case where it does, `fix` says \
what to change.
- Do not assign ids -- they are assigned for you.
- Do not claim agreement or disagreement with anyone. You have not seen their answers.
- An empty `findings` list is a real answer. Say so rather than inventing something.

The block is required and must be the last thing in your `answer`. It is parsed by a \
machine, so it must be valid JSON: escape every `"` inside a string as `\\"`, and every \
newline as `\\n`. Quoting code is the usual way this breaks -- if a snippet is awkward \
to escape, describe it instead of quoting it.

Every finding you state in prose must also appear in the block. The block is the review: \
a Critical argued in prose and left out of the block is not counted anywhere, and a \
synthesis that drops it will be accepted.

Prose without the block loses every finding you made: nothing downstream can rank them, \
group them, or check that your Critical survived. If you are asked again for the block, \
send only the block.
"""

# Sent as a second turn in the *same* consultation when the first answer's findings
# block could not be read. The material is already in that session's history, so this
# costs a few hundred characters rather than another copy of the context -- which is
# the whole reason it is a re-ask and not a re-run.
REASK_INSTRUCTIONS = """\
Your findings block could not be parsed, so every finding you just made is currently \
lost. Do not review anything again and do not change your conclusions.

Reply with the fenced JSON block for the review you just wrote, and nothing else:

```json
{"findings": [{"location": "src/auth.py:88", "severity": "critical", "why": "...", \
"example": "...", "fix": "..."}]}
```

It must be valid JSON: escape every `"` inside a string as `\\"` and every newline as \
`\\n`. If a code snippet is awkward to escape, describe it instead of quoting it. If you \
found nothing, send `{"findings": []}`.
"""
