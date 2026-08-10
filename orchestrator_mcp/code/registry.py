"""What each runtime can actually be made to do, and where a write adapter comes from.

Deliberately outside `consult.adapters`. `ConsultAdapter` is narrow on purpose --
three verbs, no way to ask for anything agentic -- and that claim is only worth
making if execution capability cannot be reached through it. So the two live in
different packages: `adapter_for` returns something that can only ask a question,
and `code_adapter_for` returns something that can write, and no caller can turn one
into the other.

The table below is the code's own statement about containment, not the operator's.
An agent that declares `isolated_write` in YAML is declaring trust; whether the
runtime can be *held* to a directory is decided here, by whoever wrote the adapter.
"""

from __future__ import annotations

from typing import Any

from ..consult.contract import ExecutionMode, Runtime
from ..consult.errors import ConsultErrorCode

# Why each runtime sits where it does:
#
#   codex        `sandbox_mode="workspace-write"` with `approval_policy="never"` is
#                enforced by the CLI at the OS level, so a writable path really is a
#                bound and not a request.
#   opencode     its permission set and our ancestor-config checks isolate
#                *configuration*, not filesystem effects: an allowed bash command can
#                leave the working directory and write anywhere the user can. Not
#                supported until the process runs inside an OS-level sandbox whose
#                writable filesystem is the worktree.
#   claude       deferred behind the same bar as opencode.
#   antigravity  writing needs `--dangerously-skip-permissions`, the one flag its
#                adapter refuses by construction.
#
# Every runtime gets `patch`, because a patch is text: it comes back through the
# read-only consult path and the host applies it. That mode adds no write surface.
RUNTIME_CAPABILITIES: dict[Runtime, frozenset[ExecutionMode]] = {
    "codex": frozenset({"consultation", "patch", "isolated_write"}),
    "claude": frozenset({"consultation", "patch"}),
    "opencode": frozenset({"consultation", "patch"}),
    "antigravity": frozenset({"consultation", "patch"}),
}

# Said back to an operator who asked for a mode the code cannot honour. Naming the
# reason matters more than usual here: "not supported" reads as "not implemented
# yet", and for opencode and claude that is true, while for antigravity it is a
# refusal that will not be lifted.
_UNSUPPORTED: dict[tuple[Runtime, ExecutionMode], str] = {
    ("opencode", "isolated_write"): (
        "opencode permissions isolate configuration, not filesystem effects: an "
        "allowed command can leave the worktree. Supported once the process runs "
        "inside an OS-level sandbox limited to the worktree"
    ),
    ("claude", "isolated_write"): (
        "the claude runtime has no contained executor yet; consultation and patch "
        "are supported"
    ),
    ("antigravity", "isolated_write"): (
        "writing through antigravity requires `--dangerously-skip-permissions`, "
        "which its adapter refuses by construction"
    ),
}


class CodeError(Exception):
    """A refusal from the execution side, carrying a code the envelope can hold."""

    def __init__(self, code: ConsultErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def runtime_capabilities(runtime: str) -> frozenset[ExecutionMode]:
    """What this runtime supports. An unknown runtime supports nothing.

    No fallthrough to a default set, for the reason `adapter_for` spells out: a
    mistyped runtime that quietly inherited another one's capabilities would be
    granted a write mode nobody chose.
    """
    return RUNTIME_CAPABILITIES.get(runtime, frozenset())  # type: ignore[arg-type]


def unsupported_reason(runtime: str, mode: ExecutionMode) -> str:
    """Why `runtime` cannot do `mode`, in words that name which side said no."""
    known = _UNSUPPORTED.get((runtime, mode))  # type: ignore[arg-type]
    if known is not None:
        return f"`{runtime}` does not support `{mode}`: {known}"
    if runtime not in RUNTIME_CAPABILITIES:
        return f"`{runtime}` is not a runtime this installation implements"
    return f"`{runtime}` does not support `{mode}`"


def code_adapter_for(agent: object, config: object) -> Any:
    """The write-capable adapter for an agent.

    The capability table is consulted first and the adapter is looked up second, so a
    runtime that has no entry above is refused by policy rather than by an import
    error -- and the refusal says which side declined.
    """
    runtime = getattr(agent, "runtime", "")
    if "isolated_write" not in runtime_capabilities(runtime):
        # Not "not built yet" -- this runtime is not going to get one on these terms.
        raise CodeError(ConsultErrorCode.AGENT_UNAVAILABLE, unsupported_reason(runtime, "isolated_write"))

    # Imported here, not at module scope: `code.service` imports this module, and the
    # adapter imports the consult transport, so a top-level import would make the two
    # packages circular for the sake of one lookup.
    from .adapters.codex_cli import CodexWriteAdapter

    adapters = {"codex": CodexWriteAdapter}
    build = adapters.get(runtime)
    if build is None:
        raise CodeError(
            ConsultErrorCode.AGENT_UNAVAILABLE,
            f"`{runtime}` can be contained, but its write adapter is not implemented yet; "
            "use `patch` or run the step on the host",
        )
    return build(timeout_s=getattr(config, "timeout_s", 180))
