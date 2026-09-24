"""Confine a worker process to one directory, using the kernel rather than its goodwill.

`registry.py` states the bar this module exists to clear: a runtime gets `isolated_write`
only once "the process runs inside an OS-level sandbox whose writable filesystem is the
worktree". Codex clears it by itself -- its CLI sets `sandbox_mode="workspace-write"` and
the writable path becomes a bound. Every other runtime ships permissions, and a
permission is a request to a cooperating process. This module supplies the bound those
runtimes do not bring.

What that means concretely:

  * **Writes** that outlive the process are the property being enforced. Inside the
    worktree, allowed; anywhere on the host, denied. The two mechanisms reach that by
    different routes and the difference is worth stating: `sandbox-exec` denies the
    syscall, so a write outside fails with `EPERM`, while bubblewrap masks a few
    credential paths with a tmpfs the process *can* write to and whose contents vanish
    with the namespace. "No write outside the worktree survives" is true of both; "every
    write outside the worktree is refused" is true only of the first. `/tmp` is not
    writable either -- the same choice `CodexWriteAdapter` makes with
    `exclude_slash_tmp`, and for the same reason: a step that leaves state outside the
    tree is a step whose result is not reproducible from the diff. Use `env_overrides()`
    to point `TMPDIR` back inside the worktree.
  * **Reads** are broad, minus a deny list of credential paths. Broad because a
    toolchain reads its interpreter, its libraries, and half of `/usr` before it does
    anything useful, and enumerating that per language is how a sandbox becomes a
    permanent source of mysterious build failures. The deny list is what makes the
    breadth acceptable.
  * **Signals** reach the confined process group and nothing else, so a build tool can
    still kill the compiler it started. What that bound rests on is the caller putting
    the wrapped process in its own session; see `_macos_profile`.
  * **Network** is off by default, with two other settings. `"loopback"` is for a
    worker whose model endpoint is served on this machine: it grants one port on this
    host and nothing else. The name describes the destination a caller is asking for,
    not the interface the kernel ends up enforcing: measured, `sandbox-exec` bounds the
    port and the host, so another service answering on the same port on this machine's
    LAN address is reachable too, and `verify_loopback` refuses the mode on such a host.
    `"internet"` is for a worker whose model is hosted -- Claude Code, a hosted
    OpenCode provider -- and grants every IP destination, this host's own services
    included. It is the weakest setting here and is offered because the alternative is
    no containment at all for those runtimes: what it keeps is the write bound, the
    credential deny list and the signal scope, and what it adds on top of `"none"` is
    that the worker can send what it can read anywhere it likes. Unix domain sockets
    stay refused, because a socket is a pathname into another process -- an agent that
    signs, a daemon that writes as root -- and not a destination on the network.
    Each setting other than `"none"` is honoured only where the mechanism can express
    it *and has been shown to*, which today is macOS; see `NETWORK_MODES`,
    `verify_loopback` and `verify_internet`.
  * **What is not bounded**: an inode reachable by a second name. Both mechanisms grant
    writes by pathname, so a hard link inside the worktree pointing at a file outside it
    is a door. `wrap` refuses to build an argv for such a worktree rather than pretend
    otherwise; see `_multiply_linked`.

Stdlib only, no config, no policy, no knowledge of delegation: the registry decides
which runtime needs which setting, and this module decides only whether the host can
confine it.

Everything fails closed. `mechanism()` returning `None` is not "run it anyway", and
neither is a mechanism that is installed but no longer denying -- see `holds`.

One thing this module cannot do is audit what it blocked. The kernel refuses the write
and the process gets `EPERM`; there is no channel back that says which path was
attempted. So what is recorded here is the verification -- which mechanism held, or
which one stopped holding -- and the attempt log has to come from the host executor,
which sees intent rather than syscalls.
"""

from __future__ import annotations

import logging
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

MACOS = "sandbox-exec"
LINUX = "bubblewrap"

# Mechanisms that can express "the host's loopback and nothing else". An allow list
# rather than a deny list, because the answer has to be demonstrated per mechanism and
# the safe default for one nobody has run is "not this one": a mechanism added later
# inherits a refusal it can be tested out of, instead of a grant nobody checked.
# `sandbox-exec` is here because its profile starts at `(deny default)`, which covers
# the network with everything else, and the loopback mode adds back exactly one
# outbound clause naming one port. Membership records only that the mechanism can
# express the bound; that this host honours it is a separate question, answered by
# running it -- see `verify_loopback`, which `loopback_unavailable_reason` asks before
# it returns None. Both are required, because a clause that stopped being enforced
# would leave the profile text and this set exactly as they are.
_GRANTS_LOOPBACK = frozenset({MACOS})

# Mechanisms that can express "every IP destination, and no Unix domain socket". Same
# reasoning as `_GRANTS_LOOPBACK`: listed only once it has been run. bubblewrap is not
# here because its one switch leaves the network namespace shared, and a shared
# namespace brings the host's abstract Unix sockets with it -- they have no pathname
# to mask, and an X server or a session bus listens on one.
_GRANTS_INTERNET = frozenset({MACOS})

# What `wrap` accepts for `network`. `internet` exists because Claude Code and hosted
# OpenCode cannot do anything without their provider, and before it they got no
# sandbox at all rather than a weaker one. It is never a default and never implied by
# another setting: a caller names it.
NETWORK_NONE = "none"
NETWORK_LOOPBACK = "loopback"
NETWORK_INTERNET = "internet"
NETWORK_MODES = (NETWORK_NONE, NETWORK_LOOPBACK, NETWORK_INTERNET)

# Read-denied, relative to the user's home. Not exhaustive and not claimed to be: it is
# the set whose exposure turns a contained worker into a credential leak. A worker
# needing one of these is a worker whose task was wrong.
#
# Split by kind because the two are masked differently and a single tuple hid that. The
# directories are the obvious half; the files are the half a `is_dir()` filter silently
# dropped, which made `SECRET_FILES` unaddable to the tuple rather than merely missing
# from it -- `~/.git-credentials` holds a plaintext token and was readable.
SECRET_DIRS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".config/gh",
    ".config/gcloud",
    ".kube",
    ".docker",
)

# Masking the last two has a cost worth naming here, because the failure does not
# look like containment: `.npmrc` and `.pypirc` carry the index URL as well as the
# token, so a confined `pip install` or `npm install` of a package that lives on a
# private registry falls back to the public index and reports a 404 for a name that
# plainly exists. That is the sandbox working. A worker needing a private dependency
# needs it vendored into the worktree or fetched before confinement, not this list
# shortened.
SECRET_FILES = (
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
)

# Character devices a confined process still needs to write to. Without these, anything
# redirecting to /dev/null dies, which is most build tooling.
_WRITABLE_DEVICES = (
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
    "/dev/dtracehelper",
)


class SandboxUnavailable(RuntimeError):
    """No confinement mechanism on this host. Callers refuse; they do not fall back."""


class SandboxEscape(RuntimeError):
    """The mechanism is here and did not hold. Two shapes, one conclusion.

    Either a probe that was supposed to be denied succeeded, or the control that
    proves the probe ran at all did not. The second reads as containment from
    outside -- nothing escaped -- which is why it is this error and not
    `SandboxUnavailable`: the tool is installed and something it does cannot be
    told apart from working.
    """


# The executable each mechanism is, as `shutil.which` spells it.
_EXECUTABLES = {MACOS: "sandbox-exec", LINUX: "bwrap"}


@lru_cache(maxsize=1)
def mechanism() -> str | None:
    """Which confinement mechanism this host has, or `None`.

    Cached because it is asked once per capability lookup and the answer cannot change
    without the process being restarted. Tests call `mechanism.cache_clear()`.
    """
    system = platform.system()
    if system == "Darwin" and shutil.which(_EXECUTABLES[MACOS]):
        return MACOS
    if system == "Linux" and shutil.which(_EXECUTABLES[LINUX]):
        return LINUX
    return None


@lru_cache(maxsize=len(_EXECUTABLES) + 1)
def _binary_for(found: str | None) -> str | None:
    """The absolute, canonical path of `found`'s executable, or `None`.

    Resolved once and reused, because the alternative was emitting the bare name into
    every argv and letting `execvp` resolve it again at launch. That made the binary
    this module verified and the binary it ran two different questions with one answer
    assumed: a `PATH` whose first `bwrap` is a wrapper inside the worktree would be
    verified never and executed always. One resolution, cached beside the verdict that
    was reached through it, closes the window.

    One resolution is not enough on its own, because `shutil.which` returns what it
    found and what it found is *relative* when the `PATH` entry was: a `PATH` carrying
    `tools` rather than `/opt/tools` yields `tools/bwrap`. That is the same hole by a
    quieter route -- this module verifies it against its own working directory, and
    a write adapter launches with `cwd=worktree`, so the two resolve to different
    files and the second is one the worker can write. Refused rather than made
    absolute here: absolutising would pick this process's directory, which is a guess
    about which of the two was meant, and a host whose `PATH` carries a relative entry
    is not one this module can make safe.

    Resolved through its symlinks before it is cached, because the check `wrap` makes
    against the worktree is a comparison of pathnames and a pathname outside the tree
    can name a file inside it. `/opt/bin/bwrap` pointing at `<worktree>/tools/bwrap`
    passes a lexical test and hands the worker the file confining it. What is compared
    and what is executed are both the canonical target now, so there is one file under
    discussion rather than two.

    Keyed on the mechanism's answer rather than on nothing. It used to be
    `lru_cache(maxsize=1)` over a function taking no arguments while depending on
    `mechanism`, which tests replace -- so the cache outlived the pin and the pair
    disagreed: `mechanism()` saying `bubblewrap` beside a `_binary()` of
    `/usr/bin/sandbox-exec`, which is one mechanism's argv built around another's
    executable. Harmless on a real host, where `mechanism()` cannot change under a
    running process. Not harmless in the suite, where it was why a Linux runner passed
    tests asserting macOS argv: the cache was warm with the runner's own `bwrap` and
    the pin never reached `shutil.which` at all.
    """
    if found not in _EXECUTABLES:
        return None
    path = shutil.which(_EXECUTABLES[found])
    if path is None:
        return None
    if not Path(path).is_absolute():
        logger.error(
            "refusing the %s executable found at the relative path %r: `PATH` carries a "
            "relative entry, so the binary verified here and the binary executed at "
            "launch would be resolved against different directories",
            found,
            path,
        )
        return None
    # `strict=False`: `which` already proved the path is there and executable, and a
    # resolution that raised here would be a third answer this function does not have.
    return str(Path(path).resolve())


def _binary() -> str | None:
    """The executable for the mechanism this host reports right now."""
    return _binary_for(mechanism())


# The cache lives on the keyed function; `_clear_verification` empties it by this name.
_binary.cache_clear = _binary_for.cache_clear


@lru_cache(maxsize=1)
def _verified() -> tuple[bool, str]:
    """Whether this host's mechanism actually denies, proved by trying to escape it,
    and what went wrong if it did not.

    Both together and under one cache, because they are one answer. Held apart -- a
    cached boolean beside a module global -- a reader could get the verdict from one
    verification and the explanation from another, and the explanation is the half an
    operator acts on.

    The distinction from `mechanism()` is the whole point. `mechanism()` answers "is the
    tool installed", which is what a capability gate reaches for and is not what the
    capability claims. `sandbox-exec` has been deprecated by Apple for several releases;
    a profile directive that stops being honoured, a hardened runtime that refuses to
    apply the profile at all, a `bwrap` that cannot create a user namespace because the
    host disabled them -- each leaves the binary exactly where it was and the boundary
    gone. Gating on the binary would hand out `isolated_write` on all of them.

    Once per process, because it costs two subprocesses and the answer cannot change
    without a restart. Tests call `holds.cache_clear()`.
    """
    try:
        report = verify()
    except SandboxUnavailable:
        return False, ""
    except SandboxEscape as escape:
        # Loud, and at ERROR: the host has a sandbox that is not sandboxing, which is
        # the one failure mode that looks identical to a working one from outside.
        logger.error("process sandbox did not hold: %s", escape)
        return False, str(escape)
    logger.info("process sandbox verified: %s", report["mechanism"])
    return True, ""


def holds() -> bool:
    """Whether this host's mechanism denies. The first half of `_verified`."""
    return _verified()[0]


def _clear_verification() -> None:
    """Empty every cache whose answer came out of a verification run.

    All of them together, always. Held separately they could disagree -- a re-run
    verdict beside a resolved binary from the previous one, or a loopback result
    reached against a mechanism that is no longer the answer -- and each of those
    pairs describes a host that does not exist.
    """
    _verified.cache_clear()
    _loopback_verified.cache_clear()
    _internet_verified.cache_clear()
    _binary.cache_clear()


# Tests clear the cache by the name they ask the question through, and there is only
# one thing to call.
holds.cache_clear = _clear_verification


def _loopback_expressible_reason() -> str | None:
    """Why this host's mechanism cannot *express* a loopback-only bound, or None.

    The cheap half, and the one `wrap` asks, because the expensive half runs `wrap`.
    Split for that reason and no other: `verify_loopback` builds a confined argv, so a
    `wrap` that consulted the verified answer would call the verification that calls it.
    The public `loopback_unavailable_reason` asks both, and it is what a capability gate
    asks, because a gate is the place where being sure is worth two subprocesses.

    bubblewrap's network control is one switch: share the host's namespace or take a
    fresh one. A fresh namespace has its own loopback, so a server on the host's
    `127.0.0.1` is not in it, and sharing the host's namespace is the open internet.
    Neither is `"loopback"`, and returning the second one under that name would be the
    module lying about what it granted. What would lift this is a mechanism that has
    been run rather than reasoned about -- a proxy inside the namespace, or the
    endpoint moved into it.

    Which is why the question is asked against `_GRANTS_LOOPBACK` rather than against
    bubblewrap by name. Naming the refusal would mean a mechanism added later is
    granted loopback by nobody having thought about it, in the one module where every
    other unknown is refused.
    """
    found = mechanism()
    if found in _GRANTS_LOOPBACK:
        return None
    if found is None:
        # A cycle runs through here: `unavailable_reason` asks `holds`, which runs
        # `verify`, which calls `wrap`, which asks this. It is closed because
        # `unavailable_reason` short-circuits before `holds` when there is no
        # mechanism -- which is the only way to reach this line -- and because
        # `verify` probes with `network=none`. Adding a `holds()` call above, to make
        # None mean "and it holds", would open it.
        return unavailable_reason()
    if found == LINUX:
        return (
            "bubblewrap cannot grant loopback alone: a fresh network namespace does "
            "not contain the host's `127.0.0.1`, and the only alternative it offers "
            "is the whole network, so a worker that needs a locally served endpoint "
            "is not containable on this host yet"
        )
    return (
        f"`{found}` has not been shown to serve a worker the host's loopback while "
        "denying the rest of the network, so a worker that needs a locally served "
        "endpoint is not containable on this host yet"
    )


def loopback_unavailable_reason() -> str | None:
    """Why a worker needing a locally served endpoint cannot be confined here, or None.

    Two questions, asked in cost order. Can the mechanism express the bound, and does
    the bound hold when a socket is actually opened against it. Only the second is
    evidence, and it is the one nothing used to ask: the mode was admitted on the
    strength of a string appearing in a generated profile, while `verify()` probed with
    the network off, so a clause that had stopped being enforced -- or that never meant
    what it reads like -- passed every check this module had.

    That is not hypothetical. The clause this module shipped, `(remote ip
    "localhost:*")`, was described in a comment as the loopback set. Run against a
    socket it turns out to bound the *host*, not the interface: a listener on this
    machine's LAN address answered a confined process through it. The port bound in
    `_macos_profile` is what narrows that, and this function is what proves the
    narrowing is real on the host in front of us.

    Split out of `wrap` so a capability gate can ask before a launch does. Containment
    and reachability are separate questions and a host can answer them differently:
    bubblewrap contains perfectly well and still cannot offer loopback alone, so a gate
    that gets as far as `holds()` and stops offers the capability and then dies at the
    process start, which is the worst place to find out.
    """
    if (why := _loopback_expressible_reason()) is not None:
        return why
    return _loopback_verified()


def _internet_expressible_reason() -> str | None:
    """Why this host's mechanism cannot *express* the internet bound, or None.

    The cheap half, for the same reason `_loopback_expressible_reason` is: `wrap` asks
    it, and the expensive half runs `wrap`.
    """
    found = mechanism()
    if found in _GRANTS_INTERNET:
        return None
    if found is None:
        # The same closed cycle `_loopback_expressible_reason` describes.
        return unavailable_reason()
    if found == LINUX:
        return (
            "bubblewrap cannot grant the network without sharing the host's network "
            "namespace, which also shares its abstract Unix sockets -- an X server or a "
            "session bus listens on one, and they have no path to mask -- so a worker "
            "that needs a hosted model is not containable on this host yet"
        )
    return (
        f"`{found}` has not been shown to grant IP destinations while denying Unix "
        "domain sockets, so a worker that needs a hosted model is not containable on "
        "this host yet"
    )


def internet_unavailable_reason() -> str | None:
    """Why a worker needing a hosted model cannot be confined here, or None.

    Expressible first, then run: the same two questions `loopback_unavailable_reason`
    asks, for the same reason -- a clause in a generated profile is not evidence.
    """
    if (why := _internet_expressible_reason()) is not None:
        return why
    return _internet_verified()


def unavailable_reason() -> str:
    """Why this host cannot confine a process, naming the platform and the missing tool.

    Worth the specificity: "not supported" reads as "not implemented yet", and an
    operator on Linux who is one `apt install bubblewrap` away from a working
    installation should be told that, not left to guess.
    """
    if mechanism() is not None and not holds():
        # Installed and not holding. A different situation from missing, and a worse
        # one -- nothing on the host looks wrong -- so it does not get to share the
        # sentence about installing a package.
        #
        # Which of the two failures it was is `verify`'s to say, not this function's.
        # Naming one of them here was wrong once already: a probe that never ran
        # denies nothing and proves nothing, and describing it as a write that was
        # not denied sends an operator looking at the profile instead of the launcher.
        # The escape message already names the mechanism, so it is a second sentence
        # rather than a clause: what `verify` raised, quoted, and nothing added to it.
        # From the same snapshot the verdict came out of, so the two cannot describe
        # different runs -- and absent, rather than guessed at, when there is none.
        escaped = _verified()[1]
        said = f" {escaped}" if escaped else ""
        return (
            f"{mechanism()} is installed on this host and did not hold when it was "
            f"tested.{said}"
        )
    system = platform.system()
    if system == "Darwin":
        return "`sandbox-exec` was not found on this macOS host"
    if system == "Linux":
        return "bubblewrap (`bwrap`) is not installed on this Linux host"
    return f"no process sandbox is implemented for {system or 'this platform'}"


def wrap(
    command: Sequence[str],
    worktree: Path,
    *,
    network: str = NETWORK_NONE,
    port: int | None = None,
) -> list[str]:
    """`command`, rewritten to run confined to `worktree`.

    The returned argv is what the caller executes. This module never launches anything
    itself, so there is no path where a caller obtains a command and runs it unwrapped
    by mistake -- wrapping is the only thing on offer.

    `port` is the one destination `network="loopback"` grants and is required for it.
    Required rather than defaulted, because the default that existed -- every port --
    is not a bound anybody chose, and a caller that cannot name the port it needs does
    not need loopback. `network="internet"` takes no port: it grants every one.

    Launch the returned argv in its own session (`start_new_session=True`, which
    `run_process` already passes). The signal bound depends on it; see
    `_macos_profile`.

    Working directory is the caller's to set, on both platforms. Neither mechanism
    chdirs for you, deliberately: a sandbox that quietly relocated the process would
    make the two platforms disagree about where a relative path points, and a security
    module whose behaviour differs by host is one nobody can reason about.
    """
    if not command:
        raise ValueError("command must not be empty")
    resolved = worktree.resolve()
    if not resolved.is_dir():
        # A profile naming a path that does not exist grants nothing and denies
        # everything, which would surface as an incomprehensible failure inside the
        # worker rather than here.
        raise ValueError(f"worktree does not exist: {resolved}")

    if network not in NETWORK_MODES:
        raise ValueError(f"network must be one of {NETWORK_MODES}, not {network!r}")
    if network == NETWORK_LOOPBACK and port is None:
        raise ValueError("network='loopback' needs the port it is meant to reach")
    if network != NETWORK_LOOPBACK and port is not None:
        # Refused rather than ignored. A caller that passed a port and got no network
        # would otherwise debug the endpoint instead of the argument, and one that got
        # every port would believe it had been bounded to one.
        raise ValueError(f"port is meaningless with network={network!r}")

    # Before the dispatch, so the refusal covers every mechanism rather than the ones
    # the branches below happen to name. Defense in depth: the capability gate asks
    # this same question at discovery, and a launch that reached here having skipped it
    # should still not get the network it was not granted. The expressible half only --
    # the verified half runs this function, and asking it here would be a cycle.
    if network == NETWORK_LOOPBACK and (why := _loopback_expressible_reason()) is not None:
        raise SandboxUnavailable(why)
    if network == NETWORK_INTERNET and (why := _internet_expressible_reason()) is not None:
        raise SandboxUnavailable(why)

    if (linked := _multiply_linked(resolved)) is not None:
        # Fail closed, and before anything launches. Both mechanisms grant writes by
        # pathname; an inode with a second name is reachable under whichever of its
        # names the grant happens to cover, so a file inside the worktree that is also
        # a file outside it is a door that the profile cannot see and no escape test
        # of a *path* would find. Executed rather than reasoned about: a planted link
        # overwrote a file outside the tree on this host, exit status zero.
        #
        # `git clone --local` is the ordinary way to end up here -- it hard-links every
        # object to the source repository's store -- which is why the message names the
        # flag rather than only the file.
        raise SandboxUnavailable(
            f"{linked} has more than one name, and this sandbox bounds pathnames rather "
            "than inodes, so writing it from inside the worktree would change a file "
            "that may be outside it. Copy the tree without hard links "
            "(`git clone --no-hardlinks`, or `git worktree add`, which shares its "
            "objects by reference instead)."
        )

    found = mechanism()
    binary = _binary()
    if binary is None:
        if found is None:
            raise SandboxUnavailable(unavailable_reason())
        # Named here rather than deferred to `unavailable_reason`, and not only for the
        # wording: that function asks `holds()`, `holds()` runs `verify()`, and
        # `verify()` comes back through this line. A mechanism that is installed while
        # `_binary` refuses its path is the one arrangement where those two disagree,
        # so the sentence is written out instead of asked for.
        raise SandboxUnavailable(
            f"{found} is installed on this host but its path could not be resolved to "
            "an absolute one, so the binary verified here and the binary executed at "
            "launch would not be the same file. Put an absolute directory on `PATH`."
        )
    if Path(binary).is_relative_to(resolved):
        # The other end of the same problem `_binary` refuses relative paths for: an
        # absolute path can still land inside the tree the worker writes to, and a
        # confining binary the confined process can replace confines nothing after the
        # first launch. Nothing in either profile covers this -- the worktree is the
        # one place both mechanisms grant writes.
        raise SandboxUnavailable(
            f"the {found} executable is {binary}, which is inside the worktree it would "
            "be asked to confine, so the worker can replace the thing confining it. "
            "Move it out of the tree, or off `PATH`."
        )
    # The resolved path, not the bare name: `execvp` would otherwise search `PATH` at
    # launch and could find something other than what was verified.
    if found == MACOS:
        return [binary, "-p", _macos_profile(resolved, network, port), *command]
    if found == LINUX:
        return [*_linux_args(resolved, network, binary), *command]
    raise SandboxUnavailable(unavailable_reason())


def _multiply_linked(worktree: Path) -> Path | None:
    """The first regular file under `worktree` carrying more than one name, or None.

    Conservative on purpose: a file with two names both inside the tree is harmless,
    and finding out which it is would mean walking the filesystem for the other name.
    Refusing on the link count is the cheap answer that is never wrong in the unsafe
    direction.

    `lstat` rather than `stat` so a symlink is counted as itself; symlinks out of the
    tree are a separate question the profile already answers by resolving the worktree.

    ponytail: one walk per launch, which is once per worker rather than once per file
    operation. If it ever shows up in a profile, the upgrade is to walk only what `git
    status` says is present rather than the whole tree.
    """
    for path in worktree.rglob("*"):
        try:
            info = path.lstat()
        except OSError:
            # Vanished or unreadable between the walk and the stat. Not evidence of a
            # link, and not this function's to diagnose.
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return path
    return None


def env_overrides(worktree: Path) -> dict[str, str]:
    """Environment a confined process needs so that a writable temp dir exists.

    Nothing outside the worktree is writable, and a toolchain that cannot write a temp
    file fails in ways that look like anything but a sandbox. So `TMPDIR` points inside
    the tree, where it is both writable and visible in the diff if something is left
    behind.
    """
    temp = worktree.resolve() / ".orchestrator-tmp"
    temp.mkdir(parents=True, exist_ok=True)
    return {"TMPDIR": str(temp)}


def _scratch_root() -> Path:
    """Where verification builds the throwaway worktree it tries to escape.

    Under the user's home rather than the system temp directory, and that is a
    requirement rather than a preference: `/tmp` is one of the directories the Linux
    side masks, and `_maskable` now refuses a worktree beneath a mask instead of
    quietly dropping it, so a tree made in the default temp directory is one `wrap`
    would decline to confine on every Linux host. Home is never masked as a whole --
    the credential paths under it are named one by one -- so a tree here is one both
    mechanisms accept, and it is also where production worktrees live, which makes the
    verification run the arrangement it is verifying.
    """
    root = Path.home() / ".cache" / "orchestrator-sandbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


def verify() -> dict[str, object]:
    """Prove the mechanism holds, by trying to escape it.

    This is the check `registry.py`'s capability claim rests on, so it runs the escape
    rather than reasoning about it: a write inside the worktree must succeed, and a
    write outside it must fail *and leave no file*. Both halves matter -- a mechanism
    that returns a nonzero exit while still writing the file has not denied anything.

    Takes no worktree. It used to accept one and never read it, which is worse than
    not offering the choice: a caller naming a specific -- or absent -- directory got
    a successful report about a different one. The tree is built here because the
    probes have to write into it and a caller's real worktree is not somewhere to
    leave probe files.

    Raises `SandboxUnavailable` where there is nothing to verify, and `SandboxEscape`
    where the mechanism is present and not holding.
    """
    found = mechanism()
    if found is None:
        raise SandboxUnavailable(unavailable_reason())

    with tempfile.TemporaryDirectory(dir=_scratch_root()) as scratch:
        root = Path(scratch).resolve()
        tree = root / "worktree"
        tree.mkdir()
        outside = root / "outside.txt"

        inside_ok, complaint = _probe_write(tree / "inside.txt", tree)
        outside_ok, outside_complaint = _probe_write(outside, tree)

        if not inside_ok:
            # Both halves, and this is the half that says the probe ran at all.
            # `_probe_write` counts a launch failure as a denial, which is the right
            # answer for the write outside and a disaster for this one: a profile the
            # host refuses to apply fails both probes, leaves no file outside, and
            # would otherwise be reported as a mechanism that held. `SandboxEscape`
            # rather than `SandboxUnavailable` because the mechanism is here and is
            # doing something nobody can distinguish from working, which is the
            # failure this error exists to name.
            raise SandboxEscape(
                f"{found} did not permit a write inside {tree}: the probe never ran or "
                "the profile denies the worktree itself, so nothing here has been "
                f"proved about what it denies. {complaint or 'It said nothing.'}"
            )
        if outside_ok is None:
            # The half this check exists for, and the one that read as containment for
            # five rounds. Nothing outside the worktree was written because nothing ran,
            # which is the same absent file a real denial leaves behind. The inside
            # probe having launched says nothing about this one: they are two processes,
            # and the second can time out on its own.
            raise SandboxEscape(
                f"{found} could not be tested for a write outside {tree}: the escape "
                "probe never ran, so the denial this reports would be an assumption. "
                f"{outside_complaint or 'It said nothing.'}"
            )
        if outside_ok or outside.exists():
            raise SandboxEscape(
                f"{found} did not deny a write to {outside}: the worktree is not a bound "
                "on this host, so `isolated_write` must stay refused"
            )
        if (signalled := _probe_signal(tree)) is not None:
            raise SandboxEscape(signalled)
        return {
            "mechanism": found,
            "write_inside_worktree": inside_ok,
            "write_outside_worktree_denied": True,
            "signal_outside_group_denied": True,
        }


def _run_confined(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess | None:
    """Run a confined argv in its own session. `None` if it could not be run at all.

    The new session is not a detail of the test harness: it is the condition the macOS
    signal bound rests on, so a probe that skipped it would be measuring a different
    arrangement than the one production runs.
    """
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, start_new_session=True
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _probe_write(target: Path, worktree: Path) -> tuple[bool | None, str]:
    """Whether `target` appeared, `None` if the probe never ran, and any complaint.

    Three answers because the two failures are different facts and were reported as
    one. A launch that never happened leaves no file, which is the same evidence a
    denied write leaves, and the caller could not tell them apart: the escape half read
    "the sandbox binary could not be run at all" as "the sandbox denied the write" and
    reported a bound from a probe that did not exist. The inside half had a diagnostic
    string to quote and the outside half never looked at it.

    So the probe says whether it ran, the way `_probe_connect` and `_probe_signal` do,
    and the verdict is read off `target` rather than off an exit status. That last part
    is deliberate on Linux: the mask outside the worktree is a tmpfs, so the write
    *succeeds* into a filesystem nobody else can see. Whether the file appeared where
    it was aimed is the question; whether the syscall returned zero is not.
    """
    probe = "\n".join(
        (
            "import pathlib",
            "print('RAN', flush=True)",
            "try:",
            f"    pathlib.Path({str(target)!r}).write_text('probe')",
            "except OSError:",
            "    pass",
        )
    )
    completed = _run_confined(wrap([sys.executable, "-c", probe], worktree))
    if completed is None:
        return None, "the sandbox binary could not be run at all"
    if "RAN" not in completed.stdout:
        return None, completed.stderr.strip() or "the confined interpreter never started"
    return target.exists(), completed.stderr.strip()


def _probe_signal(worktree: Path) -> str | None:
    """What went wrong if a confined process could signal outside its group, else None.

    Run rather than reasoned about, for the same reason as the write: the profile
    directive that expresses this bound is one Apple can stop honouring without the
    profile changing a character, and an unscoped signal grant was live in this module
    until it was executed against a victim.

    The victim is an ordinary `sleep` in this process's session, which is where the
    operator's other work lives -- the host executor, an editor. If it dies, the grant
    reaches them.

    What the verdict is read off is the `kill` syscall's own error, and nothing else.
    Two earlier versions read it off the victim instead and both passed hosts that had
    denied nothing: a nonzero exit was counted as EPERM when it was as easily ESRCH
    from a victim that had already exited, and a *successful* kill was counted as a
    denial whenever the victim outlived a two-second wait. Whether that particular
    process died is a fact about the process. Whether the kernel let the signal be
    sent is the property, so the probe asks Python for the exception type rather than
    asking `sh` for an exit status that cannot tell EPERM from ESRCH.

    `sleep 30` against a 15-second probe timeout, so the victim cannot exit underneath
    the measurement, and it is confirmed running before the signal is sent.
    """
    # Not guarded against `OSError`: if this host cannot start `/bin/sh`, the probe was
    # never arranged, and swallowing that would report a denial nobody demonstrated.
    victim = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        if victim.poll() is not None:
            return (
                f"{mechanism()} could not be tested for signal scope: the victim process "
                "exited before the signal was sent, so nothing was ever a target"
            )
        # `sys.executable` for the same reason `_probe_connect` uses it: an absolute
        # path that exists, and an interpreter that can name the errno rather than an
        # `sh` builtin that flattens every failure into one status.
        probe = "\n".join(
            (
                "import os, signal",
                "print('RAN', flush=True)",
                "try:",
                f"    os.kill({victim.pid}, signal.SIGTERM)",
                "except PermissionError:",
                "    print('DENIED')",
                "except ProcessLookupError:",
                "    print('GONE')",
                "else:",
                "    print('SENT')",
            )
        )
        argv = wrap([sys.executable, "-c", probe], worktree)
        completed = _run_confined(argv, timeout=15)
        if completed is None or "RAN" not in completed.stdout:
            return (
                f"{mechanism()} could not be tested for signal scope: the confined "
                "process never reached the point of sending one, so nothing is known "
                "about what it would have been allowed to signal"
            )
        if "SENT" in completed.stdout:
            return (
                f"{mechanism()} let a confined process signal a process outside its "
                "group: a worker can interrupt the host executor or anything else this "
                "user is running, so the profile's signal scope is not being enforced"
            )
        if "DENIED" in completed.stdout and victim.poll() is None:
            return None
        if "DENIED" in completed.stdout:
            # The same liveness condition `GONE` carries, for the same reason. A victim
            # that exited and had its PID reused by something this probe may not signal
            # answers `EPERM` about a process that never established the control.
            return (
                f"{mechanism()} could not be tested for signal scope: the victim exited "
                "during the probe, so the refusal came back about whatever holds its PID "
                "now rather than about the process outside the group"
            )
        if "GONE" in completed.stdout and victim.poll() is None:
            # The confined process could not see a live process that this one can.
            # That is bubblewrap's answer rather than sandbox-exec's -- `--unshare-pid`
            # puts the victim in another PID namespace, so there is no permission
            # question to ask -- and it is containment by a stronger route than EPERM.
            # Conditioned on the victim still running, because the same ESRCH from a
            # victim that has exited proves nothing at all.
            return None
        return (
            f"{mechanism()} could not be tested for signal scope: the probe reported "
            f"neither a refusal nor a delivery ({completed.stdout.strip()!r}), so "
            "nothing is known about what it would have been allowed to signal"
        )
    finally:
        victim.kill()
        victim.wait()


@lru_cache(maxsize=1)
def _loopback_verified() -> str | None:
    """None if the loopback bound holds on this host, else why it does not.

    Cached beside the other verdict and cleared with it, because it costs two
    subprocesses and cannot change without a restart.
    """
    try:
        verify_loopback()
    except SandboxUnavailable as missing:
        return str(missing)
    except SandboxEscape as escape:
        logger.error("sandbox loopback bound did not hold: %s", escape)
        return str(escape)
    logger.info("sandbox loopback bound verified: %s", mechanism())
    return None


def _other_local_address() -> str | None:
    """A non-loopback address this host answers on, or None if it has only loopback.

    Found by asking the routing table where a packet to a documentation address would
    leave from -- a connected UDP socket sends nothing, so this touches no network.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: reserved, unroutable
            address = str(probe.getsockname()[0])
    except OSError:
        return None
    return None if address.startswith("127.") else address


def verify_loopback() -> dict[str, object]:
    """Prove the loopback grant reaches the one port it names and no other.

    Two listeners, one granted and one not, both on this host. The granted one has to
    answer -- a bound nobody can talk through is not a capability, and a mode that
    quietly reached nothing would look exactly like a model being down. The other has
    to stay unreachable, and it is the assertion that means something: it is the
    difference between a clause that names a port and a clause that grants the host.

    A second listener rather than a public address, because a public address proves
    nothing offline: refused is what an unsandboxed process gets there too. Both
    listeners are real and both are reachable from outside the sandbox, so the only
    thing separating them is the profile.

    A third listener, where the host has a second interface, tests the dimension the
    mode's *name* claims. `localhost` in an SBPL address filter means this machine, not
    this interface: a confined process reached a listener on this host's LAN address at
    the granted port. This used to be measured, logged and allowed, on the reasoning
    that one port on this host is the documented scope of the grant. The name the
    capability is requested under says otherwise, and a caller reading `loopback` is
    not authorizing every service that happens to answer on that port from another
    address. So it is refused.

    The cost is not hypothetical and is the reason this was ever a decision:
    `sandbox-exec` cannot express an interface, so any macOS host with a routable
    address fails here and `isolated_write` stays refused on it. A host with no second
    address has nothing to reach and passes -- that is not a measurement, and it is
    the only case where this dimension is absent rather than clean.

    Not measurable is not the same as clean either: where the address exists and the
    listener could not be bound, the answer is unknown and unknown is refused.

    Raises `SandboxUnavailable` where the mode cannot be expressed here, and
    `SandboxEscape` where it is expressed and not honoured.
    """
    if (why := _loopback_expressible_reason()) is not None:
        raise SandboxUnavailable(why)

    elsewhere: bool | None = None
    with tempfile.TemporaryDirectory(dir=_scratch_root()) as scratch:
        tree = Path(scratch).resolve() / "worktree"
        tree.mkdir()
        with (
            socket.create_server(("127.0.0.1", 0)) as granted,
            socket.create_server(("127.0.0.1", 0)) as withheld,
        ):
            granted_port = granted.getsockname()[1]
            withheld_port = withheld.getsockname()[1]
            reached = _probe_connect(tree, granted_port, granted_port)
            other = _probe_connect(tree, granted_port, withheld_port)
        if (address := _other_local_address()) is not None:
            try:
                # The same port, a different interface. Bound after the loopback
                # listeners are closed so the two cannot collide on a host that
                # resolves them to one socket.
                with socket.create_server((address, granted_port)):
                    elsewhere = _probe_connect(tree, granted_port, granted_port, host=address)
            except OSError:
                # The port is taken on that interface, or the address went away with a
                # DHCP lease. Not something to fail a verification over: this half is a
                # measurement, and an unmeasured one stays None.
                elsewhere = None

    if reached is not True:
        # Includes the interpreter failing to launch, which is the same shape as the
        # write probe's inside half: nothing ran, so nothing was proved, and reading
        # that as a working grant is how a mode comes to mean nothing.
        raise SandboxEscape(
            f"{mechanism()} did not let a confined process reach the port it was "
            f"granted ({granted_port}): either the clause is not being honoured or the "
            "probe never ran, and neither leaves anything proved about what it denies"
        )
    if other is not False:
        raise SandboxEscape(
            f"{mechanism()} granted port {granted_port} and a confined process reached "
            f"{withheld_port} as well: the loopback clause is not bounded by port on "
            "this host, so `loopback` would hand the worker every service on it"
        )
    if address is not None and elsewhere is not False:
        raise SandboxEscape(
            f"{mechanism()} let a confined process reach port {granted_port} on "
            f"{address} as well as on loopback: the grant bounds the port and the host, "
            "not the interface, so `loopback` would also hand the worker whatever "
            f"answers on that port from this machine's other addresses. {mechanism()} "
            "cannot express an interface, so a host with a routable address cannot "
            "carry this mode."
            if elsewhere
            else f"{mechanism()} could not be tested on {address}: the second listener "
            f"would not bind, so whether the grant reaches port {granted_port} through "
            "another of this host's addresses is unknown, and unknown is not a bound"
        )
    return {
        "mechanism": mechanism(),
        "granted_port_reachable": True,
        "other_port_denied": True,
        # `None` only where the host has no second address. Reachable and unmeasured
        # both raise above, so this is never the reassuring value standing in for one
        # of them -- it is "there was no other interface to reach".
        "other_interface_reachable": elsewhere,
    }


def _probe_connect(
    worktree: Path,
    granted: int | None,
    target: int,
    host: str = "127.0.0.1",
    *,
    network: str = NETWORK_LOOPBACK,
    unix: Path | None = None,
) -> bool | None:
    """True/False if a confined interpreter did/did not reach `host:target`, None if it
    could not be launched at all -- a distinction every caller above depends on.

    The probe says which of the three happened, rather than the caller inferring it
    from the streams. Inferring it was wrong: a non-empty stderr was read as a refused
    connection, and stderr is also what a sandbox binary writes when it declines to
    apply its profile and exits before the interpreter starts. That turned "nothing
    ran" into "the port was properly denied", which is the one answer this function
    exists to keep apart from the other two.
    """
    connect = (
        f"    socket.create_connection(({host!r}, {target}), 3).close()"
        if unix is None
        else f"    s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect({str(unix)!r})"
    )
    probe = "\n".join(
        (
            "import socket",
            "print('RAN', flush=True)",
            "try:",
            connect,
            "except OSError:",
            "    print('REFUSED')",
            "else:",
            "    print('REACHED')",
        )
    )
    # `sys.executable` because it is an absolute path that exists: resolving `python3`
    # through `PATH` inside a security probe is the habit this module just removed.
    argv = wrap([sys.executable, "-c", probe], worktree, network=network, port=granted)
    completed = _run_confined(argv, timeout=20)
    if completed is None or "RAN" not in completed.stdout:
        return None
    if "REACHED" in completed.stdout:
        return True
    return False if "REFUSED" in completed.stdout else None


@lru_cache(maxsize=1)
def _internet_verified() -> str | None:
    """None if the internet bound holds on this host, else why it does not."""
    try:
        verify_internet()
    except SandboxUnavailable as missing:
        return str(missing)
    except SandboxEscape as escape:
        logger.error("sandbox internet bound did not hold: %s", escape)
        return str(escape)
    logger.info("sandbox internet bound verified: %s", mechanism())
    return None


def verify_internet() -> dict[str, object]:
    """Prove the internet grant reaches an IP listener and not a Unix socket.

    Both listeners are on this host and both are reachable from outside the sandbox, so
    the profile is the only thing separating them, and the check needs no network: a
    confined process that reaches a TCP listener on loopback has an IP grant, and one
    that reaches the Unix socket beside it has more than that. The socket is outside
    the worktree, where a real one -- an agent's, a daemon's -- would be.

    Name resolution is not probed. It is a Unix socket to the system resolver on macOS
    and the grant names that one path; a host where it stopped working fails loudly
    at the first request rather than quietly granting something extra.

    Raises `SandboxUnavailable` where the mode cannot be expressed here, and
    `SandboxEscape` where it is expressed and not honoured.
    """
    if (why := _internet_expressible_reason()) is not None:
        raise SandboxUnavailable(why)

    with tempfile.TemporaryDirectory(dir=_scratch_root()) as scratch:
        root = Path(scratch).resolve()
        tree = root / "worktree"
        tree.mkdir()
        # Short on purpose: a Unix socket path is capped near 104 bytes on macOS.
        socket_path = root / "s"
        with (
            socket.create_server(("127.0.0.1", 0)) as listener,
            socket.socket(socket.AF_UNIX) as unix,
        ):
            unix.bind(str(socket_path))
            unix.listen()
            reached = _probe_connect(
                tree, None, listener.getsockname()[1], network=NETWORK_INTERNET
            )
            other = _probe_connect(tree, None, 0, network=NETWORK_INTERNET, unix=socket_path)

    if reached is not True:
        raise SandboxEscape(
            f"{mechanism()} did not let a confined process reach a TCP listener under "
            "network='internet': either the clause is not being honoured or the probe "
            "never ran, and neither leaves anything proved about what it denies"
        )
    if other is not False:
        raise SandboxEscape(
            f"{mechanism()} let a confined process connect to a Unix socket outside its "
            "worktree under network='internet'"
            if other
            else f"{mechanism()} could not be tested against a Unix socket: the probe "
            "never reported, and an unmeasured bound is not one"
        )
    return {
        "mechanism": mechanism(),
        "ip_reachable": True,
        "unix_socket_denied": True,
    }


def _sbpl_string(value: str) -> str:
    """A path as an SBPL string literal.

    Quotes and backslashes are legal in POSIX filenames and would otherwise end the
    literal early -- which does not fail loudly, it produces a profile that means
    something else.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _macos_profile(worktree: Path, network: str, port: int | None = None) -> str:
    """An SBPL profile: deny everything, then hand back the minimum.

    SBPL is last-match-wins, so the credential denials come after the broad read
    allowance and override it. Reordering these two blocks silently unhides `~/.ssh`.

    The signal grant is scoped to the process group, which is the narrowest filter that
    leaves the thing worth allowing. Measured on this host: unscoped, a confined process
    sent SIGTERM to an unconfined sibling and killed it. `(target self)` stops that and
    also stops a build tool killing the compiler it started, which is not a sandbox
    anybody can use. `(target pgrp)` keeps the second and stops the first -- as long as
    the confined process is in its own group, which is the caller's job and why `wrap`
    says so; launched into the caller's group, the grant covers the caller.
    """
    lines = [
        "(version 1)",
        "(deny default)",
        # Without exec and fork the wrapped command cannot start a child, and every
        # runtime of interest is a process that starts children.
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target pgrp))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow file-read*)",
        f"(allow file-write* (subpath {_sbpl_string(str(worktree))}))",
        "(allow file-write-data",
        *[f"  (literal {_sbpl_string(device)})" for device in _WRITABLE_DEVICES],
        ")",
        "(allow file-ioctl (literal \"/dev/tty\") (literal \"/dev/dtracehelper\"))",
    ]
    # Unconditionally, whether the path is there now or not. The existence filter this
    # used to carry made the deny list a snapshot taken when the argv was built: a
    # `gh auth login` in another terminal created `~/.config/gh` that a worker already
    # running was then free to read. A `subpath` naming nothing denies nothing and
    # costs nothing -- confirmed against `sandbox-exec`, which loads such a profile
    # without complaint -- so there is no reason to look first.
    lines += [
        f"(deny file-read* (subpath {_sbpl_string(str(path))}))"
        for path in _secret_dirs()
    ]
    # `literal` and not `subpath`, because these are files. A `subpath` would also
    # deny anything whose name merely starts the same way.
    lines += [
        f"(deny file-read* (literal {_sbpl_string(str(path))}))"
        for path in _secret_files()
    ]
    if network == NETWORK_LOOPBACK:
        # One port on this host, and the port is why this clause is narrow now.
        # Measured, because the comment that used to be here was wrong: `localhost` in
        # an SBPL address filter means this *machine*, not this interface -- with
        # `localhost:*` a confined process reached a listener bound to this host's LAN
        # address, which is not loopback by any reading. What `localhost` does exclude
        # is the rest of the internet, and that half was confirmed too.
        #
        # The port is the bound that survives measurement, so it is the one used. A
        # literal `127.0.0.1:port` is not an option: SBPL rejects the profile outright.
        # What remains reachable is another service on this host answering on the same
        # port on a different interface, which is as far as this mechanism goes.
        lines.append(f'(allow network-outbound (remote ip "localhost:{int(port)}"))')
    if network == NETWORK_INTERNET:
        # `(remote ip)` and not a bare `network-outbound`, which also covers connecting
        # to a Unix socket -- measured: a confined process reached a listener at a path
        # outside the worktree through it. The one socket named is the system resolver,
        # without which no hostname resolves (EAI_NONAME, measured) and the grant
        # reaches only IP literals.
        lines.append(
            '(allow network-outbound (remote ip) (literal "/private/var/run/mDNSResponder"))'
        )
    return "\n".join(lines)


# Directories holding the host's IPC endpoints. A Unix domain socket is reachable
# through a pathname and does not care about network namespaces, so `--unshare-net`
# leaves every one of these connectable: a docker socket is arbitrary host writes, a
# session bus is most of the desktop, an agent socket signs with keys the worker was
# never allowed to read. Masked rather than denied because bubblewrap masks.
#
# `/tmp` is in the list for the same reason and one more: nothing may write there, so
# a worker that finds it empty is finding the truth. `TMPDIR` is inside the worktree.
_SOCKET_DIRS = ("/run", "/var/run", "/tmp")


def _linux_args(worktree: Path, network: str = NETWORK_NONE, binary: str = "bwrap") -> list[str]:
    """bubblewrap arguments. Later binds win, so the read-only root comes first.

    `network` is a parameter it does not branch on, and that is the point: bubblewrap
    has one network switch and no way to grant loopback alone, so the only mode it can
    serve is `none`. That was true before and correct only because
    `_loopback_expressible_reason` refuses Linux three functions away -- a dependency
    with nothing recording it, in a function that would have silently served an
    unbounded namespace the day that guard moved. Now it is checked here.
    """
    if network != NETWORK_NONE:
        why = (
            _loopback_expressible_reason()
            if network == NETWORK_LOOPBACK
            else _internet_expressible_reason()
        )
        raise SandboxUnavailable(why or f"bubblewrap cannot serve network={network!r}")
    masks: list[str] = []
    for path in _maskable(worktree):
        # An empty tmpfs over the directory: it still exists, so tooling that stats it
        # does not crash, and it holds nothing.
        masks += ["--tmpfs", str(path)]
    for path in _existing(_secret_files(), files=True):
        # A file cannot be tmpfs-mounted, so it is covered by an empty one. Read-only,
        # so the mask cannot be mistaken for somewhere to put things.
        masks += ["--ro-bind", "/dev/null", str(path)]
    args = [
        binary,
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        # Before the worktree, not after. `/tmp` is one of the masked directories and
        # a worktree is routinely made inside it, so a tmpfs laid down afterwards
        # covers the tree it was supposed to be protecting -- an empty, read-only
        # worktree, which reads as the sandbox denying its own grant.
        *masks,
        "--bind", str(worktree), str(worktree),
        # A new session detaches the controlling terminal, which closes the TIOCSTI
        # route back out of the sandbox into the operator's shell.
        "--new-session",
        "--unshare-pid",
        # SysV shared memory and POSIX message queues are neither filesystem nor
        # network, so nothing else here touches them.
        "--unshare-ipc",
        "--unshare-net",
        "--",
    ]
    return args


def _secret_dirs() -> list[Path]:
    """Every credential directory this module hides, whether it is there or not."""
    home = Path.home()
    return [home / name for name in SECRET_DIRS]


def _secret_files() -> list[Path]:
    """Every credential file this module hides, whether it is there or not."""
    home = Path.home()
    return [home / name for name in SECRET_FILES]


def _socket_paths() -> list[Path]:
    """The host IPC directories that exist here."""
    return _existing([Path(name) for name in _SOCKET_DIRS])


def _maskable(worktree: Path) -> list[Path]:
    """The directories to cover, or `SandboxUnavailable` if the worktree is under one.

    A tmpfs is mounted in the same namespace the next `--bind` reads its source from,
    so masking a directory the worktree lives under hides the worktree from its own
    bind -- and `/tmp`, which holds `ssh-agent`'s sockets, is exactly where a worktree
    can end up. Order does not rescue it in either direction: masking first hides the
    source, masking last covers the destination.

    So the launch is refused. What it used to do was keep the tree and drop the mask
    with a warning, which left every sibling of the worktree reachable: a Unix socket
    is addressed by pathname and does not care that `--unshare-net` took the network
    away, so an agent socket in `/tmp` signs with keys the worker was never allowed to
    read, and a docker socket is arbitrary writes to the host. A warning is not a
    bound. Refusing costs nothing that is actually in use -- worktrees are made under
    the service's own root, and this module's verification builds its tree under the
    home directory for exactly this reason (see `_scratch_root`) -- so the case this
    fires on is a worktree somewhere nobody intended, which is the case to stop.

    Resolved and deduplicated because `/var/run` is a symlink to `/run` on every
    mainstream distribution, and bubblewrap is asked for one mount, not two.
    """
    seen: dict[Path, None] = {}
    for path in (*_existing(_secret_dirs()), *_socket_paths()):
        real = path.resolve()
        if worktree.is_relative_to(real):
            raise SandboxUnavailable(
                f"the worktree {worktree} is inside {real}, which this sandbox has to "
                "cover with an empty filesystem to hide the host's sockets and "
                "credentials. Covering it would hide the worktree from its own mount, "
                "and skipping it would leave those sockets reachable from inside, so "
                "neither is confinement. Put the worktree somewhere else."
            )
        seen.setdefault(real)
    return list(seen)


def _existing(paths: list[Path], *, files: bool = False) -> list[Path]:
    """`paths` that are there now.

    bubblewrap only, and it is a real narrowing of what the Linux side promises: a
    mount needs a mountpoint, and naming an absent one makes `bwrap` refuse to start,
    which would take the sandbox out entirely on any host missing one of these. So the
    Linux deny list is a snapshot taken when the argv is built, and a credential
    directory created after that is readable for the life of that process. macOS has no
    such limit and takes the whole list unconditionally.
    """
    return [path for path in paths if (path.is_file() if files else path.is_dir())]
