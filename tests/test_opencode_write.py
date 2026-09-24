"""The contained opencode worker: what it is handed, and what it reads back.

Nothing here runs the binary. What these hold down is that the worker is launched
confined, with the network it needs, writing nothing that outlives the step, and that a
stream which stops early is not read as a run that did nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator_mcp.code import sandbox
from orchestrator_mcp.code.adapters import opencode_write
from orchestrator_mcp.code.adapters.opencode_write import OpenCodeWriteAdapter
from orchestrator_mcp.consult.adapters.base import AdapterError, ProcessResult
from orchestrator_mcp.consult.config import AgentConfig
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.prompts import compile_execution_prompt

MODEL = "opencode/mimo-v2.5-free"
SESSION = "ses_01JWRITER"
BINARY = "/usr/local/bin/opencode"


def agent() -> AgentConfig:
    return AgentConfig(
        agent_id="oc-writer",
        runtime="opencode",
        command="opencode",
        model=MODEL,
        scores={"coding": 60},
    )


def stream(*extra: dict, text: str = "Renamed the helper.") -> str:
    counts = {"total": 2054, "input": 2050, "output": 4, "reasoning": 0, "cache": {}}
    events = [
        {"type": "text", "sessionID": SESSION, "part": {"type": "text", "text": text}},
        *extra,
        {
            "type": "step_finish",
            "sessionID": SESSION,
            "part": {"type": "step-finish", "tokens": counts, "cost": 0.25},
        },
    ]
    return "".join(json.dumps(event) + "\n" for event in events)


def tool_event(name: str, **arguments: object) -> dict:
    return {
        "type": "tool",
        "sessionID": SESSION,
        "part": {
            "type": "tool",
            "tool": name,
            "state": {"status": "completed", "input": arguments, "output": "ok"},
        },
    }


@pytest.fixture
def worktree(tmp_path) -> Path:
    path = tmp_path / "worktrees" / "wf" / "step"
    path.mkdir(parents=True)
    (path / "app.py").write_text("def main():\n    return 1\n")
    return path


@pytest.fixture
def launched(monkeypatch) -> list[dict]:
    """Record every launch instead of performing one, and hand back a fixed stream.

    The host's sandbox is pinned to seatbelt that holds with the internet grantable:
    these assert what the adapter asks for, and the containment itself is proven by
    the `@confined` tests in `test_sandbox.py`.
    """
    calls: list[dict] = []

    async def fake(argv, stdin_text, timeout_s, env=None, cwd=None, **_):
        calls.append({"argv": argv, "env": env or {}, "cwd": cwd, "stdin": stdin_text})
        # What the child sees at launch, before the adapter's cleanup runs.
        calls[-1]["config"] = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
        return ProcessResult(returncode=0, stdout=stream(), stderr="")

    monkeypatch.setattr(opencode_write, "run_process", fake)
    monkeypatch.setattr(opencode_write, "resolve_command", lambda _: BINARY)
    monkeypatch.setattr(sandbox, "holds", lambda: True)
    monkeypatch.setattr(sandbox, "internet_unavailable_reason", lambda: None)
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    monkeypatch.setattr(sandbox, "_binary", lambda: "/usr/bin/sandbox-exec")
    return calls


def returning(monkeypatch, stdout: str, stderr: str = "") -> None:
    async def fake(*_args, **_kwargs):
        return ProcessResult(returncode=0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(opencode_write, "run_process", fake)


async def execute(worktree: Path):
    prompt = compile_execution_prompt("coding", "rename the helper", None)
    return await OpenCodeWriteAdapter(30).execute(agent(), prompt, worktree, 30)


# --- what the process is given ----------------------------------------------


async def test_the_worker_is_launched_inside_the_sandbox(worktree, launched):
    await execute(worktree)
    argv = launched[0]["argv"]
    # A bare `opencode` at argv[0] is the whole capability being false.
    assert argv[0] != BINARY
    assert argv[argv.index(BINARY) + 1] == "run"
    assert argv[argv.index("--model") + 1] == MODEL


async def test_the_sandbox_is_asked_for_the_internet(worktree, launched, monkeypatch):
    seen: list[str] = []
    real = sandbox.wrap

    def record(command, tree, *, network, port=None):
        seen.append(network)
        return real(command, tree, network=network, port=port)

    monkeypatch.setattr(sandbox, "wrap", record)
    await execute(worktree)
    assert seen == [sandbox.NETWORK_INTERNET]


async def test_the_runtime_state_lives_in_the_worktree_and_leaves_with_it(worktree, launched):
    await execute(worktree)
    env = launched[0]["env"]
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        assert Path(env[name]).is_relative_to(worktree), name
    # Removed before the service reads git: a database in the patch is nobody's change.
    assert not (worktree / opencode_write.RUNTIME_DIR).exists()


async def test_the_runtime_directory_goes_even_when_the_run_fails(worktree, launched, monkeypatch):
    async def explode(*_args, **_kwargs):
        raise AdapterError(ConsultErrorCode.TIMEOUT, "took too long")

    monkeypatch.setattr(opencode_write, "run_process", explode)
    with pytest.raises(AdapterError):
        await execute(worktree)
    assert not (worktree / opencode_write.RUNTIME_DIR).exists()


async def test_the_worker_config_denies_what_the_sandbox_cannot_say(worktree, launched):
    await execute(worktree)
    assert launched[0]["config"]["permission"] =={"*": "allow", "webfetch": "deny", "external_directory": "deny"}


async def test_the_child_carries_no_provider_key_or_proxy(worktree, launched, monkeypatch):
    """`child_env` is an allowlist, and this is the test that says so."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    await execute(worktree)
    env = launched[0]["env"]
    assert "HTTP_PROXY" not in env and "ANTHROPIC_API_KEY" not in env


async def test_the_prompt_goes_over_stdin_rather_than_the_command_line(worktree, launched):
    await execute(worktree)
    assert "rename the helper" in launched[0]["stdin"]
    assert not [word for word in launched[0]["argv"] if "rename the helper" in word]


async def test_a_config_above_the_worktree_is_refused(worktree, launched):
    (worktree.parent / "opencode.json").write_text("{}")
    with pytest.raises(AdapterError, match="sits above"):
        await execute(worktree)
    assert not launched


@pytest.mark.parametrize("withheld", ["sandbox", "internet"])
async def test_a_host_that_stopped_confining_is_refused_before_the_launch(
    worktree, launched, monkeypatch, withheld
):
    """The registry asked at boot; the adapter asks again at the line that launches."""
    if withheld == "sandbox":
        monkeypatch.setattr(sandbox, "holds", lambda: False)
    else:
        monkeypatch.setattr(sandbox, "internet_unavailable_reason", lambda: "no network")
    with pytest.raises(AdapterError, match="opencode cannot"):
        await execute(worktree)
    assert not launched


# --- what comes back --------------------------------------------------------


async def test_the_commands_it_ran_and_the_paths_it_claims_are_kept_apart(
    worktree, launched, monkeypatch
):
    returning(
        monkeypatch,
        stream(
            tool_event("bash", command="pytest -q"),
            tool_event("edit", filePath="app.py"),
            tool_event("write", filePath="helper.py"),
        ),
    )
    run = await execute(worktree)
    assert [command.command for command in run.commands] == ["pytest -q"]
    assert run.claimed_paths == ["app.py", "helper.py"]
    assert run.summary == "Renamed the helper."
    assert run.native_session_id == SESSION
    assert (run.model_used, run.model_verified) == (MODEL, False)


async def test_two_steps_are_added_rather_than_the_last_one_kept(worktree, launched, monkeypatch):
    returning(monkeypatch, stream() + stream())
    usage = (await execute(worktree)).usage
    assert (usage.total_tokens, usage.cost_usd) == (4108, 0.5)


async def test_a_stream_that_stops_early_is_not_a_run_that_cost_nothing(
    worktree, launched, monkeypatch
):
    returning(
        monkeypatch,
        json.dumps({"type": "text", "sessionID": SESSION, "part": {"type": "text", "text": "h"}}),
        "killed",
    )
    with pytest.raises(AdapterError, match="stopped rather than finished"):
        await execute(worktree)


async def test_an_error_event_fails_the_run_despite_the_zero_exit(worktree, launched, monkeypatch):
    returning(monkeypatch, '{"type":"error","error":{"name":"ProviderAuthError"}}\n', "no key")
    with pytest.raises(AdapterError, match="ProviderAuthError"):
        await execute(worktree)
