"""What every path in this server shares: the config error, redaction, and usage.

Small on purpose. The consult and review packages own their own contracts, and
what is left here is only what more than one of them needs -- so this module has
no reason to import either, and neither has to import the other to reach it.

The one exception is `consult.errors`, for the code vocabulary that every refusal
in this server carries. It is a leaf holding a single enum and imports nothing from
here, so reaching for it costs none of the independence above.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from .consult.errors import ConsultErrorCode


class CodedFailure(ToolError):
    """A refusal this server anticipated, carrying a code and a message for the caller.

    `ToolError` is the SDK's word for "a failure you saw coming". Anything else
    escaping a tool body is treated as a crash: since mcp 2.1 the caller receives only
    `Error executing tool <name>`, with the original text withheld and a traceback
    logged at ERROR. Every refusal in this server is the anticipated kind -- each one
    has a code from a closed set and a message written to be read -- so each one has
    to arrive as a `ToolError` or the model is told a bad argument and a broken server
    apart. On mcp 2.0 the message passed through either way, which is why this went
    unnoticed until the pin resolved higher.

    The subclasses stay distinct types rather than collapsing into this one. Each path
    catches its own, and a storage failure is not a routing failure however alike the
    two look from here.
    """

    def __init__(self, code: ConsultErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConfigError(ValueError):
    """Raised at startup. A misconfigured server refuses to boot rather than
    half-configured in production.

    Lives here rather than in `server`, which imports it, so that config models in
    other packages can raise it without importing the module that composes them.
    """


MAX_ERROR_CHARS = 500
# Shared by review responses and workflow review bindings. Keeping the bound here
# prevents workflow startup and review planning from disagreeing about how many
# reviewers one step may freeze.
MAX_REVIEWERS = 5

# Credential shapes, scrubbed from every error message before it is returned. These
# messages quote their source verbatim -- a provider exception, a CLI's stderr -- and
# some providers echo the request they rejected, headers included. The environment
# variable name reaching a caller is a documented cost of useful diagnostics; the
# value it held is not.
_SECRETS = re.compile(
    r"""(?xi)
    (?: sk-ant-|sk-|rk-|xai-|gsk_|ghp_|github_pat_|AIza|AKIA|ASIA|xox[abposr]-|
        eyJ[A-Za-z0-9_-]{6,}\. )[A-Za-z0-9._\-]{8,}
    | \b(?:bearer|basic)\s+[A-Za-z0-9._\-+/=]{12,}
    # Group 1 is put back verbatim, so only the value is replaced. Swallowing the name
    # and the separator along with it would turn `{"apiKey": "..."}` into `{"..."}` --
    # still valid Python, a set literal rather than a dict, and no longer the text it
    # was masking. A reviewer reading that reports a defect nobody wrote.
    | (\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)
      # The optional quote is what a JSON body looks like: `"api_key": "..."`.
      \b["']?\s*[:=]\s*["']?)[A-Za-z0-9._\-+/=]{8,}
    | -{5}BEGIN[ A-Z]*PRIVATE KEY-{5}.*?-{5}END[ A-Z]*PRIVATE KEY-{5}
    """,
    re.DOTALL,
)


def redact(text: str) -> str:
    """Replace anything credential-shaped with a marker.

    Best effort by construction -- a secret with no recognizable shape survives it --
    so it is a second line of defence behind "do not put credentials where an error
    message can reach", never a licence to forward these messages anywhere.
    """
    return redact_counted(text)[0]


def redact_counted(text: str) -> tuple[str, int]:
    """`redact`, and how many matches it replaced.

    `\\g<1>` is the key and separator of the named-key alternative, put back so the
    masked text keeps the shape it had. The other alternatives have no group 1, and a
    group that did not participate substitutes as empty.
    """
    return _SECRETS.subn(r"\g<1>[redacted]", text)


def secret_lines(text: str) -> list[int]:
    """1-based line numbers where something credential-shaped starts.

    Positions, never values. This is what a preview shows before material is sent
    anywhere, and a preview that quoted the secret back would be one more place it
    lives.
    """
    return [text.count("\n", 0, match.start()) + 1 for match in _SECRETS.finditer(text)]


def scrub_json(value: Any) -> Any:
    """`redact` over every string leaf of a JSON-shaped value.

    Leaves rather than the serialized blob: a pattern that ran up to a closing quote
    would otherwise be replaced across it and leave text that no longer parses.

    Plain strings pass straight through, which is what makes this the whole sanitizer
    contract rather than half of one -- a turn is recorded with text in four columns
    and a `model_dump()` dict in the fifth, and a `str -> str` sanitizer would have
    silently skipped the dict.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {scrub_json(key): scrub_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_json(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_json(item) for item in value)
    if isinstance(value, set):
        return {scrub_json(item) for item in value}
    return value


# The rest of the definition, kept out of the docstring because a model docstring here
# is advertised schema -- it is repeated in seventeen tool schemas, and this is a note
# for whoever writes the next adapter, not for the agent reading the output.
#
# Every CLI reports these differently: Claude calls the uncached remainder the input,
# Codex folds cache and reasoning into its two headline figures as breakdowns of them,
# Antigravity and Opencode report theirs disjoint, and Antigravity's own total leaves
# out the thinking it just reported. An adapter passing any of those straight through
# produces a field that cannot be compared with the one beside it -- and they *are*
# compared: the review and workflow rollups sum them across agents and the dashboard
# prints the sum in one column. So each adapter converts, and `total_tokens` is derived
# from the two parts rather than read from a CLI that counts a different set.
#
# The token fields are the answering model's alone, which is narrower than the money
# beside them: Claude reports a whole-invocation `total_cost_usd` covering any internal
# helper model it ran, while the counts it reports are the one that answered. Two
# different questions -- what a consultation spent, and how large the answer was --
# and the docstring says so rather than letting a reader assume one scope covers both.
#
# No validator enforces the sum. Turns written before this definition existed carry the
# old per-runtime meanings and still have to be readable: a stored turn is what was
# measured at the time, and an old Claude row's total exceeds its parts where an old
# Antigravity row's falls short. Reading them is fine. Comparing one against a row
# written since is not -- which is what `counts_incomplete` is for.
#
# A rollup is where that gets sharp. Summing a ledger that spans the change produces
# three numbers whose total is not their sum, and the paragraph above promises it is.
# The promise is kept per turn, as counted; a rollup that cannot keep it has to say so
# rather than return a figure that contradicts its own definition. Suppressing the
# fields instead would make all three nullable on the wire, for every consumer, to hide
# a number that gates nothing -- `spend.refusal` bounds on money and on turns, never on
# these -- so the number is returned and what is wrong with it is returned beside it.
class Usage(BaseModel):
    """What one turn cost in tokens, counted the same way whatever runtime answered.

    `prompt_tokens` is every input token the answering model was billed for, cache
    reads and writes included -- a cached prompt was still sent. `completion_tokens`
    is every token it generated, reasoning and thinking included. `total_tokens` is
    the two added together for a turn as it was counted. `cost_usd`, where a runtime
    reports one, may cover the whole invocation rather than the answering model alone.

    `counts_incomplete` is empty when those three are a straight measurement, and
    otherwise carries every reason they are not: a count the runtime reported
    unreadably and this server substituted a zero for, a rollup that added turns
    counted under two different definitions of these fields, a rollup whose total is
    not its own parts because some turn in it predates that rule. A zero that was
    invented reads exactly like a zero that was measured, so the difference has to be
    said rather than inferred.

    A list rather than one string, because these accumulate and then get added up. Two
    reasons joined early are indistinguishable from one reason containing a semicolon,
    which is what a rollup of rollups would then be de-duplicating against.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    counts_incomplete: list[str] = []

