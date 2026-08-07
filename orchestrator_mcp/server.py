"""Capability-routed MCP server.

A capability is a LiteLLM `model_name` alias group. Routing, retries, cooldowns and
cross-capability fallback all happen inside `litellm.Router`; this module owns the
MCP surface, the config contract, and the response envelope.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import jsonschema
import litellm
import yaml
from litellm import Router
from litellm.types.router import RouterErrors
from mcp.server import MCPServer
from pydantic import ValidationError

from .contract import (
    MAX_ERROR_CHARS,
    AskRequest,
    AskResponse,
    CapabilitiesResponse,
    CapabilityInfo,
    ConfigError,
    ErrorCode,
    ErrorInfo,
    Limits,
    Usage,
    build_ask_request,
    redact,
)
from .consult.config import host_runtime, load_consult_config
from .consult.contract import ConsultAgentsResponse, ConsultationRecord, ConsultResponse
from .consult.service import ConsultService
from .consult.store import ConsultStore
from .review.contract import (
    CombinedFinding,
    DeleteApproval,
    DeletionResult,
    MaterialItem,
    RawReviewMaterial,
    ReviewListing,
    ReviewMode,
    ReviewResponse,
    ReviewSummary,
)
from .review.service import ReviewService
from .review.store import DELETE_CONFIRM_TTL_S

__all__ =["ConfigError", "Orchestrator", "build_server", "load_config", "main", "validate_config"]

CONFIG_ENV = "ORCHESTRATOR_CONFIG"
DEFAULT_CONFIG = "config.yaml"

ABSTAIN_SENTINEL = "INSUFFICIENT_CONTEXT"

GROUNDING_DIRECTIVE = (
    "Answer using only the material inside the <context> block. Do not rely on "
    "outside knowledge and do not infer facts the context does not state.\n"
    f"If the context does not support an answer, reply with {ABSTAIN_SENTINEL} as "
    "the first line, then briefly say what is missing. Do not guess."
)

SCHEMA_DIRECTIVE = (
    "Reply with a single JSON object matching this schema and nothing else -- no "
    "prose, no markdown fence:\n{schema}\n"
    "Set `insufficient_context` to true and omit `answer` when the material given "
    "does not support an answer. Never invent a value to satisfy the schema."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# An exhausted alias group is signalled by message, not by type. Matching LiteLLM's
# own constants means a reworded message follows the upgrade instead of quietly
# downgrading to `upstream_error`.
_NO_DEPLOYMENT_MARKERS = tuple(
    marker.lower()
    for marker in (
        RouterErrors.no_deployments_available.value,
        RouterErrors.no_deployments_with_tag_routing.value,
        RouterErrors.no_deployments_with_provider_budget_routing.value,
        "No healthy deployment available",
    )
)

# Ordered most-specific first: several of these subclass BadRequestError/APIError.
_ERROR_MAP: list[tuple[type[Exception], ErrorCode]] = [
    (litellm.ContextWindowExceededError, ErrorCode.CONTEXT_EXCEEDED),
    (litellm.ContentPolicyViolationError, ErrorCode.CONTENT_FILTERED),
    (litellm.RateLimitError, ErrorCode.RATE_LIMITED),
    (litellm.AuthenticationError, ErrorCode.AUTH_FAILED),
    (litellm.Timeout, ErrorCode.TIMEOUT),
    (litellm.BadRequestError, ErrorCode.INVALID_REQUEST),
    (litellm.APIConnectionError, ErrorCode.UPSTREAM_ERROR),
    (litellm.APIError, ErrorCode.UPSTREAM_ERROR),
]


class StructuredOutputError(ValueError):
    """The reply was not usable as structured data. Never surfaced as an answer.

    Two forms on purpose. `str(exc)` is caller-safe: what failed and where, never the
    rejected value. `detail` may quote the model's own output and goes only back to
    that model in a repair turn.
    """

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail or message


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get(CONFIG_ENV, DEFAULT_CONFIG))
    if not path.exists():
        raise ConfigError(f"config not found: {path} (set {CONFIG_ENV} or create {DEFAULT_CONFIG})")
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict):
        raise ConfigError(f"config must be a YAML mapping: {path}")
    return config


def validate_config(config: dict[str, Any]) -> None:
    capabilities = config.get("capabilities") or {}
    model_list = config.get("model_list") or []

    # Neither block is required any more: `consult:` alone is a complete server, and
    # one that needs no API key at all, which is the point of running it that way.
    # What is still required is that the config configure *something*.
    if not capabilities and not model_list:
        if not config.get("consult"):
            raise ConfigError(
                "nothing is configured: give the server `capabilities:` and `model_list:` "
                "for `ask`, a `consult:` block for consultations, or both"
            )
        return

    # Half of the pair is a mistake rather than a choice, so both original refusals
    # stand -- an operator who wrote one and forgot the other wants to hear about it.
    if not isinstance(capabilities, dict) or not capabilities:
        raise ConfigError("`capabilities:` must be a non-empty mapping of name -> description")
    if not model_list:
        raise ConfigError("`model_list:` is empty; no capability has a deployment behind it")

    declared = set(capabilities)
    routed: set[str] = set()
    for entry in model_list:
        name = entry.get("model_name")
        if not name:
            raise ConfigError(f"model_list entry is missing `model_name`: {entry}")
        if name not in declared:
            raise ConfigError(
                f"deployment routes to '{name}', which is not declared under `capabilities:`"
            )
        routed.add(name)

    if orphans := declared - routed:
        raise ConfigError(f"capabilities with no deployment behind them: {sorted(orphans)}")

    for mapping in config.get("router_settings", {}).get("fallbacks") or []:
        for source, targets in mapping.items():
            for name in [source, *targets]:
                if name not in declared:
                    raise ConfigError(f"fallback references unknown capability '{name}'")


def _classify(exc: Exception) -> ErrorCode:
    message = str(exc).lower()
    if any(marker in message for marker in _NO_DEPLOYMENT_MARKERS):
        return ErrorCode.NO_DEPLOYMENT
    for exc_type, code in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return code
    return ErrorCode.UPSTREAM_ERROR


def _build_messages(
    prompt: str,
    context: str | None,
    system: str | None,
    schema: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Fixed order, our directives last, so a caller's `system` cannot disable them."""
    preamble = [
        p
        for p in (
            system.strip() if system else None,
            GROUNDING_DIRECTIVE if context else None,
            SCHEMA_DIRECTIVE.format(schema=json.dumps(schema)) if schema else None,
        )
        if p
    ]
    messages: list[dict[str, str]] = []
    if preamble:
        messages.append({"role": "system", "content": "\n\n".join(preamble)})
    user = f"<context>\n{context}\n</context>\n\n{prompt}" if context else prompt
    messages.append({"role": "user", "content": user})
    return messages


def _wrap_schema(user_schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap the caller's schema so abstention has somewhere to live.

    `answer` stays optional: a model that cannot answer must not be forced to invent
    a value that satisfies the schema.
    """
    return {
        "type": "object",
        "properties": {
            "insufficient_context": {
                "type": "boolean",
                "description": "True when the provided material does not support an answer.",
            },
            "answer": user_schema,
        },
        "required": ["insufficient_context"],
        "additionalProperties": False,
    }


def _parse_structured(
    text: str | None, wrapper_schema: dict[str, Any]
) -> tuple[dict[str, Any] | None, bool]:
    """Parse and validate a structured reply. Raises rather than returning a guess."""
    if not text or not text.strip():
        raise StructuredOutputError("model returned an empty response")
    try:
        parsed = json.loads(_FENCE.sub("", text.strip()))
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"response was not valid JSON: {exc}") from exc

    try:
        jsonschema.validate(parsed, wrapper_schema)
    except jsonschema.ValidationError as exc:
        # `exc.message` inlines the rejected value, unbounded. It is fine to hand back
        # to the model that wrote it; it is not part of the caller's error envelope.
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise StructuredOutputError(
            f"schema violation at {path}: failed the `{exc.validator}` constraint",
            detail=f"schema violation at {path}: {exc.message}",
        ) from exc
    except Exception as exc:
        # An unresolvable `$ref` and friends raise from inside the validator rather
        # than as a ValidationError. Still a rejected reply, not a crash.
        raise StructuredOutputError(f"schema could not be applied: {type(exc).__name__}") from exc

    abstained = bool(parsed.get("insufficient_context"))
    if not abstained and "answer" not in parsed:
        raise StructuredOutputError("`answer` is required unless insufficient_context is true")
    return (None if abstained else parsed["answer"]), abstained


# How much of the budget is reserved for the Router to time out, report, and cool down
# a deployment before the outer deadline cancels the whole call.
_TIMEOUT_MARGIN_S = 0.5


# A deny-list, not an allow-list: LiteLLM normalizes `finish_reason` to the OpenAI
# set, so this covers what it emits, and an unfamiliar reason from a custom provider
# must not fail an otherwise complete answer.
_REFUSED_FINISH: dict[str, tuple[ErrorCode, str]] = {
    "length": (ErrorCode.OUTPUT_TRUNCATED, "the model hit the output limit mid-answer"),
    "max_tokens": (ErrorCode.OUTPUT_TRUNCATED, "the model hit the output limit mid-answer"),
    "content_filter": (ErrorCode.CONTENT_FILTERED, "the provider filtered this completion"),
    # The model stopped to call a tool. This server exposes none, so whatever text
    # came with it is a preamble, not an answer.
    "tool_calls": (ErrorCode.UPSTREAM_ERROR, "the model stopped to call a tool, not to answer"),
    "function_call": (ErrorCode.UPSTREAM_ERROR, "the model stopped to call a tool, not to answer"),
}

# LiteLLM maps several provider reasons to "stop" that do not mean "finished answering",
# keeping the original in `provider_specific_fields`. Compared upper-case because the
# providers disagree on casing.
_REFUSED_NATIVE_FINISH: frozenset[str] = frozenset(
    {
        "MALFORMED_FUNCTION_CALL",
        "MALFORMED_RESPONSE",
        "FINISH_REASON_UNSPECIFIED",
        "TOO_MANY_TOOL_CALLS",
        "NETWORK_ERROR",
        "ERROR",
    }
)


def _completion_of(response: Any) -> tuple[str | None, tuple[ErrorCode, str] | None]:
    """The one place a provider's reply shape is trusted. Returns (content, refusal).

    Everything past this point may assume a complete, non-empty completion exists. A
    truncated answer is dropped rather than returned: a half answer that reads as a
    whole one is the failure this server exists to prevent.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None, (ErrorCode.UPSTREAM_ERROR, "provider returned no completion")

    choice = choices[0]
    if refusal := _REFUSED_FINISH.get(getattr(choice, "finish_reason", None) or ""):
        return None, refusal

    native = (getattr(choice, "provider_specific_fields", None) or {}).get("native_finish_reason")
    if isinstance(native, str) and native.upper() in _REFUSED_NATIVE_FINISH:
        return None, (
            ErrorCode.UPSTREAM_ERROR,
            f"the provider stopped for {native}, which LiteLLM reports as a normal stop",
        )

    content = getattr(getattr(choice, "message", None), "content", None)
    # A non-string body (content blocks from a custom provider) is a shape this server
    # does not read, not an answer to hand on.
    if not isinstance(content, str) or not content.strip():
        return None, (ErrorCode.UPSTREAM_ERROR, "provider returned an empty completion")
    return content, None


def _split_abstention(text: str | None) -> tuple[str | None, bool]:
    if text and text.lstrip().startswith(ABSTAIN_SENTINEL):
        remainder = text.lstrip()[len(ABSTAIN_SENTINEL) :].lstrip(" :\n")
        return (remainder or None), True
    return text, False


class Orchestrator:
    """Owns the Router and answers `ask` calls with a validated envelope."""

    def __init__(self, config: dict[str, Any]) -> None:
        validate_config(config)
        # Kept so `build_server` can read the `consult:` block off it rather than
        # loading the file a second time.
        self.config = config
        self.capabilities: dict[str, str] = config.get("capabilities") or {}
        try:
            self.limits = Limits(**(config.get("limits") or {}))
        except ValidationError as exc:
            raise ConfigError(f"invalid `limits:` block: {exc}") from exc

        # No capabilities means no `ask`, so there is no Router to build and nothing
        # for one to route to. `request_model` is what `build_server` reads to decide
        # whether to advertise the tool at all, and None is that answer.
        self.router: Router | None = None
        self.request_model: type[AskRequest] | None = None
        self._capability_of: dict[str, str] = {}
        if self.capabilities:
            self.router = Router(
                model_list=config["model_list"], **(config.get("router_settings") or {})
            )
            self.request_model = build_ask_request(list(self.capabilities), self.limits)
            # model_id -> capability, so a reply from outside the requested group is
            # reported as a fallback rather than passing as the intended model.
            self._capability_of = {
                (d.get("model_info") or {}).get("id"): d["model_name"]
                for d in (self.router.get_model_list() or [])
            }

    def _fallbacks_for(self, capability: str) -> list[str]:
        out: list[str] = []
        for mapping in getattr(self.router, "fallbacks", None) or []:
            for source, targets in mapping.items():
                if source == capability:
                    out.extend(targets)
        return out

    def list_capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            capabilities=[
                CapabilityInfo(
                    name=name,
                    description=description,
                    deployments=[
                        d["litellm_params"]["model"]
                        for d in (self.router.get_model_list(model_name=name) or [])
                    ],
                    fallbacks=self._fallbacks_for(name),
                )
                for name, description in self.capabilities.items()
            ]
        )

    async def ask(self, **kwargs: Any) -> AskResponse:
        started = time.perf_counter()
        # Only for the failure envelope, and only if it is a string: an int or a dict
        # here would fail `AskResponse` validation and raise out of the very path that
        # exists to guarantee an envelope.
        requested = kwargs.get("capability")
        capability = requested if isinstance(requested, str) else "<invalid>"

        # Second line of defence: the MCP layer already validated against the same
        # model, but tests and direct callers go through here too.
        try:
            request = self.request_model(**kwargs)
        except Exception as exc:
            return self._failed(capability, ErrorCode.INVALID_REQUEST, str(exc), started)

        wrapper: dict[str, Any] | None = None
        if request.response_schema is not None:
            try:
                wrapper = _wrap_schema(self._checked_schema(request.response_schema))
            except (jsonschema.SchemaError, ValueError) as exc:
                return self._failed(request.capability, ErrorCode.INVALID_REQUEST, str(exc), started)

        messages = _build_messages(request.prompt, request.context, request.system, wrapper)

        # One budget for the whole call, not one per leg: retries, cross-capability
        # fallback and repair turns all live under it.
        try:
            async with asyncio.timeout(self.limits.request_timeout_s):
                return await self._attempts(request, wrapper, messages, started)
        except TimeoutError:
            return self._failed(
                request.capability,
                ErrorCode.TIMEOUT,
                f"call exceeded request_timeout_s ({self.limits.request_timeout_s}s)",
                started,
            )

    async def _attempts(
        self,
        request: Any,
        wrapper: dict[str, Any] | None,
        messages: list[dict[str, str]],
        started: float,
    ) -> AskResponse:
        # One shot in prose mode; structured mode gets bounded repair attempts.
        attempts = 1 + (self.limits.schema_repair_attempts if wrapper else 0)

        for attempt in range(attempts):
            # Leave the Router a margin to hit its own timeout first. If the outer
            # deadline wins, the leg dies of `CancelledError`, which skips LiteLLM's
            # `except Exception` bookkeeping -- no failure count, no cooldown -- and the
            # next request picks the stuck deployment straight back up.
            remaining = self.limits.request_timeout_s - (time.perf_counter() - started)
            if remaining <= _TIMEOUT_MARGIN_S:
                raise TimeoutError  # answered as an envelope by the caller's handler

            try:
                response = await self.router.acompletion(
                    model=request.capability,
                    messages=messages,
                    temperature=request.temperature,
                    max_tokens=request.max_output_tokens or self.limits.max_output_tokens,
                    timeout=remaining - _TIMEOUT_MARGIN_S,
                    **self._structured_params(wrapper),
                )
            except Exception as exc:
                return self._failed(request.capability, _classify(exc), str(exc), started)

            raw, refusal = _completion_of(response)
            if refusal is not None:
                return self._failed(request.capability, *refusal, started, response)

            if wrapper is None:
                return self._succeeded(request.capability, response, started, content=raw)

            try:
                data, abstained = _parse_structured(raw, wrapper)
            except StructuredOutputError as exc:
                if attempt + 1 < attempts:
                    messages = [
                        *messages,
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": (
                                f"That reply was rejected: {exc.detail}. Reply again with a single "
                                "JSON object that matches the schema, and nothing else."
                            ),
                        },
                    ]
                    continue
                return self._failed(
                    request.capability,
                    ErrorCode.SCHEMA_VALIDATION_FAILED,
                    str(exc),
                    started,
                    response,
                )

            return self._succeeded(
                request.capability, response, started, content=raw, data=data, abstained=abstained
            )

        raise AssertionError("unreachable: loop returns on every path")

    def _checked_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Reject a malformed schema up front rather than as a confusing later failure."""
        # The schema is inlined into the prompt verbatim, so its size is the caller's
        # to spend but ours to bound.
        if len(json.dumps(schema)) > self.limits.max_schema_chars:
            raise ValueError(
                f"`response_schema` is larger than max_schema_chars "
                f"({self.limits.max_schema_chars})"
            )
        jsonschema.Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("`response_schema` must describe an object (\"type\": \"object\")")
        return schema

    def _structured_params(self, wrapper: dict[str, Any] | None) -> dict[str, Any]:
        """Ask the provider to enforce the schema where it can.

        Provider-side enforcement is a convenience, not a guarantee -- the reply is
        validated locally either way. `drop_params` keeps backends that do not
        support `response_format` from failing the call outright.
        """
        if wrapper is None:
            return {}
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "orchestrator_answer", "schema": wrapper},
            },
            "drop_params": True,
        }

    def _succeeded(
        self,
        capability: str,
        response: Any,
        started: float,
        content: str | None,
        data: dict[str, Any] | None = None,
        abstained: bool | None = None,
    ) -> AskResponse:
        hidden = getattr(response, "_hidden_params", {}) or {}
        served_by = self._capability_of.get(hidden.get("model_id"))
        usage = getattr(response, "usage", None)
        if abstained is None:  # prose mode
            content, abstained = _split_abstention(content)
        else:  # structured mode: the answer lives in `data`, never in `content`
            content = None

        return AskResponse(
            ok=True,
            content=content,
            data=data,
            insufficient_context=abstained,
            capability_requested=capability,
            model_used=hidden.get("litellm_model_name") or response.model,
            fallback_used=served_by is not None and served_by != capability,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
                cost_usd=hidden.get("response_cost"),
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
        ).check_invariants()

    def _failed(
        self,
        capability: str,
        code: ErrorCode,
        message: str,
        started: float,
        response: Any = None,
    ) -> AskResponse:
        # `response` is present only when a provider actually replied and the reply was
        # rejected. Its `finish_reason` is diagnosis, not answer -- knowing the provider
        # said "length" is the difference between raising `max_output_tokens` and
        # hunting a bug -- so it rides along while `content` and `data` stay null.
        choices = getattr(response, "choices", None) or []
        return AskResponse(
            ok=False,
            capability_requested=capability,
            finish_reason=getattr(choices[0], "finish_reason", None) if choices else None,
            # One truncation for every source: provider exceptions embed the request
            # body, pydantic echoes the caller's input, validators quote the reply.
            error=ErrorInfo(code=code, message=redact(message)[:MAX_ERROR_CHARS]),
            latency_ms=int((time.perf_counter() - started) * 1000),
        ).check_invariants()


def _tool_signature(request_model: type, return_annotation: type = AskResponse) -> inspect.Signature:
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
    orchestrator = Orchestrator(config if config is not None else load_config())
    server = MCPServer("orchestrator", version=_version())
    if orchestrator.request_model is not None:
        _add_ask_tools(server, orchestrator)

    # The whole consultation path hangs off this one branch. A config with no
    # `consult:` block advertises exactly the two tools above, byte for byte, which
    # is what the compatibility snapshot asserts.
    consult_config = load_consult_config(orchestrator.config)
    if consult_config is not None:
        runtime = host_runtime()
        # One store for both layers, not one each: a review and the consultations
        # under it are then written by the same serialized worker, and a deletion can
        # remove them in one transaction instead of hoping two connections agree.
        store = ConsultStore(consult_config.database_path, consult_config.store_full_content)
        _add_consult_tools(server, ConsultService(consult_config, runtime, store=store))
        # Advertised only when reviewers are configured. A server with none should
        # not offer a `review` tool that can do nothing but refuse.
        if consult_config.review is not None:
            _add_review_tools(server, ReviewService(consult_config, runtime, store=store))

    return server


def _add_ask_tools(server: MCPServer, orchestrator: Orchestrator) -> None:
    async def ask(**kwargs: Any) -> AskResponse:
        return await orchestrator.ask(**kwargs)

    ask.__name__ = "ask"
    ask.__signature__ = _tool_signature(orchestrator.request_model)  # type: ignore[attr-defined]
    ask.__annotations__ = {
        p.name: p.annotation for p in ask.__signature__.parameters.values()
    } | {"return": AskResponse}
    ask.__doc__ = (
        "Route a request to the model configured for the given capability.\n\n"
        "Returns an envelope: `ok`, `content`, `insufficient_context`, `model_used`, "
        "`fallback_used`, `usage`, and `error` (a code from a closed set). A failed "
        "call never carries answer text -- check `ok` before reading `content`."
    )
    server.add_tool(ask, name="ask")

    @server.tool(name="list_capabilities")
    async def list_capabilities() -> CapabilitiesResponse:
        """List configured capabilities: what each is for, the deployments behind it,
        and where it falls back when they are unavailable."""
        return orchestrator.list_capabilities()


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
        "Consult another vendor's coding agent -- a Codex or Claude Code CLI running "
        "under its own account -- and get back a structured second opinion.\n\n"
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
    server.add_tool(consult, name="consult")

    @server.tool(name="list_consult_agents")
    async def list_consult_agents() -> ConsultAgentsResponse:
        """List consultable agents: runtime, model, capability scores, and whether each
        is installed and logged in. The host's own runtime is listed but never routed
        to."""
        await service.open()
        return await service.list_agents()

    @server.tool(name="get_consultation")
    async def get_consultation(consultation_id: UUID) -> ConsultationRecord:
        """Retrieve a stored consultation: its turns, usage, and why this agent was
        chosen."""
        await service.open()
        return await service.get_consultation(consultation_id)


def _add_review_tools(server: MCPServer, service: ReviewService) -> None:
    """The review surface: a two-call handshake, then synthesis.

    Two tools rather than one that switches on its arguments. A single `review`
    taking either `(mode, goal, context)` or `(review_id, confirm_token)` is two
    mutually exclusive argument sets in one schema, which a calling model reads as
    "everything is optional" -- the same reasoning as `_agent_enum`.

    The handshake is advisory and the docstrings say so: an MCP server has no
    channel to a human and cannot verify anyone saw the plan. What it does buy is
    an explicit sequence, a `review_id` that exists before anything is sent, and a
    stored record of what was approved against what went out.
    """

    @server.tool(name="review")
    async def review(
        goal: str,
        mode: ReviewMode = "standard",
        material: list[MaterialItem] | None = None,
        context: str | None = None,
        web: bool = False,
        reviewers: list[str] | None = None,
        parent_review_id: UUID | None = None,
        host_model: str | None = None,
    ) -> ReviewResponse:
        """Plan a review and show what would be sent. **Sends nothing.**

        Show the returned `plan` to the user before calling `review_run`: which
        reviewers, how much material, whether web access is requested, how many
        requests it will cost, and `secret_hits` -- lines where something
        credential-shaped was found (positions only, never values). Secret detection
        is best-effort pattern matching; a credential with no recognizable shape
        survives it, so the plan raises the odds the user notices and guarantees
        nothing.

        `material` is your own manifest of what you put in `context`. This server
        never reads files, so it cannot check that the two agree -- it is a
        disclosure aid, and being inside the approval hash it cannot be changed
        between the preview and the send.

        `mode="deep"` asks up to five reviewers and requires your own findings first,
        passed to `review_run` as `host_findings`. `web=False` unless the user asked
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
            web=web,
            reviewers=reviewers,
            parent_review_id=parent_review_id,
            host_model=host_model,
        )

    @server.tool(name="review_run")
    async def review_run(
        review_id: UUID,
        confirm_token: str,
        host_findings: list[str] | None = None,
        secrets: Literal["mask", "send_as_is"] = "mask",
        raw: RawReviewMaterial | None = None,
    ) -> ReviewResponse:
        """Send the approved material to every reviewer, in parallel. Call this only
        after the user has seen the plan and agreed.

        `secrets="mask"` sends the redacted copy. `secrets="send_as_is"` sends the
        original and requires `raw` to repeat the exact goal and context you planned
        with -- for when the user has looked at a flagged line and decided it is a
        variable named `password` or a fixture's fake key. Either way the stored copy
        is redacted.

        `host_findings` is your own independent opinion, formed before you read
        anyone else's. Required for `mode="deep"`, and never shown to any reviewer.

        Stops at `awaiting_synthesis`: reviewers replying is not a finished review.
        Read every `results[]` entry, write the comparison yourself, and call
        `finalize_review`. A reviewer that failed leaves the others' answers intact --
        `retry_review` re-runs only the failures.
        """
        return await service.run(
            review_id,
            confirm_token,
            host_findings=host_findings,
            secrets=secrets,
            raw=raw.model_dump(mode="json") if raw else None,
        )

    @server.tool(name="retry_review")
    async def retry_review(review_id: UUID, agent_ids: list[str] | None = None) -> ReviewResponse:
        """Re-run the reviewers that failed. No new approval is needed: the material
        is unchanged, and the stored hash is what proves it. Reviewers that already
        answered are not asked again and their answers are kept."""
        return await service.retry(review_id, agent_ids)

    @server.tool(name="finalize_review")
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
        than one: disagree with it in `disagreed_by`, never drop it. This is checked,
        and the call is refused if a Critical is unaccounted for.

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

    @server.tool(name="cancel_review")
    async def cancel_review(review_id: UUID) -> ReviewResponse:
        """Stop a review. Reviewers that already answered keep their answers.

        A review launched by *this* server process is waited out before this returns.
        One launched by a second process is marked cancelled, but its subprocesses
        cannot be signalled from here -- they will finish on their own. Deleting the
        review is refused until they do.
        """
        return await service.cancel(review_id)

    @server.tool(name="test_reviewers")
    async def test_reviewers(mode: ReviewMode | None = None) -> ConsultAgentsResponse:
        """Check that the configured reviewers are installed and logged in.

        A preflight and nothing more: **no project material leaves this machine.**
        Omit `mode` to check the reviewers for both standard and deep.
        """
        return await service.test_reviewers(mode)

    @server.tool(name="get_review")
    async def get_review(review_id: UUID) -> ReviewResponse:
        """Retrieve a review: its status, every reviewer's result, and the synthesis
        if one was recorded. Answers are absent when the operator has turned off
        `store_full_content`."""
        return await service.get(review_id)

    @server.tool(name="list_reviews")
    async def list_reviews(limit: int = 20) -> list[ReviewListing]:
        """List recent reviews, newest first. Metadata only -- no material."""
        return await service.list(limit)

    @server.tool(name="delete_review")
    async def delete_review(review_id: UUID) -> DeletionResult:
        """Delete a review, its rechecks, and every consultation under either.

        Refused while a review is running, or while its reviewers are still in flight
        in any server process. Cancel it and try again."""
        return DeletionResult(deleted=await service.delete(review_id))

    @server.tool(name="request_delete_all")
    async def request_delete_all() -> DeleteApproval:
        """Ask what deleting all review history would remove.

        Deletes nothing. Show the count to the user and pass the token to
        `delete_all_reviews` only if they agree. The token is bound to exactly the
        reviews counted here, so one created in the meantime is not caught by an
        approval that never mentioned it."""
        token, count = await service.request_delete_all()
        return DeleteApproval(
            reviews=count, confirm_token=token, expires_in_s=int(DELETE_CONFIRM_TTL_S)
        )

    @server.tool(name="delete_all_reviews")
    async def delete_all_reviews(confirm_token: str) -> DeletionResult:
        """Delete the reviews `request_delete_all` counted, and only those. Requires
        the token from that call; an expired one is refused rather than silently
        widened to whatever exists now."""
        return DeletionResult(deleted=await service.delete_all(confirm_token))


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
