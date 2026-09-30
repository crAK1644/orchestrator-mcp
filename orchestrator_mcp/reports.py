"""Read-only reports over the consultation database: `usage`, `history`, `scorecard`,
`export` and `search`.

These run instead of the server, from a terminal, and open the file `mode=ro` -- never
through `ConsultStore`, which migrates. A database older than this version therefore
gets a sentence, not a schema change; the server is what migrates it.

Everything printed is stored history, and stored history is as sensitive as the prompts
that made it. It is masked on the way out, which also covers rows written before a
pattern existed, but the masking is best effort and the output is yours to look after.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import textwrap
from collections import defaultdict
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from .consult.config import load_consult_config
from .consult.errors import ConsultErrorCode

# `_spend` and `_ORDINARY_SQL` stay private to the store, which owns both rules; a report
# that wrote its own copy of either would be the second place to get them wrong.
from .consult.store import _ORDINARY_SQL, MIGRATIONS, SPEND_COLUMNS, _spend
from .contract import ConfigError, redact, scrub_json
from .spend import Spend, tallied

NOT_MIGRATED = (
    "This database is older than this version, and this opens it read-only, so it cannot "
    "migrate it -- start the MCP server once and it will."
)

# Below this many decided findings a reviewer's hit rate is anecdote, not a rate.
MIN_DECIDED = 10
MIN_PREFIX = 8
# The dashboard's own cap on `days`; a `timedelta` overflows a `datetime` far beyond it.
MAX_DAYS = 3650
MAX_LIMIT = 1000
# `search`: hits at most, and how much of a query is read, so a pasted document does not
# become a MATCH expression.
MAX_HITS = 25
MAX_QUERY_CHARS = 200

def fail(message: str) -> NoReturn:
    raise SystemExit(f"orchestrator-mcp-server: {message}")


# --- the connection -----------------------------------------------------------


def open_readonly(path: Path) -> tuple[sqlite3.Connection | None, str]:
    """The database read-only, or `(None, why not)`. It never raises `SystemExit`.

    `connect` is this for a terminal. The MCP server cannot exit, so its tool takes the
    reason and hands it to the model as text.
    """
    path = Path(path)
    if not path.exists():
        return None, f"no database at {path} yet; nothing has been consulted"
    # `as_uri` percent-encodes, which a bare f-string would not: a `?` or `#` in the
    # path would otherwise end it early.
    db = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
    )
    db.row_factory = sqlite3.Row
    # A SELECT alone opens no transaction, so each statement would read the database as
    # it stood at that moment. Another server may finalize or delete between them; an
    # explicit BEGIN makes the first read pin one snapshot for the whole report.
    db.execute("BEGIN")
    if reason := ledger_problem(db):
        db.close()
        return None, reason
    return db, ""


def connect(path: Path) -> sqlite3.Connection:
    """The database, read-only, or a `SystemExit` saying why it cannot be read."""
    db, reason = open_readonly(path)
    if db is None:
        fail(reason)
    return db


def ledger_problem(db: sqlite3.Connection) -> str:
    """Why this connection cannot be read as the current schema, or "" when it can.

    The ledger rather than a probe for one table: the scorecard reads columns from
    migration 2 and 9, so "the reviews table exists" proves too little.
    """
    try:
        (latest,) = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return NOT_MIGRATED
        raise
    except sqlite3.DatabaseError as error:
        return f"this is not a readable database: {error}"
    return "" if latest is not None and latest >= len(MIGRATIONS) - 1 else NOT_MIGRATED


# --- shared pieces ------------------------------------------------------------


def _cutoff(days: int) -> str:
    """The same shape `_now` writes and `_sweep` compares against."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _clean(text: str | None) -> str:
    """Masked, and with the bytes a terminal would obey turned into spaces."""
    return _CONTROL.sub(" ", redact(text or "")).strip()


def _shorten(text: str, width: int = 60) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def _spend_fields(spend: Spend) -> dict[str, Any]:
    usage = spend.usage
    return {
        "turns": spend.turns,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        # `None` is unknown, not free: some turn in the group reported no price.
        "cost_usd": usage.cost_usd,
        "known_cost_usd": spend.known_cost_usd,
        "caveats": [_clean(note) for note in usage.counts_incomplete],
    }


def cost_text(fields: dict[str, Any]) -> str:
    if fields["cost_usd"] is not None:
        return f"${fields['cost_usd']:.4f}"
    known = fields["known_cost_usd"]
    return f"unknown (>= ${known:.4f})" if known else "unknown"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[_clean(str(cell)) for cell in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    return "\n".join(lines)


def _notes(notes: Sequence[str]) -> str:
    return "".join(
        "\n" + textwrap.fill(_clean(note), 100, initial_indent="  - ", subsequent_indent="    ")
        for note in notes
    )


# --- usage --------------------------------------------------------------------


def usage(db: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Turns, tokens and cost per agent and model over the last `days`.

    Every turn counts, including a reviewer's and a workflow step's: spend is spend
    whichever tool asked for it.
    """
    rows = db.execute(
        f"SELECT c.target_agent_id AS agent_id, c.target_model AS model, {SPEND_COLUMNS}, "
        "COALESCE(SUM(t.error_code IS NOT NULL), 0) AS errors, "
        "COALESCE(AVG(t.latency_ms), 0) AS avg_latency_ms "
        "FROM consultation_turns t JOIN consultations c ON c.id = t.consultation_id "
        "WHERE t.created_at >= ? GROUP BY 1, 2 ORDER BY total_tokens DESC, 1, 2",
        (_cutoff(days),),
    ).fetchall()
    groups = []
    for row in rows:
        groups.append(
            {
                "agent_id": _clean(row["agent_id"]),
                "model": _clean(row["model"]),
                "errors": row["errors"],
                "avg_latency_ms": round(row["avg_latency_ms"]),
                **_spend_fields(_spend(row)),
            }
        )
    # The total follows the groups' own rule: a sum is a price only if every group's is.
    known = sum(g["known_cost_usd"] for g in groups)
    priced = bool(groups) and all(g["cost_usd"] is not None for g in groups)
    return {
        "days": days,
        "groups": groups,
        "total": {
            "turns": sum(g["turns"] for g in groups),
            "errors": sum(g["errors"] for g in groups),
            "total_tokens": sum(g["total_tokens"] for g in groups),
            "cost_usd": known if priced else None,
            "known_cost_usd": known,
        },
        "caveats": tallied(note for g in groups for note in g["caveats"]),
    }


def render_usage(report: dict[str, Any]) -> str:
    if not report["groups"]:
        return f"No turns in the last {report['days']} days."
    total = report["total"]
    rows = [
        (g["agent_id"], g["model"], g["turns"], g["errors"], f"{g['total_tokens']:,}",
         cost_text(g), f"{g['avg_latency_ms']} ms")
        for g in report["groups"]
    ]
    rows.append(
        ("total", "", total["turns"], total["errors"], f"{total['total_tokens']:,}",
         cost_text(total), "")
    )
    return (
        f"Last {report['days']} days\n"
        + _table(("agent", "model", "turns", "errors", "tokens", "cost", "avg latency"), rows)
        + _notes(report["caveats"])
    )


# --- history ------------------------------------------------------------------

KINDS = ("consultation", "review", "workflow")


def history(db: sqlite3.Connection, limit: int = 20, kind: str | None = None) -> list[dict]:
    """Newest first across consultations, reviews and workflows.

    A consultation that a review or workflow owns is left out, as the tools that list
    them leave it out: it shows under its owner, and would otherwise bury everything.
    """
    items: list[dict] = []
    if kind in (None, "consultation"):
        for row in db.execute(
            "SELECT c.id, c.updated_at, c.target_agent_id, c.capability, c.conversation_label, "
            "(SELECT COUNT(*) FROM consultation_turns t WHERE t.consultation_id = c.id) AS turns, "
            "(SELECT t.error_code FROM consultation_turns t WHERE t.consultation_id = c.id "
            " ORDER BY t.sequence_number DESC LIMIT 1) AS last_error "
            f"FROM consultations c WHERE {_ORDINARY_SQL} ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        ):
            state = f"{row['turns']} turns" + (
                f", last: {row['last_error']}" if row["last_error"] else ""
            )
            subject = row["conversation_label"] or f"{row['target_agent_id']} {row['capability']}"
            items.append(_item("consultation", row, state, subject))
    if kind in (None, "review"):
        for row in db.execute(
            "SELECT id, updated_at, status, outcome, goal FROM reviews "
            "WHERE workflow_id IS NULL ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ):
            state = row["status"] + (f" ({row['outcome']})" if row["outcome"] else "")
            items.append(_item("review", row, state, row["goal"]))
    if kind in (None, "workflow"):
        for row in db.execute(
            "SELECT id, updated_at, status, goal FROM workflow_runs "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ):
            items.append(_item("workflow", row, row["status"], row["goal"]))
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items[:limit]


def _item(kind: str, row: sqlite3.Row, state: str, subject: str | None) -> dict:
    return {
        "kind": kind,
        "id": row["id"],
        "state": state,
        "updated_at": row["updated_at"],
        "subject": _shorten(_clean(subject)),
    }


def render_history(items: list[dict]) -> str:
    if not items:
        return "Nothing recorded yet."
    return _table(
        ("kind", "id", "state", "updated", "subject"),
        [(i["kind"], i["id"], i["state"], i["updated_at"], i["subject"]) for i in items],
    )


# --- scorecard ----------------------------------------------------------------

# Reviewers that ran, in reviews that are not a recheck of another review (which would
# raise the same findings again). The review's own status is no filter: a cancel keeps
# the rows of reviewers that had already answered, and their turns are in `usage`. One
# that never started, which is what every reviewer of a review still going looks like
# until it answers, was not asked.
_SCORED = (
    "r.parent_review_id IS NULL AND r.created_at >= ? "
    f"AND rc.error_code IS NOT '{ConsultErrorCode.NOT_STARTED.value}'"
)


def scorecard(db: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Per reviewer and model: how it answered, what it cost, and what became of its findings.

    What became of a finding is the host's account, given when it finalized the review.
    This server checks none of it, and a later recheck never rewrites it.
    """
    cutoff = _cutoff(days)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    models: dict[str, dict[str, str]] = defaultdict(dict)
    summaries: dict[str, str | None] = {}
    unsynthesised = 0
    for row in db.execute(
        "SELECT r.id AS review_id, r.status AS review_status, r.summary_json, rc.agent_id, "
        "rc.status, COALESCE(c.target_model, '') AS model "
        "FROM review_consultations rc JOIN reviews r ON r.id = rc.review_id "
        f"LEFT JOIN consultations c ON c.id = rc.consultation_id WHERE {_SCORED}",
        (cutoff,),
    ):
        group = groups.setdefault(
            (row["agent_id"], row["model"]),
            {"asked": 0, "answered": 0, "errored": 0, "kept": 0, "rejected": 0, "open": 0,
             "with_summary": 0},
        )
        group["asked"] += 1
        group["answered" if row["status"] == "ok" else "errored"] += 1
        models[row["review_id"]][row["agent_id"]] = row["model"]
        if row["review_id"] not in summaries:
            summaries[row["review_id"]] = row["summary_json"]
            # Answered, and no synthesis on disk: the host has not finalized it yet, or
            # `store_full_content: false`, under which a review cannot be finalized.
            unsynthesised += (
                row["review_status"] in ("awaiting_synthesis", "complete", "cancelled")
                and row["summary_json"] is None
            )
        if row["summary_json"] is not None:
            group["with_summary"] += 1

    for review_id, raw in summaries.items():
        try:
            findings = json.loads(raw)["combined_findings"] if raw else []
        except (ValueError, KeyError, TypeError):
            findings = []
        for finding in findings:
            contributors = {i.rsplit("-", 1)[0] for i in finding.get("source_finding_ids", [])}
            outcome = {"fixed": "kept", "accepted_risk": "kept", "rejected": "rejected"}.get(
                finding.get("disposition"), "open"
            )
            for agent_id in contributors:
                if (model := models[review_id].get(agent_id)) is not None:
                    groups[(agent_id, model)][outcome] += 1

    spend = {
        (row["agent_id"], row["model"]): (_spend(row), round(row["avg_latency_ms"]))
        for row in db.execute(
            f"SELECT rc.agent_id AS agent_id, COALESCE(c.target_model, '') AS model, "
            f"{SPEND_COLUMNS}, COALESCE(AVG(t.latency_ms), 0) AS avg_latency_ms "
            "FROM consultation_turns t JOIN consultations c ON c.id = t.consultation_id "
            "JOIN review_consultations rc ON rc.consultation_id = c.id "
            f"JOIN reviews r ON r.id = rc.review_id WHERE {_SCORED} GROUP BY 1, 2",
            (cutoff,),
        )
    }
    rows = []
    for (agent_id, model), group in sorted(groups.items()):
        decided = group["kept"] + group["rejected"]
        counted = group.pop("with_summary") > 0
        entry: dict[str, Any] = {"agent_id": _clean(agent_id), "model": _clean(model), **group}
        if not counted:
            # Nothing was kept to count, which is not the same as nothing was found.
            entry |= {"kept": None, "rejected": None, "open": None}
        entry["decided"] = decided if counted else None
        entry["precision"] = group["kept"] / decided if decided >= MIN_DECIDED else None
        found = spend.get((agent_id, model))
        entry["avg_latency_ms"] = found[1] if found else None
        entry |= _spend_fields(found[0]) if found else {"cost_usd": None, "caveats": []}
        rows.append(entry)
    return {
        "days": days,
        "reviewers": rows,
        "unsynthesised_reviews": unsynthesised,
        "caveats": tallied(note for r in rows for note in r["caveats"]),
    }


SCORECARD_NOTE = (
    "Kept is fixed or accepted as a risk, rejected is rejected, open is neither yet. "
    "These are the host's dispositions at finalize; this server checks none of them, and a "
    "later recheck does not update them. Counts are per combined finding, once for each "
    "reviewer that raised it, not per raw reviewer finding. A reviewer's hit rate is shown "
    f"from {MIN_DECIDED} decided findings."
)


SCORECARD_HEADERS = (
    "reviewer", "model", "asked", "answered", "errored", "kept", "rejected", "open",
    "hit rate", "avg latency", "cost",
)  # fmt: skip


def scorecard_cells(entry: dict[str, Any]) -> tuple[str, ...]:
    """One reviewer's row as text, so the terminal and the dashboard print the same."""

    def count(value: int | None) -> str:
        return "-" if value is None else str(value)

    if entry["precision"] is not None:
        rate = f"{entry['precision']:.0%}"
    else:
        rate = "-" if entry["decided"] is None else f"n<{MIN_DECIDED}"
    return (
        entry["agent_id"],
        entry["model"],
        str(entry["asked"]),
        str(entry["answered"]),
        str(entry["errored"]),
        count(entry["kept"]),
        count(entry["rejected"]),
        count(entry["open"]),
        rate,
        "-" if entry["avg_latency_ms"] is None else f"{entry['avg_latency_ms']} ms",
        cost_text(entry) if "known_cost_usd" in entry else "-",
    )


def scorecard_footnotes(report: dict[str, Any]) -> list[str]:
    notes = [SCORECARD_NOTE]
    if report["unsynthesised_reviews"]:
        notes.append(
            f"Reviews with no synthesis on record: {report['unsynthesised_reviews']}. Not "
            "finalized yet, or `store_full_content: false`, under which none is kept; "
            "their findings are not counted."
        )
    return notes


def render_scorecard(report: dict[str, Any]) -> str:
    if not report["reviewers"]:
        return f"No reviews in the last {report['days']} days."
    return (
        f"Last {report['days']} days\n"
        + _table(SCORECARD_HEADERS, [scorecard_cells(r) for r in report["reviewers"]])
        + "\n\n"
        + "\n".join(textwrap.fill(note, 100) for note in scorecard_footnotes(report))
        + _notes(report["caveats"])
    )


# --- export -------------------------------------------------------------------

# Never printed. A session id is a credential for resuming a conversation (the dashboard
# hides it for the same reason), and the rest are the secrets a confirm or a lease is
# checked against.
_HIDDEN = {"native_session_id", "lease_holder", "lease_expires_at"}


def _record(row: sqlite3.Row) -> dict[str, Any]:
    """One row as a dict, JSON columns parsed so masking runs over leaves, not blobs."""
    record: dict[str, Any] = {}
    for key in row.keys():  # noqa: SIM118 -- sqlite3.Row has no items()
        if key in _HIDDEN or key.endswith("_token_sha"):
            continue
        value = row[key]
        if isinstance(value, str) and (key.endswith("_json") or key == "counts_incomplete"):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        record[key] = value
    return record


def _resolve(db: sqlite3.Connection, ident: str) -> tuple[str, str]:
    ident = ident.strip()
    if len(ident) < MIN_PREFIX:
        fail(f"an id needs at least {MIN_PREFIX} characters; `history` prints full ones")
    found: list[tuple[str, str]] = []
    for kind, table in (
        ("consultation", "consultations"),
        ("review", "reviews"),
        ("workflow", "workflow_runs"),
    ):
        # `substr`, not `LIKE`: an underscore or percent sign in what was typed would
        # otherwise match anything.
        found += [
            (kind, row[0])
            for row in db.execute(
                f"SELECT id FROM {table} WHERE substr(id, 1, ?) = ? LIMIT 6", (len(ident), ident)
            )
        ]
    if not found:
        fail(f"nothing recorded under `{_clean(ident)}`")
    if len(found) > 1:
        fail(
            f"`{_clean(ident)}` matches more than one record ("
            + ", ".join(f"{kind} {id_}" for kind, id_ in found[:5])
            + "); type more of the id"
        )
    return found[0]


def _one(db: sqlite3.Connection, sql: str, *params: str) -> dict[str, Any]:
    return _record(db.execute(sql, params).fetchone())


def _many(db: sqlite3.Connection, sql: str, *params: str) -> list[dict[str, Any]]:
    return [_record(row) for row in db.execute(sql, params)]


def _ids(db: sqlite3.Connection, sql: str, *params: str) -> list[str]:
    return [row[0] for row in db.execute(sql, params)]


def export(db: sqlite3.Connection, ident: str) -> dict[str, Any]:
    """Everything stored about one consultation, review or workflow, masked.

    What it owns is named by id rather than inlined: a review lists its reviewers'
    consultation ids, a workflow its steps' consultations and reviews. `export` any of
    those to read them.
    """
    kind, id_ = _resolve(db, ident)
    document: dict[str, Any] = {"kind": kind}
    if kind == "consultation":
        document["consultation"] = _one(db, "SELECT * FROM consultations WHERE id = ?", id_)
        document["turns"] = _many(
            db,
            "SELECT * FROM consultation_turns WHERE consultation_id = ? ORDER BY sequence_number",
            id_,
        )
        if document["turns"] and all(t["compiled_prompt"] is None for t in document["turns"]):
            document["note"] = _NOT_KEPT
    elif kind == "review":
        document["review"] = _one(db, "SELECT * FROM reviews WHERE id = ?", id_)
        document["reviewers"] = _many(
            db, "SELECT * FROM review_consultations WHERE review_id = ? ORDER BY agent_id", id_
        )
        document["rechecks"] = _ids(
            db, "SELECT id FROM reviews WHERE parent_review_id = ? ORDER BY created_at", id_
        )
        if any(r["status"] == "ok" and r["answer"] is None for r in document["reviewers"]):
            document["note"] = _NOT_KEPT
    else:
        document["workflow"] = _one(db, "SELECT * FROM workflow_runs WHERE id = ?", id_)
        document["steps"] = _many(
            db, "SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY sequence", id_
        )
        document["consultations"] = _ids(
            db, "SELECT id FROM consultations WHERE workflow_id = ? ORDER BY created_at", id_
        )
        document["reviews"] = _ids(
            db, "SELECT id FROM reviews WHERE workflow_id = ? ORDER BY created_at", id_
        )
    return scrub_json(document)


_NOT_KEPT = (
    "The model output in this record is empty because the database was written with "
    "`store_full_content: false`; only its shape was kept."
)


# --- search -------------------------------------------------------------------

# The index is an FTS5 table in `temp`: in memory, private to this connection, gone when it
# closes. The database file is never written, which is what lets `search` keep this
# module's rule. It holds masked text, so what can be found is what is shown: a credential
# in an old row is neither printed nor findable by guessing at it.
#
# ponytail: rebuilt on every call, at about 0.2 s per MB of stored text, nearly all of it
# masking. A persistent index is a migration and triggers; add it when a database is big
# enough to feel slow.

_NO_TEXT = (
    "Nothing to search: this database was written with `store_full_content: false`, so "
    "no prompt or answer was kept."
)
_NO_FTS5 = "This Python's SQLite was built without FTS5, so search is unavailable."


def _words(query: str) -> list[str]:
    """What was typed, as words. A control byte ends one: a NUL would end the MATCH string."""
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"a query is at most {MAX_QUERY_CHARS} characters; use fewer words")
    words = _CONTROL.sub(" ", query).split()
    if not words:
        raise ValueError("give at least one word to search for")
    return words


def _match(words: Sequence[str]) -> str:
    """The words as quoted phrases, each of which must appear.

    FTS5 reads `AND`, `NEAR(`, `*`, `^` and `column:` as syntax, and a stray quote as an
    error. Quoted, each is a word to look for.
    """
    return " ".join('"' + word.replace('"', '""') + '"' for word in words)


def _result(
    query: str,
    days: int | None,
    searched: int = 0,
    hits: list[dict[str, Any]] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {"query": query, "days": days, "searched": searched, "hits": hits or [], "note": note}


def search(
    db: sqlite3.Connection, query: str, days: int | None = None, limit: int = 10
) -> dict[str, Any]:
    """The stored prompts and answers that hold every word of `query`, best match first.

    A reviewer's answer and a workflow step's are turns like any other, so one index
    covers them. A hit is a masked excerpt and the ids to read the rest with. A
    `ValueError` says the query cannot be searched; a database that cannot be is a `note`.
    """
    words = _words(query)
    shown = _clean(" ".join(words))
    cutoff = _cutoff(days) if days else ""
    limit = min(max(limit, 1), MAX_HITS)  # a negative LIMIT is no limit at all
    db.execute("PRAGMA temp_store = MEMORY")
    db.execute("DROP TABLE IF EXISTS temp.hay")
    try:
        db.execute(
            "CREATE VIRTUAL TABLE temp.hay USING fts5("
            "text, turn_id UNINDEXED, part UNINDEXED, tokenize = 'porter unicode61')"
        )
    except sqlite3.OperationalError as error:
        if "fts5" not in str(error):
            raise
        return _result(shown, days, note=_NO_FTS5)
    db.create_function("mask", 1, _clean, deterministic=True)
    db.execute(
        "INSERT INTO temp.hay(text, turn_id, part) "
        "SELECT mask(user_prompt), id, 'prompt' FROM consultation_turns "
        "WHERE user_prompt IS NOT NULL AND created_at >= :cutoff "
        "UNION ALL "
        "SELECT mask(json_extract(validated_response_json, '$.answer')), id, 'answer' "
        "FROM consultation_turns WHERE created_at >= :cutoff AND "
        # AND is not evaluated left to right, so the guard is a CASE: `json_type` on text
        # that is not JSON is an error, not a NULL.
        "CASE WHEN json_valid(validated_response_json) "
        "THEN json_type(validated_response_json, '$.answer') END = 'text'",
        {"cutoff": cutoff},
    )
    (searched,) = db.execute("SELECT COUNT(*) FROM temp.hay").fetchone()
    if not searched:
        (turns,) = db.execute(
            "SELECT COUNT(*) FROM consultation_turns WHERE created_at >= ?", (cutoff,)
        ).fetchone()
        empty = f"No turns in the last {days} days." if days else "Nothing recorded yet."
        return _result(shown, days, note=_NO_TEXT if turns else empty)
    rows = db.execute(
        "SELECT h.part, h.excerpt, t.consultation_id, t.sequence_number, t.created_at, "
        "c.target_agent_id, c.target_model, c.capability, c.conversation_label, rc.review_id "
        "FROM (SELECT turn_id, part, rank, snippet(hay, 0, '«', '»', ' ... ', 24) AS excerpt "
        "      FROM temp.hay WHERE hay MATCH :match ORDER BY rank LIMIT :limit) h "
        "JOIN consultation_turns t ON t.id = h.turn_id "
        "JOIN consultations c ON c.id = t.consultation_id "
        "LEFT JOIN review_consultations rc ON rc.consultation_id = c.id "
        "ORDER BY h.rank, t.created_at DESC, h.turn_id, h.part",
        {"match": _match(words), "limit": limit},
    ).fetchall()
    hits = [
        {
            "consultation_id": row["consultation_id"],
            "turn": row["sequence_number"],
            "part": row["part"],
            "agent_id": _clean(row["target_agent_id"]),
            "model": _clean(row["target_model"]),
            "capability": _clean(row["capability"]),
            "conversation_label": _clean(row["conversation_label"]) or None,
            "review_id": row["review_id"],
            "created_at": row["created_at"],
            "excerpt": _clean(row["excerpt"]),
        }
        for row in rows
    ]
    return _result(shown, days, searched, hits)


def search_path(
    path: Path, query: str, days: int | None = None, limit: int = 10
) -> dict[str, Any]:
    """`search` on a database file of its own, for the server, which cannot exit.

    A file that cannot be read comes back as a `note`, where the terminal's `connect`
    would stop the process.
    """
    words = _words(query)  # before the file is opened: a bad query needs no database
    db, reason = open_readonly(path)
    if db is None:
        return _result(_clean(" ".join(words)), days, note=reason)
    with closing(db):
        return search(db, query, days, limit)


def render_search(result: dict[str, Any]) -> str:
    if result["note"]:
        return result["note"]
    if not result["hits"]:
        return f"No match for `{result['query']}` in {result['searched']:,} stored texts."
    blocks = []
    for n, hit in enumerate(result["hits"], 1):
        head = (
            f"{n}. {hit['created_at'][:10]}  {hit['agent_id']} {hit['model']}  "
            f"{hit['capability']}  in the {hit['part']}"
        )
        ids = f"consultation {hit['consultation_id']}, turn {hit['turn']}" + (
            f", review {hit['review_id']}" if hit["review_id"] else ""
        )
        blocks.append(
            "\n".join(
                [
                    _clean(head),
                    textwrap.fill(hit["excerpt"], 100, initial_indent="   ", subsequent_indent="   "),
                    "   " + _clean(ids),
                ]
            )
        )
    return (
        f"Best matches for `{result['query']}`, of {result['searched']:,} stored texts\n\n"
        + "\n\n".join(blocks)
    )


# --- the command line ---------------------------------------------------------


def _count(top: int):
    """An integer argument from 1 to `top`, so it cannot overflow a date or an SQLite integer."""

    def parse(text: str) -> int:
        value = int(text)
        if not 1 <= value <= top:
            raise argparse.ArgumentTypeError(f"must be from 1 to {top}")
        return value

    parse.__name__ = "count"
    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrator-mcp-server",
        allow_abbrev=False,
        description="Reports over the consultation database, read-only.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_: str, *, json_flag: bool = True) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_, description=help_, allow_abbrev=False)
        if json_flag:
            sub.add_argument("--json", action="store_true", help="print JSON instead of a table")
        return sub

    command("usage", "tokens and cost per agent and model").add_argument(
        "--days", type=_count(MAX_DAYS), default=30, help="how far back to look (default 30)"
    )
    log = command("history", "recent consultations, reviews and workflows, with their ids")
    log.add_argument("--limit", type=_count(MAX_LIMIT), default=20, help="how many to show (default 20)")
    log.add_argument("--kind", choices=KINDS, help="only this kind")
    command("scorecard", "how each reviewer answered and what became of its findings").add_argument(
        "--days", type=_count(MAX_DAYS), default=30, help="how far back to look (default 30)"
    )
    command("export", "one record and everything it owns, as JSON", json_flag=False).add_argument(
        "id", help=f"a full id or a prefix of at least {MIN_PREFIX} characters, from `history`"
    )
    find = command("search", "stored prompts and answers that hold some words")
    find.add_argument("query", nargs="+", metavar="word", help="every word must appear")
    find.add_argument(
        "--days", type=_count(MAX_DAYS), help="only this far back (default: all of it)"
    )
    find.add_argument(
        "--limit", type=_count(MAX_HITS), default=10, help=f"how many to show, up to {MAX_HITS} (default 10)"
    )
    return parser


def run(args: Sequence[str], load_config) -> int:
    options = _parser().parse_args(args)
    try:
        consult = load_consult_config(load_config())
    except ConfigError as error:
        fail(str(error))
    if consult is None:
        fail("the config has no `consult:` block, so there is no database to read")
    with closing(connect(consult.database_path)) as db:
        match options.command:
            case "usage":
                report = usage(db, options.days)
                print(json.dumps(report, indent=2) if options.json else render_usage(report))
            case "history":
                items = history(db, options.limit, options.kind)
                print(json.dumps(items, indent=2) if options.json else render_history(items))
            case "scorecard":
                report = scorecard(db, options.days)
                print(json.dumps(report, indent=2) if options.json else render_scorecard(report))
            case "export":
                print(json.dumps(export(db, options.id), indent=2))
            case "search":
                try:
                    found = search(db, " ".join(options.query), options.days, options.limit)
                except ValueError as error:
                    fail(str(error))
                print(json.dumps(found, indent=2) if options.json else render_search(found))
    return 0
