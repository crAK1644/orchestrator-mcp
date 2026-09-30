"""The Copilot adapter, against a stub `copilot` on PATH.

The event shapes below were captured from a real `copilot` 1.0.89, less the fields this
adapter never reads. Four things about it are the reason this adapter is not a copy of
another one: the exit code is meaningful for readiness (1 for signed out, 1 for a model
the account cannot use) yet a refused tool call exits 0, so the stream is read as well; it
has no schema flag, so the envelope travels in the prompt and can come back malformed; the
model that answered is named only in the stream, and `auto` routes each call to a
different one; and its state lives in one directory this server has to own.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from orchestrator_mcp.consult.adapters import adapter_for, copilot_cli
from orchestrator_mcp.consult.adapters.base import AdapterError
from orchestrator_mcp.consult.adapters.copilot_cli import (
    CopilotCliAdapter,
    _add,
    _load,
    _usage,
)
from orchestrator_mcp.consult.config import AgentConfig, ConsultConfig
from orchestrator_mcp.consult.contract import SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.prompts import compile_prompt

from .conftest import consult_block
from .fixtures import copilot_stub

# Captured before any test can replace it, for the tests that are about the check itself
# rather than about the adapter that calls it.
REFUSE_WRITABLE_ANCESTORS = copilot_cli._refuse_writable_ancestors

# The model `auto` routed a call to, as the stream names it.
ROUTED = "gpt-5.6-luna"

CONTENT = {
    "answer": "blue",
    "assumptions": [],
    "uncertainties": [],
    "follow_up_questions": [],
    "sources": [{"title": "model", "locator": "internal", "source_type": "model"}],
}

# A reply that parses as JSON and is missing four of the five keys.
BROKEN = json.dumps({"answer": "blue"})

# What `user.message` restates: the whole prompt, twice, which is why it is not kept.
ECHO = "THE-WHOLE-PROMPT-AGAIN"

# The usage file. The session's own totals are larger than the last call's, cache reads
# included, which is the whole reason the adapter bills the last call and not them.
USAGE = {
    "lastCallInputTokens": 1710,
    "lastCallOutputTokens": 98,
    "modelMetrics": {ROUTED: {"usage": {"inputTokens": 3366, "outputTokens": 158}}},
}

# A session the CLI has never heard of, and that is a well-formed UUID all the same.
UNKNOWN_SESSION = "3f2b8c1e-5d4a-4e9b-8a6c-1b2d3e4f5a6b"

# The flags that would let a run do more than answer.
FORBIDDEN_FLAGS = {
    "-p", "--prompt", "--allow-all-tools", "--allow-all", "--yolo", "--allow-tool",
    "--allow-url", "--allow-all-urls", "--allow-all-paths", "--add-dir",
}


def event(kind: str, **data) -> dict:
    return {"type": kind, "id": "evt", "timestamp": "2026-09-30T00:00:00.000Z", "data": data}


def jsonl(*events: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def stream(
    text: str = json.dumps(CONTENT),
    *,
    answered: str | None = ROUTED,
    routed: str | None = ROUTED,
    calls: int = 1,
    phase: str | None = "final_answer",
    tool_requests: list | None = None,
    before: tuple[dict, ...] = (),
    session: str | None = "__SESSION__",
    exit_code: object = 0,
) -> str:
    """One turn as the CLI streams it.

    `__SESSION__` is what the stub replaces with the `--session-id` it was given, which
    is what the real CLI echoes in its last event. `session=None` leaves that event out.
    `exit_code` is what that event says about the run, whatever the process itself exits.
    """
    message = {
        "content": text,
        "phase": phase,
        "toolRequests": tool_requests or [],
        "reasoningOpaque": "OPAQUE-REASONING",
        "encryptedContent": "OPAQUE-CONTENT",
    }
    if answered:
        message["model"] = answered
    events = [event("session.info", message="Unknown tool name in the tool allowlist: x")]
    if routed:
        events.append(event("session.auto_mode_resolved", chosenModel=routed, fallback=False))
    events += [
        event("user.message", content=ECHO, transformedContent=ECHO),
        event("assistant.turn_start", turnId="0"),
        *before,
        *(event("model.call_finished", outcome="success") for _ in range(calls)),
        event("assistant.message", **message),
        event("assistant.turn_end", turnId="0"),
    ]
    if session is not None:
        events.append(
            {"type": "result", "sessionId": session, "exitCode": exit_code, "usage": {"premiumRequests": 1}}
        )
    return jsonl(*events)


def ok(**kwargs) -> dict:
    """One scripted run that answers, with the usage file the CLI writes on the way out."""
    return {"stdout": stream(**kwargs), "usage": USAGE}


def agent(**overrides) -> AgentConfig:
    return AgentConfig(**{
        "agent_id": "copilot-auto",
        "runtime": "copilot",
        "command": "copilot",
        "model": "auto",
        "scores": {"reasoning": 60},
        **overrides,
    })


def prompt(mode: SourceMode = SourceMode.MODEL, context: str | None = None):
    return compile_prompt("reasoning", mode, "what colour is the sky", context)


def flag(call: dict, name: str) -> str:
    argv = call["argv"]
    return argv[argv.index(name) + 1]


@pytest.fixture
def adapter():
    return CopilotCliAdapter(timeout_s=30)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A `$HOME` of the test's own, holding the `~/.orchestrator-mcp` the state lives in.

    The adapter keeps its directory under `$HOME` on purpose -- every ancestor
    user-owned -- and checks that no ancestor can be rewritten by another account. The
    check is stubbed out here, and only here: pytest's temporary directories sit under a
    `/tmp` that is `1777` on Linux, so a fake `$HOME` built inside one is refused, correctly
    and for a reason about the machine running the suite. The check keeps its own tests
    below, where the directory being judged is one the test made writable rather than one
    it inherited.
    """
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setattr(copilot_cli, "_refuse_writable_ancestors", lambda path: None)
    under = fake / ".orchestrator-mcp"
    under.mkdir(mode=0o700)
    return under


@pytest.fixture
def stub(tmp_path, monkeypatch, home):
    def install(**spec):
        return copilot_stub.install(tmp_path, monkeypatch, **spec)

    return install


# --- readiness --------------------------------------------------------------


async def test_preflight_on_a_missing_binary_says_so(tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    status = await adapter.preflight(agent())
    assert (status.installed, status.authenticated) == (False, False)
    assert "copilot" in (status.detail or "")


async def test_a_signed_in_account_is_ready(stub, adapter):
    """Readiness is asked for a model nobody has. The CLI refuses that before it sends
    anything, and refuses it only once it knows who is asking -- so the refusal is the
    answer, and nothing is spent finding it out. That the real CLI does this was
    checked against 1.0.89; what the stub can show is that the question is asked and
    that its refusal is read as yes."""
    record = stub()
    assert (await adapter.preflight(agent())).ready

    (call,) = copilot_stub.calls(record)
    assert flag(call, "--model") == copilot_cli._NO_MODEL
    assert call["stdin"] == "x"
    assert call["served"] is False


async def test_a_signed_out_account_is_not_ready_and_says_why(stub, adapter):
    stub(signed_in=False)
    status = await adapter.preflight(agent())
    assert status.installed and not status.authenticated
    assert "not signed in" in (status.detail or "")


async def test_an_answer_it_does_not_recognise_is_not_ready(stub, adapter):
    """A failure that is neither of the two it knows is not read as a sign-in, in either
    direction: not ready, and the exit code is in what the operator is told."""
    stub(models=(copilot_cli._NO_MODEL,), runs=[{"returncode": 1, "stderr": "Error: something new\n"}])
    status = await adapter.preflight(agent())
    assert status.installed and not status.authenticated
    assert "exit 1" in (status.detail or "")


async def test_a_check_that_was_answered_outright_is_ready(stub, adapter):
    """Exit 0 means the CLI got as far as answering, which it cannot do signed out. Not a
    case the real CLI produces today -- it would mean a model named like this exists."""
    stub(models=(copilot_cli._NO_MODEL,), runs=[{}])
    assert (await adapter.preflight(agent())).ready


async def test_readiness_runs_under_the_isolation_a_consultation_gets(stub, adapter, home):
    """Otherwise it would report signed in as whoever this process is, and the
    consultation would then run as somebody else."""
    record = stub()
    await adapter.preflight(agent())

    (call,) = copilot_stub.calls(record)
    assert Path(call["env"]["COPILOT_HOME"]) == home / "copilot" / "home"
    assert call["home_mode"] == 0o700
    assert f"--available-tools={copilot_cli._NO_TOOL}" in call["argv"]
    # A session of its own, made up for the occasion: never a consultation's.
    assert str(UUID(flag(call, "--session-id"))) == flag(call, "--session-id")
    # Its own empty directory, gone again: not the one this server was started from,
    # which may be a repository with a workspace config in it.
    assert Path(call["cwd"]).parent == home / "copilot"
    assert call["cwd_entries"] == []
    assert not list((home / "copilot").glob("probe-*"))


async def test_a_readiness_check_that_hangs_is_an_error_not_a_wait(stub, adapter, home, monkeypatch):
    monkeypatch.setattr(copilot_cli, "PREFLIGHT_TIMEOUT_S", 0.5)
    stub(models=(copilot_cli._NO_MODEL,), runs=[{"sleep": 5}])

    with pytest.raises(AdapterError) as excinfo:
        await adapter.preflight(agent())

    assert excinfo.value.code is ConsultErrorCode.TIMEOUT
    assert not list((home / "copilot").glob("probe-*"))
    # The check was killed after the CLI had made its session, and that goes too.
    state = home / "copilot" / "home" / "session-state"
    assert sorted(p.name for p in state.rglob("*")) == [".session-operation-locks"]


async def test_a_readiness_check_leaves_no_session_behind_and_no_one_elses_goes(stub, adapter, home):
    """The real CLI makes the session and its lock before it looks at the model, so a
    check it refuses still leaves both, and the check repeats for as long as the server
    runs. Found by running it: 1.0.89 left one directory and one lock per check. Only the
    check's own may go; a consultation's stays."""
    record = stub(runs=[ok()])
    kept = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    await adapter.preflight(agent())
    await adapter.preflight(agent())

    probes = [flag(call, "--session-id") for call in copilot_stub.calls(record)[1:]]
    assert len(set(probes)) == 2 and kept.native_session_id not in probes
    state = home / "copilot" / "home" / "session-state"
    locks = state / ".session-operation-locks"
    for probe in probes:
        assert not (state / probe).exists() and not (locks / f"{probe}.lock").exists()
    assert (state / kept.native_session_id).is_dir()
    assert (locks / f"{kept.native_session_id}.lock").exists()


def test_the_connect_command_signs_in_under_the_home_consultations_use(home, adapter):
    command = adapter.connect_command(agent())
    assert command == f"COPILOT_HOME={shlex.quote(str(home / 'copilot' / 'home'))} copilot login"
    # Text for the user to run. Creating the directory for them is not this method's job.
    assert not (home / "copilot").exists()


def test_the_connect_command_quotes_what_it_names(home, adapter):
    assert "'/opt/my tools/copilot' login" in adapter.connect_command(agent(command="/opt/my tools/copilot"))


def test_the_copilot_runtime_picks_this_adapter():
    built = adapter_for(agent(timeout_s=45), ConsultConfig(**consult_block()))
    assert isinstance(built, CopilotCliAdapter) and built.timeout_s == 45


# --- the happy path ---------------------------------------------------------


async def test_a_consultation_returns_the_answer_the_session_the_model_and_the_tokens(stub, adapter):
    record = stub(runs=[ok()])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.content.answer == "blue"
    (call,) = copilot_stub.calls(record)
    assert result.native_session_id == flag(call, "--session-id")
    assert str(UUID(result.native_session_id)) == result.native_session_id
    # `auto` names no model, so this is the one the stream says answered.
    assert result.model_used == ROUTED and result.model_verified
    # The last call's counts, not the 3366 / 158 the session had reached.
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (1710, 98)
    assert result.usage.total_tokens == 1808
    # Premium requests, not dollars: unknown, never zero.
    assert result.usage.cost_usd is None and result.usage.counts_incomplete == []


async def test_the_raw_output_keeps_the_answer_and_drops_the_bulk(stub, adapter):
    stub(runs=[ok()])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    kept = [json.loads(line) for line in result.raw_output.splitlines()]
    assert [e["type"] for e in kept] == [
        "session.info", "session.auto_mode_resolved", "model.call_finished", "assistant.message", "result",
    ]
    assert ECHO not in result.raw_output and "OPAQUE" not in result.raw_output
    assert "blue" in result.raw_output


async def test_the_model_is_asked_to_answer_and_not_to_act(stub, adapter):
    record = stub(runs=[ok()])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    call = copilot_stub.calls(record)[0]
    argv = call["argv"]
    # `--available-tools` naming one tool that does not exist switches all of them off.
    assert f"--available-tools={copilot_cli._NO_TOOL}" in argv
    for kind in ("shell", "write", "url"):
        assert f"--deny-tool={kind}" in argv
    for switch in ("-s", "--no-ask-user", "--disable-builtin-mcps", "--no-custom-instructions"):
        assert switch in argv
    assert flag(call, "--output-format") == "json"
    assert flag(call, "--model") == "auto"
    # Nothing that would let a tool run, and no prompt on the command line.
    assert not [a for a in argv if a.split("=")[0] in FORBIDDEN_FLAGS]


async def test_the_prompt_travels_on_stdin_whole_and_never_in_argv(stub, adapter):
    record = stub(runs=[ok()])
    compiled = prompt(SourceMode.DOCUMENT, "MARKER-" + "x" * 40_000)
    await adapter.start(agent(), compiled, SourceMode.DOCUMENT)

    call = copilot_stub.calls(record)[0]
    assert call["stdin"].startswith(compiled.full_text)
    assert "MARKER-" in call["stdin"]
    assert not [a for a in call["argv"] if "MARKER-" in a or "colour" in a]


async def test_the_envelope_shape_is_spelled_out_after_the_payload(stub, adapter):
    """No schema flag on this runtime, so the shape is stated in words -- last, because
    it is a formatting instruction and the last thing read is what a small model still
    has hold of when it starts writing."""
    record = stub(runs=[ok()])
    compiled = prompt()
    await adapter.start(agent(), compiled, SourceMode.MODEL)

    stdin = copilot_stub.calls(record)[0]["stdin"]
    assert '"source_type"' in stdin and '"locator"' in stdin
    assert stdin.index('"source_type"') > stdin.index(compiled.payload_json)


async def test_a_fenced_reply_is_still_an_answer(stub, adapter):
    stub(runs=[ok(text=f"```json\n{json.dumps(CONTENT)}\n```")])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"


async def test_a_line_that_is_not_an_event_is_not_a_protocol_failure(stub, adapter):
    stub(runs=[{"stdout": "listening on 127.0.0.1\n" + stream() + "not json\n", "usage": USAGE}])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"


async def test_commentary_is_a_model_narrating_not_answering(stub, adapter):
    narrating = jsonl(event("assistant.message", content="let me think this over", phase="commentary"))
    stub(runs=[{"stdout": narrating + stream(), "usage": USAGE}])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"


# --- isolation --------------------------------------------------------------


async def test_the_environment_carries_the_home_and_none_of_the_users_credentials(
    stub, adapter, home, monkeypatch
):
    """The token variables are not passed, like every other credential this server's
    children are not given; what the child signs in with is what `connect_command` put
    under the home. Nor may a variable of the CLI's own that widens what it may do."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_ALLOW_ALL", "COPILOT_MODEL"):
        monkeypatch.setenv(name, "not-yours")
    record = stub(runs=[ok()])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    env = copilot_stub.calls(record)[0]["env"]
    assert env["COPILOT_HOME"] == str(home / "copilot" / "home")
    assert [name for name in env if name.startswith("COPILOT_")] == ["COPILOT_HOME"]
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env


def configure(home: Path, config: object) -> None:
    """A `mcp-config.json` in the home consultations run under, as `copilot mcp add` writes one."""
    state = home / "copilot" / "home"
    state.mkdir(parents=True)
    (state / "mcp-config.json").write_text(config if isinstance(config, str) else json.dumps(config))


def switched_off(call: dict) -> list[str]:
    return [a for a in call["argv"] if a.startswith("--disable-mcp-server")]


async def test_mcp_servers_the_home_configures_are_switched_off_by_name(stub, adapter, home):
    """`--available-tools` disables a server's tools and leaves the server running. Found
    against the real CLI: one in this file was launched and connected on every run, and on
    the readiness check that costs nothing, its tool listed as disabled. The flag that stops
    the launch takes a name and no pattern, so the names are read out of the file."""
    configure(home, {"mcpServers": {"zeta": {"command": "z"}, "alpha": {"command": "a"}, "-x": {}}})
    record = stub(runs=[ok()])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)
    await adapter.preflight(agent())

    calls = copilot_stub.calls(record)
    assert len(calls) == 2
    for call in calls:
        # `=` form, so that a name that opens with a dash is a value and not another flag.
        assert switched_off(call) == [
            "--disable-mcp-server=-x", "--disable-mcp-server=alpha", "--disable-mcp-server=zeta",
        ]


@pytest.mark.parametrize(
    "config",
    [None, "{}", '{"mcpServers": {}}', '{"mcpServers": null}', '{"mcpServers": []}', "[]", "{not json"],
    ids=["no file", "empty", "no servers", "null", "a list", "not an object", "unparseable"],
)
async def test_no_server_is_named_when_the_home_configures_none_it_can_read(stub, adapter, home, config):
    """Nothing to switch off is not an error, and a file this cannot read is not a reason
    to lose the consultation: the CLI is the one that reads it for real."""
    if config is not None:
        configure(home, config)
    record = stub(runs=[ok()])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert switched_off(copilot_stub.calls(record)[0]) == []


async def test_every_run_gets_an_empty_working_directory_of_its_own_that_is_then_gone(
    stub, adapter, home
):
    record = stub(runs=[ok(), ok()])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    first, second = copilot_stub.calls(record)
    assert first["cwd"] != second["cwd"]
    for call in (first, second):
        assert call["cwd_entries"] == []
        assert Path(call["cwd"]).parent.parent == home / "copilot"
        assert not Path(call["cwd"]).exists()
        # Beside the directory, so that whatever the CLI writes for itself is not in it.
        assert Path(flag(call, "--usage-output-file")).parent == Path(call["cwd"]).parent
    # One home, though: one login, not one per run.
    assert first["env"]["COPILOT_HOME"] == second["env"]["COPILOT_HOME"]
    assert not list((home / "copilot").glob("run-*"))


async def test_the_scratch_directory_is_gone_after_a_failed_run_too(stub, adapter, home):
    stub(runs=[{"returncode": 1, "stderr": "Error: nope\n"}])
    with pytest.raises(AdapterError):
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert not list((home / "copilot").glob("run-*"))


async def test_a_home_the_login_created_loosely_is_tightened_not_refused(stub, adapter, home):
    """`copilot login` creates it under the umask, and the documented first step is to
    run that with this home. Refusing what the first step produces would break the
    second."""
    root = home / "copilot"
    (root / "home").mkdir(parents=True)
    root.chmod(0o755)
    (root / "home").chmod(0o755)
    record = stub(runs=[ok()])

    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert copilot_stub.calls(record)[0]["home_mode"] == 0o700
    assert root.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("linked", ["root", "home"])
async def test_a_state_directory_that_is_a_symlink_is_refused(stub, adapter, home, tmp_path, linked):
    """`mkdir(exist_ok=True)` is satisfied by a symlink to a directory somewhere else, and
    the CLI's state -- its login among it -- would then be written outside the tree that
    was checked."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    if linked == "root":
        (home / "copilot").symlink_to(elsewhere)
    else:
        (home / "copilot").mkdir(mode=0o700)
        (home / "copilot" / "home").symlink_to(elsewhere)
    record = stub(runs=[ok()])

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert not copilot_stub.calls(record)


@pytest.mark.parametrize("mode", [0o777, 0o775])
async def test_an_ancestor_another_account_can_write_is_refused(stub, adapter, home, monkeypatch, mode):
    """Write on any ancestor is permission to rename the tree away between the check and
    the use. World-writable and group-writable both: a shared group is the likelier one
    on a real machine."""
    monkeypatch.setattr(copilot_cli, "_refuse_writable_ancestors", REFUSE_WRITABLE_ANCESTORS)
    record = stub(runs=[ok()])
    home.chmod(mode)

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert str(home.resolve()) in str(excinfo.value)
    assert not copilot_stub.calls(record)


async def test_readiness_is_refused_under_a_writable_ancestor_too(stub, adapter, home, monkeypatch):
    monkeypatch.setattr(copilot_cli, "_refuse_writable_ancestors", REFUSE_WRITABLE_ANCESTORS)
    record = stub()
    home.chmod(0o775)

    with pytest.raises(AdapterError):
        await adapter.preflight(agent())
    assert not copilot_stub.calls(record)


# --- sessions ---------------------------------------------------------------


async def test_a_session_id_that_is_a_uuid_names_the_session(stub, adapter):
    """The service hands over the consultation's own id, so nothing is parsed back out of
    the stream to find out what the session was called."""
    record = stub(runs=[ok()])
    chosen = str(uuid4())
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL, session_id=chosen)

    assert result.native_session_id == chosen
    assert flag(copilot_stub.calls(record)[0], "--session-id") == chosen


async def test_a_uuid_is_normalised_before_it_is_compared_with_the_echo(stub, adapter):
    stub(runs=[ok()])
    chosen = str(uuid4())
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL, session_id=chosen.upper())
    assert result.native_session_id == chosen


@pytest.mark.parametrize("given", [None, "", "not-a-uuid", "ses_01e1d1c6effevvnIR8XBZ9hlqX"])
async def test_anything_that_is_not_a_uuid_gets_a_fresh_one(stub, adapter, given):
    stub(runs=[ok()])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL, session_id=given)
    assert str(UUID(result.native_session_id)) == result.native_session_id
    assert result.native_session_id != given


async def test_a_resume_continues_the_session_the_first_turn_created(stub, adapter):
    record = stub(runs=[ok(), ok()])
    first = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    second = await adapter.resume(agent(), first.native_session_id, prompt(), SourceMode.MODEL)

    one, two = copilot_stub.calls(record)
    assert one["session_known"] is False and two["session_known"] is True
    assert flag(two, "--session-id") == first.native_session_id == second.native_session_id
    # Not from the same directory, which is gone: the session does not remember one.
    assert one["cwd"] != two["cwd"]


async def test_a_resume_of_a_session_copilot_no_longer_has_is_refused_not_started_empty(
    stub, adapter, home
):
    """`--session-id` with an id it does not know creates a session, silently, so the
    answer would read as a continuation and be a first turn that remembers nothing."""
    record = stub(runs=[ok(), ok()])
    first = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    shutil.rmtree(home / "copilot" / "home" / "session-state" / first.native_session_id)

    with pytest.raises(AdapterError) as excinfo:
        await adapter.resume(agent(), first.native_session_id, prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.SESSION_NOT_FOUND
    assert len(copilot_stub.calls(record)) == 1


@pytest.mark.parametrize(
    "given",
    ["", "not-a-uuid", "../../etc/passwd", "ses_01e1d1c6effevvnIR8XBZ9hlqX", UNKNOWN_SESSION],
)
async def test_a_resume_id_that_is_not_a_known_session_never_reaches_the_cli(stub, adapter, given):
    """Including the ones that are not UUIDs at all, which are never joined onto a path."""
    record = stub(runs=[ok()])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.resume(agent(), given, prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.SESSION_NOT_FOUND
    assert not copilot_stub.calls(record)


# --- web mode ---------------------------------------------------------------


@pytest.mark.parametrize("web_search", [False, True], ids=["flag off", "flag on"])
async def test_web_mode_is_refused_whatever_the_agent_is_configured_for(stub, adapter, web_search):
    """Unconditionally, not `if not agent.web_search`: the `url` tool is denied here, so
    an operator who turns the flag on must be refused rather than served a model-mode
    answer under a web-mode contract."""
    record = stub(runs=[ok()])

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(web_search=web_search), prompt(SourceMode.WEB), SourceMode.WEB)

    assert excinfo.value.code is ConsultErrorCode.WEB_SEARCH_UNAVAILABLE
    assert not copilot_stub.calls(record)


async def test_web_mode_is_refused_on_resume_as_well(stub, adapter):
    record = stub(runs=[ok()])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.resume(agent(web_search=True), UNKNOWN_SESSION, prompt(SourceMode.WEB), SourceMode.WEB)
    assert excinfo.value.code is ConsultErrorCode.WEB_SEARCH_UNAVAILABLE
    assert not copilot_stub.calls(record)


# --- acting rather than answering -------------------------------------------


@pytest.mark.parametrize(
    "stdout",
    [
        stream(before=(event("tool.execution_start", toolName="bash"),)),
        stream(before=(event("tool.execution_complete", success=False, error={"code": "denied"}),)),
        stream(before=(event("tool.something_new"),)),
        stream(tool_requests=[{"name": "bash", "arguments": {}}]),
    ],
    ids=["tool started", "tool refused", "unknown tool event", "tool requested"],
)
async def test_a_tool_call_fails_the_turn_however_it_ended(stub, adapter, stdout):
    """A refused tool call exits 0 with an answer after it, so the exit code says nothing
    and the stream is what stands between an action and a consultation that reports none.
    Fails closed on the whole `tool.` family, since a later CLI may name a new one."""
    record = stub(runs=[{"stdout": stdout, "usage": USAGE}])

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "act rather than answer" in str(excinfo.value)
    # A turn that acted is not asked again.
    assert len(copilot_stub.calls(record)) == 1


# --- failures the CLI reports ------------------------------------------------


async def test_a_signed_out_run_asks_for_the_login_under_the_isolated_home(stub, adapter, home):
    stub(signed_in=False)
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.CONNECTION_REQUIRED
    assert excinfo.value.required_action.command == adapter.connect_command(agent())
    assert str(home / "copilot" / "home") in excinfo.value.required_action.command


async def test_a_model_the_account_cannot_use_is_named(stub, adapter):
    """Only `auto` was served on the free plan this was checked on; a concrete model was
    refused with exit 1."""
    stub(models=("auto",))
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(model="gpt-5.6-sol"), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE
    assert "gpt-5.6-sol" in str(excinfo.value)


async def test_any_other_failure_is_an_unavailable_agent_with_the_reason(stub, adapter):
    stub(runs=[{"returncode": 1, "stderr": "Error: quota exceeded\n"}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert "exited 1" in str(excinfo.value) and "quota exceeded" in str(excinfo.value)


async def test_a_stream_with_no_result_is_a_fragment_not_an_answer(stub, adapter):
    """The last event is what says the turn ended. Without it, a killed child or a
    dropped connection hands back its partial text as a finished reply."""
    record = stub(runs=[{"stdout": stream(session=None), "usage": USAGE}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert "fragment" in str(excinfo.value)
    assert len(copilot_stub.calls(record)) == 1


async def test_an_answer_under_another_session_is_refused(stub, adapter):
    stub(runs=[{"stdout": stream(session="someone-elses-session"), "usage": USAGE}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "someone-elses-session" in str(excinfo.value)


@pytest.mark.parametrize(
    "stdout",
    [stream(text=""), stream(phase="commentary")],
    ids=["empty answer", "only commentary"],
)
async def test_a_turn_with_no_answer_in_it_is_not_an_empty_reply(stub, adapter, stdout):
    """Handing "" to the content parser would report it as bad JSON, and be asked again."""
    record = stub(runs=[{"stdout": stdout, "usage": USAGE}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert "no answer" in str(excinfo.value)
    assert len(copilot_stub.calls(record)) == 1


async def test_a_line_that_opens_like_an_event_and_does_not_parse_fails_the_run(stub, adapter):
    """The other direction from the test of a line that is not an event. Skipping a
    broken one is deciding what it said, and the tool check is what stands between an
    action and an answer that claims none happened."""
    broken = '{"type": "tool.execution_start", "data": {"toolName":\n'
    stub(runs=[{"stdout": stream() + broken, "usage": USAGE}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_a_timeout_kills_the_run_and_cleans_up(stub, home):
    stub(runs=[{"stdout": stream(), "sleep": 5}])
    with pytest.raises(AdapterError) as excinfo:
        await CopilotCliAdapter(timeout_s=0.5).start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TIMEOUT
    assert not list((home / "copilot").glob("run-*"))


# --- which model answered ---------------------------------------------------


async def test_a_configured_model_the_answer_did_not_come_from_is_refused(stub, adapter):
    """The CLI's own fallback: an answer from a model nobody chose, presented as the one
    they did."""
    stub(models=("gpt-5.6-sol",), runs=[ok(answered="gpt-5.6-luna", routed=None)])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(model="gpt-5.6-sol"), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE
    assert "gpt-5.6-luna" in str(excinfo.value)


async def test_a_configured_model_that_answered_is_recorded_as_verified(stub, adapter):
    stub(models=("gpt-5.6-sol",), runs=[ok(answered="gpt-5.6-sol", routed=None)])
    result = await adapter.start(agent(model="gpt-5.6-sol"), prompt(), SourceMode.MODEL)
    assert result.model_used == "gpt-5.6-sol" and result.model_verified


async def test_an_explicit_model_the_answer_does_not_name_is_passed_through_unverified(stub, adapter):
    """Like every other adapter: no metadata is not evidence of a substitution, and it is
    not evidence of anything else either -- so the flag says which it is."""
    stub(models=("gpt-5.6-sol",), runs=[ok(answered=None, routed=None)])
    result = await adapter.start(agent(model="gpt-5.6-sol"), prompt(), SourceMode.MODEL)
    assert result.model_used == "gpt-5.6-sol" and not result.model_verified


async def test_auto_with_nothing_named_stays_auto_and_unverified(stub, adapter):
    stub(runs=[ok(answered=None, routed=None)])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.model_used == "auto" and not result.model_verified


async def test_the_routed_model_stands_in_when_the_answer_names_none(stub, adapter):
    stub(runs=[ok(answered=None, routed=ROUTED)])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.model_used == ROUTED and result.model_verified


async def test_the_model_that_wrote_the_answer_outranks_the_one_routed_to(stub, adapter):
    stub(runs=[ok(answered="gpt-5.6-terra", routed=ROUTED)])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).model_used == "gpt-5.6-terra"


# --- the repair turn --------------------------------------------------------


async def test_a_malformed_envelope_is_asked_again_once_in_the_same_session(stub, adapter):
    """No schema flag here, so the envelope rests on instruction alone and a small model
    drops a required key. One follow-up in the same session costs a few hundred
    characters; failing outright would waste the context already paid for."""
    record = stub(runs=[ok(text=BROKEN), ok()])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.content.answer == "blue"

    first, second = copilot_stub.calls(record)
    assert flag(second, "--session-id") == flag(first, "--session-id") == result.native_session_id
    assert second["session_known"] is True
    assert second["stdin"].startswith("That reply did not match the required schema")
    # The shape again, not the task: resending it would cost the whole context twice.
    assert "sources" in second["stdin"] and len(second["stdin"]) < len(first["stdin"])
    # The same empty directory for both turns, and a usage file of its own for each, or
    # the second would read the first's.
    assert first["cwd"] == second["cwd"] and second["cwd_entries"] == []
    assert flag(first, "--usage-output-file") != flag(second, "--usage-output-file")


async def test_both_turns_are_billed_and_both_are_in_the_raw_output(stub, adapter):
    first = {"lastCallInputTokens": 1000, "lastCallOutputTokens": 10}
    second = {"lastCallInputTokens": 300, "lastCallOutputTokens": 50}
    stub(runs=[
        {"stdout": stream(text="{}"), "usage": first},
        {"stdout": stream(), "usage": second},
    ])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (1300, 60)
    assert result.usage.total_tokens == 1360 and result.usage.cost_usd is None
    kinds = [json.loads(line)["type"] for line in result.raw_output.splitlines()]
    assert kinds.count("assistant.message") == 2 and kinds.count("result") == 2


async def test_a_caveat_from_the_first_turn_survives_the_second(stub, adapter):
    """A number that was a guess on turn one is still a guess in the sum."""
    stub(runs=[{"stdout": stream(text="{}"), "usage": {}}, ok()])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.usage.counts_incomplete == ["copilot's usage file gave no token counts for this turn"]
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (1710, 98)


async def test_a_second_broken_reply_fails_after_exactly_two_runs(stub, adapter):
    record = stub(runs=[ok(text=BROKEN)])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert len(copilot_stub.calls(record)) == 2


async def test_a_repair_turn_that_fails_is_reported_as_what_it_was(stub, adapter):
    stub(runs=[ok(text=BROKEN), {"returncode": 1, "stderr": "Error: quota exceeded\n"}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert excinfo.value.code is ConsultErrorCode.AGENT_UNAVAILABLE


@pytest.mark.parametrize("model", ["auto", "gpt-5.6-sol"])
async def test_a_repaired_answer_is_not_credited_to_the_reply_it_replaced(stub, adapter, model):
    """The first reply named its model and was thrown away. The repair wrote the answer
    that comes back and named none, so nothing says which model that was: lending it the
    first reply's name would mark it verified against a call that did not produce it."""
    named = ROUTED if model == "auto" else model
    stub(models=(model,), runs=[
        ok(text=BROKEN, answered=named, routed=None),
        ok(answered=None, routed=None),
    ])
    result = await adapter.start(agent(model=model), prompt(), SourceMode.MODEL)

    assert result.content.answer == "blue"
    assert result.model_used == model and not result.model_verified


async def test_the_model_that_wrote_the_repair_is_the_one_recorded(stub, adapter):
    stub(runs=[
        ok(text=BROKEN, answered="gpt-5.6-terra", routed=None),
        ok(answered=ROUTED, routed=None),
    ])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.model_used == ROUTED and result.model_verified


# --- a run that contradicts itself ------------------------------------------


@pytest.mark.parametrize("code", [1, 2, 130])
async def test_a_last_event_that_reports_a_failed_run_is_not_an_answer(stub, adapter, code):
    """The process exited 0 and its own closing event says the run did not succeed. The
    two disagree, and an answer out of a run that says it failed is not handed back."""
    record = stub(runs=[{"stdout": stream(exit_code=code), "usage": USAGE}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert f"exit code {code}" in str(excinfo.value)
    assert len(copilot_stub.calls(record)) == 1  # not repaired: it was not a malformed answer


async def test_a_last_event_with_no_exit_code_is_still_an_answer(stub, adapter):
    """The check is for a contradiction, not for a field a future CLI may drop."""
    stub(runs=[{"stdout": stream(exit_code=None), "usage": USAGE}])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"


# --- usage ------------------------------------------------------------------


def test_a_turn_is_billed_at_the_last_calls_counts_not_the_sessions():
    usage = _usage(USAGE, 1)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (1710, 98, 1808)
    assert usage.cost_usd is None and usage.counts_incomplete == []


def test_a_usage_file_with_no_counts_says_so_instead_of_reading_as_zero():
    usage = _usage({"modelMetrics": {}})
    assert usage.total_tokens == 0
    assert usage.counts_incomplete == ["copilot's usage file gave no token counts for this turn"]


@pytest.mark.parametrize("report", [None, [], "not a dict", 7])
def test_an_unreadable_usage_report_is_no_counts_rather_than_a_crash(report):
    usage = _usage(report)
    assert usage.total_tokens == 0 and usage.counts_incomplete


def test_a_count_that_is_not_a_number_is_counted_as_nothing_and_said_so():
    usage = _usage({"lastCallInputTokens": "N/A", "lastCallOutputTokens": ["?"]})
    assert usage.total_tokens == 0
    assert any("is not a token count" in note for note in usage.counts_incomplete)


@pytest.mark.parametrize("calls", [0, 2, 3])
def test_a_turn_that_made_other_than_one_call_is_flagged(calls):
    """The usage file reports only the last call's tokens, so a turn that made two is
    undercounted and a turn that made none has counts that belong to nothing."""
    usage = _usage(USAGE, calls)
    assert any(f"{calls} successful model calls" in note for note in usage.counts_incomplete)
    assert usage.prompt_tokens == 1710


def test_a_usage_file_that_is_missing_or_garbled_reads_as_nothing(tmp_path):
    assert _load(tmp_path / "none.json") is None
    report = tmp_path / "usage.json"
    report.write_text("{not json")
    assert _load(report) is None
    report.write_text(json.dumps(USAGE))
    assert _load(report) == USAGE


def test_two_turns_add_and_keep_what_either_said_about_its_counts():
    both = _add(_usage({}), _usage(USAGE, 2))

    assert both.total_tokens == 1808 and both.cost_usd is None
    assert len(both.counts_incomplete) == 2


def test_the_same_caveat_from_both_turns_is_said_once_with_a_count():
    both = _add(_usage({}), _usage({}))
    assert both.counts_incomplete == ["copilot's usage file gave no token counts for this turn (x2)"]
