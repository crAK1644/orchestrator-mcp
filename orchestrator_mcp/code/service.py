"""Running a contained step: a throwaway worktree in, a diff out.

The worktree is the whole safety story on this side. The agent is handed a directory
checked out at the workflow's baseline, somewhere outside the user's repository, and
the CLI's sandbox is what keeps it there. Nothing this module does can be reached
from a consultation: it is called only by the workflow service, only for a step whose
resolved execution mode is `isolated_write`, and only after `code_adapter_for` has
agreed the runtime can be contained.

Capture is deliberately `git add -A` and then a staged diff, rather than
`git diff <baseline>..`. The plain form sees tracked modifications only, so a step
that creates a file -- which is most of them -- would be recorded as having done
nothing. `-A` picks up new files, deletions and mode changes, and `--binary` keeps
the ones git will not render as text.

Cleanup runs whatever happened, including a timeout or a crash mid-run, because a
worktree left behind is both a directory nobody will find and a registration inside
the user's repository. One exception: when *capture itself* fails the worktree stays,
because at that moment it is the only copy of the step's work and removing it would
destroy the work over a failure to read it.
"""

from __future__ import annotations

from pathlib import Path

from ..consult.adapters.base import AdapterError, run_process
from ..consult.config import AgentConfig, ConsultConfig
from ..consult.errors import ConsultErrorCode
from ..consult.prompts import CompiledPrompt
from .adapters.base import CodeResult
from .registry import CodeError, code_adapter_for

# Git is fast here, and every call is local. Long enough for a large checkout, short
# enough that a hung `git` does not hold a workflow's lease until it expires.
GIT_TIMEOUT_S = 60.0

# Outside the repository being worked on, so a stray `git add .` in the user's own
# shell cannot pick a worktree up, and so `git clean -fdx` in their tree does not
# delete a running step's directory.
WORKTREE_ROOT = Path("~/.orchestrator-mcp/worktrees").expanduser()


async def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    result = await run_process(["git", *args], None, GIT_TIMEOUT_S, cwd=cwd)
    return result.returncode, result.stdout, result.stderr.strip()


def worktree_path(workflow_id: str, step_id: str) -> Path:
    """Per step, never per workflow: a retry, a fix round and a crashed attempt that
    has not been cleaned up yet all exist at once, and one shared directory would
    have them overwrite each other."""
    return WORKTREE_ROOT / workflow_id / step_id


async def run_contained(
    config: ConsultConfig,
    agent: AgentConfig,
    prompt: CompiledPrompt,
    repo: Path,
    baseline_commit: str,
    workflow_id: str,
    step_id: str,
    timeout_s: float,
) -> CodeResult:
    """Check out the baseline, let the agent work in it, and read back what changed."""
    adapter = code_adapter_for(agent, config)
    path = worktree_path(workflow_id, step_id)
    gitdir = await _add_worktree(repo, path, baseline_commit)
    keep = False
    try:
        try:
            run = await adapter.execute(agent, prompt, path, timeout_s)
        except AdapterError as exc:
            # Rewrapped rather than propagated: the workflow service catches
            # `CodeError` from this side, and a transport failure from a contained
            # run is not something a caller should have to know two exception types
            # to handle.
            raise CodeError(exc.code, str(exc)) from exc
        try:
            patch, files, ignored = await _capture(gitdir, path, baseline_commit)
        except CodeError:
            # The worktree holds the only copy of what this step did. Deleting it
            # because *reading* it failed destroys the work over a problem the host
            # can usually look at and fix by hand -- and a nested `git init`, which
            # is all it takes, is something an ordinary scaffolding step does.
            keep = True
            raise
    finally:
        if not keep:
            await _remove_worktree(repo, path)

    return CodeResult(
        baseline_commit=baseline_commit,
        patch=patch,
        files=files,
        ignored=ignored,
        summary=run.summary,
        commands=run.commands,
        native_session_id=run.native_session_id,
        model_used=run.model_used,
        model_verified=run.model_verified,
        raw_output=run.raw_output,
        usage=run.usage,
    )


async def _add_worktree(repo: Path, path: Path, baseline_commit: str) -> Path:
    """Create the worktree and return the git directory it is administered from.

    The gitdir is read here, before the agent has run, and every later git call names
    it explicitly. Otherwise capture would rediscover it by reading `.git` inside a
    directory the agent spent the whole step writing to -- a one-line file pointing
    anywhere, and capture would faithfully diff whatever it pointed at.
    """
    if path.exists():
        raise CodeError(
            ConsultErrorCode.INVALID_REQUEST,
            f"`{path}` already exists; a previous attempt of this step was not cleaned "
            "up, and reusing its directory would mix two runs into one diff",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # `--detach`, so no branch is created in the user's repository and no branch name
    # can collide with one they are using.
    code, _, err = await _git(repo, "worktree", "add", "--detach", str(path), baseline_commit)
    if code != 0:
        raise CodeError(
            ConsultErrorCode.INVALID_REQUEST,
            f"could not create a worktree at `{path}` from `{baseline_commit[:12]}`: {err[:400]}",
        )
    code, gitdir, err = await _git(path, "rev-parse", "--absolute-git-dir")
    if code != 0 or not gitdir.strip():
        raise CodeError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"could not read the git directory of the worktree at `{path}`: {err[:400]}",
        )
    return Path(gitdir.strip())


async def _remove_worktree(repo: Path, path: Path) -> None:
    """Best effort, and quiet about it.

    This runs in a `finally`, so raising here would replace the real reason a step
    failed with a cleanup error. `--force` because the agent leaves the tree dirty by
    design, and `prune` because a directory that vanished some other way still leaves
    a registration in the repository.
    """
    await _git(repo, "worktree", "remove", "--force", str(path))
    await _git(repo, "worktree", "prune")
    # The per-workflow directory above it is ours, not git's, so nothing else removes
    # it and a finished workflow leaves an empty one behind for good. `rmdir` rather
    # than a recursive delete on purpose: it refuses a directory that still holds
    # another step's worktree, which is the only thing that makes this safe to call
    # while a sibling step is running.
    try:
        path.parent.rmdir()
    except OSError:
        pass


async def _capture(
    gitdir: Path, path: Path, baseline_commit: str
) -> tuple[str, list[str], list[str]]:
    """Everything the step changed, as one patch against the baseline.

    Staged first so untracked files are in the index and therefore in the diff. The
    agent cannot run `git add` itself -- a worktree's git directory is outside its
    sandbox -- so this index is only ever written here.

    `gitdir` was read before the agent started and is passed explicitly, so none of
    these commands discovers a repository by reading a file the agent could have
    rewritten. That also disables discovery outright: if the worktree's `.git` is gone
    or wrong, git fails here rather than quietly diffing something else.

    Two things a patch cannot carry, and neither is allowed to pass in silence. A
    nested repository makes `git add -A` refuse the entire tree, so it raises, and the
    caller keeps the worktree rather than deleting the work. Ignored files are skipped
    by design and can never appear in a diff, so they come back named.
    """

    async def git(*args: str) -> tuple[int, str, str]:
        return await _git(path, f"--git-dir={gitdir}", f"--work-tree={path}", *args)

    code, _, err = await git("add", "-A")
    if code != 0:
        raise CodeError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"could not stage the worktree's changes for capture: {err[:400]}. The "
            f"worktree has been left at `{path}` because it holds the only copy of "
            "this step's work. A repository created inside it -- a `git init`, a "
            "scaffolded subproject, a vendored fixture -- is the usual cause, and one "
            "is enough for git to refuse the whole tree",
        )
    code, patch, err = await git("diff", "--binary", "--cached", baseline_commit)
    if code != 0:
        raise CodeError(
            ConsultErrorCode.TRANSPORT_ERROR, f"could not read back the step's changes: {err[:400]}"
        )
    code, names, _ = await git("diff", "--name-only", "--cached", baseline_commit)
    files = [line.strip() for line in names.splitlines() if line.strip()] if code == 0 else []
    code, others, _ = await git("ls-files", "--others", "--ignored", "--exclude-standard")
    ignored = [line.strip() for line in others.splitlines() if line.strip()] if code == 0 else []
    return patch, files, ignored[:200]
