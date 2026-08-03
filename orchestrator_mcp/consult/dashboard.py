"""A read-only local view of what has been consulted.

Stdlib `http.server`, no framework and no optional dependency group: this serves a
handful of tables over rows that are already on disk, and a web framework would be
a install-time cost for every operator to pay for a page they may never open.

Read-only in the strongest sense available. Its own SQLite connection, opened
`mode=ro`, so a bug here cannot write to the consultation store; only GET; no path
reaches the filesystem; and YAML is never edited, which keeps the config file the
single source of truth and this page a window rather than a second one.

Everything it displays -- prompts, documents, answers -- is untrusted text that
arrived from a caller or from a consulted agent, so every value is escaped on the
way out. And it binds loopback only, checks the Host header to survive a DNS
rebind, and never runs a login command: the connect commands on the page are text
for the operator to copy, not buttons.
"""

from __future__ import annotations

import html
import json
import sqlite3
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version as _distribution_version
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..contract import ConfigError
from .adapters import adapter_for
from .config import ConsultConfig, load_consult_config

# A browser can be pointed at a name that resolves to 127.0.0.1, which is how a
# page on the internet reaches a loopback service. The bind protects the network;
# this protects the browser.
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 76rem;
       padding: 0 1rem; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1.5rem; }
th, td { border-bottom: 1px solid #8883; padding: .35rem .5rem; text-align: left;
         vertical-align: top; }
th { font-weight: 600; opacity: .7; font-size: .85rem; }
code, pre { font-family: ui-monospace, monospace; font-size: .85rem; }
pre { background: #8881; padding: .6rem .8rem; border-radius: 4px; overflow-x: auto;
      white-space: pre-wrap; word-break: break-word; max-height: 26rem; }
.meta { opacity: .65; font-size: .85rem; }
.bad { color: #c0392b; font-weight: 600; }
.ok { color: #1e8449; }
a { color: inherit; }
"""


def _version() -> str:
    try:
        return _distribution_version("orchestrator-mcp-server")
    except PackageNotFoundError:
        return "0+unknown"


class ConsultDashboard:
    """Renders pages. Holds no connection between requests -- one per request, closed
    with it, so the page cannot hold a handle on a database the operator has moved."""

    def __init__(self, config: ConsultConfig) -> None:
        self.config = config
        self.version = _version()

    # --- data ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # `mode=ro` at the driver, not by convention: this process has no business
        # being able to write, and the URI is what makes that true rather than
        # intended.
        connection = sqlite3.connect(
            f"file:{self.config.database_path}?mode=ro", uri=True, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _query(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        if not Path(self.config.database_path).exists():
            return []
        with self._connect() as connection:
            return connection.execute(sql, parameters).fetchall()

    # --- pages --------------------------------------------------------------

    def page(self, path: str) -> tuple[int, str]:
        if path == "/":
            return HTTPStatus.OK, self.index()
        if path.startswith("/consultation/"):
            return self.consultation(unquote(path.removeprefix("/consultation/")))
        return HTTPStatus.NOT_FOUND, _document("Not found", "<p>No such page.</p>")

    def index(self) -> str:
        return _document(
            "Consultations",
            f"<h1>Consult Protocol v1</h1><p class=meta>orchestrator-mcp-server {_e(self.version)}"
            f" &middot; {_e(str(self.config.database_path))}</p>"
            f"<h2>Agents</h2>{self._agents_table()}"
            f"<h2>Consultations</h2>{self._consultations_table()}",
        )

    def _agents_table(self) -> str:
        latest = {
            row["agent_id"]: row
            for row in self._query(
                "SELECT agent_id, installed, authenticated, detail, checked_at "
                "FROM agent_status_checks ORDER BY id"
            )
        }
        rows = []
        for agent_id, agent in sorted(self.config.agents.items()):
            status = latest.get(agent_id)
            scores = ", ".join(f"{k} {v}" for k, v in sorted(agent.scores.items())) or "--"
            rows.append(
                "<tr>"
                f"<td><code>{_e(agent_id)}</code></td>"
                f"<td>{_e(agent.runtime)}</td>"
                f"<td><code>{_e(agent.model)}</code></td>"
                f"<td>{agent.priority}</td>"
                f"<td>{'yes' if agent.enabled else 'no'}</td>"
                f"<td>{_e(scores)}</td>"
                f"<td>{'yes' if agent.web_search else 'no'}</td>"
                f"<td>{_status_cell(status)}</td>"
                # Text to copy, never a button: logging a runtime in is the operator's
                # action on their own account, and this server has no part in it.
                f"<td><code>{_e(adapter_for(agent, self.config).connect_command(agent))}</code></td>"
                "</tr>"
            )
        head = (
            "<tr><th>agent<th>runtime<th>model<th>priority<th>enabled<th>scores"
            "<th>web<th>last status<th>connect with</tr>"
        )
        return f"<table>{head}{''.join(rows)}</table>"

    def _consultations_table(self) -> str:
        rows = self._query(
            "SELECT c.id, c.created_at, c.updated_at, c.target_agent_id, c.target_model, "
            "c.capability, c.conversation_label, c.status, "
            "(SELECT COUNT(*) FROM consultation_turns t WHERE t.consultation_id = c.id) AS turns, "
            "(SELECT COUNT(*) FROM consultation_turns t WHERE t.consultation_id = c.id "
            " AND t.error_code IS NOT NULL) AS failures "
            "FROM consultations c ORDER BY c.created_at DESC LIMIT 200"
        )
        if not rows:
            return "<p class=meta>No consultations recorded yet.</p>"

        body = "".join(
            "<tr>"
            f"<td><a href='/consultation/{_e(row['id'])}'><code>{_e(row['id'][:8])}</code></a></td>"
            f"<td class=meta>{_e(row['created_at'])}</td>"
            f"<td><code>{_e(row['target_agent_id'])}</code></td>"
            f"<td><code>{_e(row['target_model'])}</code></td>"
            f"<td>{_e(row['capability'])}</td>"
            f"<td>{_e(row['conversation_label'] or '')}</td>"
            f"<td>{row['turns']}</td>"
            f"<td class={'bad' if row['failures'] else 'ok'}>{row['failures']}</td>"
            f"<td>{_e(row['status'])}</td>"
            "</tr>"
            for row in rows
        )
        head = (
            "<tr><th>id<th>started<th>agent<th>model<th>capability<th>label"
            "<th>turns<th>failed<th>status</tr>"
        )
        return f"<table>{head}{body}</table>"

    def consultation(self, consultation_id: str) -> tuple[int, str]:
        rows = self._query("SELECT * FROM consultations WHERE id = ?", (consultation_id,))
        if not rows:
            return HTTPStatus.NOT_FOUND, _document(
                "Not found", "<p>No such consultation.</p><p><a href='/'>Back</a></p>"
            )
        consultation = rows[0]
        return HTTPStatus.OK, _document(
            f"Consultation {consultation_id[:8]}",
            f"<p><a href='/'>&larr; all consultations</a></p>"
            f"<h1>{_e(consultation['capability'])} &rarr; "
            f"<code>{_e(consultation['target_agent_id'])}</code></h1>"
            f"<p class=meta>{_e(consultation['id'])} &middot; "
            f"model <code>{_e(consultation['target_model'])}</code> &middot; "
            f"runtime {_e(consultation['target_runtime'])} &middot; "
            f"asked by {_e(consultation['origin_runtime'])} &middot; "
            f"{_e(consultation['protocol_version'])} &middot; "
            f"config {_e(consultation['config_hash'])} &middot; "
            f"{_e(consultation['created_at'])}</p>"
            # The native session id is not shown: it is the consulted CLI's handle on
            # a live session, and a page has no use for it.
            f"<p class=meta>native session "
            f"{'bound' if consultation['native_session_id'] else 'not bound'}</p>"
            f"<h2>Routing</h2>{self._routing(consultation_id)}"
            f"<h2>Turns</h2>{self._turns(consultation_id)}",
        )

    def _routing(self, consultation_id: str) -> str:
        rows = self._query(
            "SELECT capability, selected_agent, explicit, excluded_json, error_code, created_at "
            "FROM routing_decisions WHERE consultation_id = ? ORDER BY id",
            (consultation_id,),
        )
        if not rows:
            return "<p class=meta>No routing decision recorded.</p>"

        body = ""
        for row in rows:
            excluded = json.loads(row["excluded_json"])
            reasons = "".join(
                f"<li><code>{_e(item['agent_id'])}</code> &mdash; {_e(item['reason'])}</li>"
                for item in excluded
            )
            body += (
                f"<p>selected <code>{_e(row['selected_agent'] or 'none')}</code>"
                f"{' (explicitly named)' if row['explicit'] else ' (by score)'}"
                + (f" &middot; <span class=bad>{_e(row['error_code'])}</span>"
                   if row["error_code"] else "")
                + "</p>"
                + (f"<p class=meta>not considered:</p><ul>{reasons}</ul>" if reasons else "")
            )
        return body

    def _turns(self, consultation_id: str) -> str:
        rows = self._query(
            "SELECT * FROM consultation_turns WHERE consultation_id = ? ORDER BY sequence_number",
            (consultation_id,),
        )
        if not rows:
            return "<p class=meta>No turns recorded.</p>"

        sections = []
        for row in rows:
            cost = f"${row['cost_usd']:.5f}" if row["cost_usd"] is not None else "--"
            header = (
                f"<h3>Turn {row['sequence_number']} &middot; {_e(row['source_mode'])}</h3>"
                f"<p class=meta>{row['latency_ms']} ms &middot; "
                f"{row['input_tokens']} in / {row['output_tokens']} out &middot; {cost} &middot; "
                f"{_e(row['created_at'])}"
                + (f" &middot; <span class=bad>{_e(row['error_code'])}</span>"
                   if row["error_code"] else "")
                + "</p>"
            )
            sections.append(
                header
                + _block("Prompt", row["user_prompt"])
                + _block("Context", row["context"])
                + _block("Compiled prompt", row["compiled_prompt"])
                + _block("Answer", _pretty(row["validated_response_json"]))
                + _block("Raw output", row["raw_output"])
            )
        return "".join(sections)


# --- rendering helpers ------------------------------------------------------


def _e(value: object) -> str:
    """Escape. Every value on this page came from a caller or a consulted agent."""
    return html.escape(str(value), quote=True)


def _block(label: str, value: str | None) -> str:
    if not value:
        return ""
    return f"<p class=meta>{_e(label)}</p><pre>{_e(value)}</pre>"


def _pretty(value: str | None) -> str | None:
    if not value:
        return value
    try:
        return json.dumps(json.loads(value), indent=2, ensure_ascii=False)
    except ValueError:
        return value


def _status_cell(row: sqlite3.Row | None) -> str:
    if row is None:
        return "<span class=meta>never checked</span>"
    if row["installed"] and row["authenticated"]:
        return f"<span class=ok>ready</span> <span class=meta>{_e(row['checked_at'])}</span>"
    label = "not installed" if not row["installed"] else "not connected"
    detail = f" &middot; {_e(row['detail'])}" if row["detail"] else ""
    return f"<span class=bad>{label}</span><span class=meta>{detail}</span>"


def _document(title: str, body: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)} &middot; orchestrator-mcp</title>"
        f"<style>{STYLE}</style>{body}"
    )


# --- server -----------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    dashboard: ConsultDashboard
    server_version = "orchestrator-mcp-dashboard"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 -- the stdlib's spelling
        if not self._host_allowed():
            self._respond(HTTPStatus.FORBIDDEN, _document("Forbidden", "<p>Loopback only.</p>"))
            return
        status, body = self.dashboard.page(urlparse(self.path).path)
        self._respond(status, body)

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS or host == ""

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # Nothing on this page should be cached, embedded, or allowed to fetch
        # anything: it is a local window onto stored prompts.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        """Silent by default: a request line would put consultation ids in a log
        nobody asked for."""


def build_httpd(config: ConsultConfig) -> ThreadingHTTPServer:
    if config.dashboard.host not in ALLOWED_HOSTS:
        # The config validator already refuses this; kept because the bind is the
        # thing that actually matters and it should refuse on its own terms.
        raise ConfigError(f"dashboard.host must be a loopback address, not {config.dashboard.host}")

    handler = type("_BoundHandler", (_Handler,), {"dashboard": ConsultDashboard(config)})
    return ThreadingHTTPServer((config.dashboard.host, config.dashboard.port), handler)


def main() -> int:
    # Imported here rather than at module scope: `load_config` lives in the module
    # that builds the LiteLLM router, and a dashboard should not pay for that import
    # to print a usage error.
    from ..server import load_config

    consult = load_consult_config(load_config())
    if consult is None:
        print("no `consult:` block in the config -- nothing to show", file=sys.stderr)
        return 2
    if not consult.dashboard.enabled:
        print(
            "dashboard is disabled; set `consult.dashboard.enabled: true` in the config.\n"
            "It serves every stored prompt and answer, so it is opt-in.",
            file=sys.stderr,
        )
        return 2

    httpd = build_httpd(consult)
    host, port = httpd.server_address[0], httpd.server_address[1]
    print(f"read-only dashboard on http://{host}:{port} -- ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
