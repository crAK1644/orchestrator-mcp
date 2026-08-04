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
    assert 'sandbox_mode="read-only"' in argv
    # Both as config keys, not `--sandbox` / `--ask-for-approval`: a 0.146 build
    # rejects the approval flag on `exec` and the sandbox flag on `exec resume`.
    assert "--sandbox" not in argv and "--ask-for-approval" not in argv
    assert 'approval_policy="never"' in argv
    assert "agents.enabled=false" in argv and "features.shell_tool=false" in argv
    assert "web_search=disabled" in argv
    assert argv[-1] == "-"


async def test_the_configured_reasoning_effort_is_passed(tmp_path, monkeypatch, adapter):
    """`--ignore-user-config` means the user's own `model_reasoning_effort` is not
    inherited, so an unpassed effort silently runs at the model's default."""
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(reasoning_effort="xhigh"), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    assert 'model_reasoning_effort="xhigh"' in call["argv"]
    assert call["argv"][-1] == "-"  # still last, so the prompt still comes off stdin


async def test_no_reasoning_effort_passes_none(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    assert not any("model_reasoning_effort" in arg for arg in call["argv"])


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


# `codex exec resume` takes a strictly smaller flag set than `codex exec`: it has no
# `--sandbox`, and passing one kills the process during argument parsing, so a
# consultation would start fine and then fail on every follow-up. Only options both
# subcommands accept belong in the shared argv.
RESUME_REJECTS = ("--sandbox", "--ask-for-approval", "--full-auto", "--cd", "--color")


async def test_resume_sends_nothing_the_resume_subcommand_rejects(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.resume(agent(reasoning_effort="xhigh"), THREAD, prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    assert not [flag for flag in RESUME_REJECTS if flag in call["argv"]]


async def test_resume_is_configured_exactly_like_a_fresh_turn(tmp_path, monkeypatch, adapter):
    """Isolation is not something a follow-up turn gets to relax."""
    record = agent_stub.install(
        "codex", tmp_path, monkeypatch, runs=[{"stdout": transcript()}, {"stdout": transcript()}]
    )
    await adapter.start(agent(reasoning_effort="xhigh"), prompt(), SourceMode.MODEL)
    await adapter.resume(agent(reasoning_effort="xhigh"), THREAD, prompt(), SourceMode.MODEL)

    def options(argv: list[str]) -> list[str]:
        # The schema lives in a fresh scratch directory per turn, so its path differs
        # by design. Everything else is meant to be identical.
        return [arg for arg in argv if "consult-schema.json" not in arg]

    start, resumed = (options(call["argv"]) for call in agent_stub.calls(record))
    assert resumed[3:] == start[1:], "resume differs from start beyond `resume <thread>`"


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


SEARCH_EVENT = {"type": "item.completed", "item": {"type": "web_search_call", "query": "x"}}


@pytest.mark.parametrize("source_mode", [SourceMode.MODEL, SourceMode.DOCUMENT])
async def test_reaching_the_network_off_web_mode_is_a_protocol_violation(
    tmp_path, monkeypatch, adapter, source_mode
):
    """`web_search=disabled` is what stops the search; this is what notices when it
    does not. Without the second layer a document-mode answer could be sourced from
    the open web with nothing in the envelope saying so."""
    stdout = jsonl({"type": "thread.started", "thread_id": THREAD}, SEARCH_EVENT) + transcript()
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stdout}])

    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), source_mode)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_web_mode_is_allowed_to_search(tmp_path, monkeypatch, adapter):
    """The same event, in the one mode that asked for it."""
    stdout = jsonl({"type": "thread.started", "thread_id": THREAD}, SEARCH_EVENT) + transcript()
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stdout}])

    result = await adapter.start(agent(web_search=True), prompt(), SourceMode.WEB)
    assert result.content.answer


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


# --- which model actually answered ------------------------------------------

UUID_THREAD = "019fce6c-0218-7cb0-9e79-3de60901c110"


def rollout_path(home, thread_id=UUID_THREAD):
    """Where codex keeps the session log for a thread: dated directories three deep,
    the thread id in the filename."""
    directory = home / ".codex" / "sessions" / "2026" / "08" / "04"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"rollout-2026-08-04T23-16-52-{thread_id}.jsonl"


def turn_context(model: str, turn: int = 0) -> str:
    """One turn's record, the shape codex-cli 0.146 writes."""
    return json.dumps({"type": "turn_context", "payload": {"model": model, "turn": turn}}) + "\n"


def rollout(home, model, thread_id=UUID_THREAD, turns=1) -> None:
    """Write the session log codex keeps for its own resume support.

    Shaped like the real thing: a `session_meta` header, then one `turn_context` per
    turn naming the model."""
    lines = json.dumps({"type": "session_meta", "payload": {"session_id": thread_id}}) + "\n"
    lines += "".join(turn_context(model, index) for index in range(turns))
    rollout_path(home, thread_id).write_text(lines)


def silent_transcript() -> str:
    """What codex-cli 0.146 actually prints: no model, anywhere in the stream."""
    return jsonl(
        {"type": "thread.started", "thread_id": UUID_THREAD},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(CONTENT)},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 900, "output_tokens": 120}},
    )


async def test_a_substituted_model_is_caught_even_when_the_stream_never_names_one(
    tmp_path, monkeypatch, adapter
):
    """The whole point. `--json` on this build names no model, so the substitution
    check had nothing to check and waved every answer through -- an agent configured
    for Sol could be answered by anything and the envelope would still say Sol."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rollout(tmp_path, "gpt-5.6-luna")
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": silent_transcript()}])

    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE
    assert "gpt-5.6-luna" in str(exc.value)


async def test_the_session_log_confirms_the_configured_model(tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("HOME", str(tmp_path))
    rollout(tmp_path, "gpt-5.6-sol")
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": silent_transcript()}])

    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.model_used == "gpt-5.6-sol"


async def test_the_last_turn_is_the_one_checked(tmp_path, monkeypatch, adapter):
    """A resumed consultation appends to the same file. The turn that just ran is the
    last one, so an earlier turn on the configured model must not vouch for it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".codex" / "sessions" / "2026" / "08" / "04"
    directory.mkdir(parents=True)
    (directory / f"rollout-2026-08-04T23-16-52-{UUID_THREAD}.jsonl").write_text(
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}})
        + "\n"
        + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-luna"}})
        + "\n"
    )
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": silent_transcript()}])

    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE


async def test_no_session_log_is_not_a_failure(tmp_path, monkeypatch, adapter):
    """Absent metadata stays "no evidence", not "evidence of substitution". A codex
    build that stops writing these files, or a relocated `CODEX_HOME`, must not turn
    every consultation into an outage."""
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": silent_transcript()}])

    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.model_used == "gpt-5.6-sol", "the configured name, unverified"


async def test_the_stream_is_believed_before_the_file(tmp_path, monkeypatch, adapter):
    """If a later release does name the model on stdout, that is the live answer and
    the log is not consulted at all."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rollout(tmp_path, "gpt-5.6-luna")
    stdout = jsonl(
        {"type": "thread.started", "thread_id": UUID_THREAD, "model": "gpt-5.6-sol"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(CONTENT)},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 900, "output_tokens": 120}},
    )
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": stdout}])

    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.model_used == "gpt-5.6-sol"


# --- and that it answered for *this* turn ------------------------------------


async def test_an_earlier_turn_does_not_vouch_for_a_later_one(tmp_path, monkeypatch, adapter):
    """A resume appends to a log that already names a model. If the turn that just ran
    wrote nothing we recognise -- a renamed record, a moved field, a build that stopped
    writing these -- the answer is unverified, not whatever the previous turn said."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rollout_path(tmp_path).write_text(turn_context("gpt-5.6-luna"))
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": silent_transcript()}])

    result = await adapter.resume(agent(), UUID_THREAD, prompt(), SourceMode.MODEL)
    assert result.model_used == "gpt-5.6-sol", "the configured name, unverified"


async def test_the_turn_that_just_ran_is_the_one_read(tmp_path, monkeypatch, adapter):
    """The other direction: an earlier turn on the configured model must not cover for
    a substitution in the turn being checked."""
    monkeypatch.setenv("HOME", str(tmp_path))
    path = rollout_path(tmp_path)
    path.write_text(turn_context("gpt-5.6-sol"))
    agent_stub.install(
        "codex", tmp_path, monkeypatch,
        runs=[{"stdout": silent_transcript(), "append": {str(path): turn_context("gpt-5.6-luna", 1)}}],
    )

    with pytest.raises(AdapterError) as exc:
        await adapter.resume(agent(), UUID_THREAD, prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE
    assert "gpt-5.6-luna" in str(exc.value)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b'["turn_context", {"model": "gpt-5.6-luna"}]\n', id="a JSON array"),
        pytest.param(b'{"type": "turn_context", "payload": "gpt-5.6-luna"}\n', id="a string payload"),
        pytest.param(b'{"type": "turn_context", "payload": {"model": 4}}\n', id="a numeric model"),
        pytest.param(b'{"type": "turn_context", "payload": {"model": "\xff\xfe"}}\n', id="not UTF-8"),
        pytest.param(b'{"type": "turn_context", "payload": {\n', id="a half-written line"),
    ],
)
async def test_a_session_log_we_do_not_recognise_is_unverified_not_a_crash(
    tmp_path, monkeypatch, adapter, line
):
    """This file belongs to the CLI, and its format is undocumented. Every shape it
    could take has to land on "no metadata", never on an exception escaping mid-run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rollout_path(tmp_path).write_bytes(line)
    agent_stub.install("codex", tmp_path, monkeypatch, runs=[{"stdout": silent_transcript()}])

    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.model_used == "gpt-5.6-sol"
