"""The read-only dashboard, driven over real HTTP.

Served on port 0 in a thread and fetched with `http.client`, because the things
worth proving here are properties of the server and not of the render function: that
it will not write, will not serve a non-loopback Host, and does not leak a native
session id onto a page.
"""

from __future__ import annotations

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

    def start(consult_config=None):
        consult_config = consult_config or config()
        httpd = build_httpd(consult_config)
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
