"""The sandbox, tested by trying to get out of it.

Two halves, and the split is deliberate. The argv builders are pure functions and are
asserted on every platform, because a profile that stops naming the worktree is a
silent hole and CI should catch it wherever CI runs. The containment itself is proved
by execution, and those tests skip where the host has no mechanism -- a skip says "not
proved here", which is the honest answer, whereas asserting the builder's output and
calling that a security test would say "proved" about something nobody ran.

The escape probes check both the exit code and the filesystem. A mechanism that
reports failure while still writing the file has denied nothing, and only the second
assertion notices.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from orchestrator_mcp.code import sandbox


@pytest.fixture(autouse=True)
def fresh_detection():
    """`mechanism()` is cached for the process; tests that fake a platform must not
    leak that answer into the next test.

    The clear is captured up front because a test may have replaced the whole function
    by teardown, and the cache that needs emptying belongs to the original.
    """
    clears = (sandbox.mechanism.cache_clear, sandbox.holds.cache_clear)
    for clear in clears:
        clear()
    yield
    for clear in clears:
        clear()


@pytest.fixture
def worktree(unmasked_root):
    """Under the home directory, not `tmp_path`. See `unmasked_root`: `/tmp` is one of
    the directories the Linux side masks, and a worktree beneath a mask is refused."""
    tree = unmasked_root / "worktree"
    tree.mkdir()
    return tree


confined = pytest.mark.skipif(
    not sandbox.holds(),
    # `holds()` and not `mechanism()`: an installed mechanism that cannot start a child
    # is the host where every denial assertion below passes without denying anything,
    # because a wrapper that never ran leaves the same nonzero status and the same
    # absent file as one that refused. Costs nothing extra -- the reason string already
    # runs the probe. The markers below are the second half: this one proves the
    # launcher worked once, for the probe's own command.
    reason=f"no process sandbox holds on this host: {sandbox.unavailable_reason()}",
)

loopback_confined = pytest.mark.skipif(
    not sandbox.holds() or sandbox.loopback_unavailable_reason() is not None,
    # Both, because they are different questions and Linux answers them differently:
    # bubblewrap contains filesystem writes perfectly well -- `holds()` is true there --
    # and refuses loopback by design, so `@confined` alone let these run on a Linux host
    # with bubblewrap installed and fail on the `SandboxUnavailable` the module raises
    # on purpose. Never seen, because CI's Linux runner has no `bwrap`.
    reason=f"no loopback bound holds on this host: {sandbox.loopback_unavailable_reason()}",
)

internet_confined = pytest.mark.skipif(
    not sandbox.holds() or sandbox.internet_unavailable_reason() is not None,
    reason=f"no internet bound holds on this host: {sandbox.internet_unavailable_reason()}",
)

STARTED = "ORCHESTRATOR-PROBE-STARTED"


def started(script: str) -> str:
    """`script`, announcing itself on stderr first.

    stderr because the assertions that read stdout must stay readable, and first
    because the script is expected to fail -- `sh` exits with the status of the last
    command, so the marker cannot be what the exit code describes.
    """
    return f"echo {STARTED} >&2; {script}"


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, **kwargs)


# --- detection and refusal --------------------------------------------------


def test_an_unsupported_platform_has_no_mechanism(monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "FreeBSD")
    sandbox.mechanism.cache_clear()
    assert sandbox.mechanism() is None
    assert "FreeBSD" in sandbox.unavailable_reason()


def test_a_missing_tool_is_named_rather_than_generically_unsupported(monkeypatch):
    """An operator one `apt install bubblewrap` from a working setup should be told
    that, not handed "not supported"."""
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    sandbox.mechanism.cache_clear()
    assert sandbox.mechanism() is None
    assert "bwrap" in sandbox.unavailable_reason()


def test_a_relative_executable_on_path_is_refused_rather_than_resolved(monkeypatch, worktree):
    """`shutil.which` returns what it found, and what it found is relative when the
    `PATH` entry was: `PATH=tools` yields `tools/bwrap`.

    Which is the hole the caching was supposed to close, by another door. This module
    resolves it against its own working directory; `opencode_write` launches with
    `cwd=worktree`, so the same string names a different file there -- one inside the
    tree the worker writes to. Refused rather than absolutised, because absolutising
    picks one of the two directories and there is no reason to believe it is the one
    that will be used at launch.
    """
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "tools/sandbox-exec")
    sandbox.holds.cache_clear()
    assert sandbox._binary() is None
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.wrap(["/bin/echo", "hi"], worktree)


def test_the_confining_binary_may_not_live_inside_the_worktree(monkeypatch, worktree):
    """The other end of the same problem, and the one an absolute path does not fix: a
    launcher the confined process can overwrite confines nothing after the first run.
    Both profiles grant writes to the worktree -- that is the whole grant -- so this is
    the one place a sandbox binary must not be."""
    planted = worktree / "sandbox-exec"
    planted.write_text("#!/bin/sh\nexec \"$@\"\n")
    planted.chmod(0o755)
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: str(planted))
    sandbox.holds.cache_clear()
    with pytest.raises(sandbox.SandboxUnavailable, match="inside the worktree"):
        sandbox.wrap(["/bin/echo", "hi"], worktree)


def test_wrapping_without_a_mechanism_refuses_rather_than_returning_the_command(
    monkeypatch, worktree
):
    """The failure mode this guards against is a caller receiving its own argv back and
    running it unconfined, believing it was wrapped."""
    monkeypatch.setattr(sandbox, "mechanism", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.wrap(["/bin/echo", "hi"], worktree)


def test_verifying_without_a_mechanism_refuses(monkeypatch):
    monkeypatch.setattr(sandbox, "mechanism", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.verify()


def test_an_empty_command_is_refused(worktree):
    with pytest.raises(ValueError):
        sandbox.wrap([], worktree)


def test_a_worktree_that_does_not_exist_is_refused(tmp_path):
    """A profile naming a missing path grants nothing, so the failure would otherwise
    surface as an inexplicable denial inside the worker."""
    with pytest.raises(ValueError, match="worktree does not exist"):
        sandbox.wrap(["/bin/echo", "hi"], tmp_path / "nope")


# --- what the builders emit -------------------------------------------------


def test_the_macos_profile_denies_by_default_and_grants_only_the_worktree(worktree):
    profile = sandbox._macos_profile(worktree.resolve(), sandbox.NETWORK_NONE)
    assert profile.startswith("(version 1)\n(deny default)")
    assert f'(allow file-write* (subpath "{worktree.resolve()}"))' in profile
    assert "network-outbound" not in profile


def test_the_macos_credential_denials_come_after_the_broad_read_allowance(worktree, tmp_path, monkeypatch):
    """SBPL is last-match-wins. Reordering these two blocks unhides `~/.ssh` without
    changing a single character of either one."""
    secret = tmp_path / "home" / ".ssh"
    secret.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "_secret_dirs", lambda: [secret])
    monkeypatch.setattr(sandbox, "_secret_files", list)
    profile = sandbox._macos_profile(worktree.resolve(), sandbox.NETWORK_NONE)
    assert profile.index("(allow file-read*)") < profile.index(f'(deny file-read* (subpath "{secret}"))')


def test_loopback_on_macos_grants_the_address_and_not_the_internet(worktree):
    """The whole point of the mode. A worker has to reach a model served on this
    machine, and there is deliberately no mode that would grant it more."""
    profile = sandbox._macos_profile(worktree.resolve(), sandbox.NETWORK_LOOPBACK, 11434)
    assert '(allow network-outbound (remote ip "localhost:11434"))' in profile
    assert "(allow network*)" not in profile


def test_the_macos_loopback_grant_names_a_port_and_never_a_wildcard(worktree):
    """`localhost:*` was the clause this module shipped, and a comment called it the
    loopback set. Measured against a socket it is not: a confined process reached a
    listener bound to this machine's LAN address through it. `localhost` bounds the
    host, so the port is the only half of the clause that bounds anything, and a
    wildcard there hands the worker every service the machine is running."""
    profile = sandbox._macos_profile(worktree.resolve(), sandbox.NETWORK_LOOPBACK, 11434)
    assert "localhost:*" not in profile
    assert "11434" in profile


def test_loopback_without_a_port_is_refused_rather_than_widened(worktree):
    """There is no sensible default here. Guessing one would either break the worker or
    grant more than the caller asked for, and the second failure is silent."""
    with pytest.raises(ValueError, match="port"):
        sandbox.wrap(["/bin/echo", "hi"], worktree, network=sandbox.NETWORK_LOOPBACK)


def test_a_port_without_loopback_is_refused_rather_than_ignored(worktree):
    """A caller passing a port has a destination in mind. Accepting it under
    `network='none'` would confine the worker away from that destination and leave the
    argument looking like it had been honoured."""
    with pytest.raises(ValueError, match="port"):
        sandbox.wrap(["/bin/echo", "hi"], worktree, port=11434)


def test_internet_on_macos_grants_ip_and_the_resolver_and_no_other_socket(worktree):
    """A bare `network-outbound` also covers connecting to a Unix socket -- measured,
    it reached a listener outside the worktree -- so the grant is `(remote ip)` plus
    the one path name resolution needs."""
    profile = sandbox._macos_profile(worktree.resolve(), sandbox.NETWORK_INTERNET)
    assert (
        '(allow network-outbound (remote ip) (literal "/private/var/run/mDNSResponder"))'
        in profile
    )
    assert "(allow network-outbound)" not in profile
    assert "(allow network*)" not in profile


def test_the_internet_grant_is_never_implied_by_another_mode(worktree):
    for network, port in ((sandbox.NETWORK_NONE, None), (sandbox.NETWORK_LOOPBACK, 11434)):
        assert "(remote ip)" not in sandbox._macos_profile(worktree.resolve(), network, port)


def test_a_port_with_internet_is_refused_rather_than_read_as_a_bound(worktree):
    """`internet` grants every port. A caller passing one believes it asked for less."""
    with pytest.raises(ValueError, match="port"):
        sandbox.wrap(["/bin/echo", "hi"], worktree, network=sandbox.NETWORK_INTERNET, port=443)


def test_bubblewrap_refuses_internet_rather_than_sharing_the_namespace(worktree, monkeypatch):
    """Dropping `--unshare-net` would be the one-line version, and it hands the worker
    the host's abstract Unix sockets, which have no pathname to mask."""
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.LINUX)
    assert "abstract Unix sockets" in sandbox.internet_unavailable_reason()
    with pytest.raises(sandbox.SandboxUnavailable, match="abstract Unix sockets"):
        sandbox.wrap(["/bin/echo", "hi"], worktree, network=sandbox.NETWORK_INTERNET)
    with pytest.raises(sandbox.SandboxUnavailable, match="abstract Unix sockets"):
        sandbox._linux_args(worktree.resolve(), sandbox.NETWORK_INTERNET)


def test_an_unknown_network_mode_is_refused_rather_than_treated_as_none(worktree):
    """Silently falling back to `none` would make a typo look like a policy, and the
    worker's failure to reach its model look like the model being down."""
    with pytest.raises(ValueError, match="network must be one of"):
        sandbox.wrap(["/bin/echo", "hi"], worktree, network="all")


def test_sbpl_strings_escape_quotes_that_would_end_the_literal_early():
    assert sandbox._sbpl_string('/tmp/a"b\\c') == '"/tmp/a\\"b\\\\c"'


def test_the_linux_binds_put_the_writable_worktree_after_the_read_only_root(worktree):
    """bubblewrap applies binds in order, so a worktree bind placed before `--ro-bind /
    /` would be overwritten by it and the tree would be read-only."""
    args = sandbox._linux_args(worktree.resolve())
    assert args[0].endswith("bwrap")
    assert args.index("--ro-bind") < args.index("--bind")
    assert args[args.index("--bind") + 1] == str(worktree.resolve())
    assert "--unshare-net" in args
    assert args[-1] == "--"


def test_loopback_is_refused_on_linux_rather_than_approximated(worktree, monkeypatch):
    """bubblewrap offers one switch: a fresh network namespace, whose loopback is not
    the host's, or the host's namespace, which is the whole internet. Returning the
    second under the name `loopback` would be this module lying about what it granted,
    so the host is declared unable to contain the worker instead."""
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.LINUX)
    with pytest.raises(sandbox.SandboxUnavailable, match="cannot grant loopback alone"):
        sandbox.wrap(["/bin/echo", "hi"], worktree, network=sandbox.NETWORK_LOOPBACK, port=11434)


def test_every_mechanism_granted_loopback_has_something_to_grant_it_with(worktree):
    """Membership and the clause that justifies it are two statements of one fact, and
    nothing checked they agreed. A mechanism added to the allow list without a loopback
    clause would be handed the grant by a set literal."""
    for granted in sandbox._GRANTS_LOOPBACK:
        assert granted == sandbox.MACOS, "add the argv comparison for a new mechanism"
        off = sandbox._macos_profile(worktree, sandbox.NETWORK_NONE)
        on = sandbox._macos_profile(worktree, sandbox.NETWORK_LOOPBACK, 11434)
        assert on != off
        assert "network" in on.removeprefix(off)


def test_a_mechanism_nobody_has_tested_for_loopback_is_refused_it(worktree, monkeypatch):
    """The rule that matters is the default, not bubblewrap's entry in it. A mechanism
    added later grants loopback only once someone has run it and put it in
    `_GRANTS_LOOPBACK`; until then it is refused, the way every other unknown in this
    module is. The reverse default would hand the grant out by omission."""
    monkeypatch.setattr(sandbox, "mechanism", lambda: "some-future-jail")
    assert "some-future-jail" in sandbox.loopback_unavailable_reason()
    with pytest.raises(sandbox.SandboxUnavailable, match="not containable on this host"):
        sandbox.wrap(["/bin/echo", "hi"], worktree, network=sandbox.NETWORK_LOOPBACK, port=11434)


def test_a_host_with_no_mechanism_is_refused_loopback_for_the_first_reason(
    worktree, monkeypatch
):
    """Two questions, and the blunter one answers first: a host that cannot confine
    anything is not a host with a loopback problem.

    Asserted on what the caller sees rather than on the two functions returning the
    same expression -- that version would keep passing if both returned nothing.
    """
    monkeypatch.setattr(sandbox, "mechanism", lambda: None)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "FreeBSD")
    reason = sandbox.loopback_unavailable_reason()
    assert "FreeBSD" in reason
    with pytest.raises(sandbox.SandboxUnavailable, match="FreeBSD"):
        sandbox.wrap(["/bin/echo", "hi"], worktree, network=sandbox.NETWORK_LOOPBACK, port=11434)


def test_only_existing_credential_directories_are_hidden(tmp_path, monkeypatch):
    """`--tmpfs` on a path that is not there makes bubblewrap refuse to start, which
    would take the sandbox out on any host missing one of these directories."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: home))
    assert sandbox._existing(sandbox._secret_dirs()) == [home / ".ssh"]


# --- the containment itself -------------------------------------------------


@confined
def test_a_write_inside_the_worktree_succeeds(worktree):
    target = worktree / "allowed.txt"
    assert run(sandbox.wrap(["/bin/sh", "-c", f'printf ok > "{target}"'], worktree)).returncode == 0
    assert target.read_text() == "ok"


@confined
def test_a_write_outside_the_worktree_is_denied_by_the_kernel(worktree, unmasked_root):
    """The claim `registry.py` makes about `isolated_write`, executed rather than
    argued: the model does not decline, the kernel refuses.

    The target sits beside the worktree rather than in `tmp_path`, so that the refusal
    is the profile refusing on both platforms. Under `/tmp` the Linux write would land
    in the mask's tmpfs and succeed -- invisibly to the host, so the file assertion
    still passes while the exit status quietly stops meaning anything."""
    target = unmasked_root / "escaped.txt"
    denied = run(sandbox.wrap(["/bin/sh", "-c", started(f'printf pwned > "{target}"')], worktree))
    assert STARTED in denied.stderr, "the confined shell never ran; nothing was denied"
    assert denied.returncode != 0
    assert not target.exists()


@confined
def test_the_inherited_temp_directory_is_not_writable(worktree):
    """`/tmp` is excluded on purpose -- the same choice `CodexWriteAdapter` makes --
    so that a step's result is reproducible from the diff."""
    inherited = os.environ.get("TMPDIR", "/tmp")
    target = f"{inherited.rstrip('/')}/orchestrator-escape-probe"
    denied = run(sandbox.wrap(["/bin/sh", "-c", started(f'printf pwned > "{target}"')], worktree))
    assert STARTED in denied.stderr, "the confined shell never ran; nothing was denied"
    # Survival, not exit status, and the module docstring says why: sandbox-exec denies
    # the syscall, while bubblewrap covers `/tmp` with a tmpfs the process may write to
    # and whose contents go with the namespace. "No write here survives" is the promise
    # both keep; "every write here is refused" is only the first one's.
    assert not os.path.exists(target)


@confined
def test_env_overrides_give_the_worker_a_writable_temp_dir_inside_the_tree(worktree):
    env = {**os.environ, **sandbox.env_overrides(worktree)}
    argv = sandbox.wrap(["/bin/sh", "-c", 'printf ok > "$TMPDIR/probe"'], worktree)
    assert run(argv, env=env).returncode == 0
    assert (worktree / ".orchestrator-tmp" / "probe").read_text() == "ok"


@confined
def test_a_child_process_is_confined_too(worktree, unmasked_root):
    """Confinement that a subshell escapes is not confinement: the runtimes this exists
    for spawn build tools, and those spawn more."""
    target = unmasked_root / "child-escaped.txt"
    # The marker is the child's, not the parent's: a parent that launches and a child
    # that does not would otherwise read exactly like a confined child.
    inner = started(f'printf pwned > "{target}"')
    argv = sandbox.wrap(["/bin/sh", "-c", f"/bin/sh -c '{inner}'"], worktree)
    denied = run(argv)
    assert STARTED in denied.stderr, "the child shell never ran; nothing was denied"
    assert denied.returncode != 0
    assert not target.exists()


@confined
def test_the_network_is_off_unless_asked_for(worktree):
    """One reachable listener, unreachable from inside. Not "the network is off" --
    that is the argv assertion's claim, made against `--unshare-net` and the profile's
    absent network clause, and this test does not widen it. What this proves is the
    half those cannot: that the denial happens to a real socket at runtime rather than
    only in the arguments.

    The listener is what makes the result mean anything. The two mechanisms deny
    differently -- sandbox-exec refuses the syscall, bubblewrap drops the process into
    an empty network namespace where the host's loopback is simply not there -- so the
    error text is not common ground. A port nothing is listening on is not common
    ground either: it is refused on an unsandboxed host too. Only a socket that an
    unconfined process does connect to separates containment from coincidence, which is
    why the unconfined connection is asserted first.
    """
    with socket.create_server(("127.0.0.1", 0)) as listener:
        probe = (
            "import socket, sys; "
            # Same marker, same reason as the filesystem probes: a python3 that never
            # starts inside the sandbox refuses the connection by not attempting it.
            f"print('{STARTED}', file=sys.stderr); "
            f"socket.create_connection(('127.0.0.1', {listener.getsockname()[1]}), 2)"
        )
        assert run(["/usr/bin/env", "python3", "-c", probe]).returncode == 0
        denied = run(sandbox.wrap(["/usr/bin/env", "python3", "-c", probe], worktree))
    assert STARTED in denied.stderr, "the confined interpreter never ran; nothing was denied"
    assert denied.returncode != 0


@confined
def test_credentials_are_not_readable(worktree, secret_dir):
    """Asserted on the listing, not the exit code, because the exit code describes only
    one of the two mechanisms: sandbox-exec denies the read outright, while bubblewrap
    masks the directory with an empty tmpfs that `ls` walks happily and finds nothing
    in. What both promise is the same -- no entry of the directory is visible.

    Three things make an empty listing mean that, and each of them replaced a way this
    test used to pass while proving nothing. `-A`, because credential directories are
    mostly dotfiles and plain `ls` omits them, so the unconfined listing was empty too.
    The unconfined control, because entries have to be there to be hidden. And the
    marker, because a wrapper that fails to launch also prints nothing -- an empty
    listing from a command that never ran is not containment.
    """
    listing = f'ls -A "{secret_dir}"; echo LISTED'
    assert ".hidden_token" in run(["/bin/sh", "-c", listing]).stdout
    listed = run(sandbox.wrap(["/bin/sh", "-c", listing], worktree))
    assert listed.stdout.split() == ["LISTED"]


def test_a_mechanism_that_ran_nothing_did_not_hold(monkeypatch):
    """The probe that fails to launch is the dangerous one. `_probe_write` counts a
    launch failure as a denial, which is correct for the write outside the worktree and
    catastrophic for the one inside it: a profile the host refuses to apply fails both,
    creates no file outside, and without this check reads as a mechanism that held.

    `holds()` is what the capability gate asks, so the assertion goes all the way to
    it rather than stopping at the exception."""
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    monkeypatch.setattr(sandbox, "_probe_write", lambda target, worktree: (False, "bwrap: no"))
    with pytest.raises(sandbox.SandboxEscape, match="did not permit a write inside"):
        sandbox.verify()
    sandbox.holds.cache_clear()
    assert sandbox.holds() is False
    # And the operator is told which half failed. The sentence used to be fixed --
    # "did not deny a write outside the worktree" -- which is the opposite of what
    # happened here: nothing was denied because nothing ran, and an operator reading
    # it would go looking at the profile instead of the launcher.
    reason = sandbox.unavailable_reason()
    assert "did not permit a write inside" in reason
    assert "did not deny a write outside" not in reason


def test_the_executable_is_the_one_the_mechanism_asked_for(monkeypatch):
    """`_binary` used to be `lru_cache(maxsize=1)` over a function taking no arguments
    while reading the patchable `mechanism()`, so the first answer of the process
    outlived every later pin. Measured on a real host: `mechanism()` returning
    `bubblewrap` while `_binary()` handed back `/usr/bin/sandbox-exec` -- one
    mechanism's argv built around the other's launcher.

    Harmless where nothing moves, which is production. Not harmless in this file,
    where pinning a platform is how the profiles get asserted at all, and where it is
    why a Linux runner passes tests that assert macOS argv.
    """
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: f"/hosts/{name}")
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    assert sandbox._binary() == "/hosts/sandbox-exec"
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.LINUX)
    # Deliberately no `cache_clear`: the point is that the cache is keyed on the answer
    # it depends on, so a caller that never knew to clear it still gets the right one.
    assert sandbox._binary() == "/hosts/bwrap"


def test_an_executable_that_is_a_symlink_into_the_worktree_is_refused(monkeypatch, worktree):
    """The lexical version of `wrap`'s check compares pathnames, and a pathname outside
    the tree can name a file inside it. `/opt/bin/bwrap` pointing at
    `<worktree>/tools/bwrap` passes that comparison and hands the worker the file that
    confines it -- the same hole as a planted launcher, through the one door a string
    comparison cannot see. So the path is resolved before it is cached or compared.
    """
    planted = worktree / "sandbox-exec"
    planted.write_text('#!/bin/sh\nexec "$@"\n')
    planted.chmod(0o755)
    link = worktree.parent / "sandbox-exec-link"
    link.symlink_to(planted)
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: str(link))
    with pytest.raises(sandbox.SandboxUnavailable, match="inside the worktree"):
        sandbox.wrap(["/bin/echo", "hi"], worktree)


def test_an_escape_probe_that_never_ran_is_not_a_denial(monkeypatch):
    """The sibling of `test_a_mechanism_that_ran_nothing_did_not_hold`, and the half
    that read as containment for five rounds. The inside probe launching says nothing
    about the outside one -- they are two processes and the second can time out on its
    own -- and a launch that never happened leaves exactly the absent file a real
    denial leaves. `verify` used to read that as the bound holding.
    """
    def probes(target, worktree):
        if target.parent == worktree:
            return True, ""
        return None, "sandbox-exec: cannot start the confined interpreter"

    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    monkeypatch.setattr(sandbox, "_probe_write", probes)
    with pytest.raises(sandbox.SandboxEscape, match="escape probe never ran"):
        sandbox.verify()
    sandbox.holds.cache_clear()
    assert sandbox.holds() is False
    # And it says which probe, because "did not deny a write outside" would send the
    # operator to the profile when the launcher is what failed.
    assert "could not be tested for a write outside" in sandbox.unavailable_reason()


class _ExitingVictim:
    """Running when the probe is arranged, gone by the time the answer comes back."""

    pid = 4242424

    def __init__(self, *_args, **_kwargs) -> None:
        self._polls = 0

    def poll(self):
        self._polls += 1
        return None if self._polls == 1 else 0

    def kill(self) -> None:
        pass

    def wait(self) -> None:
        pass


def test_a_refusal_about_a_dead_victims_pid_is_not_a_signal_bound(monkeypatch, worktree):
    """`EPERM` is only evidence about the process that was still there to refuse. A
    victim that exited mid-probe leaves its PID to be reused by something this user
    may genuinely not be allowed to signal -- init, another user's daemon -- and the
    refusal that comes back is about that, not about the bound being measured.

    `GONE` already carried this condition and `DENIED` did not, so the reassuring
    answer was the one accepted without it.
    """
    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    monkeypatch.setattr(sandbox.subprocess, "Popen", _ExitingVictim)
    monkeypatch.setattr(sandbox, "wrap", lambda command, worktree, **_: list(command))
    monkeypatch.setattr(
        sandbox,
        "_run_confined",
        lambda argv, timeout=30: subprocess.CompletedProcess(argv, 0, "RAN\nDENIED\n", ""),
    )
    complaint = sandbox._probe_signal(worktree)
    assert complaint is not None
    assert "the victim exited during the probe" in complaint


@confined
def test_verify_reports_the_mechanism_that_held():
    report = sandbox.verify()
    assert report["mechanism"] == sandbox.mechanism()
    assert report["write_inside_worktree"] is True
    assert report["write_outside_worktree_denied"] is True


@confined
def test_verify_raises_when_the_escape_probe_succeeds(monkeypatch):
    """The guard against a future edit that widens the profile and leaves every other
    test passing: `verify` must fail loudly rather than report a sandbox that isn't."""
    monkeypatch.setattr(sandbox, "wrap", lambda command, worktree, **_: list(command))
    with pytest.raises(sandbox.SandboxEscape):
        sandbox.verify()


@confined
def test_verify_raises_when_the_launcher_never_starts(monkeypatch):
    """The other direction, and the one that fails silently: a wrapper that cannot run
    denies every probe by refusing to run it, which is indistinguishable from a working
    sandbox to anything that only checks that the escape did not happen."""
    monkeypatch.setattr(sandbox, "wrap", lambda command, worktree, **_: ["/usr/bin/false"])
    with pytest.raises(sandbox.SandboxEscape, match="never ran"):
        sandbox.verify()


@confined
def test_the_signal_probe_reports_a_kill_it_was_allowed_to_send(monkeypatch, worktree):
    """Unwrapped, the signal goes through, and the probe has to say so.

    The direction two earlier versions got wrong, both silently. One read a nonzero
    `kill` as EPERM when a victim that had already exited makes it ESRCH; the other
    read a *successful* kill as a denial whenever the victim outlived a two-second
    wait. Neither could have failed this test, because neither was looking at the
    syscall's error.
    """
    monkeypatch.setattr(sandbox, "wrap", lambda command, worktree, **_: list(command))
    assert "signal a process outside its group" in sandbox._probe_signal(worktree)


@confined
def test_verify_reports_the_signal_scope_it_actually_tested():
    """A report key nobody sets is a claim nobody made. This one is here because the
    unscoped grant it replaced was invisible to every filesystem probe in the file."""
    assert sandbox.verify()["signal_outside_group_denied"] is True


@loopback_confined
def test_the_loopback_grant_reaches_its_port_and_stops_there():
    """The clause is `(remote ip "localhost:PORT")`, and only the second half of that
    bounds anything: measured against a socket, `localhost` turned out to mean this
    machine rather than this interface -- a confined process reached a listener on the
    host's LAN address through `localhost:*`. So the port is the grant, and this runs
    it against two real listeners to prove the narrowing is honoured here.

    The third key is the interface dimension. Not pinned to a value, because the two
    it can hold here are both facts about the machine rather than about the grant:
    False where a second interface exists and the port was denied on it, None where the
    host has no second address to reach. True is the one answer that cannot arrive --
    a host that reaches the port on another interface raises, and `loopback_confined`
    skips this test rather than reaching the assertions."""
    report = sandbox.verify_loopback()
    assert report["mechanism"] == sandbox.mechanism()
    assert report["granted_port_reachable"] is True
    assert report["other_port_denied"] is True
    assert report["other_interface_reachable"] in (False, None)


@loopback_confined
def test_verify_loopback_raises_when_the_port_bounds_nothing(monkeypatch):
    """Identity `wrap`, which is what a mechanism granting the whole host looks like
    from here: the granted port answers, and so does the one that was withheld."""
    monkeypatch.setattr(sandbox, "wrap", lambda command, worktree, **_: list(command))
    with pytest.raises(sandbox.SandboxEscape, match="not bounded by port"):
        sandbox.verify_loopback()


@loopback_confined
def test_verify_loopback_raises_when_the_probe_never_connects(monkeypatch):
    """A grant nobody could talk through is not a bound that held. Reported as a
    failure because the alternative is a `loopback` mode that silently reaches nothing
    and a worker whose model looks like it is down."""
    monkeypatch.setattr(sandbox, "wrap", lambda command, worktree, **_: ["/usr/bin/false"])
    with pytest.raises(sandbox.SandboxEscape, match="did not let a confined process reach"):
        sandbox.verify_loopback()


OTHER_INTERFACE = "192.0.2.7"  # TEST-NET-1: reserved, so it is nobody's real address


def _interface_probed(monkeypatch, elsewhere: bool | None, binds: bool = True) -> None:
    """Arrange a host with a second interface, without needing one.

    The mechanism is pinned because Linux refuses this mode statically and would raise
    before any of it ran. The third listener's bind is redirected to loopback because
    the address above is deliberately unroutable -- what is under test is the decision
    made about the answer, not the socket that produced it.
    """
    real_server = sandbox.socket.create_server

    def server(address, **kwargs):
        host, _ = address
        if host == "127.0.0.1":
            return real_server(address, **kwargs)
        if not binds:
            raise OSError("address already in use")
        return real_server(("127.0.0.1", 0))

    def connect(worktree, granted, target, host="127.0.0.1"):
        return elsewhere if host != "127.0.0.1" else granted == target

    monkeypatch.setattr(sandbox, "mechanism", lambda: sandbox.MACOS)
    monkeypatch.setattr(sandbox, "_loopback_expressible_reason", lambda: None)
    monkeypatch.setattr(sandbox, "_other_local_address", lambda: OTHER_INTERFACE)
    monkeypatch.setattr(sandbox.socket, "create_server", server)
    monkeypatch.setattr(sandbox, "_probe_connect", connect)


def test_a_grant_that_reaches_the_granted_port_elsewhere_is_an_escape(monkeypatch):
    """`localhost` in an SBPL address filter bounds the machine, not the interface, so
    the mode's own name overstates what it hands the worker. Measured, logged and
    allowed until now; refused from here, because a caller asking for `loopback` is not
    asking for every service answering on that port from this host's other addresses.

    The cost is stated rather than hidden: `sandbox-exec` cannot express an interface,
    so this refuses `isolated_write` on any macOS host with a routable address.
    """
    _interface_probed(monkeypatch, elsewhere=True)
    with pytest.raises(sandbox.SandboxEscape, match="as well as on loopback"):
        sandbox.verify_loopback()


def test_an_interface_that_could_not_be_listened_on_is_not_a_bound(monkeypatch):
    """Unmeasured and clean are different facts, and the one that used to be reported
    was the reassuring one -- a bind that failed left `None`, which the caller returned
    beside two answers that had been proved."""
    _interface_probed(monkeypatch, elsewhere=None, binds=False)
    with pytest.raises(sandbox.SandboxEscape, match="unknown is not a bound"):
        sandbox.verify_loopback()


def test_an_interface_that_denies_the_port_is_the_bound_holding(monkeypatch):
    """The control on the two above: the refusal is about what was measured, not about
    the dimension being looked at."""
    _interface_probed(monkeypatch, elsewhere=False)
    assert sandbox.verify_loopback()["other_interface_reachable"] is False


def test_a_host_with_only_loopback_has_no_second_interface_to_reach(monkeypatch):
    """The one case where the dimension is absent rather than proved. Nothing is bound,
    nothing is probed, and `None` here means there was no other address -- not that one
    went unmeasured, which now raises."""
    _interface_probed(monkeypatch, elsewhere=None)
    monkeypatch.setattr(sandbox, "_other_local_address", lambda: None)
    assert sandbox.verify_loopback()["other_interface_reachable"] is None


def test_a_worktree_holding_a_hard_link_is_refused_rather_than_confined(worktree, unmasked_root):
    """Both mechanisms bound pathnames; a hard link is a second pathname for one inode.
    A link planted inside the tree before the worker starts is a granted name on an
    ungranted file, and writing through it is inside the worktree by every rule either
    profile can state. Nothing in the sandbox can see that, so `wrap` refuses."""
    # Beside the worktree, so the two are on one filesystem: a hard link cannot cross
    # devices, and `tmp_path` is a separate tmpfs from the home directory on Linux.
    outside = unmasked_root / "precious.txt"
    outside.write_text("original")
    (worktree / "innocent.txt").hardlink_to(outside)
    with pytest.raises(sandbox.SandboxUnavailable, match="more than one name"):
        sandbox.wrap(["/bin/echo", "hi"], worktree)


def test_an_ordinary_worktree_is_not_mistaken_for_a_linked_one(worktree):
    """The other half: a scan that refused everything would be a sandbox nobody can
    use, and `git worktree add` -- the way these trees are made -- shares objects by
    reference rather than by link, so the ordinary case has to pass."""
    (worktree / "file.txt").write_text("ok")
    (worktree / "sub").mkdir()
    (worktree / "sub" / "link").symlink_to(worktree / "file.txt")
    assert sandbox.wrap(["/bin/echo", "hi"], worktree)[-2:] == ["/bin/echo", "hi"]


def test_the_linux_args_mask_the_directories_that_hold_agent_sockets(worktree):
    """`SSH_AUTH_SOCK` is not in the child's environment, but the socket it names is
    still on the filesystem and its path is guessable. A read-only bind of `/` leaves
    it connectable, and an agent socket is the private key by another route."""
    args = sandbox._linux_args(worktree.resolve())
    masked = {args[i + 1] for i, flag in enumerate(args) if flag == "--tmpfs"}
    # The list, so a directory dropped from it is caught here rather than on a Linux
    # host nobody is looking at; and `/tmp`, which exists wherever this runs, so the
    # emitting half is exercised too and the assertion is not only about a constant.
    assert {"/run", "/var/run", "/tmp"} <= set(sandbox._SOCKET_DIRS)
    # Resolved: `/var/run` is a symlink to `/run` on Linux and `/tmp` to `/private/tmp`
    # here, and bubblewrap is asked for the real directory once rather than twice.
    assert str(Path("/tmp").resolve()) in masked


def test_a_worktree_inside_a_masked_directory_is_refused(tmp_path, monkeypatch):
    """The mask and the bind read from the same mount namespace, so covering a
    directory the worktree lives under hides the tree from its own bind -- and `/tmp`,
    which is masked for `ssh-agent`'s sockets, is exactly where a worktree can end up.
    Neither order escapes it: mask first and the source is gone, mask last and the
    destination is.

    So the launch is refused. What this replaced kept the tree and dropped the mask
    with a warning, which is not a bound: a Unix socket is reached by pathname and
    `--unshare-net` does not touch it, so every agent and docker socket beside the
    worktree stayed connectable from inside a sandbox that reported itself as holding.
    """
    tree = tmp_path / "worktree"
    tree.mkdir()
    monkeypatch.setattr(sandbox, "_SOCKET_DIRS", (str(tmp_path), "/tmp"))
    monkeypatch.setattr(sandbox, "_secret_dirs", list)
    monkeypatch.setattr(sandbox, "_secret_files", list)
    with pytest.raises(sandbox.SandboxUnavailable, match="Put the worktree somewhere else"):
        sandbox._linux_args(tree.resolve())


def test_the_directories_that_do_not_hold_the_worktree_are_still_masked(worktree, monkeypatch):
    """The other half of the refusal above: only a directory containing the tree is a
    problem, and the rest of the list has to keep being covered."""
    monkeypatch.setattr(sandbox, "_secret_dirs", list)
    monkeypatch.setattr(sandbox, "_secret_files", list)
    args = sandbox._linux_args(worktree.resolve())
    masked = {args[i + 1] for i, flag in enumerate(args) if flag == "--tmpfs"}
    assert str(Path("/tmp").resolve()) in masked


def test_the_linux_masks_are_laid_down_before_the_worktree_bind(worktree):
    """Order, again, and the direction the first version got wrong. `/tmp` is masked
    and a worktree is routinely made inside it, so a tmpfs applied after the bind
    covers the tree it was meant to protect: an empty read-only worktree, which every
    probe reports as the sandbox denying its own grant rather than as a bad mount."""
    args = sandbox._linux_args(worktree.resolve())
    bind = args.index("--bind")
    assert all(i < bind for i, flag in enumerate(args) if flag == "--tmpfs")


def test_the_linux_args_replace_credential_files_rather_than_leaving_them(worktree, tmp_path, monkeypatch):
    """`--tmpfs` needs a directory. The single-file credentials get `/dev/null` bound
    over them instead, which is the same promise by the only mechanism that fits."""
    secret = tmp_path / "home" / ".netrc"
    secret.parent.mkdir(parents=True)
    secret.write_text("machine example.com login root password hunter2\n")
    monkeypatch.setattr(sandbox, "_secret_files", lambda: [secret])
    args = sandbox._linux_args(worktree.resolve())
    assert args[args.index(str(secret)) - 1] == "/dev/null"




# --- network="internet", executed ---------------------------------------------


@internet_confined
def test_the_internet_bound_verifies_on_this_host():
    assert sandbox.verify_internet() == {
        "mechanism": sandbox.mechanism(),
        "ip_reachable": True,
        "unix_socket_denied": True,
    }


@internet_confined
def test_internet_keeps_the_write_bound(worktree, unmasked_root):
    """The network is the only thing this mode changes."""
    target = unmasked_root / "escaped.txt"
    argv = sandbox.wrap(
        ["/bin/sh", "-c", started(f'printf pwned > "{target}"')],
        worktree,
        network=sandbox.NETWORK_INTERNET,
    )
    denied = run(argv)
    assert STARTED in denied.stderr, "the confined shell never ran; nothing was denied"
    assert denied.returncode != 0
    assert not target.exists()


@confined
def test_without_internet_a_tcp_listener_is_unreachable(worktree):
    """The control for the grant above: the same listener, refused under `none`."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
        assert sandbox._probe_connect(worktree, None, port, network=sandbox.NETWORK_NONE) is False
