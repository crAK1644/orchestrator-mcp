"""Shared setup for the escape matrix.

The probes all have the same shape -- run something inside the sandbox that tries to
touch a path outside it, then assert on both the exit code and the filesystem. The
second assertion is the one that matters: a mechanism that reports failure while still
performing the write has denied nothing, and only looking at the file notices.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator_mcp.code import sandbox

confined = pytest.mark.skipif(
    not sandbox.holds(),
    # `holds()` and not `mechanism()`: an installed mechanism that cannot start a
    # child denies nothing and refuses everything, which is the one host where these
    # assertions all pass while proving nothing. Costs nothing extra -- the reason
    # string below already runs the probe.
    reason=f"no process sandbox holds on this host: {sandbox.unavailable_reason()}",
)


@pytest.fixture(autouse=True)
def fresh_detection():
    """Both caches, because `holds` runs the escape and its answer outlives the test."""
    clears = (sandbox.mechanism.cache_clear, sandbox.holds.cache_clear)
    for clear in clears:
        clear()
    yield
    for clear in clears:
        clear()


@pytest.fixture
def worktree(unmasked_root: Path) -> Path:
    """Under the home directory rather than `tmp_path`; see `unmasked_root`. `/tmp` is
    masked on Linux and `sandbox._maskable` refuses a worktree beneath a mask."""
    tree = unmasked_root / "worktree"
    tree.mkdir()
    return tree


@pytest.fixture
def outside(unmasked_root: Path) -> Path:
    """A directory the worker was never granted, with something already in it.

    Pre-populated on purpose. Creating a new file outside the tree is the escape that
    gets tested; overwriting one that is already there is the escape that does damage,
    and it exercises a different SBPL/bind rule.

    Beside the worktree, not under `tmp_path`, so that every denial below is the
    profile denying. Under `/tmp` the Linux writes would land in the mask's tmpfs and
    succeed, leaving the file assertions passing on a mechanism that refused nothing.
    """
    directory = unmasked_root / "outside"
    directory.mkdir()
    (directory / "precious.txt").write_text("original")
    return directory


STARTED = "ORCHESTRATOR-PROBE-STARTED"


def attempt(script: str, worktree: Path, **kwargs) -> subprocess.CompletedProcess:
    """Run `script` confined to `worktree`. Never raises: a denial is the expected result.

    Every caller below asserts on a denial -- a nonzero status, an unchanged file --
    and a wrapper that never launched the child produces both. So the script is
    prefixed with a marker, and this helper refuses to return a result that does not
    carry it. On stderr and not stdout because two callers read stdout, and before
    the script and not after because the script is expected to fail.
    """
    argv = sandbox.wrap(["/bin/sh", "-c", f"echo {STARTED} >&2; {script}"], worktree)
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, **kwargs)
    assert STARTED in done.stderr, (
        f"the confined shell never ran, so nothing here was denied by anything: {done.stderr!r}"
    )
    return done
