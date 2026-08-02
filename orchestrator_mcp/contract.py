"""Request/response contract for the orchestrator.

Every `ask` call in and out of this server passes through the models defined here.
FastMCP derives the advertised MCP tool schema from these same classes, so the
contract a caller reads and the contract we validate against cannot drift apart.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model


class ErrorCode(str, Enum):
    """Closed set, so callers branch on a value instead of matching substrings."""

    INVALID_REQUEST = "invalid_request"
    NO_DEPLOYMENT = "no_deployment"
    UPSTREAM_ERROR = "upstream_error"
    RATE_LIMITED = "rate_limited"
    CONTEXT_EXCEEDED = "context_exceeded"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    TIMEOUT = "timeout"
    CONTENT_FILTERED = "content_filtered"
    AUTH_FAILED = "auth_failed"


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None


class Limits(BaseModel):
    """Boundary caps, read from the `limits:` block of the config."""

    max_prompt_chars: int = 100_000
    max_context_chars: int = 400_000
    max_output_tokens: int = 4096
    request_timeout_s: int = 120
    schema_repair_attempts: int = 1


class AskRequest(BaseModel):
    """Base shape. `build_ask_request` narrows `capability` to the configured set."""

    model_config = ConfigDict(extra="forbid")  # unknown keys are rejected, not ignored

    capability: str = Field(description="Which capability should handle this request.")
    prompt: str = Field(min_length=1, description="The task or question.")
    context: str | None = Field(
        default=None,
        description=(
            "Source material to ground the answer in. When set, the model is "
            "instructed to answer only from it and to abstain otherwise."
        ),
    )
    system: str | None = Field(
        default=None, description="Extra instructions prepended to the conversation."
    )
    response_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "JSON Schema for a structured answer. When set, the reply is validated "
            "against it and returned in `data`; `content` stays null."
        ),
    )
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, ge=1)


class AskResponse(BaseModel):
    """One envelope for every outcome, success or failure.

    Invariants (asserted by `check_invariants`, covered by tests):
      * `ok is False` iff `error is not None`
      * when `ok is False`, both `content` and `data` are None -- the server never
        writes prose of its own into a field callers read as a model's answer.
    """

    ok: bool
    content: str | None = None
    data: dict[str, Any] | None = None
    insufficient_context: bool = False
    capability_requested: str
    model_used: str | None = None
    fallback_used: bool = False
    finish_reason: str | None = None
    usage: Usage | None = None
    latency_ms: int = 0
    error: ErrorInfo | None = None

    def check_invariants(self) -> AskResponse:
        assert self.ok is (self.error is None), "ok must be False exactly when error is set"
        if not self.ok:
            assert self.content is None and self.data is None, "failed calls carry no answer"
        return self


class CapabilityInfo(BaseModel):
    name: str
    description: str
    deployments: list[str]
    fallbacks: list[str] = []


class CapabilitiesResponse(BaseModel):
    capabilities: list[CapabilityInfo]


def build_ask_request(capabilities: list[str], limits: Limits) -> type[AskRequest]:
    """Specialize `AskRequest` to the configured capabilities and limits.

    The capability enum and the length caps land in the advertised JSON Schema, so a
    bad capability or an oversized prompt is rejected by the protocol layer before
    any provider call is attempted.
    """
    if not capabilities:
        raise ValueError("no capabilities configured")

    return create_model(
        "AskRequest",
        __base__=AskRequest,
        capability=(
            Literal[tuple(capabilities)],  # type: ignore[valid-type]
            Field(description="Which capability should handle this request."),
        ),
        prompt=(
            str,
            Field(min_length=1, max_length=limits.max_prompt_chars, description="The task or question."),
        ),
        context=(
            str | None,
            Field(default=None, max_length=limits.max_context_chars),
        ),
        max_output_tokens=(
            int | None,
            Field(default=None, ge=1, le=limits.max_output_tokens),
        ),
    )
