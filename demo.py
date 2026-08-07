#!/usr/bin/env python3
"""A runnable tour of the consult path, with no API key, no login, and no network.

    uv run python demo.py

`consult` is driven by a fake `claude` executable this script writes into a temp
directory and puts on PATH -- the adapter builds its own argv, spawns a real
process, and parses what comes back, exactly as it would with the CLI installed.

What this proves is this server's logic. What it cannot prove is your CLIs;
`smoke_consult_live.py` and `smoke_review_live.py` are for that, and they spend
real money.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import textwrap
from pathlib import Path

from orchestrator_mcp.server import build_server

# Stands in for an installed, logged-in Claude Code. It answers in the shape the
# protocol requires and echoes back whichever session id it was handed, which is
# what makes the resume below a real resume.
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, sys

args = sys.argv[1:]
if "auth" in args:
    print(json.dumps({"loggedIn": True, "authMethod": "demo"}))
    raise SystemExit(0)

def flag(name, default=""):
    return args[args.index(name) + 1] if name in args else default

turn = "first" if "--resume" not in args else "second"
prompt = sys.stdin.read()
answers = {
    "first": "Use a queue per consumer group; one shared queue serializes them.",
    "second": "Yes -- the same reasoning holds at ten consumers, but watch the fan-out cost.",
}
print(json.dumps({
    "type": "result",
    "is_error": False,
    "result": json.dumps({
        "answer": answers[turn],
        "assumptions": ["the consumers are independent"],
        "uncertainties": ["throughput was not stated"],
        "follow_up_questions": ["how many consumers do you expect?"],
        "sources": [{"title": "supplied", "locator": "context", "source_type": "document"}],
    }),
    "session_id": flag("--session-id") or flag("--resume"),
    "modelUsage": {"claude-opus-4-1": {"inputTokens": 900 + len(prompt) // 4, "outputTokens": 120}},
    "usage": {"input_tokens": 900, "output_tokens": 120},
    "total_cost_usd": 0.0,
}))
'''


def consult_config(root: Path) -> dict:
    return {
        "consult": {
            "database_path": str(root / "consultations.sqlite3"),
            # Into the temp directory, so a real `agents.yaml` sitting in the working
            # directory is not merged in and this tour stays the same everywhere.
            "managed_agents_path": str(root / "agents.yaml"),
            "timeout_s": 60,
            "agents": {
                "claude-opus": {
                    "runtime": "claude",
                    "command": "claude",
                    "model": "opus",
                    # Consult has a fixed capability vocabulary -- coding, research,
                    # writing, reasoning, review -- and an agent scores itself
                    # against it.
                    "scores": {"coding": 90, "review": 70},
                },
                # Scores higher, and still never wins: ORCHESTRATOR_HOST_RUNTIME is
                # `codex` below, so this is the caller. Routing drops it before it
                # scores anything, which is what stops an agent consulting itself.
                "codex-gpt": {
                    "runtime": "codex",
                    "command": "codex",
                    "model": "gpt-5.1-codex",
                    "scores": {"coding": 99, "review": 99},
                },
            },
        }
    }


async def consult_path(root: Path) -> None:
    banner("consult -- a second agent, over its own account")

    bindir = root / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text(FAKE_CLAUDE)
    (bindir / "claude").chmod(0o755)
    os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
    # Read from the environment and never from a tool argument: an agent that could
    # name the host runtime could name someone else's and route the work back to
    # itself. Set `claude` here and the claude agent below becomes unroutable.
    os.environ["ORCHESTRATOR_HOST_RUNTIME"] = "codex"

    server = build_server(consult_config(root))

    result = await server.call_tool("orchestrator_list_consult_agents", {})
    show("who is configured, and whether they are reachable", result.structured_content)

    result = await server.call_tool(
        "orchestrator_consult",
        {
            "capability": "coding",
            "prompt": "Should each consumer get its own queue?",
            "context": "Three consumers read from one queue and one is slow.",
        },
    )
    first = result.structured_content
    show("the consultation", first)

    consultation_id = first["consultation_id"]
    print(f"  keep this: consultation_id = {consultation_id}\n")

    result = await server.call_tool(
        "orchestrator_consult",
        {
            "capability": "coding",
            "prompt": "Does that still hold at ten consumers?",
            "consultation_id": consultation_id,
        },
    )
    show("the same conversation, resumed", result.structured_content)

    result = await server.call_tool(
        "orchestrator_get_consultation", {"consultation_id": consultation_id}
    )
    stored = result.structured_content
    print(f"  stored: {len(stored['turns'])} turns, status {stored['status']}, bound to "
          f"`{stored['target_agent_id']}` ({stored['target_model']}) "
          f"on {stored['target_runtime']}, native session bound: "
          f"{stored['native_session_bound']}")
    decision = {k: v for k, v in stored["routing"][0].items() if k != "excluded_json"}
    show("why this agent, and who lost", decision)


# --- output -----------------------------------------------------------------


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}\n")


def show(label: str, payload: dict) -> None:
    print(f"— {label}")
    body = json.dumps(_trim(payload), indent=2, default=str)
    print(textwrap.indent(body, "  "), "\n")


def _trim(payload: dict) -> dict:
    """Drop the null and zero fields, so what matters is what you read."""
    keep = {}
    for key, value in payload.items():
        if value in (None, False, 0, [], {}) and key not in ("ok", "content", "data"):
            continue
        keep[key] = _trim(value) if isinstance(value, dict) else value
    return keep


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="orchestrator-demo-") as scratch:
        await consult_path(Path(scratch))

    banner("that was all of it, with no key and no login")
    print(textwrap.dedent("""\
        It ran for real: the routing, the envelope, the subprocess transport, the
        session binding and the SQLite store. The one thing this machine cannot
        supply was stubbed -- the consulted CLI.

        To point it at your own setup:
          * copy `config.example.yaml` to `config.yaml` and name the agents you have
          * install the other agent's CLI and log into it yourself
            (`codex login`, `claude auth login`), then set
            ORCHESTRATOR_HOST_RUNTIME to whichever agent is calling
          * `uv run python smoke_consult_live.py` and `smoke_review_live.py` do the
            same tour against the real thing, and cost real money
    """))


if __name__ == "__main__":
    asyncio.run(main())
