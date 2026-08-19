"""The `consult:` block of the config.

Validated at boot for the same reason the `limits:` block is: a server that
half-understands its routing table is worse than one that refuses to start.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..code.registry import runtime_capabilities, unsupported_reason
from ..contract import MAX_REVIEWERS, ConfigError
from ..workflow.contract import STEPS, Step
from .contract import PROTOCOL_VERSION, Capability, ExecutionMode, Runtime
from .managed import DEFAULT_MANAGED_PATH, managed_path, read_managed_document

HOST_RUNTIME_ENV = "ORCHESTRATOR_HOST_RUNTIME"

Score = Annotated[int, Field(ge=0, le=100)]


class AgentConfig(BaseModel):
    """One consultable agent runtime. The id is the key it is filed under."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = ""  # filled in from the mapping key by `ConsultConfig`
    runtime: Runtime
    command: str = Field(min_length=1, description="Executable name or path, resolved on PATH.")
    model: str = Field(min_length=1)
    # Ascending: a lower number wins a tie, which is how "priority 1" reads to
    # everyone who has ever configured a resolver.
    priority: int = Field(default=100, ge=0)
    enabled: bool = True
    # A capability missing from the map scores 0, which means ineligible -- the same
    # as spelling out `0`. Omission is the common way to say "not this one".
    scores: dict[Capability, Score] = Field(default_factory=dict)
    web_search: bool = False
    # Codex only, and worth spelling out because the adapter passes
    # `--ignore-user-config`: whatever `~/.codex/config.toml` says about reasoning is
    # deliberately not inherited, so an unset field here is the model's own default,
    # not the operator's. A closed set because a typo would otherwise be invisible --
    # it would run, quietly, at a depth nobody chose.
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # What the operator will let this agent be asked to do, which is not the same as
    # what its runtime can be held to -- see `code.registry.RUNTIME_CAPABILITIES`. A
    # workflow step may use a mode only if it is in both. The default is the mode the
    # consult path has always used, so an existing config grants nothing new.
    execution_modes: list[ExecutionMode] = Field(default_factory=lambda: ["consultation"])
    # Which file this came from, so the dashboard can offer to edit the ones it owns
    # and only show the rest. `exclude=True` keeps it out of `model_dump`, which is
    # what `config_hash` hashes and what gets written back: moving an agent between
    # two files does not change how it routes, so it must not change the hash a stored
    # consultation is read against.
    managed: bool = Field(default=False, exclude=True)

    def score_for(self, capability: str) -> int:
        return self.scores.get(capability, 0)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def _effort_is_codex_only(self) -> AgentConfig:
        # The Claude adapter has no equivalent knob and never reads this field, so
        # accepting it there would leave an operator with a config that looks set and
        # runs at whatever the CLI chose. Silently ignored is the one outcome worth
        # refusing to boot over.
        #
        # Antigravity is excluded for the opposite reason: it has an `--effort` flag,
        # but every model slug `agy models` returns already carries the level
        # (`gemini-3.6-flash-low`), and passing both is a hard error from the CLI --
        # `--model gemini-3.6-flash-low conflicts with --effort=high`. The slug is the
        # knob there, so this field would have nothing left to say.
        if self.reasoning_effort and self.runtime != "codex":
            reason = (
                "antigravity encodes it in the model slug"
                if self.runtime == "antigravity"
                else f"`{self.runtime}` ignores it"
            )
            raise ValueError(f"`reasoning_effort` is codex-only; {reason}")
        return self


class DashboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # A second flag rather than a mode of the first: turning the dashboard on gets you
    # a window, and read-only stays what that word means. Editing is a different thing
    # to consent to, so it is a different line in the config.
    editable: bool = False
    # Loopback only, and validated as such: a read-only page over stored prompts is
    # still every prompt the user has ever sent.
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("dashboard.host must be a loopback address")
        return value


MAX_DEEP_REVIEWERS = 5


class ReviewConfig(BaseModel):
    """Who reviews.

    Named agents rather than a capability score, because a review is only worth
    comparing with the last one if the same models wrote both. The score still has
    to be there -- see `check_reviewer` -- it just does not do the choosing.
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)

    reviewers: list[str] = Field(default_factory=list)
    deep_reviewers: list[str] = Field(default_factory=list)
    # Files supplied through context_paths must resolve beneath one of these. Empty
    # disables server-side path reads; callers can still pass material as context.
    roots: list[Path] = Field(default_factory=list)

    @field_validator("roots")
    @classmethod
    def _expand_roots(cls, value: list[Path]) -> list[Path]:
        expanded: list[Path] = []
        for root in value:
            path = Path(os.path.expandvars(str(root))).expanduser()
            if str(path).strip() in ("", "."):
                raise ValueError("a review root must name a directory, not a blank path")
            if not path.is_absolute():
                raise ValueError(f"review root `{path}` must be absolute")
            if path == Path(path.anchor):
                raise ValueError(f"`{path}` is a filesystem root and cannot be a review root")
            expanded.append(path)
        return expanded

    @field_validator("reviewers")
    @classmethod
    def _exactly_one(cls, value: list[str]) -> list[str]:
        if len(value) != 1:
            raise ValueError(
                f"`reviewers:` must name exactly one agent (got {len(value)}); "
                "`deep_reviewers:` is the list for asking several"
            )
        return value

    @field_validator("deep_reviewers")
    @classmethod
    def _one_to_five(cls, value: list[str]) -> list[str]:
        if not 1 <= len(value) <= MAX_DEEP_REVIEWERS:
            raise ValueError(
                f"`deep_reviewers:` must name 1 to {MAX_DEEP_REVIEWERS} agents "
                f"(got {len(value)})"
            )
        return value

    @field_validator("reviewers", "deep_reviewers")
    @classmethod
    def _no_duplicates(cls, value: list[str]) -> list[str]:
        # The same agent twice is two paid requests to one model, returned as though
        # two of them agreed.
        repeated = sorted({agent_id for agent_id in value if value.count(agent_id) > 1})
        if repeated:
            raise ValueError(f"names {', '.join(repr(a) for a in repeated)} more than once")
        return value


class HostConfig(BaseModel):
    """Which execution identity this installation *is*.

    `runtime` still comes from `ORCHESTRATOR_HOST_RUNTIME` and that stays the
    authority; naming it here is an assertion, checked at boot by
    `check_host_runtime`, so a config that has drifted from the environment refuses
    rather than routes.

    `model` is the part the environment cannot supply. With it, a workflow step can
    exclude the host by execution identity and let a *different* model on the same
    runtime take the step. Without it, exclusion stays at runtime level -- which is
    what the consult path has always done, and the conservative answer.

    Neither ever comes from a tool argument, for the reason `host_runtime` gives.
    """

    model_config = ConfigDict(extra="forbid")

    runtime: Runtime | None = None
    model: str | None = Field(default=None, min_length=1)


class ReviewPolicy(BaseModel):
    """Who may review a workflow's own work.

    Checked against resolved execution identity, not vendor names: two agent ids
    pointing at one model are one reviewer, however they are spelled.
    """

    model_config = ConfigDict(extra="forbid")

    different_from_implementer: bool = True
    different_from_planner: bool = False


class StepBinding(BaseModel):
    """Which worker takes one step, and how.

    Three shapes, and the validator refuses the mixtures:
      * `{executor: host}`            -- the host does it
      * `{agent: x, execution: patch}` -- that agent, that mode
      * `{agents: [x, y]}`             -- review only
    An `agent:` left out with `executor: agent` means `auto`: routed by score.
    """

    model_config = ConfigDict(extra="forbid")

    executor: Literal["host", "agent"] = "agent"
    agent: str | None = None
    agents: list[str] | None = None
    execution: ExecutionMode = "consultation"

    @model_validator(mode="after")
    def _one_shape(self) -> StepBinding:
        if self.executor == "host":
            if self.agent or self.agents:
                raise ValueError("`executor: host` takes no `agent:` or `agents:`")
            return self
        if self.agent and self.agents:
            raise ValueError("name either `agent:` or `agents:`, not both")
        if self.agents is not None and len(self.agents) != len(set(self.agents)):
            # The same agent twice is two paid requests to one model, returned as
            # though two of them agreed. The review config refuses this too.
            raise ValueError("`agents:` names the same agent more than once")
        if self.agents == []:
            raise ValueError("`agents:` must name at least one agent")
        return self


class WorkflowConfig(BaseModel):
    """The three-phase workflow. Absent means the workflow tools are not advertised."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    # The bound on review/fix. Reached with serious findings still open, the workflow
    # ends in `needs_attention` rather than claiming to be done.
    max_fix_rounds: int = Field(default=5, ge=1, le=20)
    # Where a workflow may work. Empty means nowhere for `isolated_write`; a host
    # workflow still needs its workdir to resolve beneath one of these.
    roots: list[Path] = Field(default_factory=list)
    # Off by default: a failed test that advances to review spends reviewers on code
    # already known to be broken.
    advance_on_failed_test: bool = False
    # Its own timeout, because `consult.timeout_s` is sized for a question and an
    # `isolated_write` step is a coding session: it reads files, edits them, runs the
    # tests and fixes what it broke. Raising the global one to suit that would give
    # every consultation a quarter of an hour to answer.
    execution_timeout_s: int = Field(default=900, ge=1)
    review_policy: ReviewPolicy = Field(default_factory=ReviewPolicy)
    bindings: dict[Step, StepBinding] = Field(default_factory=dict)

    @field_validator("roots")
    @classmethod
    def _expand_roots(cls, value: list[Path]) -> list[Path]:
        expanded: list[Path] = []
        for root in value:
            path = Path(os.path.expandvars(str(root))).expanduser()
            if str(path).strip() in ("", "."):
                raise ValueError("a root must name a directory, not a blank path")
            # `/` as a root is every file the user owns, which is not a root, it is
            # the absence of one written as though it were a decision.
            if path == Path(path.anchor):
                raise ValueError(f"`{path}` is a filesystem root and cannot be a workflow root")
            expanded.append(path)
        return expanded


class ConsultConfig(BaseModel):
    # `validate_default` so the default database path is expanded too: `~` reaching
    # `sqlite3.connect` creates a directory literally named `~` next to wherever the
    # host happened to launch the server.
    model_config = ConfigDict(extra="forbid", validate_default=True)

    database_path: Path = Path("~/.orchestrator-mcp/consultations.sqlite3")
    # The file the dashboard writes. Agents in it are merged with the ones below at
    # load; see `managed.py` for why they are not simply kept here.
    managed_agents_path: Path = DEFAULT_MANAGED_PATH
    timeout_s: int = Field(default=180, ge=1)
    protocol_version: str = PROTOCOL_VERSION
    store_full_content: bool = True
    # Claude Code 2.1.220 has no `--max-turns`, so the bound on a web-mode
    # consultation is ours to enforce: we count assistant turns in the event stream
    # and kill the child when it passes this.
    web_turn_limit: int = Field(default=8, ge=1)
    agents: dict[str, AgentConfig] = Field(min_length=1)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    # Absent means the review tools are not advertised at all. A server with no
    # reviewers configured should not offer a `review` tool that can only refuse.
    review: ReviewConfig | None = None
    host: HostConfig = Field(default_factory=HostConfig)
    # Absent means the workflow tools are not advertised, same as `review:`.
    workflow: WorkflowConfig | None = None

    @field_validator("database_path", "managed_agents_path")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        # `Path("")` is `Path(".")`, so a blank path silently becomes the working
        # directory and everything downstream tries to open a directory as a file.
        # Refusing says which line is empty; the alternative reads as a bug in us.
        #
        # Checked *after* expansion, not before: `$SOMETHING` that expands to nothing
        # is the same blank path arriving by a longer route, and it is the likelier of
        # the two -- an unset variable is easier to have than an empty YAML value.
        expanded = Path(os.path.expandvars(str(value))).expanduser()
        if str(expanded).strip() in ("", "."):
            raise ValueError("must name a file, not a blank path")
        return expanded

    @field_validator("protocol_version")
    @classmethod
    def _pinned(cls, value: str) -> str:
        if value != PROTOCOL_VERSION:
            raise ValueError(f"protocol_version must be {PROTOCOL_VERSION!r}, not {value!r}")
        return value

    @field_validator("agents")
    @classmethod
    def _label_agents(cls, agents: dict[str, AgentConfig]) -> dict[str, AgentConfig]:
        for agent_id, agent in agents.items():
            if not agent_id.strip():
                raise ValueError("agent ids must not be blank")
            agent.agent_id = agent_id
        return agents

    @model_validator(mode="after")
    def _reviewers_can_review(self) -> ConsultConfig:
        if self.review is not None:
            for where in ("reviewers", "deep_reviewers"):
                for agent_id in getattr(self.review, where):
                    self.check_reviewer(agent_id, where)
        return self

    @model_validator(mode="after")
    def _agents_can_execute(self) -> ConsultConfig:
        """An agent cannot be granted a mode its runtime does not support.

        Refused at boot rather than at the step, because the operator who wrote
        `isolated_write` under an opencode agent believes their fix rounds are
        contained. Finding out at the fourth step of a workflow is finding out late.
        """
        for agent_id, agent in sorted(self.agents.items()):
            supported = runtime_capabilities(agent.runtime)
            for mode in agent.execution_modes:
                if mode not in supported:
                    raise ValueError(
                        f"agent `{agent_id}` declares `{mode}`, but "
                        f"{unsupported_reason(agent.runtime, mode)}"
                    )
        return self

    @model_validator(mode="after")
    def _workflow_is_runnable(self) -> ConsultConfig:
        if self.workflow is None:
            return self
        if not self.store_full_content:
            # A workflow *is* its stored plans, briefs, patches, findings and
            # reports. With bodies dropped the review step cannot finalize at all,
            # so the failure would otherwise arrive several paid steps late.
            raise ValueError(
                "`workflow:` requires `store_full_content: true`: a workflow's plans, "
                "briefs, patches and reports are its state, and a review cannot be "
                "finalized without them"
            )
        for step, binding in sorted(self.workflow.bindings.items()):
            self.check_binding(step, binding)
        return self

    def check_binding(self, step: Step, binding: StepBinding) -> None:
        """Refuse a binding the step, the agent or the runtime cannot honour.

        Shared with workflow creation so an override supplied at `workflow_start`
        goes through the same three checks, and a binding refused at boot cannot
        arrive later by another door.
        """
        definition = STEPS[step]
        where = f"`workflow.bindings.{step}`"
        if binding.executor == "host":
            return
        if binding.agents is not None and not definition.supports_multiple_agents:
            raise ValueError(f"{where} names several agents, but `{step}` takes one")
        if step == "review" and binding.agents is not None and len(binding.agents) > MAX_REVIEWERS:
            raise ValueError(
                f"{where} names {len(binding.agents)} agents; a review takes at most "
                f"{MAX_REVIEWERS}"
            )
        if binding.execution not in definition.allowed_execution_modes:
            allowed = ", ".join(f"`{m}`" for m in definition.allowed_execution_modes)
            raise ValueError(
                f"{where} asks for `{binding.execution}`, which `{step}` does not allow"
                + (f" (allowed: {allowed})" if allowed else f"; `{step}` is host-only")
            )
        for agent_id in binding.agents or ([binding.agent] if binding.agent else []):
            self.check_step_agent(agent_id, step, binding.execution, where)

    def check_step_agent(
        self, agent_id: str, step: Step, mode: ExecutionMode, where: str
    ) -> None:
        """The three things a named agent has to be: configured, enabled, and both
        scored and permitted for what the step asks of it."""
        definition = STEPS[step]
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"{where} names `{agent_id}`, which is not a configured agent")
        if not agent.enabled:
            raise ValueError(f"{where} names `{agent_id}`, which is disabled")
        capability = definition.capability
        if capability is not None and agent.score_for(capability) <= 0:
            raise ValueError(
                f"{where} names `{agent_id}`, which scores 0 for `{capability}`; give it "
                f"a `{capability}:` entry under its `scores:` or name a different agent"
            )
        if mode not in self.effective_modes(agent):
            if mode not in agent.execution_modes:
                raise ValueError(
                    f"{where} asks `{agent_id}` for `{mode}`, which is not in its "
                    "`execution_modes:`"
                )
            raise ValueError(
                f"{where} asks `{agent_id}` for `{mode}`, but "
                f"{unsupported_reason(agent.runtime, mode)}"
            )

    @staticmethod
    def effective_modes(agent: AgentConfig) -> set[ExecutionMode]:
        """What an agent may actually be asked to do: operator trust intersected with
        what the code can hold the runtime to. Declaring a mode in YAML cannot make a
        runtime support it."""
        return set(agent.execution_modes) & runtime_capabilities(agent.runtime)

    def check_host_runtime(self, actual: Runtime) -> None:
        """Refuse a `consult.host.runtime:` that disagrees with the environment.

        The environment wins because it is the trusted source; a config that names a
        different runtime is not a second opinion, it is a file that has drifted, and
        every exclusion downstream would be computed against the wrong identity.
        """
        declared = self.host.runtime
        if declared is not None and declared != actual:
            raise ConfigError(
                f"`consult.host.runtime:` says `{declared}` but {HOST_RUNTIME_ENV} says "
                f"`{actual}`. The environment is the authority; fix whichever is stale."
            )

    def check_reviewer(self, agent_id: str, where: str = "reviewers") -> None:
        """Refuse an agent that cannot take a review, wherever it was named.

        Shared with the runtime paths on purpose: an explicit reviewer list on one
        call, and a recheck's selection, go through these same three checks, so a
        reviewer refused at boot cannot arrive later by another door.

        The host's own runtime is not checked here -- this config has no way to know
        which runtime it is running under. `ReviewService.plan` does, and refuses it
        there, before anything is approved.
        """
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"`{where}:` names `{agent_id}`, which is not a configured agent")
        if not agent.enabled:
            raise ValueError(
                f"`{where}:` names `{agent_id}`, which is disabled; enable it or name another"
            )
        if agent.score_for("review") <= 0:
            raise ValueError(
                f"`{where}:` names `{agent_id}`, which scores 0 for `review`; give it a "
                "`review:` entry under its `scores:` or name a different agent"
            )

    def eligible(self, host_runtime: str) -> list[AgentConfig]:
        """Configured agents that could answer at all: enabled, and not ourselves."""
        return [a for a in self.agents.values() if a.enabled and a.runtime != host_runtime]

    def config_hash(self) -> str:
        """Recorded with each consultation, so a reply can be read against the
        routing table that produced it rather than today's."""
        payload = json.dumps(
            {aid: a.model_dump(mode="json") for aid, a in sorted(self.agents.items())},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_consult_config(config: dict[str, Any]) -> ConsultConfig | None:
    """Parse the `consult:` block, or return None when there is none.

    A config without the block is the 0.1.2 config, and must keep starting a server
    that behaves exactly as it did.
    """
    block = config.get("consult")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ConfigError("`consult:` must be a mapping")

    # Read once, merge twice. Two reads of the managed file can straddle a dashboard
    # save and take the agents from before it and the reviewers from after -- a
    # combination that was never written and that no validator downstream could spot.
    document = read_managed_document(managed_path(block))
    block = dict(block) | {
        "agents": _merged_agents(block, document),
        "review": _merged_review(block, document),
    }
    try:
        return ConsultConfig(**block)
    except ValidationError as exc:
        raise ConfigError(f"invalid `consult:` block: {exc}") from exc


def _merged_review(block: dict[str, Any], document: dict[str, Any]) -> Any:
    """The `review:` block, from whichever file has one.

    Not merged key by key the way the agents are: a reviewer list is a single
    decision, and half of it from each file is not a list anyone chose. Naming it in
    both is the same startup error, for the same reason -- the copy that lost would
    look edited and saved to whoever wrote it.
    """
    # An empty block is the absent block. `ReviewConfig` requires a reviewer, so
    # keeping `review: {}` would make an older managed file -- or a `review:` key
    # left behind with nothing under it -- refuse the boot, and refuse it in the one
    # process that can edit the file back. Absent already has a meaning and it is
    # this one: the review tools are not advertised.
    # `review: null` still disables the managed block -- that is an operator saying
    # so. `review: {}` is not a decision, it is what a file that never had reviewers
    # holds, so it defers like an absent key.
    written_here = "review" in block and block["review"] != {}
    written = block.get("review") or None
    managed = document["review"] or None
    if written is not None and managed is not None:
        raise ConfigError(
            f"`review:` is defined in both the config and {managed_path(block)}. Delete "
            "one: the server will not guess which was meant, because the copy it ignored "
            "would look edited and saved to whoever wrote it."
        )
    return written if written_here else managed


def _merged_agents(block: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """The agents from `config.yaml` and the ones the dashboard owns, as one mapping.

    Merged before `ConsultConfig` is built, so everything downstream -- routing, the
    config hash, the store, the dashboard's own tables -- sees one set of agents and
    none of them needs to know there are two files.
    """
    written = block.get("agents") or {}
    if not isinstance(written, dict):
        return written  # let pydantic produce the error, in its own words

    path = managed_path(block)
    managed = document["agents"]

    both = sorted(set(written) & set(managed))
    if both:
        raise ConfigError(
            f"agent{'s' if len(both) > 1 else ''} {', '.join(repr(a) for a in both)} "
            f"defined in both the config and {path}. Delete one: the server will not "
            "guess which was meant, because the copy it ignored would look edited and "
            "saved to whoever wrote it."
        )

    # `managed` is a declared field, so a hand-written `managed: true` in either file is
    # accepted and then overwritten here -- in both directions. Where an agent lives is
    # decided by which file it is in and not by what it claims, or the dashboard would
    # offer to edit an entry it has no business writing.
    def labelled(agents: dict[str, Any], owned: bool) -> dict[str, Any]:
        # Anything that is not a mapping is passed through untouched, so pydantic gets
        # to describe it in its own words rather than this raising a `dict()` TypeError
        # from three frames away.
        return {
            aid: (a | {"managed": owned}) if isinstance(a, dict) else a
            for aid, a in agents.items()
        }

    return labelled(written, False) | labelled(managed, True)


def host_runtime() -> Runtime:
    """Which runtime this installation *is*, so it can be excluded from routing.

    Read from the environment and never from a tool argument: a model that could
    name the host runtime could name someone else's, and delegate the work straight
    back to itself.
    """
    value = (os.environ.get(HOST_RUNTIME_ENV) or "").strip().lower()
    if value not in get_args(Runtime):
        known = ", ".join(f"`{name}`" for name in get_args(Runtime))
        raise ConfigError(
            f"{HOST_RUNTIME_ENV} must be set to one of {known} when `consult:` is "
            f"configured (got {value!r}); without it the server cannot exclude itself "
            "from its own routing"
        )
    return value  # type: ignore[return-value]
