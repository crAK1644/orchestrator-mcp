"""The read-only dashboard, driven over real HTTP.

Served on port 0 in a thread and fetched with `http.client`, because the things
worth proving here are properties of the server and not of the render function: that
it will not write, will not serve a non-loopback Host, and does not leak a native
session id onto a page.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest

from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.dashboard import ConsultDashboard, build_httpd
from orchestrator_mcp.contract import ConfigError

from .conftest import consult_block
from .test_consult_service import StubAdapter, StubService


@pytest.fixture
def config(tmp_path):
    def build(**overrides):
        config = ConsultConfig(
            **consult_block(database_path=str(tmp_path / "consultations.sqlite3"), **overrides)
        )
        config.dashboard.enabled = True
        # Assignment is not re-validated by pydantic here, which is what lets the
        # test ask the OS for a free port instead of guessing one.
        config.dashboard.port = 0
        return config

    return build


class _Client:
    """Callable, so `get, config = serve()` keeps reading as a GET function, with
    `post` and `token` hanging off it for the tests that write."""

    def __init__(self, port: int, dashboard) -> None:
        self.port = port
        self.dashboard = dashboard

    def _request(self, method: str, path: str, headers: dict, body: str | None = None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        text = response.read().decode()
        location = response.getheader("Location")
        connection.close()
        return response.status, text, location

    def __call__(self, path: str, host: str | None = None):
        status, body, _ = self._request(
            "GET", path, {"Host": host or f"127.0.0.1:{self.port}"}
        )
        return status, body

    @property
    def token(self) -> str:
        return self.dashboard.token

    def post(self, path: str, form: dict, host: str | None = None, origin: str | None = None):
        """Returns (status, body, location) -- a save answers 303 with an empty body,
        so the redirect target is the only thing that says it worked."""
        body = urlencode(form)
        headers = {
            "Host": host or f"127.0.0.1:{self.port}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        }
        if origin:
            headers["Origin"] = origin
        return self._request("POST", path, headers, body)


@pytest.fixture
def serve(config):
    """A running dashboard; returns a client and the config it serves."""
    servers = []

    def start(consult_config=None, config_path=None):
        consult_config = consult_config or config()
        httpd = build_httpd(consult_config, config_path)
        servers.append(httpd)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return _Client(httpd.server_address[1], httpd.RequestHandlerClass.dashboard), consult_config

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


async def consult(consult_config, **kwargs):
    """Produce a real consultation in the store the dashboard will read."""
    service = await StubService(consult_config, "claude", adapter=StubAdapter()).open()
    try:
        return await service.consult(**kwargs)
    finally:
        await service.store.close()


# --- the index --------------------------------------------------------------


async def test_the_index_lists_agents_before_anything_has_been_consulted(serve):
    """A fresh install has no database yet, and the page is still the useful one."""
    get, _ = serve()
    status, body = get("/")

    assert status == 200
    assert "codex-sol" in body and "claude-opus" in body
    assert "No consultations recorded yet." in body


async def test_the_index_shows_the_command_that_connects_an_agent(serve):
    get, _ = serve()
    _, body = get("/")
    # Text to copy. The dashboard never runs it -- a GET that logs a CLI in would be
    # a side effect on the user's account.
    assert "codex login" in body
    assert "claude auth login" in body


async def test_the_index_reports_the_last_status_check_not_a_fresh_probe(serve):
    get, config = serve()
    service = await StubService(config, "claude", adapter=StubAdapter()).open()
    await service.store.record_status_check("codex-sol", installed=True, authenticated=False,
                                            detail="not logged in")
    await service.store.close()

    _, body = get("/")
    assert "not connected" in body and "not logged in" in body


async def test_the_monitor_strip_counts_turns_and_never_calls_a_stored_row_active(serve):
    """A consultation is `open` for as long as its row exists, so a tile counting the
    ones that had not closed counted every consultation ever made and labelled the
    total Active -- an idle server reading as fully busy. Turns are the work."""
    get, config = serve()
    first = await consult(config, capability="coding", prompt="q")
    await consult(
        config, capability="coding", prompt="again", consultation_id=first.consultation_id
    )

    _, body = get("/")
    assert "<dt>Consultations</dt><dd>1</dd>" in body
    assert "<dt>Turns / failed</dt><dd>2 / 0</dd>" in body
    assert "<dt>Active</dt>" not in body
    # No `review:` block and no review table: a permanent zero is the same lie small.
    assert "Reviews open" not in body
    # And the row badge does not say it either. `open` is every consultation there
    # has ever been, so in the signal colour the table reads as a queue of live work.
    assert "status--muted'>open" in body
    assert "status--active'>open" not in body


async def test_a_consultation_appears_in_the_index_with_its_agent_and_model(serve):
    get, config = serve()
    response = await consult(config, capability="coding", prompt="q")

    _, body = get("/")
    assert str(response.consultation_id)[:8] in body
    assert "gpt-5.6-sol" in body
    assert "coding" in body


# --- the detail page --------------------------------------------------------


async def test_the_detail_page_shows_the_whole_turn(serve):
    get, config = serve()
    response = await consult(
        config, capability="coding", prompt="what colour is the sky", context="the sky is blue"
    )

    status, body = get(f"/consultation/{response.consultation_id}")
    assert status == 200
    assert "what colour is the sky" in body  # prompt
    assert "the sky is blue" in body  # context
    assert "Compiled prompt" in body
    assert "blue" in body  # the answer
    assert "codex-sol" in body


async def test_the_detail_page_shows_the_routing_decision_and_who_was_excluded(serve):
    get, config = serve()
    response = await consult(config, capability="coding", prompt="q")

    _, body = get(f"/consultation/{response.consultation_id}")
    assert "selected" in body and "codex-sol" in body
    # The host runtime was excluded from its own routing, and the page says why.
    assert "claude-opus" in body


async def test_a_failed_turn_shows_its_error_code(serve):
    from orchestrator_mcp.consult.adapters.base import AdapterError
    from orchestrator_mcp.consult.errors import ConsultErrorCode

    get, config = serve()
    service = await StubService(
        config,
        "claude",
        adapter=StubAdapter(error=AdapterError(ConsultErrorCode.TIMEOUT, "never answered")),
    ).open()
    response = await service.consult(capability="coding", prompt="q")
    await service.store.close()

    _, body = get(f"/consultation/{response.consultation_id}")
    assert "timeout" in body


async def test_the_native_session_id_never_reaches_the_page(serve):
    """It is the consulted CLI's handle on a live session; a page has no use for it."""
    get, config = serve()
    response = await consult(config, capability="coding", prompt="q")

    _, body = get(f"/consultation/{response.consultation_id}")
    assert "bound" in body
    assert str(response.consultation_id) in body  # our id, which is fine
    assert "native-1" not in body


async def test_an_unknown_consultation_is_a_404_not_a_blank_page(serve):
    get, _ = serve()
    status, body = get("/consultation/11111111-2222-3333-4444-555555555555")
    assert status == 404
    assert "No such consultation" in body


async def test_an_unknown_path_is_a_404(serve):
    get, _ = serve()
    status, _ = get("/../../etc/passwd")
    assert status == 404


# --- what it refuses to do --------------------------------------------------


async def test_stored_prompts_are_escaped_on_the_way_out(serve):
    """Everything on this page arrived from a caller or a consulted agent."""
    get, config = serve()
    response = await consult(
        config, capability="coding", prompt="<script>alert('x')</script>", context="<b>hi</b>"
    )

    _, body = get(f"/consultation/{response.consultation_id}")
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body
    assert "<b>hi</b>" not in body


async def test_the_dashboard_connection_cannot_write(serve):
    get, config = serve()
    await consult(config, capability="coding", prompt="q")

    dashboard = ConsultDashboard(config)
    with dashboard._connect() as connection, pytest.raises(sqlite3.OperationalError) as exc:
        connection.execute("DELETE FROM consultations")
    assert "readonly" in str(exc.value)


async def test_a_foreign_host_header_is_refused(serve):
    """A page on the internet can point a name at 127.0.0.1; the bind cannot see
    that, but the Host header can."""
    get, _ = serve()
    status, body = get("/", host="dashboard.example.com")
    assert status == 403
    assert "Loopback only" in body


@pytest.mark.parametrize(
    "host, allowed",
    [
        pytest.param("127.0.0.1:8765", True, id="the ordinary one"),
        pytest.param("localhost", True, id="no port"),
        pytest.param("[::1]:8765", True, id="v6 with a port"),
        pytest.param("[::1]", True, id="v6 without one"),
        pytest.param("::1", True, id="v6 unbracketed"),
        pytest.param("evil.example.com:8765", False, id="a name that resolves here"),
        pytest.param("[::ffff:127.0.0.1]", False, id="v6 spelling of a v4 address"),
        # Everything after the bracket was discarded, so these read as `[::1]`.
        pytest.param("[::1]garbage", False, id="v6 with something after the bracket"),
        pytest.param("[::1]:evil", False, id="v6 with a port that is not a number"),
        pytest.param("127.0.0.1:evil", False, id="a port that is not a number"),
        # `str.isdigit` is true of every numeral in Unicode. Most of them cannot reach
        # a server -- headers are latin-1 and `http.client` refuses to encode them --
        # but the superscripts are in latin-1, so this one arrives.
        pytest.param("127.0.0.1:8765²", False, id="a port in digits that are not ascii"),
    ],
)
async def test_the_host_header_is_read_the_way_a_host_header_is_written(serve, host, allowed):
    """`rsplit(':', 1)` splits inside an IPv6 address, so `[::1]` used to be refused
    while `[::1]:8765` was allowed -- backwards, and the config permits `::1`."""
    get, _ = serve()
    status, _ = get("/", host=host)
    assert (status == 200) is allowed, host


async def test_a_request_with_no_host_header_is_refused(serve):
    """Absent used to pass. Fail closed: a request that will not say what it was
    addressed to is the one this check exists for."""
    get, _ = serve()
    connection = HTTPConnection("127.0.0.1", get.port, timeout=5)
    connection.putrequest("GET", "/", skip_host=True)
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 403
    connection.close()


async def test_it_serves_get_and_post_and_nothing_else(serve):
    """No handler for any other verb, which is how the stdlib refuses one: 501. POST
    exists only for the agents form; PUT and DELETE would be a second write surface to
    keep safe for no gain."""
    from orchestrator_mcp.consult.dashboard import _Handler

    assert sorted(name for name in vars(_Handler) if name.startswith("do_")) == [
        "do_GET",
        "do_POST",
    ]


async def test_it_refuses_to_bind_anything_but_loopback(config):
    consult_config = config()
    consult_config.dashboard.host = "0.0.0.0"
    with pytest.raises(ConfigError):
        build_httpd(consult_config)


async def test_the_page_names_the_version_and_the_database(serve):
    get, consult_config = serve()
    _, body = get("/")
    assert "orchestrator-mcp-server" in body
    assert "consultations.sqlite3" in body


# --- how much of the codex subscription is left -----------------------------


def token_count(used_percent: float, **overrides) -> str:
    primary = {"used_percent": used_percent, "window_minutes": 10080, "resets_at": 1786288369}
    limits = {"primary": {**primary, **overrides}, "plan_type": "plus"}
    return json.dumps(
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}
    ) + "\n"


def write_rollout(home, text: str) -> None:
    directory = home / ".codex" / "sessions" / "2026" / "08" / "04"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rollout-2026-08-04T23-16-52-019fce6c-0218-7cb0-9e79-3de60901c110.jsonl").write_text(text)


async def test_the_index_says_how_much_of_the_codex_window_is_spent(serve, tmp_path, monkeypatch):
    """The question people ask before starting a 300-second review is whether they can
    afford one, and the CLI's own session log is the only thing on the machine that
    knows -- there is no API key here to go and ask with."""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_rollout(tmp_path, token_count(23.0))
    get, _ = serve()

    _, body = get("/")
    assert "codex usage: 23%" in body
    assert "of the last 7d" in body
    assert "plus plan" in body
    # Stale by construction: it is whatever the last consultation was told, and a page
    # that showed it as current would be claiming a freshness it cannot have.
    assert "as of the last consultation" in body


async def test_nothing_is_claimed_when_no_consultation_has_run(serve, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    get, _ = serve()

    _, body = get("/")
    assert "codex usage" not in body


async def test_a_claude_only_install_is_not_told_about_codex(serve, config, tmp_path, monkeypatch):
    """The number is per-runtime, and an operator with no codex agent has no window to
    have spent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_rollout(tmp_path, token_count(23.0))
    get, _ = serve(
        config(agents={"claude-opus": {"runtime": "claude", "command": "claude", "model": "opus"}})
    )

    _, body = get("/")
    assert "codex usage" not in body


@pytest.mark.parametrize(
    "minutes, expected",
    [
        (10080, "of the last 7d"),
        # The five-hour window some plans quote. Days would round it to `0d`, which is
        # a wrong number rather than a coarse one.
        (300, "of the last 5h"),
        (0, "codex usage: 23% used"),
        ("weekly", "codex usage: 23% used"),
    ],
)
async def test_the_window_is_reported_in_a_unit_it_fits(serve, tmp_path, monkeypatch, minutes, expected):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_rollout(tmp_path, token_count(23.0, window_minutes=minutes))
    get, _ = serve()

    _, body = get("/")
    assert expected in body


@pytest.mark.parametrize("resets", [10**12, -(10**12), "friday", None])
async def test_a_reset_time_that_is_not_one_is_left_out_not_raised(
    serve, tmp_path, monkeypatch, resets
):
    """The epoch comes from a file this server does not own, and a page whose job is to
    stay readable cannot answer 500 because a number in it was implausible."""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_rollout(tmp_path, token_count(23.0, resets_at=resets))
    get, _ = serve()

    status, body = get("/")
    assert status == 200
    assert "codex usage: 23%" in body
    assert "resets" not in body
