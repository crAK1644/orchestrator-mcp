"""Capability-routed MCP server.

A capability is a LiteLLM `model_name` alias group. Routing, retries, cooldowns and
cross-capability fallback all happen inside `litellm.Router`; this module owns the
MCP surface, the config contract, and the response envelope.
"""

from __future__ import annotations

import inspect
import os
import time
from pathlib import Path
from typing import Annotated, Any

import litellm
import yaml
from litellm import Router
from mcp.server import MCPServer

from .contract import (
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

# Fields the tool exposes; `response_schema` arrives with structured mode in phase 3.
_HIDDEN_FIELDS = {"response_schema"}


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
    if "no deployments available" in str(exc).lower():
        return ErrorCode.NO_DEPLOYMENT
    for exc_type, code in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return code
    return ErrorCode.UPSTREAM_ERROR


def _build_messages(prompt: str, context: str | None, system: str | None) -> list[dict[str, str]]:
    """Fixed order, grounding directive last, so a caller's `system` cannot disable it."""
    preamble = [p for p in (system.strip() if system else None, GROUNDING_DIRECTIVE if context else None) if p]
    messages: list[dict[str, str]] = []
    if preamble:
        messages.append({"role": "system", "content": "\n\n".join(preamble)})
    user = f"<context>\n{context}\n</context>\n\n{prompt}" if context else prompt
    messages.append({"role": "user", "content": user})
    return messages


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
        self.limits = Limits(**(config.get("limits") or {}))
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

        try:
            response = await self.router.acompletion(
                model=request.capability,
                messages=_build_messages(request.prompt, request.context, request.system),
                temperature=request.temperature,
                max_tokens=request.max_output_tokens or self.limits.max_output_tokens,
                timeout=self.limits.request_timeout_s,
            )
        except Exception as exc:
            return self._failed(request.capability, _classify(exc), str(exc), started)

        return self._succeeded(request.capability, response, started)

    def _succeeded(self, capability: str, response: Any, started: float) -> AskResponse:
        choice = response.choices[0]
        hidden = getattr(response, "_hidden_params", {}) or {}
        served_by = self._capability_of.get(hidden.get("model_id"))
        content, abstained = _split_abstention(choice.message.content)
        usage = getattr(response, "usage", None)

        return AskResponse(
            ok=True,
            content=content,
            insufficient_context=abstained,
            capability_requested=capability,
            model_used=hidden.get("litellm_model_name") or response.model,
            fallback_used=served_by is not None and served_by != capability,
            finish_reason=choice.finish_reason,
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
                cost_usd=hidden.get("response_cost"),
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
        ).check_invariants()

    def _failed(self, capability: str, code: ErrorCode, message: str, started: float) -> AskResponse:
        return AskResponse(
            ok=False,
            capability_requested=capability,
            error=ErrorInfo(code=code, message=message),
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
        if name not in _HIDDEN_FIELDS
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
