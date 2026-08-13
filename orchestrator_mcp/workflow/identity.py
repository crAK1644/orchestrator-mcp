"""Whether a candidate agent *is* the host, decided on execution identity.

The public consult path answers this at runtime level: any agent sharing the host's
runtime is refused, and that is all it can do -- nothing tells it which model the
host itself is. A workflow can do better, because `consult.host.model:` names the
host's model in trusted startup configuration, so a *different* model on the same
runtime becomes routable.

Better, not looser. The rule is provable difference:

  * different runtime -> not the host, as before;
  * same runtime and no configured host model -> refused, exactly today's behaviour;
  * same runtime and `_same_model` matches in either direction -> refused;
  * same runtime and either name carries no version at all -> refused, because
    `opus` cannot be shown to differ from `claude-opus-5`.

Unprovable is refused. The cost of the last rule is that an operator who writes a
bare alias for `consult.host.model:` gets runtime-level exclusion back for that
runtime, which is the conservative answer and not a failure.

`_same_model` and `_tokens` are reused from the adapter layer rather than copied:
they are the same question the model-substitution check already answers, and two
implementations of "are these the same model" would drift.
"""

from __future__ import annotations

from ..consult.adapters.base import _same_model, _tokens


# The one reason that means "these are the same model" rather than "we cannot tell".
# A named constant because `host_identity_conflict` words those two cases
# differently and comparing against a literal twice is how they drift apart.
SAME_MODEL = "they name the same model"


def _indistinguishable(left_model: str, right_model: str) -> str | None:
    """Why these two model names cannot be shown to name different models.

    None means they provably differ. Everything that decides identity anywhere in
    the workflow comes through here, so there is one answer to the question and not
    one per caller.
    """
    left, right = _tokens(left_model), _tokens(right_model)
    if _same_model(left, right) or _same_model(right, left):
        return SAME_MODEL
    unversioned = [
        name
        for name, tokens in ((left_model, left), (right_model, right))
        if not any(part.isdigit() for part in tokens)
    ]
    if unversioned:
        return f"`{unversioned[0]}` names no version"
    return None


def same_execution_identity(
    left_runtime: str, left_model: str, right_runtime: str, right_model: str
) -> str | None:
    """Why these two `(runtime, model)` pairs are one execution identity.

    None when they provably differ. Used wherever a workflow has to keep two roles
    apart -- the host and an agent, or a reviewer and the agent whose work it is
    reviewing -- because an agent id is a label and this is the thing underneath it.
    """
    if left_runtime != right_runtime:
        return None
    return _indistinguishable(left_model, right_model)


def host_identity_conflict(
    agent_runtime: str,
    agent_model: str,
    host_runtime: str,
    host_model: str | None,
) -> str | None:
    """The reason this agent is the host answering itself, or None if it is not."""
    if agent_runtime != host_runtime:
        return None
    if not host_model:
        return (
            f"`{agent_runtime}` is this host's own runtime and no `consult.host.model:` "
            "is configured, so no agent on it can be shown to be a different model"
        )
    reason = same_execution_identity(agent_runtime, agent_model, host_runtime, host_model)
    if reason == SAME_MODEL:
        return (
            f"`{agent_runtime}/{agent_model}` is this host's own execution identity "
            f"({host_runtime}/{host_model})"
        )
    if reason is not None:
        return (
            f"`{agent_runtime}/{agent_model}` cannot be shown to differ from this host "
            f"({host_runtime}/{host_model}): {reason}"
        )
    return None
