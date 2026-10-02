"""Consulting a GitHub Copilot CLI.

Every claim here was checked against the installed binary (1.0.89). GitHub's
documentation covers the flags and says nothing about the exit codes, the event stream
or which model answered.

The prompt goes over stdin with no `-p`. `-p -` takes the dash as the prompt, and with
no prompt at all the CLI exits 1 ("No prompt provided"). 180 KB over stdin works.

There is no read-only mode, so the lockdown is layered. `--available-tools` names one
tool that does not exist, which switches off all eighteen built-ins; the CLI logs
"Unknown tool name in the tool allowlist" and carries on. That behaviour is
undocumented, so it is not the only layer: `--allow-all-tools` is never passed (a tool
that survived would need an approval `--no-ask-user` refuses), `--deny-tool` names the
shell, write and url kinds, and `_read_stream` fails the turn on any `tool.*` event or any
tool request. A prompt asking for a file to be created created nothing, and the working
directory stayed empty in every run. A `.mcp.json`, a `.vscode/mcp.json` and a
`.github/mcp.json` planted in the working directory, or in the one above it, were neither
listed nor started; the working directory is a fresh empty one regardless. See `_run`.

What the allowlist does not stop is the CLI's own startup. A server in the home's
`mcp-config.json` was launched and connected on every run and on the readiness check, its
tool listed among the disabled ones ("Disabled tools: ... real-marker-touch_marker"), so
the model could not call it. `--disable-mcp-server` does stop the launch, takes a name and
no pattern, and so `_mcp_off` reads the names out of that file. A hook ran on
`sessionStart` and `userPromptSubmitted`, and no flag or variable stops that. The setting
`disableAllHooks` does. The CLI keeps the user's settings in `settings.json` and, on a run,
moves any it finds in `config.json` there, leaving that file to what it manages. Hooks in
either file ran without the setting; with it in `settings.json` none ran, and the request
was answered all the same. `_hooks_off` puts it there before every run and every
readiness check. The move goes over what `settings.json` holds, though: with the setting
written there and `"disableAllHooks": false` in `config.json`, both hooks ran (1.0.89,
2026-10-01), so `_hooks_off` refuses that `config.json` rather than run. Whether the
setting also covers what a plugin brings was not tried, and nothing
here looks at one. The home is for the login and the sessions; the README says to keep it
that way.

The exit code means something here: 1 for a model the account cannot use (`Model "X"
from --model flag is not available.`) and 1 when signed out (`No authentication
information found.`), each on stderr with nothing on stdout. A tool call that was
refused exits 0, which is why the stream is read as well.

Readiness costs no request. A one-character prompt with a model name nobody has fails
before anything is sent: "from --model flag is not available" when signed in, "No
authentication information found" when not. It does create a session, directory and lock,
before it looks at the model, so the check names its own and removes it (`_forget`).

Which model answers is the plan's decision. On a free plan every explicit `--model` was
refused and only `auto` was served, which routes each call to a model the stream then
names (`session.auto_mode_resolved`, and `model` on the answer). The model that answered
is always read back; `auto` is the one configured name that is not compared against it.

`--session-id <uuid>` creates the session or resumes it and does not say which: resuming
one that is gone silently starts a new one that remembers nothing. So `resume` looks for
the session's own directory first.

The stream carries no token counts. `--usage-output-file` is written when the CLI exits,
with totals for the whole session and `lastCallInputTokens` / `lastCallOutputTokens` for
the final model call -- which under this lockdown is the turn, one call each, as the
`model.call_finished` events confirm. There is no price anywhere, only premium requests,
so `cost_usd` stays unknown.

There is no `--json-schema`, so the contract travels in the prompt with one repair turn,
as on opencode. Web mode is not offered.

The macOS keychain login survives an isolated `COPILOT_HOME`. Elsewhere the token is a
file under it, which is why `connect_command` names the same home. The token variables
(`COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`) are not passed on, like every other
credential this server's children are not given; sign in with `login`.

Session state accumulates under that home, one directory per consultation -- a first turn
the CLI refuses leaves an empty one. Deleting a consultation removes its directory and
lock (`forget_sessions`), and so does the retention sweep, which deletes through the same
paths. Nothing removes a directory no consultation in the database names: it may be a turn
in flight, or belong to another database that shares this home. What else the CLI keeps in
the home is not looked at, but one thing was measured on 1.0.89: `session-store.db` holds a
copy of each prompt and answer, unmasked, and outlives the directory. Nothing here removes
it: `copilot sessions` can only import, and the file is the CLI's own.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ...contract import Usage
from ...spend import tallied
from ..config import AgentConfig
from ..contract import ConsultationContent, RequiredAction, SourceMode
from ..errors import ConsultErrorCode
from ..prompts import CompiledPrompt
from .base import (
    AdapterError,
    AdapterResult,
    AgentStatus,
    ProcessResult,
    _caveat,
    accounted,
    check_model,
    child_env,
    parse_content,
    resolve_command,
    run_process,
    usage_count,
)

# ponytail: the directory checks, the file write and the envelope are opencode's, imported
# from there. Move them to `base.py` if a third adapter wants them.
from .opencode_cli import (
    ENVELOPE,
    _refuse_shared,
    _refuse_writable_ancestors,
    _write,
    repair,
)

# How long a preflight gets. It never reaches a model.
PREFLIGHT_TIMEOUT_S = 30.0

# The configured name that leaves the choice of model to Copilot.
AUTO = "auto"

# Names nothing has. `--available-tools` disables every tool it does not list, so listing
# only this one disables all of them; the model name makes the readiness check fail
# before a request is sent.
_NO_TOOL = "orchestrator-no-such-tool"
_NO_MODEL = "orchestrator-preflight-no-such-model"

# All the CLI says about either failure, on stderr. The second is the whole sentence's
# tail rather than "is not available", which a service outage could say as well.
_SIGNED_OUT = "No authentication information found"
_MODEL_REFUSED = "from --model flag is not available"

# What makes a run an answer rather than an agent, before `--model`.
_FLAGS = (
    "-s",
    "--output-format", "json",
    "--no-ask-user",
    "--no-auto-update",
    "--no-custom-instructions",
    "--disable-builtin-mcps",
    "--no-remote",
    "--no-remote-export",
    "--no-experimental",
    "--stream", "off",  # one message rather than a run of deltas
    f"--available-tools={_NO_TOOL}",
    "--deny-tool=shell",
    "--deny-tool=write",
    "--deny-tool=url",
)

# What `raw_output` keeps of the stream. The rest is bulk: `user.message` restates the
# whole prompt twice, and the answer arrives with two encrypted blobs beside it.
_RECORDED = frozenset(
    {"session.info", "session.auto_mode_resolved", "model.call_finished", "assistant.message", "result"}
)
_OPAQUE = ("reasoningOpaque", "encryptedContent")


class CopilotCliAdapter:
    runtime = "copilot"

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def connect_command(self, agent: AgentConfig) -> str:
        # The home consultations run under, or on a host with no keychain the token lands
        # where they never look. Only text: nothing is created for the user to run this.
        home = shlex.quote(str(_root() / "home"))
        return f"COPILOT_HOME={home} {shlex.quote(agent.command)} login"

    async def preflight(self, agent: AgentConfig) -> AgentStatus:
        try:
            command = resolve_command(agent)
        except AdapterError:
            return AgentStatus(agent.agent_id, installed=False, authenticated=False,
                               detail=f"`{agent.command}` is not on PATH")

        # Under the isolation a consultation gets, so that "ready" answers the question
        # that will be asked. Nothing is spent: see the module docstring.
        root, env = _isolated()
        probe = str(uuid4())
        try:
            with tempfile.TemporaryDirectory(dir=root, prefix="probe-") as cwd:
                result = await run_process(
                    [command, *_FLAGS, *_mcp_off(Path(env["COPILOT_HOME"])),
                     "--model", _NO_MODEL, "--session-id", probe],
                    "x",
                    PREFLIGHT_TIMEOUT_S,
                    env=env,
                    cwd=cwd,
                )
        finally:
            _forget(Path(env["COPILOT_HOME"]), probe)
        if _SIGNED_OUT in result.stderr:
            return AgentStatus(agent.agent_id, installed=True, authenticated=False,
                               detail="not signed in to GitHub Copilot")
        # Exit 0 means it answered, which it can only do signed in.
        if _MODEL_REFUSED in result.stderr or result.returncode == 0:
            return AgentStatus(agent.agent_id, installed=True, authenticated=True)
        return AgentStatus(
            agent.agent_id,
            installed=True,
            authenticated=False,
            detail=f"`{agent.command}` gave an answer to the sign-in check that this server "
            f"does not recognise (exit {result.returncode})",
        )

    async def start(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        session_id: str | None = None,
    ) -> AdapterResult:
        # A UUID of our own choosing is how the session is named, so nothing is parsed
        # back out of the stream. Anything that is not one gets a fresh id.
        try:
            session = str(UUID(session_id or ""))
        except ValueError:
            session = str(uuid4())
        return await self._run(agent, prompt, source_mode, session, resume=False)

    async def resume(
        self,
        agent: AgentConfig,
        native_session_id: str,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
    ) -> AdapterResult:
        return await self._run(agent, prompt, source_mode, native_session_id, resume=True)

    # --- invocation ---------------------------------------------------------

    async def _run(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        source_mode: SourceMode,
        session: str,
        resume: bool,
    ) -> AdapterResult:
        if source_mode is SourceMode.WEB:
            # Unconditionally, not `if not agent.web_search`, as on opencode: an operator
            # who sets `web_search: true` here must be refused, not served a model-mode
            # answer under a web-mode contract.
            raise AdapterError(
                ConsultErrorCode.WEB_SEARCH_UNAVAILABLE,
                f"agent `{agent.agent_id}` runs on the copilot runtime, which this "
                "server does not offer web mode on",
            )

        command = resolve_command(agent)
        root, env = _isolated()
        if resume:
            try:
                session = str(UUID(session))
            except ValueError:
                session = ""
            if not session or not (Path(env["COPILOT_HOME"]) / "session-state" / session).is_dir():
                raise AdapterError(
                    ConsultErrorCode.SESSION_NOT_FOUND,
                    f"agent `{agent.agent_id}` has no Copilot session to continue: it was removed "
                    "from Copilot's own state, and resuming it would start an empty one",
                )

        # A directory of its own for every run, so it is empty by construction and gone
        # afterwards. The session does not remember it: a resume from a different one
        # answered from the earlier turn. The usage files sit beside it, not in it.
        with tempfile.TemporaryDirectory(dir=root, prefix="run-") as scratch:
            work = Path(scratch)
            (work / "cwd").mkdir(mode=0o700)
            content, reply, usage = await self._answer(agent, command, prompt, session, env, work)

        reported = reply.model
        return AdapterResult(
            content=content,
            native_session_id=session,
            # `auto` names no model, so there is nothing to compare the answer's against.
            model_used=(reported or agent.model) if agent.model == AUTO else check_model(agent, reported),
            model_verified=reported is not None,
            raw_output=reply.record,
            usage=usage,
        )

    async def _answer(
        self,
        agent: AgentConfig,
        command: str,
        prompt: CompiledPrompt,
        session: str,
        env: dict[str, str],
        work: Path,
    ) -> tuple[ConsultationContent, _Reply, Usage]:
        """The consultation, and one repair turn if the contract came back broken.

        With no schema flag the envelope rests on instruction alone, and a small model
        drops a required key often enough that failing the whole consultation would waste
        the context already paid for. One follow-up in the same session, not a loop.
        """
        reply, usage = await self._call(agent, command, prompt.full_text + ENVELOPE, session, env, work, 1)
        try:
            return parse_content(reply.text), reply, usage
        except AdapterError as exc:
            if exc.code is not ConsultErrorCode.PROTOCOL_VALIDATION_FAILED:
                raise
            # Copied out: the name bound by `except` is deleted when the block ends.
            complaint = str(exc)[:300]

        retry, retry_usage = await self._call(
            agent, command, repair(complaint), session, env, work, 2
        )
        # The repair's own model, and none if it named none: the first reply's model wrote
        # the broken answer, not this one, so lending its name would mark the answer that
        # is returned as verified against a call that did not produce it. Its events stay
        # in the record.
        joined = _Reply(retry.text, retry.model, retry.calls, reply.record + "\n" + retry.record)
        return parse_content(retry.text), joined, _add(usage, retry_usage)

    async def _call(
        self,
        agent: AgentConfig,
        command: str,
        text: str,
        session: str,
        env: dict[str, str],
        work: Path,
        number: int,
    ) -> tuple[_Reply, Usage]:
        report = work / f"usage-{number}.json"
        result = await run_process(
            [command, *_FLAGS, *_mcp_off(Path(env["COPILOT_HOME"])),
             "--model", agent.model, "--session-id", session,
             "--usage-output-file", str(report)],
            text,
            self.timeout_s,
            env=env,
            cwd=work / "cwd",
        )
        if result.returncode != 0:
            raise self._refusal(agent, result)
        reply = _read_stream(result, session)
        return reply, _usage(_load(report), reply.calls)

    def _refusal(self, agent: AgentConfig, result: ProcessResult) -> AdapterError:
        """A non-zero exit, said as what it was."""
        if _SIGNED_OUT in result.stderr:
            return AdapterError(
                ConsultErrorCode.CONNECTION_REQUIRED,
                f"agent `{agent.agent_id}` is not signed in to GitHub Copilot",
                RequiredAction(command=self.connect_command(agent)),
            )
        if _MODEL_REFUSED in result.stderr:
            return AdapterError(
                ConsultErrorCode.CONFIGURED_MODEL_UNAVAILABLE,
                f"agent `{agent.agent_id}` is configured for model `{agent.model}`, which "
                "GitHub Copilot does not offer on this account",
            )
        return AdapterError(
            ConsultErrorCode.AGENT_UNAVAILABLE,
            f"`{agent.command}` exited {result.returncode}: {result.stderr.strip()[:400]}",
        )


# --- the isolated state -----------------------------------------------------


def _root() -> Path:
    return Path.home() / ".orchestrator-mcp" / "copilot"


def _isolated() -> tuple[Path, dict[str, str]]:
    """The private root, and the environment that points Copilot's state inside it.

    One `COPILOT_HOME` for every agent, so there is one login rather than one each. It
    holds the CLI's config and session store, which is why nothing else may write it:
    the same reasoning as opencode's scratch root, and the same checks.

    Under `$HOME` rather than the temporary directory for the same reason as there:
    `_refuse_writable_ancestors` turns "nobody else can rename a component of this path"
    from an expectation into a checked fact.
    """
    root = _root()
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _refuse_writable_ancestors(root.parent)
    _private(root)
    home = _private(root / "home")
    _hooks_off(home)
    return root, child_env({"COPILOT_HOME": str(home)})


def _hooks_off(home: Path) -> None:
    """`disableAllHooks` on in the home's `settings.json`, keeping whatever else it holds.

    A hook in the home runs at the start of a session and on every prompt, and this
    setting is all that stops it: see the module docstring. It is written only when it is
    not already true, so a file that is right keeps its writer's layout. One that cannot
    be read as a JSON object is refused rather than overwritten. It is the operator's, and
    running the CLI over it would be running with whatever hooks it names.

    A `disableAllHooks` in `config.json` that is not true is refused as well: the CLI moves
    it into `settings.json` as it starts, over the one written here. Only an operator puts
    the key there, so the file is not rewritten.
    """
    if _turns_hooks_on(home / "config.json"):
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"`{home / 'config.json'}` sets `disableAllHooks` to something other than true, "
            "and the Copilot CLI copies that over this server's setting when it starts, so "
            "its hooks would run on a consultation; remove the key from that file",
        )
    path = home / "settings.json"
    try:
        settings = json.loads(path.read_text())
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError):
        settings = None
    # ponytail: a file with comments is refused, not parsed, and nothing locks it against
    # the CLI rewriting it between this read and write. The next run puts the setting back;
    # add a JSONC reader if someone keeps comments in this file.
    if not isinstance(settings, dict):
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"`{path}` cannot be read as a JSON object, so this server cannot switch the "
            "Copilot CLI's hooks off in it, and it will not run the CLI with them on; fix "
            "or remove it",
        )
    if settings.get("disableAllHooks") is not True:
        _write(path, json.dumps({**settings, "disableAllHooks": True}, indent=2) + "\n")
    else:
        # Left as written, but not as readable: PRIVACY.md says `0600` either way.
        path.chmod(0o600)


def _turns_hooks_on(path: Path) -> bool:
    """Whether the CLI-managed `config.json` holds a `disableAllHooks` that is not true.

    The CLI writes it as JSON under `//` comment lines. A file that names the key and
    cannot be read past those is taken as setting it false, since the CLI may read it
    all the same.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if "disableAllHooks" not in text:
        return False
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    try:
        config = json.loads(body)
    except ValueError:
        return True
    return not isinstance(config, dict) or config.get("disableAllHooks") is not True


def _mcp_off(home: Path) -> list[str]:
    """A `--disable-mcp-server` for every server this home configures.

    `--available-tools` switches off a server's tools and leaves the server running. The
    flag stops the launch, but takes a name, so the names come from the file
    `copilot mcp add` writes. A server that file does not list is not stopped. The `=`
    form, so that a name that opens with a dash is still a value.
    """
    config = _load(home / "mcp-config.json")
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    return [f"--disable-mcp-server={name}" for name in sorted(servers)] if isinstance(servers, dict) else []


def _forget(home: Path, session: str) -> None:
    """Remove one session's directory, and the lock beside it.

    The readiness check's own: the CLI creates both before it looks at the model, so a
    check it then refuses still leaves them, and the check runs again every time the ready
    answer expires -- on every turn, while signed out. And, through `forget_sessions`, a
    deleted consultation's. Never one whose consultation is still there.
    """
    state = home / "session-state"
    shutil.rmtree(state / session, ignore_errors=True)
    with contextlib.suppress(OSError):
        (state / ".session-operation-locks" / f"{session}.lock").unlink(missing_ok=True)


def forget_sessions(sessions: Iterable[str]) -> None:
    """Remove the sessions of consultations that were just deleted, and their locks.

    The stores call this once the delete has committed, so one that was refused or rolled
    back keeps its history. Best effort, like the readiness check's: a directory that
    cannot be removed stays. Only a name that is a UUID is a session this server made, so
    anything else in a database row is never turned into a path. Nothing is created: a
    home that is not there is not an error.
    """
    home = _root() / "home"
    for session in sessions:
        with contextlib.suppress(ValueError):
            _forget(home, str(UUID(session)))


def _private(path: Path) -> Path:
    """`path` as a directory this user owns and nobody else can read.

    Tightened rather than refused when it is ours: the documented first step is `login`
    with this home, and the CLI creates it under the umask, so refusing a 0755 directory
    would make that step break the next one. A symlink, or a directory that belongs to
    someone else, is still refused by `_refuse_shared`.
    """
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()  # not `stat`: a symlink must not be followed into a chmod
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
        path.chmod(0o700)
    _refuse_shared(path)
    return path


# --- parsing ----------------------------------------------------------------


@dataclass(frozen=True)
class _Reply:
    text: str
    model: str | None
    calls: int  # successful model calls behind it
    record: str


def _model(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _acted(what: str) -> AdapterError:
    return AdapterError(
        ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
        f"the agent tried to act rather than answer ({what})",
    )


def _read_stream(result: ProcessResult, session: str) -> _Reply:
    """The answer, the model that gave it, and how many model calls made it.

    The exit code was read before this. What is decided here is whether a run that
    exited 0 is an answer, and whether it stayed one.
    """
    text: list[str] = []
    answered: str | None = None
    routed: str | None = None
    calls = 0
    unfinished: list[str] = []
    finished: dict[str, Any] | None = None
    kept: list[str] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            # A diagnostic that lands on stdout is not a protocol failure.
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            # Skipping a line that opens like an event would be deciding what it said, and
            # the check below is what stands between a tool call and an answer that
            # claims none happened.
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"the agent emitted a line that is not a JSON event: {exc}",
            ) from exc
        if not isinstance(event, dict):
            continue

        kind = event.get("type")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if isinstance(kind, str) and kind.startswith("tool."):
            raise _acted(f"emitted a `{kind}` event")

        if kind == "assistant.message":
            if data.get("toolRequests"):
                raise _acted("asked for a tool")
            # `commentary` is a model narrating, not answering.
            if data.get("phase") in ("final_answer", None):
                text.append(str(data.get("content") or ""))
                answered = _model(data.get("model")) or answered
            line = json.dumps({**event, "data": {k: v for k, v in data.items() if k not in _OPAQUE}})
        elif kind == "session.auto_mode_resolved":
            routed = _model(data.get("chosenModel"))
        elif kind == "model.call_finished":
            if data.get("outcome") == "success":
                calls += 1
            else:
                unfinished.append(str(data.get("outcome")))
        elif kind == "result":
            finished = event
        if kind in _RECORDED:
            kept.append(line)

    if finished is None:
        # The last event says the turn ended, and without it a killed child or a dropped
        # connection returns its partial text as a finished reply.
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            "the agent's reply ended without a `result`, so it is a fragment rather than "
            f"an answer (exit {result.returncode}): {result.stderr.strip()[:400]}",
        )
    code = finished.get("exitCode")
    if isinstance(code, int) and code != 0:
        # The process said 0 and its own last event says otherwise: neither is trusted
        # over the other, and a failed run is not an answer. A code the event leaves out
        # is not a failure.
        raise AdapterError(
            ConsultErrorCode.AGENT_UNAVAILABLE,
            f"the agent's own last event reports exit code {code} although the process "
            f"exited {result.returncode}: {result.stderr.strip()[:400]}",
        )
    if finished.get("sessionId") != session:
        raise AdapterError(
            ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
            f"the agent answered under session `{finished.get('sessionId')}` when the turn "
            f"belongs to `{session}`",
        )
    joined = "".join(text).strip()
    if not joined:
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent produced no answer (exit {result.returncode}, model calls "
            f"{', '.join(unfinished) or 'none'}): {result.stderr.strip()[:400]}",
        )
    return _Reply(joined, answered or routed, calls, "\n".join(kept))


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@accounted
def _usage(report: Any, calls: int = 1) -> Usage:
    report = report if isinstance(report, dict) else {}
    if calls != 1:
        _caveat(
            f"the stream counted {calls} successful model calls for this turn and the "
            "usage file reports only the last one's tokens"
        )
    if "lastCallInputTokens" not in report and "lastCallOutputTokens" not in report:
        _caveat("copilot's usage file gave no token counts for this turn")
    # `lastCallInputTokens` already holds the cached share: on a resumed turn it was
    # 1710 where the session's input, cache reads included, had reached 3366.
    prompt_tokens = usage_count(report.get("lastCallInputTokens"))
    completion_tokens = usage_count(report.get("lastCallOutputTokens"))
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=None,
    )


def _add(left: Usage, right: Usage) -> Usage:
    """One turn plus the repair turn, keeping what either turn said about its counts."""
    return Usage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        cost_usd=None,
        counts_incomplete=tallied([*left.counts_incomplete, *right.counts_incomplete]),
    )
