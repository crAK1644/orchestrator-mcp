"""MCP progress notifications for the tools that can run for minutes.

With `timeout_s` set high enough for a reasoning model at high effort, a deep
review is up to half an hour in which the host agent hears nothing at all -- and
a wedged run is indistinguishable from a slow one. This module is what makes the
difference visible.

The sink is a `ContextVar` rather than a parameter threaded down through the
services. One MCP call is one asyncio context, and tasks inherit the context they
were created in, so a reviewer task spawned four frames below the tool still
finds the right sink with no signature changes on the way. The alternative --
passing `Context` into `ConsultService` and the adapters -- would put an MCP
transport object in the layer this codebase deliberately keeps free of MCP
concepts.

Progress is decoration, never the work. Every emission here suppresses its own
failures: a notification that cannot be delivered must not fail a consultation
that otherwise succeeded.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from .log import get_logger

log = get_logger(__name__)

# Often enough that a watching agent sees the run is alive, rare enough that half
# an hour of waiting is a hundred-odd notifications rather than thousands.
HEARTBEAT_INTERVAL_S = 15.0

# How long one notification gets to be delivered. Failures were already suppressed,
# but a `report_progress` that never returns is not a failure -- it is a hang, and
# the start notification is awaited *before* the body runs. Without this bound a
# backpressured client could stop a consultation from ever starting, which is the
# one thing this module promises never to do.
EMIT_TIMEOUT_S = 5.0


class ProgressSink(Protocol):
    """The one method this module needs from `mcp.server.mcpserver.Context`."""

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None: ...


_CURRENT: ContextVar[ProgressSink | None] = ContextVar("orchestrator_progress", default=None)


async def step(message: str, progress: float | None = None, total: float | None = None) -> None:
    """Report a phase boundary from anywhere inside a tool call.

    A no-op outside one, and a no-op when the caller did not ask for progress --
    the SDK already treats a missing progress token that way, so nothing here has
    to know whether the client wanted notifications.
    """
    sink = _CURRENT.get()
    if sink is None:
        return
    await _emit(sink, progress if progress is not None else 0.0, total, message)


@asynccontextmanager
async def reporting(
    ctx: Any | None,
    what: str,
    timeout_s: float | None = None,
    interval_s: float = HEARTBEAT_INTERVAL_S,
) -> AsyncIterator[None]:
    """Install `ctx` as the progress sink and heartbeat while the body runs.

    `timeout_s` becomes the `total`, and belongs here only when the caller knows the
    deadline this run will *actually* be killed at. No tool passes one today: a
    per-agent `timeout_s` means the consult deadline is unknown until routing picks
    an agent, and a review fan-out has one deadline per reviewer rather than one for
    the call. Without a total the notifications carry elapsed seconds alone, which is
    still the difference between "working" and "wedged" -- and better than a bar that
    fills at a ceiling the run is not held to.
    """
    if ctx is None or not hasattr(ctx, "report_progress"):
        # A direct call, or a test harness that passed no context. The body still
        # runs; `step` simply finds no sink.
        yield
        return

    token = _CURRENT.set(ctx)
    started = time.monotonic()
    await _emit(ctx, 0.0, timeout_s, f"{what}: started")
    beat = asyncio.create_task(_heartbeat(ctx, what, started, timeout_s, interval_s))
    try:
        yield
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        _CURRENT.reset(token)
        elapsed = time.monotonic() - started
        await _emit(ctx, elapsed, timeout_s, f"{what}: finished after {elapsed:.0f}s")


async def _heartbeat(
    sink: ProgressSink, what: str, started: float, timeout_s: float | None, interval_s: float
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        elapsed = time.monotonic() - started
        remaining = f" of {timeout_s:.0f}s" if timeout_s else ""
        await _emit(sink, elapsed, timeout_s, f"{what}: {elapsed:.0f}s{remaining} elapsed")


async def _emit(
    sink: ProgressSink, progress: float, total: float | None, message: str | None
) -> None:
    try:
        async with asyncio.timeout(EMIT_TIMEOUT_S):
            await sink.report_progress(progress, total, message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Debug, not warning: a client that closed mid-call is an ordinary end to a
        # session, and a warning per heartbeat would be the noisiest thing here.
        log.debug("progress notification dropped: %s", exc)
