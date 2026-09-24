"""Every way out of the worktree we could think of, tried against the real kernel.

`tests/test_sandbox.py` proves the basic shape: a write inside works, a write outside
does not. This file is the matrix that gates the capability change -- creating a file
outside is the easiest escape to think of and the least interesting one, because a
worker that wanted to do damage would overwrite something that already exists, rename
its way out, or follow a symlink it planted earlier.

These skip where the host has no mechanism that holds -- not merely where none is
installed, because an installed mechanism that cannot start a child fails every probe
below by refusing to run it, which is indistinguishable from confinement unless
something insists the child ran. A skip says "not proved here", which is honest;
asserting on a generated profile and calling it a security test would say "proved"
about something nobody ran.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import pytest

from orchestrator_mcp.code import sandbox

from .conftest import attempt, confined

pytestmark = confined


def test_an_existing_file_outside_cannot_be_overwritten(worktree: Path, outside: Path) -> None:
    target = outside / "precious.txt"
    assert attempt(f'printf pwned > "{target}"', worktree).returncode != 0
    assert target.read_text() == "original"


def test_an_existing_file_outside_cannot_be_appended_to(worktree: Path, outside: Path) -> None:
    # A different rule from truncation on both mechanisms, and the quieter of the two:
    # an appended line survives a reader who checks that the file is still there.
    target = outside / "precious.txt"
    assert attempt(f'printf pwned >> "{target}"', worktree).returncode != 0
    assert target.read_text() == "original"


def test_a_file_outside_cannot_be_deleted(worktree: Path, outside: Path) -> None:
    target = outside / "precious.txt"
    assert attempt(f'rm -f "{target}"', worktree).returncode != 0
    assert target.exists()


def test_a_file_outside_cannot_have_its_mode_changed(worktree: Path, outside: Path) -> None:
    target = outside / "precious.txt"
    before = target.stat().st_mode
    assert attempt(f'chmod 777 "{target}"', worktree).returncode != 0
    assert target.stat().st_mode == before


def test_a_directory_cannot_be_created_outside(worktree: Path, outside: Path) -> None:
    target = outside / "new-directory"
    assert attempt(f'mkdir "{target}"', worktree).returncode != 0
    assert not target.exists()


def test_a_relative_path_cannot_climb_out(worktree: Path) -> None:
    """`..` from the worker's own working directory. The path never names the outside
    tree, so a check that matched on strings would pass it."""
    escaped = worktree.parent / "climbed.txt"
    assert attempt("printf pwned > ../climbed.txt", worktree, cwd=worktree).returncode != 0
    assert not escaped.exists()


def test_a_symlink_planted_in_the_worktree_is_not_a_door(worktree: Path, outside: Path) -> None:
    """The write is to a path inside the granted subtree. Only resolving it catches this,
    which is why the profile is written in terms of the resolved worktree."""
    (worktree / "doorway").symlink_to(outside)
    target = outside / "precious.txt"
    assert attempt('printf pwned > doorway/precious.txt', worktree, cwd=worktree).returncode != 0
    assert target.read_text() == "original"


def test_a_symlink_to_a_single_file_outside_is_not_a_door_either(
    worktree: Path, outside: Path
) -> None:
    target = outside / "precious.txt"
    (worktree / "shortcut").symlink_to(target)
    assert attempt('printf pwned > shortcut', worktree, cwd=worktree).returncode != 0
    assert target.read_text() == "original"


def test_a_file_cannot_be_renamed_out_of_the_worktree(worktree: Path, outside: Path) -> None:
    """Renaming is not writing, and a mechanism that only covered `open` would let the
    worker carry a file out rather than copy it."""
    (worktree / "carried.txt").write_text("payload")
    escaped = outside / "carried.txt"
    assert attempt(f'mv carried.txt "{escaped}"', worktree, cwd=worktree).returncode != 0
    assert not escaped.exists()


def test_credentials_cannot_be_read_through_a_symlink(worktree: Path, secret_dir: Path) -> None:
    """The deny list is written against real paths, so the interesting question is
    whether it survives being approached from inside the granted subtree."""
    (worktree / "keys").symlink_to(secret_dir)
    # The trailing slash matters: `ls keys` lists the link itself, which is a name
    # inside the worktree and tells us nothing about the target. `-A` because
    # credential directories are mostly dotfiles and plain `ls` omits them. The
    # assertion is on the listing rather than the exit code, because the mechanisms
    # deny in different shapes -- sandbox-exec refuses the read, bubblewrap leaves an
    # empty tmpfs that `ls` walks successfully -- while both leave nothing to be seen.
    # And the marker distinguishes a masked directory from a wrapper that never
    # started, which produces the same empty stdout.
    listing = "ls -A keys/; echo LISTED"
    unconfined = subprocess.run(
        ["/bin/sh", "-c", listing],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ".hidden_token" in unconfined.stdout
    assert attempt(listing, worktree, cwd=worktree).stdout.split() == ["LISTED"]


GRANDCHILD = "ORCHESTRATOR-GRANDCHILD-STARTED"


def test_a_grandchild_process_is_confined(worktree: Path, outside: Path) -> None:
    """Two levels down. One level is already covered; a runtime spawns a build tool
    that spawns a compiler, and containment that thins out with depth is not
    containment.

    Its own marker, because `attempt`'s answers a different question: that one proves
    the outer shell started, and an outer shell that starts and then fails to spawn
    anything leaves the same nonzero status and the same unchanged file as one whose
    grandchild was denied. Depth is the thing under test, so the deepest level is what
    has to announce itself.
    """
    target = outside / "precious.txt"
    # Quoted at each level rather than nested by hand: the script is read by three
    # shells, so a path with a space or a quote in it is three chances to become a
    # different command than the one written here.
    inner = f'echo {GRANDCHILD} >&2; printf pwned > {shlex.quote(str(target))}'
    script = f"/bin/sh -c {shlex.quote(f'/bin/sh -c {shlex.quote(inner)}')}"
    denied = attempt(script, worktree)
    assert GRANDCHILD in denied.stderr, "no grandchild ran; depth was never tested"
    assert denied.returncode != 0
    assert target.read_text() == "original"


def test_the_worker_cannot_write_to_the_users_home(worktree: Path) -> None:
    """The path that matters most and is not in any temp directory. Nothing is left on
    the host either way: the assertion is that the probe file never appears.

    The real home and not a faked one, because the deny list is built from
    `Path.home()` -- point that elsewhere and the test proves a rule that does not
    ship. Which makes the control necessary: a home this user cannot write to would
    pass the assertion below without anything having been denied.
    """
    target = Path.home() / ".orchestrator-escape-probe"
    assert not target.exists(), "a previous run escaped; remove the file before rerunning"
    with tempfile.NamedTemporaryFile(dir=Path.home(), prefix=".orchestrator-control-") as control:
        control.write(b"ok")
        control.flush()
    assert attempt(f'printf pwned > "{target}"', worktree).returncode != 0
    assert not target.exists()


def test_a_granted_worktree_is_still_fully_writable(worktree: Path) -> None:
    """The other half. A sandbox that denied everything would pass every test above and
    be useless, and this is the assertion that says the boundary is in the right place."""
    nested = worktree / "src" / "deep"
    assert attempt("mkdir -p src/deep && printf ok > src/deep/file.txt", worktree,
                   cwd=worktree).returncode == 0
    assert (nested / "file.txt").read_text() == "ok"


def test_the_environment_a_worker_gets_keeps_its_temp_files_in_the_tree(worktree: Path) -> None:
    env = {**os.environ, **sandbox.env_overrides(worktree)}
    assert attempt('printf ok > "$TMPDIR/scratch"', worktree, env=env).returncode == 0
    assert (worktree / ".orchestrator-tmp" / "scratch").read_text() == "ok"


def test_a_worker_cannot_hard_link_its_way_to_a_file_outside(worktree: Path, outside: Path) -> None:
    """Both mechanisms bound pathnames, and a hard link is a second pathname for one
    inode. Made from inside, it would put a granted name on an ungranted file and every
    write through it would be inside the worktree by every rule the profile can state.

    `wrap` refuses a tree that already contains one. This is the other direction: the
    link the worker makes for itself while it is running, which no scan can precede.
    """
    target = outside / "precious.txt"
    assert attempt(f'ln "{target}" ./escape', worktree, cwd=worktree).returncode != 0
    assert not (worktree / "escape").exists()
    assert target.read_text() == "original"


def test_a_worker_cannot_signal_a_process_outside_its_group(worktree: Path) -> None:
    """The grant this module shipped was `(allow signal)`, unqualified: every process
    this user runs, killable from inside the sandbox. Nothing in the worktree changes,
    so no filesystem assertion above would have noticed.

    The victim is an ordinary child of the test process, which is the session the host
    executor and the operator's editor live in.

    `start_new_session=True` because the bound is the process group and that is where
    it comes from: without it the confined shell inherits this process's group, the
    victim is inside that group, and the signal is allowed. Not a weakening of the test
    -- it is the launch every worker gets, from `run_process`, which passes the same
    flag and is covered by its own test so this one cannot be quietly undermined.
    """
    # 30 rather than 5: a victim that exits underneath the probe makes `kill` report
    # ESRCH, which is nonzero and is not a denial. Same false pass `_probe_signal` was
    # carrying, and the same fix -- outlive the measurement.
    victim = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        denied = attempt(f"kill -TERM {victim.pid}", worktree, start_new_session=True)
        assert denied.returncode != 0
        with pytest.raises(subprocess.TimeoutExpired):
            victim.wait(timeout=2)
    finally:
        victim.kill()
        victim.wait()


def test_a_worker_can_still_signal_its_own_children(worktree: Path) -> None:
    """The scope that was tried before this one, `(target self)`, denied this: a
    confined process could not kill a process it had started itself. Which breaks any
    worker that runs a test suite with a timeout, and the failure looks like a hang.

    So the bound is the process group, and this is the half of it that has to keep
    working -- a boundary in the wrong place is as wrong as no boundary.
    """
    script = 'sleep 30 & child=$!; kill -TERM "$child"; wait "$child"; echo REAPED'
    assert "REAPED" in attempt(script, worktree, start_new_session=True).stdout


def test_credential_files_are_not_readable(
    worktree: Path, unmasked_root: Path, monkeypatch
) -> None:
    """Directories were hidden; the single-file credentials next to them were not.
    `~/.netrc` and `~/.git-credentials` are plaintext passwords in a file, and a worker
    that can read them does not need to escape the worktree to do damage.

    Pointed at a temporary home rather than the operator's: the assertion is about the
    profile's shape, and a test that needed a real `~/.netrc` to exist would be skipped
    on the machines that matter and would read one where it ran.

    What is asserted is the secret's absence, not the exit status, because the two
    mechanisms reach the same promise by routes that differ in exactly that. macOS
    denies the read and `cat` fails; Linux binds `/dev/null` over the file, so `cat`
    succeeds and prints nothing. Requiring a nonzero status made the secure Linux
    behaviour a test failure -- never seen, because CI's Linux runner has no `bwrap`.
    `attempt` already refuses to return a result whose command did not run, so the
    absence below is a read that happened and found nothing.
    """
    secret = unmasked_root / "home" / ".netrc"
    secret.parent.mkdir(parents=True)
    secret.write_text("machine example.com login root password hunter2\n")
    monkeypatch.setattr(sandbox, "_secret_files", lambda: [secret])
    assert "hunter2" in secret.read_text(), "the control: unconfined, it is readable"
    denied = attempt(f'cat "{secret}"', worktree)
    assert "hunter2" not in denied.stdout
    assert "hunter2" not in denied.stderr
