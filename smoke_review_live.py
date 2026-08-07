"""Live review smoke test. Spends real money -- run it deliberately, not in CI.

    ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_review_live.py [agent_id ...]

The offline suite runs the review service against stub adapters, so it proves the
composition and nothing about what a real reviewer does with the instructions.
This is what answers the questions only installed, logged-in CLIs can:

  * does a real model actually end its answer with the fenced findings block, or
    does `findings_parsed=False` turn out to be the common case rather than the
    rare one -- the measurement the plan said would justify per-call schemas
  * do two reviewers, neither having seen the other, produce findings a synthesis
    can line up
  * does the handshake hold end to end: the plan sends nothing, the token is spent
    once, and the review sits at `awaiting_synthesis` until a summary arrives

It writes to the configured database, in the same tables the dashboard reads, and
deletes the review it created on the way out unless `--keep` is passed.
"""

from __future__ import annotations

import asyncio
import sys

from orchestrator_mcp.consult.config import host_runtime, load_consult_config
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.review.service import ReviewService
from orchestrator_mcp.server import load_config

# Small enough to read in the output, and wrong in a way with an obvious severity:
# the read is unbounded and the caller is handed a path it did not check.
MATERIAL = '''\
def load_report(path):
    with open(path) as handle:
        return handle.read()

def handle(request):
    return {"body": load_report(request.args["path"])}
'''


def show_plan(response) -> None:
    plan = response.plan
    print(f"  review_id      {response.review_id}")
    print(f"  status         {response.status}")
    print(f"  reviewers      {[f'{s.agent_id} ({s.runtime}/{s.model})' for s in plan.reviewers]}")
    print(f"  requests       {plan.expected_requests}")
    print(f"  goal/context   {plan.goal_chars} / {plan.context_chars} chars")
    print(f"  material       {[f'{m.label} {m.locator}'.strip() for m in plan.material]}")
    print(f"  web requested  {plan.web_requested}")
    print(f"  secret hits    {[(h.field, h.line) for h in plan.secret_hits]}")
    if plan.duplicate_models:
        print(f"  ! two reviewers share a model: {plan.duplicate_models}")


def show_results(response) -> None:
    for result in response.results:
        head = f"  [{'ok' if result.ok else 'FAIL'}] {result.agent_id}"
        if not result.ok:
            print(f"{head}  {result.error and result.error.code.value}: "
                  f"{result.error and result.error.message}")
            continue
        print(f"{head}  parsed={result.findings_parsed} findings={len(result.findings)} "
              f"truncated={result.findings_truncated}")
        for finding in result.findings:
            print(f"         {finding.severity:<9} {finding.location or '-'}  {finding.why[:70]}")
        if not result.findings_parsed:
            print(f"         (prose only) {(result.answer or '')[:160]!r}")


async def main() -> int:
    config = load_consult_config(load_config())
    if config is None or config.review is None:
        print("no `consult.review:` block in the config")
        return 2

    keep = "--keep" in sys.argv
    wanted = [a for a in sys.argv[1:] if a != "--keep"] or None
    service = await ReviewService(config, host_runtime()).open()
    failures = 0

    try:
        print("=== plan (nothing is sent) ===")
        planned = await service.plan(
            mode="deep",
            goal="Review this handler for security and correctness problems.",
            material=[{"label": "reports.py", "kind": "file", "locator": "lines 1-6",
                       "chars": len(MATERIAL)}],
            context=MATERIAL,
            reviewers=wanted,
        )
        if planned.error:
            print(f"  [FAIL] {planned.error.code.value}: {planned.error.message}")
            return 1
        show_plan(planned)
        token = planned.plan.confirm_token

        print("\n=== run ===")
        ran = await service.run(
            planned.review_id, token,
            host_findings=["the path from the query string reaches `open` unchecked"],
        )
        if ran.error:
            print(f"  [FAIL] {ran.error.code.value}: {ran.error.message}")
            return 1
        show_results(ran)
        print(f"  status={ran.status} outcome={ran.outcome} "
              f"tokens={ran.usage and ran.usage.total_tokens}")

        # The two properties the offline suite asserts, re-checked against the real
        # thing: a spent token cannot be spent again, and reviewers replying is not
        # a finished review.
        # `host_findings` again, or a deep review refuses for the missing opinion and
        # the check passes without the token ever being looked at.
        replayed = await service.run(
            planned.review_id, token,
            host_findings=["the path from the query string reaches `open` unchecked"],
        )
        ok = replayed.error is not None and replayed.error.code == ConsultErrorCode.INVALID_REQUEST
        failures += not ok
        print(f"\n  [{'ok' if ok else 'FAIL'}] the token cannot be spent twice"
              f"\n        {replayed.error and replayed.error.message}")

        ok = ran.status == "awaiting_synthesis"
        failures += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] the review waits for a synthesis "
              f"(status={ran.status})")

        parsed = sum(1 for r in ran.results if r.findings_parsed)
        ok = all(r.ok and r.findings_parsed for r in ran.results)
        failures += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] {parsed}/{len(ran.results)} reviewers emitted "
              "a readable findings block")

        print("\n=== finalize ===")
        criticals = [
            f
            for r in ran.results
            for f in r.findings
            if r.ok and f.severity == "critical"
        ]
        done = await service.finalize(
            planned.review_id,
            {
                "summary": "The handler reads an unvalidated path and returns the file.",
                "recommendation": "Resolve the path against a fixed root and refuse anything outside it.",
                "combined_findings": [
                    {
                        "problem": f.why[:200] or "reported without a reason",
                        "severity": f.severity,
                        "location": f.location,
                        "agreed_by": [f.agent_id],
                        "source_finding_ids": [f.finding_id],
                        "proposed_action": f.fix[:200],
                    }
                    for f in criticals
                ],
                "checked": ["reports.py lines 1-6"],
                "not_checked": ["everything the handler is called from"],
            },
        )
        if done.error:
            failures += 1
            print(f"  [FAIL] {done.error.code.value}: {done.error.message}")
        else:
            print(f"  [ok] status={done.status} "
                  f"combined={len(done.summary.combined_findings)} "
                  f"citations={len(done.summary.citations)}")

        print("\n=== apply_fixes ===")
        selected = [f.finding_id for f in criticals[:1]]
        fixes = await service.fix_plan(planned.review_id, selected)
        if fixes.error:
            failures += 1
            print(f"  [FAIL] {fixes.error.code.value}: {fixes.error.message}")
        else:
            print(f"  [ok] {len(fixes.fix_plan.findings)} finding(s) to fix, "
                  f"{len(fixes.fix_plan.criticals_omitted)} Critical(s) left out, "
                  f"{len(fixes.fix_plan.steps)} steps -- nothing was edited")
            logged = await service.record_fix_round(
                planned.review_id, selected, "skipped", notes="smoke test; no edits made"
            )
            ok = logged.error is None and len(logged.fix_rounds) == 1
            failures += not ok
            print(f"  [{'ok' if ok else 'FAIL'}] the round was recorded "
                  f"({logged.error.message if logged.error else logged.fix_rounds[0].outcome})")

        if not keep:
            print(f"\ndeleted {await service.delete(planned.review_id)} review(s); "
                  "pass --keep to look at it in the dashboard")
    finally:
        await service.close()

    print(f"\n{failures} failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
