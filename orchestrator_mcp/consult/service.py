"""What composes a consultation out of the parts.

Router picks the agent, prompts compile the turn, an adapter runs the CLI, the
store records all of it. This module is the only place that knows the order, and
the only place that turns a failure into an envelope.

Two rules shape everything below. Every path returns a `ConsultResponse` -- an
exception escaping here would leave the calling agent with a protocol error and no
consultation id, which is the one thing it needs to try again. And a failure never
carries text: `content` stays null, so nothing a caller reads as an answer was
written by this server rather than by the consulted agent.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID, uuid4

from ..contract import MAX_ERROR_CHARS, Usage, redact
from .adapters import AdapterError, adapter_for
from .adapters.base import GRACE_S, AgentStatus, ConsultAdapter
from .adapters.claude_cli import PREFLIGHT_TIMEOUT_S
from .config import AgentConfig, ConsultConfig
from .contract import (
    ConsultAgentInfo,
    ConsultAgentsResponse,
    ConsultationRecord,
    ConsultError,
    ConsultResponse,
    ConsultRoute,
    RequiredAction,
    Runtime,
    SourceMode,
    build_consult_request,
)
from .errors import ConsultErrorCode
from .prompts import compile_prompt
from .routing import ConsultRouter, SourceModeError, resolve_source_mode
from .store import ConsultStore, StoreError


class ConsultService:
    def __init__(
        self,
        config: ConsultConfig,
        host_runtime: Runtime,
        store: ConsultStore | None = None,
    ) -> None:
        self.config = config
        self.host_runtime = host_runtime
        self.router = ConsultRouter(config, host_runtime)
        self.store = store or ConsultStore(config.database_path, config.store_full_content)
        self.request_model = build_consult_request(sorted(config.agents))

    async def open(self) -> ConsultService:
        await self.store.open()
        return self

    async def close(self) -> None:
        await self.store.close()

    def adapter(self, agent: AgentConfig) -> ConsultAdapter:
        return adapter_for(agent, self.config)

    def _lease_ttl(self) -> float:
        return self.config.timeout_s + PREFLIGHT_TIMEOUT_S + GRACE_S + 30.0

    # --- consultation -------------------------------------------------------

    async def consult(self, **kwargs: Any) -> ConsultResponse:
        """The boundary. Everything past here returns an envelope, including the
        failures that are nobody's fault: an unwritable database directory, a disk
        that filled mid-turn, a shape from an adapter that no branch expected.

        Opening the store lives inside it for the same reason -- a `database_path`
        under a regular file raises `FileExistsError`, which crossed the MCP boundary
        as a bare exception before there was anything here to catch it.

        `CancelledError` is not an `Exception` and is left alone: a cancelled call has
        no caller left to hand an envelope to.
        """
        started = time.perf_counter()
        requested = kwargs.get("capability")
        capability = requested if isinstance(requested, str) else "<invalid>"
        try:
            await self.open()
            return await self._consult(started, capability, kwargs)
        except Exception as exc:
            # The type and nothing else. These messages are quoted back to a caller
            # that may not be the operator, and an operational exception carries
            # paths, connection strings, and occasionally a credential.
            requested_id = kwargs.get("consultation_id")
            return _failed(
                requested_id if isinstance(requested_id, UUID) else None,
                capability, SourceMode.AUTO,
                ConsultErrorCode.TRANSPORT_ERROR,
                f"the consultation failed inside the orchestrator ({type(exc).__name__})",
                started,
            )

    async def _consult(
        self, started: float, capability: str, kwargs: dict[str, Any]
    ) -> ConsultResponse:
        # Second line of defence: the MCP layer validated against this same model,
        # but tests and direct callers arrive here too.
        try:
            request = self.request_model(**kwargs)
        except Exception as exc:
            return _failed(None, capability, SourceMode.AUTO, ConsultErrorCode.INVALID_REQUEST,
                           str(exc), started)

        try:
            source_mode = resolve_source_mode(request.source_mode, request.context)
        except SourceModeError as exc:
            return _failed(None, capability, request.source_mode,
                           ConsultErrorCode.PROTOCOL_VALIDATION_FAILED, str(exc), started)

        try:
            consultation, route, agent, resuming = await self._bind(request, capability)
        except StoreError as exc:
            return _failed(request.consultation_id, capability, source_mode, exc.code, str(exc), started)
        except _RoutingFailure as exc:
            return _failed(None, capability, source_mode, exc.code, str(exc), started)

        consultation_id = UUID(consultation.id)
        try:
            # The lease has to outlive the turn it guards: a preflight, a CLI run
            # bounded by `timeout_s`, and the grace period spent killing a child that
            # ignored SIGTERM. Expiring earlier would let a second caller in beside a
            # consultation that is still running.
            async with self.store.lease(consultation_id, ttl_s=self._lease_ttl()):
                return await self._turn(
                    consultation_id, agent, route, request, source_mode, resuming, started
                )
        except StoreError as exc:
            # Includes `session_busy`, which is deliberately not retried here: the
            # other turn is mid-flight on a real CLI session and waiting it out is
            # the caller's decision, not ours.
            return _failed(consultation_id, capability, source_mode, exc.code, str(exc), started)

    async def _bind(
        self, request: Any, capability: str
    ) -> tuple[Any, ConsultRoute, AgentConfig, str | None]:
        """Find or create the consultation, and the agent it is bound to."""
        if request.consultation_id is not None:
            consultation = await self.store.get_consultation(request.consultation_id)
            self.store.check_target(consultation, request.target_agent)
            agent = self.config.agents.get(consultation.target_agent_id)
            if agent is None:
                # The routing table changed under a live consultation.
                raise StoreError(
                    ConsultErrorCode.SESSION_TARGET_MISMATCH,
                    f"consultation `{consultation.id}` is bound to agent "
                    f"`{consultation.target_agent_id}`, which is no longer configured",
                )
            # The id surviving a config edit does not mean the agent behind it did.
            # An id reassigned to another runtime or model is a different agent
            # wearing an old name, and resuming into it would continue someone
            # else's conversation -- including, if the new runtime is ours, straight
            # back into the host.
            if agent.runtime != consultation.target_runtime or agent.model != consultation.target_model:
                raise StoreError(
                    ConsultErrorCode.SESSION_TARGET_MISMATCH,
                    f"consultation `{consultation.id}` was bound to "
                    f"`{consultation.target_agent_id}` running "
                    f"{consultation.target_runtime}/{consultation.target_model}, which is now "
                    f"configured as {agent.runtime}/{agent.model}; start a new consultation",
                )
            if agent.runtime == self.host_runtime:
                # Checked independently of the pair above, because the host runtime is
                # the one exclusion that must hold no matter how the config got here.
                raise StoreError(
                    ConsultErrorCode.SESSION_TARGET_MISMATCH,
                    f"consultation `{consultation.id}` is bound to `{agent.agent_id}`, which now "
                    f"runs this host's own runtime ({self.host_runtime}) and cannot be consulted",
                )
            route = ConsultRoute(
                agent_id=agent.agent_id,
                runtime=agent.runtime,
                model=consultation.target_model,
                capability_score=agent.score_for(consultation.capability),
                priority=agent.priority,
                explicitly_selected=request.target_agent is not None,
            )
            return consultation, route, agent, consultation.native_session_id

        decision = self.router.select(capability, request.target_agent)
        if decision.error is not None or decision.route is None or decision.selected is None:
            code, message = decision.error or (
                ConsultErrorCode.NO_AGENT_AVAILABLE, "no agent could take this consultation"
            )
            raise _RoutingFailure(code, message)

        consultation_id = uuid4()
        consultation = await self.store.create_consultation(
            consultation_id=consultation_id,
            origin_runtime=self.host_runtime,
            route=decision.route,
            capability=capability,
            protocol_version=self.config.protocol_version,
            config_hash=self.config.config_hash(),
            conversation_label=request.conversation_label,
        )
        await self.store.record_routing(consultation_id, decision)
        return consultation, decision.route, decision.selected, None

    async def _turn(
        self,
        consultation_id: UUID,
        agent: AgentConfig,
        route: ConsultRoute,
        request: Any,
        source_mode: SourceMode,
        resuming: str | None,
        started: float,
    ) -> ConsultResponse:
        adapter = self.adapter(agent)
        capability = request.capability
        sequence = await self.store.next_sequence(consultation_id)
        prompt = compile_prompt(capability, source_mode, request.prompt, request.context, turn=sequence)

        async def fail(code: ConsultErrorCode, message: str, action: RequiredAction | None = None):
            await self.store.record_turn(
                consultation_id, sequence, source_mode,
                user_prompt=request.prompt, context=request.context,
                compiled_prompt=prompt.full_text, error_code=code,
            )
            return _failed(consultation_id, capability, source_mode, code, message, started,
                           agent_id=agent.agent_id, required_action=action)

        status = await self._status(adapter, agent)
        if not status.ready:
            # No fallback to the next-best agent, ever: quietly consulting a model
            # nobody chose is the substitution this protocol exists to prevent. The
            # user connects the agent, or the caller picks a different one.
            code = (
                ConsultErrorCode.AGENT_NOT_INSTALLED
                if not status.installed
                else ConsultErrorCode.CONNECTION_REQUIRED
            )
            action = None if not status.installed else RequiredAction(
                command=adapter.connect_command(agent)
            )
            return await fail(
                code,
                f"agent `{agent.agent_id}` cannot be consulted: {status.detail or 'unavailable'}",
                action,
            )

        try:
            if resuming:
                result = await adapter.resume(agent, resuming, prompt, source_mode)
            else:
                result = await adapter.start(
                    agent, prompt, source_mode, session_id=str(consultation_id)
                )
        except AdapterError as exc:
            return await fail(exc.code, str(exc), exc.required_action)

        await self.store.bind_native_session(consultation_id, result.native_session_id)
        await self.store.record_turn(
            consultation_id,
            sequence,
            source_mode,
            user_prompt=request.prompt,
            context=request.context,
            compiled_prompt=prompt.full_text,
            raw_output=result.raw_output,
            validated_response=result.content.model_dump(),
            input_tokens=result.usage.prompt_tokens,
            output_tokens=result.usage.completion_tokens,
            cost_usd=result.usage.cost_usd,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

        return ConsultResponse(
            ok=True,
            consultation_id=consultation_id,
            content=result.content,
            capability_requested=capability,
            source_mode_used=source_mode,
            # The model that actually answered, not the one configured: they agree
            # or the adapter has already refused the turn. `model_verified` is how a
            # caller tells that apart from a runtime that named no model at all.
            route=route.model_copy(
                update={"model": result.model_used, "model_verified": result.model_verified}
            ),
            usage=result.usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
        ).check_invariants()

    async def _status(self, adapter: ConsultAdapter, agent: AgentConfig) -> AgentStatus:
        try:
            status = await adapter.preflight(agent)
        except AdapterError as exc:
            status = AgentStatus(agent.agent_id, installed=False, authenticated=False,
                                 detail=str(exc)[:200])
        await self.store.record_status_check(
            agent.agent_id, status.installed, status.authenticated, status.detail
        )
        return status

    # --- read-only surfaces -------------------------------------------------

    async def list_agents(self, check: bool = True) -> ConsultAgentsResponse:
        agents = []
        for agent_id, agent in sorted(self.config.agents.items()):
            host = agent.runtime == self.host_runtime
            # The host's own runtime is never consulted, so probing it would be a
            # subprocess launched to answer a question already settled.
            status = (
                await self._status(self.adapter(agent), agent) if check and not host else None
            )
            agents.append(
                ConsultAgentInfo(
                    agent_id=agent_id,
                    runtime=agent.runtime,
                    model=agent.model,
                    priority=agent.priority,
                    enabled=agent.enabled,
                    scores=dict(agent.scores),
                    web_search=agent.web_search,
                    excluded_as_host=host,
                    installed=status.installed if status else None,
                    authenticated=status.authenticated if status else None,
                    detail=status.detail if status else None,
                )
            )
        return ConsultAgentsResponse(host_runtime=self.host_runtime, agents=agents)

    async def get_consultation(self, consultation_id: UUID) -> ConsultationRecord:
        consultation = await self.store.get_consultation(consultation_id)
        turns = await self.store.turns(consultation_id)
        return ConsultationRecord(
            consultation_id=UUID(consultation.id),
            target_agent_id=consultation.target_agent_id,
            target_runtime=consultation.target_runtime,  # type: ignore[arg-type]
            target_model=consultation.target_model,
            capability=consultation.capability,
            source_modes=sorted({t.source_mode for t in turns}),
            conversation_label=consultation.conversation_label,
            status=consultation.status,
            # The id itself is not returned: it is the consulted CLI's handle on a
            # live session, and nothing outside this server has a use for it.
            native_session_bound=consultation.native_session_id is not None,
            created_at=consultation.created_at,
            updated_at=consultation.updated_at,
            turns=[
                {
                    "sequence_number": t.sequence_number,
                    "source_mode": t.source_mode,
                    "prompt": t.user_prompt,
                    "answer": t.validated_response_json,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "cost_usd": t.cost_usd,
                    "latency_ms": t.latency_ms,
                    "error_code": t.error_code,
                    "created_at": t.created_at,
                }
                for t in turns
            ],
            routing=await self.store.routing_for(consultation_id),
        )


class _RoutingFailure(Exception):
    def __init__(self, code: ConsultErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _failed(
    consultation_id: UUID | None,
    capability: str,
    source_mode: SourceMode,
    code: ConsultErrorCode,
    message: str,
    started: float,
    agent_id: str | None = None,
    required_action: RequiredAction | None = None,
) -> ConsultResponse:
    return ConsultResponse(
        ok=False,
        consultation_id=consultation_id,
        capability_requested=capability,
        source_mode_used=source_mode,
        usage=Usage(),
        error=ConsultError(
            code=code,
            # One truncation for every source: a CLI's stderr, a pydantic error
            # echoing the caller's input, a validator quoting the reply.
            message=redact(message)[:MAX_ERROR_CHARS],
            agent_id=agent_id,
            required_action=required_action,
        ),
        latency_ms=int((time.perf_counter() - started) * 1000),
    ).check_invariants()
