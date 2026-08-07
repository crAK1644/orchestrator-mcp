"""What every path in this server shares: the config error, redaction, and usage.

Small on purpose. The consult and review packages own their own contracts, and
what is left here is only what more than one of them needs -- so this module has
no reason to import either, and neither has to import the other to reach it.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel


class ConfigError(ValueError):
    """Raised at startup. A misconfigured server refuses to boot rather than
    half-configured in production.

    Lives here rather than in `server`, which imports it, so that config models in
    other packages can raise it without importing the module that composes them.
    """


MAX_ERROR_CHARS = 500

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
    | \b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)
      # The optional quote is what a JSON body looks like: `"api_key": "..."`.
      \b["']?\s*[:=]\s*["']?[A-Za-z0-9._\-+/=]{8,}
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
    """`redact`, and how many matches it replaced."""
    return _SECRETS.subn("[redacted]", text)


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


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None


