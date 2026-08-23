"""The opencode adapter, against a stub executable on PATH.

The event and export shapes below were captured from a real `opencode` 1.18.15, and
four things about it are the reason this
adapter is not a copy of another one: `run` exits 0 whatever happens, so the stream
decides; the model that answered appears only in `export`; the configuration merges
from directories nobody asked it to read; and there is no schema flag, so the
envelope can come back malformed from a model that answered perfectly well.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator_mcp.consult.adapters import opencode_cli
from orchestrator_mcp.consult.adapters.base import AdapterError
from orchestrator_mcp.consult.adapters.opencode_cli import (
    OpenCodeCliAdapter,
    _add,
    _session_dir,
)
from orchestrator_mcp.consult.config import AgentConfig
from orchestrator_mcp.consult.contract import SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.prompts import compile_prompt
from orchestrator_mcp.contract import Usage

from .fixtures import opencode_stub

# Captured before any test can replace it, for the two tests that are about the check
# itself rather than about the adapter that calls it.
REFUSE_WRITABLE_ANCESTORS = opencode_cli._refuse_writable_ancestors

MODEL = "opencode/deepseek-v4-flash-free"
SESSION = "ses_01e1d1c6effevvnIR8XBZ9hlqX"

# What a locally served model is addressed as, and what the isolated configuration
# makes unreachable. Named here so the tests that pin that can say which shape they
# mean rather than describing it.
LOCAL_MODEL = "ollama/qwen2.5:7b"

CONTENT = {
    "answer": "blue",
    "assumptions": [],
    "uncertainties": [],
    "follow_up_questions": [],
    "sources": [{"title": "model", "locator": "internal", "source_type": "model"}],
}

# A user's own configuration as `opencode debug config` would print it: a locally
# served provider, a keyed one, and exactly the things the isolation exists to drop.
# None of it may reach the child, which is what the tests below check -- including
# that the adapter never asks for it in the first place.
RESOLVED = json.dumps(
    {
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://localhost:11434/v1"},
                "models": {"qwen2.5:7b": {"name": "qwen2.5 7b"}},
            },
            "deepseek": {
                "npm": "@ai-sdk/deepseek",
                "options": {"apiKey": "sk-someone-elses-key"},
                "models": {"deepseek-chat": {}},
            },
        },
        "mcp": {"internal-tools": {"type": "local", "command": ["some-server"]}},
        "instructions": ["~/notes/AGENTS.md"],
        "permission": {"bash": "allow"},
        "agent": {"reviewer": {"prompt": "you are a reviewer"}},
    }
)

EXPORT = json.dumps(
    {"info": {"id": SESSION, "model": {"id": "deepseek-v4-flash-free", "providerID": "opencode"}}}
)


def part(kind: str, **fields) -> dict:
    return {"id": "prt_x", "messageID": "msg_x", "sessionID": SESSION, "type": kind} | fields


def event(kind: str, body: dict, session: str = SESSION) -> dict:
    return {"type": kind, "timestamp": 1786201238054, "sessionID": session, "part": body}


def jsonl(*events: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def stream(text: str = json.dumps(CONTENT), session: str = SESSION, **tokens) -> str:
    counts = {"total": 2054, "input": 2050, "output": 4, "reasoning": 0, "cache": {}} | tokens
    return jsonl(
        event("step_start", part("step-start"), session),
        event("text", part("text", text=text), session),
        event("step_finish", part("step-finish", reason="stop", tokens=counts, cost=0.0), session),
    )


def agent(**overrides) -> AgentConfig:
    return AgentConfig(**{
        "agent_id": "qwen-local",
        "runtime": "opencode",
        "command": "opencode",
        "model": MODEL,
        "scores": {"reasoning": 60},
        **overrides,
    })


def prompt(mode: SourceMode = SourceMode.MODEL, context: str | None = None):
    return compile_prompt("reasoning", mode, "what colour is the sky", context)


@pytest.fixture
def adapter():
    return OpenCodeCliAdapter(timeout_s=30)


@pytest.fixture
def scratch_under(tmp_path, monkeypatch):
    """Put the adapter's working directory somewhere the test controls.

    Where it lands matters twice over: the adapter refuses to run beneath a directory
    carrying an opencode config of its own, and it deliberately sits under `$HOME` --
    every ancestor user-owned -- rather than under a `/tmp` another account can write.

    The second of those is stubbed out here, and only here. pytest's temporary
    directories live under `/tmp` on Linux, which is `1777`, so a fake `$HOME` built
    inside one is refused by `_refuse_writable_ancestors` -- correctly, and for a
    reason about the machine running the suite rather than about anything these tests
    are checking. macOS puts them under `/var/folders`, which is why this only ever
    showed up on CI. The check keeps its own tests, at the bottom of this file, where
    the chain being judged is one the test built rather than one it inherited.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(opencode_cli, "_refuse_writable_ancestors", lambda path: None)
    under = home / ".orchestrator-mcp"
    under.mkdir(mode=0o700)
    return under


@pytest.fixture
def stub(tmp_path, monkeypatch, scratch_under):
    def install(**spec):
        spec.setdefault("config", RESOLVED)
        spec.setdefault("export", {"stdout": EXPORT})
        return opencode_stub.install(tmp_path, monkeypatch, **spec)

    return install


def run_calls(record: Path) -> list[dict]:
    return opencode_stub.calls(record, "run")


# --- preflight --------------------------------------------------------------


async def test_preflight_on_a_missing_binary_says_so(tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    status = await adapter.preflight(agent())
    assert (status.installed, status.authenticated) == (False, False)
    assert "opencode" in (status.detail or "")


async def test_preflight_passes_when_the_configured_model_is_offered(stub, adapter):
    """Readiness is asked as `models`, not as a login check: providers differ in what
    signing in even means, and a listed model is one that can be asked."""
    stub(models=f"opencode/big-pickle\n{MODEL}\n")
    assert (await adapter.preflight(agent())).ready


async def test_preflight_fails_when_the_configured_model_is_not_offered(stub, adapter):
    stub(models="opencode/big-pickle\n")
    status = await adapter.preflight(agent())
    assert status.installed and not status.authenticated
    assert MODEL in (status.detail or "")


async def test_a_locally_served_model_is_never_ready(stub, adapter):
    """Because `models` is asked with the user's config out of reach, a provider they
    declared for a local endpoint is not among the answers -- so an agent pointed at one
    is refused here rather than at the run, where opencode would fail to resolve it."""
    stub(models=f"opencode/big-pickle\n{MODEL}\n")
    status = await adapter.preflight(agent(model=LOCAL_MODEL))
    assert status.installed and not status.authenticated
    assert LOCAL_MODEL in (status.detail or "")


async def test_readiness_is_not_asked_from_whatever_repository_the_server_started_in(
    stub, adapter, scratch_under
):
    """`opencode models` merges the config above its working directory too, so run from
    the server's own directory it would report the models some checked-out repository
    offers rather than the ones this machine can reach."""
    record = stub(models=f"{MODEL}\n")
    await adapter.preflight(agent())

    call = opencode_stub.calls(record, "models")[0]
    assert Path(call["cwd"]) == scratch_under / "opencode" / "probe"
    # Its own config and nothing else: the two files readiness is isolated by, the
    # same two a consultation gets.
    assert call["cwd_entries"] == ["config.json", "opencode.json"]


async def test_readiness_is_refused_under_a_config_it_would_have_merged(
    stub, adapter, scratch_under
):
    """`opencode models` merges an ancestor config the same way a run does, and the run
    refuses to start under one. Readiness that did not would report a model reachable
    that the consultation will not ask -- which is the whole failure the probe directory
    exists to prevent, arriving through the door it left open."""
    record = stub(models=f"{MODEL}\n")
    (scratch_under / "opencode.json").write_text(
        '{"provider": {"ollama": {"options": {"baseURL": "http://localhost:11434/v1"}}}}'
    )

    with pytest.raises(AdapterError) as excinfo:
        await adapter.preflight(agent())

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert not opencode_stub.calls(record, "models")


async def test_readiness_is_refused_under_a_jsonc_config_too(stub, adapter, scratch_under):
    """Both spellings, because refusing one and merging the other is the same hole with
    a different file extension."""
    record = stub(models=f"{MODEL}\n")
    (scratch_under / "opencode.jsonc").write_text("{}")

    with pytest.raises(AdapterError):
        await adapter.preflight(agent())
    assert not opencode_stub.calls(record, "models")


async def test_readiness_is_isolated_by_the_same_configuration_a_run_is(
    stub, adapter, scratch_under
):
    """Not merely by a redirected config home. `OPENCODE_CONFIG` merges rather than
    replaces, so leaving it unset let anything opencode found on its own take part in
    the answer."""
    record = stub(models=f"{MODEL}\n")
    await adapter.preflight(agent())

    call = opencode_stub.calls(record, "models")[0]
    written = json.loads((Path(call["cwd"]) / "config.json").read_text())
    assert call["env"]["OPENCODE_CONFIG"] == str(Path(call["cwd"]) / "config.json")
    assert written["permission"] == {"*": "deny"}
    assert "provider" not in written


def test_the_connect_command_is_the_users_to_run(adapter):
    assert adapter.connect_command(agent()) == "opencode auth login"


# --- the happy path ---------------------------------------------------------


async def test_a_consultation_returns_the_answer_the_session_and_the_cost(stub, adapter):
    record = stub(run=[{"stdout": stream()}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.content.answer == "blue"
    assert result.native_session_id == SESSION
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (2050, 4)
    assert result.usage.total_tokens == 2054
    assert result.model_used == MODEL and result.model_verified

    argv = run_calls(record)[0]["argv"]
    assert argv[:2] == ["run", "--pure"]
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--format") + 1] == "json"
    # No positional message: the prompt is on stdin, where neither `ARG_MAX` nor a
    # shell can get at it.
    assert not [a for a in argv[1:] if not a.startswith("-") and a not in {"json", "ERROR", MODEL}]


async def test_the_prompt_travels_on_stdin_whole(stub, adapter):
    record = stub(run=[{"stdout": stream()}])
    compiled = prompt(SourceMode.DOCUMENT, "MARKER-" + "x" * 40_000)
    await adapter.start(agent(), compiled, SourceMode.DOCUMENT)

    stdin = run_calls(record)[0]["stdin"]
    assert stdin.startswith(compiled.full_text)
    assert "MARKER-" in stdin


async def test_the_envelope_shape_is_spelled_out_in_the_prompt(stub, adapter):
    """This runtime has no schema flag, so the shape is stated in words -- and stated
    concretely, because what a small model gets wrong is `"sources": ["model"]` where
    each entry is an object, not a missing key."""
    record = stub(run=[{"stdout": stream()}])
    compiled = prompt()
    await adapter.start(agent(), compiled, SourceMode.MODEL)

    stdin = run_calls(record)[0]["stdin"]
    assert '"source_type"' in stdin and '"locator"' in stdin
    # After the payload: it is a formatting instruction, and the last thing read is
    # what a small model still has hold of when it starts writing.
    assert stdin.index('"source_type"') > stdin.index(compiled.payload_json)


async def test_the_answer_is_read_from_the_stream_not_the_exit_code(stub, adapter):
    """`opencode run` returns 0 on a hard provider failure, so a nonzero code is not
    the interesting case -- it is that the code says nothing either way."""
    record = stub(run=[{"stdout": stream(), "returncode": 3}])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"
    assert run_calls(record)


async def test_a_fenced_reply_is_still_an_answer(stub, adapter):
    stub(run=[{"stdout": stream(text=f"```json\n{json.dumps(CONTENT)}\n```")}])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"


# --- isolation --------------------------------------------------------------


async def test_the_consultation_runs_from_a_scratch_directory(stub, adapter, scratch_under):
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    call = run_calls(record)[0]
    assert Path(call["cwd"]).parent.parent == scratch_under
    # Nothing to read, permission miss or not: only what this server put there.
    assert call["cwd_entries"] == ["config.json", "opencode.json"]


async def test_the_scratch_directory_outlives_the_consultation(stub, adapter):
    """opencode records the directory a session ran in and resolves it again on
    `--session`, so deleting it -- as a `TemporaryDirectory` would -- turns every
    follow-up turn into `NotFound: FileSystem.realPath` on a path this server removed
    itself. Both turns run from the same place, and it is still there afterwards."""
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)
    await adapter.resume(agent(), SESSION, prompt(), SourceMode.MODEL)

    first, second = run_calls(record)
    assert first["cwd"] == second["cwd"]
    assert Path(first["cwd"]).is_dir()


async def test_a_scratch_directory_that_is_not_ours_is_refused(stub, adapter, scratch_under):
    """The name is fixed, so on a shared `/tmp` it is something another user can get
    to first -- as a symlink pointing wherever they like, which is where the
    consultation would then run."""
    record = stub(run=[{"stdout": stream()}])
    elsewhere = scratch_under / "someone-elses"
    elsewhere.mkdir()
    (scratch_under / "opencode").symlink_to(elsewhere)

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert not run_calls(record)


async def test_a_world_readable_scratch_directory_is_refused(stub, adapter, scratch_under):
    record = stub(run=[{"stdout": stream()}])
    (scratch_under / "opencode").mkdir(mode=0o755)

    with pytest.raises(AdapterError):
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert not run_calls(record)


@pytest.mark.parametrize("mode", [0o777, 0o775])
async def test_an_ancestor_another_account_can_write_is_refused(
    stub, adapter, scratch_under, monkeypatch, mode
):
    """Checking the scratch directory itself and then naming it by path again leaves a
    gap: write permission on any ancestor is permission to rename it away between the
    two, and the tree opencode reads its configuration from is then not the tree that
    was checked. World-writable and group-writable both, since a shared group is the
    likelier one on a real machine."""
    record = stub(run=[{"stdout": stream()}])
    # This is the one test the fixture's stub would defeat, so it puts the real check
    # back. The chmod is on the deepest directory in the chain, which the walk reaches
    # before anything the machine owns, so this fails here for its own reason.
    monkeypatch.setattr(opencode_cli, "_refuse_writable_ancestors", REFUSE_WRITABLE_ANCESTORS)
    scratch_under.chmod(mode)

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert str(scratch_under.resolve()) in str(excinfo.value)
    assert not run_calls(record)


async def test_a_working_directory_that_is_a_symlink_is_refused(
    stub, adapter, scratch_under, tmp_path
):
    """The scratch root's own check said nothing about its children, and
    `mkdir(exist_ok=True)` is satisfied by a symlink pointing at a directory somewhere
    else -- so the config would be written, and read, outside the tree that was checked.
    Only this account can plant one inside a `0700` root, which makes this less a guard
    against another user than the thing that turns "it reads what we wrote" from an
    argument about who could have been here into a checked fact."""
    record = stub(run=[{"stdout": stream()}])
    root = scratch_under / "opencode"
    root.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / _session_dir(agent().agent_id)).symlink_to(elsewhere)

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert not run_calls(record)


async def test_a_readable_but_unwritable_ancestor_is_allowed(stub, adapter, scratch_under):
    """Only write is refused. `/` and `/Users` are `0755` on any Mac, and a check that
    read `0o077` the whole way up would refuse every machine it ran on."""
    record = stub(run=[{"stdout": stream()}])
    scratch_under.chmod(0o755)

    await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert run_calls(record)


async def test_the_written_configuration_denies_everything(stub, adapter):
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)
    call = run_calls(record)[0]

    for written in (call["project_config"], call["env_config"]):
        # `"*"` rather than the permissions spelled out: opencode expands it to its own
        # set, so one added by a later release is denied too rather than defaulting to
        # *ask* -- which in a non-interactive run means hanging until the timeout.
        assert written["permission"] == {"*": "deny"}
        assert written["mcp"] == {} and written["plugin"] == []
        assert written["instructions"] == []
        assert written["share"] == "disabled" and written["autoupdate"] is False


async def test_both_the_project_and_the_environment_configuration_are_written(stub, adapter):
    """Two placements, because they fail differently: the project one outranks
    anything above it on permissions, the environment one survives a release that
    stops reading the working directory."""
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    call = run_calls(record)[0]
    assert call["project_config"] == call["env_config"]
    assert Path(call["env"]["OPENCODE_CONFIG"]).parent == Path(call["cwd"])


async def test_the_users_config_directory_is_taken_out_of_reach(stub, adapter):
    """`OPENCODE_CONFIG` merges rather than replaces -- a planted global config's `mcp`
    and `instructions` survived it -- so the config home is moved as well."""
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    call = run_calls(record)[0]
    # Under the adapter's own root, alongside the per-agent working directories rather
    # than inside one. Not empty by definition -- opencode writes a stub into its own
    # config home on first run -- but empty of any config, which is what the next test
    # is about.
    assert Path(call["env"]["XDG_CONFIG_HOME"]).parent == Path(call["cwd"]).parent
    assert call["xdg_entries"] == []


async def test_a_global_config_in_the_redirected_home_is_cleared(stub, adapter, scratch_under):
    """`_refuse_inherited_config` cannot see this one: a redirected config home is not
    an ancestor of the working directory, so nothing else in the adapter would notice a
    global config appearing here. It is reused across runs and opencode writes into it,
    which is the whole reason its contents are cleared rather than trusted."""
    record = stub(run=[{"stdout": stream()}])
    # Each level explicitly: `parents=True` applies the mode only to the last one, and
    # an intermediate left at the umask fails the adapter's own privacy check.
    xdg = scratch_under / "opencode"
    for name in ("xdg", "opencode"):
        xdg.mkdir(mode=0o700, exist_ok=True)
        xdg = xdg / name
    xdg.mkdir(mode=0o700)
    planted = xdg / "opencode.json"
    planted.write_text('{"permission": {"bash": "allow"}, "mcp": {"theirs": {}}}')

    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert run_calls(record)
    assert not planted.exists()


async def test_the_data_directory_is_left_alone(stub, adapter):
    """The saved provider credentials live under it. Relocating it would turn every
    consultation on a keyed provider into a login prompt."""
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    env = run_calls(record)[0]["env"]
    assert "OPENCODE_DATA_DIR" not in env and "XDG_DATA_HOME" not in env


async def test_nothing_at_all_crosses_from_the_users_configuration(stub, adapter):
    """The MCP servers, instructions, agents, permissions *and* providers beside them
    all stay behind. Nothing is read out of the user's config, so nothing has to be
    argued safe to carry."""
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    written = run_calls(record)[0]["project_config"]
    assert "agent" not in written
    assert written["mcp"] == {} and written["instructions"] == []
    assert written["permission"] == {"*": "deny"}
    # Nothing of the resolved configuration reaches the child, credential included.
    assert "sk-someone-elses-key" not in json.dumps(written)
    assert not opencode_stub.calls(record, "debug config")


async def test_no_provider_is_written_so_no_model_can_be_served_locally(stub, adapter):
    """The one that keeps this runtime like the other three. A `provider` block is the
    only way an endpoint outside opencode's own catalogue is named, which is to say the
    only way a *local* one is; writing none leaves the child hosted providers and
    nothing else. Checked against the real CLI too: under this configuration
    `--model ollama/qwen2.5:7b` fails with `ProviderModelNotFoundError`."""
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    call = run_calls(record)[0]
    for written in (call["project_config"], call["env_config"]):
        assert "provider" not in written
    assert "localhost" not in json.dumps(call["project_config"])


async def test_readiness_cannot_see_the_users_global_configuration(stub, adapter):
    """Otherwise a model the user has configured locally is reported ready and then
    fails at the run, where that provider does not exist."""
    record = stub(models=f"{MODEL}\n")
    await adapter.preflight(agent())

    call = opencode_stub.calls(record, "models")[0]
    assert call["xdg_entries"] == []


async def test_a_stray_jsonc_beside_the_written_config_is_cleared(stub, adapter, scratch_under):
    """opencode merges both spellings in one directory rather than picking one, with
    `.jsonc` landing last and winning any key it repeats. Nothing writes one here, which
    is exactly the assumption worth one unlink."""
    record = stub(run=[{"stdout": stream()}])
    (scratch_under / "opencode").mkdir(mode=0o700)
    session = scratch_under / "opencode" / _session_dir(agent().agent_id)
    session.mkdir(mode=0o700)
    (session / "opencode.jsonc").write_text('{"permission": {"bash": "allow"}}')

    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert Path(run_calls(record)[0]["cwd"]) == session
    assert not (session / "opencode.jsonc").exists()


async def test_the_written_configuration_is_readable_only_by_this_user(stub, adapter):
    """It is the file saying this consultation may run no tools."""
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    session = Path(run_calls(record)[0]["cwd"])
    for name in ("opencode.json", "config.json"):
        assert (session / name).stat().st_mode & 0o777 == 0o600, name


async def test_two_agents_do_not_share_a_working_directory(stub, adapter):
    """Identical bytes today, and resting on that is what made a shared directory look
    safe the last time it was argued: one release that gives an agent something of its
    own to write turns it back into a race."""
    record = stub(run=[{"stdout": stream()}, {"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)
    await adapter.start(agent(agent_id="other"), prompt(), SourceMode.MODEL)

    first, second = run_calls(record)
    assert first["cwd"] != second["cwd"]
    # Still under the one root, so `_scratch`'s ownership check and its clearing of a
    # stray ancestor config cover both.
    assert Path(first["cwd"]).parent == Path(second["cwd"]).parent


async def test_no_api_key_from_this_process_reaches_the_child(stub, adapter, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-yours")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-nor-this")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-nor-that")
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    for call in opencode_stub.calls(record):
        assert not [name for name in call["env"] if name.endswith("_API_KEY")]


async def test_no_variable_naming_an_endpoint_reaches_the_child(stub, adapter, monkeypatch):
    """The hosted-only guarantee is that no configuration this adapter runs under can
    name an endpoint outside opencode's catalogue. A `provider` block is one way in;
    the environment is the other, and until this test the environment side rested on
    reading `child_env` and seeing an allowlist rather than on anything that would fail
    if the allowlist grew. Two reviewers read the same code and both concluded it was
    a denylist of `*_API_KEY`; that they were wrong is exactly why it needs asserting."""
    endpoints = {
        "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1",
        "OPENAI_API_BASE": "http://127.0.0.1:11434/v1",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
        "DEEPSEEK_BASE_URL": "http://127.0.0.1:8080",
        "AZURE_OPENAI_ENDPOINT": "http://127.0.0.1:8080",
        "OLLAMA_HOST": "127.0.0.1:11434",
        "HTTP_PROXY": "http://127.0.0.1:3128",
        "HTTPS_PROXY": "http://127.0.0.1:3128",
        "OPENCODE_CONFIG": "/tmp/theirs.json",
        "OPENCODE_CONFIG_CONTENT": '{"provider": {"ollama": {}}}',
        "XDG_CONFIG_HOME": "/tmp/theirs",
    }
    for name, value in endpoints.items():
        monkeypatch.setenv(name, value)
    record = stub(run=[{"stdout": stream()}])
    await adapter.start(agent(), prompt(), SourceMode.MODEL)

    for call in opencode_stub.calls(record):
        for name, value in endpoints.items():
            assert call["env"].get(name) != value, name


async def test_a_config_above_the_scratch_directory_is_refused(stub, adapter, scratch_under):
    """opencode merges every `opencode.json` above the working directory, and a
    parent's permissions outrank ours -- one saying `bash: allow` re-enables the shell
    for a consultation that asked for no tools. `mcp` and `instructions` from up there
    cannot be unset from below at all, so the only defence is not to run."""
    record = stub(run=[{"stdout": stream()}])
    (scratch_under / "opencode.json").write_text('{"permission": {"bash": "allow"}}')

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert "opencode.json" in str(excinfo.value)
    assert not run_calls(record)


async def test_the_jsonc_spelling_of_an_inherited_config_is_refused_too(
    stub, adapter, scratch_under
):
    stub(run=[{"stdout": stream()}])
    (scratch_under / "opencode.jsonc").write_text("{}")
    with pytest.raises(AdapterError):
        await adapter.start(agent(), prompt(), SourceMode.MODEL)


async def test_a_config_left_in_our_own_scratch_root_is_cleared_not_refused(
    stub, adapter, scratch_under
):
    """The scratch root outlives the run, so a config an earlier layout wrote directly
    into it would be an ancestor of every consultation from then on -- refused forever.
    Nothing but this adapter can write there, so that one is ours to remove."""
    record = stub(run=[{"stdout": stream()}])
    root = scratch_under / "opencode"
    root.mkdir(mode=0o700)
    stale = root / "opencode.json"
    stale.write_text('{"permission": {"bash": "allow"}}')

    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.content.answer
    assert not stale.exists()
    assert run_calls(record)


# --- web mode ---------------------------------------------------------------


@pytest.mark.parametrize("web_search", [False, True], ids=["flag off", "flag on"])
async def test_web_mode_is_refused_whatever_the_agent_is_configured_for(
    stub, adapter, web_search
):
    """Unconditionally, not `if not agent.web_search`: this runtime has no web mode in
    v1, so an operator who turns the flag on must be refused rather than served a
    model-mode answer under a web-mode contract."""
    record = stub(run=[{"stdout": stream()}])

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(web_search=web_search), prompt(SourceMode.WEB), SourceMode.WEB)

    assert excinfo.value.code is ConsultErrorCode.WEB_SEARCH_UNAVAILABLE
    assert not opencode_stub.calls(record)


async def test_web_mode_is_refused_on_resume_as_well(stub, adapter):
    stub(run=[{"stdout": stream()}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.resume(agent(web_search=True), SESSION, prompt(), SourceMode.WEB)
    assert excinfo.value.code is ConsultErrorCode.WEB_SEARCH_UNAVAILABLE


# --- acting rather than answering -------------------------------------------


async def test_a_tool_part_fails_the_consultation(stub, adapter):
    """Named parts rather than named tools: no tool part was ever observed in this
    stream, so a check that looked for one by name would pass every run whether or not
    a tool had run. Anything that is not the three parts of an answer is the runtime
    acting, and fails closed."""
    stub(
        run=[
            {
                "stdout": jsonl(
                    event("step_start", part("step-start")),
                    event("tool", part("tool", tool="bash", state={"status": "completed"})),
                    event("text", part("text", text=json.dumps(CONTENT))),
                )
            }
        ]
    )
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "act rather than answer" in str(excinfo.value)


async def test_an_unknown_part_type_fails_rather_than_being_skipped(stub, adapter):
    stub(
        run=[
            {
                "stdout": jsonl(
                    event("text", part("text", text=json.dumps(CONTENT))),
                    event("patch", part("patch", files=["/etc/hosts"])),
                )
            }
        ]
    )
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


# --- failures the stream reports --------------------------------------------


async def test_an_error_event_is_an_unavailable_agent(stub, adapter):
    stub(
        run=[
            {
                "stdout": jsonl(
                    {
                        "type": "error",
                        "sessionID": SESSION,
                        "error": {"name": "UnknownError", "data": {"message": "no such model"}},
                    }
                ),
                # The event carries an opaque reference; the cause is only on stderr,
                # and only because the adapter asks for logs.
                "stderr": "ERROR provider=ollama connection refused\n",
            }
        ]
    )
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.AGENT_UNAVAILABLE
    assert "connection refused" in str(excinfo.value)


async def test_a_stream_with_no_text_is_not_an_empty_answer(stub, adapter):
    """What a refused tool call looks like from out here. Handing "" to the content
    parser would report it as bad JSON rather than as no reply at all."""
    stub(run=[{"stdout": jsonl(
        event("step_start", part("step-start")),
        event("step_finish", part("step-finish", tokens={"input": 1, "output": 0})),
    )}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert "no answer" in str(excinfo.value)


async def test_a_stream_that_never_finishes_a_step_is_refused(stub, adapter):
    """The exit code is deliberately not consulted, so the terminal part is the only
    thing in the stream that says the turn ended rather than was cut off. Without this
    a killed child's partial text came back as a finished answer, with its unknown cost
    reported as a known zero."""
    stub(run=[{"stdout": jsonl(
        event("step_start", part("step-start")),
        event("text", part("text", text=json.dumps(CONTENT))),
    )}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR
    assert "step-finish" in str(excinfo.value)


async def test_a_second_session_in_one_stream_is_refused(stub, adapter):
    """Every id used to overwrite the last, so the session handed back for the resume
    could belong to a different conversation than the text returned with it."""
    stub(run=[{"stdout": jsonl(
        event("step_start", part("step-start")),
        event("text", part("text", text=json.dumps(CONTENT)), session="ses_other"),
        event("step_finish", part("step-finish", tokens={"total": 1})),
    )}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert "ses_other" in str(excinfo.value)


async def test_a_resume_answered_under_another_session_is_refused(stub, adapter):
    """On a resume the session asked for is known before the stream is read, which
    makes this the check that the reply is to the question that was put."""
    stub(run=[{"stdout": stream(session="ses_somewhere_else")}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.resume(agent(), SESSION, prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_a_reasoning_part_is_neither_an_action_nor_the_answer(stub, adapter):
    """A model thinking out loud is not the runtime acting. Refusing it would fail a
    reasoning model's every consultation with a message accusing it of running a tool;
    appending it would feed the thinking to a parser expecting the answer."""
    stub(run=[{"stdout": jsonl(
        event("step_start", part("step-start")),
        event("reasoning", part("reasoning", text="let me work through this")),
        event("text", part("text", text=json.dumps(CONTENT))),
        event("step_finish", part("step-finish", tokens={"total": 9})),
    )}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.content.answer == CONTENT["answer"]


async def test_the_cost_reported_is_the_one_opencode_counted(stub, adapter):
    """Not `input + output`: opencode's total counts reasoning and the cache read too,
    and a cached prompt is mostly cache. Deriving it here under-reported by that whole
    share -- 2231 against 439 on a turn measured against the binary."""
    stub(run=[{"stdout": jsonl(
        event("step_start", part("step-start")),
        event("text", part("text", text=json.dumps(CONTENT))),
        event("step_finish", part("step-finish", tokens={
            "total": 2231, "input": 424, "output": 15,
            "reasoning": 0, "cache": {"write": 0, "read": 1792},
        })),
    )}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.usage.total_tokens == 2231
    assert result.usage.prompt_tokens == 424
    assert result.usage.completion_tokens == 15


async def test_a_cost_of_true_is_not_a_dollar(stub, adapter):
    """`bool` is a subclass of `int`, so `"cost": true` passes an isinstance check and
    `float(True)` bills the turn at one dollar. An unknown cost stays unknown."""
    stub(run=[{"stdout": jsonl(
        event("step_start", part("step-start")),
        event("text", part("text", text=json.dumps(CONTENT))),
        event("step_finish", part("step-finish", tokens={"total": 9}, cost=True)),
    )}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.usage.cost_usd is None


async def test_a_stream_with_no_session_id_is_refused(stub, adapter):
    stub(run=[{"stdout": jsonl({"type": "text", "part": {"type": "text", "text": "hi"}})}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR


async def test_a_non_event_line_is_not_a_protocol_failure(stub, adapter):
    """`--print-logs` puts diagnostics on stderr, but a line that is not an event is
    not the same thing as a broken one."""
    stub(run=[{"stdout": "listening on 127.0.0.1\n" + stream() + "not json\n"}])
    assert (await adapter.start(agent(), prompt(), SourceMode.MODEL)).content.answer == "blue"


async def test_a_line_that_opens_like_an_event_and_does_not_parse_fails_the_run(stub, adapter):
    """The other direction from the test above. Skipping a broken event is deciding what
    it said, and the part check is the only thing standing between a tool call and a
    consultation that reports none happened."""
    stub(run=[{"stdout": stream() + '{"type": "step_start", "part": {"type":\n'}])
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


def test_half_a_cost_is_not_a_total():
    """Billing both turns of a repair means both costs are known. One of them missing
    makes the sum unknown, not equal to the half that arrived."""
    known, unknown = Usage(total_tokens=3, cost_usd=0.5), Usage(total_tokens=4)
    assert _add(known, unknown).cost_usd is None
    assert _add(unknown, known).cost_usd is None
    assert _add(known, known).cost_usd == 1.0
    assert _add(known, unknown).total_tokens == 7


async def test_a_timeout_kills_the_run(stub, monkeypatch):
    stub(run=[{"stdout": stream(), "sleep": 5}])
    with pytest.raises(AdapterError) as excinfo:
        await OpenCodeCliAdapter(timeout_s=0.5).start(agent(), prompt(), SourceMode.MODEL)
    assert excinfo.value.code is ConsultErrorCode.TIMEOUT


# --- the repair turn --------------------------------------------------------


async def test_a_malformed_envelope_is_asked_again_once(stub, adapter):
    """There is no schema flag on this runtime, so the envelope rests on instruction
    alone and a small model drops a required key. One follow-up in the same session
    costs a few hundred characters; failing outright would waste the context already
    paid for."""
    record = stub(
        run=[
            {"stdout": stream(text=json.dumps({"answer": "blue"}))},
            {"stdout": stream()},
        ]
    )
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert result.content.answer == "blue"

    first, second = run_calls(record)
    assert "--session" not in first["argv"]
    # Into the session that just answered: on a document-mode consultation, resending
    # the task would be the whole context again.
    assert second["argv"][second["argv"].index("--session") + 1] == SESSION
    assert "sources" in second["stdin"] and len(second["stdin"]) < len(first["stdin"])


async def test_both_turns_are_billed(stub, adapter):
    record = stub(
        run=[
            {"stdout": stream(text="{}", input=100, output=10, total=110)},
            {"stdout": stream(input=30, output=5, total=35)},
        ]
    )
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert len(run_calls(record)) == 2
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (130, 15)
    assert result.usage.total_tokens == 145


async def test_the_record_of_a_repair_holds_the_turn_that_failed(stub, adapter):
    """`raw_output` is what a human reads back when a consultation went wrong, and the
    repair path is the only one where it has something to explain. Keeping the retry's
    stream alone threw away the malformed answer that caused the repair."""
    stub(
        run=[
            {"stdout": stream(text=json.dumps({"answer": "blue"}))},
            {"stdout": stream()},
        ]
    )
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert '{\\"answer\\": \\"blue\\"}' in result.raw_output
    assert result.raw_output.count('"step-finish"') == 2


async def test_a_second_malformed_envelope_is_the_end_of_it(stub, adapter):
    """One, not a loop: a model that cannot produce the shape twice is not going to on
    the third ask."""
    record = stub(run=[{"stdout": stream(text="not json")}, {"stdout": stream(text="{}")}])

    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert excinfo.value.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED
    assert len(run_calls(record)) == 2


async def test_a_tool_part_is_not_repaired(stub, adapter):
    """The repair turn is for a malformed envelope. A runtime that acted is a refusal,
    and asking it again would be asking it to act again."""
    record = stub(
        run=[{"stdout": jsonl(event("tool", part("tool", tool="bash")))}, {"stdout": stream()}]
    )
    with pytest.raises(AdapterError):
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert len(run_calls(record)) == 1


# --- sessions ---------------------------------------------------------------


async def test_a_resume_continues_the_native_session(stub, adapter):
    record = stub(run=[{"stdout": stream()}])
    result = await adapter.resume(agent(), SESSION, prompt(), SourceMode.MODEL)

    argv = run_calls(record)[0]["argv"]
    assert argv[argv.index("--session") + 1] == SESSION
    assert result.native_session_id == SESSION


async def test_a_session_id_from_this_server_is_not_offered_to_opencode(stub, adapter):
    """opencode mints its own ids and has no flag that accepts ours, so the native id
    comes back out of the stream instead of going in."""
    record = stub(run=[{"stdout": stream()}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL, session_id="ours-1")

    assert "ours-1" not in run_calls(record)[0]["argv"]
    assert result.native_session_id == SESSION


# --- which model answered ---------------------------------------------------


async def test_the_model_is_read_back_from_the_stored_session(stub, adapter):
    """It is not in the event stream at all. `export` is the only place it appears,
    which is why verification costs one extra local command."""
    record = stub(run=[{"stdout": stream()}])
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.model_verified and result.model_used == MODEL
    assert opencode_stub.calls(record, "export")[0]["argv"] == ["export", SESSION]


async def test_a_substituted_model_is_refused(stub, adapter):
    stub(
        run=[{"stdout": stream()}],
        export={
            "stdout": json.dumps(
                {"info": {"model": {"id": "qwen2.5:0.5b", "providerID": "ollama"}}}
            )
        },
    )
    with pytest.raises(AdapterError) as excinfo:
        await adapter.start(agent(), prompt(), SourceMode.MODEL)
    assert excinfo.value.code is ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE


@pytest.mark.parametrize(
    "export",
    [
        pytest.param({"stdout": "", "returncode": 1}, id="the command failed"),
        pytest.param({"stdout": "not json"}, id="unreadable output"),
        pytest.param({"stdout": "{}"}, id="no session info"),
        pytest.param({"stdout": json.dumps({"info": {"model": {"id": "qwen2.5:7b"}}})},
                     id="half a model name"),
    ],
)
async def test_missing_model_metadata_is_an_unverified_answer_not_an_error(stub, adapter, export):
    """The answer is already in hand. `model_verified=False` says exactly what
    happened, and inventing a failure from absent metadata would make every quiet
    release of the CLI an outage."""
    stub(run=[{"stdout": stream()}], export=export)
    result = await adapter.start(agent(), prompt(), SourceMode.MODEL)

    assert result.content.answer == "blue"
    assert result.model_used == MODEL and not result.model_verified


# --- the ancestor chain -----------------------------------------------------
#
# Called directly rather than through the adapter, because these two are about the
# check's own rule. The walk starts at the directory it is given and goes up, so a
# mode planted here is judged before any directory the machine owns.


def chain(root: Path, mode: int) -> Path:
    middle = root / "middle"
    deep = middle / "deep"
    deep.mkdir(parents=True)
    middle.chmod(mode)
    return deep


def test_a_sticky_world_writable_ancestor_is_refused_too(tmp_path):
    """Deliberately, and this is the one that decides whether the suite passes on Linux:
    `/tmp` is `1777`. The sticky bit stops another account *renaming* `middle` out from
    under us, which is the rename race this check's docstring argues about -- but it
    does nothing about *creating* `middle/opencode.json` between `_refuse_inherited_config`
    reading the chain and opencode reading it, which is the reason `_scratch` sits under
    `$HOME` at all. Half the threat is not enough to pass."""
    with pytest.raises(AdapterError) as excinfo:
        REFUSE_WRITABLE_ANCESTORS(chain(tmp_path, 0o1777))

    assert str(tmp_path / "middle") in str(excinfo.value)
    assert excinfo.value.code is ConsultErrorCode.TRANSPORT_ERROR


def test_a_chain_this_account_owns_passes(tmp_path):
    """The refusals only mean something if the ordinary case is not refused as well.

    Skipped where the temporary directory's own ancestors are writable by others --
    `/tmp` again -- because there the outcome is settled by the machine rather than by
    anything this test built. That is the same fact `scratch_under` stubs the check out
    for, and it is why this one cannot be the thing that catches a regression there.
    """
    resolved = tmp_path.resolve()
    if any(parent.stat().st_mode & 0o022 for parent in resolved.parents):
        pytest.skip("this machine's temporary directory sits under a writable one")

    REFUSE_WRITABLE_ANCESTORS(chain(resolved, 0o755))
