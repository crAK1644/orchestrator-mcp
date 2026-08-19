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

import asyncio
import os
import stat
import time
from contextlib import suppress
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

# Recovery is a bounded safety net, not archival storage for raw patches or abandoned
# checkouts. Long enough for a human to recover a lost response across several days.
RECOVERY_RETENTION_S = 7 * 24 * 60 * 60

# Each stale worktree costs several git subprocesses. A startup sweep is a hygiene
# pass, not an unbounded maintenance job, so leave any excess for a later process.
MAX_RECOVERY_WORKTREES_PER_SWEEP = 8

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


def patch_path(workflow_id: str, step_id: str) -> Path:
    """Private recovery copy for a patch whose MCP response may be lost."""
    return WORKTREE_ROOT / workflow_id / f"{step_id}.patch"


async def save_patch(workflow_id: str, step_id: str, patch: str) -> Path:
    """Persist a raw patch before its worktree or only response can disappear."""
    target = patch_path(workflow_id, step_id)

    def write() -> None:
        try:
            _private(target.parent)
        except CodeError:
            raise
        except Exception as exc:
            raise CodeError(
                ConsultErrorCode.TRANSPORT_ERROR,
                f"could not preserve the raw patch at `{target}`: {type(exc).__name__}",
            ) from exc

        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None
            with handle:
                handle.write(patch)
                handle.flush()
                os.fsync(handle.fileno())
        except CodeError:
            if created:
                with suppress(OSError):
                    target.unlink()
            raise
        except Exception as exc:
            if created:
                with suppress(OSError):
                    target.unlink()
            raise CodeError(
                ConsultErrorCode.TRANSPORT_ERROR,
                f"could not preserve the raw patch at `{target}`: {type(exc).__name__}",
            ) from exc
        except BaseException:
            if created:
                with suppress(OSError):
                    target.unlink()
            raise
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    await asyncio.to_thread(write)
    return target


async def read_patch(workflow_id: str, step_id: str) -> str | None:
    target = patch_path(workflow_id, step_id)

    def read() -> str | None:
        descriptor: int | None = None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        # O_NONBLOCK prevents a replaced FIFO from stalling before fstat; O_NOFOLLOW
        # makes the opened descriptor, rather than an earlier pathname check, the
        # object whose ownership and permissions we validate.
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(target, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CodeError(
                ConsultErrorCode.INVALID_REQUEST,
                f"raw patch recovery file `{target}` cannot be opened safely: "
                f"{type(exc).__name__}",
            ) from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise CodeError(
                    ConsultErrorCode.INVALID_REQUEST,
                    f"raw patch recovery file `{target}` is not a private regular file",
                )
            handle = os.fdopen(descriptor, "r", encoding="utf-8")
            descriptor = None
            with handle:
                return handle.read()
        except CodeError:
            raise
        except (OSError, UnicodeError) as exc:
            raise CodeError(
                ConsultErrorCode.INVALID_REQUEST,
                f"raw patch recovery file `{target}` cannot be read safely: "
                f"{type(exc).__name__}",
            ) from exc
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    return await asyncio.to_thread(read)


async def remove_patch(workflow_id: str, step_id: str) -> None:
    target = patch_path(workflow_id, step_id)

    def remove() -> None:
        with suppress(FileNotFoundError):
            target.unlink()
        with suppress(OSError):
            target.parent.rmdir()

    await asyncio.to_thread(remove)


def remove_workflow_patches_now(workflow_id: str) -> None:
    """Best-effort removal of every raw patch owned by one workflow.

    Never follows a replacement symlink and never removes worktree directories. The
    database deletion path calls this after its transaction commits, where cleanup
    failure must not resurrect rows that were already deleted.
    """
    directory = WORKTREE_ROOT / workflow_id
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return
    try:
        children = list(directory.iterdir())
    except OSError:
        return
    for child in children:
        if child.suffix != ".patch":
            continue
        try:
            child_info = child.lstat()
            if stat.S_ISREG(child_info.st_mode) and child_info.st_uid == os.getuid():
                child.unlink()
        except OSError:
            continue
    with suppress(OSError):
        directory.rmdir()


async def remove_workflow_patches(workflow_id: str) -> None:
    await asyncio.to_thread(remove_workflow_patches_now, workflow_id)


async def sweep_recovery_artifacts(
    max_age_s: float = RECOVERY_RETENTION_S,
    max_worktrees: int = MAX_RECOVERY_WORKTREES_PER_SWEEP,
) -> tuple[int, int]:
    """Remove owned recovery patches and registered worktrees older than the TTL."""
    if max_age_s < 0:
        raise ValueError("recovery retention cannot be negative")
    if max_worktrees < 0:
        raise ValueError("recovery worktree limit cannot be negative")
    patches, worktrees = await asyncio.to_thread(
        _stale_recovery_artifacts, max_age_s, max_worktrees
    )
    removed_worktrees = 0
    for path in worktrees:
        if await _remove_expired_worktree(path):
            removed_worktrees += 1
        else:
            await asyncio.to_thread(_defer_expired_worktree, path)
        with suppress(OSError):
            path.parent.rmdir()
    return patches, removed_worktrees


def _defer_expired_worktree(path: Path) -> None:
    """Quarantine one failed cleanup attempt until the next retention window.

    Unknown directories are deliberately preserved because they may hold the only
    copy of a failed run. Refreshing only the owned directory's mtime keeps that
    safety property while preventing the same few failures from consuming every
    bounded sweep forever.
    """
    try:
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            os.utime(path, None, follow_symlinks=False)
    except OSError:
        pass


def _stale_recovery_artifacts(
    max_age_s: float, max_worktrees: int
) -> tuple[int, list[Path]]:
    """Delete stale regular patch files and return stale worktrees for git cleanup."""
    try:
        root_info = WORKTREE_ROOT.lstat()
    except FileNotFoundError:
        return 0, []
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
        return 0, []

    cutoff = time.time() - max_age_s
    removed_patches = 0
    worktrees: list[Path] = []
    try:
        workflows = list(WORKTREE_ROOT.iterdir())
    except OSError:
        return 0, []
    for workflow in workflows:
        try:
            workflow_info = workflow.lstat()
            if not stat.S_ISDIR(workflow_info.st_mode) or workflow_info.st_uid != os.getuid():
                continue
            children = list(workflow.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                info = child.lstat()
            except OSError:
                continue
            if info.st_uid != os.getuid() or info.st_mtime > cutoff:
                continue
            if stat.S_ISREG(info.st_mode) and child.suffix == ".patch":
                try:
                    child.unlink()
                    removed_patches += 1
                except OSError:
                    pass
            elif stat.S_ISDIR(info.st_mode) and len(worktrees) < max_worktrees:
                worktrees.append(child)
        with suppress(OSError):
            workflow.rmdir()
    return removed_patches, worktrees


async def _remove_expired_worktree(path: Path) -> bool:
    """Deregister a stale worktree; preserve it when git cannot identify its owner."""
    code, common, _ = await _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code != 0 or not common.strip():
        return False
    common_dir = Path(common.strip())
    code, _, _ = await _git(
        common_dir,
        "--git-dir",
        str(common_dir),
        "worktree",
        "remove",
        "--force",
        str(path),
    )
    if code != 0:
        return False
    await _git(common_dir, "--git-dir", str(common_dir), "worktree", "prune")
    return not path.exists()


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
    recovery_warning = ""
    recovery_written = False
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
            if patch:
                try:
                    await save_patch(workflow_id, step_id, patch)
                    recovery_written = True
                except Exception as exc:
                    # The raw response is still useful, and the worktree is a second
                    # copy until an operator can repair the recovery directory.
                    keep = True
                    recovery_warning = (
                        str(exc)
                        if isinstance(exc, CodeError)
                        else f"could not preserve the raw patch: {type(exc).__name__}"
                    )
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
        recovery_warning=recovery_warning,
        recovery_written=recovery_written,
    )


def _private(directory: Path) -> None:
    """Make the worktree directories ours alone, and refuse one that is not.

    `mkdir` honours the umask, so on a lax one these arrived world-readable -- and a
    worktree is a checkout of the user's repository plus, between the run and the
    capture, the only copy of the step's work. Another local user could read it, or
    swap a file underneath it before it is diffed.

    Loosened permissions on a directory we own are tightened rather than refused: it
    is ours to fix, and the alternative is a server that stops working over a
    directory it created itself under a different umask. A directory owned by someone
    else, or a symlink standing where one should be, is refused -- `lstat`, so the
    symlink fails here instead of being followed to somewhere respectable.

    `lstat` before `mkdir`, because `mkdir(exist_ok=True)` only swallows the collision
    when `is_dir()` agrees, and `is_dir()` follows links: a plain file or a dangling
    symlink used to raise `FileExistsError` out of here and reach the caller as a
    transport failure carrying an exception string, instead of the refusal that says
    which path to remove. The second `lstat` is for the race -- something arriving at
    the path between the first one and the `mkdir`, whether it is another step's
    directory or the file this check exists to refuse. Whatever won is what gets
    checked, so the collision is swallowed here and answered there.
    """
    for target in (WORKTREE_ROOT, directory):
        try:
            info = target.lstat()
        except FileNotFoundError:
            with suppress(FileExistsError):
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = target.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise CodeError(
                ConsultErrorCode.INVALID_REQUEST,
                f"`{target}` is not a directory this user owns, so it cannot hold a "
                "worktree. Remove it, or point the server at a path you own",
            )
        if info.st_mode & 0o077:
            target.chmod(0o700)


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
    _private(path.parent)
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
