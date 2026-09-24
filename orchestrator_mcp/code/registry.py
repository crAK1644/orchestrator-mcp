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
from ..contract import CodedFailure
from . import sandbox

# Why each runtime sits where it does:
#
#   codex        `sandbox_mode="workspace-write"` with `approval_policy="never"` is
#                enforced by the CLI at the OS level, so a writable path really is a
#                bound and not a request.
#   opencode     its permission set and our ancestor-config checks isolate
#                *configuration*, not filesystem effects: an allowed bash command can
#                leave the working directory and write anywhere the user can. So its
#                `isolated_write` is not in the table at all -- it is not a property of
#                the runtime. See `_SANDBOX_GATED_WRITE` below.
#   claude       the same: its permission modes are requests the CLI honours, not
#                bounds the kernel enforces.
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

# Runtimes whose `isolated_write` is supplied from outside the runtime, by `sandbox`.
# Kept out of the table above because that table says what a runtime is, and this
# depends on the host: the same binary is containable on a machine where the sandbox
# holds and not on one where it does not.
_SANDBOX_GATED_WRITE: frozenset[Runtime] = frozenset({"claude", "opencode"})

# Runtimes with a write adapter that exists. Names rather than classes because
# importing the adapters here would make `code` and `consult` circular; the import
# happens inside `code_adapter_for`, which checks this same set first.
#
# This gates the capability as well, so that a containable runtime with no adapter is
# refused at boot by `_agents_can_execute` rather than four steps into a workflow.
_WRITE_ADAPTERS: frozenset[str] = frozenset({"codex"})

# Said back to an operator who asked for a mode the code cannot honour. Naming the
# reason matters more than usual here: "not supported" reads as "not implemented
# yet", and for antigravity it is a refusal that will not be lifted. The gated
# runtimes explain themselves in `unsupported_reason`, from the host's own answers.
_UNSUPPORTED: dict[tuple[Runtime, ExecutionMode], str] = {
    ("antigravity", "isolated_write"): (
        "writing through antigravity requires `--dangerously-skip-permissions`, "
        "which its adapter refuses by construction"
    ),
}


class CodeError(CodedFailure):
    """A refusal from the execution side, carrying a code the envelope can hold."""


def runtime_capabilities(runtime: str) -> frozenset[ExecutionMode]:
    """What this runtime supports *here*. An unknown runtime supports nothing.

    No fallthrough to a default set, for the reason `adapter_for` spells out: a
    mistyped runtime that quietly inherited another one's capabilities would be
    granted a write mode nobody chose.

    Host-dependent for the runtimes in `_SANDBOX_GATED_WRITE`, and three conditions
    are required: a write adapter must exist, the sandbox must *hold*, and it must be
    able to grant the network the worker reaches its hosted model over. The adapter is
    asked first because it is free: `holds()` runs a real escape probe, and a boot
    whose config names a claude agent should not spawn one for a mode it cannot have.

    `sandbox.holds()` rather than `sandbox.mechanism()`: the capability says a write
    outside the worktree is impossible, which is a statement about what the kernel
    does; the presence of a binary is a statement about the filesystem.
    """
    base = RUNTIME_CAPABILITIES.get(runtime, frozenset())  # type: ignore[arg-type]
    if runtime not in _SANDBOX_GATED_WRITE or runtime not in _WRITE_ADAPTERS:
        return base
    if not sandbox.holds() or sandbox.internet_unavailable_reason() is not None:
        return base
    return base | {"isolated_write"}


def unsupported_reason(runtime: str, mode: ExecutionMode) -> str:
    """Why `runtime` cannot do `mode`, in words that name which side said no."""
    if mode == "isolated_write" and runtime in _SANDBOX_GATED_WRITE:
        # Three refusals, three sentences: the operator missing bubblewrap can fix
        # their host today, the one on Linux waiting on a network the sandbox can
        # grant cannot, and the one waiting on an adapter cannot either. Host first,
        # because an adapter landing does not help a host that cannot run it.
        if not sandbox.holds():
            return (
                f"`{runtime}` can only be contained by an OS-level sandbox, and "
                f"{sandbox.unavailable_reason()}"
            )
        if (why := sandbox.internet_unavailable_reason()) is not None:
            return (
                f"`{runtime}` can be contained on this host, yet reaches its hosted "
                f"model over a network the containment cannot grant: {why}"
            )
        if runtime not in _WRITE_ADAPTERS:
            return (
                f"`{runtime}` can be contained on this host, yet has no write adapter "
                "of its own; use `patch` or run the step on the host"
            )
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
