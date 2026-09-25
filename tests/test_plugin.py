"""The Claude Code plugin: its manifests, the server it starts before a config exists,
and the tool annotations the plugin directory asks for.

The plugin pins the PyPI release it runs, in places `pyproject.toml` knows nothing
about. A release that bumps one and not the others ships a plugin running the version
before it -- or one PyPI has never heard of.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from orchestrator_mcp import cli
from orchestrator_mcp.contract import ConfigError
from orchestrator_mcp.server import build_server, load_config

from .conftest import consult_block

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"


def test_every_pin_follows_the_package_version():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    setup = (PLUGIN / "commands" / "setup.md").read_text()

    assert manifest["version"] == version
    assert manifest["mcpServers"]["orchestrator"]["args"] == [f"orchestrator-mcp-server@{version}"]
    assert set(re.findall(r"orchestrator-mcp-server@([\w.]+)", setup)) == {version}


def test_the_marketplace_points_at_a_plugin():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())

    for entry in marketplace["plugins"]:
        assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()


async def test_a_missing_config_starts_a_server_that_says_how_to_write_one(
    tmp_path, monkeypatch, host_claude
):
    """The plugin host spawns the server before anyone has run `init`."""
    missing = tmp_path / "absent.yaml"
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(missing))
    server = build_server()

    assert [tool.name for tool in await server.list_tools()] == ["orchestrator_setup"]
    text = (await server.call_tool("orchestrator_setup", {})).content[0].text
    assert str(missing) in text
    assert "--host claude" in text


@pytest.mark.parametrize("configured", [None, "custom dir/config.yaml"], ids=["default", "custom"])
async def test_the_stub_names_the_file_the_next_start_reads(
    tmp_path, monkeypatch, host_claude, configured
):
    """`init` writes to its own default unless told where, so the command has to carry
    the path -- absolute, because `init` runs from some other directory."""
    monkeypatch.chdir(tmp_path)
    if configured:
        monkeypatch.setenv("ORCHESTRATOR_CONFIG", configured)
    else:
        monkeypatch.delenv("ORCHESTRATOR_CONFIG", raising=False)
    text = (await build_server().call_tool("orchestrator_setup", {})).content[0].text

    argv = shlex.split(text.split("`")[1])
    written = Path(argv[argv.index("--path") + 1])
    assert written.is_absolute()
    written.parent.mkdir(exist_ok=True)
    written.write_text("consult: {}\n")
    assert load_config() == {"consult": {}}


def test_a_config_that_exists_but_is_wrong_still_refuses(tmp_path, monkeypatch):
    """Only a missing file gets the stub. A wrong one is a mistake to fix, and a stub
    would hide it behind a server that looks like it started."""
    (tmp_path / "config.yaml").write_text("- not a mapping\n")
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(tmp_path / "config.yaml"))

    with pytest.raises(ConfigError, match="mapping"):
        build_server()


def test_doctor_still_fails_a_missing_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(tmp_path / "absent.yaml"))

    assert cli.doctor(load_config) == 1
    assert capsys.readouterr().out.startswith("FAIL config: config not found")


async def test_every_tool_carries_every_hint(tmp_path, monkeypatch, host_claude):
    """The directory requires them, and MCP reads a missing hint as the worst case:
    destructive, and reaching off the machine."""
    full = build_server(
        {
            "consult": consult_block(
                review={"reviewers": ["codex-sol"], "deep_reviewers": ["claude-opus"]},
                workflow={"bindings": {"research": {"agent": "codex-sol"}}},
            )
        }
    )
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(tmp_path / "absent.yaml"))
    tools = await full.list_tools() + await build_server().list_tools()

    for tool in tools:
        hints = tool.annotations
        assert hints and hints.title, tool.name
        assert None not in (
            hints.read_only_hint,
            hints.destructive_hint,
            hints.idempotent_hint,
            hints.open_world_hint,
        ), tool.name
        # The one mistake a client acts on: a deletion it would run without asking.
        if "delete" in tool.name and "request_delete" not in tool.name:
            assert hints.destructive_hint and not hints.read_only_hint, tool.name
