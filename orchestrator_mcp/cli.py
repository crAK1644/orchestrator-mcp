"""`init` and `doctor`: the two things a person runs by hand before a client does.

`init` writes a starter config from the CLIs it finds, so the first thing a user
edits is a file that already starts. `doctor` answers "will it work" before the first
tool call does, one line per check. Neither sends any project material anywhere: the
only subprocesses are the CLIs' own login checks.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any, get_args

import yaml

from .consult.config import HOST_RUNTIME_ENV, host_runtime, load_consult_config
from .consult.contract import Runtime
from .consult.managed import managed_path, read_managed_document
from .contract import ConfigError

DEFAULT_PATH = "~/.orchestrator-mcp/config.yaml"

# What `init` offers per runtime: where to look for the CLI, and the agent it writes
# if one is found. Taken from `config.example.yaml`; the scores are starting points
# for the user to edit, not measurements.
# ponytail: no opencode entry. Its free models rotate, so any model written here goes
# stale; add one by hand from `opencode models` (config.example.yaml shows how).
_TEMPLATES: dict[str, tuple[list[str], str, dict[str, Any]]] = {
    "codex": (
        # The ChatGPT desktop app bundles the CLI and does not put it on PATH.
        ["codex", "/Applications/ChatGPT.app/Contents/Resources/codex"],
        "codex-sol",
        {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "priority": 10,
            "web_search": True,
            "scores": {"coding": 95, "research": 85, "reasoning": 90, "review": 95,
                       "planning": 85, "prompt_authoring": 80, "synthesis": 85},
        },
    ),
    "claude": (
        ["claude"],
        "claude-opus",
        {
            "runtime": "claude",
            "model": "claude-opus-5-5",
            "priority": 20,
            "web_search": True,
            "scores": {"writing": 95, "review": 90, "research": 90, "reasoning": 90},
        },
    ),
    "antigravity": (
        # Installs to ~/.local/bin, which is often not on a GUI client's PATH.
        ["agy", "~/.local/bin/agy"],
        "gemini-reviewer",
        {
            "runtime": "antigravity",
            "model": "gemini-3.1-pro-high",
            "priority": 30,
            "scores": {"reasoning": 90, "review": 85},
        },
    ),
}


def _find(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if found := shutil.which(os.path.expanduser(candidate)):
            # Absolute, because a GUI-launched client inherits a smaller PATH than
            # the terminal `init` ran in. Not resolved: a Homebrew symlink survives
            # `brew upgrade`, the versioned path behind it does not.
            return str(Path(found).absolute())
    return None


def starter_config(host: str) -> dict[str, Any]:
    """The config `init` would write for `host`, from the CLIs installed now.

    Whatever the dashboard's managed file already defines is left out: the server
    refuses an agent id or a `review:` block named in both files.
    """
    managed = read_managed_document(managed_path({}))
    agents = {
        agent_id: {**template, "command": command}
        for candidates, agent_id, template in _TEMPLATES.values()
        if agent_id not in managed["agents"] and (command := _find(candidates))
    }
    others = sorted(
        # A reviewer must score above 0 for `review`, or the config is refused.
        (a for a in agents.items() if a[1]["runtime"] != host and a[1]["scores"].get("review")),
        key=lambda a: (-a[1]["scores"].get("review", 0), a[1]["priority"]),
    )
    if not others and not managed["review"]:
        raise ConfigError(
            f"no reviewer CLI found besides the host (`{host}`): install and log in to "
            "one of codex, claude or agy (antigravity), then run init again"
        )
    consult: dict[str, Any] = {
        "database_path": "~/.orchestrator-mcp/consultations.sqlite3",
        "timeout_s": 180,
        "agents": agents,
    }
    if not managed["review"]:
        consult["review"] = {
            "reviewers": [others[0][0]],
            "deep_reviewers": [agent_id for agent_id, _ in others[:5]],
        }
    return {"consult": consult}


_HEADER = """\
# Written by `orchestrator-mcp-server init`. Edit freely; see config.example.yaml
# in the project for every key. There is no `workflow:` block because a workflow
# needs `roots:` -- the directories it may work in -- and only you can choose those.
"""


def init(args: list[str]) -> int:
    host, path = None, DEFAULT_PATH
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag in ("--host", "--path") and rest:
            value = rest.pop(0)
            host, path = (value, path) if flag == "--host" else (host, value)
        else:
            print(f"orchestrator-mcp-server init: unexpected argument {flag!r}", file=sys.stderr)
            return 2
    if host not in get_args(Runtime):
        # Never inferred, for the same reason the server reads it from the
        # environment: the host is the one runtime routing must leave out.
        print(
            "orchestrator-mcp-server init: --host is required: the client this server "
            f"runs under, one of {', '.join(get_args(Runtime))}",
            file=sys.stderr,
        )
        return 2
    target = Path(path).expanduser().absolute()
    try:
        config = starter_config(host)
        load_consult_config(config)  # never write a file the server would refuse
    except ConfigError as exc:
        print(f"orchestrator-mcp-server init: {exc}", file=sys.stderr)
        return 1
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # O_EXCL: the file is the user's once it exists, and 0600 because it names
        # which CLIs and accounts this machine uses.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(
            f"orchestrator-mcp-server init: {target} already exists; not overwriting it. "
            "Pass --path to write somewhere else.",
            file=sys.stderr,
        )
        return 1
    with os.fdopen(fd, "w") as out:
        out.write(_HEADER + yaml.safe_dump(config, sort_keys=False))

    found = ", ".join(f"{a} ({c['command']})" for a, c in config["consult"]["agents"].items())
    print(f"wrote {target}\nagents: {found or 'none new'}")
    if "review" not in config["consult"]:
        print(f"review: left out, {managed_path({})} already sets the reviewers")
    print()
    print("Add the server to your client:\n")
    env = f"ORCHESTRATOR_CONFIG={target} {HOST_RUNTIME_ENV}={host}"
    if host == "claude":
        print(
            f"  claude mcp add orchestrator --env ORCHESTRATOR_CONFIG={target} "
            f"--env {HOST_RUNTIME_ENV}=claude -- orchestrator-mcp-server\n"
        )
    elif host == "codex":
        print(
            "  # in ~/.codex/config.toml\n  [mcp_servers.orchestrator]\n"
            '  command = "orchestrator-mcp-server"\n'
            f'  env = {{ ORCHESTRATOR_CONFIG = "{target}", {HOST_RUNTIME_ENV} = "codex" }}\n'
        )
    else:
        print(f"  command: orchestrator-mcp-server\n  env: {env}\n")
    print(f"Then check it:\n\n  {env} orchestrator-mcp-server doctor")
    return 0


def doctor(load_config) -> int:
    """One `ok` / `FAIL` line per check; 1 if anything failed."""
    failed = False

    def line(ok: bool, what: str, detail: str = "") -> None:
        nonlocal failed
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {what}{': ' + detail if detail else ''}")

    try:
        from .server import build_server

        config = load_config()
        build_server(config)  # every refusal the server would start with
        consult, runtime = load_consult_config(config), host_runtime()
    except ConfigError as exc:
        line(False, "config", str(exc))
        return 1
    line(True, "config", f"{len(consult.agents)} agents, host `{runtime}`")

    from .consult.service import ConsultService

    async def agents() -> list | None:
        # Opening the store is the database check: it creates the file and runs the
        # migrations, the first thing the server does with it.
        service = ConsultService(consult, runtime)
        try:
            await service.open()
        except Exception as exc:  # noqa: BLE001 -- any reason is a FAIL line
            line(False, "database", f"{consult.database_path}: {exc}")
            return None
        try:
            return (await service.list_agents(check=True)).agents
        finally:
            await service.close()

    listed = asyncio.run(agents())
    if listed is not None:
        line(True, "database", str(consult.database_path))
    for agent in listed or []:
        if agent.excluded_as_host:
            print(f"--   agent {agent.agent_id}: the host's own runtime, not checked")
        elif not agent.enabled:
            print(f"--   agent {agent.agent_id}: disabled")
        else:
            ready = bool(agent.installed and agent.authenticated)
            line(ready, f"agent {agent.agent_id}", "" if ready else agent.detail or "not ready")

    from .code import sandbox

    # Informational: only `isolated_write` needs it, and the server already refused
    # above if a configured agent needed it and could not have it.
    print(f"--   sandbox: {'holds' if sandbox.holds() else sandbox.unavailable_reason()}")
    return 1 if failed else 0
