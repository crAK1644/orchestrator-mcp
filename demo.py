#!/usr/bin/env python3
"""A runnable tour of both paths, with no API key, no login, and no network.

    uv run python demo.py

`ask` is driven by LiteLLM's own `mock_response`, so the router, the envelope, the
grounding directive and the structured-output path all run for real against a
deployment that answers from the config instead of from a provider. `consult` is
driven by a fake `claude` executable this script writes into a temp directory and
puts on PATH -- the adapter builds its own argv, spawns a real process, and parses
what comes back, exactly as it would with the CLI installed.

What this proves is this server's logic. What it cannot prove is your providers or
your CLIs; `smoke_live.py` and `smoke_consult_live.py` are for that, and they spend
real money.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import textwrap
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from orchestrator_mcp.server import build_server

logging.disable(logging.INFO)  # LiteLLM narrates every call otherwise.

# --- the ask path -----------------------------------------------------------

ASK_CONFIG = {
    "capabilities": {
        "fast": "Cheap, low-latency answers.",
        "coding": "Code, review, and debugging.",
    },
    "model_list": [
        {
            "model_name": "fast",
            "litellm_params": {
                "model": "openai/gpt-4o-mini",
                "api_key": "sk-not-a-real-key",
                # Answers from the config. No key is read and no request leaves.
                "mock_response": "Ankara.",
            },
        },
        {
            "model_name": "coding",
            "litellm_params": {
                "model": "openai/gpt-4o",
                "api_key": "sk-not-a-real-key",
                "mock_response": json.dumps(
                    {"insufficient_context": False, "answer": {"bug": "off-by-one", "line": 42}}
                ),
            },
        },
    ],
}

SCHEMA = {
    "type": "object",
    "properties": {"bug": {"type": "string"}, "line": {"type": "integer"}},
    "required": ["bug", "line"],
    "additionalProperties": False,
}


async def ask_path() -> None:
    server = build_server(ASK_CONFIG)
    banner("1. ask -- one envelope for every outcome")

    result = await server.call_tool("ask", {"capability": "fast", "prompt": "Capital of Turkey?"})
    show("a plain answer", result.structured_content)

    # `context` switches on the grounding directive: answer only from this material,
    # and say so when it does not support an answer. The stub replies the same either
    # way -- what runs for real here is the prompt assembly, with our directive last.
    result = await server.call_tool(
        "ask",
        {
            "capability": "fast",
            "prompt": "What is the population?",
            "context": "Ankara is the capital of Turkey.",
        },
    )
    show("grounded in supplied material", result.structured_content)

    result = await server.call_tool(
        "ask",
        {"capability": "coding", "prompt": "Find the bug.", "response_schema": SCHEMA},
    )
    show("structured: validated into `data`, `content` stays null", result.structured_content)

    # A capability nobody configured never reaches the tool body: the advertised schema
    # types `capability` as an enum, so the protocol layer refuses it before any
    # provider is chosen. That is a protocol error, not an envelope.
    try:
        await server.call_tool("ask", {"capability": "research", "prompt": "hi"})
    except ToolError as exc:
        print(f"— a capability that does not exist\n  refused by the schema: {exc}\n")

    result = await server.call_tool("list_capabilities", {})
    show("what the caller can see", result.structured_content)


# --- the consult path -------------------------------------------------------

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
    return ASK_CONFIG | {
        "consult": {
            "database_path": str(root / "consultations.sqlite3"),
            "timeout_s": 60,
            "agents": {
                "claude-opus": {
                    "runtime": "claude",
                    "command": "claude",
                    "model": "opus",
                    # Consult has its own fixed capability vocabulary -- coding,
                    # research, writing, reasoning, review -- separate from the
                    # `capabilities:` you name for `ask`.
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
    banner("2. consult -- a second agent, over its own account")

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

    result = await server.call_tool("list_consult_agents", {})
    show("who is configured, and whether they are reachable", result.structured_content)

    result = await server.call_tool(
        "consult",
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
        "consult",
        {
            "capability": "coding",
            "prompt": "Does that still hold at ten consumers?",
            "consultation_id": consultation_id,
        },
    )
    show("the same conversation, resumed", result.structured_content)

    result = await server.call_tool("get_consultation", {"consultation_id": consultation_id})
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
    await ask_path()
    with tempfile.TemporaryDirectory(prefix="orchestrator-demo-") as scratch:
        await consult_path(Path(scratch))

    banner("that was all of it, with no key and no login")
    print(textwrap.dedent("""\
        Both paths ran for real: the router, the envelope, the schema validation, the
        subprocess transport, the session binding and the SQLite store. Only the two
        things this machine cannot supply were stubbed -- the provider's reply, and
        the consulted CLI.

        To point it at your own setup:
          * copy `config.example.yaml` to `config.yaml`, drop the `mock_response`
            lines, and reference your key as `os.environ/YOUR_KEY_NAME`
          * for consult, install the other agent's CLI and log into it yourself
            (`codex login`, `claude auth login`), then set
            ORCHESTRATOR_HOST_RUNTIME to whichever agent is calling
          * `uv run python smoke_live.py` and `smoke_consult_live.py` do the same
            tour against the real thing, and cost real money
    """))


if __name__ == "__main__":
    asyncio.run(main())
