"""Live workflow smoke test. Spends real money -- run it deliberately, not in CI.

    ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_workflow_live.py [--keep]

The offline suite drives the whole state machine against stub adapters, so it proves
the transitions and nothing about what a real model does when asked for an
`ImplementationPlan` or a unified diff. This is what answers the questions only
installed, logged-in CLIs can:

  * does a real model return a plan that validates as `ImplementationPlan`, or is
    schema-shaped prose the common case
  * does `execution: patch` produce a diff that `git apply` actually accepts, from
    an agent that never saw the tree
  * does the returned raw patch match the `raw_patch_sha256` stored beside the
    scrubbed audit copy
  * does a real test exit code drive the loop -- `reviewing` on green, `fixing` on
    red -- and does `loop_done` stay false while a serious finding is open

It works in a scratch git repository it creates and deletes, never in this one, and
it never writes to your `config.yaml`: the workflow block, the host model and the
scores the steps route on are injected into the loaded config in memory.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from orchestrator_mcp.consult.config import host_runtime, load_consult_config
from orchestrator_mcp.contract import scrub_json
from orchestrator_mcp.server import load_config
from orchestrator_mcp.workflow.service import WorkflowService

ROOT = Path.home() / ".orchestrator-mcp" / "smoke-workflows"

# Small, wrong in a way with an obvious severity, and paired with a test that keeps
# passing whatever the fix looks like -- the exit code has to come from the tree, so
# it must not be a test only one particular patch satisfies.
APP = '''\
def load_report(path):
    """Read a report file and return its text."""
    with open(path) as handle:
        return handle.read()
'''

# The file it reads lives *inside* the reports directory on purpose. A test reading
# from `tmp_path` would make the goal self-contradictory -- containment against a
# fixed root cannot hold while a test reads from a random temp directory -- and the
# first live run proved a model will notice and refuse rather than guess.
TEST = '''\
from pathlib import Path

from app import load_report

HERE = Path(__file__).parent


def test_reads_a_file_in_the_reports_directory():
    target = HERE / "r.txt"
    target.write_text("hello")
    assert load_report(str(target)) == "hello"
'''

GOAL = (
    "`load_report` in app.py takes a caller-supplied path and reads it with no bound "
    "and no check that it stays inside the reports directory. Add a module-level "
    "REPORTS_ROOT = Path(__file__).parent.resolve(), resolve the caller's path against "
    "it, raise ValueError for anything that is not REPORTS_ROOT or beneath it, and read "
    "at most 64 KiB. Keep the signature `load_report(path)`. test_app.py reads a file "
    "inside that directory and must keep passing."
)

# Everything not named here falls to the host, which is the conservative default.
# `plan` goes to codex for the same reason path B does: the free flash model truncates
# a long structured answer often enough to fail the step outright. What this path is
# here to prove is `implement` and `fix` -- a delegated diff from a model that never
# saw the tree -- so those are the ones that stay on it.
PATCH_PATH = {
    "plan": {"agent": "codex-sol"},
    "implement": {"agent": "deepseek-flash", "execution": "patch"},
    "fix": {"agent": "deepseek-flash", "execution": "patch"},
    "review": {"agents": ["codex-sol"]},
}
# A different model on the same step, which is the whole claim about bindings. It is
# also the reliable one: `deepseek-v4-flash-free` returns schema-shaped prose often
# enough that the first live runs failed here, and a `plan` that will not parse is
# recorded as a failed step rather than stored as an artifact.
HOST_PATH = {
    "plan": {"agent": "codex-sol"},
    "review": {"agents": ["codex-sol"]},
}

# What the host would have written itself, in the second path. Applied verbatim, so
# the test run is a real exit code over real edited files.
HOST_EDIT = '''\
from pathlib import Path

REPORTS_ROOT = Path(__file__).parent.resolve()
MAX_BYTES = 64 * 1024


def load_report(path):
    """Read a report file inside REPORTS_ROOT and return its text."""
    target = Path(path).resolve()
    if target != REPORTS_ROOT and REPORTS_ROOT not in target.parents:
        raise ValueError(f"{target} is outside the reports directory")
    with open(target) as handle:
        return handle.read(MAX_BYTES)
'''


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def scratch_repo(name: str) -> Path:
    """A repository with one commit, under a root the workflow is allowed to use."""
    repo = ROOT / f"{name}-{uuid4().hex[:8]}"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text(APP)
    (repo / "test_app.py").write_text(TEST)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "smoke@example.invalid")
    git(repo, "config", "user.name", "workflow smoke")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    return repo


def ends_with_newline(patch: str) -> str:
    """`git apply` refuses a diff whose last line has no newline -- "corrupt patch".

    Models leave it off routinely. Normalizing belongs here, on the host side, and not
    in the server: `raw_patch_sha256` covers the bytes the model actually returned, and
    a server that quietly edited them before hashing would be attesting to text nobody
    produced.
    """
    return patch if patch.endswith("\n") or not patch else patch + "\n"


def apply_diff(repo: Path, raw: str) -> tuple[bool, str]:
    """Apply a delegated diff the way a host actually would, and say how it went.

    Plain `git apply` first. Small models miscount hunk headers often enough that
    `--recount` is the ordinary remedy, and `-3` resolves context that drifted; both
    are host-side leniency about *how* the diff is read, and neither edits the bytes
    the hash attests to. The note records which one it took, because "applied after a
    recount" is a different fact from "applied as written".
    """
    text = ends_with_newline(raw)
    attempts = (("as written", ["git", "apply", "-"]),
                ("after --recount -3", ["git", "apply", "--recount", "-3", "-"]))
    error = ""
    for note, argv in attempts:
        done = subprocess.run(argv, cwd=repo, input=text, capture_output=True, text=True)
        if done.returncode == 0:
            return True, note
        error = done.stderr.strip()[:160]
    (repo / "rejected.patch").write_text(text)
    return False, error


def material(repo: Path) -> str:
    """The source a delegated step is shown, because it cannot look for itself.

    This is the host's job in every mode where the agent has no filesystem: a patch
    written against files nobody sent is a guess, and the step says so rather than
    guessing. Two small files here; a real host would send what the plan named.
    """
    return "\n\n".join(
        f"--- {name} ---\n{(repo / name).read_text()}" for name in ("app.py", "test_app.py")
    )


def run_tests(repo: Path) -> dict:
    """A test report the host observed, which is the only kind that counts."""
    started = time.perf_counter()
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=repo, capture_output=True, text=True
    )
    return {
        "command": f"{Path(sys.executable).name} -m pytest -q",
        "workdir": str(repo),
        "exit_code": done.returncode,
        "status": "passed" if done.returncode == 0 else "failed",
        "stdout_tail": done.stdout[-800:],
        "stderr_tail": done.stderr[-400:],
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "commit": git(repo, "rev-parse", "HEAD"),
    }


def live_config(root: Path) -> dict:
    """The loaded config with the workflow switched on, in memory only."""
    doc = load_config()
    consult = doc["consult"]
    agents = consult["agents"]
    # The steps route on capabilities the example config carries and a working
    # config.yaml written before the workflow existed does not.
    agents["deepseek-flash"].setdefault("scores", {}).update(
        {"planning": 60, "prompt_authoring": 55, "synthesis": 50}
    )
    agents["deepseek-flash"]["execution_modes"] = ["consultation", "patch"]
    agents["codex-sol"].setdefault("scores", {}).update(
        {"planning": 85, "prompt_authoring": 80, "synthesis": 85}
    )
    agents["codex-sol"]["execution_modes"] = ["consultation", "patch"]
    consult["host"] = {"model": "claude-opus-5"}
    consult["workflow"] = {
        "max_fix_rounds": 2,
        "roots": [str(root)],
        "advance_on_failed_test": False,
        "review_policy": {"different_from_implementer": True},
    }
    return doc


class Runner:
    """One workflow, one line of output per step."""

    def __init__(self, service: WorkflowService, workflow_id: str, repo: Path) -> None:
        self.service = service
        self.id = workflow_id
        self.repo = repo
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.failures += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
        return ok

    async def plan(self, step: str, context: str = ""):
        response = await self.service.plan_step(self.id, step, context)
        if response.error is not None:
            self.check(False, f"plan `{step}`", f"{response.error.code.value}: {response.error.message}")
            return None
        preview = response.preview
        who = ", ".join(a.agent_id for a in preview.agents) or "host"
        print(f"  plan `{step}`  {who} ({preview.executor}/{preview.execution_mode or '-'}, "
              f"{preview.repository_access}) {preview.prompt_chars} chars")
        return preview

    async def run(self, preview):
        response = await self.service.run_step(self.id, preview.step_id, preview.confirm_token)
        if response.error is not None:
            self.check(False, f"run `{preview.step}`",
                       f"{response.error.code.value}: {response.error.message}")
            return None
        print(f"  run  `{preview.step}`  status={response.status}")
        return response

    async def record(self, preview, result: dict):
        response = await self.service.record_host_step(
            self.id, preview.step_id, preview.confirm_token, result
        )
        if response.error is not None:
            self.check(False, f"record `{preview.step}`",
                       f"{response.error.code.value}: {response.error.message}")
            return None
        print(f"  host `{preview.step}`  status={response.status}")
        return response


async def synthesize(runner: Runner, review_id: str) -> None:
    """Carry every reviewer finding into the summary, dispositions untouched.

    Nothing here decides a finding is fixed. An open serious finding is what makes
    `loop_done` false, and inventing a disposition to get a green run would be the
    smoke test proving itself.
    """
    review = await runner.service.reviews.get(review_id)
    findings = [f for r in review.results if r.ok for f in r.findings]
    print(f"  review {review_id} -> {len(findings)} finding(s) from "
          f"{sum(1 for r in review.results if r.ok)} reviewer(s)")

    preview = await runner.plan("synthesize")
    if preview is None:
        return
    await runner.record(preview, {
        "summary": "Reviewer findings on the path handling in app.py.",
        "recommendation": "Address the findings below before shipping.",
        "combined_findings": [
            {
                "problem": f.why[:200] or "reported without a reason",
                "severity": f.severity,
                "location": f.location,
                "agreed_by": [f.agent_id],
                "source_finding_ids": [f.finding_id],
                "proposed_action": f.fix[:200],
            }
            for f in findings
        ],
        "checked": ["app.py", "test_app.py"],
        "not_checked": ["anything calling load_report"],
    })


async def fix_round(runner: Runner, repo: Path, delegated: bool) -> str:
    """One round of the capped loop, which is the part offline stubs cannot prove.

    The point is the transition, not a green tree: `fixing` takes a fix step, the
    patch path goes back through `awaiting_host_apply`, and the re-test comes from a
    real exit code either way. Nothing here decides a finding is fixed.
    """
    print("  --- fix round ---")
    preview = await runner.plan("fix", material(repo))
    if preview is None:
        return ""
    if delegated:
        response = await runner.run(preview)
        if response is None:
            return ""
        raw = response.patch
        ok, note = apply_diff(repo, raw)
        ok = runner.check(ok, "`git apply` accepted the fix diff", note)
        preview = await runner.plan("apply_patch")
        if preview is None:
            return ""
        if ok:
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "apply fix patch")
        await runner.record(preview, {
            "summary": "applied the fix patch" if ok else "the fix diff did not apply",
            "files": [line.split()[-1] for line in raw.splitlines() if line.startswith("+++ ")],
            "commit": git(repo, "rev-parse", "HEAD"),
        })
    else:
        # The host writes its own fix. Recording the round with no edit is the honest
        # entry when there was no edit -- a summary claiming otherwise would be the
        # smoke test lying to its own database.
        await runner.record(preview, {
            "summary": "recorded the fix round; the host made no further code change",
            "files": [],
            "commit": git(repo, "rev-parse", "HEAD"),
        })

    preview = await runner.plan("test")
    if preview is None:
        return ""
    report = run_tests(repo)
    response = await runner.record(preview, report)
    if response is not None:
        runner.check(
            response.status in ("reviewing", "fixing"),
            f"the round's re-test moved the workflow to `{response.status}`",
            f"exit={report['exit_code']}",
        )
    status = await runner.service.status(runner.id)
    print(f"  after one round: status={status.status}  fix_rounds={status.workflow.fix_rounds}")
    return status.status


async def one_path(service: WorkflowService, name: str, bindings: dict, delegated: bool) -> int:
    repo = scratch_repo(name)
    print(f"\n=== {name}: {repo} ===")
    started = await service.start(GOAL, str(repo), web=False, bindings=bindings)
    if started.error is not None:
        print(f"  [FAIL] start: {started.error.code.value}: {started.error.message}")
        shutil.rmtree(repo, ignore_errors=True)
        return 1

    runner = Runner(service, started.workflow_id, repo)
    print(f"  workflow {started.workflow_id}  baseline "
          f"{started.workflow.baseline_commit[:12]}  next={started.workflow.next_steps}")

    # --- plan (delegated either way: this is the step a real model has to shape) ---
    preview = await runner.plan("plan", material(repo))
    if preview is None or await runner.run(preview) is None:
        return runner.failures or 1
    plan_step = (await service.status(runner.id)).workflow.steps[-1]
    runner.check(
        isinstance((plan_step.output or {}).get("changes"), list),
        "the plan validated as an ImplementationPlan",
        str((plan_step.output or {}).get("summary", ""))[:90],
    )

    # --- execution brief, host-authored ---
    preview = await runner.plan("author_execution_prompt")
    if preview is None:
        return runner.failures
    await runner.record(preview, {
        "objective": GOAL,
        "constraints": ["keep the signature", "no new dependencies"],
        "steps": ["resolve against REPORTS_ROOT", "refuse anything outside it", "cap the read"],
        "done_when": ["test_app.py passes", "a path outside the root raises"],
    })

    # --- implementation ---
    if delegated:
        preview = await runner.plan("implement", material(repo))
        if preview is None:
            return runner.failures
        response = await runner.run(preview)
        if response is None:
            return runner.failures
        raw = response.patch
        stored = response.step.output or {}
        runner.check(bool(raw), "the raw patch came back once, in the run response",
                     f"{len(raw)} chars")
        runner.check(
            sha256(raw.encode()).hexdigest() == stored.get("raw_patch_sha256"),
            "the stored hash matches the raw patch",
        )
        runner.check(
            stored.get("patch") == scrub_json(raw),
            "the stored copy is what the scrubber made of the raw patch",
            "(equal to the raw text here: nothing credential-shaped in it)",
        )

        # --- host applies it; only the party holding the tree can ---
        ok, note = apply_diff(repo, raw)
        ok = runner.check(ok, "`git apply` accepted the diff", note)
        preview = await runner.plan("apply_patch")
        if preview is None:
            return runner.failures
        if ok:
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "apply delegated patch")
        await runner.record(preview, {
            "summary": "applied the delegated patch" if ok else "the diff did not apply",
            "files": [line.split()[-1] for line in raw.splitlines() if line.startswith("+++ ")],
            "commit": git(repo, "rev-parse", "HEAD"),
        })
    else:
        preview = await runner.plan("implement")
        if preview is None:
            return runner.failures
        (repo / "app.py").write_text(HOST_EDIT)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "host implementation")
        await runner.record(preview, {
            "summary": "rewrote load_report to resolve against REPORTS_ROOT and cap the read",
            "files": ["app.py"],
            "commit": git(repo, "rev-parse", "HEAD"),
        })

    # --- tests, observed ---
    preview = await runner.plan("test")
    if preview is None:
        return runner.failures
    report = run_tests(repo)
    response = await runner.record(preview, {**report, "reported_by": "orchestrator"})
    if response is None:
        return runner.failures
    recorded = response.step.output or {}
    runner.check(recorded.get("reported_by") == "host",
                 "a host-written report cannot claim to be orchestrator-observed",
                 f"exit={report['exit_code']}")
    runner.check(
        response.status == ("reviewing" if report["status"] == "passed" else "fixing"),
        f"a {report['status']} test run moved the workflow to `{response.status}`",
    )

    if response.status == "fixing":
        # A delegated patch that breaks the tests is an ordinary outcome, and `fixing`
        # is exactly where the loop belongs. Take the round the state asks for and then
        # carry on: what this path is here to prove is the whole sequence through
        # review, not that a free model writes a working patch first try.
        print("  tests failed, so the loop went to `fixing` before review")
        if await fix_round(runner, repo, delegated) != "reviewing":
            print("  still not green after a round; not proceeding to review")
            return runner.failures

    # --- review, through the review layer ---
    preview = await runner.plan("review", material(repo))
    if preview is None:
        return runner.failures
    runner.check(preview.review_id is not None,
                 "the review step's token is the review plan's own token")
    if await runner.run(preview) is None:
        return runner.failures
    await synthesize(runner, preview.review_id)

    final = await service.status(runner.id)
    synth = next((s.output for s in reversed(final.workflow.steps) if s.step == "synthesize"), {})
    print(f"  status={final.status}  fix_rounds={final.workflow.fix_rounds}  "
          f"loop_done={(synth or {}).get('loop_done')}  "
          f"open_serious={(synth or {}).get('open_serious')}")
    for reason in (synth or {}).get("reasons", []):
        print(f"    not done: {reason}")
    runner.check(
        bool((synth or {}).get("loop_done")) == (final.status == "completed"),
        "the computed `loop_done` agrees with where the workflow landed",
    )
    if final.status == "fixing":
        await fix_round(runner, repo, delegated)
    return runner.failures


async def main() -> int:
    keep = "--keep" in sys.argv
    ROOT.mkdir(parents=True, exist_ok=True)
    config = load_consult_config(live_config(ROOT))
    service = await WorkflowService(config, host_runtime()).open()
    failures = 0
    try:
        failures += await one_path(service, "delegated-patch", PATCH_PATH, delegated=True)
        failures += await one_path(service, "host-implementation", HOST_PATH, delegated=False)
    finally:
        await service.close()
        if not keep:
            shutil.rmtree(ROOT, ignore_errors=True)
            print(f"\nremoved {ROOT}; pass --keep to look at the repositories")

    print(f"\n{failures} check(s) failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
