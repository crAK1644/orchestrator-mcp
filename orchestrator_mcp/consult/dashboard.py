"""A local view of what has been consulted, and a form for configuring who can be.

Stdlib `http.server`, no framework and no optional dependency group: this serves a
handful of tables over rows that are already on disk, and a web framework would be
a install-time cost for every operator to pay for a page they may never open.

The consultation store stays read-only in the strongest sense available: its own
SQLite connection, opened `mode=ro`, so a bug here cannot write a row. What this page
*can* write is one file, `managed.py`'s agents file, and only when
`dashboard.editable` is on -- a second opt-in, because turning a viewer on and
turning an editor on are different things to agree to. `config.yaml` is never
written, so the operator's own file and its comments are not something a click can
reformat.

Everything it displays -- prompts, documents, answers, and now agent ids someone
typed -- is untrusted text, so every value is escaped on the way out. It binds
loopback only and checks the Host header to survive a DNS rebind; writes need a
per-process token as well, since a browser reaches loopback as happily as anything
else. And it still never runs a login command: the connect commands on the page are
text for the operator to copy, not buttons.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import sqlite3
import sys
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version as _distribution_version
from pathlib import Path
from typing import Any, get_args
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import ValidationError

from ..contract import ConfigError
from .adapters import adapter_for
from .adapters.base import AdapterError, resolve_command
from .config import AgentConfig, ConsultConfig, load_consult_config
from .contract import Capability
from .managed import read_managed, write_managed

CAPABILITIES = get_args(Capability)
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Conservative on purpose: an agent id ends up in a file name's neighbourhood, in a
# URL, and in an MCP tool's advertised enum. Nothing here needs to be more exciting.
AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# A form of this shape is a few hundred bytes. The cap is here so a request cannot ask
# this process to allocate whatever it likes before anything has been checked.
MAX_BODY_BYTES = 64 * 1024

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
label { display: block; margin: .6rem 0; }
label > span { display: block; font-size: .85rem; opacity: .7; margin-bottom: .15rem; }
input[type=text], input[type=number], select { font: inherit; padding: .3rem .4rem;
       min-width: 18rem; max-width: 100%; }
input[type=number] { min-width: 5rem; }
fieldset { border: 1px solid #8883; border-radius: 4px; margin: 1rem 0; padding: .5rem 1rem 1rem; }
legend { font-size: .85rem; opacity: .7; padding: 0 .3rem; }
.scores label { display: inline-block; margin-right: 1rem; }
button { font: inherit; padding: .35rem .9rem; border-radius: 4px; cursor: pointer; }
.banner { border: 1px solid #8884; border-left: 3px solid currentColor; border-radius: 4px;
       padding: .6rem .8rem; margin: 1rem 0; }
.banner.warn { color: #b9770e; } .banner.done { color: #1e8449; }
.error { color: #c0392b; margin: .5rem 0; }
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
        # One per process, embedded in every form and required by every write. A page
        # on the internet can aim a form post at 127.0.0.1, but it cannot read this out
        # of a response it is not allowed to see -- which is what makes the token, and
        # not the loopback bind, the thing protecting the write.
        self.token = secrets.token_urlsafe(32)
        # Every write to the managed file is a read, an edit and a write back, and
        # `ThreadingHTTPServer` gives each request its own thread. See `_rewrite`.
        self._writing = threading.Lock()

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

    def page(self, path: str, query: str = "") -> tuple[int, str]:
        if path == "/":
            return HTTPStatus.OK, self.index()
        if path.startswith("/consultation/"):
            return self.consultation(unquote(path.removeprefix("/consultation/")))
        if path == "/agents":
            return self._if_editable(lambda: (HTTPStatus.OK, self.agents_page(query)))
        if path == "/agents/new":
            return self._if_editable(lambda: (HTTPStatus.OK, self.agent_form(None)))
        if path.startswith("/agents/"):
            return self._if_editable(
                lambda: self.agent_edit(unquote(path.removeprefix("/agents/")))
            )
        return HTTPStatus.NOT_FOUND, _document("Not found", "<p>No such page.</p>")

    def _if_editable(self, render) -> tuple[int, str]:
        """Editing pages do not exist unless editing is on.

        403 rather than 404: an operator who reached this page followed a link that the
        config turned off, and "no such page" would send them looking for a typo."""
        if not self.config.dashboard.editable:
            return HTTPStatus.FORBIDDEN, _document(
                "Read-only",
                "<h1>Read-only</h1><p>This dashboard is not configured for editing. "
                "Set <code>consult.dashboard.editable: true</code> to change agents "
                "from the browser.</p><p><a href='/'>Back</a></p>",
            )
        return render()

    def index(self) -> str:
        link = (
            " &middot; <a href='/agents'>configure agents</a>"
            if self.config.dashboard.editable
            else ""
        )
        return _document(
            "Consultations",
            f"<h1>Consult Protocol v1</h1><p class=meta>orchestrator-mcp-server {_e(self.version)}"
            f" &middot; {_e(str(self.config.database_path))}{link}</p>"
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


    # --- configuring agents --------------------------------------------------

    def _split_agents(self) -> tuple[dict[str, AgentConfig], dict[str, AgentConfig]]:
        """(editable here, defined in config.yaml). The second kind is shown and not
        touched: this page does not own that file.

        The managed half is re-read from disk on every request rather than taken from
        the config this process loaded at boot. This process is the one writing that
        file, so a save has to be visible on the page that follows it -- otherwise you
        save an agent, land on a table that does not list it, and cannot edit or delete
        it until the dashboard is restarted.
        """
        written = {aid: a for aid, a in self.config.agents.items() if not a.managed}
        try:
            on_disk = read_managed(self.config.managed_agents_path)
            # Reading the file here skips `_label_agents`, which is where a boot refuses
            # a blank id. Without this the page renders a nameless row, offers to edit
            # it, and the server it is configuring will not start.
            unbootable = self._unbootable(on_disk)
            if unbootable:
                raise ConfigError(unbootable)
            managed = {
                aid: AgentConfig(agent_id=aid, managed=True, **_settings(data))
                for aid, data in on_disk.items()
            }
        except (ConfigError, ValidationError, TypeError, OSError, UnicodeDecodeError):
            # A hand-edit can put anything in that file. Falling back to what booted
            # keeps the page readable; the file itself is what the next server start
            # refuses on, and it says why in far more detail than a table cell could.
            managed = {aid: a for aid, a in self.config.agents.items() if a.managed}
        return managed, written

    def _rewrite(
        self, change: Callable[[dict[str, Any]], tuple[int, str] | None]
    ) -> tuple[int, str] | None:
        """Read the managed file, let `change` edit what is in it, write it back.

        One lock around all three, because they are one operation. Each request runs in
        its own thread, so two saves that each read the file before either wrote it
        would leave whichever wrote second having silently dropped the other's agent --
        a save that succeeded, said so, and lost.

        Reading here rather than reusing `_split_agents` is the other half. That method
        falls back to the boot-time config so a broken file still renders a readable
        page; writing that fallback back out would overwrite whatever the operator
        broke, including the part they were about to fix. So a file this process cannot
        read refuses the write and says why, rather than raising in a request thread
        where nothing turns it into a page.

        The lock is this object's, so it covers this dashboard and nothing else. Two
        dashboards on two ports pointed at one managed file would still lose an update
        between them. That is not a supported arrangement -- one file, one process that
        writes it -- and a lock file would only make the unsupported case fail more
        quietly, so what stands in for it is this paragraph.

        Returns None when it wrote, or a status and a message when it did not.
        """
        with self._writing:
            try:
                agents = read_managed(self.config.managed_agents_path)
            # A decode error is not an `OSError`: bytes that are not text read fine and
            # fail at the last step, which is a hand-edit in the wrong encoding and
            # exactly the case this exists to answer.
            except (ConfigError, OSError, UnicodeDecodeError) as exc:
                return HTTPStatus.CONFLICT, str(exc)
            refusal = change(agents)
            if refusal is not None:
                return refusal
            # What is about to be written has to be something the server can boot on.
            # The form cannot produce a bad entry, but it can be asked to save alongside
            # one a hand-edit left there -- and writing that back is this process
            # putting its name on a file that will refuse the next start.
            unbootable = self._unbootable(agents)
            if unbootable:
                return HTTPStatus.CONFLICT, unbootable
            try:
                write_managed(self.config.managed_agents_path, agents)
            except OSError as exc:
                # A readable file in a directory this user cannot write to. The read
                # above says nothing about that, so the failure lands here and has to
                # become a page rather than a traceback in a request thread.
                return HTTPStatus.INTERNAL_SERVER_ERROR, (
                    f"{self.config.managed_agents_path} could not be written: {exc}"
                )
            return None

    def _unbootable(self, agents: dict[str, Any]) -> str | None:
        """Why the next server start would refuse this mapping, or None if it would not.

        The rules a boot applies -- a text id that is not blank, no id also in
        `config.yaml`, and an entry `AgentConfig` accepts -- checked here so that
        neither a page nor a save has to assume the file only ever held what this form
        put in it.
        """
        written = {aid for aid, a in self.config.agents.items() if not a.managed}
        # Sorted by the *string* of the key: a file holding both `1:` and `alpha:` has
        # keys that cannot be compared to each other at all, and a `TypeError` from
        # inside `sorted` would escape every catch below.
        for agent_id, data in sorted(agents.items(), key=lambda item: str(item[0])):
            if not isinstance(agent_id, str) or not agent_id.strip():
                return (
                    "An agent in this file has a blank or non-text id, which the server "
                    "refuses to start on."
                )
            if agent_id in written:
                return (
                    f"`{agent_id}` is defined in config.yaml as well as in this file, and "
                    "the server refuses to start with both. Delete one of the two."
                )
            if not isinstance(data, dict):
                return f"`{agent_id}` in this file is not a mapping of settings."
            try:
                AgentConfig(agent_id=agent_id, **_settings(data))
            except ValidationError as exc:
                return f"`{agent_id}` in this file is not a valid agent: {_first_error(exc)}"
            except TypeError:
                # Keys that are not strings, which `**` refuses.
                return f"`{agent_id}` in this file is not a mapping of settings."
        return None

    def _restart_banner(self) -> str:
        """Say whether the running MCP server is on a different configuration.

        This process cannot ask that server anything -- it is a separate program that
        read its config at boot. But every consultation records the `config_hash` that
        produced it, so the most recent row says what the server was using the last
        time it did any work."""
        rows = self._query(
            "SELECT config_hash FROM consultations ORDER BY created_at DESC LIMIT 1"
        )
        if rows and rows[0]["config_hash"] != self.config.config_hash():
            return (
                "<p class='banner warn'>The last consultation ran on a different "
                "configuration than this file describes. Restart the MCP server (in "
                "Claude Code, restart the app) for changes here to take effect.</p>"
            )
        return ""

    def agents_page(self, query: str = "") -> str:
        params = parse_qs(query)
        saved, deleted = params.get("saved", [None])[0], params.get("deleted", [None])[0]
        notice = ""
        if saved:
            notice = (
                f"<p class='banner done'>Saved <code>{_e(saved)}</code>. It takes effect "
                "when the MCP server next starts.</p>"
            )
        elif deleted:
            notice = (
                f"<p class='banner done'>Deleted <code>{_e(deleted)}</code>. It takes "
                "effect when the MCP server next starts.</p>"
            )

        managed, written = self._split_agents()
        rows = "".join(
            "<tr>"
            f"<td><code>{_e(aid)}</code></td>"
            f"<td>{_e(agent.runtime)}</td>"
            f"<td><code>{_e(agent.model)}</code></td>"
            f"<td>{_e(agent.reasoning_effort or '--')}</td>"
            f"<td>{'yes' if agent.enabled else 'no'}</td>"
            f"<td>{_e(', '.join(f'{k} {v}' for k, v in sorted(agent.scores.items())) or '--')}</td>"
            f"<td><a href='/agents/{_e(aid)}'>edit</a> &middot; "
            f"{_delete_form(aid, self.token)}</td>"
            "</tr>"
            for aid, agent in sorted(managed.items())
        )
        managed_table = (
            "<table><tr><th>agent<th>runtime<th>model<th>effort<th>enabled<th>scores"
            f"<th></tr>{rows}</table>"
            if rows
            else "<p class=meta>No agents configured here yet.</p>"
        )

        read_only = ""
        if written:
            listed = "".join(
                f"<li><code>{_e(aid)}</code> &mdash; {_e(agent.runtime)}, "
                f"<code>{_e(agent.model)}</code></li>"
                for aid, agent in sorted(written.items())
            )
            read_only = (
                "<h2>Defined in config.yaml</h2>"
                "<p class=meta>Edit these where they are written. Naming one of them "
                "here as well is a startup error rather than a merge.</p>"
                f"<ul>{listed}</ul>"
            )

        return _document(
            "Agents",
            "<p><a href='/'>&larr; consultations</a></p><h1>Agents</h1>"
            f"<p class=meta>{_e(str(self.config.managed_agents_path))}</p>"
            f"{notice}{self._restart_banner()}"
            f"{managed_table}"
            "<p><a href='/agents/new'>Add an agent</a></p>"
            f"{read_only}",
        )

    def agent_edit(self, agent_id: str) -> tuple[int, str]:
        managed, written = self._split_agents()
        if agent_id in written:
            return HTTPStatus.FORBIDDEN, _document(
                "Not editable here",
                f"<h1>Not editable here</h1><p><code>{_e(agent_id)}</code> is defined in "
                "config.yaml. Edit it there, or delete it from that file first.</p>"
                "<p><a href='/agents'>Back</a></p>",
            )
        if agent_id not in managed:
            return HTTPStatus.NOT_FOUND, _document(
                "Not found", "<p>No such agent.</p><p><a href='/agents'>Back</a></p>"
            )
        return HTTPStatus.OK, self.agent_form(managed[agent_id])

    def agent_form(
        self,
        agent: AgentConfig | None,
        values: dict[str, str] | None = None,
        error: str = "",
        editing: bool | None = None,
    ) -> str:
        """The form, rendered from an existing agent, or from what was just submitted.

        Re-rendering from the submission is the point of `values`: a save that fails
        validation must come back with what was typed, not with an empty form.

        `editing` is separate from `agent` for that same re-render: a failed edit has no
        stored agent to render from -- what was typed did not validate -- but it is
        still an edit, and coming back as a New agent form with a writable id turns a
        mistyped field into a second agent.
        """
        v = values if values is not None else _as_form(agent)
        editing = (agent is not None) if editing is None else editing
        scores = "".join(
            f"<label><span>{_e(capability)}</span>"
            f"<input type=number name='score.{_e(capability)}' min=0 max=100 "
            f"value='{_e(v.get(f'score.{capability}', ''))}'></label>"
            for capability in CAPABILITIES
        )
        efforts = "".join(
            f"<option value='{_e(level)}'"
            f"{' selected' if v.get('reasoning_effort') == level else ''}>{_e(level)}</option>"
            for level in EFFORTS
        )
        runtimes = "".join(
            f"<option value='{_e(runtime)}'"
            f"{' selected' if v.get('runtime') == runtime else ''}>{_e(runtime)}</option>"
            for runtime in ("codex", "claude")
        )

        return _document(
            "Edit agent" if editing else "New agent",
            "<p><a href='/agents'>&larr; agents</a></p>"
            f"<h1>{'Edit' if editing else 'New'} agent</h1>"
            + (f"<p class=error>{_e(error)}</p>" if error else "")
            + "<form method=post action='/agents'>"
            f"<input type=hidden name=_token value='{_e(self.token)}'>"
            # Which agent this form is replacing, so the save can tell an edit from a
            # new agent that happens to be typed with the same id. `readonly` above is
            # the browser's half of that and is not a check.
            + (f"<input type=hidden name=_editing value='{_e(v.get('id', ''))}'>" if editing else "")
            + f"<label><span>agent id</span><input type=text name=id required "
            f"value='{_e(v.get('id', ''))}'{' readonly' if editing else ''}></label>"
            f"<label><span>runtime</span><select name=runtime>{runtimes}</select></label>"
            "<label><span>command &mdash; a name on PATH, or an absolute path. The Codex "
            "CLI ships inside ChatGPT.app and is not on PATH.</span>"
            f"<input type=text name=command required value='{_e(v.get('command', ''))}'></label>"
            f"<label><span>model</span><input type=text name=model required "
            f"value='{_e(v.get('model', ''))}'></label>"
            "<label><span>reasoning effort &mdash; codex only; unset means the model's "
            "own default, which is not what your ~/.codex/config.toml says</span>"
            f"<select name=reasoning_effort><option value=''>unset</option>{efforts}</select></label>"
            f"<label><span>priority &mdash; lower wins a tie</span>"
            f"<input type=number name=priority min=0 value='{_e(v.get('priority', '100'))}'></label>"
            f"<label><input type=checkbox name=enabled{' checked' if v.get('enabled') else ''}> "
            "enabled</label>"
            f"<label><input type=checkbox name=web_search"
            f"{' checked' if v.get('web_search') else ''}> web search &mdash; required "
            "before a web-mode consultation will route here</label>"
            "<fieldset class=scores><legend>scores &mdash; 0 to 100, blank means never "
            f"route this capability here</legend>{scores}</fieldset>"
            "<button type=submit>Save</button></form>",
        )

    def save(self, form: dict[str, str]) -> tuple[int, str, str | None]:
        agent_id = (form.get("id") or "").strip()
        editing = (form.get("_editing") or "").strip()
        _, written = self._split_agents()

        def refuse(message: str, status: int = HTTPStatus.OK) -> tuple[int, str, str | None]:
            # `editing` and not `agent is not None`: a failed edit has no valid agent to
            # render from, and coming back as a New agent form would offer a writable id
            # on a page the operator opened to change one field.
            return status, self.agent_form(
                None, values=form, error=message, editing=bool(editing)
            ), None

        if not AGENT_ID.match(agent_id):
            return refuse(
                "An agent id must start with a letter or digit and use only lowercase "
                "letters, digits, dots, dashes and underscores."
            )
        if editing and editing != agent_id:
            # The id field is `readonly`, so a browser cannot produce this. Renaming by
            # writing the new id and leaving the old one behind is not a rename, and
            # silently doing it is worse than saying no.
            return refuse(
                f"This form is editing `{editing}`. Renaming an agent means adding the "
                "new one and deleting the old one, so that nothing is left behind."
            )
        if agent_id in written:
            return refuse(
                f"`{agent_id}` is already defined in config.yaml. Delete it there first, "
                "or pick another id -- the server refuses to boot with both."
            )

        try:
            agent = AgentConfig(agent_id=agent_id, **_from_form(form))
        except ValidationError as exc:
            return refuse(_first_error(exc))

        # The one check worth making before writing: a command that does not resolve is
        # a config that looks fine and fails at consult time. Nothing is executed here
        # -- `resolve_command` is a PATH lookup, not a subprocess.
        try:
            resolve_command(agent)
        except AdapterError as exc:
            return refuse(str(exc))

        data = agent.model_dump(mode="json", exclude_defaults=True)
        data.pop("agent_id", None)

        def store(agents: dict[str, Any]) -> tuple[int, str] | None:
            # Inside the lock, against the file as it is now, because a form is open for
            # as long as someone leaves it open. What was true when the page rendered is
            # a guess by the time it is submitted.
            if editing and agent_id not in agents:
                return HTTPStatus.CONFLICT, (
                    f"`{agent_id}` is no longer in this file -- it was deleted while this "
                    "form was open. Add it as a new agent if you meant to bring it back."
                )
            if not editing and agent_id in agents:
                return HTTPStatus.CONFLICT, (
                    f"`{agent_id}` is already configured here. Edit it rather than adding "
                    "it again -- saving this would replace it without saying so."
                )
            agents[agent_id] = data
            return None

        problem = self._rewrite(store)
        if problem:
            # Back into the form rather than out as a bare status: whoever is looking at
            # this typed a page of fields, and the file being unreadable is not a reason
            # to make them type it again. The status is the one `_rewrite` decided, so a
            # refusal is not answered 200 here and 409 by the delete beside it.
            return refuse(problem[1], problem[0])
        return HTTPStatus.SEE_OTHER, "", f"/agents?saved={agent_id}"

    def delete(self, form: dict[str, str]) -> tuple[int, str, str | None]:
        agent_id = (form.get("id") or "").strip()

        def remove(agents: dict[str, Any]) -> tuple[int, str] | None:
            if agent_id not in agents:
                return HTTPStatus.NOT_FOUND, "No such agent in this file."
            del agents[agent_id]
            return None

        problem = self._rewrite(remove)
        if problem:
            status, message = problem
            return status, _document(
                "Not deleted", f"<p>{_e(message)}</p><p><a href='/agents'>Back</a></p>"
            ), None
        return HTTPStatus.SEE_OTHER, "", f"/agents?deleted={agent_id}"


# --- form translation -------------------------------------------------------


def _as_form(agent: AgentConfig | None) -> dict[str, str]:
    if agent is None:
        return {"runtime": "codex", "priority": "100", "enabled": "on"}
    values = {
        "id": agent.agent_id,
        "runtime": agent.runtime,
        "command": agent.command,
        "model": agent.model,
        "reasoning_effort": agent.reasoning_effort or "",
        "priority": str(agent.priority),
    }
    if agent.enabled:
        values["enabled"] = "on"
    if agent.web_search:
        values["web_search"] = "on"
    return values | {f"score.{c}": str(s) for c, s in agent.scores.items()}


def _from_form(form: dict[str, str]) -> dict[str, object]:
    """Form strings to the shapes `AgentConfig` validates.

    Deliberately thin: anything it cannot turn into the right type is passed through
    for pydantic to refuse, so the rules live in one place and this stays a translator
    rather than a second validator."""
    scores = {
        capability: int(form[f"score.{capability}"])
        for capability in CAPABILITIES
        if (form.get(f"score.{capability}") or "").strip().lstrip("-").isdigit()
    }
    fields: dict[str, object] = {
        "runtime": form.get("runtime", ""),
        "command": (form.get("command") or "").strip(),
        "model": (form.get("model") or "").strip(),
        "reasoning_effort": (form.get("reasoning_effort") or "").strip() or None,
        # An unchecked checkbox is absent from the body rather than false, which is why
        # these read presence and not value.
        "enabled": "enabled" in form,
        "web_search": "web_search" in form,
        "scores": scores,
    }
    # Passed through as typed, so `-1` and `abc` are refused the way a bad score is.
    # This used to read `.isdigit()` and fall back to 100, which meant a typo saved
    # successfully at a priority nobody chose. Absent stays absent -- an empty field is
    # dropped by `parse_qs` before it gets here, and the model's own default covers it.
    if "priority" in form:
        fields["priority"] = form["priority"]
    return fields


def _settings(data: dict[str, Any]) -> dict[str, Any]:
    """An entry without the `agent_id` it may spell out for itself.

    A boot accepts that field and then overwrites it from the mapping key, in
    `_label_agents`. Passing both to `AgentConfig` is a duplicate-argument `TypeError`,
    which would make this page and this save refuse a file the server starts on fine.
    """
    return {key: value for key, value in data.items() if key != "agent_id"}


def _first_error(exc: ValidationError) -> str:
    """One message, in the operator's terms. A pydantic dump names `value_error` and a
    model class; the person reading it typed into a box."""
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error["loc"]) or "form"
    return f"{field}: {error['msg']}"


def _delete_form(agent_id: str, token: str) -> str:
    return (
        "<form method=post action='/agents/delete' style='display:inline'>"
        f"<input type=hidden name=_token value='{_e(token)}'>"
        f"<input type=hidden name=id value='{_e(agent_id)}'>"
        "<button type=submit>delete</button></form>"
    )


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
        url = urlparse(self.path)
        status, body = self.dashboard.page(url.path, url.query)
        self._respond(status, body)

    def do_POST(self) -> None:  # noqa: N802 -- the stdlib's spelling
        """The only way anything on this machine gets written from a browser.

        Four checks before the body is even read, in the order that costs least: the
        Host header, the editable flag, the size, and the origin. Then the token, which
        is the one that stops a page on the internet posting a form here."""
        if not self._host_allowed():
            self._respond(HTTPStatus.FORBIDDEN, _document("Forbidden", "<p>Loopback only.</p>"))
            return

        status, body = self.dashboard._if_editable(lambda: (HTTPStatus.OK, ""))
        if status != HTTPStatus.OK:
            self._respond(status, body)
            return

        if not self._origin_allowed():
            self._refuse("That form was submitted from another site.")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._refuse("That request did not say how long its body was.")
            return
        # The lower bound matters as much as the cap: `rfile.read(-1)` reads until the
        # client decides to stop, which is a header away from holding a thread open.
        if not 0 <= length <= MAX_BODY_BYTES:
            self._refuse("That form was too large to be one of ours.")
            return

        form = {
            key: values[0]
            for key, values in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()
        }
        # `compare_digest` rather than `==`: the comparison is against a secret, and the
        # cheap habit is the one worth having even where the timing is unmeasurable.
        if not secrets.compare_digest(form.get("_token", ""), self.dashboard.token):
            self._refuse("That form is stale. Reload the page and try again.")
            return

        path = urlparse(self.path).path
        if path == "/agents":
            status, body, location = self.dashboard.save(form)
        elif path == "/agents/delete":
            status, body, location = self.dashboard.delete(form)
        else:
            status, body, location = (
                HTTPStatus.NOT_FOUND,
                _document("Not found", "<p>No such page.</p>"),
                None,
            )
        self._respond(status, body, location)

    def _refuse(self, message: str) -> None:
        self._respond(
            HTTPStatus.FORBIDDEN,
            _document("Refused", f"<h1>Refused</h1><p>{_e(message)}</p><p><a href='/agents'>Back</a></p>"),
        )

    def _host_allowed(self) -> bool:
        """No header at all is refused, not waved through: a request that will not say
        what it was addressed to is exactly the one this check exists for."""
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        if host.startswith("["):
            # `[::1]:8765` -- the brackets are there precisely so the port colon can be
            # told apart from the address's own, and `rsplit` cannot.
            host = host.partition("]")[0] + "]"
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _origin_allowed(self) -> bool:
        """A browser sends `Origin` on form posts. Absent is allowed -- curl does not
        send one, and the token is what a browser has to get past anyway.

        `null` is present and refused: that is what a sandboxed frame sends, and the
        one thing an attacker's page can produce here without knowing the token."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return (urlparse(origin).hostname or "") in ALLOWED_HOSTS

    def _respond(self, status: int, body: str, location: str | None = None) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # Nothing on this page should be cached, embedded, or allowed to fetch
        # anything: it is a local window onto stored prompts.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # `form-action 'self'` because `default-src 'none'` does not cover where a form
        # may submit to: without it, injected markup could post this page's token out.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        )
        # `same-origin` and not `no-referrer`, which this sent until a form post proved
        # why: a browser serializes the `Origin` header as `null` when the referrer
        # policy is `no-referrer`, so the strictest setting made every save look like it
        # came from another site. Nothing here can reach off this origin anyway -- the
        # CSP above forbids it -- so the referrer has nowhere to leak to.
        self.send_header("Referrer-Policy", "same-origin")
        if location:
            # 303, so the browser follows with a GET and a refresh is not a resubmit.
            self.send_header("Location", location)
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
    mode = (
        f"editing agents in {consult.managed_agents_path}"
        if consult.dashboard.editable
        else "read-only -- set `consult.dashboard.editable: true` to configure agents here"
    )
    print(f"dashboard on http://{host}:{port} ({mode}) -- ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
