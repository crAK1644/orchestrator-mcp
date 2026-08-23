"""The Claude Code adapter, against a stub executable on PATH.

The envelope and event shapes replayed here were captured from the real CLI
(2.1.220) during implementation, not invented: the same `result` keys, the same
`system`/`init` event, the same `modelUsage` map. What is being proved is that the
adapter builds the isolating argv, refuses a substituted model, fails closed on an
unexpected tool, and stops a web-mode run at its turn budget.
"""

from __future__ import annotations

import json

import pytest

from orchestrator_mcp.consult.adapters.base import AdapterError
from orchestrator_mcp.consult.adapters.claude_cli import ClaudeCliAdapter
from orchestrator_mcp.consult.config import AgentConfig
from orchestrator_mcp.consult.contract import SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.prompts import compile_prompt

from .fixtures import agent_stub

CONTENT = {
    "answer": "blue",
    "assumptions": [],
    "uncertainties": ["daylight assumed"],
    "follow_up_questions": [],
    "sources": [{"title": "supplied", "locator": "context", "source_type": "document"}],
}

SESSION = "11111111-2222-3333-4444-555555555555"


def envelope(**overrides) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 4210,
        "num_turns": 1,
        "result": json.dumps(CONTENT),
        "session_id": SESSION,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 1180, "output_tokens": 96, "cache_read_input_tokens": 0},
        "modelUsage": {"claude-opus-5": {"inputTokens": 1180, "outputTokens": 96}},
        "permission_denials": [],
    }
    return json.dumps(payload | overrides)


def agent(**overrides) -> AgentConfig:
    return AgentConfig(
        agent_id="claude-opus",
        runtime="claude",
        command="claude",
        model="opus",
        scores={"research": 90},
        **overrides,
    )


def prompt(mode: SourceMode = SourceMode.DOCUMENT):
    return compile_prompt("research", mode, "what colour is the sky", "the sky is blue")


@pytest.fixture
def adapter():
    return ClaudeCliAdapter(timeout_s=30, web_turn_limit=3)


# --- preflight --------------------------------------------------------------


async def test_preflight_reads_the_json_and_not_the_exit_code(tmp_path, monkeypatch, adapter):
    """`claude auth status --json` exits 0 logged out too, so the code proves nothing."""
    agent_stub.install("claude", tmp_path, monkeypatch, auth={"stdout": '{"loggedIn": false}'})
    status = await adapter.preflight(agent())
    assert (status.installed, status.authenticated, status.ready) == (True, False, False)


async def test_preflight_on_a_logged_in_cli_is_ready(tmp_path, monkeypatch, adapter):
    agent_stub.install("claude", tmp_path, monkeypatch, auth={"stdout": '{"loggedIn": true}'})
    assert (await adapter.preflight(agent())).ready


async def test_preflight_on_a_missing_binary_says_so_instead_of_raising(tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on it at all
    status = await adapter.preflight(agent())
    assert (status.installed, status.ready) == (False, False)
    assert "PATH" in status.detail


# --- the invocation ---------------------------------------------------------


async def test_a_document_consultation_disables_every_tool(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope()}])
    await adapter.start(agent(), prompt(), SourceMode.DOCUMENT, session_id=SESSION)

    (call,) = agent_stub.calls(record)
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" in argv and "--strict-mcp-config" in argv and "--no-chrome" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--session-id") + 1] == SESSION


async def test_the_schema_is_inline_json_not_a_path(tmp_path, monkeypatch, adapter):
    """Checked against the real binary: `--json-schema` takes the schema itself."""
    record = agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope()}])
    await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)

    (call,) = agent_stub.calls(record)
    schema = json.loads(call["argv"][call["argv"].index("--json-schema") + 1])
    assert set(schema["properties"]) == {
        "answer", "assumptions", "uncertainties", "follow_up_questions", "sources"
    }


async def test_the_contract_goes_through_system_prompt_and_the_task_through_stdin(
    tmp_path, monkeypatch, adapter
):
    record = agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope()}])
    compiled = prompt()
    await adapter.start(agent(), compiled, SourceMode.DOCUMENT)

    (call,) = agent_stub.calls(record)
    assert call["argv"][call["argv"].index("--system-prompt") + 1] == compiled.system
    assert json.loads(call["stdin"])["task"] == "what colour is the sky"


async def test_a_successful_consultation_returns_content_session_and_usage(
    tmp_path, monkeypatch, adapter
):
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope()}])
    result = await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)

    assert result.content.answer == "blue"
    assert result.content.uncertainties == ["daylight assumed"]
    assert result.native_session_id == SESSION
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (1180, 96)
    assert result.usage.cost_usd == 0.0123


async def test_the_cached_share_of_a_prompt_is_counted_in_the_total(
    tmp_path, monkeypatch, adapter
):
    """Live numbers from a resumed turn: 2 fresh input tokens, 2771 from cache.

    `input_tokens` alone made that turn look like 1323 tokens. It was nearer 4100, and
    the cost reported beside it was billed on the larger figure.
    """
    usage = {
        "input_tokens": 2,
        "output_tokens": 1321,
        "cache_read_input_tokens": 1334,
        "cache_creation_input_tokens": 1437,
    }
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope(usage=usage)}])

    result = await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)

    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (2, 1321)
    assert result.usage.total_tokens == 2 + 1321 + 1334 + 1437


async def test_a_boolean_cost_stays_unknown(tmp_path, monkeypatch, adapter):
    """`bool` is an `int`, but a malformed cost field is not a one-dollar charge."""
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope(total_cost_usd=True)}])
    result = await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)

    assert result.usage.cost_usd is None


async def test_resume_continues_the_same_session(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope()}])
    await adapter.resume(agent(), SESSION, prompt(), SourceMode.DOCUMENT)

    (call,) = agent_stub.calls(record)
    assert call["argv"][call["argv"].index("--resume") + 1] == SESSION
    assert "--session-id" not in call["argv"]


async def test_a_fenced_answer_is_still_a_valid_answer(tmp_path, monkeypatch, adapter):
    fenced = f"```json\n{json.dumps(CONTENT)}\n```"
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope(result=fenced)}])
    assert (await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)).content.answer == "blue"


# --- refusals ---------------------------------------------------------------


async def test_a_substituted_model_is_refused(tmp_path, monkeypatch, adapter):
    """The CLI's own fallback must not answer as a model nobody configured."""
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": envelope(modelUsage={"claude-haiku-4-5": {}})}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)
    assert exc.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE


async def test_a_model_family_name_still_matches_the_configured_alias(tmp_path, monkeypatch, adapter):
    """`opus` configured, `claude-opus-5` reported: the same model, not a substitution."""
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": envelope()}])
    assert (await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)).model_used == "claude-opus-5"


async def test_primary_usage_selects_opus_when_claude_also_reports_a_haiku_helper(
    tmp_path, monkeypatch, adapter
):
    """Current Claude Code reports internal helper usage beside the answering model."""
    model_usage = {
        "claude-haiku-4-5-20251001": {"inputTokens": 521, "outputTokens": 11},
        "claude-opus-5": {"inputTokens": 1180, "outputTokens": 96},
    }
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": envelope(modelUsage=model_usage)}],
    )

    result = await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)

    assert result.model_used == "claude-opus-5"


async def test_ambiguous_multi_model_usage_is_refused(tmp_path, monkeypatch, adapter):
    """Several reported models without one primary-usage match cannot be verified."""
    model_usage = {
        "claude-haiku-4-5": {"inputTokens": 10, "outputTokens": 2},
        "claude-opus-5": {"inputTokens": 20, "outputTokens": 4},
    }
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": envelope(modelUsage=model_usage)}],
    )

    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)

    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


# The three below carry token counts copied from live `claude` runs on 2.1.220, not
# invented ones. The question they answer is whether `modelUsage` covers one
# invocation or a whole session: if it accumulated, a resumed or multi-turn run would
# stop matching the top-level `usage` and be refused for being ambiguous. Observed:
# it is per invocation, and the top-level usage equals the answering model's entry in
# every shape below.


async def test_a_resumed_turn_reports_only_the_model_that_answered_it(
    tmp_path, monkeypatch, adapter
):
    """Live: the helper from turn one is absent from turn two's `modelUsage`.

    Per invocation, not cumulative -- so the resumed turn has one entry, and the
    match never has to disambiguate anything.
    """
    payload = json.loads(envelope())
    payload["usage"] = {
        "input_tokens": 2,
        "output_tokens": 1321,
        "cache_read_input_tokens": 1334,
        "cache_creation_input_tokens": 1437,
    }
    payload["modelUsage"] = {
        "claude-opus-5": {
            "inputTokens": 2,
            "outputTokens": 1321,
            "cacheReadInputTokens": 1334,
            "cacheCreationInputTokens": 1437,
        }
    }
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": json.dumps(payload)}])

    result = await adapter.resume(agent(), SESSION, prompt(), SourceMode.DOCUMENT)

    assert result.model_used == "claude-opus-5"


async def test_a_seven_turn_web_run_still_matches_the_answering_model(
    tmp_path, monkeypatch, adapter
):
    """Live: seven turns, and the helper's input tokens dwarf the answering model's.

    99411 against 494, because a search result lands in the helper's context. Size is
    exactly the wrong signal here, and the top-level usage picks the right entry
    without it.
    """
    payload = json.loads(envelope())
    payload["num_turns"] = 7
    payload["usage"] = {
        "input_tokens": 494,
        "output_tokens": 3093,
        "cache_read_input_tokens": 8002,
        "cache_creation_input_tokens": 4126,
    }
    payload["modelUsage"] = {
        "claude-haiku-4-5-20251001": {"inputTokens": 99411, "outputTokens": 729},
        "claude-opus-5": {"inputTokens": 494, "outputTokens": 3093},
    }
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": stream(INIT, assistant("searching"), payload)}],
    )

    result = await adapter.start(agent(web_search=True), prompt(SourceMode.WEB), SourceMode.WEB)

    assert result.model_used == "claude-opus-5"


async def test_an_overloaded_api_is_a_failure_not_a_model_substitution(
    tmp_path, monkeypatch, adapter
):
    """Live: a 529 left `modelUsage` holding the helper alone and the usage at zero.

    Read as metadata that would be a substitution -- the configured model nowhere in
    sight. It is not: nothing answered at all. The exit code is checked first, so the
    caller is told the agent was unavailable rather than told it swapped models.
    """
    payload = json.loads(envelope())
    payload["is_error"] = True
    payload["result"] = "API Error: 529 Overloaded."
    payload["usage"] = {"input_tokens": 0, "output_tokens": 0}
    payload["modelUsage"] = {
        "claude-haiku-4-5-20251001": {"inputTokens": 574, "outputTokens": 19}
    }
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": json.dumps(payload), "returncode": 1}],
    )

    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)

    assert exc.value.code is ConsultErrorCode.AGENT_UNAVAILABLE


async def test_missing_model_metadata_is_not_treated_as_substitution(tmp_path, monkeypatch, adapter):
    payload = json.loads(envelope())
    del payload["modelUsage"]
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": json.dumps(payload)}])
    assert (await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)).model_used == "opus"


async def test_a_reply_that_is_not_the_contract_fails_validation(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "claude", tmp_path, monkeypatch, runs=[{"stdout": envelope(result='{"answer": "blue"}')}]
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_prose_instead_of_json_fails_validation(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "claude", tmp_path, monkeypatch, runs=[{"stdout": envelope(result="the sky is blue")}]
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_an_error_envelope_is_agent_unavailable(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": envelope(is_error=True, result="Failed to authenticate")}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)
    assert exc.value.code is ConsultErrorCode.AGENT_UNAVAILABLE


async def test_a_nonzero_exit_is_refused_before_the_envelope_is_read(tmp_path, monkeypatch, adapter):
    """A CLI that exited nonzero said the run failed, and a well-formed envelope on
    stdout does not overrule it -- that answer was abandoned, not delivered."""
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": envelope(), "stderr": "rate limit", "returncode": 1}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)
    assert exc.value.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert "exited 1" in str(exc.value) and "rate limit" in str(exc.value)


async def test_no_envelope_at_all_is_a_transport_error(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": "not json", "stderr": "warming up"}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.DOCUMENT)
    assert exc.value.code is ConsultErrorCode.TRANSPORT_ERROR


async def test_web_mode_on_an_agent_without_web_search_is_refused(tmp_path, monkeypatch, adapter):
    agent_stub.install("claude", tmp_path, monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(SourceMode.WEB), SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.WEB_SEARCH_UNAVAILABLE


# --- web mode ---------------------------------------------------------------


def stream(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": SESSION,
    "tools": list(("WebSearch", "WebFetch")),
    "mcp_servers": [],
    "model": "claude-opus-5",
    "permissionMode": "auto",
    "apiKeySource": "none",
}


def assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


async def test_web_mode_enables_only_search_and_streams(tmp_path, monkeypatch, adapter):
    record = agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": stream(INIT, assistant("searching"), json.loads(envelope()))}],
    )
    result = await adapter.start(agent(web_search=True), prompt(SourceMode.WEB), SourceMode.WEB)

    (call,) = agent_stub.calls(record)
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # Rejected by the real CLI without it.
    assert "--verbose" in argv
    assert result.content.answer == "blue"


async def test_an_unexpected_tool_in_the_init_event_fails_closed(tmp_path, monkeypatch, adapter):
    """Our flags said search only. If the CLI enabled Bash anyway, stop."""
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": stream(INIT | {"tools": ["WebSearch", "Bash"]}, json.loads(envelope())),
               "sleep": 30}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(web_search=True), prompt(SourceMode.WEB), SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "Bash" in str(exc.value)


async def test_the_turn_budget_stops_a_runaway_search(tmp_path, monkeypatch, adapter):
    """No `--max-turns` exists, so the budget is ours: three assistant turns, then
    the child is killed rather than left spending the user's account."""
    endless = stream(INIT, *(assistant(f"turn {n}") for n in range(50)))
    agent_stub.install("claude", tmp_path, monkeypatch, runs=[{"stdout": endless, "sleep": 30}])

    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(web_search=True), prompt(SourceMode.WEB), SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "3-turn" in str(exc.value)


def test_a_runtime_picks_its_own_adapter():
    from orchestrator_mcp.consult.adapters import adapter_for
    from orchestrator_mcp.consult.config import ConsultConfig

    from .conftest import consult_block

    config = ConsultConfig(**consult_block())
    for agent_config in config.agents.values():
        assert adapter_for(agent_config, config).runtime == agent_config.runtime


def test_a_runtime_with_no_adapter_is_refused_rather_than_routed_to_codex():
    """`adapter_for` used to end in `return CodexCliAdapter(...)`, so a runtime added to
    the contract before its adapter exists would be consulted through the wrong CLI --
    wrong model, wrong flags, and an answer plausible enough to be believed."""
    from orchestrator_mcp.consult.adapters import adapter_for
    from orchestrator_mcp.consult.config import AgentConfig, ConsultConfig

    from .conftest import consult_block

    config = ConsultConfig(**consult_block())
    agent_config = AgentConfig(runtime="codex", command="x", model="m")
    # Assigned past validation on purpose: the `Runtime` literal is what stops this
    # reaching `adapter_for` today, and the point of the test is what happens on the
    # day a runtime is added to that literal before its adapter exists.
    agent_config.runtime = "someday"  # type: ignore[assignment]
    with pytest.raises(AdapterError) as exc:
        adapter_for(agent_config, config)
    assert exc.value.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert "someday" in str(exc.value)


async def test_a_web_run_that_never_answers_is_a_transport_error(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "claude", tmp_path, monkeypatch,
        runs=[{"stdout": stream(INIT), "stderr": "crashed", "returncode": 1}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(web_search=True), prompt(SourceMode.WEB), SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.TRANSPORT_ERROR
