"""The service that composes a review, with the CLIs replaced by stub adapters.

What is being proved is the composition and the promises around it: that planning
sends nothing, that the approval is bound to the reviewer rather than to its name,
that a token is spent once, that a partial review keeps the answers it got, and
that a cancel cannot be overwritten by a batch finishing a moment later.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from orchestrator_mcp.consult.adapters.base import AdapterError, AdapterResult, AgentStatus
from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.contract import ConsultationContent, ConsultSource, SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.store import ConsultStore
from orchestrator_mcp.contract import Usage
from orchestrator_mcp.review.contract import MAX_GOAL_CHARS, MAX_REVIEWERS
from orchestrator_mcp.review.service import ReviewService

from .conftest import agent

FINDINGS = (
    'I looked at it.\n\n```json\n{"findings": [{"location": "a.py:1", "severity": "critical", '
    '"why": "unbounded read", "example": "a 2GB file", "fix": "stream it"}]}\n```'
)

REVIEWERS = {
    "codex-sol": agent("codex", "gpt-5.6-sol", 10),
    "gemini-x": agent("antigravity", "gemini-3.6", 20),
}


class StubAdapter:
    """Answers with whatever it was given, and records the prompts it saw."""

    def __init__(
        self,
        answer: str = FINDINGS,
        error=None,
        status=None,
        delay: float = 0.0,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        sources: list[ConsultSource] | None = None,
    ) -> None:
        self.answer = answer
        self._sources = sources or []
        self._error = error
        self._status = status
        self._delay = delay
        self._entered = entered
        self._release = release
        self.prompts: list[str] = []
        self.modes: list[SourceMode] = []

    def connect_command(self, agent):
        return f"{agent.command} login"

    async def preflight(self, agent):
        return self._status or AgentStatus(agent.agent_id, installed=True, authenticated=True)

    async def start(self, agent, prompt, source_mode, session_id=None):
        return await self._answer(prompt, source_mode)

    async def resume(self, agent, native_session_id, prompt, source_mode):
        return await self._answer(prompt, source_mode)

    async def _answer(self, prompt, source_mode) -> AdapterResult:
        self.prompts.append(prompt.full_text)
        self.modes.append(source_mode)
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            await self._release.wait()
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return AdapterResult(
            content=ConsultationContent(
                answer=self.answer, assumptions=[], uncertainties=[],
                follow_up_questions=[], sources=list(self._sources),
            ),
            native_session_id="native-1",
            model_used="gpt-5.6-sol",
            model_verified=True,
            raw_output="{}",
            usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        )


class StubService(ReviewService):
    def __init__(self, *args, adapters: dict[str, StubAdapter], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.adapters = adapters
        # The consult path underneath is what actually runs the adapter.
        self.consult.adapter = lambda agent: adapters[agent.agent_id]  # type: ignore[method-assign]


@pytest.fixture
def build(tmp_path, host_claude):
    async def make(adapters: dict[str, StubAdapter] | None = None, store=None, **overrides):
        adapters = adapters or {aid: StubAdapter() for aid in REVIEWERS}
        config = ConsultConfig(
            **{
                "database_path": str(tmp_path / "c.sqlite3"),
                "agents": dict(REVIEWERS),
                "review": {"reviewers": ["codex-sol"], "deep_reviewers": list(REVIEWERS)},
                **overrides,
            }
        )
        return await StubService(config, "claude", store=store, adapters=adapters).open()

    return make


async def planned(service, **overrides):
    response = await service.plan(goal="review the parser", **overrides)
    assert response.error is None, response.error
    return response


async def sql(service, statement: str, *params):
    """Edit the database behind the service's back, to build a state the code cannot
    reach on purpose: a legacy row, or one written by a contract this build predates."""
    return await service.store._run(lambda: service.store._db.execute(statement, params))


# --- planning sends nothing -------------------------------------------------


async def test_planning_shows_what_would_be_sent_and_sends_it(build):
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    response = await planned(
        service,
        material=[{"label": "src/a.py", "kind": "file", "locator": "lines 1-80", "chars": 80}],
        context="def parse(): ...",
    )

    assert response.status == "pending" and response.results == []
    assert response.plan.expected_requests == 1
    assert [r.agent_id for r in response.plan.reviewers] == ["codex-sol"]
    assert response.plan.material[0].label == "src/a.py"
    assert response.plan.context_chars == len("def parse(): ...")
    # No adapter was touched. Not even a preflight.
    assert all(a.prompts == [] for a in adapters.values())


async def test_the_server_reads_the_paths_and_measures_what_it_read(build, tmp_path):
    """The reviewer has no filesystem, so naming a path only works if the server
    reads it. Having read it, the manifest is a measurement rather than a claim."""
    first = tmp_path / "a.py"
    first.write_text("def parse(): ...")
    second = tmp_path / "b.py"
    second.write_text("def emit(): ...")
    service = await build()

    response = await planned(service, context_paths=[str(first), str(second)])

    assert response.plan.material_verified is True
    assert [m.label for m in response.plan.material] == [str(first), str(second)]
    assert [m.chars for m in response.plan.material] == [16, 15]
    # The headers and the blank line between the two files count too.
    assert response.plan.context_chars > 16 + 15

    run = await service.run(response.review_id, response.plan.confirm_token)
    sent = next(iter(service.adapters.values())).prompts[0]
    assert "def parse(): ..." in sent and "def emit(): ..." in sent
    assert run.status == "awaiting_synthesis"


@pytest.mark.parametrize("kind", ["missing", "directory"])
async def test_a_path_that_is_not_a_readable_file_is_refused_by_name(build, tmp_path, kind):
    target = tmp_path / "gone.py" if kind == "missing" else tmp_path
    service = await build()

    response = await service.plan(goal="review the parser", context_paths=[str(target)])
    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert str(target) in response.error.message


async def test_context_and_context_paths_together_are_refused(build, tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def parse(): ...")
    service = await build()

    response = await service.plan(
        goal="review the parser", context="def parse(): ...", context_paths=[str(path)]
    )
    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "not both" in response.error.message


@pytest.mark.parametrize("over", ["goal", "reviewers"])
async def test_a_refused_plan_leaves_no_row_behind(build, over):
    """A `pending` row for a plan the caller was told was refused is unreachable:
    nothing returns its id, and it holds material nobody accepted.

    Two bounds because they fail in different places: the goal is checked here, and
    the reviewer count only by `ReviewPlan` -- which used to run after the insert."""
    many = {f"codex-{n}": agent("codex", f"gpt-5.6-{n}", 10) for n in range(MAX_REVIEWERS + 1)}
    service = await build(
        {aid: StubAdapter() for aid in many},
        agents=many,
        review={"reviewers": [next(iter(many))], "deep_reviewers": [next(iter(many))]},
    )

    if over == "goal":
        response = await service.plan(goal="x" * (MAX_GOAL_CHARS + 1))
    else:
        response = await service.plan(goal="review the parser", reviewers=list(many))

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert await service.list() == []


async def test_the_same_reviewer_named_twice_is_refused(build):
    """One row per `(review_id, agent_id)`, so the second task overwrites the first
    rather than adding an opinion -- and both were paid for."""
    service = await build()

    response = await service.plan(goal="review the parser", reviewers=["codex-sol", "codex-sol"])
    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "more than once" in response.error.message


async def test_a_deep_review_plans_every_configured_reviewer(build):
    service = await build()
    response = await planned(service, mode="deep")
    assert response.plan.expected_requests == 2


async def test_the_plan_reports_where_a_secret_shaped_string_was_found(build):
    service = await build()
    response = await planned(
        service, context="line one\nAPI_KEY = sk-ant-api03-AAAABBBBCCCCDDDDEEEE\nline three"
    )
    hits = response.plan.secret_hits

    assert [(h.field, h.line) for h in hits] == [("context", 2)]
    # A position, never a value: the plan is shown to a user and stored.
    assert "sk-ant" not in response.model_dump_json()


async def test_the_plan_warns_when_two_reviewers_share_one_model(build):
    """Two reviewers on one model agree cheaply and mean nothing."""
    service = await build(
        adapters={"a": StubAdapter(), "b": StubAdapter()},
        agents={"a": agent("codex", "same"), "b": agent("antigravity", "same")},
        review={"reviewers": ["a"], "deep_reviewers": ["a", "b"]},
    )
    response = await planned(service, mode="deep")
    assert response.plan.duplicate_models == ["same"]


async def test_the_plan_warns_when_a_reviewer_runs_the_hosts_own_model(build):
    service = await build()
    response = await planned(service, host_model="gpt-5.6-sol")
    assert response.plan.host_model_conflict == "gpt-5.6-sol"


async def test_a_reviewer_on_the_hosts_runtime_is_refused_at_plan_time(build):
    """The router would reject it later. Approving a send that is guaranteed to fail
    is a preview that lies."""
    service = await build(
        adapters={"self": StubAdapter()},
        agents={"self": agent("claude", "opus")},
        review={"reviewers": ["self"], "deep_reviewers": ["self"]},
    )
    response = await service.plan(goal="g")

    assert response.error.code == ConsultErrorCode.INVALID_REQUEST
    assert "own runtime" in response.error.message


async def test_planning_with_no_goal_is_refused(build):
    service = await build()
    assert (await service.plan(goal="   ")).error is not None


# --- the handshake ----------------------------------------------------------


async def test_running_an_approved_plan_asks_every_reviewer(build):
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    plan = await planned(service, mode="deep", context="the code")
    response = await service.run(plan.review_id, plan.plan.confirm_token, host_findings=["mine"])

    assert response.status == "awaiting_synthesis" and response.outcome == "all"
    assert {r.agent_id for r in response.results} == set(REVIEWERS)
    assert all(a.prompts for a in adapters.values())


async def test_a_reviewer_is_asked_with_the_instructions_appended_to_the_goal(build):
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    plan = await planned(service, context="the code")
    await service.run(plan.review_id, plan.plan.confirm_token)

    prompt = adapters["codex-sol"].prompts[0]
    assert "review the parser" in prompt
    # JSON-encoded into the compiled prompt, so the block's quotes arrive escaped.
    assert "findings" in prompt and "independent reviewers" in prompt


async def test_the_host_findings_never_reach_a_reviewer(build):
    """A deep review is worth having because the opinions were formed independently.
    Showing one reviewer the host's answer is how that stops being true."""
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    plan = await planned(service, mode="deep")
    await service.run(
        plan.review_id, plan.plan.confirm_token, host_findings=["the-host-said-this"]
    )

    assert all("the-host-said-this" not in p for a in adapters.values() for p in a.prompts)


async def test_a_deep_review_refuses_to_run_without_the_hosts_own_findings(build):
    service = await build()
    plan = await planned(service, mode="deep")
    response = await service.run(plan.review_id, plan.plan.confirm_token)

    assert "host_findings" in response.error.message
    assert response.status == "pending"  # the token was not spent


async def test_a_token_is_spent_once(build):
    service = await build()
    plan = await planned(service)
    await service.run(plan.review_id, plan.plan.confirm_token)
    again = await service.run(plan.review_id, plan.plan.confirm_token)

    assert again.error.code == ConsultErrorCode.INVALID_REQUEST


async def test_two_simultaneous_runs_launch_one_review(build):
    """Otherwise a doubled call is two paid requests for one approval."""
    service = await build()
    plan = await planned(service)
    first, second = await asyncio.gather(
        service.run(plan.review_id, plan.plan.confirm_token),
        service.run(plan.review_id, plan.plan.confirm_token),
    )
    assert sorted(r.error is None for r in (first, second)) == [False, True]


async def test_a_wrong_token_does_not_send_anything(build):
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    plan = await planned(service)
    response = await service.run(plan.review_id, "not-the-token")

    assert response.error is not None
    assert all(a.prompts == [] for a in adapters.values())


async def test_a_busy_lease_does_not_burn_the_confirmation(build):
    service = await build()
    plan = await planned(service)

    async with service.store.lease(plan.review_id, ttl_s=60):
        refused = await service.run(plan.review_id, plan.plan.confirm_token)
    assert refused.error.code == ConsultErrorCode.SESSION_BUSY
    assert refused.status == "pending"
    assert (await service.run(plan.review_id, plan.plan.confirm_token)).error is None


# --- the approval is bound to the reviewer, not its name --------------------


async def test_a_reviewer_whose_model_changed_between_plan_and_run_is_refused(build):
    service = await build()
    plan = await planned(service)
    service.config.agents["codex-sol"].model = "something-else"

    response = await service.run(plan.review_id, plan.plan.confirm_token)
    assert "changed since you approved" in response.error.message
    assert "model" in response.error.message


async def test_a_reviewer_that_vanished_between_plan_and_run_is_refused(build):
    service = await build()
    plan = await planned(service)
    del service.config.agents["codex-sol"]

    response = await service.run(plan.review_id, plan.plan.confirm_token)
    assert "no longer configured" in response.error.message


# --- secrets ----------------------------------------------------------------


async def test_by_default_the_reviewer_gets_the_redacted_copy(build):
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    secret = "sk-ant-api03-AAAABBBBCCCCDDDDEEEE"
    plan = await planned(service, context=f"KEY={secret}")
    await service.run(plan.review_id, plan.plan.confirm_token)

    assert secret not in adapters["codex-sol"].prompts[0]


async def test_send_as_is_sends_the_original_and_stores_the_redaction(build):
    """A user may knowingly approve what the pattern flagged -- a fixture's fake key,
    a variable named `password`."""
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    secret = "sk-ant-api03-AAAABBBBCCCCDDDDEEEE"
    goal, context = "review the parser", f"KEY={secret}"
    plan = await planned(service, context=context)

    await service.run(
        plan.review_id, plan.plan.confirm_token, secrets="send_as_is",
        raw={"goal": goal, "context": context},
    )
    assert secret in adapters["codex-sol"].prompts[0]
    assert secret not in (await service.store.get_review(plan.review_id)).context


async def test_a_mismatched_raw_copy_is_refused_without_burning_the_token(build):
    """A typo must not cost a whole new plan."""
    service = await build()
    plan = await planned(service, context="original")
    response = await service.run(
        plan.review_id, plan.plan.confirm_token, secrets="send_as_is",
        raw={"goal": "review the parser", "context": "something else"},
    )

    assert "does not match what was planned" in response.error.message
    assert response.status == "pending"
    # The same token still works.
    assert (await service.run(plan.review_id, plan.plan.confirm_token)).error is None


async def test_send_as_is_retry_requires_and_rechecks_the_original_material(build):
    secret = "sk-ant-api03-AAAABBBBCCCCDDDDEEEE"
    broken = StubAdapter(error=AdapterError(ConsultErrorCode.TIMEOUT, "slow"))
    service = await build({"codex-sol": broken, "gemini-x": StubAdapter()})
    plan = await planned(service, context=f"KEY={secret}")
    raw = {"goal": "review the parser", "context": f"KEY={secret}"}
    await service.run(
        plan.review_id,
        plan.plan.confirm_token,
        secrets="send_as_is",
        raw=raw,
    )
    broken._error = None

    refused = await service.retry(plan.review_id)
    assert "send_as_is" in refused.error.message
    assert refused.status == "failed"

    retried = await service.retry(plan.review_id, raw=raw)
    assert retried.error is None
    assert secret in broken.prompts[-1]


# --- partial and failed reviews ---------------------------------------------


async def test_one_reviewer_offline_still_yields_the_others_findings(build):
    adapters = {
        "codex-sol": StubAdapter(),
        "gemini-x": StubAdapter(
            status=AgentStatus("gemini-x", installed=False, authenticated=False, detail="missing")
        ),
    }
    service = await build(adapters)
    plan = await planned(service, mode="deep")
    response = await service.run(plan.review_id, plan.plan.confirm_token, host_findings=["mine"])

    assert response.status == "awaiting_synthesis" and response.outcome == "some"
    by_agent = {r.agent_id: r for r in response.results}
    assert by_agent["codex-sol"].findings[0].severity == "critical"
    assert by_agent["gemini-x"].ok is False and by_agent["gemini-x"].findings == []


async def test_every_reviewer_failing_leaves_a_review_that_can_be_retried(build):
    """A review where everyone was offline is exactly the one worth running again,
    so `failed` is not terminal."""
    adapters = {
        aid: StubAdapter(status=AgentStatus(aid, installed=False, authenticated=False))
        for aid in REVIEWERS
    }
    service = await build(adapters)
    plan = await planned(service)
    response = await service.run(plan.review_id, plan.plan.confirm_token)

    assert response.status == "failed" and response.outcome == "none"
    assert all(not r.findings for r in response.results)


async def test_a_retry_of_one_failure_does_not_demote_the_review_to_none(build):
    """The outcome is recomputed over every persisted reviewer, never over the batch
    that just ran. Counting the batch would throw away a real finding set."""
    working = StubAdapter()
    broken = StubAdapter(error=AdapterError(ConsultErrorCode.TIMEOUT, "slow"))
    service = await build({"codex-sol": working, "gemini-x": broken})
    plan = await planned(service, mode="deep")
    await service.run(plan.review_id, plan.plan.confirm_token, host_findings=["mine"])

    again = await service.retry(plan.review_id)

    assert again.outcome == "some"
    assert again.status == "awaiting_synthesis"
    assert {r.agent_id for r in again.results} == set(REVIEWERS)


async def test_a_retry_only_re_asks_the_reviewers_that_failed(build):
    working = StubAdapter()
    broken = StubAdapter(error=AdapterError(ConsultErrorCode.TIMEOUT, "slow"))
    service = await build({"codex-sol": working, "gemini-x": broken})
    plan = await planned(service, mode="deep")
    await service.run(plan.review_id, plan.plan.confirm_token, host_findings=["mine"])
    asked = len(working.prompts)

    await service.retry(plan.review_id)
    assert len(working.prompts) == asked


async def test_a_retry_reuses_the_failed_reviewers_consultation(build):
    """Another turn in the same consultation, so no orphan is left where the delete
    cannot find it."""
    broken = StubAdapter(error=AdapterError(ConsultErrorCode.TIMEOUT, "slow"))
    service = await build({"codex-sol": broken, "gemini-x": StubAdapter()})
    plan = await planned(service)
    first = await service.run(plan.review_id, plan.plan.confirm_token)
    before = first.results[0].consultation_id

    again = await service.retry(plan.review_id)
    assert before is not None and again.results[0].consultation_id == before


async def test_retrying_a_review_where_everyone_answered_is_refused(build):
    service = await build()
    plan = await planned(service)
    await service.run(plan.review_id, plan.plan.confirm_token)
    assert "nothing to retry" in (await service.retry(plan.review_id)).error.message


async def test_a_reviewer_with_no_row_at_all_is_still_asked_again(build):
    """A review planned before reviewers were reserved can have a reviewer with no
    row. Reading only the rows is what made that reviewer stop having been asked."""
    service = await build()
    plan = await planned(service, mode="deep")
    await service.run(plan.review_id, plan.plan.confirm_token, host_findings=["mine"])
    await sql(service, "DELETE FROM review_consultations WHERE agent_id = 'gemini-x'")

    # Failed rather than omitted, so the review does not read as full coverage.
    got = await service.get(plan.review_id)
    missing = {r.agent_id: r for r in got.results}["gemini-x"]
    assert missing.ok is False and missing.error.code is ConsultErrorCode.NOT_STARTED

    again = await service.retry(plan.review_id)
    assert again.error is None, again.error
    assert {r.agent_id: r.ok for r in again.results} == {"codex-sol": True, "gemini-x": True}


async def test_a_cancel_during_the_reservation_still_sends_nothing(build, monkeypatch):
    """`cancel` does not take the execution lease, so every await between the status
    check and `create_task` is a window where a cancelled review still sends."""
    service = await build()
    reserve = service.store.reserve_reviewers

    async def cancel_midway(review_id, agent_ids):
        await reserve(review_id, agent_ids)
        await service.store.transition(review_id, "cancelled", ("running",))

    monkeypatch.setattr(service.store, "reserve_reviewers", cancel_midway)

    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)

    assert run.error is not None
    assert all(a.prompts == [] for a in service.adapters.values())
    assert (await service.store.get_review(plan.review_id)).status == "cancelled"


async def test_a_stored_citation_this_build_cannot_read_costs_one_citation(build):
    """Reconstruction runs inside every read of a review. One unreadable source must
    not make the whole review unreadable and unfinalizable."""
    kept = ConsultSource(title="CVE-1", locator="https://example.test/1", source_type="web")
    service = await build({aid: StubAdapter(sources=[kept]) for aid in REVIEWERS})
    plan = await planned(service)
    await service.run(plan.review_id, plan.plan.confirm_token)
    await sql(
        service,
        "UPDATE review_consultations SET sources_json = ?",
        json.dumps([{"from": "a contract this build does not have"}, kept.model_dump(mode="json")]),
    )

    got = await service.get(plan.review_id)
    assert [s.locator for r in got.results for s in r.sources] == ["https://example.test/1"]


async def test_a_reviewer_that_dies_before_recording_still_counts_against_the_review(
    build, monkeypatch
):
    """A reviewer is read back from its row, so one that never wrote a row used to
    vanish -- and a review missing a reviewer settled `all` and could be certified."""
    service = await build()
    original = service.store.record_reviewer_result

    async def die_for_one(review_id, agent_id, *args, **kwargs):
        if agent_id == "gemini-x":
            raise RuntimeError("the write died")
        return await original(review_id, agent_id, *args, **kwargs)

    monkeypatch.setattr(service.store, "record_reviewer_result", die_for_one)

    plan = await planned(service, mode="deep")
    run = await service.run(
        plan.review_id, plan.plan.confirm_token, host_findings=["the read is unbounded"]
    )

    assert run.error is None, run.error
    assert run.outcome == "some"
    assert {r.agent_id: r.ok for r in run.results} == {"codex-sol": True, "gemini-x": False}


async def test_a_crash_after_the_token_is_spent_does_not_leave_the_review_running(build):
    """The token is gone and the status says `running`, but the lease is released and
    no reviewer exists. Left that way the review refuses to be deleted -- "still
    running; cancel it first" -- for reviewers that were never started."""
    service = await build()

    async def boom(*args, **kwargs):
        raise RuntimeError("the database went away")

    service.store.reserve_reviewers = boom  # after `consume_confirm_token`, before any task
    plan = await planned(service)
    response = await service.run(plan.review_id, plan.plan.confirm_token)

    assert response.error is not None
    assert (await service.store.get_review(plan.review_id)).status == "failed"


async def test_a_cancel_that_won_the_race_is_not_overwritten_by_that_failure(build):
    """The transition is guarded by `allowed_from`, so a review that a cancel already
    moved stays cancelled: the failure path reports its own crash, never someone
    else's state."""
    service = await build()
    plan = await planned(service)

    async def cancel_then_fail(*args, **kwargs):
        await service.store.transition(plan.review_id, "cancelled", ("running",))
        raise RuntimeError("the database went away")

    service.store.reserve_reviewers = cancel_then_fail
    response = await service.run(plan.review_id, plan.plan.confirm_token)

    assert response.error is not None
    assert (await service.store.get_review(plan.review_id)).status == "cancelled"


# --- cancellation -----------------------------------------------------------


async def test_a_cancel_is_not_overwritten_by_the_batch_finishing(build):
    entered = asyncio.Event()
    release = asyncio.Event()
    adapters = {
        "codex-sol": StubAdapter(entered=entered, release=release),
        "gemini-x": StubAdapter(),
    }
    service = await build(adapters)
    plan = await planned(service)
    running = asyncio.create_task(service.run(plan.review_id, plan.plan.confirm_token))
    await entered.wait()

    cancelling = asyncio.create_task(service.cancel(plan.review_id))
    await asyncio.sleep(0)
    assert not cancelling.done()
    release.set()
    cancelled = await cancelling
    finished = await running

    assert cancelled.status == "cancelled"
    assert finished.status == "cancelled"
    assert (await service.store.get_review(plan.review_id)).status == "cancelled"


async def test_a_reviewer_that_answered_before_the_cancel_keeps_its_answer(build):
    adapters = {aid: StubAdapter() for aid in REVIEWERS}
    service = await build(adapters)
    plan = await planned(service)
    await service.run(plan.review_id, plan.plan.confirm_token)

    cancelled = await service.cancel(plan.review_id)
    assert cancelled.results[0].ok is True


async def test_a_completed_review_cannot_be_cancelled(build):
    service = await build()
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    await service.finalize(plan.review_id, _synthesis(run))

    assert "cannot be cancelled" in (await service.cancel(plan.review_id)).error.message


# --- synthesis --------------------------------------------------------------


def _synthesis(run, **overrides):
    """A summary that accounts for every Critical, which is the baseline the
    check demands."""
    critical = [f for r in run.results for f in r.findings if f.severity == "critical"]
    return {
        "summary": "one real problem",
        "recommendation": "stream the file",
        "combined_findings": [
            {
                "problem": f.why,
                "severity": "critical",
                "agreed_by": [f.agent_id],
                "source_finding_ids": [f.finding_id],
                "proposed_action": f.fix,
            }
            for f in critical
        ],
        "checked": ["the parser"],
        "not_checked": ["everything else"],
        **overrides,
    }


async def test_finalizing_is_the_only_thing_that_completes_a_review(build):
    service = await build()
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    assert run.status == "awaiting_synthesis"

    done = await service.finalize(plan.review_id, _synthesis(run))
    assert done.status == "complete" and done.summary.recommendation == "stream the file"


async def test_a_summary_that_drops_a_lone_critical_is_refused(build):
    service = await build()
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)

    response = await service.finalize(plan.review_id, _synthesis(run, combined_findings=[]))
    assert "Critical" in response.error.message
    assert (await service.store.get_review(plan.review_id)).status == "awaiting_synthesis"


async def test_a_summary_with_an_invented_source_finding_is_refused(build):
    service = await build()
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    invented = {
        "problem": "invented",
        "severity": "minor",
        "agreed_by": ["codex-sol"],
        "source_finding_ids": ["ghost-1"],
    }
    response = await service.finalize(
        plan.review_id,
        _synthesis(run, combined_findings=[*_synthesis(run)["combined_findings"], invented]),
    )
    assert "ghost-1" in response.error.message


async def test_unparsed_reviewer_findings_make_finalization_refuse(build):
    service = await build({"codex-sol": StubAdapter(answer="prose only"), "gemini-x": StubAdapter()})
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)

    response = await service.finalize(plan.review_id, _synthesis(run))
    assert "unparsed findings" in response.error.message
    assert response.unparsed_reviewers == ["codex-sol"]
    assert response.status == "awaiting_synthesis"


class SecondTurnAdapter(StubAdapter):
    """Prose on the first turn, then whatever it is told to send on the second."""

    def __init__(self, second: str) -> None:
        super().__init__()
        self._second = second

    async def _answer(self, prompt, source_mode):
        # `prompts` records this call inside `super()`, so an empty list is turn 1.
        self.answer = "prose only, no block" if not self.prompts else self._second
        return await super()._answer(prompt, source_mode)


async def test_an_unparsed_block_is_asked_for_once_more_in_the_same_session(build):
    stub = SecondTurnAdapter('```json\n{"findings": []}\n```')
    service = await build({"codex-sol": stub, "gemini-x": StubAdapter()})
    plan = await planned(service, context="def parse(): ...")
    run = await service.run(plan.review_id, plan.plan.confirm_token)

    result = next(r for r in run.results if r.agent_id == "codex-sol")
    assert result.findings_parsed and result.findings == []
    # Turn 1's prose is the review; only the structure came from turn 2.
    assert result.answer == "prose only, no block"
    assert run.unparsed_reviewers == []
    # One re-ask, and it carried no second copy of the material.
    assert len(stub.prompts) == 2
    assert "def parse(): ..." not in stub.prompts[1]
    record = await service.consult.get_consultation(result.consultation_id)
    assert len(record.turns) == 2


async def test_a_reviewer_that_will_not_send_a_block_is_asked_once_and_reported(build):
    stub = StubAdapter(answer="prose only, no block")
    service = await build({"codex-sol": stub, "gemini-x": StubAdapter()})
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)

    assert run.unparsed_reviewers == ["codex-sol"]
    assert len(stub.prompts) == 2  # one retry, not a loop


async def test_empty_but_parseable_findings_can_be_finalized(build):
    empty = 'nothing found\n```json\n{"findings": []}\n```'
    service = await build({"codex-sol": StubAdapter(answer=empty), "gemini-x": StubAdapter()})
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)

    done = await service.finalize(plan.review_id, _synthesis(run))
    assert done.status == "complete"


async def test_content_free_storage_cannot_certify_an_empty_finding_set(build):
    service = await build(store_full_content=False)
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)

    response = await service.finalize(plan.review_id, _synthesis(run))
    assert "store_full_content" in response.error.message
    assert response.status == "awaiting_synthesis"


async def test_finalizing_a_review_that_is_not_waiting_on_synthesis_is_refused(build):
    service = await build()
    plan = await planned(service)
    response = await service.finalize(plan.review_id, _synthesis(plan))

    assert response.error is not None
    assert response.status == "pending"
    assert (await service.store.get_review(plan.review_id)).summary_json is None


async def test_finalizing_twice_is_refused(build):
    service = await build()
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    await service.finalize(plan.review_id, _synthesis(run))

    again = await service.finalize(plan.review_id, _synthesis(run))
    assert again.error is not None and again.status == "complete"


async def test_a_web_reviewers_citations_reach_the_summary(build):
    """Most of what made a web-mode answer checkable is its sources. Losing them in
    synthesis is losing the answer."""
    service = await build()
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    done = await service.finalize(
        plan.review_id,
        _synthesis(
            run,
            citations=[
                {"title": "CVE-1", "locator": "https://example.test/1", "source_type": "web"}
            ],
        ),
    )
    assert done.summary.citations[0].title == "CVE-1"


async def test_a_reviewers_own_citations_survive_a_later_finalize(build):
    """Finalization rebuilds every result from the rows, so a summary written in a
    separate call carries the reviewer's sources only if the row kept them."""
    cited = ConsultSource(title="CVE-1", locator="https://example.test/1", source_type="web")
    service = await build({"codex-sol": StubAdapter(sources=[cited]), "gemini-x": StubAdapter()})
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    assert run.results[0].sources == [cited]

    done = await service.finalize(plan.review_id, _synthesis(run))
    assert [(s.title, s.locator) for s in done.summary.citations] == [
        ("CVE-1", "https://example.test/1")
    ]


# --- reading back -----------------------------------------------------------


async def test_get_returns_the_review_with_its_results_and_synthesis(build):
    service = await build()
    plan = await planned(service)
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    await service.finalize(plan.review_id, _synthesis(run))

    stored = await service.get(plan.review_id)
    assert stored.status == "complete"
    assert stored.summary.summary == "one real problem"
    assert stored.results[0].findings[0].agent_id == "codex-sol"


async def test_get_on_an_unknown_review_is_an_envelope_not_an_exception(build):
    service = await build()
    response = await service.get(uuid.uuid4())
    assert response.error.code == ConsultErrorCode.SESSION_NOT_FOUND


async def test_list_reports_metadata_and_no_material(build):
    service = await build()
    plan = await planned(service, context="secret-ish material")
    rows = await service.list()

    assert rows[0].review_id == str(plan.review_id)
    assert rows[0].reviewers == ["codex-sol"]
    assert "material" not in rows[0].model_dump_json()


async def test_testing_the_reviewers_preflights_only_them(build):
    """With `check=True` every row costs a subprocess, so narrowing afterwards would
    preflight agents nobody asked about."""
    service = await build(
        adapters={aid: StubAdapter() for aid in [*REVIEWERS, "spare"]},
        agents={**REVIEWERS, "spare": agent("codex", "unused")},
    )
    response = await service.test_reviewers(mode="standard")
    assert [a.agent_id for a in response.agents] == ["codex-sol"]


# --- deletion through the service ------------------------------------------


async def test_a_recheck_is_deleted_with_its_parent(build):
    service = await build()
    parent = await planned(service)
    await service.run(parent.review_id, parent.plan.confirm_token)
    recheck = await planned(service, parent_review_id=parent.review_id)
    await service.run(recheck.review_id, recheck.plan.confirm_token)

    assert await service.delete(parent.review_id) == 2
    assert (await service.get(recheck.review_id)).error is not None


async def test_delete_all_needs_the_count_it_reported(build):
    """An omitted argument must never mean "erase all history"."""
    service = await build()
    await planned(service)
    token, count = await service.request_delete_all()

    assert count == 1
    assert await service.delete_all(token) == 1
    assert await service.list() == []


# --- one store for both layers ----------------------------------------------


async def test_the_review_and_its_consultations_share_one_connection(build, tmp_path):
    """Two connections to one file would be two writers where the design assumes
    one, and a delete could not remove both halves in a single transaction."""
    store = ConsultStore(tmp_path / "shared.sqlite3", store_full_content=True)
    service = await build(store=store)
    assert service.consult.store is store
    assert service.store.store is store
