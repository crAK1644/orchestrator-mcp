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
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version as _distribution_version
from pathlib import Path
from typing import Any, get_args
from urllib.parse import parse_qs, unquote, urlparse

import yaml
from pydantic import ValidationError

from ..contract import ConfigError
from ..review.contract import Severity
from .adapters import adapter_for
from .adapters.base import AdapterError, resolve_command
from .adapters.codex_cli import rate_limit as codex_rate_limit
from .config import (
    MAX_DEEP_REVIEWERS,
    AgentConfig,
    ConsultConfig,
    ReviewConfig,
    load_consult_config,
)
from .contract import Capability, Runtime
from .managed import read_managed, read_managed_document, write_managed

CAPABILITIES = get_args(Capability)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Worst first, and taken from the contract rather than retyped, so a severity added
# there cannot end up rendered in whatever order SQLite returned it.
SEVERITIES = get_args(Severity)

# Slugs each runtime is known to take, offered as a `datalist` rather than a `select`:
# the field stays free text, because a model that ships tomorrow has to be typeable
# today rather than wait for this list to catch up. Keyed by runtime so the browser can
# label each suggestion with the runtime it belongs to -- the form is one page of plain
# HTML with no script in it, so the list cannot filter itself when the runtime changes.
#
# On antigravity the reasoning level is part of the slug, which is why those read
# `-high` / `-low`. On codex and claude it is the separate `reasoning_effort` field, so
# `gpt-5.6-sol` at `max` is that slug plus that level, not a slug of its own.
MODEL_PRESETS: dict[str, tuple[str, ...]] = {
    "codex": ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5"),
    "claude": (
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        # The CLI takes an unversioned alias too, and resolves it to the latest.
        "fable",
        "opus",
        "sonnet",
    ),
    "antigravity": (
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3.5-flash-high",
        "gemini-3.5-flash-medium",
        "gemini-3.5-flash-low",
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ),
}

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

    def __init__(self, config: ConsultConfig, config_path: Path | None = None) -> None:
        self.config = config
        # Where `config.yaml` is, so the duplicate-id check can ask what it holds now
        # rather than what it held when this process started. Optional because a test
        # that builds a `ConsultConfig` directly has no such file; without it the check
        # falls back to the boot snapshot, which is what it used to be. See
        # `_written_ids`.
        self.config_path = config_path
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
        if path == "/reviews":
            return HTTPStatus.OK, self.reviews_page()
        if path == "/reviewers":
            return self._if_editable(lambda: (HTTPStatus.OK, self.reviewers_page(query)))
        if path.startswith("/reviews/"):
            return self.review(unquote(path.removeprefix("/reviews/")))
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
            " &middot; <a href='/reviewers'>configure reviewers</a>"
            if self.config.dashboard.editable
            else ""
        )
        return _document(
            "Consultations",
            f"<h1>Consult Protocol v1</h1><p class=meta>orchestrator-mcp-server {_e(self.version)}"
            f" &middot; {_e(str(self.config.database_path))}{link}</p>"
            f"<h2>Agents</h2>{self._agents_table()}{self._rate_limit_line()}"
            f"{self._reviews_section()}"
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

    def _rate_limit_line(self) -> str:
        """What is left of the Codex subscription window, when anything knows.

        Under the table rather than in it: one `~/.codex` serves every codex agent, so
        a per-row copy of the same number would read as several separate budgets.
        Nothing at all when no codex agent is configured, or when no consultation has
        run yet to be told a number.
        """
        if not any(agent.runtime == "codex" for agent in self.config.agents.values()):
            return ""
        limit = codex_rate_limit()
        if not limit:
            return ""
        window = _window(limit.get("window_minutes"))
        when = _resets(limit.get("resets_at"))
        plan = f" &middot; {_e(str(limit['plan_type']))} plan" if limit.get("plan_type") else ""
        return (
            f"<p class=meta>codex usage: {limit['used_percent']:.0f}%{window} used{when}{plan}"
            # Said plainly because it is: this is whatever the last consultation was
            # told, not a number this page went and asked for.
            " &middot; as of the last consultation</p>"
        )

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

    # --- reviews -------------------------------------------------------------

    def _reviews_ready(self) -> bool:
        """Whether the migration that creates `reviews` has run yet.

        This connection is `mode=ro` and could not create the table if it wanted to, so
        a server that has not been restarted since this version shipped leaves every
        review query raising `no such table` -- on a page whose whole job is to stay
        readable. Probing `sqlite_master` turns that into a sentence about restarting.
        """
        return bool(
            self._query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews'")
        )

    def _reviews_missing(self) -> str:
        """Why there is no review table to read, or "" when there is one.

        Two different absences, and telling someone to restart is only the answer to
        one of them: a database that does not exist yet is a server that has never
        consulted anything, and no restart will conjure a review into it.
        """
        if self._reviews_ready():
            return ""
        if not Path(self.config.database_path).exists():
            return "No reviews recorded yet."
        return NOT_MIGRATED

    def _review_rows(self, limit: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT r.id, r.created_at, r.mode, r.status, r.outcome, r.goal, "
            "r.parent_review_id, "
            "(SELECT COUNT(*) FROM review_consultations c WHERE c.review_id = r.id) "
            "  AS reviewers, "
            "(SELECT COUNT(*) FROM review_consultations c WHERE c.review_id = r.id "
            " AND c.error_code IS NOT NULL) AS failures "
            "FROM reviews r ORDER BY r.created_at DESC LIMIT ?",
            (limit,),
        )

    def _reviews_section(self) -> str:
        """The newest few on the index -- and nothing at all where reviews are unused.

        An install with no `review:` block and no review history gets no empty section
        advertising a feature it has not configured. One that *has* configured reviews
        gets the un-migrated notice, because there the missing table is news.
        """
        missing = self._reviews_missing()
        if missing:
            return (
                f"<h2>Reviews</h2><p class=meta>{_e(missing)}</p>"
                if self.config.review is not None
                else ""
            )
        rows = self._review_rows(10)
        if not rows and self.config.review is None:
            return ""
        more = "<p><a href='/reviews'>All reviews</a></p>" if rows else ""
        return f"<h2>Reviews</h2>{self._reviews_table(rows)}{more}"

    def _reviews_table(self, rows: list[sqlite3.Row]) -> str:
        if not rows:
            return "<p class=meta>No reviews recorded yet.</p>"
        body = "".join(
            "<tr>"
            f"<td><a href='/reviews/{_e(row['id'])}'><code>{_e(row['id'][:8])}</code></a></td>"
            f"<td class=meta>{_e(row['created_at'])}</td>"
            f"<td>{_e(row['mode'])}{' &middot; recheck' if row['parent_review_id'] else ''}</td>"
            f"<td>{_status_word(row['status'])}</td>"
            f"<td>{_e(row['outcome'] or '--')}</td>"
            f"<td>{row['reviewers']}</td>"
            f"<td class={'bad' if row['failures'] else 'ok'}>{row['failures']}</td>"
            f"<td>{_e(_short(row['goal'], 90))}</td>"
            "</tr>"
            for row in rows
        )
        head = (
            "<tr><th>id<th>started<th>mode<th>status<th>outcome<th>reviewers"
            "<th>failed<th>goal</tr>"
        )
        return f"<table>{head}{body}</table>"

    def reviews_page(self) -> str:
        head = "<p><a href='/'>&larr; consultations</a></p><h1>Reviews</h1>"
        missing = self._reviews_missing()
        if missing:
            return _document("Reviews", f"{head}<p class=meta>{_e(missing)}</p>")
        return _document("Reviews", head + self._reviews_table(self._review_rows(200)))

    def review(self, review_id: str) -> tuple[int, str]:
        missing = self._reviews_missing()
        if missing:
            return HTTPStatus.NOT_FOUND, _document(
                "Reviews",
                f"<h1>Reviews</h1><p class=meta>{_e(missing)}</p>"
                "<p><a href='/'>Back</a></p>",
            )
        rows = self._query("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if not rows:
            return HTTPStatus.NOT_FOUND, _document(
                "Not found", "<p>No such review.</p><p><a href='/reviews'>Back</a></p>"
            )
        review = rows[0]
        reviewers = self._query(
            "SELECT * FROM review_consultations WHERE review_id = ? ORDER BY agent_id",
            (review_id,),
        )
        findings = [
            finding
            for row in reviewers
            for finding in _findings_of(row)
        ]
        parent = (
            f" &middot; recheck of <a href='/reviews/{_e(review['parent_review_id'])}'>"
            f"<code>{_e(review['parent_review_id'][:8])}</code></a>"
            if review["parent_review_id"]
            else ""
        )
        return HTTPStatus.OK, _document(
            f"Review {review_id[:8]}",
            "<p><a href='/reviews'>&larr; all reviews</a></p>"
            f"<h1>{_e(review['mode'])} review &middot; {_status_word(review['status'])}</h1>"
            f"<p class=meta>{_e(review['id'])} &middot; outcome "
            f"{_e(review['outcome'] or '--')} &middot; {_e(review['created_at'])}"
            f" &middot; updated {_e(review['updated_at'])}"
            f" &middot; web {'requested' if review['web_requested'] else 'off'}"
            f"{_secret_line(review['secret_hits_json'])}{parent}</p>"
            f"{_material(review['material_json'])}"
            # The stored copies, which are the redacted ones -- what a reviewer was
            # sent is not kept anywhere this page can reach.
            f"{_block('Goal (as stored)', review['goal'])}"
            f"{_block('Context (as stored)', review['context'])}"
            f"{_host_findings(review['host_findings_json'])}"
            f"<h2>Reviewers</h2>{_reviewer_table(reviewers)}"
            f"<h2>Findings</h2>{_findings_table(findings)}"
            f"<h2>Synthesis</h2>{_synthesis(review['summary_json'])}"
            f"<h2>Answers</h2>{_answers(reviewers)}"
            f"{_fix_rounds(review['fix_rounds_json'])}"
            f"{self._rechecks(review_id)}",
        )

    def _rechecks(self, review_id: str) -> str:
        rows = self._query(
            "SELECT id, created_at, status, outcome FROM reviews "
            "WHERE parent_review_id = ? ORDER BY created_at",
            (review_id,),
        )
        if not rows:
            return ""
        items = "".join(
            f"<li><a href='/reviews/{_e(row['id'])}'><code>{_e(row['id'][:8])}</code></a> "
            f"&mdash; {_status_word(row['status'])} "
            f"<span class=meta>{_e(row['outcome'] or '')} {_e(row['created_at'])}</span></li>"
            for row in rows
        )
        return f"<h2>Rechecks</h2><ul>{items}</ul>"

    # --- configuring reviewers -----------------------------------------------

    def _review_in_config(self) -> bool:
        """Whether the operator's own file defines `review:`.

        A `review:` block in both files is a startup error, not a merge -- the same
        rule the agents follow -- so this page has to refuse the save rather than write
        a file the next boot rejects. Read off disk for the reason `_written_ids`
        gives: what that file said at boot is not what it says now.

        A file this cannot read falls back to "yes, it is written there" whenever a
        review block is configured at all. That over-refuses in one case (the block
        came from the managed file and `config.yaml` has since become unreadable) and
        under-refuses in none -- and a `config.yaml` that cannot be read is a server
        that will not start either way. When no config path was supplied there is no
        operator-owned file to inspect, so the answer is false.
        """
        if self.config_path is None:
            return False
        try:
            document = yaml.safe_load(self.config_path.read_text()) or {}
            consult = document.get("consult")
            if not isinstance(consult, dict):
                return self.config.review is not None
            return "review" in consult
        except (OSError, UnicodeDecodeError, AttributeError, yaml.YAMLError):
            return self.config.review is not None

    def _reviewable(self) -> dict[str, AgentConfig]:
        """The agents this page can see, whichever file they live in.

        `self.config.agents` alone is the boot snapshot, and an agent added through
        this dashboard a minute ago is not in it -- refusing that one would refuse
        exactly what the form is for.
        """
        managed, written = self._split_agents()
        return written | managed

    def reviewers_page(self, query: str = "") -> str:
        params = parse_qs(query)
        notice = (
            "<p class='banner done'>Saved. It takes effect when the MCP server next "
            "starts.</p>"
            if params.get("saved")
            else ""
        )
        return self.reviewers_form(notice=notice)

    def reviewers_form(self, values: dict[str, str] | None = None,
                       error: str = "", notice: str = "") -> str:
        """Who reviews: one agent for `review`, one to five for `deep_review`."""
        agents = self._reviewable()
        current = self.config.review or ReviewConfig.model_construct(
            reviewers=[], deep_reviewers=[]
        )
        chosen = values or {
            **({"reviewer": current.reviewers[0]} if current.reviewers else {}),
            **{f"deep.{aid}": "on" for aid in current.deep_reviewers},
        }

        if self._review_in_config():
            return _document(
                "Reviewers",
                "<p><a href='/'>&larr; consultations</a></p><h1>Reviewers</h1>"
                f"<p>The <code>review:</code> block is defined in {_e(self._config_name)}. "
                "Edit it there, or delete it from that file first -- the server refuses "
                "to start with both.</p>"
                f"{_reviewer_summary(current)}",
            )

        offered = sorted(agents.items())
        options = "".join(
            f"<option value='{_e(aid)}'"
            f"{' selected' if chosen.get('reviewer') == aid else ''}"
            f"{' disabled' if _not_reviewable(agent, aid) else ''}>{_e(aid)}"
            f"{_e(_why_not(agent, aid))}</option>"
            for aid, agent in offered
        )
        boxes = "".join(
            f"<label><input type=checkbox name='deep.{_e(aid)}'"
            f"{' checked' if chosen.get(f'deep.{aid}') else ''}"
            f"{' disabled' if _not_reviewable(agent, aid) else ''}> "
            f"<code>{_e(aid)}</code> <span class=meta>{_e(agent.runtime)} "
            f"{_e(agent.model)}{_e(_why_not(agent, aid))}</span></label>"
            for aid, agent in offered
        ) or "<p class=meta>No agents configured yet.</p>"

        return _document(
            "Reviewers",
            "<p><a href='/'>&larr; consultations</a></p><h1>Reviewers</h1>"
            f"<p class=meta>{_e(str(self.config.managed_agents_path))}</p>"
            f"{notice}{self._restart_banner()}"
            + (f"<p class=error>{_e(error)}</p>" if error else "")
            + "<form method=post action='/reviewers'>"
            f"<input type=hidden name=_token value='{_e(self.token)}'>"
            "<label><span>review &mdash; the single agent a standard review asks. One, "
            "because a second opinion nobody compared is just a slower first one; "
            "several is what deep review is for.</span>"
            f"<select name=reviewer><option value=''>none</option>{options}</select></label>"
            f"<fieldset><legend>deep review &mdash; 1 to {MAX_DEEP_REVIEWERS} agents, each "
            "asked independently and none of them shown another's answer</legend>"
            f"{boxes}</fieldset>"
            "<p class=meta>An agent has to be enabled and scored for <code>review</code> "
            "before it can be named here. Tick <code>review</code> in its capabilities on "
            "its own page.</p>"
            "<button type=submit>Save</button></form>",
        )

    def save_reviewers(self, form: dict[str, str]) -> tuple[int, str, str | None]:
        reviewer = (form.get("reviewer") or "").strip()
        deep = [
            key.removeprefix("deep.")
            for key in form
            if key.startswith("deep.") and key.removeprefix("deep.").strip()
        ]
        agents = self._reviewable()

        def refuse(message: str, status: int = HTTPStatus.OK) -> tuple[int, str, str | None]:
            return status, self.reviewers_form(values=form, error=message), None

        if self._review_in_config():
            # Not back into the form: `reviewers_form` renders the read-only page in
            # this case, and it would swallow the message explaining the refusal.
            return HTTPStatus.CONFLICT, _document(
                "Not saved",
                f"<h1>Not saved</h1><p><code>review:</code> is defined in "
                f"{_e(self._config_name)}. Delete it there first -- the server refuses "
                "to start with the block in both files.</p>"
                "<p><a href='/reviewers'>Back</a></p>",
            ), None
        for agent_id in sorted({reviewer, *deep} - {""}):
            problem = _not_reviewable(agents.get(agent_id), agent_id)
            if problem:
                return refuse(problem)
        try:
            block = ReviewConfig(
                reviewers=[reviewer] if reviewer else [], deep_reviewers=sorted(deep)
            )
        except ValidationError as exc:
            return refuse(_first_error(exc))

        def store(document: dict[str, Any]) -> tuple[int, str] | None:
            document["review"] = block.model_dump(mode="json")
            return None

        problem = self._rewrite_document(store)
        if problem:
            return refuse(problem[1], problem[0])
        return HTTPStatus.SEE_OTHER, "", "/reviewers?saved=1"

    # --- configuring agents --------------------------------------------------

    def _split_agents(self) -> tuple[dict[str, AgentConfig], dict[str, AgentConfig]]:
        """(editable here, defined in config.yaml). The second kind is shown and not
        touched: this page does not own that file.

        The managed half is re-read from disk on every request rather than taken from
        the config this process loaded at boot. This process is the one writing that
        file, so a save has to be visible on the page that follows it -- otherwise you
        save an agent, land on a table that does not list it, and cannot edit or delete
        it until the dashboard is restarted.

        The other half is the boot snapshot filtered by what `config.yaml` still says,
        because those two can disagree: move an agent out of that file and into this one
        and the snapshot would go on listing it as "defined in config.yaml" beside the
        editable copy that now exists -- the same agent, twice, one of the rows a lie. An
        id *added* to that file since boot goes the other way and is simply not shown:
        rendering a row needs an `AgentConfig`, and building one means the merge, which
        raises rather than returns. It still blocks a save, which is the half that matters.
        """
        on_file = self._written_ids()
        written = {
            aid: a for aid, a in self.config.agents.items() if not a.managed and aid in on_file
        }
        try:
            on_disk = read_managed(self.config.managed_agents_path)
            # Reading the file here skips `_label_agents`, which is where a boot refuses
            # a blank id. Without this the page renders a nameless row, offers to edit
            # it, and the server it is configuring will not start. Only the per-entry
            # rules: a duplicate is a reason to warn, not a reason to drop the table that
            # holds the delete button for it.
            unbootable = self._unbootable(on_disk)
            if unbootable:
                raise ConfigError(unbootable)
            managed = {
                aid: AgentConfig(agent_id=aid, managed=True, **_settings(data))
                for aid, data in on_disk.items()
            }
        except (ConfigError, ValidationError, TypeError, OSError):
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
        return self._rewrite_document(lambda document: change(document["agents"]))

    def _rewrite_document(
        self, change: Callable[[dict[str, Any]], tuple[int, str] | None]
    ) -> tuple[int, str] | None:
        """`_rewrite` over the whole managed document, agents and reviewers together.

        Both keys are read once and written once, so an agent save cannot drop the
        reviewer block it never looked at, and a reviewer save cannot drop an agent.
        """
        with self._writing:
            try:
                document = read_managed_document(self.config.managed_agents_path)
            except (ConfigError, OSError) as exc:
                return HTTPStatus.CONFLICT, str(exc)
            refusal = change(document)
            if refusal is not None:
                return refusal
            agents = document["agents"]
            # What is about to be written has to be something the server can boot on.
            # The form cannot produce a bad entry, but it can be asked to save alongside
            # one a hand-edit left there -- and writing that back is this process
            # putting its name on a file that will refuse the next start. Both halves of
            # the boot rules here, unlike the page, which wants only the first.
            unbootable = (
                self._unbootable(agents)
                or self._unbootable_review(document["review"], agents)
                or self._duplicate_of_written(agents)
            )
            if unbootable:
                return HTTPStatus.CONFLICT, unbootable
            try:
                write_managed(self.config.managed_agents_path, agents, document["review"])
            except OSError as exc:
                # A readable file in a directory this user cannot write to. The read
                # above says nothing about that, so the failure lands here and has to
                # become a page rather than a traceback in a request thread.
                return HTTPStatus.INTERNAL_SERVER_ERROR, (
                    f"{self.config.managed_agents_path} could not be written: {exc}"
                )
            return None

    def _written_ids(self) -> set[str]:
        """The agent ids `config.yaml` holds now, or the ones it held at boot.

        The boot snapshot alone is not enough to predict the next start. An operator who
        adds `beta` to `config.yaml` by hand and then adds `beta` here gets a save this
        process thinks is fine and a next boot that refuses the duplicate -- a file
        written broken, by the check that exists to prevent exactly that.

        So this reads the ids back off disk, and only the ids: the merge those agents go
        through raises rather than returns, and a page render has nowhere to put that.
        Names are all the duplicate check needs, and a name is the one thing a partially
        edited file still yields.

        A file that has moved, or that is mid-edit and unparseable, falls back to the
        snapshot rather than to nothing -- stale is a worse answer than fresh and a much
        better one than treating `config.yaml` as empty.

        A file with no `consult:` in it falls back for the same reason, and it is the
        likelier accident: an editor that truncates before it writes leaves exactly that,
        and reading it as "no agents are written anywhere" is how this check would wave
        through the duplicate it exists to catch. A `consult:` block with no `agents:`
        under it is a different thing and is taken at its word -- a config whose agents
        all live in the managed file is a supported shape, not a truncated read.

        What is left after that is a truncation that happens to parse, with `consult:`
        present and an agent missing from it. Nothing short of locking the operator's own
        file closes that, and the boot refusal is what catches it.
        """
        booted = {aid for aid, a in self.config.agents.items() if not a.managed}
        if self.config_path is None:
            return booted
        try:
            document = yaml.safe_load(self.config_path.read_text()) or {}
            consult = document.get("consult")
            if not isinstance(consult, dict):
                return booted
            agents = consult.get("agents") or {}
            if not isinstance(agents, dict):
                return booted
            return {str(agent_id) for agent_id in agents}
        except (OSError, UnicodeDecodeError, AttributeError, yaml.YAMLError):
            return booted

    @property
    def _config_name(self) -> str:
        """What to call the operator's own file in a refusal.

        `config.yaml` is only its name by default -- `ORCHESTRATOR_CONFIG` can point the
        server at any path, and a refusal that names the wrong file sends someone to edit
        a file that does not have the problem in it.
        """
        return str(self.config_path) if self.config_path else "config.yaml"

    def _duplicate_of_written(self, agents: dict[str, Any]) -> str | None:
        """An id in this mapping that `config.yaml` also defines, or None.

        Separate from `_unbootable` because the two answer different questions and only
        one of them belongs on a page. An entry the form cannot fix has to keep a row out
        of the table -- it would render nameless and offer to edit nothing. A duplicate is
        a fact about two files, and dropping the table over it hides the delete button for
        the very copy the operator has to remove.
        """
        written = self._written_ids()
        # Keyed by `str`, since a file holding both `1:` and `alpha:` has keys that cannot
        # be compared to each other at all and `sorted` would raise.
        for agent_id in sorted(agents, key=str):
            if agent_id in written:
                return (
                    f"`{agent_id}` is defined in {self._config_name} as well as in this "
                    "file, and the server refuses to start with both. Delete one of the two."
                )
        return None

    def _unbootable(self, agents: dict[str, Any]) -> str | None:
        """Why a server start would refuse an entry in this mapping, or None if it would
        not.

        The per-entry rules a boot applies -- a text id that is not blank, and settings
        `AgentConfig` accepts -- checked here so that neither a page nor a save has to
        assume the file only ever held what this form put in it. The one boot rule that is
        *not* here is the duplicate: see `_duplicate_of_written`.
        """
        # Sorted by the *string* of the key: a file holding both `1:` and `alpha:` has
        # keys that cannot be compared to each other at all, and a `TypeError` from
        # inside `sorted` would escape every catch below.
        for agent_id, data in sorted(agents.items(), key=lambda item: str(item[0])):
            if not isinstance(agent_id, str) or not agent_id.strip():
                return (
                    "An agent in this file has a blank or non-text id, which the server "
                    "refuses to start on."
                )
            if not isinstance(data, dict):
                return f"`{agent_id}` in this file is not a mapping of settings."
            if not isinstance(data.get("agent_id", ""), str):
                # `_settings` drops this field before validating, because a boot accepts
                # it and overwrites it from the key. What a boot does *not* do is accept
                # any type for it, so dropping it without looking would wave through the
                # one entry this method exists to catch.
                return f"`{agent_id}` in this file has an `agent_id` that is not text."
            try:
                AgentConfig(agent_id=agent_id, **_settings(data))
            except ValidationError as exc:
                return f"`{agent_id}` in this file is not a valid agent: {_first_error(exc)}"
            except TypeError:
                # Keys that are not strings, which `**` refuses.
                return f"`{agent_id}` in this file is not a mapping of settings."
        return None

    def _unbootable_review(self, review: Any, agents: dict[str, Any]) -> str | None:
        """Why the managed review block would stop the next server boot."""
        # Empty is absent, the same way `_merged_review` reads it.
        if not review:
            return None
        try:
            block = ReviewConfig(**review)
        except (ValidationError, TypeError) as exc:
            message = _first_error(exc) if isinstance(exc, ValidationError) else str(exc)
            return f"`review:` in this file is not valid: {message}"
        available = {
            agent_id: agent
            for agent_id, agent in self.config.agents.items()
            if not agent.managed
        }
        for agent_id, data in agents.items():
            available[agent_id] = AgentConfig(agent_id=agent_id, **_settings(data))
        for where in ("reviewers", "deep_reviewers"):
            for agent_id in getattr(block, where):
                if problem := _not_reviewable(available.get(agent_id), agent_id):
                    return f"`review:` in this file is not valid: {problem}"
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
            "<p><a href='/agents/new'>Add an agent</a> &middot; "
            "<a href='/reviewers'>configure reviewers</a></p>"
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
        scores = "".join(_score_box(v, capability) for capability in CAPABILITIES)
        efforts = "".join(
            f"<option value='{_e(level)}'"
            f"{' selected' if v.get('reasoning_effort') == level else ''}>{_e(level)}</option>"
            for level in EFFORTS
        )
        # Derived from the type rather than retyped here, so a new runtime cannot be
        # added to the contract and silently stay unofferable in the form.
        runtimes = "".join(
            f"<option value='{_e(runtime)}'"
            f"{' selected' if v.get('runtime') == runtime else ''}>{_e(runtime)}</option>"
            for runtime in get_args(Runtime)
        )
        # Every runtime's slugs in one list, each labelled with the runtime it is for.
        # Suggestions, not a closed set: the input keeps taking anything typed into it.
        models = "".join(
            f"<option value='{_e(slug)}' label='{_e(runtime)}'>"
            for runtime in get_args(Runtime)
            for slug in MODEL_PRESETS.get(runtime, ())
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
            "<label><span>model &mdash; pick one of the known slugs or type any other. "
            "On antigravity the reasoning level is part of the name; on codex and claude "
            "it is the next field.</span>"
            f"<input type=text name=model required list=model-presets "
            f"value='{_e(v.get('model', ''))}'></label>"
            f"<datalist id=model-presets>{models}</datalist>"
            "<label><span>reasoning effort &mdash; codex only; unset means the model's "
            "own default, which is not what your ~/.codex/config.toml says. Antigravity "
            "carries the level in the model slug instead</span>"
            f"<select name=reasoning_effort><option value=''>unset</option>{efforts}</select></label>"
            f"<label><span>priority &mdash; lower wins a tie</span>"
            f"<input type=number name=priority min=0 value='{_e(v.get('priority', '100'))}'></label>"
            f"<label><input type=checkbox name=enabled{' checked' if v.get('enabled') else ''}> "
            "enabled</label>"
            f"<label><input type=checkbox name=web_search"
            f"{' checked' if v.get('web_search') else ''}> web search &mdash; required "
            "before a web-mode consultation will route here</label>"
            "<fieldset class=scores><legend>capabilities &mdash; the work this agent is "
            "offered. Unticked means never route it here; between two agents ticked for "
            f"the same capability, the lower priority wins</legend>{scores}</fieldset>"
            "<button type=submit>Save</button></form>",
        )

    def save(self, form: dict[str, str]) -> tuple[int, str, str | None]:
        agent_id = (form.get("id") or "").strip()
        editing = (form.get("_editing") or "").strip()

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
        # `_written_ids` and not the boot snapshot, so that this refusal and the one
        # `_unbootable` raises inside the lock are answering from the same file. The
        # snapshot cannot see an id added to `config.yaml` since, and goes on refusing
        # one deleted from it.
        if agent_id in self._written_ids():
            return refuse(
                f"`{agent_id}` is already defined in {self._config_name}. Delete it there "
                "first, or pick another id -- the server refuses to boot with both."
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


def _score_box(values: dict[str, str], capability: str) -> str:
    """One capability as a yes/no: is this agent offered this kind of work.

    The routing score behind it is a number, but choosing it is a job nobody asked for
    -- the ranking that number feeds is decided by `priority` for everyone who ticks the
    same box. So a newly ticked box is 100, and the ordering stays in the field named
    after it.

    A score already in the config rides back out in the checkbox's own `value`, which is
    what the browser submits when it is ticked. That is the whole reason it is there: an
    operator who hand-wrote `research: 70` to break a tie must not lose it by saving an
    unrelated field on this form.
    """
    kept = (values.get(f"score.{capability}") or "").strip()
    # `> 0` and not merely "is a number": a stored `0` means ineligible, which is what an
    # unticked box means, so it must not come back ticked.
    score = int(kept) if kept.isdigit() and int(kept) > 0 else 0
    return (
        f"<label><input type=checkbox name='score.{_e(capability)}' "
        f"value='{score or 100}'{' checked' if score else ''}> {_e(capability)}</label>"
    )


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


def _window(minutes: object) -> str:
    """A rate-limit window, in the unit it reads naturally in.

    Codex plans quote both a weekly window and a five-hour one, so dividing by a day
    and printing days would render the short one as `0d` -- a number that is wrong
    rather than merely coarse.
    """
    if not isinstance(minutes, int) or minutes <= 0:
        return ""
    if minutes >= 1440:
        return f" of the last {minutes // 1440}d"
    if minutes >= 60:
        return f" of the last {minutes // 60}h"
    return f" of the last {minutes}m"


def _resets(when: object) -> str:
    """When the window rolls over, if that is a time at all.

    The epoch here came out of a file this server does not own, and a number that is
    not a plausible timestamp raises rather than returning something wrong -- which on
    a render path means a 500 on a page whose whole job is to still be readable.
    """
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        return ""
    try:
        return f", resets {datetime.fromtimestamp(when, UTC):%Y-%m-%d %H:%M} UTC"
    except (OSError, OverflowError, ValueError):
        return ""


def _status_cell(row: sqlite3.Row | None) -> str:
    """The newest recorded check, said as one.

    "Last checked" and not a bare timestamp, because this page runs no subprocess: the
    row is whatever the last preflight wrote, and a week-old one read as a fresh probe
    is how an agent looks ready long after its login expired. `orchestrator_test_reviewers` is the
    tool that actually asks.
    """
    if row is None:
        return "<span class=meta>never checked</span>"
    when = f"<span class=meta> &middot; last checked {_e(row['checked_at'])}</span>"
    if row["installed"] and row["authenticated"]:
        return f"<span class=ok>ready</span>{when}"
    label = "not installed" if not row["installed"] else "not connected"
    detail = f"<span class=meta> &middot; {_e(row['detail'])}</span>" if row["detail"] else ""
    return f"<span class=bad>{label}</span>{detail}{when}"


# --- reviews ----------------------------------------------------------------

NOT_MIGRATED = (
    "This database has no review tables yet. This page opens it read-only and cannot "
    "create them -- restart the MCP server and it will add them at startup."
)


def _short(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_word(status: object) -> str:
    """A status, coloured only where the colour says something.

    `complete` is the only finished-well state; `failed` and `cancelled` are the two
    a reader should not skim past. Everything between them is in progress and gets no
    colour, because "running" is not news.
    """
    text = _e(status)
    if status == "complete":
        return f"<span class=ok>{text}</span>"
    if status in ("failed", "cancelled"):
        return f"<span class=bad>{text}</span>"
    return text


def _loads(value: object, fallback: Any) -> Any:
    """Stored JSON, or the fallback. A column this page cannot parse is a row it still
    has to render -- half a review is worth more than a traceback."""
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except ValueError:
        return fallback


def _secret_line(secret_hits_json: object) -> str:
    hits = _loads(secret_hits_json, [])
    if not isinstance(hits, list) or not hits:
        return ""
    where = ", ".join(
        f"{_e(hit.get('field'))} line {_e(hit.get('line'))}"
        for hit in hits
        if isinstance(hit, dict)
    )
    # Positions, never values -- which is also all that was ever stored.
    return f" &middot; <span class=bad>{len(hits)} secret-shaped</span> ({where})"


def _material(material_json: object) -> str:
    """The manifest the host declared. Not proof: this server never read those files,
    so what it says is a disclosure the caller put on the record, not a measurement."""
    items = _loads(material_json, [])
    if not isinstance(items, list) or not items:
        return ""
    listed = "".join(
        f"<li><code>{_e(item.get('label'))}</code> "
        f"<span class=meta>{_e(item.get('kind'))} {_e(item.get('locator'))} &middot; "
        f"{_e(item.get('chars'))} chars</span></li>"
        for item in items
        if isinstance(item, dict)
    )
    return f"<p class=meta>material, as declared by the caller:</p><ul>{listed}</ul>"


def _host_findings(host_findings_json: object) -> str:
    findings = _loads(host_findings_json, [])
    if not isinstance(findings, list) or not findings:
        return ""
    listed = "".join(f"<li>{_e(item)}</li>" for item in findings)
    return (
        "<h2>The host's own reading</h2>"
        "<p class=meta>Formed before any reviewer was asked, and shown to none of "
        f"them.</p><ul>{listed}</ul>"
    )


def _fix_rounds(fix_rounds_json: object) -> str:
    """What the host says it did about the findings, in the order it said so.

    Reported, not verified: nothing in this server edits a file or runs a command,
    so a round is an account of one. The heading says so rather than letting a
    table of outcomes read like a record of work this machine watched happen.
    """
    rounds = _loads(fix_rounds_json, [])
    if not isinstance(rounds, list) or not rounds:
        return ""
    rows = "".join(
        f"<tr><td>{_e(item.get('recorded_at', ''))}</td>"
        f"<td>{_e(item.get('outcome', ''))}</td>"
        f"<td><code>{_e(', '.join(str(f) for f in item.get('finding_ids', [])))}</code></td>"
        f"<td>{_e(item.get('notes', ''))}</td></tr>"
        for item in rounds
        if isinstance(item, dict)
    )
    return (
        "<h2>Fix rounds</h2>"
        "<p class=meta>As reported by the host AI. Nothing here edits files or runs "
        "commands, so these are claims about work done elsewhere.</p>"
        "<table><tr><th>Recorded</th><th>Outcome</th><th>Findings</th><th>Notes</th></tr>"
        f"{rows}</table>"
    )


def _findings_of(row: sqlite3.Row) -> list[dict[str, Any]]:
    findings = _loads(row["findings_json"], [])
    return [item for item in findings if isinstance(item, dict)] if isinstance(
        findings, list
    ) else []


def _reviewer_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "<p class=meta>No reviewer has been asked yet.</p>"
    body = "".join(
        "<tr>"
        f"<td><code>{_e(row['agent_id'])}</code></td>"
        f"<td>{_status_word(row['status'])}</td>"
        f"<td>{len(_findings_of(row))}</td>"
        f"<td class=bad>{_e(row['error_code'] or '')}</td>"
        + (
            f"<td><a href='/consultation/{_e(row['consultation_id'])}'>"
            f"<code>{_e(str(row['consultation_id'])[:8])}</code></a></td>"
            if row["consultation_id"]
            else "<td class=meta>--</td>"
        )
        + f"<td class=meta>{_e(row['created_at'])}</td></tr>"
        for row in rows
    )
    head = "<tr><th>reviewer<th>status<th>findings<th>error<th>consultation<th>answered</tr>"
    return f"<table>{head}{body}</table>"


def _findings_table(findings: list[dict[str, Any]]) -> str:
    """Every reviewer's findings in one table, worst first.

    Sorted by severity and not by reviewer on purpose: a lone Critical from one model
    is exactly the row that must not be buried under another model's Minors.
    """
    if not findings:
        return "<p class=meta>No findings recorded.</p>"
    order = {severity: rank for rank, severity in enumerate(SEVERITIES)}
    body = "".join(
        "<tr>"
        f"<td>{_severity(finding.get('severity'))}</td>"
        f"<td><code>{_e(finding.get('location') or '')}</code></td>"
        f"<td><code>{_e(finding.get('agent_id') or '')}</code></td>"
        f"<td>{_e(finding.get('why') or '')}</td>"
        f"<td>{_e(finding.get('fix') or '')}</td>"
        "</tr>"
        for finding in sorted(
            findings, key=lambda f: order.get(f.get("severity"), len(order))
        )
    )
    return (
        "<table><tr><th>severity<th>location<th>reviewer<th>why<th>fix</tr>"
        f"{body}</table>"
    )


def _severity(severity: object) -> str:
    if severity == "critical":
        return f"<span class=bad>{_e(severity)}</span>"
    return _e(severity)


def _synthesis(summary_json: object) -> str:
    """The host AI's conclusion, in the four columns the review format promises:
    problem, seriousness, who agreed, and what to do."""
    summary = _loads(summary_json, None)
    if not isinstance(summary, dict):
        return (
            "<p class=meta>Not written yet. Reviewers replying is not a finished "
            "review -- the conclusion arrives with <code>finalize_review</code>.</p>"
        )
    rows = "".join(
        "<tr>"
        f"<td>{_e(finding.get('problem') or '')}</td>"
        f"<td>{_severity(finding.get('severity'))}</td>"
        f"<td><code>{_e(', '.join(finding.get('agreed_by') or []) or '--')}</code>"
        + (
            f"<br><span class=meta>disagreed: "
            f"{_e(', '.join(finding.get('disagreed_by') or []))}</span>"
            if finding.get("disagreed_by")
            else ""
        )
        + f"</td><td>{_e(finding.get('proposed_action') or '')}</td></tr>"
        for finding in summary.get("combined_findings") or []
        if isinstance(finding, dict)
    )
    table = (
        "<table><tr><th>problem<th>seriousness<th>reviewers agreeing"
        f"<th>proposed action</tr>{rows}</table>"
        if rows
        else ""
    )
    return (
        f"<p>{_e(summary.get('summary') or '')}</p>{table}"
        f"<p><strong>Recommendation.</strong> {_e(summary.get('recommendation') or '')}</p>"
        + _list("Agreements", summary.get("agreements"))
        + _list("Disagreements", summary.get("disagreements"))
        + _list("Options", summary.get("options"))
        + _list("Checked", summary.get("checked"))
        + _list("Not checked", summary.get("not_checked"))
        + _citations(summary.get("citations"))
    )


def _list(label: str, items: object) -> str:
    if not isinstance(items, list) or not items:
        return ""
    listed = "".join(f"<li>{_e(item)}</li>" for item in items)
    return f"<p class=meta>{_e(label)}</p><ul>{listed}</ul>"


def _citations(sources: object) -> str:
    """Web-mode citations, carried through the synthesis rather than dropped in it.

    The URL is text, never a link: it came out of a model, and this page does not
    offer one-click navigation to somewhere a model named.
    """
    if not isinstance(sources, list) or not sources:
        return ""
    listed = "".join(
        f"<li>{_e(source.get('title') or '')} "
        f"<code>{_e(source.get('url') or '')}</code></li>"
        for source in sources
        if isinstance(source, dict)
    )
    return f"<p class=meta>Citations</p><ul>{listed}</ul>"


def _answers(rows: list[sqlite3.Row]) -> str:
    """Each reviewer's answer as it arrived, folded away.

    Folded because the point of the page above is the comparison; the original is
    what you open when the comparison looks wrong.
    """
    sections = [
        f"<details><summary><code>{_e(row['agent_id'])}</code> "
        f"&middot; {_status_word(row['status'])}</summary>"
        f"<pre>{_e(row['answer'])}</pre></details>"
        for row in rows
        if row["answer"]
    ]
    if not sections:
        return (
            "<p class=meta>Nothing stored. Either no reviewer answered, or "
            "<code>store_full_content</code> is off.</p>"
        )
    return "".join(sections)


def _not_reviewable(agent: AgentConfig | None, agent_id: str) -> str | None:
    """Why this agent cannot be named a reviewer, or None.

    The three rules `ConsultConfig.check_reviewer` applies at boot, restated against
    the agents this page can see. Restated rather than called, because that method
    reads the boot snapshot, and an agent added through this dashboard a minute ago
    is not in it -- refusing that one would refuse the very thing the form is for.
    """
    if agent is None:
        return f"`{agent_id}` is not a configured agent."
    if not agent.enabled:
        return f"`{agent_id}` is disabled. Enable it, or name another."
    if agent.score_for("review") <= 0:
        return (
            f"`{agent_id}` is not offered `review` work. Tick `review` in its "
            "capabilities, or name another."
        )
    return None


def _why_not(agent: AgentConfig, agent_id: str) -> str:
    problem = _not_reviewable(agent, agent_id)
    return f" -- {problem}" if problem else ""


def _reviewer_summary(review: ReviewConfig) -> str:
    return (
        f"<p>review: <code>{_e(', '.join(review.reviewers) or 'none')}</code><br>"
        f"deep review: <code>{_e(', '.join(review.deep_reviewers) or 'none')}</code></p>"
    )


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
        elif path == "/reviewers":
            status, body, location = self.dashboard.save_reviewers(form)
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
            address, closed, rest = host.partition("]")
            if not closed or (rest and not rest.startswith(":")):
                return False
            host, port = address + "]", rest[1:]
        elif host.count(":") == 1:
            host, _, port = host.partition(":")
        else:
            port = ""
        # Everything after the address used to be discarded, so `[::1]garbage` and
        # `[::1]:evil` were both read as `[::1]` and allowed. A port is digits or it is
        # not a port, and a host that spells itself that way is not one of ours.
        # `isascii` as well, because `isdigit` is true of `٥` and every other numeral in
        # Unicode, none of which a port is written in.
        if port and not (port.isascii() and port.isdigit()):
            return False
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


def build_httpd(config: ConsultConfig, config_path: Path | None = None) -> ThreadingHTTPServer:
    if config.dashboard.host not in ALLOWED_HOSTS:
        # The config validator already refuses this; kept because the bind is the
        # thing that actually matters and it should refuse on its own terms.
        raise ConfigError(f"dashboard.host must be a loopback address, not {config.dashboard.host}")

    handler = type(
        "_BoundHandler", (_Handler,), {"dashboard": ConsultDashboard(config, config_path)}
    )
    return ThreadingHTTPServer((config.dashboard.host, config.dashboard.port), handler)


def main() -> int:
    # Imported here rather than at module scope: `load_config` lives in the module
    # that builds the LiteLLM router, and a dashboard should not pay for that import
    # to print a usage error.
    import os

    from ..server import CONFIG_ENV, DEFAULT_CONFIG, load_config

    # Resolved here rather than left to `load_config`'s own default, because the
    # dashboard needs the path as well as the contents: it re-reads that file to see
    # which agent ids `config.yaml` claims now. See `ConsultDashboard._written_ids`.
    config_path = Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)
    consult = load_consult_config(load_config(config_path))
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

    httpd = build_httpd(consult, config_path)
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
