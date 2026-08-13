"""The write-side protocol, and what a contained run is allowed to report.

Separate from `ConsultAdapter` on purpose. That one is narrow -- three verbs, no way
to ask for anything agentic -- and the claim only holds if a write path cannot be
reached through it. So this protocol lives in its own package with its own registry,
and nothing converts one into the other.

One verb, and no resume. A contained run is a whole piece of work against a worktree
that is thrown away afterwards; there is no session to continue into, and offering
one would mean keeping the worktree alive to be continued into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ...contract import Usage


@dataclass(frozen=True)
class ObservedCommand:
    """A command the CLI told us about, with the exit code it reported.

    "Told us about" is the whole caveat, and it is not a small one. Codex omits
    commands its sandbox denied from the JSONL stream entirely -- three commands run,
    two events -- so this list is a partial account by construction. It is evidence
    about what a step tried, never the record of what it did. That record is the diff.
    """

    command: str
    exit_code: int | None = None
    output_tail: str = ""


@dataclass(frozen=True)
class CodeResult:
    """What one contained run produced, read from the worktree rather than the model.

    `patch` and `files` come from git after the child exits, so they describe what is
    actually on disk. `summary`, `commands` and `raw_output` are the run's own account
    of itself and are kept as that: useful for reading, never the thing the workflow
    believes.

    No result commit. The plan named one, and building it would mean committing inside
    the worktree -- which writes an object into the user's repository that nothing
    references the moment the worktree is removed, and that git will garbage-collect.
    An anchor that expires is worse than no anchor, so the diff against the baseline is
    the whole artifact, and the real result commit is the one the *host* makes when it
    applies the patch, exactly as in `patch` mode.
    """

    baseline_commit: str
    patch: str
    files: list[str]
    # Files the step wrote that no patch can carry: `git add -A` skips ignored paths
    # by design. Named rather than dropped, so a step that wrote a generated file
    # under an ignored directory does not look like a step that wrote nothing there.
    ignored: list[str] = field(default_factory=list)
    summary: str = ""
    commands: list[ObservedCommand] = field(default_factory=list)
    native_session_id: str | None = None
    model_used: str = ""
    model_verified: bool = False
    raw_output: str = ""
    usage: Usage = field(default_factory=Usage)

    @property
    def changed(self) -> bool:
        return bool(self.patch.strip())


@dataclass(frozen=True)
class AdapterRun:
    """The child's side of a contained run: what it said, and what it says it did.

    Deliberately not `CodeResult`: an adapter never reports what *changed*, because an
    adapter only has the model's word for it. The service reads git and builds the
    result. Keeping the two types apart is what stops a future adapter from filling
    in `patch` from something the model printed.
    """

    summary: str
    commands: list[ObservedCommand] = field(default_factory=list)
    claimed_paths: list[str] = field(default_factory=list)
    native_session_id: str | None = None
    model_used: str = ""
    model_verified: bool = False
    raw_output: str = ""
    usage: Usage = field(default_factory=Usage)


class CodeAdapter(Protocol):
    """A runtime that can be held to a directory while it edits.

    Implemented only where the containment is enforced by something other than the
    model's cooperation -- see `RUNTIME_CAPABILITIES`. The worktree is passed in
    rather than chosen here: deciding where a write may land is the service's job,
    and an adapter that picked its own directory could pick the user's checkout.
    """

    runtime: str

    async def execute(
        self,
        agent: object,
        prompt: object,
        worktree: Path,
        timeout_s: float,
    ) -> AdapterRun: ...
