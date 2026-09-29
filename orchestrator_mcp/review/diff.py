"""`diff_ref`: a review's material, read from git objects on this machine.

Saves the caller writing a branch diff into a file just so `context_paths` can name it.
The reviewer still gets bytes and never a repository, so the same preview, secret scan
and approval hash apply as for any other material.

Three things keep `git` from becoming a way to run or read more than asked:

  * both sides are resolved to full SHAs first and the diff runs on those, so a branch
    that moves between the two calls cannot make the manifest describe something else;
  * only commit to commit. A diff against the worktree reads the index, which is where
    `core.fsmonitor` runs, and would send changes nobody committed;
  * `--no-ext-diff --no-textconv`: a repository's config can name a program for either,
    and a diff of a repository you do not control must not run it.

`git` goes through `run_process`, so it inherits the process-group kill and the output
cap, and its environment is the passthrough set: no `GIT_*` reaches it.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..consult.adapters.base import AdapterError, run_process
from ..consult.contract import MAX_CONTEXT_CHARS, MAX_LABEL_CHARS
from .contract import MaterialItem

GIT_TIMEOUT_S = 30.0
MAX_REF_CHARS = 200
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def parse_ref(ref: str) -> tuple[str, str, str | None]:
    """`(left, operator, right)`, where a single commit `X` is `(X, "..", None)`."""
    if not ref or len(ref) > MAX_REF_CHARS:
        raise ValueError(f"`diff_ref` must be 1 to {MAX_REF_CHARS} characters")
    if any(c.isspace() or not c.isprintable() for c in ref):
        raise ValueError("`diff_ref` may not hold whitespace or control characters")
    op = "..." if "..." in ref else ".." if ".." in ref else ""
    sides = ref.split(op) if op else [ref]
    if len(sides) != (2 if op else 1) or not all(sides) or any(s.startswith("-") for s in sides):
        raise ValueError(
            "`diff_ref` is `A..B`, `A...B` or one commit; a name may not be empty or "
            "start with `-`"
        )
    return sides[0], op or "..", sides[1] if op else None


async def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        result = await run_process(["git", *args], None, GIT_TIMEOUT_S, cwd=repo)
    except AdapterError as exc:
        raise ValueError(f"git failed in `{repo}`: {exc}") from exc
    return result.returncode, result.stdout


async def _commit(repo: Path, name: str, shown: str) -> str:
    code, out = await _git(repo, "rev-parse", "--verify", "--end-of-options", f"{name}^{{commit}}")
    out = out.strip()
    if code != 0 or not _SHA.fullmatch(out):
        raise ValueError(
            f"`{shown}` is not a commit in `{repo}`. Name a branch, tag or commit: "
            "uncommitted changes, files and trees are not accepted"
        )
    return out


async def read_diff(ref: str, repo: str | None, roots: list[Path]) -> tuple[str, MaterialItem]:
    """The text of one committed diff, and the manifest entry that describes it.

    Refusals are `ValueError`, as they are for `context_paths`.
    """
    left, op, right = parse_ref(ref)
    if not roots:
        raise ValueError(
            "`diff_ref` is disabled until `consult.review.roots:` names the directories "
            "a repository may sit in"
        )
    allowed: list[Path] = []
    for root in roots:
        try:
            allowed.append(root.resolve(strict=True))
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"root `{root}` cannot be read: {type(exc).__name__}") from exc
    if repo is None:
        if len(allowed) != 1:
            raise ValueError(
                "`diff_repo` is required: `consult.review.roots:` names more than one directory"
            )
        repo = str(allowed[0])
    try:
        path = Path(repo).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"`{repo}` cannot be read: {type(exc).__name__}") from exc
    choices = ", ".join(f"`{r}`" for r in allowed)
    if not path.is_dir() or not any(path.is_relative_to(r) for r in allowed):
        raise ValueError(
            f"`{repo}` is not a directory beneath `consult.review.roots`; allowed roots: {choices}"
        )

    code, top = await _git(path, "rev-parse", "--show-toplevel")
    top = top.strip()
    if code != 0 or not top:
        raise ValueError(f"`{path}` is not inside a git repository; pass `diff_repo`")
    # A subdirectory of a repository is inside a root while the repository is not, and a
    # diff covers the whole repository: the files it names are the ones that leave.
    if not any(Path(top).resolve().is_relative_to(r) for r in allowed):
        raise ValueError(
            f"`{path}` is inside the repository `{top}`, which is outside "
            f"`consult.review.roots`; allowed roots: {choices}"
        )

    if right is None:
        # A merge means its first parent, which is what `git show` displays too.
        right = await _commit(path, left, left)
        try:
            left = await _commit(path, f"{right}^1", left)
        except ValueError:
            raise ValueError(f"`{ref}` has no parent commit; name a range instead") from None
    else:
        left, right = await _commit(path, left, left), await _commit(path, right, right)

    code, text = await _git(
        path, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
        "--end-of-options", f"{left}{op}{right}", "--",
    )
    if code != 0:
        raise ValueError(f"`git diff {ref}` failed in `{path}`")
    if not text.strip():
        raise ValueError(f"no changes between {left[:12]} and {right[:12]}")
    if len(text) > MAX_CONTEXT_CHARS:
        raise ValueError(
            f"`git diff {ref}` is {len(text)} characters, over the {MAX_CONTEXT_CHARS} "
            "limit; narrow the range"
        )
    item = MaterialItem(
        label=f"git diff {ref}"[:MAX_LABEL_CHARS],
        kind="text",
        locator=f"{left[:12]}{op}{right[:12]}",
        chars=len(text),
    )
    return text, item
