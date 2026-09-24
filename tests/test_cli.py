"""`init` and `doctor`, the two subcommands a person does run by hand."""

from __future__ import annotations

import stat

import pytest
import yaml

from orchestrator_mcp import cli
from orchestrator_mcp.consult.adapters.base import AgentStatus
from orchestrator_mcp.consult.config import load_consult_config
from orchestrator_mcp.consult.service import ConsultService
from orchestrator_mcp.server import main

from .conftest import consult_block


@pytest.fixture
def installed(monkeypatch):
    """Every CLI `init` looks for, at a made-up absolute path."""
    monkeypatch.setattr(cli, "_find", lambda candidates: f"/opt/bin/{candidates[0]}")


def test_init_writes_a_config_the_server_accepts(installed, tmp_path, capsys):
    target = tmp_path / "sub" / "config.yaml"

    assert cli.init(["--host", "claude", "--path", str(target)]) == 0

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    config = yaml.safe_load(target.read_text())
    parsed = load_consult_config(config)
    assert set(parsed.agents) == {"codex-sol", "claude-opus", "gemini-reviewer"}
    # The host never reviews itself: the best of the rest does.
    assert parsed.review.reviewers == ["codex-sol"]
    assert parsed.review.deep_reviewers == ["codex-sol", "gemini-reviewer"]
    assert "workflow" not in config
    assert "claude mcp add orchestrator" in capsys.readouterr().out


def test_init_never_overwrites(installed, tmp_path, capsys):
    target = tmp_path / "config.yaml"
    target.write_text("mine")

    assert cli.init(["--host", "claude", "--path", str(target)]) == 1

    assert target.read_text() == "mine"
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["--host", "nobody"], ["--host", "claude", "--force"]])
def test_init_refuses_a_missing_host_or_a_stray_argument(installed, tmp_path, args):
    assert cli.init([*args, "--path", str(tmp_path / "c.yaml")]) == 2
    assert not (tmp_path / "c.yaml").exists()


def test_init_needs_a_reviewer_besides_the_host(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_find", lambda c: "/opt/bin/claude" if c == ["claude"] else None)

    assert cli.init(["--host", "claude", "--path", str(tmp_path / "c.yaml")]) == 1

    assert "no reviewer CLI" in capsys.readouterr().err
    assert not (tmp_path / "c.yaml").exists()


def test_init_leaves_out_what_the_dashboard_already_owns(installed, tmp_path):
    """The server refuses an agent or a `review:` named in both files."""
    from orchestrator_mcp.consult import managed

    managed.DEFAULT_MANAGED_PATH.write_text(yaml.safe_dump({
        "agents": {"gemini-reviewer": {
            "runtime": "antigravity", "command": "agy", "model": "m", "scores": {"review": 80},
        }},
        "review": {"reviewers": ["gemini-reviewer"], "deep_reviewers": ["gemini-reviewer"]},
    }))
    target = tmp_path / "c.yaml"

    assert cli.init(["--host", "claude", "--path", str(target)]) == 0

    consult = yaml.safe_load(target.read_text())["consult"]
    assert "gemini-reviewer" not in consult["agents"]
    assert "review" not in consult


def test_main_routes_init(installed, tmp_path):
    with pytest.raises(SystemExit) as exit:
        main(["init", "--host", "codex", "--path", str(tmp_path / "c.yaml")])

    assert exit.value.code == 0


def test_doctor_takes_no_arguments(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["doctor", "--fix"])

    assert exit.value.code == 2


class _Preflight:
    def __init__(self, logged_in: bool) -> None:
        self.logged_in = logged_in

    async def preflight(self, agent):
        return AgentStatus(agent.agent_id, installed=True, authenticated=self.logged_in,
                           detail=None if self.logged_in else "run `codex login`")


def _doctor(monkeypatch, tmp_path, config, logged_in=True):
    monkeypatch.setattr(ConsultService, "adapter", lambda self, agent: _Preflight(logged_in))
    return cli.doctor(lambda: config)


def test_doctor_passes_a_working_setup(host_claude, monkeypatch, tmp_path, capsys):
    config = {"consult": consult_block(database_path=str(tmp_path / "db.sqlite3"))}

    assert _doctor(monkeypatch, tmp_path, config) == 0

    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "ok   agent codex-sol" in out
    assert "agent claude-opus: the host's own runtime" in out


def test_doctor_fails_an_agent_that_is_not_logged_in(host_claude, monkeypatch, tmp_path, capsys):
    config = {"consult": consult_block(database_path=str(tmp_path / "db.sqlite3"))}

    assert _doctor(monkeypatch, tmp_path, config, logged_in=False) == 1

    assert "FAIL agent codex-sol: run `codex login`" in capsys.readouterr().out


def test_doctor_fails_a_broken_config(host_claude, monkeypatch, tmp_path, capsys):
    config = {"consult": consult_block(review={"reviewers": ["nobody"]})}

    assert _doctor(monkeypatch, tmp_path, config) == 1

    assert capsys.readouterr().out.startswith("FAIL config:")
