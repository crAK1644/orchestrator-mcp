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
from pathlib import Path
from typing import Annotated, Any

import jsonschema
import litellm
import yaml
from litellm import Router
from litellm.types.router import RouterErrors
from mcp.server import MCPServer
from pydantic import ValidationError

from .contract import (
    MAX_ERROR_CHARS,
    AskResponse,
    CapabilitiesResponse,
    CapabilityInfo,
    ErrorCode,
    ErrorInfo,
    Limits,
    Usage,
    build_ask_request,
)

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


class ConfigError(ValueError):
    """Raised at startup. A misconfigured server refuses to boot rather than
    half-routing in production."""


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


# A deny-list, not an allow-list: LiteLLM normalizes `finish_reason` to the OpenAI
# set, so this covers what it emits, and an unfamiliar reason from a custom provider
# must not fail an otherwise complete answer.
_REFUSED_FINISH: dict[str, tuple[ErrorCode, str]] = {
    "length": (ErrorCode.OUTPUT_TRUNCATED, "the model hit the output limit mid-answer"),
    "max_tokens": (ErrorCode.OUTPUT_TRUNCATED, "the model hit the output limit mid-answer"),
    "content_filter": (ErrorCode.CONTENT_FILTERED, "the provider filtered this completion"),
}


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

    content = getattr(getattr(choice, "message", None), "content", None)
    if content is None or not content.strip():
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
        self.capabilities: dict[str, str] = config["capabilities"]
        try:
            self.limits = Limits(**(config.get("limits") or {}))
        except ValidationError as exc:
            raise ConfigError(f"invalid `limits:` block: {exc}") from exc
        self.router = Router(model_list=config["model_list"], **(config.get("router_settings") or {}))
        self.request_model = build_ask_request(list(self.capabilities), self.limits)

        # model_id -> capability, so a reply from outside the requested group is
        # reported as a fallback rather than passing as the intended model.
        self._capability_of: dict[str, str] = {
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
        capability = kwargs.get("capability", "<unknown>")

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
            try:
                response = await self.router.acompletion(
                    model=request.capability,
                    messages=messages,
                    temperature=request.temperature,
                    max_tokens=request.max_output_tokens or self.limits.max_output_tokens,
                    # What is left of the budget, so a hung leg times out inside the
                    # Router -- which cools the deployment down -- rather than being
                    # cut from outside it.
                    timeout=max(1, self.limits.request_timeout_s - int(time.perf_counter() - started)),
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
            error=ErrorInfo(code=code, message=message[:MAX_ERROR_CHARS]),
            latency_ms=int((time.perf_counter() - started) * 1000),
        ).check_invariants()


def _tool_signature(request_model: type) -> inspect.Signature:
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
    return inspect.Signature(parameters, return_annotation=AskResponse)


def build_server(config: dict[str, Any] | None = None) -> MCPServer:
    orchestrator = Orchestrator(config if config is not None else load_config())
    server = MCPServer("orchestrator")

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

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
