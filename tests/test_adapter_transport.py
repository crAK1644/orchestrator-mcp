"""The shared subprocess transport, against a fake executable.

No Codex, no Claude, no network, no account. What is being proved here is the
part that is identical for both runtimes: arguments are arguments, the prompt
arrives over stdin, our API keys do not travel, and a child that will not stop
gets its whole process group killed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from orchestrator_mcp.consult.adapters.base import (
    PASSTHROUGH_ENV,
    AdapterError,
    _terminate,
    child_env,
    resolve_command,
    run_process,
)
from orchestrator_mcp.consult.config import AgentConfig
from orchestrator_mcp.consult.errors import ConsultErrorCode

FAKE_CLI = str(Path(__file__).parent / "fixtures" / "fake_cli.py")


def fake(mode: str, *args: str) -> list[str]:
    return [sys.executable, FAKE_CLI, mode, *args]


# --- arguments and stdin ----------------------------------------------------


async def test_the_prompt_travels_over_stdin():
    result = await run_process(fake("echo"), "a very long document", timeout_s=30)
    assert result.stdout == "a very long document"
    assert result.returncode == 0


async def test_a_prompt_larger_than_the_argv_limit_still_gets_through():
    """Why stdin and not an argument: a document-mode context routinely exceeds
    `ARG_MAX`, and passing it as an argument would fail at the OS."""
    payload = "x" * (2 * 1024 * 1024)
    result = await run_process(fake("echo"), payload, timeout_s=60)
    assert len(result.stdout) == len(payload)


@pytest.mark.parametrize(
    "hostile",
    [
        "; rm -rf /tmp/nope",
        "$(touch /tmp/nope)",
        "`id`",
        "--model=$(whoami)",
        "a b c\nd",
    ],
)
async def test_arguments_are_arguments_and_never_shell(hostile):
    """A model-supplied string reaching an argument is data, not syntax."""
    result = await run_process(fake("argv", hostile), None, timeout_s=30)
    assert json.loads(result.stdout) == [hostile]


async def test_an_exit_code_is_reported_not_raised():
    result = await run_process(fake("fail"), None, timeout_s=30)
    assert result.returncode == 3
    assert "refused" in result.stderr


async def test_a_missing_executable_is_a_transport_error():
    with pytest.raises(AdapterError) as exc:
        await run_process(["/nonexistent/definitely-not-here"], None, timeout_s=5)
    assert exc.value.code is ConsultErrorCode.TRANSPORT_ERROR


# --- environment ------------------------------------------------------------


async def test_our_api_keys_do_not_travel_to_a_consulted_cli(monkeypatch):
    """The agent that starts this server may have provider keys in its environment.
    A consulted CLI authenticates with its own saved credentials and must not
    inherit them."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-secret")
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", "/private/config.yaml")

    result = await run_process(fake("env"), None, timeout_s=30)
    child = json.loads(result.stdout)

    assert not [name for name in child if name.endswith("_API_KEY")]
    assert "ORCHESTRATOR_CONFIG" not in child
    assert "secret" not in result.stdout
    # HOME still goes through: it is where the CLI's own credentials live.
    assert child["HOME"] == os.environ["HOME"]


def test_the_passthrough_list_carries_nothing_secret():
    assert not [name for name in PASSTHROUGH_ENV if "KEY" in name or "TOKEN" in name]
    assert child_env({"CODEX_HOME": "/tmp/iso"})["CODEX_HOME"] == "/tmp/iso"


async def test_the_child_runs_where_it_is_told(tmp_path):
    """Adapters hand the CLI a scratch directory, not the user's repository."""
    result = await run_process(fake("cwd"), None, timeout_s=30, cwd=tmp_path)
    assert Path(result.stdout).resolve() == tmp_path.resolve()


# --- timeout and cancellation -----------------------------------------------


async def test_a_hanging_child_times_out():
    started = time.perf_counter()
    with pytest.raises(AdapterError) as exc:
        await run_process(fake("hang"), None, timeout_s=0.5)
    assert exc.value.code is ConsultErrorCode.TIMEOUT
    assert time.perf_counter() - started < 5


async def alive(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        await asyncio.sleep(0.1)
    return True


async def test_a_timeout_kills_the_whole_process_group(tmp_path):
    """The fixture ignores SIGTERM and leaves a grandchild running. Signalling the
    process rather than the group would leak both."""
    pidfile = tmp_path / "pids"
    with pytest.raises(AdapterError) as exc:
        await run_process(fake("stubborn", str(pidfile)), None, timeout_s=1.0)
    assert exc.value.code is ConsultErrorCode.TIMEOUT

    child_pid, grandchild_pid = (int(p) for p in pidfile.read_text().split())
    assert not await alive(child_pid), "SIGTERM was ignored; the group was never killed"
    assert not await alive(grandchild_pid), "the CLI's own child outlived the consultation"


async def test_a_grandchild_outliving_a_reaped_leader_is_still_killed(tmp_path):
    """The child exits immediately and its grandchild keeps the pipe open. The group
    leader is reaped, so looking the group id up by pid finds nothing -- and that is
    precisely the case worth signalling, because the CLI forked a worker and left."""
    pidfile = tmp_path / "pids"
    with pytest.raises(AdapterError) as exc:
        await run_process(fake("orphan", str(pidfile)), None, timeout_s=1.0)
    assert exc.value.code is ConsultErrorCode.TIMEOUT

    _, grandchild_pid = (int(p) for p in pidfile.read_text().split())
    assert not await alive(grandchild_pid), "the group was never signalled"


async def test_cancellation_also_kills_the_child(tmp_path):
    """An outer deadline must not leave a CLI running against the user's account."""
    pidfile = tmp_path / "pids"
    task = asyncio.create_task(run_process(fake("stubborn", str(pidfile)), None, timeout_s=300))
    while not pidfile.exists():
        await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    child_pid, grandchild_pid = (int(p) for p in pidfile.read_text().split())
    assert not await alive(child_pid)
    assert not await alive(grandchild_pid)


# --- command resolution -----------------------------------------------------


def agent_config(command: str) -> AgentConfig:
    return AgentConfig(agent_id="a", runtime="codex", command=command, model="m")


def test_an_uninstalled_command_names_itself():
    with pytest.raises(AdapterError) as exc:
        resolve_command(agent_config("definitely-not-installed-anywhere"))
    assert exc.value.code is ConsultErrorCode.AGENT_NOT_INSTALLED
    assert "definitely-not-installed-anywhere" in str(exc.value)


def test_an_installed_command_resolves_to_an_absolute_path():
    assert Path(resolve_command(agent_config("python3"))).is_absolute()


# --- what a killed child leaves behind --------------------------------------


async def test_a_killed_child_hands_its_pipes_back_while_there_is_still_a_loop():
    """A transport nobody closed is a `RuntimeError` raised where nothing can catch it.

    A subprocess transport closes itself once the child has exited *and* every pipe
    has reached EOF, and the second half stops being true exactly where `_terminate`
    is reached: a `StreamReader` holding more than its limit has paused the transport,
    so no EOF is coming for a pipe nobody may read again. What is left is collected
    whenever the collector gets to it, and if the loop has closed by then,
    `BaseSubprocessTransport.__del__` calls into it and raises `RuntimeError: Event
    loop is closed` from a `__del__`, where the only thing that can report it is the
    unraisable hook.

    The line over the limit is the real path, not a stand-in: it is what
    `STREAM_LINE_LIMIT` refuses in a streamed consultation.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, time; sys.stdout.write('x' * 200_000); sys.stdout.flush(); time.sleep(300)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=1024,
    )
    with pytest.raises(ValueError):
        await process.stdout.readline()

    await _terminate(process)

    assert process.returncode is not None
    assert process._transport.is_closing()
