"""Request/response contract for the orchestrator.

Every `ask` call in and out of this server passes through the models defined here.
FastMCP derives the advertised MCP tool schema from these same classes, so the
contract a caller reads and the contract we validate against cannot drift apart.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)


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
    OUTPUT_TRUNCATED = "output_truncated"  # the model ran out of room mid-answer


MAX_ERROR_CHARS = 500


class ErrorInfo(BaseModel):
    code: ErrorCode
    # Bounded by contract, not by habit at one call site: the sources feeding this
    # (provider exceptions, validator complaints, pydantic echoing the request) all
    # quote their input back verbatim and none of them bound it.
    message: str = Field(max_length=MAX_ERROR_CHARS)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None


class Limits(BaseModel):
    """Boundary caps, read from the `limits:` block of the config."""

    max_prompt_chars: int = Field(default=100_000, ge=1)
    max_context_chars: int = Field(default=400_000, ge=1)
    max_system_chars: int = Field(default=10_000, ge=1)
    max_schema_chars: int = Field(default=20_000, ge=1)
    max_output_tokens: int = Field(default=4096, ge=1)
    request_timeout_s: int = Field(default=120, ge=1)
    schema_repair_attempts: int = Field(default=1, ge=0)


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
    # A string is accepted as well as an object: some clients build their tool
    # definitions with a strict function-calling schema that will not carry a
    # free-form object, and those callers can send the schema as JSON text.
    response_schema: dict[str, Any] | str | None = Field(
        default=None,
        description=(
            "JSON Schema for a structured answer, as an object or as a JSON string. "
            "When set, the reply is validated against it and returned in `data`; "
            "`content` stays null."
        ),
    )
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, ge=1)

    @field_validator("response_schema", mode="before")
    @classmethod
    def _parse_schema_string(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"`response_schema` was a string but not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("`response_schema` must decode to an object")
        return parsed

    @model_validator(mode="after")
    def _pin_structured_temperature(self) -> AskRequest:
        # Structured extraction has no business being creative.
        if self.response_schema is not None:
            self.temperature = 0.0
        return self


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
        # Explicit raises, not `assert`: `python -O` strips assertions, and the
        # never-ghostwrite guard is exactly the thing that must not vanish there.
        if self.ok is not (self.error is None):
            raise AssertionError("ok must be False exactly when error is set")
        if not self.ok and (self.content is not None or self.data is not None):
            raise AssertionError("failed calls carry no answer")
        return self


class CapabilityInfo(BaseModel):
    name: str
    description: str
    deployments: list[str]
    fallbacks: list[str] = []


class CapabilitiesResponse(BaseModel):
    capabilities: list[CapabilityInfo]


def _always_an_enum(capabilities: list[str]):
    """Pydantic emits `const` for a one-element Literal, and a single-capability
    config is a perfectly normal config. Clients that translate an MCP schema into a
    function-calling definition understand `enum` far more widely than `const`."""

    def patch(schema: dict[str, Any]) -> None:
        schema.pop("const", None)
        schema["type"] = "string"
        schema["enum"] = list(capabilities)

    return patch


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
            Field(
                description="Which capability should handle this request.",
                json_schema_extra=_always_an_enum(capabilities),
            ),
        ),
        prompt=(
            str,
            Field(min_length=1, max_length=limits.max_prompt_chars, description="The task or question."),
        ),
        # Redefining a field replaces it outright, so the descriptions are repeated
        # here -- they are what the calling agent reads.
        context=(
            str | None,
            Field(
                default=None,
                max_length=limits.max_context_chars,
                description=AskRequest.model_fields["context"].description,
            ),
        ),
        system=(
            str | None,
            Field(
                default=None,
                max_length=limits.max_system_chars,
                description=AskRequest.model_fields["system"].description,
            ),
        ),
        max_output_tokens=(
            int | None,
            Field(default=None, ge=1, le=limits.max_output_tokens),
        ),
    )
