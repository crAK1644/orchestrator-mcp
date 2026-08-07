"""The agents file the dashboard owns, and how it merges with `config.yaml`.

Two files describing one routing table is the risky part of the design, so what is
asserted here is mostly the seams: that a merged agent is indistinguishable from a
written one everywhere it matters, and that the one case where they disagree stops
the server rather than picking a winner.
"""

from __future__ import annotations

import os
import stat

import pytest
import yaml

from orchestrator_mcp.consult.config import ConsultConfig, load_consult_config
from orchestrator_mcp.consult.managed import read_managed, write_managed
from orchestrator_mcp.contract import ConfigError

from .conftest import agent, consult_block


@pytest.fixture
def managed(tmp_path):
    """Write a managed file and return the config block pointing at it."""
    path = tmp_path / "agents.yaml"

    def write(agents: dict, **overrides):
        path.write_text(yaml.safe_dump({"agents": agents}))
        return consult_block(managed_agents_path=str(path), **overrides)

    write.path = path
    return write


def test_a_missing_file_is_no_agents_rather_than_an_error(tmp_path):
    """The common case by a distance: nobody has opened the dashboard here."""
    config = load_consult_config(
        {"consult": consult_block(managed_agents_path=str(tmp_path / "nope.yaml"))}
    )
    assert sorted(config.agents) == ["claude-opus", "codex-sol"]


def test_a_managed_agent_lands_beside_the_written_ones(managed):
    block = managed({"codex-luna": agent("codex", "gpt-5.6-luna", 5)})
    config = load_consult_config({"consult": block})

    assert sorted(config.agents) == ["claude-opus", "codex-luna", "codex-sol"]
    assert config.agents["codex-luna"].agent_id == "codex-luna"
    assert config.agents["codex-luna"].managed is True
    assert config.agents["codex-sol"].managed is False
    # And it routes: being in the other file is not a second class of agent.
    assert "codex-luna" in [a.agent_id for a in config.eligible("claude")]


def test_the_same_id_in_both_files_refuses_to_boot(managed):
    block = managed({"codex-sol": agent("codex", "gpt-5.6-sol", 5)})
    with pytest.raises(ConfigError, match="codex-sol"):
        load_consult_config({"consult": block})


def test_the_refusal_names_both_places_to_look(managed):
    block = managed({"codex-sol": agent(), "claude-opus": agent("claude", "opus")})
    with pytest.raises(ConfigError) as caught:
        load_consult_config({"consult": block})

    message = str(caught.value)
    assert "codex-sol" in message and "claude-opus" in message
    assert str(managed.path) in message, "the operator has to be told which file to edit"


def test_a_malformed_managed_file_refuses_to_boot(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text("agents: [not, a, mapping]")
    with pytest.raises(ConfigError, match="mapping"):
        load_consult_config({"consult": consult_block(managed_agents_path=str(path))})

    path.write_text("agents: {oops: [")
    with pytest.raises(ConfigError, match="valid YAML"):
        load_consult_config({"consult": consult_block(managed_agents_path=str(path))})

    # A decode error is not an `OSError` and not a YAML one: the bytes reach `read_text`
    # and fail there, which used to end a boot with a raw traceback naming no file.
    path.write_bytes(b"\xff\xfe agents:")
    with pytest.raises(ConfigError, match="cannot be read"):
        load_consult_config({"consult": consult_block(managed_agents_path=str(path))})


def test_an_invalid_managed_agent_refuses_to_boot_like_any_other(managed):
    """Merged before validation, so the managed file gets the same scrutiny rather
    than a lenient path of its own."""
    block = managed({"nope": agent(runtime="claude", reasoning_effort="xhigh")})
    with pytest.raises(ConfigError):
        load_consult_config({"consult": block})


def test_moving_an_agent_between_the_files_does_not_change_the_config_hash(managed, tmp_path):
    """`config_hash` is recorded against every consultation so a stored reply can be
    read against the routing table that produced it. Which file an agent lives in is
    not part of that table, and a hash that moved would strand every stored row."""
    written = ConsultConfig(**consult_block())

    block = managed({"claude-opus": agent("claude", "opus", 20)})
    block["agents"] = {"codex-sol": agent("codex", "gpt-5.6-sol", 10)}
    split = load_consult_config({"consult": block})

    assert sorted(split.agents) == sorted(written.agents)
    assert split.config_hash() == written.config_hash()


def test_a_managed_flag_written_by_hand_does_not_decide_anything(managed):
    """`managed:` is a declared field, so a config could set it. Where an agent lives
    is decided by the file it is in -- otherwise the dashboard would offer to edit an
    entry it cannot write."""
    block = managed({"codex-luna": agent(managed=False)})
    block["agents"]["codex-sol"]["managed"] = True
    config = load_consult_config({"consult": block})

    assert config.agents["codex-luna"].managed is True
    assert config.agents["codex-sol"].managed is False


# --- the file itself --------------------------------------------------------


def test_a_write_lands_private_in_a_private_directory(tmp_path):
    path = tmp_path / "nested" / "agents.yaml"
    write_managed(path, {"a": {"runtime": "codex", "command": "codex", "model": "m"}})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert read_managed(path) == {"a": {"runtime": "codex", "command": "codex", "model": "m"}}


def test_a_write_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "agents.yaml"
    write_managed(path, {"a": {"runtime": "codex"}})
    write_managed(path, {"a": {"runtime": "claude"}})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["agents.yaml"]


def test_the_file_says_where_it_came_from(tmp_path):
    """It is going to be found by someone who did not put it there."""
    path = tmp_path / "agents.yaml"
    write_managed(path, {})
    assert "dashboard" in path.read_text().splitlines()[0]


def test_expanding_the_path_happens_before_anything_opens_it(monkeypatch, tmp_path):
    """`~` reaching `open()` creates a directory literally named `~`, which is the
    same bug `database_path` has the same guard against."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = ConsultConfig(**consult_block(managed_agents_path="~/agents.yaml"))
    assert config.managed_agents_path == tmp_path / "agents.yaml"
    assert not (os.getcwd() + "/~") in str(config.managed_agents_path)
