"""Request/response contract for the consultation path.

One envelope for every outcome, a failure never carries an answer, and the models
here are what the advertised MCP schema is derived from.

`ConsultationContent` does double duty -- it is both what we validate a reply
against and, as a generated JSON Schema, what we hand to the target CLI's
structured-output flag. Generating it from the model is what keeps the two from
drifting apart.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..contract import MAX_ERROR_CHARS, Usage
from .errors import ConsultErrorCode

# The consultation capabilities are a fixed vocabulary, not the operator's
# `capabilities:` block: an agent's score is only meaningful against a name that
# means the same thing in every installation.
CONSULT_CAPABILITIES = ("coding", "research", "writing", "reasoning", "review")

Capability = Literal["coding", "research", "writing", "reasoning", "review"]
Runtime = Literal["codex", "claude", "antigravity"]

PROTOCOL_VERSION = "consult-v1"

# Caps on the free text a caller may send. Generous enough for a real document-mode
# consultation -- a megabyte of context is a long paper -- and finite, which is the
# point: every one of these is copied into a compiled prompt, encoded onto a child's
# stdin, and stored, so an uncapped field is several times its own size in memory
# and on disk before anything refuses it.
MAX_PROMPT_CHARS = 100_000
MAX_CONTEXT_CHARS = 1_000_000
MAX_LABEL_CHARS = 200


class SourceMode(str, Enum):
    AUTO = "auto"
    DOCUMENT = "document"
    WEB = "web"
    MODEL = "model"


class ConsultRequest(BaseModel):
    """Base shape. `build_consult_request` narrows `target_agent` to the configured
    agent ids and applies the configured length caps."""

    model_config = ConfigDict(extra="forbid")

    capability: Capability = Field(description="Which capability this consultation needs.")
    prompt: str = Field(
        min_length=1,
        max_length=MAX_PROMPT_CHARS,
        description="The task or question for the consulted agent.",
    )
    context: str | None = Field(
        default=None,
        max_length=MAX_CONTEXT_CHARS,
        description="Source material. Treated as untrusted evidence, never as instructions.",
    )
    source_mode: SourceMode = Field(
        default=SourceMode.AUTO,
        description=(
            "`auto` picks `document` when `context` is set and `model` otherwise. "
            "`document` grounds the answer in `context` with every tool disabled. "
            "`web` enables only the target runtime's web search. `model` uses neither."
        ),
    )
    consultation_id: UUID | None = Field(
        default=None,
        description=(
            "Omit on the first call. The response returns one; send it back on every "
            "later call in the same conversation to continue the same session."
        ),
    )
    target_agent: str | None = Field(
        default=None, description="Optional explicit agent id, overriding the scores."
    )
    conversation_label: str | None = Field(
        default=None,
        max_length=MAX_LABEL_CHARS,
        description="Free-text label recorded with the consultation.",
    )


class ConsultSource(BaseModel):
    # Not decoration: OpenAI's structured outputs reject a schema unless *every* object
    # in it, `$defs` included, says `additionalProperties: false`. Without this the
    # provider 400s before the model is ever asked anything.
    model_config = ConfigDict(extra="forbid")

    title: str
    locator: str
    source_type: Literal["document", "web", "model"]


class ConsultationContent(BaseModel):
    """What a consulted agent must return. Every field is required.

    Arrays may be empty but may never be omitted: "no assumptions" is a claim the
    consulted agent has to make, not one the caller has to infer from a missing key.
    """

    model_config = ConfigDict(extra="forbid")

    answer: str
    assumptions: list[str]
    uncertainties: list[str]
    follow_up_questions: list[str]
    sources: list[ConsultSource]


class ConsultRoute(BaseModel):
    """Why this agent answered. Recorded, and returned so the caller can see it."""

    agent_id: str
    runtime: Runtime
    model: str
    capability_score: int
    priority: int
    explicitly_selected: bool
    # Whether `model` is what the runtime said answered, or only what it was asked for.
    # A substitution is already refused, so False does not mean "wrong model" -- it means
    # the runtime named none and nobody checked. Without this the two are indistinguish-
    # able in the envelope, which is the same as not having the check.
    model_verified: bool = False


class RequiredAction(BaseModel):
    """A command for the *user* to run. Never run by this server, which has no
    business touching another vendor's login flow."""

    command: str
    retry_after_connection: bool = True


class ConsultError(BaseModel):
    code: ConsultErrorCode
    # Same bound and the same reason as `ErrorInfo`: the sources feeding this quote
    # their input back verbatim and none of them bound it.
    message: str = Field(max_length=MAX_ERROR_CHARS)
    agent_id: str | None = None
    required_action: RequiredAction | None = None


class ConsultResponse(BaseModel):
    """One envelope for every outcome.

    Invariants (asserted by `check_invariants`, covered by tests):
      * `ok is False` iff `error is not None`
      * when `ok is False`, `content` is None -- a failed consultation never carries
        text a caller could read as the consulted agent's answer.
    """

    ok: bool
    # Null only when the failure happened before a consultation existed -- a bad
    # request, or no eligible agent. Any other outcome has an id worth sending back.
    consultation_id: UUID | None = None
    content: ConsultationContent | None = None
    capability_requested: str
    source_mode_used: SourceMode
    route: ConsultRoute | None = None
    usage: Usage | None = None
    latency_ms: int = 0
    error: ConsultError | None = None

    def check_invariants(self) -> ConsultResponse:
        # Explicit raises, not `assert`: `python -O` strips assertions, and the
        # never-ghostwrite guard is exactly the thing that must not vanish there.
        if self.ok is not (self.error is None):
            raise AssertionError("ok must be False exactly when error is set")
        if not self.ok and self.content is not None:
            raise AssertionError("failed consultations carry no answer")
        return self


class ConsultAgentInfo(BaseModel):
    """One row of `list_consult_agents`."""

    agent_id: str
    runtime: Runtime
    model: str
    priority: int
    enabled: bool
    scores: dict[str, int]
    web_search: bool
    excluded_as_host: bool
    installed: bool | None = None
    authenticated: bool | None = None
    detail: str | None = None


class ConsultAgentsResponse(BaseModel):
    host_runtime: Runtime
    agents: list[ConsultAgentInfo]


class ConsultationRecord(BaseModel):
    """What `get_consultation` returns: the session, its turns, and why this agent."""

    consultation_id: UUID
    target_agent_id: str
    target_runtime: Runtime
    target_model: str
    capability: str
    source_modes: list[str]
    conversation_label: str | None
    status: str
    native_session_bound: bool
    created_at: str
    updated_at: str
    turns: list[dict[str, Any]]
    routing: list[dict[str, Any]]


def _agent_enum(agent_ids: list[str]):
    """Advertise `target_agent` as the configured ids, or null.

    A calling model reads the enum out of the tool schema, and one that cannot name
    an unconfigured agent cannot ask for one. Written flat rather than as an `anyOf`, which is what a
    nullable `Literal` would otherwise produce, because a plain enum survives
    translation into a function-calling definition far more widely.
    """

    def patch(schema: dict[str, Any]) -> None:
        schema.pop("const", None)
        schema.pop("anyOf", None)
        schema["type"] = ["string", "null"]
        schema["enum"] = [*agent_ids, None]

    return patch


def build_consult_request(agent_ids: list[str]) -> type[ConsultRequest]:
    """Specialize `ConsultRequest` to the configured agents."""
    if not agent_ids:
        raise ValueError("no consult agents configured")

    return create_model(
        "ConsultRequest",
        __base__=ConsultRequest,
        target_agent=(
            Literal[tuple(agent_ids)] | None,  # type: ignore[valid-type]
            Field(
                default=None,
                description=ConsultRequest.model_fields["target_agent"].description,
                json_schema_extra=_agent_enum(agent_ids),
            ),
        ),
    )


def consultation_content_schema() -> dict[str, Any]:
    """The JSON Schema handed to `--json-schema` / `--output-schema`.

    Generated from the model rather than written out, so what the target is told to
    produce and what we validate can never disagree.
    """
    return ConsultationContent.model_json_schema()
