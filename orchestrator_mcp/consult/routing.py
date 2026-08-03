"""Deterministic agent selection.

Nothing here is adaptive: same config, same request, same winner, every time. The
LiteLLM path can afford to shuffle deployments because they are interchangeable
endpoints; a consultation is bound to one agent for its whole life, so the choice
has to be reproducible and explainable after the fact.

There is deliberately no fallback. If the winner is not installed or not logged
in, the caller is told to connect it -- silently consulting a different vendor's
model than the one the scores chose is exactly the substitution this protocol
exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import AgentConfig, ConsultConfig
from .contract import ConsultRoute, SourceMode
from .errors import ConsultErrorCode


@dataclass(frozen=True)
class ExcludedCandidate:
    agent_id: str
    reason: str


@dataclass(frozen=True)
class RoutingDecision:
    """The winner, and everyone who lost and why. Persisted as-is."""

    capability: str
    selected: AgentConfig | None = None
    route: ConsultRoute | None = None
    excluded: list[ExcludedCandidate] = field(default_factory=list)
    error: tuple[ConsultErrorCode, str] | None = None


class SourceModeError(ValueError):
    """A source mode the request cannot support. Caught as a protocol violation."""


def resolve_source_mode(mode: SourceMode, context: str | None) -> SourceMode:
    """`auto` is a request-side convenience and never reaches a target.

    Resolved before the prompt is compiled, because the mode picks the system
    section, the tool set, and the sandbox flags -- three things that must agree.
    """
    if mode is SourceMode.AUTO:
        return SourceMode.DOCUMENT if context and context.strip() else SourceMode.MODEL
    if mode is SourceMode.DOCUMENT and not (context and context.strip()):
        raise SourceModeError("`source_mode=document` requires `context`")
    return mode


class ConsultRouter:
    def __init__(self, config: ConsultConfig, host_runtime: str) -> None:
        self.config = config
        self.host_runtime = host_runtime

    def select(self, capability: str, target_agent: str | None = None) -> RoutingDecision:
        agents = self.config.agents
        excluded = [
            ExcludedCandidate(agent_id, reason)
            for agent_id, agent in sorted(agents.items())
            if (reason := self._ineligible(agent, capability))
        ]

        if target_agent is not None:
            return self._explicit(capability, target_agent, excluded)

        eligible = [a for a in agents.values() if not self._ineligible(a, capability)]
        if not eligible:
            return RoutingDecision(
                capability=capability,
                excluded=excluded,
                error=(
                    ConsultErrorCode.NO_AGENT_AVAILABLE,
                    f"no configured agent scores above zero for `{capability}` once this "
                    f"installation's own runtime ({self.host_runtime}) is excluded",
                ),
            )

        # Score first, then priority ascending, then the id -- the last one purely so
        # that two identically configured agents still resolve the same way twice.
        winner = min(
            eligible,
            key=lambda a: (-a.score_for(capability), a.priority, a.agent_id),
        )
        return self._decision(capability, winner, excluded, explicit=False)

    def _explicit(
        self, capability: str, target_agent: str, excluded: list[ExcludedCandidate]
    ) -> RoutingDecision:
        agent = self.config.agents.get(target_agent)
        if agent is None:
            return RoutingDecision(
                capability=capability,
                excluded=excluded,
                error=(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"`{target_agent}` is not a configured agent "
                    f"(configured: {sorted(self.config.agents)})",
                ),
            )
        if reason := self._ineligible(agent, capability):
            return RoutingDecision(
                capability=capability,
                excluded=excluded,
                error=(
                    ConsultErrorCode.NO_AGENT_AVAILABLE,
                    f"`{target_agent}` cannot take this consultation: {reason}",
                ),
            )
        # An explicit target skips scoring the rest entirely, so the others are not
        # "excluded" by anything -- they were never compared.
        return self._decision(capability, agent, excluded, explicit=True)

    def _ineligible(self, agent: AgentConfig, capability: str) -> str | None:
        if not agent.enabled:
            return "disabled"
        if agent.runtime == self.host_runtime:
            # The whole reason the host runtime is an env var and not an argument.
            return f"runtime `{agent.runtime}` is this installation's own host runtime"
        if agent.score_for(capability) <= 0:
            return f"scores 0 for `{capability}`"
        return None

    def _decision(
        self,
        capability: str,
        agent: AgentConfig,
        excluded: list[ExcludedCandidate],
        explicit: bool,
    ) -> RoutingDecision:
        return RoutingDecision(
            capability=capability,
            selected=agent,
            route=ConsultRoute(
                agent_id=agent.agent_id,
                runtime=agent.runtime,
                model=agent.model,
                capability_score=agent.score_for(capability),
                priority=agent.priority,
                explicitly_selected=explicit,
            ),
            excluded=[e for e in excluded if e.agent_id != agent.agent_id],
        )
