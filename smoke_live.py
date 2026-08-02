"""Live smoke test. Spends real money -- run it deliberately, not in CI.

    uv run python smoke_live.py [capability ...]

The unit suite stubs every provider, so it proves the orchestrator's logic and
nothing about the providers themselves. These are the checks that only a real
endpoint can answer: whether `response_format` survives the round trip, whether a
model actually honours the abstention sentinel, and what `finish_reason` a real
provider sends when it runs out of room.

Roughly four short calls per capability.
"""

from __future__ import annotations

import asyncio
import sys

from orchestrator_mcp.contract import ErrorCode
from orchestrator_mcp.server import Orchestrator, load_config

CITY = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
    "required": ["city", "country"],
    "additionalProperties": False,
}


async def checks(orchestrator: Orchestrator, capability: str):
    """Each yields (name, passed, detail). Detail is printed either way."""

    r = await orchestrator.ask(capability=capability, prompt="Reply with the single word: pong")
    yield (
        "prose round trip",
        r.ok and bool(r.content),
        f"{r.model_used} | {r.usage.total_tokens} tok | ${r.usage.cost_usd or 0:.5f} | {r.latency_ms}ms",
    )

    r = await orchestrator.ask(
        capability=capability,
        prompt="Istanbul is in which country? Answer as JSON.",
        response_schema=CITY,
    )
    yield (
        "structured mode",
        r.ok and isinstance(r.data, dict) and "country" in (r.data or {}),
        f"data={r.data} error={r.error and r.error.message}",
    )

    r = await orchestrator.ask(
        capability=capability,
        prompt="What is this company's 2027 revenue forecast?",
        context="Our office moved to the third floor in March. The coffee machine is new.",
    )
    yield (
        "abstains instead of inventing",
        r.ok and r.insufficient_context,
        f"insufficient_context={r.insufficient_context} content={(r.content or '')[:80]!r}",
    )

    r = await orchestrator.ask(
        capability=capability,
        prompt="Write a detailed 500-word history of the printing press.",
        max_output_tokens=16,
    )
    yield (
        "truncation is a failure",
        r.error is not None and r.error.code is ErrorCode.OUTPUT_TRUNCATED and r.content is None,
        f"ok={r.ok} code={r.error and r.error.code.value} finish={r.finish_reason}",
    )


async def main() -> int:
    orchestrator = Orchestrator(load_config())
    wanted = sys.argv[1:] or list(orchestrator.capabilities)
    if unknown := set(wanted) - set(orchestrator.capabilities):
        print(f"unknown capabilities: {sorted(unknown)}")
        return 2

    failures = 0
    for capability in wanted:
        print(f"\n=== {capability} ===")
        async for name, passed, detail in checks(orchestrator, capability):
            failures += not passed
            print(f"  [{'ok' if passed else 'FAIL'}] {name}\n        {detail}")

    print(f"\n{failures} failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
