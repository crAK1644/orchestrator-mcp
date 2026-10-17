"""What a contained `isolated_write` run spends, and everything that has to see it.

A contained step never comes through the consult path, and nothing used to write its
run down: the workflow's rollup, its ceilings and the reports all read the turn ledger,
so a workflow whose implement, fix and test steps ran contained was bounded by none of
its own ceilings and reported as having cost nothing. Each run is now a turn of a
consultation its step owns. What these check is that every reader of the ledger counts
it without knowing it was contained, and that its rows go with the workflow.

Offline. A run is the stub `codex` from `fixtures.agent_stub` -- a real executable
writing into a real worktree -- or, where a test needs a price no codex stream carries,
`run_contained` replaced at the workflow's own seam.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing

import pytest

from orchestrator_mcp.code.adapters.base import CodeResult, ObservedCommand
from orchestrator_mcp.consult.contract import Usage
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.store import StoreError
from orchestrator_mcp.reports import connect, strengths, usage

from .fixtures import agent_stub
from .test_consult_dashboard import config, serve  # noqa: F401 -- fixtures
from .test_workflow_service import (  # noqa: F401 -- fixtures
    AGENTS,
    BRIEF,
    FINDINGS,
    HOST_BINDINGS,
    PLAN,
    RESEARCH,
    StubAdapter,
    StubWorkflow,
    _spent,
    _to_testing,
    build,
    host_step,
    repo,
    sql,
    started,
    step,
    workflow_agent,
)

CONTAINED = {
    **AGENTS,
    "codex-sol": workflow_agent(
        "codex", "gpt-5.6-sol", 10, execution_modes=["consultation", "patch", "isolated_write"]
    ),
}

# What the stub writes into its worktree, and what its stream says the run cost.
WROTE = {"src/b.py": "def scan():\n    return 2\n"}
SPENT = {"input_tokens": 400, "output_tokens": 90}


def stream(text: str = "Split the scanner out into src/b.py.", usage: dict | None = SPENT) -> str:
    events = [
        {"type": "thread.started", "thread_id": "th-1", "model": "gpt-5.6-sol"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
    ]
    if usage is not None:
        events.append({"type": "turn.completed", "usage": usage})
    return "".join(json.dumps(event) + "\n" for event in events)


async def contained(build, *steps: str, **overrides):
    """A service whose `steps` run contained on codex-sol, every other step the host's."""
    return await build(
        bindings={name: {"agent": "codex-sol", "execution": "isolated_write"} for name in steps},
        agents=CONTAINED,
        **overrides,
    )


async def to_implement(service, repo) -> str:
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    return workflow_id


async def run(service, workflow_id: str, name: str):
    """Plan and run one step: `(step_id, response)`."""
    step_id, token = await step(service, workflow_id, name)
    return step_id, await service.run_step(workflow_id, step_id, token)


async def turns(service, workflow_id: str) -> list[dict]:
    """Every turn behind this workflow's own consultations, with what owns it."""
    rows = await sql(
        service,
        "SELECT c.step_id, c.capability, c.target_agent_id, c.conversation_label, t.* "
        "FROM consultation_turns t JOIN consultations c ON c.id = t.consultation_id "
        "WHERE c.workflow_id = ? ORDER BY t.id",
        workflow_id,
    )
    return [dict(row) for row in rows.fetchall()]


def priced(monkeypatch, cost_usd: float, counts_incomplete: list[str] | None = None) -> None:
    """A contained test run that reports a price, which no codex stream does."""

    async def fake(config, agent, prompt, *, baseline_commit, **_):
        return CodeResult(
            baseline_commit=baseline_commit,
            patch="",
            files=[],
            summary="Ran the suite.",
            commands=[ObservedCommand("pytest -q", 0, "12 passed")],
            raw_output="{}",
            usage=Usage(
                prompt_tokens=1000, completion_tokens=200, total_tokens=1200, cost_usd=cost_usd,
                counts_incomplete=counts_incomplete or [],
            ),
        )

    monkeypatch.setattr("orchestrator_mcp.workflow.service.run_contained", fake)


# --- the run is a turn ----------------------------------------------------------


async def test_a_contained_run_is_counted_in_what_the_workflow_spent(
    build, repo, tmp_path, monkeypatch
):
    """The rollup, the status view and the run's own response all read one ledger."""
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stream(), "append": WROTE}])
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)

    step_id, response = await run(service, workflow_id, "implement")

    assert response.error is None, response.error
    assert response.status == "awaiting_host_apply"
    spend = await service.consult.store.workflow_usage(workflow_id)
    assert spend[step_id].turns == 1
    assert spend[step_id].usage.total_tokens == 490
    total, steps = await _spent(service, workflow_id)
    assert (steps["implement"].prompt_tokens, steps["implement"].completion_tokens) == (400, 90)
    assert total.total_tokens == 490
    # Codex names no price, so the cost is unknown -- never a measured zero.
    assert total.cost_usd is None
    assert response.step is not None and response.step.usage.total_tokens == 490
    # And still not through the consult path: the run is recorded, not consulted.
    assert service.adapters["codex-sol"].prompts == []


async def test_the_turn_is_owned_by_the_step_and_holds_what_was_sent_and_said(
    build, repo, tmp_path, monkeypatch
):
    agent_stub.install(
        "codex", tmp_path, monkeypatch,
        runs=[{"stdout": stream("Done. I used sk-proj-a1B2c3D4e5F6g7H8i9J0k1L2 to check."),
               "append": WROTE}],
    )
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)

    step_id, response = await run(service, workflow_id, "implement")

    assert response.error is None, response.error
    [turn] = await turns(service, workflow_id)
    assert turn["step_id"] == step_id
    assert (turn["capability"], turn["target_agent_id"]) == ("coding", "codex-sol")
    assert turn["conversation_label"] == f"implement ({workflow_id[:8]})"
    assert turn["source_mode"] == "document"
    assert turn["error_code"] is None
    assert turn["compiled_prompt"].startswith("You are an execution endpoint")
    assert "split the scanner out" in turn["user_prompt"]
    # The run's own stream is kept, as a consultation's raw output is -- masked.
    assert "agent_message" in turn["raw_output"]
    assert "sk-proj-a1B2c3D4e5F6g7H8i9J0k1L2" not in json.dumps(turn)
    assert "[redacted]" in turn["raw_output"]


async def test_store_full_content_false_keeps_the_counts_and_drops_the_bodies(
    build, repo, tmp_path, monkeypatch
):
    """Config refuses a workflow without full content, so this is held at the store: the
    point is only that a contained turn is written by the same rule as every other."""
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stream(), "append": WROTE}])
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)
    service.consult.store.store_full_content = False

    _, response = await run(service, workflow_id, "implement")

    assert response.error is None, response.error
    [turn] = await turns(service, workflow_id)
    assert (turn["input_tokens"], turn["output_tokens"], turn["total_tokens"]) == (400, 90, 490)
    for body in ("user_prompt", "context", "compiled_prompt", "raw_output",
                 "validated_response_json"):
        assert turn[body] is None, body


async def test_what_a_run_said_about_its_own_count_is_kept(build, repo, monkeypatch):
    """A count this server had to invent reads like a measured one unless it says so,
    and the turn row is where whoever reads the workflow later finds out."""
    note = "codex reported its input as 'sk-proj-a1B2c3D4e5F6g7H8i9J0k1L2'; counted as 0"
    priced(monkeypatch, cost_usd=0.5, counts_incomplete=[note])
    service = await contained(build, "test")
    workflow_id = await _to_testing(service, repo)

    _, response = await run(service, workflow_id, "test")

    assert response.error is None, response.error
    [turn] = await turns(service, workflow_id)
    [stored] = json.loads(turn["counts_incomplete"])
    assert stored.startswith("codex reported its input as") and "sk-proj" not in stored
    _, steps = await _spent(service, workflow_id)
    assert steps["test"].counts_incomplete == [stored]


# --- a run that did not succeed -------------------------------------------------


async def test_a_failed_run_is_a_turn_with_its_code_and_no_price(
    build, repo, tmp_path, monkeypatch
):
    """As a delegated turn that failed is: counted, coded, and unpriced.

    The CLI's count of a run that failed is never read, so it has no tokens and an
    unknown cost rather than a zero one. It still counts towards the turn ceiling,
    which is what bounds a workflow whose runs keep failing.
    """
    record = agent_stub.install(
        "codex", tmp_path, monkeypatch,
        runs=[{"stdout": stream(), "stderr": "rate limited", "returncode": 1, "append": WROTE}],
    )
    service = await contained(build, "implement", spend={"max_turns_per_workflow": 1})
    workflow_id = await to_implement(service, repo)

    step_id, response = await run(service, workflow_id, "implement")

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert response.status == "coding"
    [turn] = await turns(service, workflow_id)
    assert turn["error_code"] == "agent_unavailable"
    assert (turn["input_tokens"], turn["output_tokens"], turn["cost_usd"]) == (0, 0, None)
    spend = await service.consult.store.workflow_usage(workflow_id)
    assert spend[step_id].turns == 1 and spend[step_id].usage.cost_usd is None

    _, again = await run(service, workflow_id, "implement")

    assert again.error is not None
    assert again.error.code is ConsultErrorCode.SPEND_LIMIT_REACHED
    assert "1 of its 1 turn ceiling" in again.error.message
    assert len(agent_stub.calls(record)) == 1


async def test_a_run_whose_diff_could_not_be_read_keeps_what_it_spent(
    build, repo, tmp_path, monkeypatch
):
    """The run finished and reported its count before capture failed; dropping that
    would record the most expensive failure there is as unmeasured."""
    agent_stub.install(
        "codex", tmp_path, monkeypatch,
        runs=[{
            "stdout": stream(),
            "append": {
                **WROTE,
                # Enough for git to call `sub` a repository and refuse the whole tree.
                "sub/.git/HEAD": "ref: refs/heads/main\n",
                "sub/.git/objects/.keep": "",
                "sub/.git/refs/.keep": "",
            },
        }],
    )
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)

    _, response = await run(service, workflow_id, "implement")

    assert response.error is not None
    assert "only copy of this step's work" in response.error.message
    [turn] = await turns(service, workflow_id)
    assert turn["error_code"] == "transport_error"
    assert (turn["input_tokens"], turn["output_tokens"]) == (400, 90)


async def test_a_run_that_failed_in_a_way_nobody_anticipated_is_still_a_turn(
    build, repo, monkeypatch
):
    """Recorded under the code its envelope carries, so the two cannot disagree."""

    async def broken(*args, **kwargs):
        raise RuntimeError("the worktree vanished")

    monkeypatch.setattr("orchestrator_mcp.workflow.service.run_contained", broken)
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)

    _, response = await run(service, workflow_id, "implement")

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.TRANSPORT_ERROR
    assert "the worktree vanished" in response.error.message
    [turn] = await turns(service, workflow_id)
    assert turn["error_code"] == "transport_error" and turn["cost_usd"] is None


async def test_a_run_that_changed_nothing_still_spent_what_it_spent(
    build, repo, tmp_path, monkeypatch
):
    """The step fails and the run does not: the CLI finished and answered, so its turn
    is recorded as an answer, as a delegated step whose reply could not be parsed is."""
    agent_stub.install(
        "codex", tmp_path, monkeypatch, runs=[{"stdout": stream("I could not find it.")}]
    )
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)

    step_id, response = await run(service, workflow_id, "implement")

    assert response.error is not None and "changed no files" in response.error.message
    [turn] = await turns(service, workflow_id)
    assert turn["error_code"] is None and turn["total_tokens"] == 490
    assert (await service.consult.store.workflow_usage(workflow_id))[step_id].turns == 1


# --- the ceilings ---------------------------------------------------------------


async def test_a_contained_run_counts_towards_the_workflows_turn_ceiling(
    build, repo, tmp_path, monkeypatch
):
    """Codex names no price, so for a workflow whose runs are contained codex runs the
    turn ceiling is the bound -- and it used to count none of them."""
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": "".join(
        json.dumps(event) + "\n" for event in (
            {"type": "thread.started", "thread_id": "th-1", "model": "gpt-5.6-sol"},
            {"type": "item.completed", "item": {
                "type": "command_execution", "command": ["pytest", "-q"],
                "exit_code": 0, "aggregated_output": "12 passed",
            }},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Ran it."}},
        )
    )}])
    service = await contained(build, "test", spend={"max_turns_per_workflow": 1})
    workflow_id = await _to_testing(service, repo)
    _, tested = await run(service, workflow_id, "test")
    assert tested.error is None, tested.error
    assert tested.status == "reviewing"
    service.adapters["codex-sol"].answers = [FINDINGS]
    review_id, token = await step(service, workflow_id, "review")

    response = await service.run_step(workflow_id, review_id, token)

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.SPEND_LIMIT_REACHED
    assert "1 of its 1 turn ceiling" in response.error.message
    assert service.adapters["codex-sol"].prompts == []
    # Refused before the token was spent, so the same token opens it once raised.
    service.config.spend.max_turns_per_workflow = 10
    assert (await service.run_step(workflow_id, review_id, token)).error is None


async def test_a_contained_run_counts_towards_the_workflows_dollar_ceiling(
    build, repo, monkeypatch
):
    priced(monkeypatch, cost_usd=2.0)
    service = await contained(build, "test", spend={"max_cost_usd_per_workflow": 1.0})
    workflow_id = await _to_testing(service, repo)
    _, tested = await run(service, workflow_id, "test")
    assert tested.error is None, tested.error
    total, steps = await _spent(service, workflow_id)
    assert steps["test"].cost_usd == pytest.approx(2.0) and total.cost_usd == pytest.approx(2.0)
    service.adapters["codex-sol"].answers = [FINDINGS]

    _, response = await run(service, workflow_id, "review")

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.SPEND_LIMIT_REACHED
    assert "$2.00 of its $1.00 ceiling" in response.error.message
    assert service.adapters["codex-sol"].prompts == []


# --- the reports and the estimate ----------------------------------------------


async def test_the_reports_count_a_contained_run_like_any_other_turn(
    build, repo, tmp_path, monkeypatch
):
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stream(), "append": WROTE}])
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)
    assert (await run(service, workflow_id, "implement"))[1].error is None

    with closing(connect(service.config.database_path)) as db:
        spent = usage(db)
        asked = strengths(db)

    [group] = spent["groups"]
    assert (group["agent_id"], group["turns"], group["total_tokens"]) == ("codex-sol", 1, 490)
    assert group["cost_usd"] is None
    [row] = asked["groups"]
    assert (row["capability"], row["agent_id"], row["asked"], row["answered"]) == (
        "coding", "codex-sol", 1, 1
    )


async def test_the_dashboard_shows_what_a_contained_run_spent(
    serve, config, repo, host_claude, tmp_path, monkeypatch
):
    """Read straight from the ledger like everything else on the page: the workflow's
    total and the step's row both count the run, and the step links its consultation."""
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stream(), "append": WROTE}])
    consult_config = config(
        agents=CONTAINED,
        store_full_content=True,
        host={"runtime": "claude", "model": "claude-opus-5"},
        review={"reviewers": ["codex-sol"], "deep_reviewers": list(CONTAINED)},
        workflow={
            "roots": [str(repo.parent)],
            "bindings": {
                **HOST_BINDINGS,
                "implement": {"agent": "codex-sol", "execution": "isolated_write"},
                "review": {"agents": ["codex-sol"]},
            },
        },
    )
    service = await StubWorkflow(
        consult_config, "claude", adapters={agent_id: StubAdapter() for agent_id in CONTAINED}
    ).open()
    try:
        workflow_id = await to_implement(service, repo)
        assert (await run(service, workflow_id, "implement"))[1].error is None
        [turn] = await turns(service, workflow_id)
    finally:
        await service.close()
    get, _ = serve(consult_config)

    _, listing = get("/workflows")
    _, page = get(f"/workflows/{workflow_id}")

    assert "490 tokens" in listing and "490 tokens" in page
    assert f"/consultation/{turn['consultation_id']}" in page
    assert get(f"/consultation/{turn['consultation_id']}")[0] == 200


async def test_a_contained_run_is_not_fitted_into_the_agents_estimate(
    build, repo, tmp_path, monkeypatch
):
    """A whole job sent a question-sized prompt: fitted beside the agent's answers, it
    would price its next question as one. The turn is still in the ledger."""
    agent_stub.install(
        "codex", tmp_path, monkeypatch,
        runs=[{"stdout": stream(usage={"input_tokens": 900_000, "output_tokens": 40_000}),
               "append": WROTE}],
    )
    service = await contained(build, "implement")
    service.adapters["codex-sol"].answers = [RESEARCH]
    workflow_id = await started(service, repo, bindings={"research": {"agent": "codex-sol"}})
    assert (await run(service, workflow_id, "research"))[1].error is None
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    implement_id, response = await run(service, workflow_id, "implement")
    assert response.error is None, response.error
    assert (await service.consult.store.workflow_usage(workflow_id))[implement_id].turns == 1

    history = await service.consult.store.turn_history("codex-sol", "gpt-5.6-sol", 50)

    # The research consultation's 10 in and 2 out, and nothing of the run's 900K.
    assert [(i, o) for _, i, o, _ in history] == [(10, 2)]


# --- deletion -------------------------------------------------------------------


async def test_deleting_the_workflow_takes_its_contained_runs_with_it(
    build, repo, tmp_path, monkeypatch
):
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stream(), "append": WROTE}])
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)
    assert (await run(service, workflow_id, "implement"))[1].error is None
    assert len(await turns(service, workflow_id)) == 1
    assert (await service.cancel(workflow_id)).error is None

    assert await service.delete(workflow_id) == 1

    for table in ("workflow_runs", "workflow_steps", "consultations", "consultation_turns",
                  "consultation_leases"):
        assert (await sql(service, f"SELECT COUNT(*) FROM {table}")).fetchone()[0] == 0, table


async def test_a_delete_after_a_cancel_waits_for_the_run_still_in_flight(
    build, repo, tmp_path, monkeypatch
):
    """Cancel marks a contained step and cannot stop its process, so the workflow is
    terminal while the run is still going. Deleted then, the run would come back to a
    workflow that is gone, with nowhere to write what it spent. Its consultation is
    leased for the run, as a delegated turn's is, so the delete waits on it instead."""
    record = agent_stub.install(
        "codex", tmp_path, monkeypatch,
        runs=[{"stdout": stream(), "append": WROTE, "sleep": 2}],
    )
    service = await contained(build, "implement")
    workflow_id = await to_implement(service, repo)
    step_id, token = await step(service, workflow_id, "implement")
    running = asyncio.create_task(service.run_step(workflow_id, step_id, token))
    for _ in range(200):
        if agent_stub.calls(record):
            break
        await asyncio.sleep(0.02)
    assert agent_stub.calls(record), "the run never started"
    assert (await service.cancel(workflow_id)).error is None

    with pytest.raises(StoreError, match="turn in flight") as caught:
        await service.delete(workflow_id)
    assert caught.value.code == ConsultErrorCode.SESSION_BUSY

    late = await running
    # The step was cancelled under the run, so its result is refused -- and what it
    # spent is still written down.
    assert late.error is not None and "no longer running" in late.error.message
    assert len(await turns(service, workflow_id)) == 1
    assert await service.delete(workflow_id) == 1
    for table in ("consultations", "consultation_turns", "workflow_steps"):
        assert (await sql(service, f"SELECT COUNT(*) FROM {table}")).fetchone()[0] == 0, table
