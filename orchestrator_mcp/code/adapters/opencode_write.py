"""OpenCode, editing one worktree, held there by the kernel rather than its config.

opencode ships no `--sandbox` flag, so the bound is `sandbox.wrap`, and every argv
this adapter builds goes through it. Its network is `internet`: the model is hosted,
so the worker can reach anything, and only its *writes* are held to the worktree.
That is weaker than codex and is the trade `registry._SANDBOX_GATED_WRITE` names.

Three things were measured against opencode 1.18.15 and each shaped the code:

  * **A denying `permission` block denies the runtime its own eyes.** Under
    `{"*": "deny"}` the read tool is refused, and `"read": "allow"` does not lift it --
    `read` is not a permission name. So the block is not where containment lives; the
    sandbox is. The two denials in `WORKER_PERMISSION` state intent for what a
    filesystem rule cannot say.
  * **A repository's own `opencode.json` merges into the run.** Allowed, because it
    belongs to the task the same way `AGENTS.md` does, and whatever it starts is
    confined with the rest. Configs *above* the worktree belong to nobody in the task,
    and are refused.
  * **All runtime state has to live inside the worktree**, because nothing else is
    writable. `HOME` and the XDG directories point into `RUNTIME_DIR`, which is removed
    before the service reads the diff.

Stored credentials are not carried in: the data directory moves with the rest, so a
provider that needs `opencode auth login` fails with its auth error. opencode's own
free catalogue needs none.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ...consult.adapters.base import (
    AdapterError,
    ProcessResult,
    child_env,
    resolve_command,
    run_process,
)
from ...consult.adapters.opencode_cli import _add, _refuse_inherited_config, _usage
from ...consult.config import AgentConfig
from ...consult.errors import ConsultErrorCode
from ...consult.prompts import CompiledPrompt
from ...contract import Usage
from .. import sandbox
from .base import AdapterRun, ObservedCommand

# Everything the child writes that is not the work: its config, database, logs. Inside
# the worktree because nothing outside it is writable, and removed before the service
# reads git because a patch is supposed to be the change.
RUNTIME_DIR = ".orchestrator-runtime"

# `"*": "allow"` is not the absence of a decision -- an allowlist keyed by tool name is
# not available, see the module docstring. The two denials are what the sandbox cannot
# say: fetching the web as a tool, and touching paths outside the project.
WORKER_PERMISSION = {"*": "allow", "webfetch": "deny", "external_directory": "deny"}

_WORKER_CONFIG = json.dumps(
    {"permission": WORKER_PERMISSION, "plugin": [], "share": "disabled", "autoupdate": False}
)

# How much of a command's output is kept per command, matching the codex adapter.
OUTPUT_TAIL_CHARS = 2000

_SHELL_TOOLS = frozenset({"bash"})
# Tools that report a path they wrote. Evidence about what a run tried, never the
# record of what it did -- that record is the diff.
_WRITE_TOOLS = frozenset({"edit", "write", "patch", "multiedit"})


class OpenCodeWriteAdapter:
    """A contained opencode run. No resume: a worktree does not outlive its step."""

    runtime = "opencode"

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    async def execute(
        self,
        agent: AgentConfig,
        prompt: CompiledPrompt,
        worktree: Path,
        timeout_s: float | None = None,
    ) -> AdapterRun:
        command = resolve_command(agent)
        _refuse_inherited_config(worktree)
        # The registry asked at boot; this is the line that launches, and a boot-time
        # answer is not evidence about the host now.
        if not sandbox.holds():
            raise AdapterError(
                ConsultErrorCode.AGENT_UNAVAILABLE,
                f"opencode cannot be contained on this host: {sandbox.unavailable_reason()}",
            )
        if (why := sandbox.internet_unavailable_reason()) is not None:
            raise AdapterError(
                ConsultErrorCode.AGENT_UNAVAILABLE,
                f"opencode cannot reach its hosted model from inside the sandbox: {why}",
            )

        runtime_dir = worktree / RUNTIME_DIR
        try:
            argv = sandbox.wrap(
                [
                    command,
                    "run",
                    "--pure",  # no external plugins
                    "--format",
                    "json",
                    # Error events carry an opaque reference; the cause is only on
                    # stderr, and only when logs are asked for.
                    "--print-logs",
                    "--log-level",
                    "ERROR",
                    "--model",
                    agent.model,
                ],
                worktree,
                network=sandbox.NETWORK_INTERNET,
            )
            result = await run_process(
                argv,
                prompt.full_text,
                timeout_s or self.timeout_s,
                env=_child_env(runtime_dir),
                cwd=worktree,
            )
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)

        summary, native, usage, tools = _read_stream(result)
        return AdapterRun(
            summary=summary,
            commands=[tool for tool in tools if isinstance(tool, ObservedCommand)],
            claimed_paths=sorted({path for path in tools if isinstance(path, str)}),
            native_session_id=native,
            # The stream never names the model; `opencode export` does, but the session
            # it reads went with `RUNTIME_DIR`. Asked-for and unverified, said so.
            model_used=agent.model,
            model_verified=False,
            raw_output=result.stdout,
            usage=usage,
        )


def _child_env(runtime_dir: Path) -> dict[str, str]:
    """An environment whose every writable location is inside the worktree.

    `HOME` moves with the XDG directories because a runtime falling back to `~/.cache`
    where that is not writable fails with nothing resembling the truth. `child_env` is
    the allowlist underneath, so no provider key or proxy variable rides along.
    """
    home = runtime_dir / "home"
    dirs = {
        "HOME": home,
        "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data",
        "XDG_CACHE_HOME": home / "cache",
        "XDG_STATE_HOME": home / "state",
        "TMPDIR": runtime_dir / "tmp",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    config = runtime_dir / "config.json"
    config.write_text(_WORKER_CONFIG)
    return child_env(
        {name: str(path) for name, path in dirs.items()} | {"OPENCODE_CONFIG": str(config)}
    )


# --- reading the stream -----------------------------------------------------


def _read_stream(result: ProcessResult) -> tuple[str, str, Usage, list[Any]]:
    """The final text, the session id, the cost, and what the run says it did.

    The exit code is not consulted: `opencode run` returns 0 on a hard provider
    failure, so the stream is the only account of what happened.
    """
    text: list[str] = []
    native = ""
    usage: Usage | None = None
    tools: list[Any] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise AdapterError(
                ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                f"the agent emitted a line that is not a JSON event: {exc}",
            ) from exc
        if not isinstance(event, dict):
            continue

        session = event.get("sessionID")
        if isinstance(session, str) and session:
            if native and session != native:
                raise AdapterError(
                    ConsultErrorCode.PROTOCOL_VALIDATION_FAILED,
                    f"the agent answered under session `{session}` when the run belongs "
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
        if kind == "text":
            text.append(str(part.get("text") or ""))
        elif kind == "tool":
            # Unlike a consultation, a tool part is the point here.
            tools.extend(_tool(part))
        elif kind == "step-finish":
            step = _usage(part)
            usage = step if usage is None else _add(usage, step)

    if not native:
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent returned no session id (exit {result.returncode}): "
            f"{result.stderr.strip()[:400]}",
        )
    if usage is None:
        # The terminal part is the only thing saying the turn ended, so its absence is
        # a fragment -- a killed child, a dropped connection -- not a run that cost nothing.
        raise AdapterError(
            ConsultErrorCode.TRANSPORT_ERROR,
            f"the agent's run ended without a `step-finish`, so it stopped rather than "
            f"finished (exit {result.returncode}): {result.stderr.strip()[:400]}",
        )
    return "".join(text).strip(), native, usage, tools


def _tool(part: dict) -> list[Any]:
    """One tool part, as either a command that ran or a path it says it wrote."""
    name = part.get("tool")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    arguments = state.get("input") if isinstance(state.get("input"), dict) else {}
    if name in _SHELL_TOOLS:
        text = arguments.get("command")
        if not isinstance(text, str) or not text.strip():
            return []
        return [
            ObservedCommand(
                command=text.strip(),
                # No exit code in this stream; a failed command arrives as
                # `status: "error"`, which is what the 1 stands for.
                exit_code=0 if state.get("status") == "completed" else 1,
                output_tail=str(state.get("output") or state.get("error") or "")[
                    -OUTPUT_TAIL_CHARS:
                ],
            )
        ]
    if name in _WRITE_TOOLS:
        path = arguments.get("filePath") or arguments.get("path")
        return [path] if isinstance(path, str) and path else []
    return []
