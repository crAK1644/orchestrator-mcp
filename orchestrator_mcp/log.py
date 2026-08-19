"""stderr logging for the MCP server.

Named `log` rather than `logging` so nothing in the package has to think about
whether an import is the stdlib or this module.

Two properties are load-bearing:

**stdout is the MCP transport.** A log record written there is framed as a
protocol message and corrupts the session. The handler here is pinned to stderr
and installed on the `orchestrator_mcp` logger with `propagate = False`, so a
host process that has configured the root logger to write somewhere else cannot
pull these records along with it.

**Redaction is the handler's job, not the call site's.** Same reasoning the store
already uses for `scrub_json` at the insert: an adapter that starts quoting a
failing argv, or an error string that happens to carry a token, would otherwise
leak through a log line nobody re-audited. Filtering here means the guarantee
belongs to the sink.
"""

from __future__ import annotations

import logging
import os
import sys

from .contract import redact


ROOT = "orchestrator_mcp"
LEVEL_ENV = "ORCHESTRATOR_LOG_LEVEL"
DEFAULT_LEVEL = "WARNING"

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class RedactingFilter(logging.Filter):
    """Mask credential-shaped text in the fully rendered message.

    Rendered first, then masked, rather than masking `msg` and `args` separately.
    Masking only the string arguments looks equivalent and is not: `log.warning(
    "could not start: %s", exc)` passes an *exception*, `%s` calls its `__str__`
    at format time, and an `AdapterError` that quotes a failing argv would sail
    straight through an argument-wise filter. Rendering here means the mask sees
    exactly the bytes the handler is about to write, whatever produced them.

    The record is left carrying the rendered text with `args` cleared, which is
    what any later handler would have produced anyway.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def get_logger(name: str) -> logging.Logger:
    """A logger under `orchestrator_mcp.*`.

    Call it with `__name__`. Modules in this package already sit under that root,
    so the name arrives correct and the handler installed by `configure` applies.
    """
    return logging.getLogger(name)


def configure(level: str | None = None) -> logging.Logger:
    """Install the stderr handler once, and return the package logger.

    Idempotent: `build_server` may be called more than once in a test process, and
    a second handler would double every line. Off by default in spirit -- the level
    is `WARNING` unless `ORCHESTRATOR_LOG_LEVEL` asks for more, the same way the
    dashboard is something you turn on.
    """
    logger = logging.getLogger(ROOT)
    logger.setLevel(_level(level))
    # stdout is the transport. Records must not reach a root handler that a host
    # process pointed somewhere else, so this subtree does not propagate.
    logger.propagate = False
    if not any(getattr(handler, "_orchestrator", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(RedactingFilter())
        handler._orchestrator = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


def _level(level: str | None) -> int:
    """The configured level, falling back rather than refusing to start.

    A typo in an environment variable is not worth a server that will not boot, and
    a server that boots silent is the failure this module exists to fix.
    """
    name = (level or os.environ.get(LEVEL_ENV) or DEFAULT_LEVEL).strip().upper()
    resolved = logging.getLevelNamesMapping().get(name)
    return resolved if resolved is not None else logging.WARNING
