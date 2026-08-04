"""Regressions for the defects an external review found in the consult path.

Each test here is a hole somebody could walk through before it was closed: a
consultation resumed into the host's own runtime, a smaller model answering under
a bigger one's name, a CLI that failed and was believed anyway, a child that could
talk until the server ran out of memory, and two threads sharing one SQLite
connection without agreeing whose transaction was open.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestrator_mcp.consult.adapters import base
from orchestrator_mcp.consult.adapters.base import (
    AdapterError,
    check_model,
    run_process,
    run_streaming,
)
from orchestrator_mcp.consult.adapters.claude_cli import ClaudeCliAdapter
from orchestrator_mcp.consult.adapters.codex_cli import CodexCliAdapter
from orchestrator_mcp.consult.config import AgentConfig, ConsultConfig
from orchestrator_mcp.consult.contract import MAX_CONTEXT_CHARS, MAX_PROMPT_CHARS, SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.prompts import compile_prompt
from orchestrator_mcp.consult.store import ConsultStore, StoreError

from .conftest import consult_block
from .fixtures import agent_stub
from .test_claude_adapter import CONTENT, envelope
from .test_consult_service import StubAdapter, StubService

import json


def claude_agent(**overrides) -> AgentConfig:
    fields = {
        "agent_id": "claude-opus", "runtime": "claude", "command": "claude",
        "model": "opus", "scores": {"research": 90}, "web_search": True,
    }
    return AgentConfig(**(fields | overrides))


# --- a substituted model ----------------------------------------------------


@pytest.mark.parametrize(
    "configured, reported",
    [
        # The fallback nobody notices: the smaller sibling's name *contains* the
        # bigger one, so plain containment waves it through.
        ("gpt-5", "gpt-5-mini"),
        ("gpt-5.6", "gpt-5.6-nano"),
        ("claude-opus-5", "claude-haiku-4-5"),
        ("opus", "sonnet"),
        # A version number has no end a substring can see: `gpt-5.1` sits inside
        # `gpt-5.10`, and `claude-sonnet-4` inside `claude-sonnet-4-5`.
        ("gpt-5.1", "gpt-5.10"),
        ("claude-sonnet-4", "claude-sonnet-4-5"),
        ("claude-opus-4-1", "claude-opus-4"),
        # A name with nothing nameable in it. It tokenizes to nothing, and an empty
        # token list matches every model as if it were an unversioned alias.
        ("gpt-5.6-sol", "???"),
        ("gpt-5.6-sol", "-"),
    ],
)
def test_a_smaller_sibling_is_not_the_configured_model(configured, reported):
    with pytest.raises(AdapterError) as exc:
        check_model(claude_agent(model=configured), reported)
    assert exc.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE


@pytest.mark.parametrize(
    "configured, reported",
    [
        ("opus", "claude-opus-5"),  # an operator's shorthand for the same model
        ("claude-opus-5", "claude-opus-5"),
        ("gpt-5.6-sol", "gpt-5.6-sol"),
        ("gpt-5-mini", "gpt-5-mini"),
        # A dated snapshot of the pinned model, which is the same model.
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
        ("opus", "claude-opus-4-1-20250805"),
    ],
)
def test_a_longer_spelling_of_the_same_model_is_still_that_model(configured, reported):
    assert check_model(claude_agent(model=configured), reported) == reported


def test_absent_metadata_is_still_not_evidence_of_substitution():
    """Deliberate: inventing a failure from a missing field would make every quiet
    release of either CLI an outage."""
    assert check_model(claude_agent(model="opus"), None) == "opus"


# --- a CLI that failed ------------------------------------------------------


async def test_claude_web_mode_refuses_a_tool_use_it_never_enabled(tmp_path, monkeypatch):
    """The init event can say `tools: []` and the stream can then use `Bash`. Only
    the second one is the agent actually acting."""
    stream = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "system", "subtype": "init", "tools": ["WebSearch", "WebFetch"]},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
            },
            json.loads(envelope()),
        ]
    )
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": stream}])
    adapter = ClaudeCliAdapter(timeout_s=30, web_turn_limit=3)

    with pytest.raises(AdapterError) as exc:
        await adapter.start(claude_agent(), compile_prompt("research", SourceMode.WEB, "q", None),
                            SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "Bash" in str(exc.value)


async def test_claude_web_mode_allows_the_two_tools_it_did_enable(tmp_path, monkeypatch):
    stream = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "system", "subtype": "init", "tools": ["WebSearch"]},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {}}]},
            },
            json.loads(envelope()),
        ]
    )
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": stream}])
    adapter = ClaudeCliAdapter(timeout_s=30, web_turn_limit=3)

    result = await adapter.start(
        claude_agent(), compile_prompt("research", SourceMode.WEB, "q", None), SourceMode.WEB
    )
    assert result.content.answer == CONTENT["answer"]


async def test_an_unnamed_tool_block_is_refused_rather_than_ignored(tmp_path, monkeypatch):
    stream = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "system", "subtype": "init", "tools": []},
            {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
            json.loads(envelope()),
        ]
    )
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": stream}])
    adapter = ClaudeCliAdapter(timeout_s=30, web_turn_limit=3)

    with pytest.raises(AdapterError) as exc:
        await adapter.start(claude_agent(), compile_prompt("research", SourceMode.WEB, "q", None),
                            SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


# --- credentials ------------------------------------------------------------


async def test_the_codex_preflight_does_not_read_the_auth_command_output(tmp_path, monkeypatch):
    """Its exit code is the whole answer, so its output goes to /dev/null rather
    than into this process where a traceback could carry it somewhere."""
    agent_stub.install(
        "codex", tmp_path, monkeypatch,
        auth={"stdout": "logged in as someone@example.com token=sk-secret", "returncode": 0},
    )
    agent = AgentConfig(agent_id="codex-sol", runtime="codex", command="codex", model="m")
    status = await CodexCliAdapter(timeout_s=30).preflight(agent)

    assert status.authenticated is True
    assert "sk-secret" not in str(status)
    assert "someone@example.com" not in str(status)


# --- a child that will not stop ---------------------------------------------


async def test_a_child_that_floods_stdout_is_killed_not_buffered(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "MAX_OUTPUT_BYTES", 4096)
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": "x" * 200_000}])

    with pytest.raises(AdapterError) as exc:
        await run_process([str(tmp_path / "bin" / "codex")], None, timeout_s=30)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_a_single_oversized_event_is_an_envelope_not_a_valueerror(tmp_path, monkeypatch):
    """`StreamReader` raises a bare `ValueError` for a line past its limit, and that
    would cross the MCP boundary as an exception instead of a code."""
    monkeypatch.setattr(base, "STREAM_LINE_LIMIT", 4096)
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": "y" * 200_000 + "\n"}])

    with pytest.raises(AdapterError) as exc:
        await run_streaming(
            [str(tmp_path / "bin" / "claude")], None, timeout_s=30, on_line=lambda _: True
        )
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


# --- the store --------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return ConsultStore(tmp_path / "consultations.sqlite3")


async def test_two_first_calls_can_open_the_store_at_once(store):
    """The MCP tools open on first use, so two simultaneous first calls both find
    no connection and race to create the schema."""
    await asyncio.gather(*(store.open() for _ in range(8)))
    assert store._connection is not None
    await store.close()


async def test_concurrent_transactions_do_not_collide_on_the_shared_connection(store):
    """One connection, many worker threads: without a lock the second `BEGIN
    IMMEDIATE` lands inside the first one's transaction."""
    await store.open()

    async def take(index: int) -> None:
        async with store.lease(f"consultation-{index}"):
            await asyncio.sleep(0)

    await asyncio.gather(*(take(i) for i in range(16)))
    await store.close()


async def test_a_stale_release_cannot_free_somebody_else_s_lease(store):
    """After an expiry the same process can hold the next lease on the same
    consultation, so a release keyed on the process would delete the wrong row."""
    await store.open()
    await store._run(lambda: store._acquire("c1", 0.0, "token-1"))  # expires immediately
    await store._run(lambda: store._acquire("c1", 300.0, "token-2"))
    await store._run(lambda: store._release("c1", "token-1"))

    with pytest.raises(StoreError) as exc:
        await store._run(lambda: store._acquire("c1", 300.0, "token-3"))
    assert exc.value.code is ConsultErrorCode.SESSION_BUSY
    await store.close()


# --- the service ------------------------------------------------------------


@pytest.fixture
def rebind(tmp_path, host_claude):
    """A consultation created against one config, continued against another."""

    async def run(**second_agent):
        first = ConsultConfig(**consult_block(database_path=str(tmp_path / "c.sqlite3")))
        service = await StubService(first, "claude", adapter=StubAdapter()).open()
        response = await service.consult(capability="coding", prompt="q")
        assert response.ok

        second = ConsultConfig(
            **consult_block(
                database_path=str(tmp_path / "c.sqlite3"),
                agents={"codex-sol": second_agent},
            )
        )
        moved = await StubService(second, "claude", adapter=StubAdapter()).open()
        return await moved.consult(
            capability="coding", prompt="q2", consultation_id=response.consultation_id
        )

    return run


async def test_a_consultation_cannot_be_resumed_into_the_host_runtime(rebind):
    """The agent id survived a config edit; the agent behind it did not. Resuming
    would hand the work straight back to the host."""
    response = await rebind(runtime="claude", command="claude", model="opus",
                            scores={"coding": 90})
    assert response.ok is False
    assert response.error.code is ConsultErrorCode.SESSION_TARGET_MISMATCH


async def test_a_consultation_cannot_be_resumed_onto_a_different_model(rebind):
    response = await rebind(runtime="codex", command="codex", model="gpt-5-mini",
                            scores={"coding": 90})
    assert response.ok is False
    assert response.error.code is ConsultErrorCode.SESSION_TARGET_MISMATCH


async def test_the_lease_outlives_the_turn_it_guards(tmp_path, host_claude):
    """A fixed lease shorter than a configured timeout expires under a consultation
    that is still running, and lets a second caller in beside it."""
    config = ConsultConfig(
        **consult_block(database_path=str(tmp_path / "c.sqlite3"), timeout_s=900)
    )
    service = StubService(config, "claude", adapter=StubAdapter())
    assert service._lease_ttl() > config.timeout_s

    taken: list[float] = []
    original = service.store.lease

    def record(consultation_id, ttl_s=None):
        taken.append(ttl_s)
        return original(consultation_id, ttl_s)

    service.store.lease = record  # type: ignore[method-assign]
    await service.open()
    await service.consult(capability="coding", prompt="q")
    assert taken == [service._lease_ttl()]


@pytest.mark.parametrize(
    "field, value",
    [
        ("prompt", "x" * (MAX_PROMPT_CHARS + 1)),
        ("context", "x" * (MAX_CONTEXT_CHARS + 1)),
        ("conversation_label", "x" * 500),
    ],
)
async def test_free_text_is_capped_before_it_is_copied_five_times(
    tmp_path, host_claude, field, value
):
    """Each of these is compiled into a prompt, encoded onto a child's stdin, and
    stored, so an uncapped field is several times its own size before anything
    refuses it."""
    config = ConsultConfig(**consult_block(database_path=str(tmp_path / "c.sqlite3")))
    service = await StubService(config, "claude", adapter=StubAdapter()).open()
    response = await service.consult(**{"capability": "coding", "prompt": "q", field: value})

    assert response.ok is False
    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert response.consultation_id is None


# --- the web turn budget ----------------------------------------------------


def web_stream(turns: int, *, result: bool = True) -> str:
    events = [{"type": "system", "subtype": "init", "tools": ["WebSearch", "WebFetch"]}]
    events += [{"type": "assistant", "message": {"content": []}} for _ in range(turns)]
    if result:
        events.append(json.loads(envelope()))
    return "\n".join(json.dumps(event) for event in events) + "\n"


@pytest.mark.parametrize("limit", [1, 3])
async def test_the_turn_that_spends_the_last_of_the_budget_still_answers(
    tmp_path, monkeypatch, limit
):
    """Stopping *at* the limit would kill the child one event before the answer it had
    already produced, and make `web_turn_limit: 1` mean no web consultation at all."""
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": web_stream(limit)}])
    adapter = ClaudeCliAdapter(timeout_s=30, web_turn_limit=limit)

    result = await adapter.start(claude_agent(), compile_prompt(
        capability="research", source_mode=SourceMode.WEB, task="q", context=None), SourceMode.WEB)
    assert result.content.answer == CONTENT["answer"]


async def test_a_turn_past_the_budget_is_stopped_without_an_answer(tmp_path, monkeypatch):
    agent_stub.install(
        "claude", tmp_path, monkeypatch, runs=[{"stdout": web_stream(4, result=False)}]
    )
    adapter = ClaudeCliAdapter(timeout_s=30, web_turn_limit=3)

    with pytest.raises(AdapterError) as exc:
        await adapter.start(claude_agent(), compile_prompt(
            capability="research", source_mode=SourceMode.WEB, task="q", context=None), SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "3-turn web budget" in str(exc.value)


# --- a child that floods the other pipe -------------------------------------


async def test_a_streaming_child_cannot_spend_the_output_cap_twice(tmp_path, monkeypatch):
    """stderr is drained in the background so it cannot deadlock stdout. Draining it
    without a cap would let a child send the whole budget again down a stream nobody
    is even parsing."""
    monkeypatch.setattr(base, "MAX_OUTPUT_BYTES", 4096)
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": "{}\n", "stderr": "x" * 200_000}],
    )

    with pytest.raises(AdapterError) as exc:
        await run_streaming(
            [str(tmp_path / "bin" / "claude")], None, timeout_s=30, on_line=lambda _: True
        )
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


# --- nothing escapes as an exception ----------------------------------------


async def test_an_unusable_database_path_is_an_envelope_not_a_traceback(tmp_path, host_claude):
    """Opening the store happens inside the boundary: a `database_path` under a
    regular file raises `FileExistsError`, which crossed the MCP boundary bare."""
    (tmp_path / "afile").write_text("not a directory")
    config = ConsultConfig(**consult_block(database_path=str(tmp_path / "afile" / "c.sqlite3")))
    service = StubService(config, "claude", adapter=StubAdapter())

    response = await service.consult(capability="coding", prompt="q")

    assert response.ok is False
    assert response.error.code is ConsultErrorCode.TRANSPORT_ERROR
    assert response.content is None
    # The type and nothing else: these messages are quoted back to a caller that may
    # not be the operator, and an operational exception carries paths.
    assert str(tmp_path) not in response.error.message


async def test_a_credential_in_an_error_message_is_redacted(tmp_path, host_claude):
    """Second line of defence. A CLI that echoes its own argv into stderr, or a
    provider that quotes the request it rejected, puts a token where a caller reads."""
    config = ConsultConfig(**consult_block(database_path=str(tmp_path / "c.sqlite3")))
    adapter = StubAdapter(error=AdapterError(
        ConsultErrorCode.AGENT_UNAVAILABLE,
        "the agent exited 1: Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDD rejected",
    ))
    service = await StubService(config, "claude", adapter=adapter).open()

    response = await service.consult(capability="coding", prompt="q")

    assert response.ok is False
    assert "sk-ant-api03" not in response.error.message
    assert "[redacted]" in response.error.message
