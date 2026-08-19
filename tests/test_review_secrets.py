"""The one that must not be skipped: no credential-shaped string reaches the disk.

The promise the review layer makes is not "we clean up afterwards" -- it is that
the raw value never lands. So every assertion here reopens the database with a
plain `sqlite3` connection and sweeps **every column of every table**, rather than
checking the columns we happened to think of. A new column added later that
forgets the sanitizer fails these tests without anyone updating them.

What this cannot promise is stated in `test_the_reviewers_own_history_is_out_of_reach`:
the material still reaches the reviewer's CLI, which keeps its own logs.
"""

from __future__ import annotations

import sqlite3

import pytest

from orchestrator_mcp.consult.adapters.base import AdapterResult, AgentStatus
from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.contract import ConsultationContent
from orchestrator_mcp.contract import Usage, redact, scrub_json
from orchestrator_mcp.review.service import ReviewService

from .conftest import agent

SECRET = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"
OTHER = "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIII"

REVIEWERS = {"codex-sol": agent("codex", "gpt-5.6-sol", 10)}


@pytest.mark.parametrize(
    "source, masked",
    [
        pytest.param(
            '{"apiKey": "sk-someone-elses-key"}',
            '{"apiKey": "[redacted]"}',
            id="a JSON member stays a member",
        ),
        pytest.param(
            "password=hunter2hunter2", "password=[redacted]", id="an assignment stays one"
        ),
        pytest.param(
            "Authorization: Bearer AAAABBBBCCCCDDDD",
            "Authorization: [redacted]",
            # `bearer <token>` matches first and takes the header name with it, which is
            # the whole match rather than the value -- the header still reads as one.
            id="a header keeps its name",
        ),
    ],
)
def test_masking_a_value_leaves_the_text_around_it_alone(source, masked):
    """A reviewer reads the masked copy, so a substitution that changes the *shape* of
    the code sends it after a defect nobody wrote. Swallowing the key here turned
    `{"apiKey": "..."}` into `{"[redacted]"}` -- a set literal, and a real reviewer
    duly reported that the dict it was meant to be would not serialize."""
    assert redact(source) == masked


def test_scrubbing_covers_mapping_keys_and_non_list_collections():
    value = {
        f"prefix-{SECRET}": (f"tuple-{SECRET}",),
        "set": {f"set-{SECRET}"},
    }
    cleaned = scrub_json(value)
    serialized = repr(cleaned)
    assert SECRET not in serialized
    assert serialized.count("[redacted]") == 3


class LeakyAdapter:
    """A reviewer that answers with the credential in its prose *and* in a
    structured field, because those are stored through different code paths."""

    def connect_command(self, agent):
        return f"{agent.command} login"

    async def preflight(self, agent):
        return AgentStatus(agent.agent_id, installed=True, authenticated=True)

    async def start(self, agent, prompt, source_mode, session_id=None):
        return self._answer()

    async def resume(self, agent, native_session_id, prompt, source_mode):
        return self._answer()

    def _answer(self) -> AdapterResult:
        return AdapterResult(
            content=ConsultationContent(
                answer=f"the key {SECRET} is hardcoded here",
                assumptions=[f"the deploy uses {SECRET}"],
                uncertainties=[],
                follow_up_questions=[],
                sources=[],
            ),
            native_session_id="native-1",
            model_used="gpt-5.6-sol",
            model_verified=True,
            raw_output=f'{{"answer": "the key {SECRET}"}}',
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class StubService(ReviewService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.consult.adapter = lambda agent: LeakyAdapter()  # type: ignore[method-assign]


@pytest.fixture
def build(tmp_path, host_claude):
    path = tmp_path / "consultations.sqlite3"

    async def make():
        config = ConsultConfig(
            database_path=str(path),
            agents=dict(REVIEWERS),
            review={
                "reviewers": ["codex-sol"],
                "deep_reviewers": ["codex-sol"],
                "roots": [str(tmp_path)],
            },
        )
        return await StubService(config, "claude").open()

    make.path = path  # type: ignore[attr-defined]
    return make


def every_value(path) -> list[tuple[str, str, str]]:
    """(table, column, value) for every text-bearing cell in the database."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return [
            (table, key, str(row[key]))
            for table in tables
            for row in db.execute(f"SELECT * FROM {table}")  # noqa: S608 -- names from the schema
            for key in row.keys()
            if row[key] is not None
        ]
    finally:
        db.close()


def assert_absent(path, *needles: str) -> None:
    found = [
        f"{table}.{column}"
        for table, column, value in every_value(path)
        for needle in needles
        if needle in value
    ]
    assert found == [], f"raw secret reached {found}"


async def run_a_review(service, **overrides):
    plan = await service.plan(
        goal=f"is {SECRET} safe to ship",
        context=f"AUTH = {OTHER}",
        material=[{"label": f"src/{OTHER}.py", "kind": "file"}],
        **overrides,
    )
    assert plan.error is None, plan.error
    return plan, await service.run(plan.review_id, plan.plan.confirm_token)


# --- the sweep --------------------------------------------------------------


async def test_nothing_a_review_touched_is_stored_raw(build):
    """Goal, context, manifest, the reviewer's prose, and its structured fields --
    five different columns written by four different statements."""
    service = await build()
    plan, run = await run_a_review(service)
    assert run.status == "awaiting_synthesis"

    await service.finalize(
        plan.review_id,
        {
            "summary": f"the key {SECRET} is in the repo",
            "recommendation": f"rotate {OTHER}",
            "checked": [f"src/{OTHER}.py"],
            "not_checked": [],
        },
    )
    # And the round recorded afterwards, which is another host-written column on the
    # same row: a field added later is how a redaction rule quietly stops being true.
    await service.record_fix_round(plan.review_id, [], "applied", notes=f"rotated {SECRET}")
    await service.close()
    assert_absent(build.path, SECRET, OTHER)


async def test_a_credential_shaped_model_name_is_not_stored_and_still_runs(tmp_path, host_claude):
    """The reviewer snapshot is the one column written from configuration rather than
    from a model, and a model name is a free-form string like any other."""
    path = tmp_path / "consultations.sqlite3"
    config = ConsultConfig(
        database_path=str(path),
        agents={"codex-sol": agent("codex", OTHER, 10)},
        review={"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol"]},
    )
    service = await StubService(config, "claude").open()
    plan = await service.plan(goal="review the parser", context="def parse(): ...")
    assert plan.error is None, plan.error
    # Run it too: the snapshot is stored redacted and checked again at run, and the
    # consultation underneath stores the model in `target_model` and compares *that*
    # on resume. A plan-only test would pass while the review could never be run.
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    assert run.error is None, run.error
    # And again, through the retry path, which resumes the same consultation: the
    # resume compares the live model against the stored one to prove the agent id
    # was not reassigned, and the stored one is redacted. Comparing raw against
    # redacted reads as a reassignment, and every retry is refused.
    await service.store._run(
        lambda: service.store._db.execute(
            "UPDATE review_consultations SET status = 'failed', error_code = 'transport_error'"
        )
    )
    again = await service.retry(plan.review_id)
    assert again.error is None, again.error
    assert again.results[0].ok is True
    await service.close()

    assert_absent(path, OTHER)


async def test_a_crash_immediately_after_the_turn_leaves_nothing_raw(build, monkeypatch):
    """The promise is that the raw value never lands, not that a later pass cleans
    it up. A cleanup pass is only needed by a design that writes the secret first --
    and a process killed here would never run it."""
    from orchestrator_mcp.consult.store import ConsultStore

    service = await build()
    original = ConsultStore.record_turn

    async def record_then_die(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("killed right after the insert")

    monkeypatch.setattr(ConsultStore, "record_turn", record_then_die)

    _, run = await run_a_review(service)
    assert run.results[0].ok is False  # the crash became an envelope, as it must
    await service.close()

    # The turn row is on disk. The credential is not.
    values = every_value(build.path)
    assert any(table == "consultation_turns" for table, _, _ in values)
    assert_absent(build.path, SECRET, OTHER)


async def test_send_as_is_reaches_the_reviewer_and_still_never_reaches_the_disk(build):
    """`send_as_is` is a decision about what leaves the machine, not about what is
    logged. A user may knowingly approve a fixture's fake key; that is not a reason
    to keep it in our database forever."""
    seen: list[str] = []

    service = await build()
    inner = service.consult.adapter

    def watching(agent):
        adapter = inner(agent)
        start = adapter.start

        async def recording(agent, prompt, source_mode, session_id=None):
            seen.append(prompt.full_text)
            return await start(agent, prompt, source_mode, session_id)

        adapter.start = recording
        return adapter

    service.consult.adapter = watching  # type: ignore[method-assign]

    goal, context = f"is {SECRET} safe to ship", f"AUTH = {OTHER}"
    plan = await service.plan(goal=goal, context=context)
    run = await service.run(
        plan.review_id, plan.plan.confirm_token,
        secrets="send_as_is", raw={"goal": goal, "context": context},
    )

    assert run.status == "awaiting_synthesis"
    assert SECRET in seen[0] and OTHER in seen[0]  # the reviewer got the real thing
    await service.close()
    assert_absent(build.path, SECRET, OTHER)


async def test_the_host_ais_own_findings_are_scrubbed_too(build):
    """`host_findings` is written by the host AI, which has been reading the files
    the credential is in."""
    service = await build()
    config_review = service.config.review
    assert config_review is not None
    plan = await service.plan(mode="deep", goal="review it")
    await service.run(
        plan.review_id, plan.plan.confirm_token, host_findings=[f"line 4 hardcodes {SECRET}"]
    )
    await service.close()
    assert_absent(build.path, SECRET)


async def test_a_plan_that_was_never_run_is_stored_redacted(build):
    """The row is written at plan time, before any approval exists. A user who reads
    the preview and cancels must not have left the credential behind."""
    service = await build()
    plan = await service.plan(goal=f"is {SECRET} safe", context=f"AUTH = {OTHER}")

    assert [(h.field, h.line) for h in plan.plan.secret_hits] == [("goal", 1), ("context", 1)]
    assert SECRET not in plan.model_dump_json()  # positions, never values
    await service.close()
    assert_absent(build.path, SECRET, OTHER)


async def test_a_credential_read_off_disk_is_caught_and_never_stored(build, tmp_path):
    """`context_paths` is a second door into the same field. A scan that only sees
    what the host typed would let a credential in a file walk straight past it."""
    path = tmp_path / "settings.py"
    path.write_text(f"AUTH = {OTHER}\n")
    service = await build()

    plan = await service.plan(goal=f"is {SECRET} safe", context_paths=[str(path)])

    assert {h.field for h in plan.plan.secret_hits} == {"goal", "context"}
    assert OTHER not in plan.model_dump_json()
    run = await service.run(plan.review_id, plan.plan.confirm_token)
    assert run.status == "awaiting_synthesis"
    await service.close()
    assert_absent(build.path, SECRET, OTHER)


async def test_a_credential_in_an_error_message_is_scrubbed(build):
    """Adapter errors quote the command line, and a command line can carry a token."""
    service = await build()

    class Failing(LeakyAdapter):
        async def start(self, agent, prompt, source_mode, session_id=None):
            raise RuntimeError(f"spawn failed: --token {SECRET}")

    service.consult.adapter = lambda agent: Failing()  # type: ignore[method-assign]
    _, run = await run_a_review(service)

    assert run.results[0].ok is False
    assert SECRET not in run.model_dump_json()
    await service.close()
    assert_absent(build.path, SECRET, OTHER)


# --- the limit, stated rather than implied ----------------------------------


def test_the_reviewers_own_history_is_out_of_reach():
    """Redaction covers this database and nothing else. The CLIs keep their own
    logs -- this repo already reads Codex's -- and material sent to a reviewer lands
    there. Documented rather than implied, because a user who reads "redacted" as
    "erased everywhere" is relying on something no code here can deliver."""
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1] / "README.md"
    guardrail = readme.read_text()
    assert "Redaction covers every retained database copy" in guardrail
    assert "Vendor history is outside all of this" in guardrail
    assert "best-effort" in guardrail and "~/.codex/sessions/" in guardrail
    # The original material still reaches the vendor CLI and its own history; only
    # this database's retained copy is scrubbed. What is transmitted differs by
    # flow, so the README names the flows rather than making one blanket claim.
    assert "An ordinary consultation sends its original material" in guardrail
    assert "the Codex adapter opens the rollout file" in guardrail
