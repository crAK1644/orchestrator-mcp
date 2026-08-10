"""The workflow layer: routing, state, tokens, artifacts and the bypass it must refuse.

Offline throughout. The adapters are stubbed at `ConsultService.adapter`, the same
seam `test_review_service.py` uses, so everything below the bind -- the prompt, the
lease, the store, the envelope -- is the real code path.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from orchestrator_mcp.consult.adapters.base import AdapterResult, AgentStatus
from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.contract import ConsultationContent, Usage
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.workflow.service import WorkflowService, _review_id
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


class StubAdapter:
    """A CLI that is installed, authenticated, and answers whatever it was handed."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers) or ["{}"]
        self.prompts: list[str] = []
        self.modes: list[Any] = []

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
            usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
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
        **workflow_overrides,
    ):
        agents = dict(AGENTS if agents is None else agents)
        adapters = adapters or {aid: StubAdapter() for aid in agents}
        config = ConsultConfig(
            database_path=str(tmp_path / "c.sqlite3"),
            agents=agents,
            host={"runtime": "claude", "model": "claude-opus-5"},
            review={"reviewers": ["codex-sol"], "deep_reviewers": list(agents)},
            workflow={
                "roots": [str(repo.parent)],
                "bindings": {**HOST_BINDINGS, "review": {"agents": ["codex-sol"]},
                             **(bindings or {})},
                **workflow_overrides,
            },
        )
        return await StubWorkflow(config, "claude", adapters=adapters).open()

    return make


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
        assert (await service.status(workflow_id)).status == expected
        await service.close()


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
            "reported_by": "orchestrator",
        },
    )
    report = [s for s in response.workflow.steps if s.step == "test"][0]
    assert report.output["reported_by"] == "host"
    assert report.reported_by == "host"


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
    service = await build(bindings={"review": {"executor": "host"}})
    workflow_id = await started(service, repo)
    await sql(service, "UPDATE workflow_runs SET status = 'reviewing' WHERE id = ?", workflow_id)
    response = await service.plan_step(workflow_id, "review")
    assert response.error is not None
    assert "cannot be `executor: host`" in response.error.message


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


async def test_a_patch_comes_back_raw_once_and_is_stored_scrubbed(build, repo):
    """The one carve-out in the storage rule, and the hash that ties the two together."""
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
