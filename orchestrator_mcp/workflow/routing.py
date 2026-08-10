"""Which worker takes a step, resolved once and then frozen.

`ConsultRouter` is untouched and still refuses the host's whole runtime; this is a
second router with the same scoring and different eligibility, because a workflow
knows two things a consultation does not: which step it is filling, and which
execution identity the host itself is.

Everything resolved here is written into the workflow's binding snapshot at
creation. A config edited halfway through does not reroute a running workflow --
that is what the replan handshake is for -- so this module runs at
`workflow_start` and at `replan`, and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..consult.config import AgentConfig, ConsultConfig, StepBinding
from ..consult.contract import Capability, ExecutionMode
from ..consult.errors import ConsultErrorCode
from .contract import (
    STEPS,
    Executor,
    RepositoryAccess,
    Step,
    WorkflowError,
    repository_access_for,
)
from .identity import host_identity_conflict

# The two runtimes whose adapters accept `source_mode=web`. opencode and antigravity
# refuse it outright -- not conditionally on `web_search:` -- so an agent on either
# cannot take a step that needs the web however it is configured.
WEB_RUNTIMES = frozenset({"codex", "claude"})


@dataclass(frozen=True)
class ResolvedStep:
    """One step's binding, fully decided.

    `agents` is a tuple because `review` takes several; every other step resolves to
    exactly one, or to none when the host is doing it.
    """

    step: Step
    executor: Executor
    execution_mode: ExecutionMode | None
    repository_access: RepositoryAccess
    capability: Capability | None
    agents: tuple[AgentConfig, ...]
    web: bool

    def as_binding(self) -> dict:
        """What gets stored on the workflow, and what a status view reads back."""
        return {
            "executor": self.executor,
            "execution": self.execution_mode,
            "repository_access": self.repository_access,
            "capability": self.capability,
            "web": self.web,
            "agents": [
                {"agent_id": a.agent_id, "runtime": a.runtime, "model": a.model}
                for a in self.agents
            ],
        }


class WorkflowRouter:
    def __init__(self, config: ConsultConfig, host_runtime: str) -> None:
        self.config = config
        self.host_runtime = host_runtime
        self.host_model = config.host.model

    def resolve_all(
        self, bindings: dict[Step, StepBinding], want_web: bool
    ) -> dict[Step, ResolvedStep]:
        """Every configured step, resolved together.

        All of them at creation rather than each one when it is reached: an operator
        finding out at the review step that no agent can review is finding out after
        paying for research, planning and implementation.
        """
        return {step: self.resolve(step, binding, want_web) for step, binding in bindings.items()}

    def resolve(self, step: Step, binding: StepBinding, want_web: bool) -> ResolvedStep:
        definition = STEPS[step]
        web = bool(want_web and definition.needs_web)

        if binding.executor == "host":
            return ResolvedStep(
                step=step,
                executor="host",
                execution_mode=None,
                repository_access=repository_access_for("host", None),
                capability=definition.capability,
                agents=(),
                # The host reaches the web the way it always does; nothing here grants
                # or withholds that, so claiming it as a property of the step would be
                # a value nobody set.
                web=False,
            )

        # Everything below is delegated, so the step has to have a delegated form at
        # all. `apply_patch` does not: only the party holding the working tree can
        # apply anything to it.
        if not definition.allowed_execution_modes:
            raise WorkflowError(
                ConsultErrorCode.INVALID_REQUEST,
                f"`{step}` is host-only and cannot be given to an agent",
            )
        # The same three checks the config runs at boot, so a binding overridden at
        # `workflow_start` cannot arrive by a door that skips them.
        try:
            self.config.check_binding(step, binding)
        except ValueError as exc:
            raise WorkflowError(ConsultErrorCode.INVALID_REQUEST, str(exc)) from exc

        mode = binding.execution
        named = binding.agents or ([binding.agent] if binding.agent else [])
        agents = (
            self._explicit(step, named, mode, web)
            if named
            else (self._auto(step, mode, web),)
        )
        return ResolvedStep(
            step=step,
            executor="agent",
            execution_mode=mode,
            repository_access=repository_access_for("agent", mode),
            capability=definition.capability,
            agents=agents,
            web=web,
        )

    def _explicit(
        self, step: Step, named: list[str], mode: ExecutionMode, web: bool
    ) -> tuple[AgentConfig, ...]:
        chosen: list[AgentConfig] = []
        for agent_id in named:
            agent = self.config.agents[agent_id]  # `check_binding` proved it exists
            if reason := self._ineligible(agent, step, mode, web):
                raise WorkflowError(
                    ConsultErrorCode.NO_AGENT_AVAILABLE,
                    f"`workflow.bindings.{step}` names `{agent_id}`, which cannot take it: "
                    f"{reason}",
                )
            chosen.append(agent)
        return tuple(chosen)

    def _auto(self, step: Step, mode: ExecutionMode, web: bool) -> AgentConfig:
        definition = STEPS[step]
        capability = definition.capability or ""
        eligible = [
            agent
            for agent in self.config.agents.values()
            if not self._ineligible(agent, step, mode, web)
        ]
        if not eligible:
            refused = "; ".join(
                f"`{agent_id}`: {reason}"
                for agent_id, agent in sorted(self.config.agents.items())
                if (reason := self._ineligible(agent, step, mode, web))
            )
            raise WorkflowError(
                ConsultErrorCode.NO_AGENT_AVAILABLE,
                f"no configured agent can take `{step}` as `{mode}`"
                + (f" ({refused})" if refused else ""),
            )
        # Same order as the consult router: score, then priority ascending, then the id
        # so that two identically configured agents resolve the same way twice.
        return min(eligible, key=lambda a: (-a.score_for(capability), a.priority, a.agent_id))

    def _ineligible(
        self, agent: AgentConfig, step: Step, mode: ExecutionMode, web: bool
    ) -> str | None:
        definition = STEPS[step]
        if not agent.enabled:
            return "disabled"
        if conflict := host_identity_conflict(
            agent.runtime, agent.model, self.host_runtime, self.host_model
        ):
            return conflict
        capability = definition.capability
        if capability is not None and agent.score_for(capability) <= 0:
            return f"scores 0 for `{capability}`"
        if mode not in self.config.effective_modes(agent):
            return f"cannot be asked for `{mode}`"
        if web and (agent.runtime not in WEB_RUNTIMES or not agent.web_search):
            return (
                f"`{step}` needs the web, and `{agent.runtime}` offers no web mode"
                if agent.runtime not in WEB_RUNTIMES
                else f"`{step}` needs the web, and `web_search:` is off for it"
            )
        return None
