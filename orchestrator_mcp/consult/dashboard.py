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
:root {
  color-scheme: light dark;
  --ink: #101418;
  --panel: #ffffff;
  --panel-raised: #f8fafb;
  --paper: #f3f5f7;
  --text: #17212b;
  --muted: #697785;
  --rule: #d8dee4;
  --rule-strong: #bac4ce;
  --signal: #1677e8;
  --signal-soft: #e8f2ff;
  --good: #16805a;
  --good-soft: #e7f5ef;
  --bad: #c34646;
  --bad-soft: #fcecec;
  --warn: #9a6512;
  --warn-soft: #fff3dc;
  --shadow: 0 12px 36px #1d29331a;
  font-synthesis: none;
}
@media (prefers-color-scheme: dark) {
  :root {
    --panel: #171c21;
    --panel-raised: #1c2228;
    --paper: #101418;
    --text: #edf2f6;
    --muted: #8b98a5;
    --rule: #2b333b;
    --rule-strong: #3b4650;
    --signal: #63a8ff;
    --signal-soft: #182c42;
    --good: #65c9a3;
    --good-soft: #17332a;
    --bad: #ff8f8f;
    --bad-soft: #3a2224;
    --warn: #e7b35f;
    --warn-soft: #382d1e;
    --shadow: 0 14px 40px #00000038;
  }
}
* { box-sizing: border-box; }
html { background: var(--paper); }
body {
  margin: 0;
  background: var(--paper);
  color: var(--text);
  font: 13px/1.48 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  text-rendering: optimizeLegibility;
}
.app-shell { min-height: 100vh; display: grid; grid-template-columns: 13.5rem minmax(0, 1fr); }
.rail {
  position: sticky; top: 0; height: 100vh; align-self: start;
  display: flex; flex-direction: column; padding: 1.2rem .8rem;
  background: var(--ink); color: #edf2f6; border-right: 1px solid #2b333b;
}
.brand { display: flex; align-items: center; gap: .7rem; padding: .25rem .45rem 1.25rem; }
.brand-mark {
  display: grid; place-items: center; width: 2rem; height: 2rem;
  border: 1px solid #50606f; border-radius: 6px;
  color: #fff; font: 700 .66rem/1 ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: -.04em;
}
.brand-copy strong {
  display: block; font: 700 .91rem/1.1 "Arial Narrow", "Avenir Next Condensed", ui-sans-serif, sans-serif;
  letter-spacing: .015em;
}
.brand-copy span { display: block; margin-top: .22rem; color: #8b98a5; font-size: .67rem; }
.rail-label { margin: .45rem .55rem .35rem; color: #74818d; font-size: .63rem; font-weight: 750; letter-spacing: .13em; text-transform: uppercase; }
.rail nav { display: grid; gap: .18rem; }
.rail nav a {
  display: flex; align-items: center; gap: .65rem; min-height: 2.15rem;
  padding: .42rem .55rem; color: #aeb9c3; text-decoration: none;
  border: 1px solid transparent; border-radius: 5px;
}
.rail nav a::before { content: ""; width: .36rem; height: .36rem; border: 1px solid #5f6b76; border-radius: 50%; }
.rail nav a:hover { color: #fff; background: #ffffff0a; }
.rail nav a[aria-current=page] { color: #fff; background: #ffffff0d; border-color: #ffffff12; }
.rail nav a[aria-current=page]::before { background: var(--signal); border-color: var(--signal); box-shadow: 0 0 0 3px #63a8ff20; }
.rail-foot { margin-top: auto; padding: .75rem .55rem 0; border-top: 1px solid #2b333b; color: #7f8c97; font-size: .66rem; }
main { min-width: 0; width: 100%; max-width: 96rem; padding: 2rem 2.35rem 4rem; }
.page-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 1.5rem; margin-bottom: 1.15rem; }
.page-heading-copy { min-width: 0; }
.eyebrow { margin: 0 0 .38rem; color: var(--signal); font-size: .66rem; font-weight: 800; letter-spacing: .13em; text-transform: uppercase; }
h1, h2, h3, h4 { color: var(--text); font-family: "Arial Narrow", "Avenir Next Condensed", ui-sans-serif, sans-serif; }
h1 { margin: 0; font-size: clamp(1.55rem, 2.5vw, 2rem); line-height: 1.08; letter-spacing: -.025em; }
h2 { margin: 2.2rem 0 .7rem; font-size: 1rem; line-height: 1.2; letter-spacing: .01em; }
h3 { margin: 0; font-size: .95rem; }
h4 { margin: 1rem 0 .45rem; font-size: .82rem; }
p { margin: .6rem 0; }
a { color: inherit; text-decoration-color: color-mix(in srgb, currentColor 45%, transparent); text-underline-offset: .18em; }
a:hover { color: var(--signal); text-decoration-color: currentColor; }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {
  outline: 2px solid var(--signal); outline-offset: 2px;
}
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .82rem; }
code { font-variant-ligatures: none; }
.meta { color: var(--muted); font-size: .76rem; }
.context-line {
  display: flex; flex-wrap: wrap; gap: .35rem .8rem; margin: .85rem 0 0;
  color: var(--muted); font-size: .72rem;
}
.context-line > span { min-width: 0; overflow-wrap: anywhere; }
.back-link { display: inline-flex; align-items: center; gap: .35rem; margin-bottom: 1.2rem; color: var(--muted); font-size: .76rem; text-decoration: none; }
.back-link:hover { color: var(--signal); }
.monitor-strip {
  display: grid; grid-template-columns: repeat(4, minmax(8rem, 1fr));
  margin: 1.25rem 0 1.5rem; border: 1px solid var(--rule); border-radius: 7px;
  background: var(--panel); box-shadow: var(--shadow); overflow: hidden;
}
.monitor-strip > div { min-width: 0; padding: .72rem .85rem; border-right: 1px solid var(--rule); }
.monitor-strip > div:last-child { border-right: 0; }
.monitor-strip dt { color: var(--muted); font-size: .63rem; font-weight: 750; letter-spacing: .1em; text-transform: uppercase; }
.monitor-strip dd { margin: .25rem 0 0; font: 650 1rem/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; }
.section-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; border-bottom: 1px solid var(--rule); }
.section-heading h2 { margin-bottom: .58rem; }
.section-heading a { color: var(--muted); font-size: .72rem; }
.section-heading + .table-shell, .section-heading + .empty-state { margin-top: .7rem; }
.table-shell { width: 100%; overflow-x: auto; border: 1px solid var(--rule); border-radius: 7px; background: var(--panel); box-shadow: var(--shadow); }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: .56rem .68rem; text-align: left; vertical-align: top; border-bottom: 1px solid var(--rule); }
tr:last-child td { border-bottom: 0; }
th {
  color: var(--muted); background: var(--panel-raised); font-size: .62rem;
  font-weight: 800; letter-spacing: .09em; text-transform: uppercase; white-space: nowrap;
}
tbody tr { transition: background-color 100ms ease; }
tbody tr:hover { background: color-mix(in srgb, var(--signal) 4%, transparent); }
.data-table td { vertical-align: middle; }
.primary-cell { min-width: 13rem; }
.primary-cell > a, .primary-cell > strong { display: block; font-weight: 680; text-decoration: none; }
.primary-cell .meta { display: block; margin-top: .12rem; }
.route-cell { min-width: 11rem; }
.route-cell .meta { display: block; margin-top: .12rem; }
.capabilities { display: flex; flex-wrap: wrap; gap: .25rem; max-width: 24rem; }
.tag { display: inline-flex; align-items: center; min-height: 1.25rem; padding: .08rem .35rem; border: 1px solid var(--rule); border-radius: 4px; color: var(--muted); background: var(--panel-raised); font-size: .67rem; white-space: nowrap; }
.status { display: inline-flex; align-items: center; gap: .35rem; min-height: 1.35rem; padding: .08rem .42rem; border: 1px solid var(--rule); border-radius: 999px; font-size: .68rem; font-weight: 700; white-space: nowrap; }
.status::before { content: ""; width: .32rem; height: .32rem; border-radius: 50%; background: currentColor; }
.status + .meta { display: block; margin-top: .18rem; }
.status--good { color: var(--good); border-color: color-mix(in srgb, var(--good) 35%, var(--rule)); background: var(--good-soft); }
.status--bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 35%, var(--rule)); background: var(--bad-soft); }
.status--active { color: var(--signal); border-color: color-mix(in srgb, var(--signal) 35%, var(--rule)); background: var(--signal-soft); }
.status--muted { color: var(--muted); background: var(--panel-raised); }
.bad { color: var(--bad); font-weight: 680; }
.ok { color: var(--good); }
.empty-state { padding: 1.1rem; border: 1px dashed var(--rule-strong); border-radius: 7px; color: var(--muted); background: var(--panel); }
.rate-limit { display: flex; align-items: center; gap: .55rem; margin: .65rem 0 0; color: var(--muted); font-size: .72rem; }
.rate-limit::before { content: "budget"; padding: .06rem .32rem; border: 1px solid var(--rule); border-radius: 3px; font-size: .58rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.detail-header { padding-bottom: 1rem; border-bottom: 1px solid var(--rule); }
.detail-title { display: flex; align-items: center; flex-wrap: wrap; gap: .7rem; }
.detail-grid { display: grid; grid-template-columns: minmax(0, 1fr) 17rem; gap: 1.2rem; align-items: start; }
.detail-aside { position: sticky; top: 1rem; padding: .8rem; border: 1px solid var(--rule); border-radius: 7px; background: var(--panel); }
.detail-aside dl { margin: 0; display: grid; gap: .7rem; }
.detail-aside dt { color: var(--muted); font-size: .62rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.detail-aside dd { margin: .12rem 0 0; overflow-wrap: anywhere; }
.route-trace { position: relative; margin: .5rem 0 1rem; padding-left: 1.2rem; }
.route-trace::before { content: ""; position: absolute; left: .28rem; top: .7rem; bottom: .7rem; width: 1px; background: var(--rule-strong); }
.route-step { position: relative; margin: 0 0 .55rem; padding: .62rem .72rem; border: 1px solid var(--rule); border-radius: 6px; background: var(--panel); }
.route-step::before { content: ""; position: absolute; left: -1.13rem; top: .9rem; width: .43rem; height: .43rem; border: 2px solid var(--paper); border-radius: 50%; background: var(--signal); box-shadow: 0 0 0 1px var(--signal); }
.route-step p { margin: 0; }
.route-exclusions { margin: .5rem 0 0; padding: .45rem .6rem; border-top: 1px solid var(--rule); color: var(--muted); }
.route-exclusions ul { margin: .35rem 0 0; padding-left: 1.1rem; }
.turn { margin: .65rem 0; border: 1px solid var(--rule); border-radius: 7px; background: var(--panel); overflow: hidden; }
.turn-head { display: flex; justify-content: space-between; gap: 1rem; padding: .68rem .78rem; border-bottom: 1px solid var(--rule); background: var(--panel-raised); }
.turn-stats { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: .3rem .75rem; color: var(--muted); font: .68rem/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
.turn-body { padding: .25rem .78rem .7rem; }
.payload-block { margin-top: .7rem; }
.payload-block > .meta { margin: 0 0 .3rem; font-size: .64rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
pre { margin: 0; padding: .7rem .78rem; max-height: 28rem; overflow: auto; white-space: pre-wrap; word-break: break-word; border: 1px solid var(--rule); border-radius: 5px; background: var(--paper); color: var(--text); }
details { margin: .55rem 0; border: 1px solid var(--rule); border-radius: 5px; background: var(--panel); }
summary { cursor: pointer; padding: .48rem .62rem; color: var(--muted); font-size: .72rem; font-weight: 700; }
details > pre, details > p, details > table { margin: 0 .62rem .62rem; }
.form-shell { max-width: 52rem; }
.form-section { margin: 1rem 0; padding: 1rem; border: 1px solid var(--rule); border-radius: 7px; background: var(--panel); box-shadow: var(--shadow); }
.form-section-title { margin: 0 0 .85rem; padding-bottom: .55rem; border-bottom: 1px solid var(--rule); font-size: .72rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .8rem 1rem; }
label { display: block; margin: 0; }
label > span { display: block; margin-bottom: .28rem; color: var(--text); font-size: .72rem; font-weight: 700; }
.help { display: block; margin-top: .3rem; color: var(--muted); font-size: .68rem; font-weight: 450; line-height: 1.4; }
input[type=text], input[type=number], select {
  width: 100%; min-height: 2.2rem; padding: .38rem .5rem;
  border: 1px solid var(--rule-strong); border-radius: 5px; background: var(--paper); color: var(--text); font: inherit;
}
input[readonly] { color: var(--muted); background: var(--panel-raised); }
input[type=checkbox] { accent-color: var(--signal); }
.choice-row { display: flex; flex-wrap: wrap; gap: .45rem 1rem; }
.choice-row label, .scores label { display: inline-flex; align-items: center; gap: .38rem; min-height: 1.7rem; font-size: .73rem; }
fieldset { min-width: 0; margin: 0; padding: .72rem .8rem; border: 1px solid var(--rule); border-radius: 6px; }
legend { padding: 0 .3rem; color: var(--muted); font-size: .68rem; font-weight: 700; }
.form-actions { display: flex; align-items: center; gap: .7rem; margin-top: 1rem; }
button, .button { display: inline-flex; align-items: center; justify-content: center; min-height: 2.15rem; padding: .4rem .8rem; border: 1px solid var(--signal); border-radius: 5px; background: var(--signal); color: #071421; font: 750 .75rem/1 ui-sans-serif, sans-serif; cursor: pointer; text-decoration: none; }
button:hover, .button:hover { filter: brightness(1.06); color: #071421; }
button.danger { border-color: var(--bad); background: transparent; color: var(--bad); }
.banner { padding: .68rem .78rem; margin: 1rem 0; border: 1px solid var(--rule); border-left: 3px solid currentColor; border-radius: 5px; background: var(--panel); }
.banner.warn { color: var(--warn); background: var(--warn-soft); }
.banner.done { color: var(--good); background: var(--good-soft); }
.error { padding: .65rem .75rem; color: var(--bad); border: 1px solid color-mix(in srgb, var(--bad) 38%, var(--rule)); border-radius: 5px; background: var(--bad-soft); }
@media (max-width: 880px) {
  .app-shell { grid-template-columns: 1fr; }
  .rail { position: static; width: 100%; height: auto; padding: .55rem .7rem; }
  .brand { padding: .15rem .35rem .55rem; }
  .brand-mark { width: 1.65rem; height: 1.65rem; }
  .brand-copy span, .rail-label, .rail-foot { display: none; }
  .rail nav { display: flex; overflow-x: auto; }
  .rail nav a { flex: 0 0 auto; min-height: 1.9rem; }
  main { padding: 1.3rem 1rem 3rem; }
  .detail-grid { grid-template-columns: 1fr; }
  .detail-aside { position: static; order: -1; }
}
@media (max-width: 640px) {
  .page-heading { align-items: flex-start; flex-direction: column; gap: .7rem; }
  .monitor-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .monitor-strip > div:nth-child(2) { border-right: 0; }
  .monitor-strip > div:nth-child(-n+2) { border-bottom: 1px solid var(--rule); }
  .form-grid { grid-template-columns: 1fr; }
  .turn-head { flex-direction: column; }
  .turn-stats { justify-content: flex-start; }
  .table-shell--cards { overflow: visible; border: 0; background: transparent; box-shadow: none; }
  .table-shell--cards .data-table, .table-shell--cards tbody { display: block; }
  .table-shell--cards thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  .table-shell--cards tr { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-bottom: .55rem; padding: .55rem .68rem; border: 1px solid var(--rule); border-radius: 6px; background: var(--panel); box-shadow: var(--shadow); }
  .table-shell--cards td { display: block; padding: .28rem 0; border: 0; min-width: 0; overflow-wrap: anywhere; }
  .table-shell--cards td::before { content: attr(data-label); display: block; margin-bottom: .12rem; color: var(--muted); font-size: .57rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
  .table-shell--cards td.primary-cell { grid-column: 1 / -1; padding-bottom: .45rem; border-bottom: 1px solid var(--rule); margin-bottom: .18rem; }
  .table-shell--cards td.route-cell { min-width: 0; }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; } }
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
        actions = (
            "<div class=form-actions><a class=button href='/agents'>Configure agents</a>"
            "<a href='/reviewers'>Configure reviewers</a></div>"
            if self.config.dashboard.editable
            else ""
        )
        return _document(
            "Consultations",
            "<header class=page-heading><div class=page-heading-copy>"
            "<p class=eyebrow>Consult Protocol v1</p><h1>Operations monitor</h1>"
            "<p class=context-line>"
            f"<span>orchestrator-mcp-server {_e(self.version)}</span>"
            f"<span>{_e(str(self.config.database_path))}</span></p></div>{actions}</header>"
            f"{self._monitor_strip()}"
            "<section><div class=section-heading><h2>Consultations</h2>"
            "<span class=meta>Newest 200</span></div>"
            f"{self._consultations_table()}</section>"
            f"{self._reviews_section()}"
            "<section><div class=section-heading><h2>Agents</h2>"
            f"<span class=meta>{len(self.config.agents)} configured</span></div>"
            f"{self._agents_table()}{self._rate_limit_line()}</section>",
            editable=self.config.dashboard.editable,
        )

    def _open_reviews(self) -> int | None:
        """How many reviews are still mid-flight, or None where the tile does not belong.

        Reviews are the only thing on this page with a real lifecycle: `finalize`
        writes a terminal status, so this number goes up and comes back down. The
        tables exist in every migrated database though, so an install that has never
        configured a reviewer would get a tile that is permanently zero -- hidden on
        the same rule the reviews section uses.
        """
        if not self._reviews_ready():
            return None
        rows = self._query(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status "
            "NOT IN ('complete', 'failed', 'cancelled') THEN 1 ELSE 0 END) AS open FROM reviews"
        )
        if not rows or (not rows[0]["total"] and self.config.review is None):
            return None
        return int(rows[0]["open"] or 0)

    def _monitor_strip(self) -> str:
        """Counts that can change. A tile that reads the same number every time it is
        looked at is not being monitored, and one that reads the *total* while calling
        itself Active is worse than absent -- it says the server is busy when it is
        idle. Consultations never leave `open`: they stay resumable for as long as the
        row exists, so there is no consultation status worth counting here. Turns are
        the work that actually happened, and reviews are the thing that can be running
        right now."""
        rows = self._query(
            "SELECT COUNT(*) AS total, "
            "(SELECT COUNT(*) FROM consultation_turns) AS turns, "
            "(SELECT COUNT(*) FROM consultation_turns WHERE error_code IS NOT NULL) AS failures "
            "FROM consultations"
        )
        stats = rows[0] if rows else None
        total = int(stats["total"] or 0) if stats else 0
        turns = int(stats["turns"] or 0) if stats else 0
        failures = int(stats["failures"] or 0) if stats else 0
        enabled = sum(agent.enabled for agent in self.config.agents.values())
        open_reviews = self._open_reviews()
        # Omitted where reviews are unused: on such an install a permanent `0` is the
        # same lie in a smaller font.
        reviews = (
            f"<div><dt>Reviews open</dt><dd>{open_reviews}</dd></div>"
            if open_reviews is not None
            else ""
        )
        return (
            "<dl class=monitor-strip aria-label='Current operating state'>"
            f"<div><dt>Consultations</dt><dd>{total}</dd></div>"
            f"<div><dt>Turns / failed</dt><dd>{turns} / {failures}</dd></div>"
            f"{reviews}"
            f"<div><dt>Agents enabled</dt><dd>{enabled} / {len(self.config.agents)}</dd></div>"
            "</dl>"
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
            scores = "".join(
                f"<span class=tag>{_e(k)} {v}</span>" for k, v in sorted(agent.scores.items())
            ) or "<span class=meta>none</span>"
            rows.append(
                "<tr>"
                f"<td class=primary-cell data-label=Agent><strong><code>{_e(agent_id)}</code></strong>"
                f"<span class=meta>{_e(agent.runtime)} &middot; <code>{_e(agent.model)}</code></span></td>"
                f"<td data-label=Availability>{_status_cell(status)}"
                f"<span class=meta>{'enabled' if agent.enabled else 'disabled'}</span></td>"
                f"<td data-label=Routing><span class=meta>priority {agent.priority}</span>"
                f"<div class=capabilities>{scores}</div></td>"
                f"<td data-label=Web>{'yes' if agent.web_search else 'no'}</td>"
                # Text to copy, never a button: logging a runtime in is the operator's
                # action on their own account, and this server has no part in it.
                f"<td data-label='Connect with'><code>"
                f"{_e(adapter_for(agent, self.config).connect_command(agent))}</code></td>"
                "</tr>"
            )
        head = "<thead><tr><th>Agent<th>Availability<th>Routing<th>Web<th>Connect with</tr></thead>"
        return (
            "<div class='table-shell table-shell--cards'>"
            f"<table class=data-table>{head}<tbody>{''.join(rows)}</tbody></table></div>"
        )

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
            f"<p class=rate-limit>codex usage: {limit['used_percent']:.0f}%{window} used{when}{plan}"
            # Said plainly because it is: this is whatever the last consultation was
            # told, not a number this page went and asked for.
            " &middot; as of the last consultation</p>"
        )

    def _consultations_table(self) -> str:
        rows = self._query(
            "SELECT c.id, c.created_at, c.updated_at, c.target_agent_id, c.target_model, "
            "c.capability, c.conversation_label, c.status, "
            "(SELECT t.user_prompt FROM consultation_turns t WHERE t.consultation_id = c.id "
            " ORDER BY t.sequence_number LIMIT 1) AS first_prompt, "
            "(SELECT COUNT(*) FROM consultation_turns t WHERE t.consultation_id = c.id) AS turns, "
            "(SELECT COUNT(*) FROM consultation_turns t WHERE t.consultation_id = c.id "
            " AND t.error_code IS NOT NULL) AS failures "
            "FROM consultations c ORDER BY c.created_at DESC LIMIT 200"
        )
        if not rows:
            return (
                "<div class=empty-state><strong>No consultations recorded yet.</strong> "
                "New consultations will appear here with their route and current state.</div>"
            )

        body = "".join(
            "<tr>"
            f"<td class=primary-cell data-label=Consultation>"
            f"<a href='/consultation/{_e(row['id'])}'>"
            f"{_e(row['conversation_label'] or _short(row['first_prompt'], 72) or 'Untitled consultation')}</a>"
            f"<span class=meta><code>{_e(row['id'][:8])}</code> &middot; {_e(row['capability'])}</span></td>"
            f"<td class=route-cell data-label=Route><code>{_e(row['target_agent_id'])}</code>"
            f"<span class=meta><code>{_e(row['target_model'])}</code></span></td>"
            f"<td class=meta data-label=Started>{_e(row['created_at'])}</td>"
            f"<td data-label=Activity>{row['turns']} turn{'s' if row['turns'] != 1 else ''}"
            f"<span class={'bad' if row['failures'] else 'meta'}> &middot; "
            f"{row['failures']} failed</span></td>"
            "</tr>"
            for row in rows
        )
        # No State column. `status` is 'open' in every row of this table and always
        # will be, and a column that reads the same in every row is width, not data.
        head = "<thead><tr><th>Consultation<th>Route<th>Started<th>Activity</tr></thead>"
        return (
            "<div class='table-shell table-shell--cards'>"
            f"<table class=data-table>{head}<tbody>{body}</tbody></table></div>"
        )

    def consultation(self, consultation_id: str) -> tuple[int, str]:
        rows = self._query("SELECT * FROM consultations WHERE id = ?", (consultation_id,))
        if not rows:
            return HTTPStatus.NOT_FOUND, _document(
                "Not found", "<p>No such consultation.</p><p><a href='/'>Back</a></p>"
            )
        consultation = rows[0]
        return HTTPStatus.OK, _document(
            f"Consultation {consultation_id[:8]}",
            "<a class=back-link href='/'>&larr; Operations monitor</a>"
            "<header class=detail-header><p class=eyebrow>Consultation trace</p>"
            "<div class=detail-title>"
            f"<h1>{_e(consultation['capability'])} &rarr; "
            f"<code>{_e(consultation['target_agent_id'])}</code></h1></div>"
            f"<p class=context-line><span><code>{_e(consultation['id'])}</code></span>"
            f"<span>{_e(consultation['created_at'])}</span></p></header>"
            "<div class=detail-grid><div>"
            f"<h2>Routing</h2>{self._routing(consultation_id)}"
            f"<h2>Turns</h2>{self._turns(consultation_id)}</div>"
            "<aside class=detail-aside aria-label='Consultation metadata'><dl>"
            f"<div><dt>Agent</dt><dd><code>{_e(consultation['target_agent_id'])}</code></dd></div>"
            f"<div><dt>Model</dt><dd><code>{_e(consultation['target_model'])}</code></dd></div>"
            f"<div><dt>Runtime</dt><dd>{_e(consultation['target_runtime'])}</dd></div>"
            f"<div><dt>Asked by</dt><dd>{_e(consultation['origin_runtime'])}</dd></div>"
            f"<div><dt>Protocol</dt><dd>{_e(consultation['protocol_version'])}</dd></div>"
            f"<div><dt>Config</dt><dd><code>{_e(consultation['config_hash'])}</code></dd></div>"
            # The native session id is not shown: it is the consulted CLI's handle on
            # a live session, and a page has no use for it.
            f"<div><dt>Native session</dt><dd>"
            f"{'bound' if consultation['native_session_id'] else 'not bound'}</dd></div>"
            "</dl></aside></div>",
            editable=self.config.dashboard.editable,
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
                "<article class=route-step>"
                f"<p><span class=meta>{_e(row['capability'])} route</span><br>"
                f"selected <code>{_e(row['selected_agent'] or 'none')}</code> "
                f"<span class=meta>{'explicitly named' if row['explicit'] else 'by score'}</span>"
                + (f" &middot; <span class=bad>{_e(row['error_code'])}</span>"
                   if row["error_code"] else "")
                + "</p>"
                + ("<div class=route-exclusions><span>Not considered</span>"
                   f"<ul>{reasons}</ul></div>" if reasons else "")
                + "</article>"
            )
        return f"<div class=route-trace>{body}</div>"

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
                "<div class=turn-head>"
                f"<h3>Turn {row['sequence_number']} &middot; {_e(row['source_mode'])}</h3>"
                f"<div class=turn-stats><span>{row['latency_ms']} ms</span>"
                f"<span>{row['input_tokens']} in / {row['output_tokens']} out</span>"
                f"<span>{cost}</span><span>{_e(row['created_at'])}</span>"
                + (f" &middot; <span class=bad>{_e(row['error_code'])}</span>"
                   if row["error_code"] else "")
                + "</div></div>"
            )
            sections.append(
                "<article class=turn>" + header + "<div class=turn-body>"
                + _block("Prompt", row["user_prompt"])
                + _block("Context", row["context"])
                + _block("Compiled prompt", row["compiled_prompt"])
                + _block("Answer", _pretty(row["validated_response_json"]))
                + _block("Raw output", row["raw_output"])
                + "</div></article>"
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
                "<section><div class=section-heading><h2>Reviews</h2></div>"
                f"<div class=empty-state>{_e(missing)}</div></section>"
                if self.config.review is not None
                else ""
            )
        rows = self._review_rows(10)
        if not rows and self.config.review is None:
            return ""
        more = "<a href='/reviews'>All reviews</a>" if rows else ""
        return (
            "<section><div class=section-heading><h2>Reviews</h2>"
            f"{more}</div>{self._reviews_table(rows)}</section>"
        )

    def _reviews_table(self, rows: list[sqlite3.Row]) -> str:
        if not rows:
            return "<div class=empty-state>No reviews recorded yet.</div>"
        body = "".join(
            "<tr>"
            f"<td class=primary-cell data-label=Review>"
            f"<a href='/reviews/{_e(row['id'])}'>{_e(_short(row['goal'], 90) or 'Untitled review')}</a>"
            f"<span class=meta><code>{_e(row['id'][:8])}</code> &middot; {_e(row['mode'])}"
            f"{' &middot; recheck' if row['parent_review_id'] else ''}</span></td>"
            f"<td data-label=State>{_status_word(row['status'])}</td>"
            f"<td data-label=Outcome>{_e(row['outcome'] or '--')}</td>"
            f"<td data-label=Coverage>{row['reviewers']} reviewer{'s' if row['reviewers'] != 1 else ''}"
            f"<span class={'bad' if row['failures'] else 'meta'}> &middot; {row['failures']} failed</span></td>"
            f"<td class=meta data-label=Started>{_e(row['created_at'])}</td>"
            "</tr>"
            for row in rows
        )
        head = "<thead><tr><th>Review<th>State<th>Outcome<th>Coverage<th>Started</tr></thead>"
        return (
            "<div class='table-shell table-shell--cards'>"
            f"<table class=data-table>{head}<tbody>{body}</tbody></table></div>"
        )

    def reviews_page(self) -> str:
        head = (
            "<a class=back-link href='/'>&larr; Operations monitor</a>"
            "<header class=page-heading><div class=page-heading-copy>"
            "<p class=eyebrow>Independent review</p><h1>Reviews</h1>"
            "<p class=context-line><span>Recent review runs and rechecks</span></p>"
            "</div></header>"
        )
        missing = self._reviews_missing()
        if missing:
            return _document(
                "Reviews", f"{head}<div class=empty-state>{_e(missing)}</div>",
                editable=self.config.dashboard.editable,
            )
        return _document(
            "Reviews", head + self._reviews_table(self._review_rows(200)),
            editable=self.config.dashboard.editable,
        )

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
            "<a class=back-link href='/reviews'>&larr; All reviews</a>"
            "<header class=detail-header><p class=eyebrow>Independent review trace</p>"
            "<div class=detail-title>"
            f"<h1>{_e(review['mode'])} review</h1>{_status_word(review['status'])}</div>"
            f"<p class=context-line><span><code>{_e(review['id'])}</code></span>"
            f"<span>outcome {_e(review['outcome'] or '--')}</span>"
            f"<span>{_e(review['created_at'])}</span><span>updated {_e(review['updated_at'])}</span>"
            f"<span>web {'requested' if review['web_requested'] else 'off'}</span>"
            f"<span>{_secret_line(review['secret_hits_json'])}{parent}</span></p></header>"
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
            editable=self.config.dashboard.editable,
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
                "<a class=back-link href='/'>&larr; Operations monitor</a>"
                "<header class=page-heading><div class=page-heading-copy>"
                "<p class=eyebrow>Review configuration</p><h1>Reviewers</h1>"
                "</div></header><div class=empty-state>"
                f"<p>The <code>review:</code> block is defined in {_e(self._config_name)}. "
                "Edit it there, or delete it from that file first -- the server refuses "
                "to start with both.</p>"
                f"{_reviewer_summary(current)}</div>",
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
            "<a class=back-link href='/'>&larr; Operations monitor</a>"
            "<header class=page-heading><div class=page-heading-copy>"
            "<p class=eyebrow>Review configuration</p><h1>Reviewers</h1>"
            f"<p class=context-line><span>{_e(str(self.config.managed_agents_path))}</span></p>"
            "</div></header><div class=form-shell>"
            f"{notice}{self._restart_banner()}"
            + (f"<p class=error>{_e(error)}</p>" if error else "")
            + "<form method=post action='/reviewers'>"
            f"<input type=hidden name=_token value='{_e(self.token)}'>"
            "<section class=form-section><h2 class=form-section-title>Standard review</h2>"
            "<label><span>Reviewer</span>"
            f"<select name=reviewer><option value=''>none</option>{options}</select>"
            "<small class=help>A standard review asks one agent. Use deep review for independent comparison.</small>"
            "</label></section>"
            "<section class=form-section><h2 class=form-section-title>Deep review</h2>"
            f"<fieldset><legend>Select 1 to {MAX_DEEP_REVIEWERS} independent reviewers</legend>"
            f"<div class=choice-row>{boxes}</div></fieldset>"
            "<p class=help>Reviewers do not see one another's answers. An agent must be enabled and "
            "offered <code>review</code> work before it can be selected.</p></section>"
            "<div class=form-actions><button type=submit>Save reviewers</button>"
            "<a href='/'>Cancel</a></div></form></div>",
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
            f"<td class=primary-cell data-label=Agent><strong><code>{_e(aid)}</code></strong>"
            f"<span class=meta>{_e(agent.runtime)} &middot; <code>{_e(agent.model)}</code></span></td>"
            f"<td data-label=Effort>{_e(agent.reasoning_effort or '--')}</td>"
            f"<td data-label=State>{_status_word('active' if agent.enabled else 'disabled')}</td>"
            f"<td data-label=Capabilities><div class=capabilities>"
            f"{''.join(f'<span class=tag>{_e(k)} {v}</span>' for k, v in sorted(agent.scores.items())) or '<span class=meta>none</span>'}"
            f"</div></td>"
            f"<td data-label=Actions><a href='/agents/{_e(aid)}'>edit</a> &middot; "
            f"{_delete_form(aid, self.token)}</td>"
            "</tr>"
            for aid, agent in sorted(managed.items())
        )
        managed_table = (
            "<div class='table-shell table-shell--cards'><table class=data-table>"
            "<thead><tr><th>Agent<th>Effort<th>State<th>Capabilities<th>Actions</tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
            if rows
            else "<div class=empty-state>No agents configured here yet.</div>"
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
            "<a class=back-link href='/'>&larr; Operations monitor</a>"
            "<header class=page-heading><div class=page-heading-copy>"
            "<p class=eyebrow>Routing configuration</p><h1>Agents</h1>"
            f"<p class=context-line><span>{_e(str(self.config.managed_agents_path))}</span></p>"
            "</div><a class=button href='/agents/new'>Add an agent</a></header>"
            f"{notice}{self._restart_banner()}"
            f"{managed_table}"
            "<p><a href='/reviewers'>Configure reviewers</a></p>"
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
            "<a class=back-link href='/agents'>&larr; Agents</a>"
            "<header class=page-heading><div class=page-heading-copy>"
            "<p class=eyebrow>Routing configuration</p>"
            f"<h1>{'Edit' if editing else 'New'} agent</h1>"
            "<p class=context-line><span>Choose where this runtime can receive work.</span></p>"
            "</div></header><div class=form-shell>"
            + (f"<p class=error>{_e(error)}</p>" if error else "")
            + "<form method=post action='/agents'>"
            f"<input type=hidden name=_token value='{_e(self.token)}'>"
            # Which agent this form is replacing, so the save can tell an edit from a
            # new agent that happens to be typed with the same id. `readonly` above is
            # the browser's half of that and is not a check.
            + (f"<input type=hidden name=_editing value='{_e(v.get('id', ''))}'>" if editing else "")
            + "<section class=form-section><h2 class=form-section-title>Identity</h2>"
            "<div class=form-grid>"
            f"<label><span>Agent ID</span><input type=text name=id required "
            f"value='{_e(v.get('id', ''))}'{' readonly' if editing else ''}>"
            "<small class=help>Stable name used in routing records and URLs.</small></label>"
            f"<label><span>Runtime</span><select name=runtime>{runtimes}</select></label>"
            "<label><span>Command</span>"
            f"<input type=text name=command required value='{_e(v.get('command', ''))}'>"
            "<small class=help>A name on PATH or an absolute path. The bundled Codex CLI is not on PATH.</small></label>"
            "<label><span>Model</span>"
            f"<input type=text name=model required list=model-presets value='{_e(v.get('model', ''))}'>"
            "<small class=help>Choose a known slug or enter a newer one directly.</small></label>"
            f"<datalist id=model-presets>{models}</datalist>"
            "<label><span>Reasoning effort</span>"
            f"<select name=reasoning_effort><option value=''>unset</option>{efforts}</select>"
            "<small class=help>Codex only. Antigravity carries the level in the model slug.</small></label>"
            f"<label><span>Priority</span><input type=number name=priority min=0 "
            f"value='{_e(v.get('priority', '100'))}'>"
            "<small class=help>Lower priority wins when scores tie.</small></label>"
            "</div></section>"
            "<section class=form-section><h2 class=form-section-title>Routing</h2>"
            "<div class=choice-row>"
            f"<label><input type=checkbox name=enabled{' checked' if v.get('enabled') else ''}> enabled</label>"
            f"<label><input type=checkbox name=web_search{' checked' if v.get('web_search') else ''}> "
            "web search</label></div>"
            "<p class=help>Web search must be enabled before a web-mode consultation can route here.</p>"
            "<fieldset class=scores><legend>Capabilities offered to this agent</legend>"
            f"{scores}</fieldset></section>"
            f"<div class=form-actions><button type=submit>{'Save changes' if editing else 'Add agent'}</button>"
            "<a href='/agents'>Cancel</a></div></form></div>",
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
        "<button class=danger type=submit>delete</button></form>"
    )


# --- rendering helpers ------------------------------------------------------


def _e(value: object) -> str:
    """Escape. Every value on this page came from a caller or a consulted agent."""
    return html.escape(str(value), quote=True)


def _block(label: str, value: str | None) -> str:
    if not value:
        return ""
    return (
        "<section class=payload-block>"
        f"<p class=meta>{_e(label)}</p><pre>{_e(value)}</pre></section>"
    )


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
        return "<span class='status status--muted'>never checked</span>"
    when = f"<span class=meta>last checked {_e(row['checked_at'])}</span>"
    if row["installed"] and row["authenticated"]:
        return f"<span class='status status--good'>ready</span>{when}"
    label = "not installed" if not row["installed"] else "not connected"
    detail = f"<span class=meta>{_e(row['detail'])}</span>" if row["detail"] else ""
    return f"<span class='status status--bad'>{label}</span>{detail}{when}"


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

    `open` is deliberately absent: it belongs to consultations, which this page no
    longer badges at all, because every consultation that has ever been recorded is
    open and stays that way. Should anything render it again, muted is the honest
    colour for it.
    """
    text = _e(status)
    if status == "complete":
        return f"<span class='status status--good'>{text}</span>"
    if status in ("failed", "cancelled"):
        return f"<span class='status status--bad'>{text}</span>"
    if status in ("running", "active", "awaiting_synthesis"):
        return f"<span class='status status--active'>{text}</span>"
    return f"<span class='status status--muted'>{text}</span>"


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
        f"<code>{_e(source.get('locator') or '')}</code></li>"
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


def _navigation(title: str, editable: bool = False) -> str:
    section = (
        "reviewers" if title == "Reviewers"
        else "reviews" if title.startswith("Review")
        else "agents" if "agent" in title.lower()
        else "monitor"
    )
    # Editing pages already passed the editable guard, so they retain their local
    # navigation even when rendered from an error path that did not pass the flag on.
    show_admin = editable or section in ("agents", "reviewers")
    items = [
        ("monitor", "/", "Monitor"),
        ("reviews", "/reviews", "Reviews"),
    ]
    if show_admin:
        items.extend(
            (("agents", "/agents", "Agents"), ("reviewers", "/reviewers", "Reviewers"))
        )
    links = "".join(
        f"<a href='{href}'{' aria-current=page' if key == section else ''}>{label}</a>"
        for key, href, label in items
    )
    admin_label = "<p class=rail-label>Configure</p>" if show_admin else ""
    return (
        "<aside class=rail>"
        "<div class=brand><span class=brand-mark>O/M</span>"
        "<span class=brand-copy><strong>orchestrator-mcp</strong>"
        "<span>local control surface</span></span></div>"
        "<p class=rail-label>Operate</p><nav aria-label=Primary>"
        f"{links}</nav>{admin_label}"
        "<p class=rail-foot>Loopback interface<br>Stored content stays local</p>"
        "</aside>"
    )


def _document(title: str, body: str, *, editable: bool = False) -> str:
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)} &middot; orchestrator-mcp</title>"
        f"<style>{STYLE}</style><body><div class=app-shell>{_navigation(title, editable)}"
        f"<main id=main>{body}</main></div></body>"
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
    # that builds the whole MCP surface, and a dashboard should not pay for that
    # import to print a usage error.
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
