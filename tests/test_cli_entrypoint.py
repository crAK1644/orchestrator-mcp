"""The console script, which nobody is supposed to run by hand.

An MCP server is spawned by its client and speaks a protocol on stdin, so the one
person typing its name at a shell is someone checking whether it installed. What
they must not get is a traceback: it reads as a crash in our code when it is a line
of their config, and it says nothing about which line.
"""

from __future__ import annotations

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


def test_a_config_error_is_one_line_on_stderr_not_a_traceback(monkeypatch, tmp_path):
    """The message already names the key and its allowed values; the frames between
    `main` and `host_runtime` add nothing the reader can act on."""
    monkeypatch.delenv("ORCHESTRATOR_HOST_RUNTIME", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"consult": consult_block()}))

    with pytest.raises(SystemExit) as exit:
        main([])

    assert "ORCHESTRATOR_HOST_RUNTIME" in str(exit.value)
