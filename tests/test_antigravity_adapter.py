"""The Antigravity adapter, against a stub executable on PATH.

The event shapes here were captured from a real `agy` 1.1.10; the flag spellings come
from its own `--help`. What these tests pin is the part that is ours -- the fixtures
below are the record of those shapes now: the prompt reaching argv intact even when
it has to be split, the schema file being cleaned up, and the four ways this runtime
can hand back a non-answer that looks like a success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator_mcp.consult.adapters import antigravity_cli
from orchestrator_mcp.consult.adapters.antigravity_cli import (
    MAX_ARG_BYTES,
    AntigravityCliAdapter,
)
from orchestrator_mcp.consult.adapters.base import AdapterError
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

CONV = "6f0903c5-2f1e-4b3a-9f77-2a1d0c8e5b42"
MODEL = "gemini-3.1-pro-high"

# Every builtin `agy` advertises on every run, denied or not. Present in the fixture on
# purpose: a check on this list rather than on tool *use* would refuse every run.
BUILTIN_TOOLS = ["run_command", "write_to_file", "call_mcp_tool", "browser_navigate", "view_file"]


def jsonl(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def init(model: str = MODEL) -> dict:
    return {
        "event": "init",
        "conversation_id": CONV,
        "init": {"model": model, "cwd": "/tmp/scratch", "tools": BUILTIN_TOOLS},
    }


def result_event(**overrides) -> dict:
    body = {
        "conversation_id": CONV,
        "status": "SUCCESS",
        # Prose *and* the object, which is what the real runtime returns and why the
        # adapter must read `structured_output` instead.
        "response": "Here is the answer.\n" + json.dumps(CONTENT),
        "structured_output": CONTENT,
        "usage": {"input_tokens": 900, "output_tokens": 120, "thinking_tokens": 30,
                  "cache_read_tokens": 800, "total_tokens": 1020},
    } | overrides
    return {"event": "result", "result": body}


def transcript(model: str = MODEL, **overrides) -> str:
    return jsonl(
        init(model),
        {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE",
                                                 "step_type": "agent_response",
                                                 "text_delta": "..."}},
        result_event(**overrides),
    )


def ack(index: int) -> str:
    """A fragment turn: acknowledged, no structured output, no schema was sent."""
    return jsonl(init(), result_event(response=f"ACK {index}", structured_output=None))


def agent(**overrides) -> AgentConfig:
    return AgentConfig(
        agent_id="gemini-reviewer",
        runtime="antigravity",
        command="agy",
        model=MODEL,
        scores={"reasoning": 90},
        **overrides,
    )


def prompt(mode: SourceMode = SourceMode.MODEL, context: str | None = None):
    return compile_prompt("reasoning", mode, "what colour is the sky", context)


def argv_of(call: dict) -> list[str]:
    return call["argv"]


def prompt_arg(call: dict) -> str:
    """The `-p` value, which is the only place this runtime accepts a prompt."""
    argv = call["argv"]
    return argv[argv.index("-p") + 1]


@pytest.fixture
def adapter():
    return AntigravityCliAdapter(timeout_s=30)


# --- preflight --------------------------------------------------------------


async def test_preflight_on_a_missing_binary_says_so(tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("PATH", str(tmp_path))
    status = await adapter.preflight(agent())
    assert not status.installed


async def test_preflight_reports_readiness_it_cannot_verify_and_says_which(
    tmp_path, monkeypatch, adapter
):
    """This runtime has no auth probe -- no `login status`, and `agy models` exits 0
    logged out or not. Reporting `authenticated=False` would make every working agent
    look broken, so it claims ready and the detail carries the caveat."""
    record = agent_stub.install("agy", tmp_path, monkeypatch)
    status = await adapter.preflight(agent())

    assert status.ready
    assert "not verifiable" in (status.detail or "")
    # And nothing was run to find that out: there is no command whose exit code would
    # have meant anything, so none was spent against the user's account.
    assert agent_stub.calls(record) == [] and agent_stub.calls(record, auth=True) == []


def test_the_connect_command_is_the_bare_binary(adapter):
    """`agy` has no login verb at all: it authenticates on its first interactive
    launch, and logging out is the in-session `/logout`."""
    assert adapter.connect_command(agent()) == "agy"


# --- the invocation ---------------------------------------------------------


async def test_the_invocation_is_isolated(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    argv = argv_of(call)
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--model") + 1] == MODEL
    assert "--sandbox" in argv
    assert "--disable-slash-commands" in argv
    # The one flag that would let a consultation run commands on the user's machine.
    assert "--dangerously-skip-permissions" not in argv
    # And the one that is a hard error next to any effort-bearing slug, which every
    # model this runtime offers is.
    assert "--effort" not in argv


async def test_stdin_is_not_used_because_this_runtime_ignores_it(tmp_path, monkeypatch, adapter):
    """The transport invariant every other adapter here relies on does not hold: `-p ""`
    is an error and `-p -` answers the dash. The prompt has to be in argv."""
    record = agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    assert call["stdin"] == ""
    assert prompt_arg(call).startswith("You are a consultation endpoint")
    assert '"task": "what colour is the sky"' in prompt_arg(call)


async def test_our_credentials_do_not_travel_to_this_runtime(tmp_path, monkeypatch, adapter):
    """It authenticates from the OS keyring under `HOME`, and this server's own keys --
    including the Google ones it would happily use -- are none of its business."""
    for name in (
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, f"secret-{name}")
    record = agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    child = call["env"]
    assert not [name for name in child if "secret-" in child[name]]
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in child
    assert "HOME" in child, "the keyring lives there; removing it would break every login"


async def test_the_schema_file_is_gone_when_the_run_is(tmp_path, monkeypatch, adapter):
    """`--json-schema` takes a path, so one gets written. It lives in the scratch
    directory and leaves with it -- a consultation does not litter."""
    record = agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    argv = argv_of(call)
    schema = Path(argv[argv.index("--json-schema") + 1])
    assert schema.name.endswith(".json")
    assert not schema.exists() and not schema.parent.exists()


async def test_a_successful_consultation_returns_content_conversation_and_usage(
    tmp_path, monkeypatch, adapter
):
    agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.content.answer == "blue"
    assert result.content.follow_up_questions == ["at what time of day"]
    assert result.native_session_id == CONV
    assert result.model_used == MODEL
    assert result.model_verified, "`init` named the model, so this is checked not assumed"
    # Thinking is generated and billed, so it lands on the completion side.
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (900, 150)


async def test_the_advertised_tool_list_is_not_itself_a_violation(tmp_path, monkeypatch, adapter):
    """`init.tools` always carries the full builtin surface, denied or not. A check on
    availability rather than on use would refuse every consultation this runtime runs."""
    agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.content.answer == "blue"


async def test_resume_continues_the_same_conversation(tmp_path, monkeypatch, adapter):
    record = agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.resume(agent(), CONV, prompt(), SourceMode.MODEL)

    (call,) = agent_stub.calls(record)
    argv = argv_of(call)
    assert argv[argv.index("--conversation") + 1] == CONV


async def test_a_fresh_start_names_no_conversation(tmp_path, monkeypatch, adapter):
    """The id is the runtime's to assign. Whether `--conversation` accepts one for a
    conversation that does not exist yet is unverified, and guessing wrong would either
    fail every first turn or start a session bound to nothing."""
    record = agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": transcript()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL, session_id="ours-not-theirs")

    (call,) = agent_stub.calls(record)
    assert "--conversation" not in argv_of(call)
    assert "ours-not-theirs" not in argv_of(call)


@pytest.mark.parametrize("web_search", [False, True])
async def test_web_mode_is_refused_however_the_agent_is_configured(
    tmp_path, monkeypatch, adapter, web_search
):
    """This adapter does not offer web mode, so `web_search: true` on an antigravity
    agent must still be refused -- otherwise a model-mode answer is returned under a
    web-mode contract, with sources it never went and got."""
    agent_stub.install("agy", tmp_path, monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(web_search=web_search), prompt(SourceMode.WEB), SourceMode.WEB)
    assert exc.value.code is ConsultErrorCode.WEB_SEARCH_UNAVAILABLE


# --- a prompt too large for one argument ------------------------------------


def big_prompt():
    """Large enough to need splitting: Linux caps a single argv value at 128 KiB, well
    under this project's 1 MB context limit."""
    return prompt(SourceMode.DOCUMENT, context="lorem ipsum dolor sit amet. " * 9000)


def reassemble(calls: list[dict]) -> str:
    """The fragments as the model is asked to put them back together."""
    body = ""
    for call in calls:
        text = prompt_arg(call)
        if "--- BEGIN PART" not in text:
            continue
        body += text.split("---\n", 1)[1].rsplit("\n--- END PART", 1)[0]
    return body


async def test_an_oversized_prompt_is_split_across_turns_of_one_conversation(
    tmp_path, monkeypatch, adapter
):
    compiled = big_prompt()
    record = agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": ack(1)}, {"stdout": ack(2)}, {"stdout": ack(3)},
              {"stdout": transcript()}],
    )
    result = await adapter.start(agent(), compiled, SourceMode.MODEL)
    calls = agent_stub.calls(record)

    assert len(calls) > 2, "this prompt is meant to need splitting"
    # Nothing is lost or reordered in the split: what the model is handed, in order, is
    # the message that was compiled.
    assert reassemble(calls) == compiled.full_text
    # Every argument stays under the ceiling that forced the split, wrapper included.
    assert all(len(prompt_arg(call).encode()) < 131072 for call in calls)
    # One conversation: the first turn opens it, every later turn names it.
    assert "--conversation" not in argv_of(calls[0])
    assert all(argv_of(call)[argv_of(call).index("--conversation") + 1] == CONV
               for call in calls[1:])
    # The schema belongs to the turn that is meant to produce JSON, not to the ones
    # meant to produce an ACK.
    assert [("--json-schema" in argv_of(call)) for call in calls] == [False] * (len(calls) - 1) + [True]
    # One deadline for the whole consultation: what each turn hands the CLI is what is
    # left of it, so the values only ever shrink.
    budgets = [argv_of(call)[argv_of(call).index("--print-timeout") + 1] for call in calls]
    assert budgets == sorted(budgets, key=lambda value: -int(value.rstrip("s")))
    assert result.content.answer == "blue"


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 7, 11])
@pytest.mark.parametrize("text", ["a" * 10, "é" * 10, "🙂" * 10, "aé🙂" * 7, "🙂a🙂"])
def test_the_split_terminates_and_loses_nothing_however_narrow_it_is(text, limit):
    """The back-off that keeps a fragment from ending mid-character can reach the start
    of the one it began at -- when a character is wider than the whole limit. That used
    to emit an empty fragment and advance nothing, which is a loop with no end to it.

    Unreachable at `MAX_ARG_BYTES`, where the limit is 100 KB and the widest character
    is 4 bytes. Tested at the limits that reach it so lowering that constant is a
    smaller fragment rather than a hang."""
    parts = antigravity_cli._fragments(text, limit)
    assert "".join(parts) == text
    assert "" not in parts, "an empty part is the shape of the loop that never ended"
    # One character may exceed the limit -- the only alternative to it is no progress.
    assert all(len(part.encode()) <= limit or len(part) == 1 for part in parts)


async def test_a_fragment_the_agent_never_acknowledged_stops_the_consultation(
    tmp_path, monkeypatch, adapter
):
    """Exit 0, `SUCCESS`, empty response is what an auto-denied tool call looks like.
    Continuing would build an answer on a part of the prompt that never landed."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": jsonl(init(), result_event(response="", structured_output=None))}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), big_prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "incomplete" in str(exc.value)


async def test_the_turns_of_a_chunked_consultation_are_all_billed(tmp_path, monkeypatch, adapter):
    """Each turn re-sends the history and is charged for it, cached or not. Reporting
    only the last turn would understate a chunked consultation several times over."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": ack(1)}, {"stdout": ack(2)}, {"stdout": ack(3)},
              {"stdout": transcript()}],
    )
    result = await adapter.start(agent(), big_prompt(), SourceMode.MODEL)
    assert result.usage.prompt_tokens == 900 * 4
    assert result.usage.completion_tokens == 150 * 4


# --- refusals ---------------------------------------------------------------


async def test_a_tool_step_is_a_protocol_violation(tmp_path, monkeypatch, adapter):
    """A consultation reads and answers. Acting means the permission default this
    runtime depends on did not hold, and the answer is not worth having."""
    stdout = jsonl(
        init(),
        {"event": "step_update", "step_update": {"step_index": 3, "state": "ACTIVE",
                                                 "step_type": "tool", "tool_name": "run_command",
                                                 "tool_info": {"parameters": {"CommandLine": "id"}}}},
    ) + transcript()
    agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": stdout}])

    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "run_command" in str(exc.value)


@pytest.mark.parametrize(
    "structured", [None, {}, ""], ids=["null", "an empty object", "an empty string"]
)
async def test_a_success_with_no_structured_output_is_not_an_answer(
    tmp_path, monkeypatch, adapter, structured
):
    """The landmine: a denied tool ends the run at exit 0 with `SUCCESS`, an empty
    response and no structured output. Terminal status is not trustworthy on its own."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": jsonl(init(), result_event(response="", structured_output=structured))}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_the_prose_response_is_never_read_as_the_answer(tmp_path, monkeypatch, adapter):
    """`response` carries the answer wrapped in prose, which is exactly what
    `parse_content` refuses. Falling back to it would trade a clean failure for a
    parse error on every run that produced no structured output."""
    envelope = result_event(structured_output=None)
    agent_stub.install(
        "agy", tmp_path, monkeypatch, runs=[{"stdout": jsonl(init(), envelope)}]
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert "no structured output" in str(exc.value)


async def test_an_error_result_is_refused_even_though_it_arrives_on_stdout(
    tmp_path, monkeypatch, adapter
):
    """Failures come back as a `result` event with an empty stderr, so a failure path
    that read stderr would report nothing at all."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{
            "stdout": jsonl(result_event(status="ERROR", response="", structured_output=None,
                                         error="model gemini-9.9 is not recognized")),
            "stderr": "",
            "returncode": 1,
        }],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert "not recognized" in str(exc.value)


async def test_an_authentication_failure_is_reported_without_quoting_it(
    tmp_path, monkeypatch, adapter
):
    """This server never reads, stores or parses this runtime's credential. A failed
    login is the one message where a fragment of one could arrive and then be stored
    verbatim beside every other consultation, so the text is dropped."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{
            "stdout": jsonl(result_event(
                status="ERROR", response="", structured_output=None,
                error="unauthenticated: refresh credential ya29.A0eXAMPLE has expired",
            )),
            "returncode": 1,
        }],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert exc.value.code is ConsultErrorCode.CONNECTION_REQUIRED
    assert "ya29.A0eXAMPLE" not in str(exc.value)
    assert exc.value.required_action is not None
    assert exc.value.required_action.command == "agy"


async def test_a_transcript_that_carries_a_credential_is_not_the_part_that_is_kept(
    tmp_path, monkeypatch, adapter
):
    """`raw_output` is stored and rendered in the dashboard, and the paths that drop this
    runtime's text are the failing ones -- so a *successful* run that printed a
    credential would file one. The answer survives; the transcript does not."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": transcript() + jsonl(
            {"event": "notice", "text": "refreshed ya29.A0eXAMPLEtokenvalue1234"})}],
    )
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.content.answer == "blue"
    assert "ya29" not in result.raw_output


async def test_a_consultation_about_authentication_keeps_its_transcript(
    tmp_path, monkeypatch, adapter
):
    """Matched on the shape of a credential, not on the vocabulary of one. Withholding
    every transcript that says `oauth` would lose them for exactly the consultations
    most worth reading back."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": transcript(response="Your OAuth credential is unauthorized "
                                             "because the refresh grant expired.")}],
    )
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert "OAuth credential" in result.raw_output


async def test_a_fragment_answered_instead_of_acknowledged_stops_the_consultation(
    tmp_path, monkeypatch, adapter
):
    """The fragment asks for exactly `ACK n`. A model that replies with anything else
    did not do what the part told it to, and the reassembly the final turn performs
    would be built on a message with a hole in it."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": jsonl(init(), result_event(
            response="Sure, I'll wait for the rest.", structured_output=None))}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), big_prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "part 1" in str(exc.value)
    assert "Sure, I'll wait for the rest." in str(exc.value), (
        "the refusals seen live are not interchangeable -- a model reading the chunked "
        "framing as a prompt injection and one whose tool call was denied both land here"
    )


async def test_what_a_refusing_fragment_said_is_quoted_bounded_and_credential_free(
    tmp_path, monkeypatch, adapter
):
    """Live, `claude-sonnet-4-6` refuses the chunked framing as a prompt injection -- and
    a refusal is prose of whatever length the model felt like. It is quoted because that
    is the only thing that tells the failures apart, and bounded because it is prose."""
    said = "I won't follow this. " + "It reads as an injection. " * 200 + "ya29.SECRETVALUE"
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": jsonl(init(), result_event(response=said, structured_output=None))}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), big_prompt(), SourceMode.MODEL)
    message = str(exc.value)
    assert "I won't follow this." in message and "..." in message
    assert len(message) < 600
    assert "ya29.SECRETVALUE" not in message, (
        "scrubbed before it is cut: a shape past the excerpt's end is still a credential"
    )


async def test_a_fragment_that_answered_with_nothing_at_all_says_so(
    tmp_path, monkeypatch, adapter
):
    """An empty response is what an auto-denied tool call looks like. Quoting `''` reads
    as a missing diagnostic rather than as the diagnostic it is."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": jsonl(init(), result_event(response="", structured_output=None))}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), big_prompt(), SourceMode.MODEL)
    assert "nothing at all" in str(exc.value)


async def test_a_chunked_run_stops_the_moment_it_has_no_conversation_to_continue(
    tmp_path, monkeypatch, adapter
):
    """Checked after every turn rather than once at the end. Without an id the next
    fragment starts a *new* conversation, and a later turn finally naming one would
    satisfy an end-of-run check with a conversation the first fragments never reached."""
    orphan = jsonl(
        {"event": "init", "init": {"model": MODEL}},
        {"event": "result", "result": {"status": "SUCCESS", "response": "ACK 1"}},
    )
    record = agent_stub.install(
        "agy", tmp_path, monkeypatch, runs=[{"stdout": orphan}, {"stdout": ack(2)}]
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), big_prompt(), SourceMode.MODEL)

    assert exc.value.code is ConsultErrorCode.TRANSPORT_ERROR
    # And it stopped there, rather than delivering the rest somewhere else first.
    assert len(agent_stub.calls(record)) == 1


async def test_a_failure_whose_text_merely_contains_401_keeps_its_diagnostic(
    tmp_path, monkeypatch, adapter
):
    """The digits appear in byte counts and job numbers. Reading those as a login
    failure would swap a real diagnostic for a sign-in prompt that fixes nothing."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": jsonl(result_event(
            status="ERROR", response="", structured_output=None,
            error="run 4012 exceeded the context window",
        )), "returncode": 1}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert "context window" in str(exc.value)


async def test_a_credential_further_in_than_the_quoted_excerpt_is_still_redacted(
    tmp_path, monkeypatch, adapter
):
    """The word that identifies the failure and the credential need not be near each
    other, so the scan runs on the whole of both streams and the truncation happens
    only for the message that is allowed to be recorded."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stderr": "starting\n" + "trace line\n" * 60
                         + "unauthorized: ya29.A0eXAMPLE was rejected", "returncode": 1}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.CONNECTION_REQUIRED
    assert "ya29.A0eXAMPLE" not in str(exc.value)


async def test_token_counts_that_are_not_numbers_do_not_lose_the_answer(
    tmp_path, monkeypatch, adapter
):
    """Usage is reporting. An answer that arrived and validated must not be thrown away
    because the runtime wrote `N/A` where a count belonged."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": transcript(usage={"input_tokens": "N/A", "output_tokens": None,
                                           "total_tokens": ["?"]})}],
    )
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.content.answer == "blue"
    assert result.usage.total_tokens == 0


async def test_a_login_failure_before_any_protocol_event_is_also_not_quoted(
    tmp_path, monkeypatch, adapter
):
    """The other path to the same rule. A CLI that cannot authenticate at all never gets
    as far as emitting a `result`, so it complains on stderr and exits -- and that text
    would otherwise be recorded verbatim as a transport error."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stderr": "Error: not logged in (cached credential ya29.A0eXAMPLE is "
                         "expired). Run `agy` to sign in.", "returncode": 1}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert exc.value.code is ConsultErrorCode.CONNECTION_REQUIRED
    assert "ya29.A0eXAMPLE" not in str(exc.value)
    assert exc.value.required_action is not None
    assert exc.value.required_action.command == "agy"


async def test_the_raw_transcript_of_a_chunked_run_is_bounded(tmp_path, monkeypatch, adapter):
    """`base.MAX_OUTPUT_BYTES` bounds one child, which is the whole run everywhere else
    here. This one spawns a child per fragment, so their sum needs a ceiling of its own
    or the transcript held and then stored is that cap times the fragment count."""
    monkeypatch.setattr(antigravity_cli, "MAX_RAW_CHARS", 200)
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": ack(1)}, {"stdout": ack(2)}, {"stdout": ack(3)},
              {"stdout": transcript()}],
    )
    result = await adapter.start(agent(), big_prompt(), SourceMode.MODEL)
    assert len(result.raw_output) == 200


async def test_a_substituted_model_is_refused(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "agy", tmp_path, monkeypatch, runs=[{"stdout": transcript(model="gemini-3.6-flash-low")}]
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE
    assert "gemini-3.6-flash-low" in str(exc.value)


async def test_a_model_swapped_partway_through_a_chunked_run_is_refused(
    tmp_path, monkeypatch, adapter
):
    """Checked every turn, not only the last: a consultation that read three fragments
    on one model and answered on another was answered by a model nobody chose."""
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": ack(1)},
              {"stdout": jsonl(init("gemini-3.6-flash-low"),
                               result_event(response="ACK 2", structured_output=None))}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), big_prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE


async def test_a_reply_that_is_not_the_contract_fails_validation(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": jsonl(init(), result_event(structured_output={"answer": "blue"}))}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_no_result_event_at_all_is_a_transport_error(tmp_path, monkeypatch, adapter):
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": jsonl(init()), "stderr": "unknown flag", "returncode": 2}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.TRANSPORT_ERROR


async def test_a_run_with_no_conversation_id_cannot_be_resumed(tmp_path, monkeypatch, adapter):
    """Without one there is no session to continue, and returning the answer anyway
    would leave a consultation the caller can never add a turn to."""
    stdout = jsonl(
        {"event": "init", "init": {"model": MODEL}},
        {"event": "result", "result": {"status": "SUCCESS", "structured_output": CONTENT}},
    )
    agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": stdout}])
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.TRANSPORT_ERROR


async def test_a_non_json_line_is_not_a_failure(tmp_path, monkeypatch, adapter):
    """`agy` writes progress lines to stdout alongside the stream. Noise, not protocol."""
    stdout = "Shell cwd was reset\n" + transcript()
    agent_stub.install("agy", tmp_path, monkeypatch, runs=[{"stdout": stdout}])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"


async def test_a_hanging_run_times_out_and_the_schema_still_leaves(tmp_path, monkeypatch):
    # Comfortably longer than a Python interpreter takes to start, and far shorter than
    # the stub's sleep: the timeout under test is the adapter's, not the fixture's.
    adapter = AntigravityCliAdapter(timeout_s=2.0)
    record = agent_stub.install(
        "agy", tmp_path, monkeypatch, runs=[{"stdout": transcript(), "sleep": 30}]
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.TIMEOUT

    (call,) = agent_stub.calls(record)
    argv = argv_of(call)
    assert not Path(argv[argv.index("--json-schema") + 1]).exists()


async def test_the_deadline_covers_the_whole_consultation_not_each_turn(
    tmp_path, monkeypatch
):
    """A chunked prompt runs several children. Giving each the full `timeout_s` would
    multiply the caller's timeout by the number of fragments -- so no single turn here
    comes near the budget, and the run still has to end on it."""
    adapter = AntigravityCliAdapter(timeout_s=2.5)
    agent_stub.install(
        "agy", tmp_path, monkeypatch,
        runs=[{"stdout": ack(1), "sleep": 0.5}, {"stdout": ack(2), "sleep": 0.5},
              {"stdout": ack(3), "sleep": 0.5}, {"stdout": transcript(), "sleep": 0.5}],
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.start(agent(), big_prompt(), SourceMode.MODEL)
    assert exc.value.code is ConsultErrorCode.TIMEOUT


def test_the_argv_ceiling_stays_under_the_one_linux_enforces():
    """`MAX_ARG_STRLEN` is 128 KiB per single argument and is not raisable. The
    remainder is headroom for the fragment wrapper, which is added on top of a body
    already at the limit."""
    assert MAX_ARG_BYTES < 131072
    assert 131072 - MAX_ARG_BYTES > len(
        json.dumps({"wrapper": "x"})
    ) + 2000, "not enough headroom for the fragment instruction"
