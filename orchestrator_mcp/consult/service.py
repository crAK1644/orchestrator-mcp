"""What composes a consultation out of the parts.

Router picks the agent, prompts compile the turn, an adapter runs the CLI, the
store records all of it. This module is the only place that knows the order, and
the only place that turns a failure into an envelope.

There are three entry points and three bind methods, paired by name and never by a
public value: `consult()` binds through `_bind_public`, while review and workflow
calls use private ownership-aware paths. No MCP argument moves a call between them.

Two rules shape everything below. Every path returns a `ConsultResponse` -- an
exception escaping here would leave the calling agent with a protocol error and no
consultation id, which is the one thing it needs to try again. And a failure never
carries text: `content` stays null, so nothing a caller reads as an answer was
written by this server rather than by the consulted agent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from ..contract import MAX_ERROR_CHARS, Usage, redact, scrub_json
from ..workflow.contract import StepSnapshot
from ..workflow.identity import host_identity_conflict
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
from .routing import ConsultRouter, RoutingDecision, SourceModeError, resolve_source_mode
from .store import ConsultStore, StoreError
from ..log import get_logger

log = get_logger(__name__)


class ConsultService:
    def __init__(
        self,
        config: ConsultConfig,
        host_runtime: Runtime,
        store: ConsultStore | None = None,
        store_sanitizer: Callable[[Any], Any] | None = None,
    ) -> None:
        self.config = config
        self.host_runtime = host_runtime
        self.router = ConsultRouter(config, host_runtime)
        self.store = store or ConsultStore(config.database_path, config.store_full_content)
        self.request_model = build_consult_request(sorted(config.agents))
        # What every turn is passed through on its way into the database, and only
        # there -- the adapter is still handed the caller's own text, because a
        # consultation answering a redacted question is not the same consultation.
        #
        # Credential-shaped values are scrubbed for every storage path. The adapter
        # still receives the caller's original material; only the copy headed for
        # SQLite passes through this function. Injection remains available for a
        # stricter embedding, but opting out accidentally is no longer the default.
        self._scrub: Callable[[Any], Any] = store_sanitizer or scrub_json
        # In-process, deliberately: a fresh process re-probing once is fine, and a
        # SQLite round-trip to avoid a subprocess is not obviously the cheaper of the
        # two. Keyed on what actually decides the answer, so editing an agent's
        # command or model in the dashboard cannot be answered from the old one's
        # entry.
        self._ready_until: dict[tuple[str, str, str], tuple[float, AgentStatus]] = {}

    async def open(self) -> ConsultService:
        await self.store.open()
        return self

    async def close(self) -> None:
        await self.store.close()

    def adapter(self, agent: AgentConfig) -> ConsultAdapter:
        return adapter_for(agent, self.config)

    def _lease_ttl(self, agent: AgentConfig | None = None) -> float:
        timeout_s = (agent.timeout_s if agent else None) or self.config.timeout_s
        return timeout_s + PREFLIGHT_TIMEOUT_S + GRACE_S + 30.0

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

    async def consult_review(
        self, review_id: UUID | str, reviewer_id: str, **kwargs: Any
    ) -> ConsultResponse:
        """Review-only consult path that links ownership before the turn starts.

        The owner fields are not MCP arguments. Creation writes the consultation and
        its review link in one transaction, so it never looks like ordinary deletable
        history between those writes.
        """
        started = time.perf_counter()
        requested = kwargs.get("capability")
        capability = requested if isinstance(requested, str) else "<invalid>"
        try:
            await self.open()
            return await self._consult(
                started,
                capability,
                kwargs,
                review_owner=(str(review_id), reviewer_id),
            )
        except Exception as exc:
            requested_id = kwargs.get("consultation_id")
            return _failed(
                requested_id if isinstance(requested_id, UUID) else None,
                capability,
                SourceMode.AUTO,
                ConsultErrorCode.TRANSPORT_ERROR,
                f"the consultation failed inside the orchestrator ({type(exc).__name__})",
                started,
            )

    async def _consult(
        self,
        started: float,
        capability: str,
        kwargs: dict[str, Any],
        review_owner: tuple[str, str] | None = None,
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
            consultation, route, agent, resuming = await (
                self._bind_review(request, capability, review_owner)
                if review_owner is not None
                else self._bind_public(request, capability)
            )
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
            async with self.store.lease(consultation_id, ttl_s=self._lease_ttl(agent)):
                return await self._turn(
                    consultation_id, agent, route, request, source_mode, resuming, started
                )
        except StoreError as exc:
            # Includes `session_busy`, which is deliberately not retried here: the
            # other turn is mid-flight on a real CLI session and waiting it out is
            # the caller's decision, not ours.
            return _failed(consultation_id, capability, source_mode, exc.code, str(exc), started)

    async def _bind_public(
        self, request: Any, capability: str
    ) -> tuple[Any, ConsultRoute, AgentConfig, str | None]:
        """Find or create the consultation, and the agent it is bound to.

        The path behind `orchestrator_consult`, and the only one a tool argument can
        reach. Exclusion here is runtime-level and unconditional: no argument relaxes
        it, and `_bind_workflow` is not selectable from here by any value a caller
        could send.
        """
        if request.consultation_id is not None:
            consultation = await self.store.get_consultation(request.consultation_id)
            if consultation.workflow_id is not None:
                # The one thing that would otherwise undo the split. A workflow step's
                # consultation was bound under execution-identity exclusion, which can
                # legitimately be an agent on this host's own runtime; resuming it from
                # the public tool would inherit that binding and hand the host a
                # conversation with itself. The id is not a secret -- status and history
                # both surface it -- so the refusal is on the row, not on knowing it.
                raise StoreError(
                    ConsultErrorCode.WORKFLOW_OWNED_SESSION,
                    f"consultation `{consultation.id}` belongs to workflow "
                    f"`{consultation.workflow_id}`; continue it through its workflow step",
                )
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
            # `target_model` is stored scrubbed, so the live model is scrubbed the
            # same way to compare. Raw against redacted would make a credential-shaped
            # model name look reassigned on every resume.
            if (
                agent.runtime != consultation.target_runtime
                or scrub_json(agent.model) != consultation.target_model
            ):
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
                # The live model, not the stored one: the check above proved they are
                # the same agent, and the stored copy is redacted. Nothing routes on
                # this -- the adapter reads `agent.model` and a successful turn
                # overwrites it with what actually answered -- but a route reporting
                # `[redacted]` as its model would be a lie on the way out.
                model=agent.model,
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

    async def _bind_review(
        self, request: Any, capability: str, owner: tuple[str, str]
    ) -> tuple[Any, ConsultRoute, AgentConfig, str | None]:
        """Bind the reviewer approved by ReviewService under identity-level exclusion."""
        review_id, reviewer_id = owner
        if request.target_agent != reviewer_id:
            raise StoreError(
                ConsultErrorCode.SESSION_TARGET_MISMATCH,
                f"review `{review_id}` reserved `{reviewer_id}`, not "
                f"`{request.target_agent or '(automatic routing)'}`",
            )
        try:
            self.config.check_reviewer(reviewer_id, "reviewers")
        except ValueError as exc:
            raise StoreError(ConsultErrorCode.SESSION_TARGET_MISMATCH, str(exc)) from exc
        agent = self.config.agents[reviewer_id]
        if conflict := host_identity_conflict(
            agent.runtime, agent.model, self.host_runtime, self.config.host.model
        ):
            raise StoreError(
                ConsultErrorCode.SESSION_TARGET_MISMATCH,
                f"review `{review_id}` cannot be sent to `{reviewer_id}`: {conflict}",
            )

        existing = await self.store.review_consultation(review_id, reviewer_id)
        if existing is not None:
            if request.consultation_id is not None and str(request.consultation_id) != existing.id:
                raise StoreError(
                    ConsultErrorCode.SESSION_TARGET_MISMATCH,
                    f"review `{review_id}` already owns consultation `{existing.id}`, not "
                    f"`{request.consultation_id}`",
                )
            if (
                existing.target_agent_id != agent.agent_id
                or existing.target_runtime != agent.runtime
                or existing.target_model != scrub_json(agent.model)
            ):
                raise StoreError(
                    ConsultErrorCode.SESSION_TARGET_MISMATCH,
                    f"review `{review_id}` was approved for {existing.target_runtime}/"
                    f"{existing.target_model}, which is now {agent.runtime}/{agent.model}",
                )
            return existing, self._route_for(agent, capability), agent, existing.native_session_id
        if request.consultation_id is not None:
            raise StoreError(
                ConsultErrorCode.SESSION_NOT_FOUND,
                f"review `{review_id}` has no consultation `{request.consultation_id}` to resume",
            )

        route = self._route_for(agent, capability)
        consultation_id = uuid4()
        consultation = await self.store.create_consultation(
            consultation_id=consultation_id,
            origin_runtime=self.host_runtime,
            route=route,
            capability=capability,
            protocol_version=self.config.protocol_version,
            config_hash=self.config.config_hash(),
            conversation_label=request.conversation_label,
            review_id=review_id,
            review_agent_id=reviewer_id,
        )
        await self.store.record_routing(
            consultation_id,
            RoutingDecision(capability=capability, selected=agent, route=route),
        )
        return consultation, route, agent, None

    # --- the workflow's private path ----------------------------------------
    #
    # Not reachable from MCP. `consult_step` takes a `StepSnapshot`, which only the
    # workflow service can produce, and there is no argument on `consult()` that
    # selects this path -- the two entry points pick their bind method by name.

    async def consult_step(
        self, snapshot: StepSnapshot, prompt: str, context: str | None = None
    ) -> ConsultResponse:
        """Run one workflow step as a consultation.

        Everything below the bind is the ordinary read-only path: same adapter, same
        compiled prompt, same lease, same envelope. What differs is who the step may
        be sent to, and that was decided when the workflow resolved its bindings --
        this method re-checks the snapshot against live configuration rather than
        trusting it, because the config can have been edited since.
        """
        started = time.perf_counter()
        capability = snapshot.capability or "<invalid>"
        try:
            await self.open()
            return await self._consult_step(started, snapshot, prompt, context)
        except Exception as exc:
            return _failed(
                None, capability, SourceMode.AUTO, ConsultErrorCode.TRANSPORT_ERROR,
                f"the consultation failed inside the orchestrator ({type(exc).__name__})",
                started,
            )

    async def _consult_step(
        self, started: float, snapshot: StepSnapshot, prompt: str, context: str | None
    ) -> ConsultResponse:
        capability = snapshot.capability or "<invalid>"
        # `web` only ever narrows to what the step asked for; document/model is the
        # same auto rule the public path uses.
        source_mode = (
            SourceMode.WEB if snapshot.web
            else (SourceMode.DOCUMENT if context and context.strip() else SourceMode.MODEL)
        )

        if snapshot.executor != "agent" or snapshot.capability is None or not snapshot.agent_id:
            return _failed(None, capability, source_mode, ConsultErrorCode.INVALID_REQUEST,
                           "this step has no delegated agent to consult", started)
        if snapshot.execution_mode not in ("consultation", "patch"):
            # `isolated_write` has its own package and its own protocol. Letting it
            # fall through to here would run a write step as a plain question and
            # report the answer as if the work had been done.
            return _failed(None, capability, source_mode, ConsultErrorCode.INVALID_REQUEST,
                           f"`{snapshot.execution_mode}` steps do not run over the consult path",
                           started)

        try:
            request = self.request_model(
                capability=snapshot.capability, prompt=prompt, context=context,
                source_mode=source_mode,
            )
        except Exception as exc:
            return _failed(None, capability, source_mode, ConsultErrorCode.INVALID_REQUEST,
                           str(exc), started)

        try:
            consultation, route, agent, resuming = await self._bind_workflow(snapshot)
        except StoreError as exc:
            return _failed(None, capability, source_mode, exc.code, str(exc), started)

        consultation_id = UUID(consultation.id)
        try:
            async with self.store.lease(consultation_id, ttl_s=self._lease_ttl(agent)):
                return await self._turn(
                    consultation_id, agent, route, request, source_mode, resuming, started
                )
        except StoreError as exc:
            return _failed(consultation_id, capability, source_mode, exc.code, str(exc), started)

    async def _bind_workflow(
        self, snapshot: StepSnapshot
    ) -> tuple[Any, ConsultRoute, AgentConfig, str | None]:
        """Find or create the consultation a workflow step owns.

        No router: the agent was chosen when the workflow resolved its bindings, and
        rerouting a running workflow is the replan handshake's job, not a side effect
        of a config edit. What is checked here is that the agent named in the snapshot
        still *is* that agent, and that it is not this host.

        Exclusion is at execution identity rather than runtime, which is the whole
        reason this path exists -- and it is strictly a refinement: with no configured
        host model `host_identity_conflict` refuses the entire runtime, exactly as the
        public path does.
        """
        agent = self.config.agents.get(snapshot.agent_id or "")
        if agent is None:
            raise StoreError(
                ConsultErrorCode.SESSION_TARGET_MISMATCH,
                f"step `{snapshot.step_id}` is bound to agent `{snapshot.agent_id}`, which is "
                "no longer configured",
            )
        # Exact, not `_same_model`: the snapshot recorded what the config said, so any
        # difference at all is an edit under a running workflow, and continuing into it
        # would be a reroute nobody approved.
        if agent.runtime != snapshot.agent_runtime or agent.model != snapshot.agent_model:
            raise StoreError(
                ConsultErrorCode.SESSION_TARGET_MISMATCH,
                f"step `{snapshot.step_id}` was bound to `{snapshot.agent_id}` running "
                f"{snapshot.agent_runtime}/{snapshot.agent_model}, which is now configured as "
                f"{agent.runtime}/{agent.model}; replan the workflow to reroute it",
            )
        conflict = host_identity_conflict(
            agent.runtime, agent.model, self.host_runtime, self.config.host.model
        )
        if conflict is not None:
            raise StoreError(
                ConsultErrorCode.SESSION_TARGET_MISMATCH,
                f"step `{snapshot.step_id}` cannot be sent to `{agent.agent_id}`: {conflict}",
            )

        existing = await self.store.step_consultation(snapshot.workflow_id, snapshot.step_id)
        if existing is not None:
            if (
                existing.target_agent_id != agent.agent_id
                or existing.target_runtime != agent.runtime
                or existing.target_model != scrub_json(agent.model)
            ):
                raise StoreError(
                    ConsultErrorCode.SESSION_TARGET_MISMATCH,
                    f"step `{snapshot.step_id}` already holds a consultation with "
                    f"`{existing.target_agent_id}`; it cannot be continued against "
                    f"`{agent.agent_id}`",
                )
            return existing, self._route_for(agent, snapshot.capability), agent, \
                existing.native_session_id

        route = self._route_for(agent, snapshot.capability)
        consultation_id = uuid4()
        consultation = await self.store.create_consultation(
            consultation_id=consultation_id,
            origin_runtime=self.host_runtime,
            route=route,
            capability=snapshot.capability or "",
            protocol_version=self.config.protocol_version,
            config_hash=self.config.config_hash(),
            conversation_label=f"{snapshot.step} ({snapshot.workflow_id[:8]})",
            workflow_id=snapshot.workflow_id,
            step_id=snapshot.step_id,
        )
        return consultation, route, agent, None

    def _route_for(self, agent: AgentConfig, capability: str | None) -> ConsultRoute:
        return ConsultRoute(
            agent_id=agent.agent_id,
            runtime=agent.runtime,
            model=agent.model,
            capability_score=agent.score_for(capability) if capability else 0,
            priority=agent.priority,
            # A binding is a choice someone made and wrote down, which is what this
            # flag means everywhere else.
            explicitly_selected=True,
        )

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
                user_prompt=self._scrub(request.prompt), context=self._scrub(request.context),
                compiled_prompt=self._scrub(prompt.full_text), error_code=code,
            )
            return _failed(consultation_id, capability, source_mode, code, message, started,
                           agent_id=agent.agent_id, required_action=action)

        status = await self._status(adapter, agent, cached=True)
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
            if exc.code in (
                ConsultErrorCode.AGENT_NOT_INSTALLED,
                ConsultErrorCode.CONNECTION_REQUIRED,
            ):
                self._forget_status(agent)
            return await fail(exc.code, str(exc), exc.required_action)

        await self.store.bind_native_session(consultation_id, result.native_session_id)
        await self.store.record_turn(
            consultation_id,
            sequence,
            source_mode,
            user_prompt=self._scrub(request.prompt),
            context=self._scrub(request.context),
            compiled_prompt=self._scrub(prompt.full_text),
            raw_output=self._scrub(result.raw_output),
            # Scrubbed as a dict, before `record_turn` serializes it. The structured
            # answer is a column like any other, and a sanitizer that only understood
            # text would have left a credential here with `raw_output` clean.
            validated_response=self._scrub(result.content.model_dump()),
            input_tokens=result.usage.prompt_tokens,
            output_tokens=result.usage.completion_tokens,
            total_tokens=result.usage.total_tokens,
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

    async def _status(
        self, adapter: ConsultAdapter, agent: AgentConfig, cached: bool = False
    ) -> AgentStatus:
        """Probe the CLI, optionally reusing a recent `ready` answer.

        `cached=True` is for the preflight a turn pays on its way to the real work.
        It is off by default because `list_agents(check=True)` *is* the probe: a
        dashboard refresh that answered from memory would report a login state
        nobody checked, under a timestamp that says somebody did.

        **Only a ready status is ever cached.** A failure hands the caller a
        `required_action` login command, and the retry after the user runs it must
        not be answered from a stale "not authenticated" -- which would make the
        cache the reason the agent stays unusable.
        """
        key = (agent.agent_id, agent.command, agent.model)
        if cached and (hit := self._ready_until.get(key)) is not None:
            expires_at, status = hit
            if expires_at > time.monotonic():
                # No `agent_status_checks` row: that table means "we probed", which is
                # what the dashboard reads it as, and a row here would be a probe that
                # did not happen.
                log.debug("preflight served from cache for %s", agent.agent_id)
                return status
            del self._ready_until[key]

        try:
            status = await adapter.preflight(agent)
        except AdapterError as exc:
            status = AgentStatus(agent.agent_id, installed=False, authenticated=False,
                                 detail=str(exc)[:200])
        await self.store.record_status_check(
            agent.agent_id, status.installed, status.authenticated, status.detail
        )
        if status.ready and self.config.preflight_ttl_s:
            self._ready_until[key] = (
                time.monotonic() + self.config.preflight_ttl_s,
                status,
            )
        return status

    def _forget_status(self, agent: AgentConfig) -> None:
        """Drop a cached `ready` that the turn itself just disproved.

        A CLI can be logged out between the probe and the run, and without this the
        cache would keep answering `ready` for the rest of its TTL while every turn
        failed for the reason it is holding stale."""
        self._ready_until.pop((agent.agent_id, agent.command, agent.model), None)

    # --- read-only surfaces -------------------------------------------------

    async def list_agents(
        self, check: bool = True, agent_ids: list[str] | None = None
    ) -> ConsultAgentsResponse:
        # Filtered here rather than by the caller afterwards: with `check=True` every
        # row costs a subprocess, so narrowing after the fact would preflight agents
        # nobody asked about. None means all of them, as it always did.
        wanted = None if agent_ids is None else set(agent_ids)
        unknown = sorted((wanted or set()) - set(self.config.agents))
        if unknown:
            raise ValueError(f"unknown agent id{'s' if len(unknown) != 1 else ''}: {', '.join(unknown)}")
        agents = []
        for agent_id, agent in sorted(self.config.agents.items()):
            if wanted is not None and agent_id not in wanted:
                continue
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
                    "total_tokens": t.total_tokens,
                    "cost_usd": t.cost_usd,
                    "latency_ms": t.latency_ms,
                    "error_code": t.error_code,
                    "created_at": t.created_at,
                }
                for t in turns
            ],
            routing=await self.store.routing_for(consultation_id),
        )

    async def delete_consultation(self, consultation_id: UUID | str) -> int:
        await self.open()
        return await self.store.delete_consultation(consultation_id)

    async def request_delete_all_consultations(self) -> tuple[str, int]:
        await self.open()
        return await self.store.request_delete_all_consultations()

    async def delete_all_consultations(self, confirm_token: str) -> int:
        await self.open()
        return await self.store.delete_all_consultations(confirm_token)


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
