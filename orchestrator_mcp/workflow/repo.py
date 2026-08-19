"""Where a workflow may work, and what the tree looked like when it started.

Two jobs, both of them refusals:

  * a working directory has to resolve beneath a configured root, and `roots:` being
    empty means nowhere -- never "anywhere", and never inferred from this process's
    current directory, which is wherever the host happened to launch the server;
  * a workflow records the commit it started from, so every later step can say what
    it changed relative to something. A dirty tree at that moment makes the baseline
    a description of a state nobody has, which is why it needs acknowledging.

`git` runs through the same `run_process` the adapters use, so it inherits the
process-group kill and the output cap rather than growing a second way to spawn a
child.
"""

from __future__ import annotations

from pathlib import Path

from ..consult.adapters.base import run_process
from ..consult.errors import ConsultErrorCode
from .contract import WorkflowError

GIT_TIMEOUT_S = 30.0


def resolve_workdir(workdir: str, roots: list[Path]) -> Path:
    """The directory this workflow works in, proven to be under an allowed root."""
    if not roots:
        raise WorkflowError(
            ConsultErrorCode.INVALID_REQUEST,
            "no `consult.workflow.roots:` are configured, so there is nowhere a "
            "workflow is allowed to work; name the directories it may use",
        )
    if not workdir.strip():
        raise WorkflowError(ConsultErrorCode.INVALID_REQUEST, "a workflow needs a `workdir`")
    # `resolve` and not `absolute`: a path with `..` in it, or a symlink pointing
    # out of a root, is under that root only until it is followed.
    path = Path(workdir).expanduser().resolve()
    if not path.is_dir():
        raise WorkflowError(
            ConsultErrorCode.INVALID_REQUEST, f"`{path}` is not a directory that exists"
        )
    for root in roots:
        resolved = root.expanduser().resolve()
        if path == resolved or resolved in path.parents:
            return path
    allowed = ", ".join(f"`{r}`" for r in roots)
    raise WorkflowError(
        ConsultErrorCode.INVALID_REQUEST,
        f"`{path}` is not under any configured workflow root ({allowed})",
    )


async def _git(path: Path, *args: str) -> tuple[int, str]:
    result = await run_process(["git", *args], None, GIT_TIMEOUT_S, cwd=path)
    return result.returncode, result.stdout.strip()


async def baseline(path: Path, allow_dirty: bool) -> str:
    """The commit this workflow starts from.

    A repository with no commit yet is refused rather than given an empty baseline:
    every later step describes itself relative to this, and "relative to nothing" is
    a claim that reads as though it were checked.
    """
    code, top = await _git(path, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        raise WorkflowError(
            ConsultErrorCode.INVALID_REQUEST,
            f"`{path}` is not inside a git repository, so there is no baseline to "
            "record and no way to say what a step changed",
        )
    code, head = await _git(path, "rev-parse", "HEAD")
    if code != 0 or not head:
        raise WorkflowError(
            ConsultErrorCode.INVALID_REQUEST,
            f"the repository at `{top}` has no commits yet; make one so the workflow "
            "has a baseline to compare against",
        )
    code, dirty = await _git(path, "status", "--porcelain")
    if code == 0 and dirty and not allow_dirty:
        changed = len(dirty.splitlines())
        raise WorkflowError(
            ConsultErrorCode.INVALID_REQUEST,
            f"the tree at `{top}` has {changed} uncommitted change(s), so `{head[:12]}` "
            "does not describe what a step would actually be working from. Commit or "
            "stash them, or pass `allow_dirty=true` to say the difference is intended",
        )
    return head
