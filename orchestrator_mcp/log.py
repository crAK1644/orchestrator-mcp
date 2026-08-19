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

# The transport, by descriptor. Both POSIX and Windows put stdout on 1, and it is the
# number a handler that reached it another way still holds.
_STDOUT_FD = 1

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class RedactingFormatter(logging.Formatter):
    """Mask credential-shaped text in the *entire* formatted record.

    A `logging.Filter` was the obvious place for this and is the wrong one. A filter
    only ever sees `record.msg`, while `Formatter.format` appends `record.exc_text`
    and `record.stack_info` afterwards -- so one `exc_info=True` on an error whose
    traceback quotes a token would walk straight past a filter that had already
    approved the message. Masking the formatter's return value means the mask sees
    exactly the bytes about to be written, whichever of the three parts they came
    from.

    Formatting rather than filtering also leaves `record.msg` and `record.args` as the
    caller wrote them. A filter that rewrites them mutates an object shared with every
    other handler on the logger, so a second sink -- a file handler, a test's
    `caplog` -- would see text this module edited rather than what its own caller
    logged. Not the same as leaving the record untouched: `Formatter.format` populates
    `record.message`, `record.asctime` and a cached `record.exc_text` as it goes, so a
    later handler can read an `exc_text` this call filled in. Those carry the stdlib's
    own unredacted values, which is exactly what that handler would have produced for
    itself -- the masking applies to what *this* sink writes, and claims nothing about
    anyone else's.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


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
    _drop_stdout_handlers(logger)
    if not any(getattr(handler, "_orchestrator", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(RedactingFormatter(_FORMAT))
        handler._orchestrator = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


def _drop_stdout_handlers(logger: logging.Logger) -> None:
    """Remove every handler under this logger that writes to stdout.

    `propagate = False` stops records reaching a handler on the *root* logger, and
    stops nothing already attached inside this subtree -- an embedder that configured
    `orchestrator_mcp` itself keeps its handler, and if that handler's stream is
    stdout every log line it writes is framed as an MCP message and corrupts the
    session. Dropping it is not politeness, but it is the only outcome where the
    server still works, and the alternative is a transport failure nobody attributes
    to a logging config.

    The whole subtree, not just this logger: a handler on `orchestrator_mcp.consult`
    runs *before* the record propagates up here, so checking only the parent leaves
    the one sink that gets first look at every record from that module.

    What it cannot cover is a handler installed after this runs. `configure` is
    called as the first statement of `build_server`, so anything added later is
    outside its reach -- which is worth knowing rather than implying otherwise.
    """
    for target in _subtree(logger):
        for handler in list(target.handlers):
            if _writes_to_stdout(handler):
                target.removeHandler(handler)


def _subtree(logger: logging.Logger) -> list[logging.Logger]:
    """This logger and every already-created logger under it.

    `loggerDict` also holds `PlaceHolder` entries for names that only exist as an
    ancestor of something real; those own no handlers and are skipped.
    """
    prefix = f"{logger.name}."
    return [logger] + [
        existing
        for name, existing in logging.Logger.manager.loggerDict.items()
        if name.startswith(prefix) and isinstance(existing, logging.Logger)
    ]


def _writes_to_stdout(handler: logging.Handler) -> bool:
    """Whether this handler's output would land on the transport.

    Identity against `sys.stdout` is the obvious test and misses the interesting
    cases: a handler built from `sys.__stdout__`, or a `FileHandler` opened on
    `/dev/stdout`, holds a different object and the same file descriptor. So the
    descriptor is what decides wherever one can be had.

    Wherever one cannot -- a `StringIO`, a socket, a closed stream -- the answer is
    no. This removes what it can prove writes to the transport, and a handler that
    cannot be proven to is somebody's deliberate choice.
    """
    stream = getattr(handler, "stream", None)
    if stream is None:
        return False
    if stream is sys.stdout or stream is sys.__stdout__:
        return True
    try:
        return stream.fileno() == _STDOUT_FD
    except (AttributeError, OSError, ValueError):
        return False


def _level(level: str | None) -> int:
    """The configured level, falling back rather than refusing to start.

    A typo in an environment variable is not worth a server that will not boot, and
    a server that boots silent is the failure this module exists to fix.
    """
    name = (level or os.environ.get(LEVEL_ENV) or DEFAULT_LEVEL).strip().upper()
    resolved = logging.getLevelNamesMapping().get(name)
    return resolved if resolved is not None else logging.WARNING
