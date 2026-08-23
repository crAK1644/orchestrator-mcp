"""The workflow layer: routing, state, tokens, artifacts and the bypass it must refuse.

Offline throughout. The adapters are stubbed at `ConsultService.adapter`, the same
seam `test_review_service.py` uses, so everything below the bind -- the prompt, the
lease, the store, the envelope -- is the real code path.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

import pytest

from orchestrator_mcp.code.registry import CodeError
from orchestrator_mcp.code.service import patch_path
from orchestrator_mcp.consult.adapters.base import AdapterResult, AgentStatus
from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.contract import ConsultationContent, Usage
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.store import StoreError
from orchestrator_mcp.json_objects import MAX_JSON_CANDIDATES, json_object_candidates
from orchestrator_mcp.workflow.prompts import parse_reply
from orchestrator_mcp.workflow.service import WorkflowError, WorkflowService, _review_id
from orchestrator_mcp.workflow.store import _sha256

from .conftest import agent

RESEARCH = json.dumps(
    {"summary": "the parser is hand-rolled", "findings": ["one entry point"], "open_questions": []}
)
PLAN = json.dumps(
    {
        "summary": "rewrite the tokenizer",
        "changes": [{"path": "src/a.py", "intent": "split the scanner out"}],
        "order": ["src/a.py"],
        "validation_strategy": "pytest",
        "risks": ["none known"],
        "acceptance_criteria": ["tests pass"],
    }
)
BRIEF = json.dumps(
    {
        "objective": "split the scanner out",
        "constraints": ["no new dependencies"],
        "steps": ["edit src/a.py"],
        "done_when": ["tests pass"],
    }
)
PATCH = """--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-old
+new
"""
FINDINGS = """```json
{"findings": [{"severity": "important", "problem": "the scanner drops the last token",
  "evidence": "src/a.py:12", "suggested_fix": "advance before returning",
  "confidence": "high"}], "summary": "one real bug"}
```"""


def test_step_reply_uses_the_last_json_object_not_an_earlier_draft():
    final = json.loads(PLAN)
    final["summary"] = "the final plan"
    answer = f"First draft:\n{PLAN}\n\nFinal:\n```json\n{json.dumps(final)}\n```"

    parsed = parse_reply("plan", answer)

    assert parsed.summary == "the final plan"


def test_step_reply_ignores_inline_backticks_inside_the_final_json():
    final = json.loads(PLAN)
    final["summary"] = "use ```json {example: true} ``` safely"

    parsed = parse_reply("plan", f"```json\n{json.dumps(final)}\n```")

    assert parsed.summary.endswith("safely")


def test_json_candidates_are_bounded_and_keep_a_large_outer_final_answer():
    final = json.loads(PLAN)
    final["changes"] = [
        {"path": f"src/{index}.py", "intent": "change it"} for index in range(200)
    ]
    answer = "\n".join(["{}"] * 500 + [json.dumps(final)])

    candidates = list(json_object_candidates(answer))
    parsed = parse_reply("plan", answer)

    assert len(candidates) <= MAX_JSON_CANDIDATES
    assert len(parsed.changes) == 200


def test_json_candidates_recover_after_an_impossible_newline_in_prose():
    final = json.loads(PLAN)
    final["summary"] = "recovered"
    answer = 'Prose with a stray { and "unterminated quote\n' + json.dumps(final)

    assert parse_reply("plan", answer).summary == "recovered"


def test_json_candidates_keep_a_large_answer_before_trailing_object_noise():
    final = json.loads(PLAN)
    final["changes"] = [
        {"path": f"src/{index}.py", "intent": "change it"} for index in range(100)
    ]
    answer = json.dumps(final) + "\n" + "\n".join("{}" for _ in range(100))

    assert len(parse_reply("plan", answer).changes) == 100


def test_json_candidates_keep_a_shorter_corrected_answer_after_a_long_draft():
    draft = json.loads(PLAN)
    draft["summary"] = "old draft"
    draft["changes"] = [
        {"path": f"src/{index}.py", "intent": "draft change"} for index in range(40)
    ]
    final = json.loads(PLAN)
    final["summary"] = "corrected final"
    answer = "\n".join(
        [json.dumps(draft), json.dumps(final), *("{}" for _ in range(100))]
    )

    parsed = parse_reply("plan", answer)

    assert parsed.summary == "corrected final"


def test_step_reply_pairs_non_json_and_json_fences_independently():
    final = json.loads(PLAN)
    final["summary"] = "after shell"
    answer = f"```sh\necho '{{'\n```\n```json\n{json.dumps(final)}\n```"

    assert parse_reply("plan", answer).summary == "after shell"


def test_step_reply_accepts_a_compact_json_fence():
    final = json.loads(PLAN)
    final["summary"] = "compact"

    assert parse_reply("plan", f"```json {json.dumps(final)} ```").summary == "compact"


class StubAdapter:
    """A CLI that is installed, authenticated, and answers whatever it was handed."""

    def __init__(self, *answers: str, cost_usd: float | None = None) -> None:
        self.answers = list(answers) or ["{}"]
        self.prompts: list[str] = []
        self.modes: list[Any] = []
        # Unpriced by default, which is what a free tier reports and the harder case
        # for anything that adds these up.
        self.cost_usd = cost_usd

    def connect_command(self, agent):
        return ["true"]

    async def preflight(self, agent):
        return AgentStatus(agent.agent_id, installed=True, authenticated=True)

    async def start(self, agent, prompt, source_mode, session_id=None):
        return await self._answer(agent, prompt, source_mode)

    async def resume(self, agent, native_session_id, prompt, source_mode):
        return await self._answer(agent, prompt, source_mode)

    async def _answer(self, agent, prompt, source_mode) -> AdapterResult:
        self.prompts.append(prompt.full_text)
        self.modes.append(source_mode)
        answer = self.answers[min(len(self.prompts), len(self.answers)) - 1]
        return AdapterResult(
            content=ConsultationContent(
                answer=answer, assumptions=[], uncertainties=[],
                follow_up_questions=[], sources=[],
            ),
            native_session_id="native-1",
            model_used=agent.model,
            model_verified=True,
            raw_output="{}",
            usage=Usage(
                prompt_tokens=10, completion_tokens=2, total_tokens=12, cost_usd=self.cost_usd
            ),
        )


class StubWorkflow(WorkflowService):
    def __init__(self, *args, adapters: dict[str, StubAdapter], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.adapters = adapters
        self.consult.adapter = lambda agent: adapters[agent.agent_id]  # type: ignore[method-assign]


@pytest.fixture
def repo(tmp_path):
    """A real git repository with one commit, because the baseline is read with git."""
    path = tmp_path / "work"
    path.mkdir()
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(command, cwd=path, check=True)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("old\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=path, check=True)
    return path


# The four capabilities the workflow added. `conftest.agent` predates them, and a
# step scoring 0 is refused at config time, so every agent here has to say what it
# can do for the new steps as well.
WORKFLOW_SCORES = {
    "coding": 90, "research": 90, "writing": 90, "reasoning": 90, "review": 90,
    "planning": 80, "prompt_authoring": 70, "testing": 60, "synthesis": 75,
}


def workflow_agent(runtime: str, model: str, priority: int, **overrides) -> dict[str, Any]:
    # `scores` merged rather than passed through, so a caller zeroing one capability
    # gets the full table with that one hole rather than a `scores:` of one entry --
    # which would fail on some other capability and prove the wrong thing.
    return agent(
        runtime, model, priority, **{"scores": {**WORKFLOW_SCORES, **overrides.pop("scores", {})}, **overrides}
    )


AGENTS = {
    "codex-sol": workflow_agent(
        "codex", "gpt-5.6-sol", 10, execution_modes=["consultation", "patch"]
    ),
    "opus-agent": workflow_agent("claude", "claude-opus-4.9", 20),
    "flash": workflow_agent(
        "opencode", "deepseek-v4-flash-free", 30, execution_modes=["consultation", "patch"]
    ),
}

HOST_BINDINGS: dict[str, dict[str, Any]] = {
    step: {"executor": "host"}
    for step in ("research", "plan", "author_execution_prompt", "implement", "apply_patch",
                 "test", "synthesize", "fix")
}


@pytest.fixture
def build(tmp_path, repo, host_claude):
    async def make(
        adapters: dict[str, StubAdapter] | None = None,
        bindings: dict[str, dict[str, Any]] | None = None,
        agents: dict[str, dict] | None = None,
        spend: dict[str, Any] | None = None,
        **workflow_overrides,
    ):
        agents = dict(AGENTS if agents is None else agents)
        adapters = adapters or {aid: StubAdapter() for aid in agents}
        config = ConsultConfig(
            database_path=str(tmp_path / "c.sqlite3"),
            agents=agents,
            host={"runtime": "claude", "model": "claude-opus-5"},
            review={"reviewers": ["codex-sol"], "deep_reviewers": list(agents)},
            spend=spend or {},
            workflow={
                "roots": [str(repo.parent)],
                "bindings": {**HOST_BINDINGS, "review": {"agents": ["codex-sol"]},
                             **(bindings or {})},
                **workflow_overrides,
            },
        )
        return await StubWorkflow(config, "claude", adapters=adapters).open()

    return make


async def test_recovery_sweep_is_background_and_scheduled_only_once(
    build, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def sweep():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return 0, 0

    monkeypatch.setattr("orchestrator_mcp.workflow.service.sweep_recovery_artifacts", sweep)

    service = await asyncio.wait_for(build(), timeout=1)
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert await service.open() is service
    assert calls == 1
    release.set()
    await service._recovery_sweep_task
    await service.close()


async def test_recovery_sweep_failure_does_not_break_workflow_open(build, monkeypatch):
    attempted = asyncio.Event()

    async def fail():
        attempted.set()
        raise RuntimeError("broken abandoned worktree")

    monkeypatch.setattr("orchestrator_mcp.workflow.service.sweep_recovery_artifacts", fail)

    service = await build()
    await asyncio.wait_for(attempted.wait(), timeout=1)
    await service._recovery_sweep_task

    assert await service.open() is service
    await service.close()


async def started(service, repo, **overrides):
    response = await service.start(goal="split the scanner out", workdir=str(repo), **overrides)
    assert response.error is None, response.error
    return response.workflow_id


async def sql(service, statement: str, *params):
    return await service.store._run(lambda: service.store._db.execute(statement, params))


async def step(service, workflow_id: str, name: str):
    """Plan one step and hand back `(step_id, token)`."""
    response = await service.plan_step(workflow_id, name)
    assert response.error is None, response.error
    assert response.preview is not None
    return response.preview.step_id, response.preview.confirm_token


async def host_step(service, workflow_id: str, name: str, result: dict):
    step_id, token = await step(service, workflow_id, name)
    response = await service.record_host_step(workflow_id, step_id, token, result)
    assert response.error is None, response.error
    return response


# --- the bypass, first ------------------------------------------------------


@pytest.mark.parametrize("agent_id", ["codex-sol", "opus-agent"])
async def test_a_workflow_consultation_cannot_be_resumed_from_the_public_tool(
    build, repo, agent_id
):
    """The one thing that would undo the two bind paths.

    A workflow step's consultation was bound under execution-identity exclusion, so
    resuming it from `orchestrator_consult` would inherit that binding -- including,
    for `opus-agent`, an agent on the host's own runtime that the public tool refuses
    outright. Both are refused, and for the same reason.
    """
    service = await build(bindings={"research": {"agent": agent_id}})
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    service.adapters[agent_id].answers = [RESEARCH]
    assert (await service.run_step(workflow_id, step_id, token)).error is None

    rows = (await sql(service, "SELECT id, workflow_id, step_id FROM consultations")).fetchall()
    assert len(rows) == 1 and rows[0][1] == workflow_id and rows[0][2] == step_id

    response = await service.consult.consult(
        capability="research", prompt="something else", consultation_id=rows[0][0]
    )
    assert response.error is not None
    assert response.error.code == ConsultErrorCode.WORKFLOW_OWNED_SESSION
    # And nothing was sent: the refusal lands before the adapter.
    assert len(service.adapters[agent_id].prompts) == 1


async def test_no_public_tool_argument_reaches_the_private_path(build, repo):
    """`consult_step` takes a `StepSnapshot`, and `consult()` takes keywords."""
    service = await build()
    for kwargs in (
        {"capability": "research", "prompt": "x", "snapshot": {"workflow_id": "w"}},
        {"capability": "research", "prompt": "x", "workflow_id": "w"},
        {"capability": "research", "prompt": "x", "step_id": "s"},
    ):
        response = await service.consult.consult(**kwargs)
        assert response.error is not None, kwargs
        assert response.error.code == ConsultErrorCode.INVALID_REQUEST


# --- start ------------------------------------------------------------------


async def test_start_resolves_every_binding_and_records_the_baseline(build, repo):
    service = await build()
    response = await service.start(goal="split the scanner out", workdir=str(repo))
    assert response.error is None, response.error
    view = response.workflow
    assert view is not None and view.status == "created"
    assert view.workdir == str(repo.resolve())
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert view.baseline_commit == head
    # Every step, not only the ones about to run.
    assert set(view.bindings) == {
        "research", "plan", "author_execution_prompt", "implement", "apply_patch",
        "test", "review", "synthesize", "fix",
    }
    assert view.next_steps == ["research", "plan"]
    assert view.bindings["review"]["agents"][0]["agent_id"] == "codex-sol"


async def test_a_workdir_outside_every_root_is_refused(build, tmp_path, repo):
    service = await build()
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    response = await service.start(goal="g", workdir=str(outside))
    assert response.error is not None
    assert "not under any configured workflow root" in response.error.message


async def test_a_dirty_tree_needs_acknowledging(build, repo):
    service = await build()
    (repo / "src" / "a.py").write_text("dirty\n")
    response = await service.start(goal="g", workdir=str(repo))
    assert response.error is not None
    assert "uncommitted change" in response.error.message
    assert (await service.start(goal="g", workdir=str(repo), allow_dirty=True)).error is None


async def test_a_directory_that_is_not_a_repository_has_no_baseline(build, tmp_path, repo):
    service = await build()
    plain = repo.parent / "plain"
    plain.mkdir()
    response = await service.start(goal="g", workdir=str(plain))
    assert response.error is not None
    assert "not inside a git repository" in response.error.message


# --- the host path, end to end ----------------------------------------------


async def test_a_host_workflow_runs_the_whole_loop(build, repo):
    """Research to completion with the host doing everything but the review.

    The point is the state machine and the artifact chain, so every step here is
    host-recorded except the one step that cannot be: `review`.
    """
    service = await build()
    reviewer = service.adapters["codex-sol"]
    reviewer.answers = [FINDINGS]
    workflow_id = await started(service, repo)

    await host_step(service, workflow_id, "research", json.loads(RESEARCH))
    assert (await service.status(workflow_id)).status == "planning"
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    assert (await service.status(workflow_id)).status == "coding"

    # Host implementation lands in the tree, so it goes straight to testing.
    await host_step(
        service, workflow_id, "implement",
        {"summary": "split the scanner", "files": ["src/a.py"], "patch": PATCH},
    )
    assert (await service.status(workflow_id)).status == "testing"
    await host_step(
        service, workflow_id, "test",
        {"command": "pytest -q", "workdir": str(repo), "exit_code": 0, "status": "passed"},
    )
    assert (await service.status(workflow_id)).status == "reviewing"

    step_id, token = await step(service, workflow_id, "review")
    response = await service.run_step(workflow_id, step_id, token)
    assert response.error is None, response.error
    assert response.status == "synthesizing"

    ids = await finding_ids(service, workflow_id)
    final = await host_step(service, workflow_id, "synthesize", summary_with("fixed", ids))
    assert final.status == "completed", final.workflow.reason if final.workflow else None
    record = [s for s in final.workflow.steps if s.step == "synthesize"][0]
    assert record.output["loop_done"] is True and record.output["reasons"] == []
    assert record.output["review_id"]


async def test_a_failed_test_goes_back_to_fixing_unless_allowed(build, repo):
    for advance, expected in ((False, "fixing"), (True, "reviewing")):
        service = await build(advance_on_failed_test=advance)
        workflow_id = await started(service, repo)
        await host_step(service, workflow_id, "plan", json.loads(PLAN))
        await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
        await host_step(
            service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH}
        )
        await host_step(
            service, workflow_id, "test",
            {"command": "pytest", "workdir": str(repo), "exit_code": 1, "status": "failed"},
        )
        status = await service.status(workflow_id)
        assert status.status == expected
        assert status.workflow.fix_rounds == (0 if advance else 1)
        await service.close()


async def test_repeated_failed_tests_stop_at_the_fix_round_cap(build, repo):
    service = await build(max_fix_rounds=1)
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(
        service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH}
    )
    first = await host_step(
        service, workflow_id, "test",
        {"command": "pytest", "workdir": str(repo), "exit_code": 1, "status": "failed"},
    )
    assert first.status == "fixing" and first.workflow.fix_rounds == 1

    await host_step(service, workflow_id, "fix", {"summary": "s", "files": [], "patch": PATCH})
    stopped = await host_step(
        service, workflow_id, "test",
        {"command": "pytest", "workdir": str(repo), "exit_code": 1, "status": "failed"},
    )

    assert stopped.status == "needs_attention"
    assert "tests still fail" in (stopped.workflow.reason or "")


async def test_reported_by_is_assigned_by_the_service(build, repo):
    """A host claim cannot describe itself as an observed one."""
    service = await build()
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH})
    response = await host_step(
        service, workflow_id, "test",
        {
            "command": "pytest", "workdir": str(repo), "exit_code": 0, "status": "passed",
            "reported_by": "orchestrator", "commit": "0" * 40,
        },
    )
    report = [s for s in response.workflow.steps if s.step == "test"][0]
    assert report.output["reported_by"] == "host"
    assert report.reported_by == "host"
    # The commit is this process's record of what the workflow holds, not a claim the
    # host gets to make: a report naming some other commit would be a pass the next
    # round could not tell was stale.
    assert report.output["commit"] == response.workflow.baseline_commit


# --- steps, tokens and state ------------------------------------------------


async def test_a_step_cannot_be_planned_from_the_wrong_state(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    response = await service.plan_step(workflow_id, "test")
    assert response.error is not None
    assert "runs from `testing`" in response.error.message
    assert "`created`" in response.error.message


async def test_one_step_token_does_not_open_another_step(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    research_id, research_token = await step(service, workflow_id, "research")
    plan_id, _plan_token = await step(service, workflow_id, "plan")

    response = await service.record_host_step(
        workflow_id, plan_id, research_token, json.loads(PLAN)
    )
    assert response.error is not None
    assert response.error.code == ConsultErrorCode.INVALID_REQUEST


async def test_a_spent_token_cannot_be_spent_again(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    assert (
        await service.record_host_step(workflow_id, step_id, token, json.loads(RESEARCH))
    ).error is None
    again = await service.record_host_step(workflow_id, step_id, token, json.loads(RESEARCH))
    assert again.error is not None


async def test_a_step_planned_before_another_one_moved_the_workflow_will_not_run(build, repo):
    """`planned` is not enough, and the review that found this said why.

    Two steps can be previewed from one state. Once the first runs, the second's
    preview describes a workflow that no longer exists -- but its row is still
    `planned`, so it used to run, be paid for, and record an artifact for a state the
    workflow had left. The guarded transition afterwards then failed silently.
    """
    service = await build()
    workflow_id = await started(service, repo)
    first, first_token = await step(service, workflow_id, "research")
    stale, stale_token = await step(service, workflow_id, "research")

    assert (
        await service.record_host_step(workflow_id, first, first_token, json.loads(RESEARCH))
    ).error is None

    response = await service.record_host_step(
        workflow_id, stale, stale_token, json.loads(RESEARCH)
    )
    assert response.error is not None
    assert "`created`" in response.error.message and "`planning`" in response.error.message
    # Refused before the token, not after paying for the step: it can still be spent
    # on nothing, and the row is untouched.
    row = (
        await sql(service, "SELECT status, output_json FROM workflow_steps WHERE id = ?", stale)
    ).fetchone()
    assert row[0] == "planned"
    assert not row[1]


async def test_a_transition_that_lost_the_race_is_not_discarded(build, repo):
    """The backstop behind the check above, called directly because reaching it means
    winning a race the lease exists to prevent.

    `store.transition` guards the change in SQL and returns False when something else
    got there first. Every caller in the service threw that away, so a step could
    finish, be paid for and store its artifact while the workflow silently stayed put.
    """
    service = await build()
    workflow_id = await started(service, repo)
    with pytest.raises(WorkflowError, match="could not move to `reviewing`"):
        await service._transition(workflow_id, "reviewing", ("testing",))


async def test_a_step_id_from_another_workflow_is_refused(build, repo):
    service = await build()
    first = await started(service, repo)
    second = await started(service, repo)
    step_id, token = await step(service, first, "research")
    response = await service.record_host_step(second, step_id, token, json.loads(RESEARCH))
    assert response.error is not None
    assert f"belongs to workflow `{first}`" in response.error.message


async def test_a_delegated_step_refuses_the_host_tool_and_the_other_way_round(build, repo):
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    response = await service.record_host_step(workflow_id, step_id, token, json.loads(RESEARCH))
    assert response.error is not None and "is delegated" in response.error.message

    plan_id, plan_token = await step(service, workflow_id, "plan")
    response = await service.run_step(workflow_id, plan_id, plan_token)
    assert response.error is not None and "is the host's to do" in response.error.message


async def test_the_review_step_cannot_be_the_hosts(build, repo):
    """And it is refused at creation, before anything has been spent.

    It used to be refused only when the review step was planned -- after research,
    planning and implementation had all run and been paid for. Worse, unbound steps
    default to the host, so an operator who simply never mentioned `review` got a
    workflow that could not finish and no way to know until it got there.
    """
    service = await build(bindings={"review": {"executor": "host"}})
    response = await service.start(goal="split the scanner out", workdir=str(repo))
    assert response.error is not None
    assert "cannot be `executor: host`" in response.error.message
    assert not (await sql(service, "SELECT COUNT(*) FROM workflow_runs")).fetchone()[0]


async def test_repeated_rounds_reconstruct_in_order(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    first, token = await step(service, workflow_id, "research")
    assert (
        await service.record_host_step(workflow_id, first, token, json.loads(RESEARCH))
    ).error is None
    # A second research attempt in the same round: `plan` also runs from `planning`,
    # but research does not, so re-planning it is the honest way to get an attempt 2.
    await sql(service, "UPDATE workflow_runs SET status = 'created' WHERE id = ?", workflow_id)
    second, _ = await step(service, workflow_id, "research")

    rows = await service.store.steps(workflow_id)
    by_id = {row.id: row for row in rows}
    assert by_id[first].attempt == 1 and by_id[second].attempt == 2
    assert by_id[first].sequence < by_id[second].sequence


# --- delegated steps --------------------------------------------------------


async def test_a_delegated_step_stores_a_validated_artifact(build, repo):
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    service.adapters["codex-sol"].answers = [
        "Here is what I found.\n\n```json\n" + RESEARCH + "\n```"
    ]
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    response = await service.run_step(workflow_id, step_id, token)
    assert response.error is None, response.error
    assert response.status == "planning"
    assert response.step is not None
    assert response.step.output["summary"] == "the parser is hand-rolled"


async def test_an_unreadable_reply_fails_the_step_rather_than_storing_prose(build, repo):
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    service.adapters["codex-sol"].answers = ["I had a look and it seems fine."]
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    response = await service.run_step(workflow_id, step_id, token)
    assert response.error is not None
    assert response.error.code == ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    rows = await service.store.steps(workflow_id)
    assert [row.status for row in rows] == ["failed"]
    # The workflow did not advance on a failed step.
    assert (await service.status(workflow_id)).status == "created"


async def test_a_patch_comes_back_raw_and_is_recoverable_while_storage_stays_scrubbed(build, repo):
    """The database stays safe while a private recovery copy survives transport loss."""
    secret = "sk-ant-api03-" + "A" * 40
    raw = PATCH.replace("+new", f"+TOKEN = \"{secret}\"")
    service = await build(
        bindings={"implement": {"agent": "flash", "execution": "patch"}}
    )
    service.adapters["flash"].answers = [raw]
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))

    step_id, token = await step(service, workflow_id, "implement")
    response = await service.run_step(workflow_id, step_id, token)
    assert response.error is None, response.error
    assert response.patch == raw
    assert response.status == "awaiting_host_apply"

    stored = json.loads(
        (await sql(service, "SELECT output_json FROM workflow_steps WHERE id = ?", step_id))
        .fetchone()[0]
    )
    assert secret not in stored["patch"]
    assert secret not in json.dumps(stored)
    assert stored["raw_patch_sha256"] == _sha256(raw)
    assert stored["files"] == ["src/a.py"]
    assert patch_path(workflow_id, step_id).stat().st_mode & 0o777 == 0o600

    recovered = await service.status(workflow_id)
    assert recovered.patch == raw


@pytest.mark.parametrize(
    "failure",
    [
        CodeError(ConsultErrorCode.TRANSPORT_ERROR, "recovery disk is full"),
        OSError("recovery disk is full"),
        UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogate"),
    ],
    ids=["normalized", "raw-os-error", "raw-unicode-error"],
)
async def test_a_recovery_write_failure_still_returns_the_paid_patch(
    build, repo, monkeypatch, failure
):
    service = await build(bindings={"implement": {"agent": "flash", "execution": "patch"}})
    service.adapters["flash"].answers = [PATCH]
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))

    async def fail(*_args):
        raise failure

    monkeypatch.setattr("orchestrator_mcp.workflow.service.save_patch", fail)
    step_id, token = await step(service, workflow_id, "implement")
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is None and response.status == "awaiting_host_apply"
    assert response.patch == PATCH
    assert (
        type(failure).__name__ in response.recovery_warning
        or str(failure) in response.recovery_warning
    )
    stored_step = await service.store.get_step(step_id)
    assert stored_step.status == "done"
    assert stored_step.recovery_written == 0

    later = await service.status(workflow_id)
    assert later.patch == ""
    assert "no raw patch recovery copy was written" in later.recovery_warning
    assert "keep the patch returned by the run response" in later.recovery_warning


async def test_a_missing_written_recovery_copy_still_reports_possible_expiry(build, repo):
    service = await build(bindings={"implement": {"agent": "flash", "execution": "patch"}})
    workflow_id = await _to_apply(service, repo)
    source = next(row for row in await service.store.steps(workflow_id) if row.step == "implement")
    assert source.recovery_written == 1
    patch_path(workflow_id, source.id).unlink()

    response = await service.status(workflow_id)

    assert response.patch == ""
    assert "may have expired after the seven-day retention window" in response.recovery_warning


async def test_an_unsafe_recovery_file_does_not_break_workflow_status(build, repo):
    service = await build(bindings={"implement": {"agent": "flash", "execution": "patch"}})
    workflow_id = await _to_apply(service, repo)
    source = next(row for row in await service.store.steps(workflow_id) if row.step == "implement")
    patch_path(workflow_id, source.id).chmod(0o640)

    response = await service.status(workflow_id)

    assert response.error is None and response.status == "awaiting_host_apply"
    assert response.patch == ""
    assert "not a private regular file" in response.recovery_warning


async def test_a_corrupt_recovery_file_is_not_returned_as_the_patch(build, repo):
    service = await build(bindings={"implement": {"agent": "flash", "execution": "patch"}})
    workflow_id = await _to_apply(service, repo)
    source = next(row for row in await service.store.steps(workflow_id) if row.step == "implement")
    patch_path(workflow_id, source.id).write_text("different patch")

    response = await service.status(workflow_id)

    assert response.error is None and response.patch == ""
    assert "does not match the recorded patch hash" in response.recovery_warning


async def test_recording_patch_application_removes_the_raw_recovery_copy(build, repo):
    service = await build(bindings={"implement": {"agent": "flash", "execution": "patch"}})
    workflow_id = await _to_apply(service, repo)
    source = next(
        row for row in await service.store.steps(workflow_id) if row.step == "implement"
    )
    assert patch_path(workflow_id, source.id).exists()
    older = patch_path(workflow_id, "older-round")
    older.write_text("raw old patch")
    older.chmod(0o600)

    step_id, token = await step(service, workflow_id, "apply_patch")
    response = await service.record_host_step(
        workflow_id,
        step_id,
        token,
        {"summary": "applied", "files": ["src/a.py"], "commit": "new-commit"},
    )

    assert response.error is None and response.status == "testing"
    assert not patch_path(workflow_id, source.id).exists()
    assert not older.exists()


async def test_the_material_the_host_shows_a_step_reaches_the_agent(build, repo):
    """The only channel a delegated step has to the source it is changing.

    It cannot see the repository, so material passed at plan time has to survive to
    the send or the step is being asked to patch a file it was never shown -- which
    is what a live run against a real model produced before this existed.
    """
    service = await build(bindings={"plan": {"agent": "codex-sol"}})
    service.adapters["codex-sol"].answers = ["```json\n" + PLAN + "\n```"]
    workflow_id = await started(service, repo)
    # One line and no escapes, because the payload is JSON: a newline in the
    # material is `\n` by the time it reaches the wire.
    source = "def parse(line): return line.split()"

    planned = await service.plan_step(workflow_id, "plan", context=source)
    assert planned.error is None, planned.error
    step_id, token = planned.preview.step_id, planned.preview.confirm_token
    response = await service.run_step(workflow_id, step_id, token)
    assert response.error is None, response.error

    sent = service.adapters["codex-sol"].prompts[0]
    assert source in sent
    # Counted in the preview too: a `prompt_chars` that ignored the material would
    # understate exactly the part the operator most wants to see the size of.
    assert planned.preview.prompt_chars > len(source)


async def test_host_material_is_redacted_before_it_is_stored_and_before_it_is_sent(
    build, repo
):
    """One string for all three: the preview, the token's hash, and the send.

    Storing the original and sending it would make the record a description of
    something else; sending the original and storing the redaction would make the
    hash unverifiable against either.
    """
    secret = "sk-ant-api03-" + "B" * 40
    service = await build(bindings={"plan": {"agent": "codex-sol"}})
    service.adapters["codex-sol"].answers = ["```json\n" + PLAN + "\n```"]
    workflow_id = await started(service, repo)

    planned = await service.plan_step(workflow_id, "plan", context=f'KEY = "{secret}"\n')
    assert planned.error is None, planned.error
    step_id = planned.preview.step_id
    response = await service.run_step(workflow_id, step_id, planned.preview.confirm_token)
    assert response.error is None, response.error

    assert secret not in service.adapters["codex-sol"].prompts[0]
    snapshot = (
        await sql(service, "SELECT agent_snapshot_json FROM workflow_steps WHERE id = ?", step_id)
    ).fetchone()[0]
    assert secret not in snapshot
    assert "KEY" in snapshot


async def test_a_reviewer_sees_the_same_material_the_coding_steps_did(build, repo):
    """A reviewer reading a diff without the file it changes is reviewing half of it."""
    service = await build()
    service.adapters["codex-sol"].answers = ["Looks fine.\n\n```json\n{\"findings\": []}\n```"]
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(
        service, workflow_id, "implement", {"summary": "did it", "files": ["src/a.py"]}
    )
    await host_step(
        service, workflow_id, "test",
        {"command": "pytest -q", "workdir": str(repo), "exit_code": 0, "status": "passed"},
    )

    # One line and no escapes, because the payload is JSON: a newline in the
    # material is `\n` by the time it reaches the wire.
    source = "def parse(line): return line.split()"
    planned = await service.plan_step(workflow_id, "review", context=source)
    assert planned.error is None, planned.error
    stored = (
        await sql(service, "SELECT context FROM reviews WHERE id = ?", planned.preview.review_id)
    ).fetchone()[0]
    assert source in stored


async def test_the_prompt_a_step_sends_is_the_one_its_preview_described(build, repo):
    service = await build(bindings={"plan": {"agent": "codex-sol"}})
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "research", json.loads(RESEARCH))
    step_id, token = await step(service, workflow_id, "plan")

    # The research brief the preview hashed is replaced behind the step's back.
    changed = json.dumps({"summary": "something else entirely", "findings": [], "open_questions": []})
    await sql(
        service,
        "UPDATE workflow_steps SET output_json = ? WHERE workflow_id = ? AND step = 'research'",
        changed, workflow_id,
    )
    response = await service.run_step(workflow_id, step_id, token)
    assert response.error is not None
    assert "other than what its preview described" in response.error.message
    assert service.adapters["codex-sol"].prompts == []


async def test_a_contained_step_returns_a_patch_and_leaves_the_tree_alone(
    build, repo, tmp_path, monkeypatch
):
    """The whole of `isolated_write` from the workflow's side.

    The stub is a real executable on PATH writing real files into whatever directory
    it is given, so what is exercised here is the worktree, the capture and the
    transition -- everything except the sandbox, which is codex's and was checked
    live rather than here.
    """
    from .fixtures import agent_stub

    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{
        "stdout": "".join(json.dumps(event) + "\n" for event in (
            {"type": "thread.started", "thread_id": "th-1", "model": "gpt-5.6-sol"},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "Split the scanner out into src/b.py."}},
        )),
        "append": {"src/b.py": "def scan():\n    return 2\n"},
    }])
    service = await build(
        bindings={"implement": {"agent": "codex-sol", "execution": "isolated_write"}},
        agents={
            **AGENTS,
            "codex-sol": workflow_agent(
                "codex", "gpt-5.6-sol", 10,
                execution_modes=["consultation", "patch", "isolated_write"],
            ),
        },
    )
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    step_id, token = await step(service, workflow_id, "implement")
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is None, response.error
    # The raw patch comes back once, the same way a delegated one does.
    assert "+def scan():" in response.patch
    # The host still owns applying it: a contained write has touched no branch.
    assert response.status == "awaiting_host_apply"
    assert not (repo / "src" / "b.py").exists()
    # And the consult path was never involved.
    assert service.adapters["codex-sol"].prompts == []


async def test_a_contained_step_builds_on_the_applied_result_not_the_first_baseline(
    build, repo, tmp_path, monkeypatch
):
    """The single-round smoke test could not see this, and a review found it.

    Every contained run checked out `baseline_commit`, so the second round of a
    workflow edited pre-implementation source and returned a patch computed against a
    commit the branch had already left. Applying it reverts round one. The workflow
    tracks `result_commit` for exactly this and simply was not reading it.
    """
    from .fixtures import agent_stub

    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{
        "stdout": "".join(json.dumps(event) + "\n" for event in (
            {"type": "thread.started", "thread_id": "th-1", "model": "gpt-5.6-sol"},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "Extended the scanner."}},
        )),
        "append": {"src/a.py": "extra\n"},
    }])
    service = await build(
        bindings={"implement": {"agent": "codex-sol", "execution": "isolated_write"}},
        agents={
            **AGENTS,
            "codex-sol": workflow_agent(
                "codex", "gpt-5.6-sol", 10,
                execution_modes=["consultation", "patch", "isolated_write"],
            ),
        },
    )
    workflow_id = await started(service, repo)

    # Round one, as the host would have applied it: a real commit the workflow now
    # points at. `src/a.py` says `new`; the baseline still says `old`.
    (repo / "src" / "a.py").write_text("new\n")
    subprocess.run(["git", "commit", "-aqm", "round one"], cwd=repo, check=True)
    applied = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    await sql(service, "UPDATE workflow_runs SET result_commit = ? WHERE id = ?",
              applied, workflow_id)

    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    step_id, token = await step(service, workflow_id, "implement")
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is None, response.error
    # The context line is what the worktree was checked out at. `old` here would mean
    # the round started from the first baseline and its patch reverts round one.
    assert "\n new\n" in response.patch
    assert "old" not in response.patch


async def test_a_contained_step_that_changes_nothing_does_not_advance(
    build, repo, tmp_path, monkeypatch
):
    """`awaiting_host_apply` with an empty patch would ask the host to apply nothing
    and then test it. The model's own account of why is kept in the refusal."""
    from .fixtures import agent_stub

    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{
        "stdout": json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "I could not find the scanner."},
        }) + "\n",
    }])
    service = await build(
        bindings={"implement": {"agent": "codex-sol", "execution": "isolated_write"}},
        agents={
            **AGENTS,
            "codex-sol": workflow_agent(
                "codex", "gpt-5.6-sol", 10,
                execution_modes=["consultation", "patch", "isolated_write"],
            ),
        },
    )
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    step_id, token = await step(service, workflow_id, "implement")
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is not None
    assert "changed no files" in response.error.message
    assert "could not find the scanner" in response.error.message
    assert response.status == "coding"


def _contained_test(service_agents: dict) -> dict:
    return {
        **service_agents,
        "codex-sol": workflow_agent(
            "codex", "gpt-5.6-sol", 10,
            execution_modes=["consultation", "patch", "isolated_write"],
        ),
    }


async def _to_testing(service, repo) -> str:
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH})
    return workflow_id


async def test_a_step_body_travels_with_the_call_that_touched_it(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    response = await host_step(
        service, workflow_id, "author_execution_prompt", json.loads(BRIEF)
    )

    views = {v.step: v for v in response.workflow.steps}
    assert views["author_execution_prompt"].output is not None
    assert response.step.output == views["author_execution_prompt"].output
    # The plan did not change because a brief was written after it.
    assert views["plan"].output is None
    assert views["plan"].status == "done", "shape survives, only the body goes"

    full = await service.status(workflow_id)
    assert all(v.output is not None for v in full.workflow.steps if v.status == "done")


def _ran(command: list[str], exit_code: int, output: str) -> str:
    return "".join(json.dumps(event) + "\n" for event in (
        {"type": "thread.started", "thread_id": "th-1", "model": "gpt-5.6-sol"},
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": command,
            "exit_code": exit_code, "aggregated_output": output,
        }},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "Ran the suite."}},
    ))


async def test_a_delegated_test_step_reports_the_exit_code_this_process_read(
    build, repo, tmp_path, monkeypatch
):
    """`test` is bound to `isolated_write` and had no path that could succeed.

    It went through `_run_contained`, which refuses a run that changed nothing -- which
    is exactly what a passing test run looks like -- and otherwise built a `CodeChange`,
    so `_advance` read `status` off a code change, found none, and sent every delegated
    test to `fixing`. The step was unrunnable and `reported_by="orchestrator"` was a
    value nothing could produce.
    """
    from .fixtures import agent_stub

    agent_stub.install("codex", tmp_path, monkeypatch,
                       runs=[{"stdout": _ran(["pytest", "-q"], 0, "12 passed")}])
    service = await build(
        bindings={"test": {"agent": "codex-sol", "execution": "isolated_write"}},
        agents=_contained_test(AGENTS),
    )
    workflow_id = await _to_testing(service, repo)
    step_id, token = await step(service, workflow_id, "test")
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is None, response.error
    assert response.status == "reviewing"
    report = [s for s in response.workflow.steps if s.step == "test"][0].output
    assert report["status"] == "passed"
    assert report["command"] == "pytest -q" and report["exit_code"] == 0
    assert "12 passed" in report["stdout_tail"]
    # The point of the whole path: an exit code this process read out of the event
    # stream, not a sentence the model wrote.
    assert report["reported_by"] == "orchestrator"
    # A test step is not a channel for edits, so a run that changed nothing says so.
    assert report["changed_files"] == []


async def test_a_delegated_test_that_failed_goes_back_to_fixing(
    build, repo, tmp_path, monkeypatch
):
    """And a run that edited the code while testing it says which files.

    The worktree is gone either way -- nothing here can apply those edits -- so this is
    only about the report not looking identical to a clean pass.
    """
    from .fixtures import agent_stub

    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{
        "stdout": _ran(["pytest", "-q"], 1, "1 failed, 11 passed"),
        "append": {"src/a.py": "# nudged the test into passing\n"},
    }])
    service = await build(
        bindings={"test": {"agent": "codex-sol", "execution": "isolated_write"}},
        agents=_contained_test(AGENTS),
    )
    workflow_id = await _to_testing(service, repo)
    step_id, token = await step(service, workflow_id, "test")
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is None, response.error
    assert response.status == "fixing"
    report = [s for s in response.workflow.steps if s.step == "test"][0].output
    assert report["status"] == "failed" and report["exit_code"] == 1
    assert report["changed_files"] == ["src/a.py"]


async def test_a_step_that_stopped_running_cannot_still_record_its_result(build, repo):
    """`finish_step` matched on the id alone, so a writer that had lost the step won.

    The step is reaped as `abandoned` while its run is still in flight; the run then
    comes back and writes. Without a status guard that write resurrects it as `done`,
    clears the lease columns and reports success for a step the workflow already gave
    up on -- and says nothing about it.
    """
    service = await build()
    workflow_id = await started(service, repo)
    step_id, _ = await step(service, workflow_id, "research")
    await sql(service, "UPDATE workflow_steps SET status = 'abandoned' WHERE id = ?", step_id)

    with pytest.raises(StoreError, match="no longer running"):
        await service.store.finish_step(step_id, "done", output={"summary": "s"})
    row = (
        await sql(service, "SELECT status, output_json FROM workflow_steps WHERE id = ?", step_id)
    ).fetchone()
    assert row[0] == "abandoned" and not row[1]


async def test_a_second_caller_cannot_take_a_lease_the_first_one_still_holds(build, repo):
    """The held-lease query exempted the step being leased, which was the hole.

    A duplicate call on one step overwrote the first caller's `lease_holder`, failed on
    the spent token, and on the way out cleared the lease it had just stolen -- leaving
    the first still running with no lease at all, so a *different* step of the same
    workflow could then start beside it. The lease is on the workflow; it has to hold
    against every caller, including one naming the same step.
    """
    service = await build()
    workflow_id = await started(service, repo)
    step_id, _ = await step(service, workflow_id, "research")

    async with service.store.lease(workflow_id, step_id, ttl_s=60):
        with pytest.raises(StoreError, match="already has step"):
            async with service.store.lease(workflow_id, step_id, ttl_s=60):
                pass
        held = (
            await sql(service, "SELECT lease_holder FROM workflow_steps WHERE id = ?", step_id)
        ).fetchone()[0]
        assert held, "the first caller's lease was cleared by the one that was refused"


async def test_a_live_workflow_lease_is_renewed_past_its_initial_expiry(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    step_id, _ = await step(service, workflow_id, "research")

    async with service.store.lease(workflow_id, step_id, ttl_s=0.06):
        initial = (
            await sql(
                service, "SELECT lease_expires_at FROM workflow_steps WHERE id = ?", step_id
            )
        ).fetchone()[0]
        await asyncio.sleep(0.08)
        renewed = (
            await sql(
                service, "SELECT lease_expires_at FROM workflow_steps WHERE id = ?", step_id
            )
        ).fetchone()[0]
        assert renewed > initial


async def test_a_stolen_workflow_lease_interrupts_the_guarded_step(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    step_id, _ = await step(service, workflow_id, "research")

    with pytest.raises(StoreError, match="lost its execution lease"):
        async with service.store.lease(workflow_id, step_id, ttl_s=0.03):
            await sql(
                service,
                "UPDATE workflow_steps SET lease_holder = 'stolen' WHERE id = ?",
                step_id,
            )
            await asyncio.sleep(1)


# --- review, synthesis and the loop -----------------------------------------


async def test_the_review_steps_token_is_the_review_plans_token(build, repo):
    service = await build()
    service.adapters["codex-sol"].answers = [FINDINGS]
    workflow_id = await started(service, repo)
    await sql(service, "UPDATE workflow_runs SET status = 'reviewing' WHERE id = ?", workflow_id)
    response = await service.plan_step(workflow_id, "review")
    assert response.preview is not None and response.preview.review_id
    review_id = response.preview.review_id

    # The same secret opens the review directly, which is what "one approval" means.
    stored = (
        await sql(service, "SELECT confirm_token_sha FROM workflow_steps WHERE id = ?",
                  response.preview.step_id)
    ).fetchone()[0]
    review_sha = (
        await sql(service, "SELECT confirm_token_sha FROM reviews WHERE id = ?", review_id)
    ).fetchone()[0]
    assert stored == review_sha


async def test_replanning_a_review_cancels_and_erases_the_superseded_pending_review(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    await sql(service, "UPDATE workflow_runs SET status = 'reviewing' WHERE id = ?", workflow_id)
    first = await service.plan_step(workflow_id, "review", context="first copy")
    first_review = first.preview.review_id

    second = await service.plan_step(workflow_id, "review", context="second copy")

    old_step = await service.store.get_step(first.preview.step_id)
    assert old_step.status == "cancelled" and old_step.review_id is None
    reviews = (
        await sql(service, "SELECT id, context, status FROM reviews WHERE workflow_id = ?", workflow_id)
    ).fetchall()
    assert [(row[0], row[2]) for row in reviews] == [(second.preview.review_id, "pending")]
    assert "second copy" in reviews[0][1] and "first copy" not in reviews[0][1]
    assert first_review != second.preview.review_id


async def test_a_reviewer_that_is_the_implementer_is_refused(build, repo):
    """`review_policy` is checked on execution identity, not on the agent id typed."""
    service = await build(
        bindings={
            "implement": {"agent": "codex-sol", "execution": "patch"},
            "review": {"agents": ["codex-twin"]},
        },
        agents={
            **AGENTS,
            # A second id for the same model. The policy has to see through it.
            "codex-twin": workflow_agent("codex", "gpt-5.6-sol", 40),
        },
        review_policy={"different_from_implementer": True},
    )
    workflow_id = await started(service, repo)
    await sql(service, "UPDATE workflow_runs SET status = 'reviewing' WHERE id = ?", workflow_id)
    response = await service.plan_step(workflow_id, "review")
    assert response.error is not None
    assert "cannot be shown to differ from the implementer" in response.error.message
    # Refused before a review row exists.
    assert (await sql(service, "SELECT COUNT(*) FROM reviews")).fetchone()[0] == 0


async def _to_synthesis(service, repo, findings: str = FINDINGS) -> str:
    service.adapters["codex-sol"].answers = [findings]
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH})
    await host_step(
        service, workflow_id, "test",
        {"command": "pytest", "workdir": str(repo), "exit_code": 0, "status": "passed"},
    )
    step_id, token = await step(service, workflow_id, "review")
    assert (await service.run_step(workflow_id, step_id, token)).error is None
    return workflow_id


async def finding_ids(service, workflow_id: str) -> list[str]:
    """The ids this server assigned to the reviewer's findings.

    Read back rather than guessed: `missing_serious` resolves provenance by id, and a
    made-up one would fail the same check a dropped finding does, for a different
    reason, which would make the tests below prove nothing.
    """
    review_id = _review_id(await service.store.steps(workflow_id))
    response = await service.reviews.get(review_id)
    return [f.finding_id for result in response.results for f in result.findings]


def summary_with(disposition: str, ids: list[str], **extra) -> dict:
    """A synthesis that keeps the reviewer's one Important finding, disposed as asked."""
    return {
        "summary": "one real bug, and it is the one the reviewer found",
        "combined_findings": [
            {
                "problem": "the scanner drops the last token",
                "severity": "important",
                "source_finding_ids": ids,
                "disposition": disposition,
                **extra,
            }
        ],
        "recommendation": "fix the scanner",
        "checked": ["src/a.py"],
        "not_checked": [],
    }


async def test_an_open_important_finding_keeps_the_loop_going(build, repo):
    service = await build()
    workflow_id = await _to_synthesis(service, repo)
    ids = await finding_ids(service, workflow_id)
    response = await host_step(service, workflow_id, "synthesize", summary_with("open", ids))
    assert response.status == "fixing"
    record = [s for s in response.workflow.steps if s.step == "synthesize"][0]
    assert record.output["loop_done"] is False and record.output["open_serious"] == 1
    assert response.workflow.fix_rounds == 1


async def test_a_fix_round_is_sent_the_findings_it_is_supposed_to_fix(build, repo):
    """The gap a live run found: `fix` was previewed and sent with an empty payload.

    Its declared input was `review_outcome`, which no step ever writes -- the review's
    product is a review row -- so the round arrived carrying nothing and the agent
    answered from the goal a second time instead of from what the reviewer said.
    """
    adapters = {aid: StubAdapter(FINDINGS, PATCH) for aid in AGENTS}
    service = await build(adapters=adapters,
                          bindings={"fix": {"agent": "flash", "execution": "patch"}})
    workflow_id = await _to_synthesis(service, repo)
    ids = await finding_ids(service, workflow_id)
    assert (await host_step(service, workflow_id, "synthesize",
                            summary_with("open", ids))).status == "fixing"

    response = await service.plan_step(workflow_id, "fix")
    assert response.error is None, response.error
    assert "open_findings" in response.preview.inputs

    step_id, token = response.preview.step_id, response.preview.confirm_token
    assert (await service.run_step(workflow_id, step_id, token)).error is None
    sent = adapters["flash"].prompts[-1]
    assert "the scanner drops the last token" in sent

    # A closed finding is not what a round is for, and carrying it back would invite a
    # patch for something already disposed of.
    stored = json.loads(
        [s for s in await service.store.steps(workflow_id) if s.step == "fix"][-1].agent_snapshot_json
    )
    assert stored["inputs"] == ["implementation_plan", "test_report", "open_findings"]


async def test_dropping_a_reviewers_important_finding_is_refused(build, repo):
    service = await build()
    workflow_id = await _to_synthesis(service, repo)
    step_id, token = await step(service, workflow_id, "synthesize")
    empty = {"summary": "nothing to report", "combined_findings": [],
             "recommendation": "ship it", "checked": ["src/a.py"], "not_checked": []}
    result = await service.record_host_step(workflow_id, step_id, token, empty)
    assert result.error is not None
    assert "source_finding_ids" in result.error.message


async def test_rejecting_a_serious_finding_without_a_reason_is_refused(build, repo):
    service = await build()
    workflow_id = await _to_synthesis(service, repo)
    ids = await finding_ids(service, workflow_id)
    step_id, token = await step(service, workflow_id, "synthesize")
    result = await service.record_host_step(
        workflow_id, step_id, token, summary_with("rejected", ids)
    )
    assert result.error is not None
    assert "disposition_reason" in result.error.message

    step_id, token = await step(service, workflow_id, "synthesize")
    ok = await service.record_host_step(
        workflow_id, step_id, token,
        summary_with("rejected", ids, disposition_reason="the scanner is fed a sentinel"),
    )
    assert ok.error is None, ok.error
    assert ok.status == "completed"


async def test_the_cap_ends_in_needs_attention_not_completed(build, repo):
    service = await build(max_fix_rounds=1)
    workflow_id = await _to_synthesis(service, repo)
    ids = await finding_ids(service, workflow_id)
    first = await host_step(service, workflow_id, "synthesize", summary_with("open", ids))
    assert first.status == "fixing" and first.workflow.fix_rounds == 1

    # Second round: fix, test, review again, and synthesize with it still open.
    await host_step(service, workflow_id, "fix", {"summary": "s", "files": [], "patch": PATCH})
    await host_step(
        service, workflow_id, "test",
        {"command": "pytest", "workdir": str(repo), "exit_code": 0, "status": "passed"},
    )
    service.adapters["codex-sol"] = StubAdapter(FINDINGS)
    step_id, token = await step(service, workflow_id, "review")
    assert (await service.run_step(workflow_id, step_id, token)).error is None
    second = await host_step(
        service, workflow_id, "synthesize",
        summary_with("open", await finding_ids(service, workflow_id)),
    )
    assert second.status == "needs_attention"
    assert "cap was reached" in (second.workflow.reason or "")


async def test_loop_done_is_false_without_a_passing_report(build, repo):
    service = await build(advance_on_failed_test=True)
    service.adapters["codex-sol"].answers = [FINDINGS]
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH})
    await host_step(
        service, workflow_id, "test",
        {"command": "pytest", "workdir": str(repo), "exit_code": 1, "status": "failed"},
    )
    step_id, token = await step(service, workflow_id, "review")
    assert (await service.run_step(workflow_id, step_id, token)).error is None
    response = await host_step(
        service, workflow_id, "synthesize",
        summary_with("fixed", await finding_ids(service, workflow_id)),
    )
    record = [s for s in response.workflow.steps if s.step == "synthesize"][0]
    assert record.output["loop_done"] is False
    assert "the last test run was `failed`" in record.output["reasons"]


# --- replan and cancel ------------------------------------------------------


async def test_a_config_edit_does_not_reroute_a_running_workflow(build, repo):
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    workflow_id = await started(service, repo)
    # The operator edits the config underneath the running workflow.
    service.policy.bindings["research"].agent = "flash"
    view = (await service.status(workflow_id)).workflow
    assert view.bindings["research"]["agents"][0]["agent_id"] == "codex-sol"


async def test_rerouting_takes_the_replan_handshake(build, repo):
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    workflow_id = await started(service, repo)
    before = (await service.status(workflow_id)).workflow.bindings

    preview = await service.plan_replan(workflow_id, {"research": {"agent": "flash"}})
    assert preview.error is None, preview.error
    assert preview.preview is not None
    # Staged only: nothing routes differently until the token is spent.
    assert (await service.status(workflow_id)).workflow.bindings == before

    assert (await service.replan(workflow_id, "not-the-token")).error is not None
    assert (await service.status(workflow_id)).workflow.bindings == before

    done = await service.replan(workflow_id, preview.preview.confirm_token)
    assert done.error is None, done.error
    assert done.workflow.bindings["research"]["agents"][0]["agent_id"] == "flash"
    # And spent: one handshake, one reroute.
    assert (await service.replan(workflow_id, preview.preview.confirm_token)).error is not None


async def test_cancel_stops_the_workflow_and_it_takes_no_more_steps(build, repo):
    service = await build()
    workflow_id = await started(service, repo)
    response = await service.cancel(workflow_id)
    assert response.error is None and response.status == "cancelled"
    assert (await service.plan_step(workflow_id, "research")).error is not None
    assert (await service.cancel(workflow_id)).error is not None


async def test_an_expired_lease_leaves_the_step_abandoned_and_replannable(build, repo):
    """A status call is enough to unwedge a workflow whose runner died."""
    service = await build()
    workflow_id = await started(service, repo)
    step_id, _ = await step(service, workflow_id, "research")
    await sql(
        service,
        # A unix time, the way `lease()` writes it: an ISO string here would compare
        # as text against a number and silently never expire.
        "UPDATE workflow_steps SET status = 'running', lease_holder = 'gone', "
        "lease_expires_at = 1 WHERE id = ?",
        step_id,
    )
    view = (await service.status(workflow_id)).workflow
    assert [s.status for s in view.steps] == ["abandoned"]
    assert (await service.plan_step(workflow_id, "research")).error is None


# --- what a step is allowed to claim ----------------------------------------


async def _to_apply(service, repo) -> str:
    """A workflow waiting on the host to apply a delegated patch."""
    service.adapters["flash"].answers = [PATCH]
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    step_id, token = await step(service, workflow_id, "implement")
    assert (await service.run_step(workflow_id, step_id, token)).status == "awaiting_host_apply"
    return workflow_id


@pytest.mark.parametrize("commit", ["", "baseline"])
async def test_apply_patch_without_a_new_commit_is_a_failed_step(build, repo, commit):
    """`CodeChange` has no way to say "it did not apply", so this had to become one.

    The host records the step, writes "the diff did not apply" in the summary, and the
    workflow moved to `testing` regardless -- from there a passing test report on the
    unchanged tree carried the whole loop to `completed` over work that was never
    applied. A step that did not do its work is a failed step.
    """
    service = await build(bindings={"implement": {"agent": "flash", "execution": "patch"}})
    workflow_id = await _to_apply(service, repo)
    baseline = (await service.status(workflow_id)).workflow.baseline_commit

    step_id, token = await step(service, workflow_id, "apply_patch")
    response = await service.record_host_step(
        workflow_id, step_id, token,
        {
            "summary": "the diff did not apply",
            "files": [],
            "commit": baseline if commit == "baseline" else "",
        },
    )
    assert response.error is not None
    assert "records the commit the applied patch produced" in response.error.message
    # Recorded as failed, not left `running`, so the host can plan it again.
    row = (
        await sql(service, "SELECT status FROM workflow_steps WHERE id = ?", step_id)
    ).fetchone()
    assert row[0] == "failed"
    assert (await service.status(workflow_id)).status == "awaiting_host_apply"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"exit_code": 1, "status": "passed"}, "cannot be `passed`"),
        ({"exit_code": None, "status": "passed"}, "cannot be `passed`"),
        ({"exit_code": 0, "status": "failed"}, "cannot be `failed`"),
        ({"exit_code": 0, "status": "timeout"}, "cannot be `timeout`"),
        ({"exit_code": 1, "status": "skipped"}, "cannot be `skipped`"),
    ],
)
async def test_a_test_report_whose_status_contradicts_its_exit_code_is_refused(
    build, repo, payload, message
):
    """`_loop_done` reads `status` alone, so nothing ever compared it to the number.

    `exit_code: 1` with `status: "passed"` was a report the store accepted and the
    loop believed -- and the exit code was the only evidence in the row.
    """
    service = await build()
    workflow_id = await _to_testing(service, repo)
    step_id, token = await step(service, workflow_id, "test")
    response = await service.record_host_step(
        workflow_id, step_id, token,
        {"command": "pytest", "workdir": str(repo), **payload},
    )
    assert response.error is not None
    assert message in response.error.message
    assert (await service.status(workflow_id)).status == "testing"


async def test_a_timeout_report_preserves_a_nonzero_wrapper_exit_code(build, repo):
    service = await build()
    workflow_id = await _to_testing(service, repo)
    step_id, token = await step(service, workflow_id, "test")

    response = await service.record_host_step(
        workflow_id,
        step_id,
        token,
        {"command": "timeout 30 pytest", "workdir": str(repo), "exit_code": 124,
         "status": "timeout"},
    )

    assert response.error is None
    stored = (await service.store.get_step(step_id)).output()
    assert stored is not None
    assert stored["status"] == "timeout" and stored["exit_code"] == 124


async def test_a_contained_test_with_no_exit_code_is_skipped_rather_than_passed(
    build, repo, tmp_path, monkeypatch
):
    """The one thing `reported_by="orchestrator"` exists to rule out.

    A command the stream reports running without reporting how it ended used to fall
    through to `passed` with a fabricated zero -- this process attesting to an exit
    code it never read.
    """
    from .fixtures import agent_stub

    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": "".join(
        json.dumps(event) + "\n" for event in (
            {"type": "thread.started", "thread_id": "th-1", "model": "gpt-5.6-sol"},
            {"type": "item.completed", "item": {
                "type": "command_execution", "command": ["pytest", "-q"],
                "aggregated_output": "collecting ...",
            }},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Ran it."}},
        )
    )}])
    service = await build(
        bindings={"test": {"agent": "codex-sol", "execution": "isolated_write"}},
        agents=_contained_test(AGENTS),
    )
    workflow_id = await _to_testing(service, repo)
    step_id, token = await step(service, workflow_id, "test")
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is None, response.error
    report = [s for s in response.workflow.steps if s.step == "test"][0].output
    assert report["status"] == "skipped" and report["exit_code"] is None
    # And a report that read no exit code is not a pass, so the loop goes on.
    assert response.status == "fixing"


async def test_a_passing_report_from_an_earlier_commit_does_not_finish_the_loop(
    build, repo
):
    """The stale pass: a report says `passed` forever, and rounds move on without it.

    Nothing compared what the tests ran against to what the workflow now holds, so the
    round that changed the code again inherited the previous round's green.
    """
    service = await build()
    workflow_id = await _to_synthesis(service, repo)
    # The fix round landed a new commit; the test report still names the old one.
    await sql(
        service, "UPDATE workflow_runs SET result_commit = ? WHERE id = ?", "f" * 40, workflow_id
    )
    ids = await finding_ids(service, workflow_id)
    response = await host_step(service, workflow_id, "synthesize", summary_with("fixed", ids))

    assert response.status == "fixing"
    record = [s for s in response.workflow.steps if s.step == "synthesize"][0]
    assert record.output["loop_done"] is False
    assert any("the workflow now holds" in reason for reason in record.output["reasons"])


# --- the policy a workflow was started under --------------------------------


async def test_a_config_edit_does_not_move_the_round_cap_of_a_running_workflow(build, repo):
    """`policy_json` was written at `start`, hashed into `workflow_hash`, then ignored.

    Every read went to the live config, so editing the file under a running workflow
    moved its cap and flipped whether a failed test advances -- without the replan
    handshake that exists to make exactly that change visible.
    """
    service = await build(max_fix_rounds=1)
    workflow_id = await _to_synthesis(service, repo)
    await sql(service, "UPDATE workflow_runs SET fix_rounds = 1 WHERE id = ?", workflow_id)
    # The operator raises the cap in the config file mid-workflow.
    service.policy.max_fix_rounds = 5

    ids = await finding_ids(service, workflow_id)
    response = await host_step(service, workflow_id, "synthesize", summary_with("open", ids))
    assert response.status == "needs_attention"
    assert "1-round cap" in response.workflow.reason
    assert response.workflow.max_fix_rounds == 1


async def test_a_config_edit_does_not_change_whether_a_failed_test_advances(build, repo):
    service = await build(advance_on_failed_test=False)
    workflow_id = await _to_testing(service, repo)
    service.policy.advance_on_failed_test = True

    await host_step(
        service, workflow_id, "test",
        {"command": "pytest", "workdir": str(repo), "exit_code": 1, "status": "failed"},
    )
    assert (await service.status(workflow_id)).status == "fixing"


async def test_a_replan_keeps_the_bindings_it_was_not_asked_about(build, repo):
    """Rebinding one step re-resolved every other one from the config file.

    The overrides `start` was given went first, and so did the routing the workflow had
    been running under -- a replan of `fix` quietly moved `research` somewhere else.
    """
    service = await build()
    workflow_id = await started(service, repo, bindings={"research": {"agent": "flash"}})

    preview = await service.plan_replan(workflow_id, {"plan": {"agent": "codex-sol"}})
    assert preview.error is None, preview.error
    done = await service.replan(workflow_id, preview.preview.confirm_token)
    assert done.error is None, done.error

    assert done.workflow.bindings["plan"]["agents"][0]["agent_id"] == "codex-sol"
    assert done.workflow.bindings["research"]["agents"][0]["agent_id"] == "flash"


async def test_a_replan_does_not_re_resolve_the_steps_it_was_not_asked_about(build, repo):
    """Keeping the stored agent id is not the same as keeping the stored binding.

    Reading the snapshot back and putting all of it through the router again sent every
    unnamed step past today's agent table: a replan of `plan` failed outright once
    `research`'s agent had been removed from the config, and the rest came back
    re-decided from whatever the file says now.
    """
    service = await build()
    workflow_id = await started(service, repo, bindings={"research": {"agent": "flash"}})
    before = (await service.status(workflow_id)).workflow.bindings["research"]
    # The operator drops the agent `research` is bound to and rebinds a different step.
    service.config.agents.pop("flash")

    preview = await service.plan_replan(workflow_id, {"plan": {"agent": "codex-sol"}})
    assert preview.error is None, preview.error
    done = await service.replan(workflow_id, preview.preview.confirm_token)
    assert done.error is None, done.error

    assert done.workflow.bindings["research"] == before


# --- steps that do not come back --------------------------------------------


async def test_a_step_whose_run_raised_does_not_stay_running_forever(build, repo):
    """The row nothing could resolve.

    An exception that escaped left the step `running` with its lease released -- and
    `reap_abandoned` only matches an *expired* lease, so the row read as a live step
    for good. A process that really dies still leaves its lease held, which is why the
    reaper is unchanged.
    """
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")

    async def boom(*args, **kwargs):
        raise RuntimeError("the adapter exploded")

    service.consult.consult_step = boom
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is not None
    assert "the adapter exploded" in response.error.message
    row = (
        await sql(
            service,
            "SELECT status, lease_holder, lease_expires_at FROM workflow_steps WHERE id = ?",
            step_id,
        )
    ).fetchone()
    assert row[0] == "failed" and row[1] is None and row[2] is None
    # And the step can simply be planned again.
    assert (await service.plan_step(workflow_id, "research")).error is None


async def test_a_step_planned_before_a_cancel_cannot_start_after_it(build, repo):
    """`run_step` reads the workflow's status, then spends the token -- two statements.

    A cancel landing between them left the step to start anyway: an agent paid for a
    workflow that had already ended, with a result nothing would accept. The status is
    checked in the same UPDATE that spends the token, so the window is not a window.
    Called at the store here, because going through `run_step` would be refused by the
    earlier read and prove only that the first check works.
    """
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    assert (await service.cancel(workflow_id)).error is None

    with pytest.raises(StoreError, match="the workflow itself has ended"):
        await service.store.consume_step_token(step_id, workflow_id, token)
    row = (
        await sql(service, "SELECT status FROM workflow_steps WHERE id = ?", step_id)
    ).fetchone()
    # Still `planned`: cancel marks what is running, and this one never started.
    assert row[0] == "planned"


async def test_a_workflows_consultation_is_not_deleted_with_the_ordinary_ones(build, repo):
    """It is the step's record of the work, not a loose consultation.

    Deleted, the workflow reads as intact with a step pointing at a row that is gone.
    """
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    service.adapters["codex-sol"].answers = [RESEARCH]
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    assert (await service.run_step(workflow_id, step_id, token)).error is None
    consultation_id = (await sql(service, "SELECT id FROM consultations")).fetchone()[0]

    _, count = await service.store.store.request_delete_all_consultations()
    assert count == 0
    with pytest.raises(StoreError, match="is a step of workflow"):
        await service.store.store.delete_consultation(consultation_id)
    assert (await sql(service, "SELECT COUNT(*) FROM consultations")).fetchone()[0] == 1


# --- what a workflow spent ---------------------------------------------------


async def _spent(service, workflow_id: str):
    """The workflow view's own totals: `(workflow usage, {step name: usage})`."""
    response = await service.status(workflow_id)
    assert response.error is None, response.error
    view = response.workflow
    assert view is not None
    return view.usage, {v.step: v.usage for v in view.steps}


async def test_a_workflow_totals_what_its_steps_spent(build, repo):
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    service.adapters["codex-sol"].answers = [RESEARCH]
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    assert (await service.run_step(workflow_id, step_id, token)).error is None

    total, steps = await _spent(service, workflow_id)
    assert steps["research"].total_tokens == 12
    assert total.total_tokens == 12
    assert total.prompt_tokens == 10 and total.completion_tokens == 2


async def test_a_host_step_reports_no_usage_rather_than_zero(build, repo):
    """Nothing was spent on it here, which is not the same as it having cost zero."""
    service = await build()
    workflow_id = await started(service, repo)
    await host_step(service, workflow_id, "plan", json.loads(PLAN))

    total, steps = await _spent(service, workflow_id)
    assert steps["plan"] is None
    assert total is None


async def test_a_reviewers_spend_is_charged_to_the_review_step(build, repo):
    """The reviewer's consultation carries its review, not the workflow.

    A rollup that only followed `consultations.workflow_id` would report the most
    expensive step of the whole design as free.
    """
    service = await build()
    workflow_id = await _to_synthesis(service, repo)

    total, steps = await _spent(service, workflow_id)
    assert steps["review"].total_tokens == 12
    assert total.total_tokens == 12


async def test_one_unpriced_step_makes_the_whole_cost_unknown(build, repo):
    """A total built from an unpriced turn is a floor. A floor shown as a sum lies."""
    service = await build(
        adapters={
            "codex-sol": StubAdapter(FINDINGS, cost_usd=0.25),
            "opus-agent": StubAdapter(),
            "flash": StubAdapter(RESEARCH),
        },
        bindings={"research": {"agent": "flash"}},
    )
    workflow_id = await started(service, repo)
    research_id, research_token = await step(service, workflow_id, "research")
    assert (await service.run_step(workflow_id, research_id, research_token)).error is None
    await host_step(service, workflow_id, "plan", json.loads(PLAN))
    await host_step(service, workflow_id, "author_execution_prompt", json.loads(BRIEF))
    await host_step(
        service, workflow_id, "implement", {"summary": "s", "files": [], "patch": PATCH}
    )
    await host_step(
        service, workflow_id, "test",
        {"command": "pytest", "workdir": str(repo), "exit_code": 0, "status": "passed"},
    )
    review_id, review_token = await step(service, workflow_id, "review")
    assert (await service.run_step(workflow_id, review_id, review_token)).error is None

    total, steps = await _spent(service, workflow_id)
    assert steps["review"].cost_usd == pytest.approx(0.25)
    assert steps["research"].cost_usd is None
    # The tokens still add up; only the price is withheld.
    assert total.total_tokens == 24
    assert total.cost_usd is None


async def test_every_turn_of_one_step_is_counted_once(build, repo):
    """Rebuilt from the turn ledger, so a second turn adds and a re-read does not."""
    service = await build(bindings={"research": {"agent": "codex-sol"}})
    service.adapters["codex-sol"].answers = [RESEARCH]
    workflow_id = await started(service, repo)
    step_id, token = await step(service, workflow_id, "research")
    assert (await service.run_step(workflow_id, step_id, token)).error is None
    consultation_id = (await sql(service, "SELECT id FROM consultations")).fetchone()[0]
    turn = (
        "INSERT INTO consultation_turns (consultation_id, sequence_number, source_mode, "
        "user_prompt, input_tokens, output_tokens, total_tokens, latency_ms, created_at) "
        "VALUES (?, 2, 'model', 'again', 5, 1, 6, 1, '2024-01-01T00:00:00Z')"
    )
    await sql(service, turn, consultation_id)

    total, steps = await _spent(service, workflow_id)
    assert steps["research"].total_tokens == 18
    assert (await _spent(service, workflow_id))[0].total_tokens == total.total_tokens == 18


# --- the spend ceiling -------------------------------------------------------


async def test_a_step_past_the_ceiling_is_refused_before_the_token_is_spent(build, repo):
    """A refusal must cost nothing: neither the subprocess nor the one-time token.

    Burning the token would mean raising the ceiling costs a whole re-plan, which
    turns a bound into a punishment.
    """
    service = await build(
        adapters={
            "codex-sol": StubAdapter(RESEARCH, cost_usd=2.0),
            "opus-agent": StubAdapter(),
            "flash": StubAdapter(PLAN),
        },
        bindings={"research": {"agent": "codex-sol"}, "plan": {"agent": "flash"}},
        spend={"max_cost_usd_per_workflow": 1.0},
    )
    workflow_id = await started(service, repo)
    research_id, research_token = await step(service, workflow_id, "research")
    assert (await service.run_step(workflow_id, research_id, research_token)).error is None
    plan_id, plan_token = await step(service, workflow_id, "plan")
    sent = len(service.adapters["flash"].prompts)

    response = await service.run_step(workflow_id, plan_id, plan_token)

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.SPEND_LIMIT_REACHED
    assert "$2.00 of its $1.00 ceiling" in response.error.message
    assert len(service.adapters["flash"].prompts) == sent
    # And the token still opens the step it approved, once the ceiling is raised.
    service.config.spend.max_cost_usd_per_workflow = 10.0
    assert (await service.run_step(workflow_id, plan_id, plan_token)).error is None


async def test_a_turn_ceiling_bounds_a_workflow_no_dollar_ceiling_can_price(build, repo):
    """Same hole, same fix, one step further out: unpriced steps count as turns."""
    service = await build(
        adapters={
            "codex-sol": StubAdapter(RESEARCH),  # reports no price
            "opus-agent": StubAdapter(),
            "flash": StubAdapter(PLAN),
        },
        bindings={"research": {"agent": "codex-sol"}, "plan": {"agent": "flash"}},
        spend={"max_cost_usd_per_workflow": 0.01, "max_turns_per_workflow": 1},
    )
    workflow_id = await started(service, repo)
    research_id, research_token = await step(service, workflow_id, "research")
    assert (await service.run_step(workflow_id, research_id, research_token)).error is None
    plan_id, plan_token = await step(service, workflow_id, "plan")
    sent = len(service.adapters["flash"].prompts)

    response = await service.run_step(workflow_id, plan_id, plan_token)

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.SPEND_LIMIT_REACHED
    assert "1 of its 1 turn ceiling" in response.error.message
    assert len(service.adapters["flash"].prompts) == sent
    # And the token still opens the step it approved, once the ceiling is raised.
    service.config.spend.max_turns_per_workflow = 10
    assert (await service.run_step(workflow_id, plan_id, plan_token)).error is None


async def test_a_reviewer_counts_towards_the_workflows_ceiling(build, repo):
    """The workflow is what paid for the reviewer, whatever row the consultation
    hangs off."""
    service = await build(
        adapters={
            "codex-sol": StubAdapter(FINDINGS, cost_usd=2.0),
            "opus-agent": StubAdapter(),
            "flash": StubAdapter(RESEARCH),
        },
        bindings={"fix": {"agent": "flash", "execution": "patch"}},
        spend={"max_cost_usd_per_workflow": 1.0},
    )
    workflow_id = await _to_synthesis(service, repo)
    ids = await finding_ids(service, workflow_id)
    assert (await host_step(service, workflow_id, "synthesize",
                            summary_with("open", ids))).status == "fixing"
    step_id, token = await step(service, workflow_id, "fix")

    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.SPEND_LIMIT_REACHED


async def test_a_workflow_with_no_ceiling_is_unchanged(build, repo):
    service = await build(
        adapters={
            "codex-sol": StubAdapter(RESEARCH, cost_usd=1000.0),
            "opus-agent": StubAdapter(),
            "flash": StubAdapter(PLAN),
        },
        bindings={"research": {"agent": "codex-sol"}, "plan": {"agent": "flash"}},
    )
    workflow_id = await started(service, repo)
    research_id, research_token = await step(service, workflow_id, "research")
    assert (await service.run_step(workflow_id, research_id, research_token)).error is None
    plan_id, plan_token = await step(service, workflow_id, "plan")

    assert (await service.run_step(workflow_id, plan_id, plan_token)).error is None


async def test_a_free_reviewer_does_not_hide_what_the_paid_one_cost(build, repo):
    """One unpriced turn must not erase the money that *was* counted.

    The step's own `cost_usd` is `None`, because a total containing an unpriced turn
    is a floor. The floor is still $2, and a $1 ceiling is still crossed -- reading
    the unknown as zero would let one free-tier reviewer buy an unbounded workflow.
    """
    service = await build(
        adapters={
            "codex-sol": StubAdapter(FINDINGS, cost_usd=2.0),
            "opus-agent": StubAdapter(),
            "flash": StubAdapter(FINDINGS),
        },
        bindings={
            "review": {"agents": ["codex-sol", "flash"]},
            "fix": {"agent": "flash", "execution": "patch"},
        },
        spend={"max_cost_usd_per_workflow": 1.0},
    )
    workflow_id = await _to_synthesis(service, repo)

    _, steps = await _spent(service, workflow_id)
    assert steps["review"].cost_usd is None, "a floor must not be shown as a sum"

    ids = await finding_ids(service, workflow_id)
    assert (await host_step(service, workflow_id, "synthesize",
                            summary_with("open", ids))).status == "fixing"
    step_id, token = await step(service, workflow_id, "fix")

    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.SPEND_LIMIT_REACHED
    assert "$2.00" in response.error.message


async def test_spend_that_names_no_step_still_counts_against_the_ceiling(build, repo):
    """No path writes a workflow consultation without a step today, and the schema
    does not forbid one. Money that arrives without a step is still money spent, so
    it is bucketed rather than dropped -- the total and the ceiling both see it, and
    no step view claims it."""
    service = await build(
        adapters={
            "codex-sol": StubAdapter(FINDINGS),
            "opus-agent": StubAdapter(),
            "flash": StubAdapter(RESEARCH, cost_usd=5.0),
        },
        bindings={"research": {"agent": "flash"}, "plan": {"agent": "flash"}},
        spend={"max_cost_usd_per_workflow": 1.0},
    )
    workflow_id = await started(service, repo)
    research_id, research_token = await step(service, workflow_id, "research")
    assert (await service.run_step(workflow_id, research_id, research_token)).error is None
    await sql(service, "UPDATE consultations SET step_id = NULL WHERE workflow_id = ?", workflow_id)

    total, steps = await _spent(service, workflow_id)
    assert steps["research"] is None, "no step owns it any more"
    assert total is not None and total.cost_usd == pytest.approx(5.0)

    plan_id, plan_token = await step(service, workflow_id, "plan")
    response = await service.run_step(workflow_id, plan_id, plan_token)

    assert response.error is not None
    assert response.error.code is ConsultErrorCode.SPEND_LIMIT_REACHED
