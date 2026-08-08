"""Consulting an opencode CLI.

Every claim here was checked against the installed binary (1.18.15), and five of the
obvious assumptions did not survive that:

`OPENCODE_CONFIG` **merges**, it does not replace. A user's global config still
contributes its MCP servers and its `instructions` through it, so the isolated
environment also moves `XDG_CONFIG_HOME` to an empty directory, and nothing is
carried back across. Worse, an `opencode.json` or `opencode.jsonc` anywhere up the
ancestor chain of the working directory is merged too, and it beats
`OPENCODE_CONFIG` on permissions -- a parent directory saying `bash: allow` wins.
Neither `mcp` nor `instructions` can be unset from below, so the only defence is to
refuse to run under such an ancestor.

Carrying nothing across is what keeps this runtime like the other three: a
`provider` block is the only place a *local* endpoint is ever named, so dropping it
leaves the child with opencode's own catalogue, every entry of it remote. Verified
against the binary -- under this configuration `--model ollama/qwen2.5:7b` fails
with `ProviderModelNotFoundError` and the local server is never contacted. See
`_ISOLATED_CONFIG`.

`opencode run` exits 0 whatever happens, including a hard provider failure, so the
exit code decides nothing and the event stream decides everything.

There is no `--json-schema` equivalent. The contract travels in the prompt, which a
small model does get wrong -- hence `ENVELOPE` and the one repair turn in `_answer`.

The model that answered is not in the stream at all. `opencode export <session>`
reports it, so verification costs one extra local command rather than being given
up on.

A session remembers the directory it ran in and resolves that path again on resume,
which rules out the per-run temporary directory the Codex adapter uses: deleting it
turns every follow-up turn into `NotFound: FileSystem.realPath`. See `_scratch`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...contract import Usage
from ..config import AgentConfig
from ..contract import ConsultationContent, SourceMode
from ..errors import ConsultErrorCode
from ..prompts import CompiledPrompt
from .base import (
    AdapterError,
    AdapterResult,
    AgentStatus,
    ProcessResult,
    check_model,
    child_env,
    parse_content,
    resolve_command,
    run_process,
)

# How long a preflight gets. It is one local command; a slow one is a broken one.
PREFLIGHT_TIMEOUT_S = 30.0

# Read after the answer, from a session that has already finished, so it is not
# waiting on a model.
EXPORT_TIMEOUT_S = 30.0

# What a consultation is allowed to consist of. Anything else -- a tool call, a
# file edit, a subagent -- is the runtime acting rather than answering, and fails
# closed. Named parts rather than named tools on purpose: no tool part was ever
# observed in this stream, so a check that looks for one by name would pass every
# run whether or not a tool had run.
ANSWER_PARTS = frozenset({"step-start", "text", "step-finish"})

# Emitted by a model that reasons before it answers. Not an action and not an answer,
# so neither a refusal nor part of the text. `"reasoning"` is a part type this binary
# knows -- the literal sits beside `"tool"` and `"step-finish"` in it -- though none of
# the models reachable on this account emit one, so this is a latent break closed
# rather than an observed one repaired.
IGNORED_PARTS = frozenset({"reasoning"})

# Merged in from every directory above the working one, and neither `mcp` nor
# `instructions` can be removed by a config lower down.
ANCESTOR_CONFIGS = ("opencode.json", "opencode.jsonc")

# The other three runtimes are handed a JSON schema by flag -- `--json-schema` on
# antigravity, structured outputs on the other two -- and this one has nothing of the
# sort, so the shape has to be stated in words. Concretely, because the failures
# measured against a 7B model were not omitted keys but wrong ones: `"sources":
# ["model"]` where each entry is an object. The shared contract names the five fields;
# what it cannot name, having no runtime to be specific for, is their shape.
#
# Appended after the payload rather than folded into the system half: this is a
# formatting instruction, and the last thing read is the part a small model still has
# hold of when it starts writing.
ENVELOPE = """

Reply with one JSON object and nothing else -- no prose before or after it, no
markdown fence. Exactly these five keys, all of them required, with empty arrays
where there is nothing to list:

{"answer": "...", "assumptions": ["..."], "uncertainties": ["..."],
 "follow_up_questions": ["..."],
 "sources": [{"title": "...", "locator": "...", "source_type": "model"}]}

Each entry in `sources` is an object with exactly those three keys, and `source_type`
is one of "document", "web", "model". Every other array holds plain strings."""


def repair(complaint: str) -> str:
    """The complaint, then the shape again.

    A function rather than a `.format` template, because the shape it quotes is full of
    braces and `str.format` would read every one of them as a placeholder.

    The task itself is not repeated: this goes into the session that just answered, and
    resending it would cost the whole context a second time.
    """
    return f"That reply did not match the required schema: {complaint}\n" + ENVELOPE


class OpenCodeCliAdapter:
    runtime = "opencode"

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def connect_command(self, agent: AgentConfig) -> str:
        return f"{agent.command} auth login"

    async def preflight(self, agent: AgentConfig) -> AgentStatus:
        try:
            command = resolve_command(agent)
        except AdapterError:
            return AgentStatus(agent.agent_id, installed=False, authenticated=False,
                               detail=f"`{agent.command}` is not on PATH")

        # `opencode models` rather than an auth command: signing in is not the same
        # act for every provider -- an API key, an OAuth round trip, or a gateway that
        # asks for nothing -- and a check written for one of those reads as permanently
        # logged out on another. A model that is listed is a model that can be asked.
        # Under the same isolation a consultation gets -- the same code, so that
        # readiness answers the question that will actually be asked. Listed under the
        # user's own config instead, this would call a locally-served model ready and
        # then fail at the run, where that model does not exist. Its own directory
        # rather than an agent's, because opencode merges the working directory's
        # config: asking inside one would resolve our own last answer back to us.
        probe, env = _isolated("probe")
        result = await run_process(
            [command, "models"],
            None,
            timeout_s=PREFLIGHT_TIMEOUT_S,
            env=env,
            cwd=str(probe),
        )
        available = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if agent.model in available:
            return AgentStatus(agent.agent_id, installed=True, authenticated=True)
        return AgentStatus(
            agent.agent_id,
            installed=True,
            authenticated=False,
            detail=f"`{agent.model}` is not among the models opencode offers",
        )

    async def start(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        session_id: str | None = None,
    ) -> AdapterResult:
        # `session_id` is ignored: opencode mints its own ids and has no flag to
        # accept ours, so the native id comes back out of the stream instead.
        return await self._run(agent, prompt, source_mode, resume=None)

    async def resume(
        self,
        agent: AgentConfig,
        native_session_id: str,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
    ) -> AdapterResult:
        return await self._run(agent, prompt, source_mode, resume=native_session_id)

    # --- invocation ---------------------------------------------------------

    async def _run(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        resume: str | None,
    ) -> AdapterResult:
        if source_mode is SourceMode.WEB:
            # Unconditionally, not `if not agent.web_search`: this adapter does not
            # offer web mode at all, so an operator who sets `web_search: true` on an
            # opencode agent must still be refused rather than served a model-mode
            # answer under a web-mode contract.
            raise AdapterError(
                ConsultErrorCode.WEB_SEARCH_UNAVAILABLE,
                f"agent `{agent.agent_id}` runs on the opencode runtime, which this "
                "server does not offer web mode on",
            )

        command = resolve_command(agent)
        # One directory per agent, not one shared by all of them. The configuration
        # written here is the same for every agent today, and resting on that is what
        # made a shared directory look safe last time it was argued: one release that
        # gives an agent something of its own to write turns a settled question back
        # into a race between a consultation starting and reading its config.
        #
        #
        # Separate from the one readiness is asked in, because opencode merges the
        # working directory's config: asking there would resolve the copy this adapter
        # wrote last time. See `_isolated`.
        root, env = _isolated(_session_dir(agent.agent_id))
        scratch = str(root)

        base = [
            command,
            "run",
            "--pure",  # no external plugins
            "--format",
            "json",
            # The error events carry an opaque reference and nothing else; the
            # cause only appears on stderr, and only when logs are asked for.
            "--print-logs",
            "--log-level",
            "ERROR",
            "--model",
            agent.model,
        ]

        # opencode has no `--system-prompt`, so the protocol contract travels with
        # the payload. It stays first, which is what keeps a task from reading as
        # one. Over stdin: no argv ceiling, and no shell to quote for.
        content, native, result, usage = await self._answer(base, prompt, scratch, env, resume)
        reported = await self._reported_model(command, native, scratch, env)

        return AdapterResult(
            content=content,
            native_session_id=native,
            model_used=check_model(agent, reported),
            model_verified=reported is not None,
            raw_output=result.stdout,
            usage=usage,
        )

    async def _answer(
        self,
        base: list[str],
        prompt: CompiledPrompt,
        cwd: str,
        env: dict[str, str],
        resume: str | None,
    ) -> tuple[ConsultationContent, str, ProcessResult, Usage]:
        """The consultation, and one repair turn if the contract came back broken.

        With no schema flag the envelope rests on instruction alone, and a small model
        drops a required key often enough that failing the whole consultation for it
        would waste the context that was already paid for. One follow-up in the same
        session costs a few hundred characters. One, not a loop: a model that cannot
        produce the shape twice is not going to on the third ask.
        """
        argv = base + ["--session", resume] if resume else base
        result = await run_process(
            argv, prompt.full_text + ENVELOPE, self.timeout_s, env=env, cwd=cwd
        )
        text, native, usage = _read_stream(result, resume)
        try:
            return parse_content(text), native, result, usage
        except AdapterError as exc:
            if exc.code is not ConsultErrorCode.PROTOCOL_VALIDATION_FAILED:
                raise
            # Copied out: the name bound by `except` is deleted when the block ends,
            # and what went wrong is the whole content of the repair request.
            complaint = str(exc)[:300]

        # Into the session that just answered, so the task itself does not have to be
        # sent a second time -- on a document-mode consultation that would be the whole
        # context again.
        retry = await run_process(
            base + ["--session", native],
            repair(complaint),
            self.timeout_s,
            env=env,
            cwd=cwd,
        )
        text, native, retry_usage = _read_stream(retry, native)
        return parse_content(text), native, retry, _add(usage, retry_usage)

    async def _reported_model(
        self, command: str, native: str, cwd: str, env: dict[str, str]
    ) -> str | None:
        """Which model actually answered, from the session opencode just stored.

        Not in the event stream -- `opencode export` is the only place it appears. A
        failure here is not the consultation's failure: the answer is already in hand,
        and `model_verified=False` says exactly what happened.
        """
        try:
            result = await run_process(
                [command, "export", native], None, EXPORT_TIMEOUT_S, env=env, cwd=cwd
            )
            info = json.loads(result.stdout).get("info", {})
            model = info.get("model") or {}
            provider, name = model.get("providerID"), model.get("id")
        except (AdapterError, ValueError, AttributeError):
            return None
        if not isinstance(provider, str) or not isinstance(name, str) or not (provider and name):
            return None
        return f"{provider}/{name}"


# --- the isolated configuration ---------------------------------------------

# `"*": "deny"` rather than the thirteen keys spelled out: opencode expands the
# wildcard to whatever its own permission set is, so a key added by a later release
# is denied too. An unlisted permission defaults to *ask*, and asking in a
# non-interactive run means hanging until the timeout kills the group.
#
# No `provider` key, and that absence is load-bearing rather than an omission. A
# provider block is the only way an endpoint outside opencode's own catalogue is ever
# named -- which is to say the only way a *local* one is -- so writing none leaves the
# child able to reach hosted services and nothing else. This runtime consults a
# subscription the user already holds, exactly as the codex and claude ones do; it
# does not run a model on anybody's machine. The cost is real and is documented: a
# self-hosted or locally served model cannot be consulted through this server at all.
_ISOLATED_CONFIG: dict[str, Any] = {
    "permission": {"*": "deny"},
    "mcp": {},
    "plugin": [],
    "share": "disabled",
    "autoupdate": False,
    "instructions": [],
}

# Constant, so every agent's configuration is the same bytes and serializing it per
# consultation would be work done again for the same answer.
_ISOLATED_JSON = json.dumps(_ISOLATED_CONFIG)


def _scratch() -> Path:
    """The root the per-agent working directories and the probe directory sit in.

    Not a `TemporaryDirectory`. opencode records the directory a session ran in and
    resolves it again on `--session`, so a per-run directory means every resume fails
    with `NotFound: FileSystem.realPath` on a path this server deleted itself. A stable
    directory per agent is the smallest thing that keeps a follow-up turn working, and
    it stays as uninteresting to read as the temporary one was.

    Under `$HOME` rather than the temporary directory, because `_refuse_inherited_config`
    can only check the ancestor chain *before* opencode reads it. Beneath a shared
    `/tmp` -- world-writable on most Linux, whatever the sticky bit implies -- another
    user can create `/tmp/opencode.json` inside that window and have its permissions
    outrank ours. `_refuse_writable_ancestors` is what turns "under `$HOME`" from an
    expectation into a checked fact; without it this docstring claimed an invariant
    nothing established, and `mkdir(mode=0o700, exist_ok=True)` on an already-permissive
    `~/.orchestrator-mcp` says nothing, since the mode applies only on creation.
    """
    root = Path.home() / ".orchestrator-mcp" / "opencode"
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _refuse_writable_ancestors(root.parent)
    root.mkdir(mode=0o700, exist_ok=True)

    _refuse_shared(root)

    # An opencode config directly here would be an *ancestor* of the directory the
    # consultation runs in, which `_refuse_inherited_config` rightly refuses to run
    # under -- and since this directory outlives the run now, one left by an earlier
    # version of this adapter would wedge every consultation from then on. Nothing but
    # this adapter can write here, so a stray one is ours to clear.
    for name in ANCESTOR_CONFIGS:
        stale = root / name
        if stale.is_file():
            stale.unlink()
    return root


def _refuse_shared(path: Path) -> None:
    """Refuse a directory that is not this user's own, private, and not a symlink.

    Applied to the scratch root and to every working directory under it. The root's
    own check said nothing about its children, and `mkdir(mode=0o700, exist_ok=True)`
    on an existing entry says nothing either -- the mode applies only on creation, and
    `exist_ok` is satisfied by a symlink pointing at a directory somewhere else
    entirely. Creating one inside a `0700` root takes this account, so this is not a
    guard against another user; it is what makes "the config we wrote is the config it
    reads" a checked fact rather than an argument about who could have been here.
    """
    info = path.lstat()  # not `stat`: a symlink must fail here, not be followed
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"`{path}` is not a private directory belonging to this user, so it is not "
            "somewhere a consultation can safely be run from",
        )


def _refuse_writable_ancestors(path: Path) -> None:
    """Refuse to run under a chain any other account can rewrite.

    The scratch root's own `0700` check happens once, and every use after it names the
    directory by path again. That gap is only harmless while no other account can rename
    a component of the chain out from under it -- given a group- or world-writable
    ancestor, one can, and the tree the child ends up reading its configuration from is
    then not the tree that was checked.

    *Write* is the permission that matters, not read: renaming an entry needs write on
    the directory holding it. So `/` and `/Users` at `0755` pass, and only `0002` or
    `0020` fails. Ownership by root passes too -- an account that already has root has
    no need of a rename race.

    Resolved first, so a symlink's own mode is never what gets judged: `lrwxrwxrwx` is
    every symlink on Linux, and reading that as world-writable would refuse every
    machine with a `/var` link in the way.
    """
    resolved = path.resolve()
    for parent in [resolved, *resolved.parents]:
        info = parent.lstat()
        if info.st_mode & 0o022 or info.st_uid not in (os.getuid(), 0):
            raise AdapterError(
                ConsultErrorCode.TRANSPORT_ERROR,
                f"`{parent}` can be written by an account other than this one, so "
                f"nothing beneath it -- `{path}` included -- can be trusted to still be "
                "the directory this server checked",
            )


def _session_dir(agent_id: str) -> str:
    """A private directory name for one agent.

    Hashed rather than used as-is: an agent id is a mapping key in the user's YAML and
    nothing validates its characters, so a `/` or a `..` in one would place the
    consultation somewhere other than under the scratch root. The name only has to be
    stable and distinct, which a digest is.
    """
    return hashlib.sha256(agent_id.encode()).hexdigest()[:16]


def _isolated(name: str) -> tuple[Path, dict[str, str]]:
    """A working directory, and the environment that makes opencode read only our config.

    One function with two callers rather than one each, because readiness and the
    consultation have to be isolated *identically* -- a check answered under different
    configuration is an answer to a different question. They had diverged: readiness
    redirected the config home and nothing else, so a project config above it still
    merged into `opencode models`, and a model could be reported reachable that the
    consultation would then refuse to run under. Sharing the code is what keeps them
    from drifting apart again, rather than two call sites that happen to agree.
    """
    root = _scratch() / name
    root.mkdir(mode=0o700, exist_ok=True)
    _refuse_shared(root)
    _refuse_inherited_config(root)

    # Both places, because they fail differently. As the project config it outranks
    # anything above it on permissions; as `OPENCODE_CONFIG` it applies even if a
    # future release stops reading the working directory.
    _write(root / "opencode.json", _ISOLATED_JSON)
    _write(root / "config.json", _ISOLATED_JSON)

    # Both spellings in one directory are *merged*, not resolved in favour of one --
    # verified against the binary, `.jsonc` landing after `.json` and so winning any
    # key it repeats. Only this adapter writes here and it writes neither, but the
    # cost of saying so is one unlink, and the cost of being wrong is a consultation
    # running under configuration this server did not write.
    stray = root / "opencode.jsonc"
    if stray.is_file():
        stray.unlink()

    env = child_env({
        # `OPENCODE_CONFIG` merges rather than replaces, so the user's global config
        # has to be out of reach as well, not merely outranked.
        "OPENCODE_CONFIG": str(root / "config.json"),
        "XDG_CONFIG_HOME": str(_empty_xdg()),
    })
    # `OPENCODE_DATA_DIR` is deliberately left alone: the saved credentials live under
    # it, and relocating it would turn every consultation on a keyed provider into a
    # login prompt.
    return root, env


def _empty_xdg() -> Path:
    """Where `XDG_CONFIG_HOME` points for every child this adapter starts.

    `OPENCODE_CONFIG` merges rather than replaces, so the user's global config has to
    be somewhere opencode cannot find rather than merely outranked. One directory for
    all of them, and this adapter never writes into it.

    opencode does: on its first run under a fresh config home it leaves an
    `opencode/opencode.jsonc` holding a `$schema` line and nothing else, alongside a
    `.gitignore` and whatever it installs for the providers it carries. That stub is
    inert today -- it declares no permission, no `mcp`, no `instructions`.

    Which is why the config it leaves is cleared rather than trusted. This directory is
    reused across runs, and `_refuse_inherited_config` cannot see it: it is a redirected
    config home, not an ancestor of the working directory, so nothing else in this file
    would notice a global config appearing here. A release that started writing a real
    one would then merge it into every consultation, silently. Deleting is cheaper than
    inspecting and does not have to be revisited when the stub's contents change --
    opencode rewrites what it needs, and it needs nothing this adapter has not passed
    on the command line.
    """
    empty = _scratch() / "xdg"
    empty.mkdir(mode=0o700, exist_ok=True)
    _refuse_shared(empty)
    for name in ANCESTOR_CONFIGS:
        stale = empty / "opencode" / name
        if stale.is_file():
            stale.unlink()
    return empty


def _write(path: Path, text: str) -> None:
    """Replace a file's contents in one step, readable only by this user.

    An agent's scratch directory outlives its consultations, so two of that agent's
    turns starting together must not have one reading a configuration the other is
    halfway through writing.

    0600 rather than whatever the umask says. What it holds is a constant today and
    nothing here is secret, but this is the file that decides a consultation may run no
    tools, and a mode is cheaper to set now than to notice missing later.
    """
    # A pid alone named the same scratch file for both, so two turns of one agent
    # starting together in this process shared an inode and each truncated what the
    # other was writing -- the exact tearing the paragraph above says cannot happen.
    # `O_EXCL` is what makes the name this call's own; `O_NOFOLLOW` is then redundant
    # on it and kept for the leftover a crash could have left behind.
    scratch = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    descriptor = os.open(
        scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    # The mode argument applies only when `os.open` creates the file, so `fchmod` before
    # the write narrows a leftover rather than after it has already held the config.
    os.fchmod(descriptor, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def _refuse_inherited_config(cwd: Path) -> None:
    """Refuse to run under a directory tree that carries its own opencode config.

    opencode merges every `opencode.json` above the working directory, and a parent's
    permissions outrank ours -- one saying `bash: allow` re-enables the shell for a
    consultation that asked for no tools. `mcp` and `instructions` merge additively
    and cannot be unset from below at all. Nothing in the config can defend against
    this, so the check is on the directory instead, and it fails closed.
    """
    for parent in cwd.resolve().parents:
        for name in ANCESTOR_CONFIGS:
            if (parent / name).exists():
                raise AdapterError(
                    ConsultErrorCode.TRANSPORT_ERROR,
                    f"`{parent / name}` sits above the consultation's working directory, "
                    "and opencode would merge it -- refusing rather than running with "
                    "configuration this server did not write",
                )


# --- parsing ----------------------------------------------------------------


def _read_stream(result: ProcessResult, fallback_session: str | None) -> tuple[str, str, Usage]:
    """The answer text, the session to resume, and what the turn cost.

    The exit code is not consulted: `opencode run` returns 0 on a hard provider
    failure, so the stream is the only account of what happened.
    """
    text: list[str] = []
    native = fallback_session or ""
    # `None` until a `step-finish` says otherwise, so that seeding with a zero-cost
    # `Usage()` cannot make an unknown cost look like a known one once `_add` runs.
    usage: Usage | None = None

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            # Not every line of a stream is an event; a diagnostic that happens to land
            # on stdout is not a protocol failure.
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            # A line that opens like an event and does not parse is a broken stream, and
            # skipping it would be deciding what it said. The check below is what stands
            # between a tool call and a consultation that claims none happened.
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"the agent emitted a line that is not a JSON event: {exc}",
            ) from exc
        if not isinstance(event, dict):
            continue

        session = event.get("sessionID")
        if isinstance(session, str) and session:
            if native and session != native:
                # Every id used to overwrite the last, so a stream carrying two sessions
                # would hand back the id of whichever spoke last while the answer text
                # came from both. On a resume `native` starts as the session that was
                # asked, which makes this the check that the reply is to that question.
                raise AdapterError(
                    ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                    f"the agent answered under session `{session}` when the turn belongs "
                    f"to `{native}`",
                )
            native = session

        if event.get("type") == "error":
            detail = json.dumps(event.get("error"))[:300]
            raise AdapterError(
                ConsultErrorCode.AGENT_UNAVAILABLE,
                f"the agent reported a failure: {detail}; {result.stderr.strip()[:400]}",
            )

        part = event.get("part")
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in IGNORED_PARTS:
            # A model thinking out loud is not the runtime acting, and refusing it here
            # would fail a reasoning model's every consultation with a message accusing
            # it of running a tool. Ignored rather than appended: what a model thinks is
            # not what it answered, and `parse_content` reads the answer as JSON.
            continue
        if kind not in ANSWER_PARTS:
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"the agent tried to act rather than answer (emitted a `{kind}` part)",
            )
        if kind == "text":
            text.append(str(part.get("text") or ""))
        elif kind == "step-finish":
            step = _usage(part)
            usage = step if usage is None else _add(usage, step)

    if not native:
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR, "the agent returned no session id to resume"
        )
    if usage is None:
        # No `step-finish` at all. The exit code is deliberately not consulted, so
        # without this a stream cut off mid-answer -- a killed child, a dropped
        # connection, output past the cap -- returned its partial text as a finished
        # reply. The terminal part is the only thing in the stream that says the turn
        # ended, and three lines below this used to turn its absence into `Usage()`,
        # reporting an unknown cost as a known zero in the same breath.
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent's reply ended without a `step-finish`, so it is a fragment "
            f"rather than an answer (exit {result.returncode}): "
            f"{result.stderr.strip()[:400]}",
        )
    joined = "".join(text).strip()
    if not joined:
        # A stream that starts and finishes without a text part is what a refused tool
        # call looks like from out here. Empty is not an answer, and passing it to
        # `parse_content` would report it as bad JSON rather than as no reply at all.
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent produced no answer (exit {result.returncode}): "
            f"{result.stderr.strip()[:400]}",
        )
    return joined, native, usage


def _usage(part: dict) -> Usage:
    tokens = part.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    prompt_tokens = int(tokens.get("input") or 0)
    completion_tokens = int(tokens.get("output") or 0)
    # opencode's own total, which is not `input + output`: it counts `reasoning` and the
    # `cache` read and write as well, and a cached prompt is mostly cache. Deriving the
    # total here under-reported by the whole cached share -- 2231 against 439 on a turn
    # measured against the binary. Falls back to the sum only if the field is absent,
    # which is the old behaviour and still better than reporting nothing.
    total = tokens.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        total = prompt_tokens + completion_tokens
    cost = part.get("cost")
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


def _add(left: Usage, right: Usage) -> Usage:
    """One turn's cost plus the repair turn's, so the caller is billed the truth.

    Half a sum is not a total: if either side's cost is unknown the result is unknown,
    rather than the known half presented as though it covered both turns.
    """
    known = left.cost_usd is not None and right.cost_usd is not None
    return Usage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        cost_usd=left.cost_usd + right.cost_usd if known else None,
    )
