"""The Codex adapter, against a stub executable on PATH.

Unlike the Claude side, nothing here was captured from a real binary: codex is not
installed on this machine, so the event names and flag spellings come from the
`codex exec` documentation and `smoke_consult_live.py` is what confirms them. What
these tests do pin regardless of that is the part that is ours -- the isolating
argv, the prompt going in over stdin, the refusal of an action event, and the
refusal of a substituted model.
"""

from __future__ import annotations

import json

import pytest

from orchestrator_mcp.consult.adapters.base import AdapterError
from orchestrator_mcp.consult.adapters.codex_cli import CodexCliAdapter
from orchestrator_mcp.consult.config import AgentConfig
from orchestrator_mcp.consult.contract import SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.prompts import compile_prompt

from .fixtures import agent_stub

CONTENT = {
    "answer": "blue",
    "assumptions": [],
    "uncertainties": [],
    "follow_up_questions": ["at what time of day"],
    "sources": [{"title": "model", "locator": "internal", "source_type": "model"}],
}

THREAD = "thread_01JABCDEF"


def jsonl(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def transcript(text: str | None = None, **overrides) -> str:
    return jsonl(
        {"type": "thread.started", "thread_id": THREAD, "model": "gpt-5.6-sol"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": text or json.dumps(CONTENT)},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 900, "output_tokens": 120}}
        | overrides,
    )


def agent(**overrides) -> AgentConfig:
    return AgentConfig(
        agent_id="codex-sol",
        runtime="codex",
        command="codex",
        model="gpt-5.6-sol",
        scores={"coding": 95},
        **overrides,
    )


def prompt(mode: SourceMode = SourceMode.MODEL):
    return compile_prompt("coding", mode, "what colour is the sky", None)


@pytest.fixture
def adapter():
    return CodexCliAdapter(timeout_s=30)


# --- preflight --------------------------------------------------------------


async def test_preflight_reads_the_exit_code_and_nothing_else(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "codex", tmp_path, monkeypatch,
        auth={"stdout": "Logged in as someone@example.com\n", "returncode": 0},
    )
    status = await adapter.preflight(agent())
    assert status.ready
    # The login output is not copied anywhere -- an address is not ours to keep.
    assert status.detail is None


async def test_a_logged_out_codex_is_installed_but_not_ready(tmp_path, monkeypatch, adapter):
    agent_stub.install("codex", tmp_path, monkeypatch, auth={"returncode": 1})
    status = await adapter.preflight(agent())
    assert (status.installed, status.authenticated) == (True, False)


async def test_preflight_on_a_missing_binary_says_so(tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on it at all
    assert not (await adapter.preflight(agent())).installed


# --- the invocation ---------------------------------------------------------


async def test_the_invocation_is_isolated(tmp_path, monkeypatch, adapter):
    """No user config, no rules, no shell, no subagents, no writes."""
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    argv = call["argv"]
    assert argv[0] == "exec"
    for flag in ("--strict-config", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert "agents.enabled=false" in argv and "features.shell_tool=false" in argv
    assert "web_search=disabled" in argv
    assert argv[-1] == "-"


async def test_the_prompt_goes_in_over_stdin_with_the_contract_first(tmp_path, monkeypatch, adapter):
    """Codex has no `--system-prompt`, so the contract travels with the payload --
    first, where a task cannot read as an instruction that replaces it."""
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    assert call["stdin"].startswith("You are a consultation endpoint")
    assert '"task": "what colour is the sky"' in call["stdin"]


async def test_the_output_schema_is_a_file_the_agent_can_read(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    path = call["argv"][call["argv"].index("--output-schema") + 1]
    # In the scratch directory, and gone with it: the schema is not left behind.
    assert path.endswith(".json")


async def test_web_mode_turns_search_on(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(web_search=True), prompt(SourceMode.WEB), SourceMode.WEB)

    (call,) = agent_stub.calls(record)
    assert "web_search=live" in call["argv"]


async def test_web_mode_without_web_search_configured_is_refused(tmp_path, monkeypatch, adapter):
    agent_stub.install("codex", tmp_path, monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(SourceMode.WEB), SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.WEB_SEARCH_UNAVAILABLE


async def test_a_successful_consultation_returns_content_thread_and_usage(
    tmp_path, monkeypatch, adapter
):
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.content.answer == "blue"
    assert result.content.follow_up_questions == ["at what time of day"]
    assert result.native_session_id == THREAD
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (900, 120)


async def test_resume_names_the_thread(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.resume(agent(), THREAD, prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    assert call["argv"][:3] == ["exec", "resume", THREAD]


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        {"type": "item.started", "item": {"type": "command_execution", "command": "rm -rf /"}},
        {"type": "item.completed", "item": {"type": "file_change", "path": "/etc/hosts"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "orchestrator"}},
        {"type": "subagent.started", "item": {"type": "subagent"}},
    ],
)
async def test_an_action_event_is_a_protocol_violation(tmp_path, monkeypatch, adapter, event):
    """A consultation reads and answers. Anything else means the isolation flags did
    not hold, and the answer is not worth having."""
    stdout = jsonl({"type": "thread.started", "thread_id": THREAD}, event) + transcript()
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stdout}])

    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_a_substituted_model_is_refused(tmp_path, monkeypatch, adapter):
    stdout = transcript().replace("gpt-5.6-sol", "gpt-5-mini")
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stdout}])
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE


async def test_a_reply_that_is_not_the_contract_fails_validation(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "codex", tmp_path, monkeypatch, runs=[{"stdout": transcript('{"answer": "blue"}')}]
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_no_events_at_all_is_a_transport_error(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "codex", tmp_path, monkeypatch,
        runs=[{"stdout": "", "stderr": "error: unexpected argument", "returncode": 2}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert "unexpected argument" in str(exc.value)


async def test_a_stream_with_no_final_message_is_a_transport_error(tmp_path, monkeypatch, adapter):
    stdout = jsonl({"type": "thread.started", "thread_id": THREAD}, {"type": "turn.completed"})
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stdout}])
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.TRANSPORT_ERROR


async def test_a_failed_turn_is_refused_even_with_an_answer_before_it(
    tmp_path, monkeypatch, adapter
):
    """A well-formed message earlier in the stream does not undo the CLI saying the
    turn failed -- that answer was abandoned, not delivered."""
    stdout = transcript() + jsonl({"type": "turn.failed", "error": "model overloaded"})
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stdout}])
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert "model overloaded" in str(exc.value)


async def test_a_nonzero_exit_is_refused_even_with_an_answer(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "codex", tmp_path, monkeypatch,
        runs=[{"stdout": transcript(), "stderr": "killed", "returncode": 1}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.AGENT_UNAVAILABLE


async def test_a_non_json_line_is_not_a_failure(tmp_path, monkeypatch, adapter):
    """A progress line on stdout is noise, not a protocol violation."""
    stdout = "Reading prompt from stdin...\n" + transcript()
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stdout}])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"
