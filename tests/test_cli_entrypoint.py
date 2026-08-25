"""The console script, which nobody is supposed to run by hand.

An MCP server is spawned by its client and speaks a protocol on stdin, so the one
person typing its name at a shell is someone checking whether it installed. What
they must not get is a traceback: it reads as a crash in our code when it is a line
of their config, and it says nothing about which line.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import yaml

from orchestrator_mcp.server import main

from .conftest import consult_block


def test_help_prints_help_rather_than_starting_a_server(capsys):
    """`--help` used to be swallowed: `main` read no arguments at all, so the flag
    went straight past into a server booting against the caller's real config."""
    main(["--help"])

    out = capsys.readouterr().out
    assert "ORCHESTRATOR_HOST_RUNTIME" in out
    assert "--version" in out


def test_version_prints_the_installed_version(capsys):
    main(["--version"])

    assert capsys.readouterr().out.strip()


def test_an_unknown_argument_is_refused_instead_of_ignored(capsys):
    """Ignoring it would leave the typo looking like a server that started and went
    quiet, which is exactly what a working stdio server looks like."""
    with pytest.raises(SystemExit) as exit:
        main(["--helpp"])

    assert exit.value.code == 2
    assert "--helpp" in capsys.readouterr().err


def test_a_typo_is_still_refused_when_a_real_flag_is_beside_it(capsys):
    """Answering the flag that was spelled right would send the reader off believing
    the other one took as well."""
    with pytest.raises(SystemExit) as exit:
        main(["--help", "--helpp"])

    assert exit.value.code == 2
    assert "--helpp" in capsys.readouterr().err


def test_a_config_error_leaves_as_a_systemexit_carrying_its_message(monkeypatch, tmp_path):
    """Which is what makes it one line on stderr: the interpreter prints a `SystemExit`
    built from a string and stops, where any other exception prints the frames. The
    message already names the key and its values; the frames between `main` and
    `host_runtime` name only our modules."""
    monkeypatch.delenv("ORCHESTRATOR_HOST_RUNTIME", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"consult": consult_block()}))

    with pytest.raises(SystemExit) as exit:
        main([])

    assert "ORCHESTRATOR_HOST_RUNTIME" in str(exit.value)


def test_the_installed_script_prints_that_message_and_no_frames(tmp_path):
    """The same path in the process that actually has a shell attached.

    Everything above calls `main` in-process, where the interpreter's own handling of
    a `SystemExit` never runs -- and that handling is the half of the claim: the message
    on stderr, no frames behind it, a non-zero status. Run it for real once. Not pinned
    to one line, because a pydantic `consult:` error is legitimately several.
    """
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"consult": consult_block()}))
    # `HOME` too: the managed agents file is `~/.orchestrator-mcp/agents.yaml`, and the
    # autouse fixture that points it somewhere disposable cannot reach a subprocess.
    # Without this the developer's own agents decide which error this run produces.
    env = {k: v for k, v in os.environ.items() if k != "ORCHESTRATOR_HOST_RUNTIME"}
    env["HOME"] = str(tmp_path)

    done = subprocess.run(
        [sys.executable, "-m", "orchestrator_mcp.server"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 1
    assert "Traceback" not in done.stderr
    assert done.stderr.startswith("orchestrator-mcp-server: ORCHESTRATOR_HOST_RUNTIME")
