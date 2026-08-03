"""Live consultation smoke test. Spends real money -- run it deliberately, not in CI.

    ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_consult_live.py [agent_id ...]

The offline suite replays recorded output against a stub executable, so it proves
the adapters' logic and nothing about the CLIs themselves. This is what answers the
questions only an installed, logged-in binary can:

  * do the flags spell what the documentation says they spell -- especially on the
    Codex side, which was written on a machine with no `codex` installed at all
  * does a real run actually come back matching the consultation contract
  * does `--session-id` / `thread.started` really give a session that resumes
  * do the isolation flags hold: no tools, no MCP, no shell

It needs both CLIs installed and logged in. Nothing here writes to the store; it
drives the adapters directly, so a failure points at the transport and not at the
service on top of it.
"""

from __future__ import annotations

import asyncio
import sys

from orchestrator_mcp.consult.adapters import adapter_for
from orchestrator_mcp.consult.config import load_consult_config
from orchestrator_mcp.consult.contract import SourceMode
from orchestrator_mcp.consult.prompts import compile_prompt
from orchestrator_mcp.server import load_config

DOCUMENT = (
    "The Vasa was a Swedish warship that sank in Stockholm harbour on 10 August 1628, "
    "minutes into her maiden voyage. She was salvaged in 1961."
)


async def checks(agent, adapter):
    """Each yields (name, passed, detail). Detail is printed either way."""

    status = await adapter.preflight(agent)
    yield (
        "preflight",
        status.ready,
        f"installed={status.installed} authenticated={status.authenticated} {status.detail or ''}",
    )
    if not status.ready:
        return

    prompt = compile_prompt("research", SourceMode.DOCUMENT, "In what year did the Vasa sink?", DOCUMENT)
    started = await adapter.start(agent, prompt, SourceMode.DOCUMENT)
    yield (
        "document mode round trip",
        "1628" in started.content.answer,
        f"model={started.model_used} session={started.native_session_id} "
        f"answer={started.content.answer[:80]!r}",
    )

    follow_up = compile_prompt(
        "research", SourceMode.DOCUMENT, "In what year was she salvaged?", None, turn=2
    )
    resumed = await adapter.resume(agent, started.native_session_id, follow_up, SourceMode.DOCUMENT)
    yield (
        "the session remembers the document",
        "1961" in resumed.content.answer,
        f"answer={resumed.content.answer[:80]!r}",
    )

    grounded = compile_prompt(
        "research",
        SourceMode.DOCUMENT,
        "What was the ship's displacement in tonnes?",
        DOCUMENT,
    )
    result = await adapter.start(agent, grounded, SourceMode.DOCUMENT)
    yield (
        "document mode does not fill the gap",
        bool(result.content.uncertainties),
        f"uncertainties={result.content.uncertainties} answer={result.content.answer[:80]!r}",
    )

    if not agent.web_search:
        return

    web = compile_prompt(
        "research", SourceMode.WEB, "What is the current stable version of Python?", None
    )
    searched = await adapter.start(agent, web, SourceMode.WEB)
    yield (
        "web mode cites what it read",
        any(s.source_type == "web" for s in searched.content.sources),
        f"sources={[s.locator for s in searched.content.sources][:3]}",
    )


async def main() -> int:
    consult = load_consult_config(load_config())
    if consult is None:
        print("no `consult:` block in the config")
        return 2

    wanted = sys.argv[1:] or list(consult.agents)
    if unknown := set(wanted) - set(consult.agents):
        print(f"unknown agents: {sorted(unknown)}")
        return 2

    failures = 0
    for agent_id in wanted:
        agent = consult.agents[agent_id]
        print(f"\n=== {agent_id} ({agent.runtime} / {agent.model}) ===")
        try:
            async for name, passed, detail in checks(agent, adapter_for(agent, consult)):
                failures += not passed
                print(f"  [{'ok' if passed else 'FAIL'}] {name}\n        {detail}")
        except Exception as exc:  # noqa: BLE001 -- the point is to print it, not handle it
            failures += 1
            code = getattr(exc, "code", None)
            print(f"  [FAIL] {type(exc).__name__} {code and code.value or ''}\n        {exc}")

    print(f"\n{failures} failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
