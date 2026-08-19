"""Logging and progress: the two things that make a long run visible.

The load-bearing assertion in the logging half is negative -- nothing on stdout.
stdout is the MCP transport, so a single stray log record there is not noisy
output, it is a corrupt protocol frame and a dead session.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys

import pytest

from orchestrator_mcp import log as logmod
from orchestrator_mcp import progress
from orchestrator_mcp.consult.adapters.base import AdapterError
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.server import build_server

from .conftest import consult_block

SECRET = "sk-ant-api03-0123456789abcdefghijklmnopqrstuvwxyz"  # noqa: S105 - shaped, not real


@pytest.fixture
def fresh_logger():
    """A package logger with no handlers, restored afterwards.

    `configure` is idempotent by design, so without this a test that asserts on
    handler count would depend on whether an earlier test built a server."""
    logger = logging.getLogger(logmod.ROOT)
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers.clear()
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate


# --- stdout is the transport ------------------------------------------------


def test_records_go_to_stderr_and_never_to_stdout(fresh_logger, capsys, monkeypatch):
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    logmod.configure()
    logmod.get_logger("orchestrator_mcp.probe").warning("a visible line")

    captured = capsys.readouterr()
    assert "a visible line" in captured.err
    assert captured.out == ""


def test_building_a_server_installs_exactly_one_handler(fresh_logger, host_claude):
    build_server({"consult": consult_block()})
    build_server({"consult": consult_block()})

    # Ours specifically: pytest attaches its own capture handlers directly to a
    # logger that does not propagate, which is itself evidence the subtree is
    # isolated. Two builds in one process is ordinary in a test run, and a second
    # stderr handler would double every line for the rest of it.
    ours = [h for h in fresh_logger.handlers if getattr(h, "_orchestrator", False)]
    assert len(ours) == 1


def test_the_subtree_does_not_propagate(fresh_logger):
    logmod.configure()
    # A host process that pointed the root logger at stdout must not pull these
    # records along with it.
    assert fresh_logger.propagate is False


def test_an_unreadable_level_falls_back_rather_than_refusing_to_start(
    fresh_logger, monkeypatch
):
    monkeypatch.setenv(logmod.LEVEL_ENV, "chatty")
    assert logmod.configure().level == logging.WARNING


# --- redaction belongs to the sink ------------------------------------------


def test_a_credential_in_a_logged_message_is_masked(fresh_logger, capsys, monkeypatch):
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    logmod.configure()
    logmod.get_logger("orchestrator_mcp.probe").warning("child said %s", SECRET)

    err = capsys.readouterr().err
    assert SECRET not in err
    assert "[redacted]" in err


def test_an_adapter_error_carrying_a_credential_is_masked(fresh_logger, capsys, monkeypatch):
    """The case the filter exists for: nobody re-audits an error string.

    An adapter that starts quoting a failing argv would otherwise leak through a
    log line that looked entirely innocent at the call site."""
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    logmod.configure()
    exc = AdapterError(ConsultErrorCode.TRANSPORT_ERROR, f"spawn failed: --token {SECRET}")
    logmod.get_logger("orchestrator_mcp.probe").warning("could not start: %s", exc)

    err = capsys.readouterr().err
    assert SECRET not in err


def test_the_filter_leaves_non_string_arguments_alone(fresh_logger, capsys, monkeypatch):
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    logmod.configure()
    logmod.get_logger("orchestrator_mcp.probe").info("pid=%d rc=%d", 4321, 0)

    assert "pid=4321 rc=0" in capsys.readouterr().err


# --- progress ---------------------------------------------------------------


class Sink:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float | None, str | None]] = []

    async def report_progress(self, progress_, total=None, message=None) -> None:
        self.calls.append((progress_, total, message))


async def test_progress_brackets_the_run():
    sink = Sink()
    async with progress.reporting(sink, "consulting", 180.0):
        pass

    assert [c[2] for c in sink.calls] == [
        "consulting: started",
        "consulting: finished after 0s",
    ]
    assert all(c[1] == 180.0 for c in sink.calls)


async def test_the_heartbeat_speaks_while_the_body_waits():
    sink = Sink()
    async with progress.reporting(sink, "consulting", 1.0, interval_s=0.01):
        await asyncio.sleep(0.05)

    beats = [c[2] for c in sink.calls if "elapsed" in (c[2] or "")]
    assert beats, sink.calls
    assert "of 1s elapsed" in beats[0]


async def test_a_step_reports_from_anywhere_inside_the_call():
    sink = Sink()

    async def four_frames_down() -> None:
        await progress.step("2 of 5 reviewers answered", 2, 5)

    async with progress.reporting(sink, "review"):
        # A task, because the reviewer fan-out is one: the ContextVar has to be
        # inherited rather than looked up on the calling frame.
        await asyncio.create_task(four_frames_down())

    assert ("2 of 5 reviewers answered", 2.0, 5.0) in [(c[2], c[0], c[1]) for c in sink.calls]


async def test_a_step_outside_a_tool_call_is_a_no_op():
    # Every service here is also callable directly, by the smoke scripts and by the
    # dashboard. None of them has a Context.
    await progress.step("nobody is listening")


async def test_a_failing_sink_does_not_fail_the_run():
    class Broken:
        async def report_progress(self, progress_, total=None, message=None):
            raise RuntimeError("the client went away")

    async with progress.reporting(Broken(), "consulting", 5.0):
        await progress.step("still working")


async def test_no_context_means_the_body_still_runs():
    ran = False
    async with progress.reporting(None, "consulting", 5.0):
        ran = True
    assert ran


# --- the schema stays clean -------------------------------------------------


async def test_the_context_parameter_is_not_advertised(host_claude):
    """`orchestrator_consult` builds its signature by hand from the request model.

    The SDK strips a `Context` parameter by annotation, but that path is unusual
    enough to assert directly rather than to rely on the golden snapshot alone."""
    tools = await build_server(
        {
            "consult": consult_block(
                review={
                    "reviewers": ["codex-sol"],
                    "deep_reviewers": ["codex-sol", "claude-opus"],
                },
                workflow={"bindings": {"research": {"agent": "codex-sol"}}},
            )
        }
    ).list_tools()
    by_name = {t.name: t for t in tools}

    for name in (
        "orchestrator_consult",
        "orchestrator_review_run",
        "orchestrator_retry_review",
        "orchestrator_workflow_run_step",
    ):
        assert "ctx" not in by_name[name].input_schema.get("properties", {}), name
        assert "ctx" not in (by_name[name].input_schema.get("required") or []), name


# --- what the second review found -------------------------------------------


def test_a_credential_in_a_traceback_is_masked(fresh_logger, capsys, monkeypatch):
    """The escape a `logging.Filter` cannot close.

    A filter only ever sees `record.msg`. `Formatter.format` appends `exc_text`
    afterwards, so one `exc_info=True` on an error whose traceback quotes a token
    walks straight past a filter that already approved the message."""
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    logmod.configure()
    try:
        raise AdapterError(ConsultErrorCode.TRANSPORT_ERROR, f"--token {SECRET}")
    except AdapterError:
        logmod.get_logger("orchestrator_mcp.probe").warning("child died", exc_info=True)

    err = capsys.readouterr().err
    assert "Traceback" in err
    assert SECRET not in err


def test_a_stdout_handler_on_the_package_logger_is_dropped(fresh_logger, capsys, monkeypatch):
    """`propagate = False` stops the root logger and nothing attached directly here.

    An embedder that configured `orchestrator_mcp` itself keeps its handler, and if
    that handler's stream is stdout every line it writes is framed as an MCP message.
    Removing it is rude; it is also the only outcome where the server still works."""
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    fresh_logger.addHandler(logging.StreamHandler(sys.stdout))
    logmod.configure()
    logmod.get_logger("orchestrator_mcp.probe").warning("a visible line")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "a visible line" in captured.err


def test_a_handler_pointed_somewhere_else_is_left_alone(fresh_logger):
    """Only stdout, and only by identity. A file or a socket is somebody's choice."""
    theirs = logging.StreamHandler(io.StringIO())
    fresh_logger.addHandler(theirs)
    logmod.configure()

    assert theirs in fresh_logger.handlers


def test_the_record_is_left_intact_for_other_handlers(fresh_logger, monkeypatch):
    """Redaction formats, it does not rewrite.

    The old filter set `record.msg` and cleared `record.args` on an object shared
    with every other handler on the logger, so what a second sink wrote depended on
    handler order. Our own sink is what this module guarantees."""
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    logmod.configure()
    seen: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    fresh_logger.addHandler(Capture())
    logmod.get_logger("orchestrator_mcp.probe").warning("pid=%d rc=%d", 4321, 0)

    assert seen[0].args == (4321, 0)


async def test_a_sink_that_never_answers_does_not_stall_the_run(monkeypatch):
    """Suppressing exceptions was not enough: a `report_progress` that never returns
    is not a failure, it is a hang -- and the start notification is awaited *before*
    the body runs."""
    monkeypatch.setattr(progress, "EMIT_TIMEOUT_S", 0.05)
    ran = False

    class Wedged:
        async def report_progress(self, progress_, total=None, message=None):
            await asyncio.sleep(3600)

    async with progress.reporting(Wedged(), "consulting"):
        ran = True

    assert ran


# --- what the third review found --------------------------------------------


def test_the_context_parameter_is_still_injectable():
    """The other half of the schema test, and the half that fails silently.

    `_tool_signature` annotates `ctx` as `Context | None` rather than `Context`, and
    the SDK decides both injection *and* schema exclusion from that annotation. A
    widening it did not recognise would keep every schema assertion green while no
    tool ever received a context again -- so this asserts against the SDK's own
    predicate. If that private helper is renamed, this failing is the point: the
    contract it stands for is the thing worth knowing about."""
    from mcp.server.mcpserver.resolve import _is_context_annotation

    from orchestrator_mcp.consult.contract import ConsultRequest, ConsultResponse
    from orchestrator_mcp.server import _tool_signature

    ctx = _tool_signature(ConsultRequest, ConsultResponse, context=True).parameters["ctx"]

    assert _is_context_annotation(ctx.annotation)
    assert ctx.default is None


def test_a_stdout_handler_on_a_child_logger_is_dropped(fresh_logger, capsys, monkeypatch):
    """A child's handlers run before the record ever reaches the package logger.

    Checking only `orchestrator_mcp` leaves the one sink that gets first look at
    every record from `orchestrator_mcp.consult`."""
    child = logging.getLogger("orchestrator_mcp.consult")
    kept = child.handlers[:]
    child.handlers.clear()
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    child.addHandler(logging.StreamHandler(sys.stdout))
    try:
        logmod.configure()
        child.warning("a visible line")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "a visible line" in captured.err
    finally:
        child.handlers[:] = kept


def test_a_handler_holding_the_real_stdout_is_dropped(fresh_logger, monkeypatch):
    """Identity against `sys.stdout` alone misses the interesting cases.

    A handler built from `sys.__stdout__`, or a file opened on `/dev/stdout`, holds
    a different object and the same descriptor -- and the descriptor is what the
    MCP peer is reading."""
    monkeypatch.setenv(logmod.LEVEL_ENV, "DEBUG")
    theirs = logging.StreamHandler(sys.__stdout__)
    fresh_logger.addHandler(theirs)
    logmod.configure()

    assert theirs not in fresh_logger.handlers


async def test_an_outer_cancellation_is_not_swallowed_by_the_emit_timeout(monkeypatch):
    """`asyncio.timeout` cancels the current task to fire, which is the same
    mechanism an outer cancellation uses. The final emission runs in a `finally`,
    so the two meet there -- and a `TimeoutError` claimed from somebody else's
    cancel would leave a cancelled tool call running.

    The bound is what makes this terminate at all: teardown waits out
    `EMIT_TIMEOUT_S` against a sink that will not answer, and only then does the
    cancellation resume. Real value, real wait."""
    monkeypatch.setattr(progress, "EMIT_TIMEOUT_S", 0.05)

    class WedgesOnTheWayOut:
        """Answers the start notification, hangs on the one in the `finally`."""

        def __init__(self) -> None:
            self.calls = 0

        async def report_progress(self, progress_, total=None, message=None):
            self.calls += 1
            if self.calls > 1:
                await asyncio.sleep(3600)

    async def body():
        async with progress.reporting(WedgesOnTheWayOut(), "consulting"):
            await asyncio.sleep(3600)

    task = asyncio.create_task(body())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
