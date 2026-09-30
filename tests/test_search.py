"""`search`: words in stored prompts and answers, from a terminal and from a tool.

Every database here is written by the real services. What these pin down is that the
words are only ever words (no query string is FTS5 syntax), that what can be found is
what is shown (a credential in an old row is neither printed nor findable), that a
database which cannot be searched says so instead of failing, and that nothing here
writes -- not the file, and not an index left behind in it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from orchestrator_mcp.consult.contract import ConsultationContent
from orchestrator_mcp.reports import (
    _NO_FTS5,
    _NO_TEXT,
    MAX_HITS,
    NOT_MIGRATED,
    _parser,
    connect,
    render_search,
    search,
    search_path,
)
from orchestrator_mcp.server import build_server

from .conftest import consult_block
from .test_consult_dashboard import config  # noqa: F401 -- fixture
from .test_consult_service import StubAdapter, StubService
from .test_reports import cli, digest, read, report, write  # noqa: F401 -- fixture
from .test_review_dashboard import (
    SECRET,
    make_review,
    old_database,
    review_config,  # noqa: F401 -- fixture
)
from .test_review_service import REVIEWERS

TOOL = "orchestrator_search_consultations"


async def ask(consult_config, prompt, answer="blue", **kwargs):
    """One real consultation, whose stored answer is `answer`."""
    content = ConsultationContent(
        answer=answer, assumptions=[], uncertainties=[], follow_up_questions=[], sources=[]
    )
    service = await StubService(
        consult_config, "claude", adapter=StubAdapter(content=content)
    ).open()
    try:
        return await service.consult(capability="coding", prompt=prompt, **kwargs)
    finally:
        await service.store.close()


def found(consult_config, query, **kwargs):
    return report(consult_config, search, query, **kwargs)["hits"]


# --- what comes back ---------------------------------------------------------


async def test_the_best_match_comes_first(config):
    consult_config = config()
    # Same length, so the one that says it more often is the better match.
    await ask(consult_config, "retention alpha beta gamma")
    await ask(consult_config, "retention retention retention retention")

    hits = found(consult_config, "retention")

    assert [h["excerpt"].count("«") for h in hits] == [4, 1]


async def test_a_hit_says_which_conversation_turn_and_side_it_came_from(config):
    consult_config = config()
    response = await ask(
        consult_config, "where does the zebra sleep", "The zebra sleeps in the barn.",
        conversation_label="zoo notes",
    )

    [prompt] = found(consult_config, "where")
    [answer] = found(consult_config, "barn")

    assert (prompt["part"], answer["part"]) == ("prompt", "answer")
    for hit in (prompt, answer):
        assert hit["consultation_id"] == str(response.consultation_id)
        assert (hit["turn"], hit["review_id"]) == (1, None)
        assert (hit["agent_id"], hit["model"], hit["capability"]) == (
            "codex-sol", "gpt-5.6-sol", "coding",
        )
        assert hit["conversation_label"] == "zoo notes"
        assert hit["created_at"].startswith("20")
    assert "«barn»" in answer["excerpt"]


async def test_a_second_turn_is_its_own_hit(config):
    consult_config = config()
    first = await ask(consult_config, "first question")
    await ask(
        consult_config, "second zebra question", consultation_id=first.consultation_id
    )

    [hit] = found(consult_config, "zebra")

    assert (hit["consultation_id"], hit["turn"]) == (str(first.consultation_id), 2)


async def test_every_word_must_be_in_the_same_prompt_or_the_same_answer(config):
    consult_config = config()
    await ask(consult_config, "alpha beta", "delta")
    await ask(consult_config, "alpha gamma", "delta")

    assert len(found(consult_config, "alpha")) == 2
    assert len(found(consult_config, "alpha beta")) == 1
    assert found(consult_config, "alpha delta") == []  # one in a prompt, one in an answer
    assert found(consult_config, "alpha nothing") == []


async def test_a_reviewers_prompt_and_answer_are_found_under_their_review(review_config):
    consult_config = review_config()
    review_id = str(await make_review(consult_config, goal="audit the zebra allocator"))

    prompts = found(consult_config, "zebra")
    answers = found(consult_config, "unbounded")

    assert {h["agent_id"] for h in prompts} == set(REVIEWERS)
    assert {h["part"] for h in prompts} == {"prompt"}
    assert {h["part"] for h in answers} == {"answer"}
    assert {h["review_id"] for h in prompts + answers} == {review_id}


async def test_the_limit_is_kept_and_cannot_be_asked_away(config):
    consult_config = config()
    for n in range(MAX_HITS + 5):
        await ask(consult_config, f"needle number {n}")

    assert len(found(consult_config, "needle", limit=5)) == 5
    assert len(found(consult_config, "needle", limit=10_000)) == MAX_HITS
    # A negative LIMIT is no limit in SQLite.
    assert len(found(consult_config, "needle", limit=-1)) == 1
    assert len(found(consult_config, "needle", limit=0)) == 1


async def test_days_narrows_the_window_and_an_empty_one_says_so(config):
    consult_config = config()
    await ask(consult_config, "an old zebra")
    write(consult_config, "UPDATE consultation_turns SET created_at = '2020-01-01T00:00:00+00:00'")

    assert found(consult_config, "zebra")  # all of it, by default
    assert found(consult_config, "zebra", days=10_000)
    recent = report(consult_config, search, "zebra", days=30)
    assert recent["hits"] == [] and recent["note"] == "No turns in the last 30 days."


async def test_a_database_with_no_turns_yet_says_so(config):
    consult_config = config()
    service = await StubService(consult_config, "claude", adapter=StubAdapter()).open()
    await service.store.close()

    result = report(consult_config, search, "zebra")

    assert result["hits"] == [] and result["note"] == "Nothing recorded yet."


async def test_a_database_that_kept_no_text_says_so(config):
    consult_config = config(store_full_content=False)
    await ask(consult_config, "zebra", "zebra")

    result = report(consult_config, search, "zebra")

    assert result["hits"] == [] and result["note"] == _NO_TEXT
    assert render_search(result) == _NO_TEXT


async def test_no_match_says_how_much_was_searched(config):
    consult_config = config()
    await ask(consult_config, "one", "two")

    result = report(consult_config, search, "zebra")

    assert result["hits"] == [] and result["searched"] == 2 and result["note"] is None
    assert "No match for `zebra` in 2 stored texts" in render_search(result)


async def test_a_row_the_json_guard_must_skip_does_not_break_the_search(config):
    """`json_type` on text that is not JSON is an error, and `AND` does not promise to
    evaluate its left side first, so the guard is a CASE. One bad row must not cost the
    search of every other."""
    consult_config = config()
    await ask(consult_config, "zebra one", "kept")
    await ask(consult_config, "zebra two", "kept")
    await ask(consult_config, "zebra three", "kept")
    write(consult_config, "UPDATE consultation_turns SET validated_response_json = 'not json {' "
                          "WHERE user_prompt = 'zebra one'")
    write(consult_config, "UPDATE consultation_turns SET validated_response_json = '{\"answer\": 7}' "
                          "WHERE user_prompt = 'zebra two'")

    hits = found(consult_config, "zebra")

    assert sorted(h["part"] for h in hits) == ["prompt", "prompt", "prompt"]
    assert [h["part"] for h in found(consult_config, "kept")] == ["answer"]


# --- words are only words ----------------------------------------------------

HOSTILE = [
    '"', "'", '""', '"unclosed', "AND", "OR", "NOT", "NEAR(", "NEAR(a b, 2)", "*", "a*",
    "^a", "a:b", "-", "((", "{a b}", "' OR 1=1 --", "; DROP TABLE consultations;",
    "a\x00b", "\x1b[31m", "é", "‮", "x " * 100, "a" * 200,
]  # fmt: skip


async def test_no_query_string_is_fts5_syntax(config):
    consult_config = config()
    await ask(consult_config, "the cat AND the dog", "OR nothing NEAR here")

    for query in HOSTILE:
        result = report(consult_config, search, query)
        assert isinstance(result["hits"], list), repr(query)
    assert read(consult_config, "SELECT COUNT(*) FROM consultations") == [(1,)]


async def test_an_operator_word_is_a_word_to_find(config):
    consult_config = config()
    await ask(consult_config, "the cat AND the dog", "OR nothing NEAR here, in the category")

    assert len(found(consult_config, "AND")) == 1
    assert len(found(consult_config, "NEAR")) == 1
    assert len(found(consult_config, "cat AND dog")) == 1
    # Were `OR` an operator this would match the prompt, which has `cat` but no `zebra`.
    assert found(consult_config, "cat OR zebra") == []
    # Nor is `*` a prefix (that would find `category`) or `:` a column filter (an error).
    assert found(consult_config, "cate*") == [] and found(consult_config, "col:x") == []


async def test_a_word_with_nothing_to_index_costs_the_others_nothing(config):
    consult_config = config()
    await ask(consult_config, "the retention race")

    assert len(found(consult_config, "retention - race")) == 1
    assert found(consult_config, "-") == []


@pytest.mark.parametrize("query", ["", "   ", "\x00\x01\x1f", "\n\t"])
def test_a_query_with_no_words_is_refused(config, query):
    with pytest.raises(ValueError, match="at least one word"):
        search_path(config().database_path, query)


def test_a_query_past_the_cap_is_refused_and_not_cut_mid_word(config):
    with pytest.raises(ValueError, match="at most 200 characters"):
        search_path(config().database_path, "x" * 201)


# --- what can be found is what is shown --------------------------------------


async def test_a_credential_in_an_old_row_is_neither_shown_nor_findable(config):
    consult_config = config()
    await ask(consult_config, "placeholder", "plain")
    write(
        consult_config,
        "UPDATE consultation_turns SET user_prompt = ?, validated_response_json = ?",
        f"the staging key is {SECRET}",
        json.dumps({"answer": f"log in to staging with {SECRET}"}),
    )

    result = report(consult_config, search, "staging")
    shown = json.dumps(result) + render_search(result)

    assert len(result["hits"]) == 2
    assert SECRET not in shown and "[redacted]" in shown
    # Masked before it is indexed, so guessing at the value finds nothing.
    assert found(consult_config, "AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH") == []


async def test_a_terminal_escape_in_a_stored_row_never_reaches_the_output(config):
    consult_config = config()
    await ask(consult_config, "placeholder")
    write(
        consult_config,
        "UPDATE consultation_turns SET user_prompt = ?",
        "\x1b[31mred\x1b[0m staging \x00 \x07 bell",
    )

    result = report(consult_config, search, "staging")
    shown = json.dumps(result, ensure_ascii=False) + render_search(result)

    assert result["hits"] and "«staging»" in shown
    assert not any(ch in shown for ch in "\x1b\x00\x07")


async def test_a_credential_typed_as_the_query_is_not_echoed(config):
    consult_config = config()
    await ask(consult_config, "placeholder")

    result = report(consult_config, search, f"password={SECRET}")

    assert SECRET not in json.dumps(result) + render_search(result)


# --- what cannot be searched -------------------------------------------------


def test_a_database_that_cannot_be_read_is_a_note_because_the_server_cannot_exit(
    config, tmp_path
):
    consult_config = config()
    missing = search_path(tmp_path / "missing.sqlite3", "zebra")
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"this is not sqlite" * 100)
    old_database(consult_config.database_path)

    notes = [
        search_path(path, "zebra")["note"]
        for path in (tmp_path / "missing.sqlite3", junk, consult_config.database_path)
    ]

    assert missing["hits"] == [] and "no database" in notes[0]
    assert "not a readable database" in notes[1]
    assert NOT_MIGRATED in notes[2]


def test_a_path_that_exists_but_is_no_database_is_a_note_and_a_sentence(tmp_path):
    # A directory passes the missing-file check, and SQLite only refuses it at the first read.
    result = search_path(tmp_path, "zebra")
    with pytest.raises(SystemExit) as done:
        connect(tmp_path)

    assert result["hits"] == [] and "cannot open the database" in result["note"]
    assert "cannot open the database" in str(done.value)


async def test_a_python_without_fts5_says_so_and_stops(config):
    consult_config = config()
    await ask(consult_config, "zebra")

    class WithoutFts5:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql.startswith("CREATE VIRTUAL TABLE"):
                raise sqlite3.OperationalError("no such module: fts5")
            return self.db.execute(sql, *args)

    with closing(sqlite3.connect(consult_config.database_path)) as db:
        result = search(WithoutFts5(db), "zebra")

    assert result["hits"] == [] and result["note"] == _NO_FTS5


async def test_the_index_is_never_written_to_the_database(config):
    consult_config = config()
    await ask(consult_config, "zebra", "zebra")
    before = digest(consult_config)

    found(consult_config, "zebra")
    search_path(consult_config.database_path, "zebra")

    assert digest(consult_config) == before
    assert read(consult_config, "SELECT name FROM sqlite_master WHERE name LIKE 'hay%'") == []


async def test_a_second_search_on_one_connection_starts_from_nothing(config):
    consult_config = config()
    await ask(consult_config, "zebra one")

    with closing(sqlite3.connect(f"{consult_config.database_path.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        first = search(db, "zebra")
        second = search(db, "zebra")

    assert first == second and len(first["hits"]) == 1


# --- the command line --------------------------------------------------------


async def test_the_command_takes_several_words_and_prints_hits_or_json(config, cli):
    consult_config = config()
    await ask(consult_config, "where does the zebra sleep")
    before = digest(consult_config)

    code, out, err = cli("search", "zebra", "sleep", consult_config=consult_config)
    assert (code, err) == (0, "")
    assert "«zebra»" in out and "«sleep»" in out and "consultation " in out and "turn 1" in out
    code, out, _ = cli("search", "zebra", "--json", "--limit", "3", consult_config=consult_config)
    assert code == 0 and json.loads(out)["hits"][0]["part"] == "prompt"
    code, out, _ = cli("search", "zebra", "--days", "30", consult_config=consult_config)
    assert code == 0 and "«zebra»" in out
    code, out, _ = cli("search", "nothing", consult_config=consult_config)
    assert code == 0 and out.startswith("No match for `nothing`")

    assert digest(consult_config) == before


async def test_a_query_the_command_cannot_search_is_a_sentence(config, cli):
    consult_config = config()
    await ask(consult_config, "zebra")

    for args, sentence in (
        (["search", " "], "at least one word"),
        (["search", "x" * 201], "at most 200 characters"),
    ):
        code, out, _ = cli(*args, consult_config=consult_config)
        assert sentence in str(code) and out == "", args
    assert "no database" in str(cli("search", "zebra")[0])


def test_a_count_out_of_range_is_a_usage_error(cli):
    assert cli("search")[0] == 2
    assert cli("search", "x", "--limit", str(MAX_HITS + 1))[0] == 2
    assert cli("search", "x", "--limit", "0")[0] == 2
    assert cli("search", "x", "--days", "0")[0] == 2
    assert cli("search", "x", "--days", "3651")[0] == 2
    assert cli("search", "--help")[0] == 0
    options = _parser().parse_args(["search", "a", "b", "--days", "3650", "--limit", str(MAX_HITS)])
    assert (options.query, options.days, options.limit) == (["a", "b"], 3650, MAX_HITS)
    assert _parser().parse_args(["search", "a"]).days is None


# --- the tool ----------------------------------------------------------------


async def test_the_tool_the_command_and_the_function_return_the_same_hits(config, cli):
    consult_config = config()
    await ask(consult_config, "where does the zebra sleep", "In the barn.", conversation_label="zoo")
    server = build_server(
        {"consult": consult_block(database_path=str(consult_config.database_path))}
    )

    direct = report(consult_config, search, "zebra")
    by_tool = (await server.call_tool(TOOL, {"query": "zebra"})).structured_content
    _, out, _ = cli("search", "zebra", "--json", consult_config=consult_config)

    assert direct["hits"] and by_tool == direct == json.loads(out)


async def test_the_tool_finds_a_reviewers_answer_and_names_its_review(review_config):
    consult_config = review_config()
    review_id = str(await make_review(consult_config))
    server = build_server(
        {"consult": consult_block(database_path=str(consult_config.database_path))}
    )

    result = (await server.call_tool(TOOL, {"query": "unbounded", "limit": 1})).structured_content

    assert [h["review_id"] for h in result["hits"]] == [review_id]


async def test_the_tool_on_a_database_that_is_not_there_yet_answers_with_a_note(
    tmp_path, host_claude
):
    server = build_server(
        {"consult": consult_block(database_path=str(tmp_path / "consultations.sqlite3"))}
    )

    result = (await server.call_tool(TOOL, {"query": "zebra"})).structured_content

    assert result["hits"] == [] and "no database" in result["note"]
    assert not (tmp_path / "consultations.sqlite3").exists()  # asking creates nothing


async def test_the_tool_refuses_what_the_schema_forbids_and_what_has_no_words(
    tmp_path, host_claude
):
    server = build_server(
        {"consult": consult_block(database_path=str(tmp_path / "consultations.sqlite3"))}
    )

    for arguments in (
        {"query": ""},
        {"query": "x" * 201},
        {"query": "x", "limit": MAX_HITS + 1},
        {"query": "x", "limit": 0},
        {"query": "x", "days": 0},
        {"query": "x", "days": 3651},
    ):
        with pytest.raises(ToolError):
            await server.call_tool(TOOL, arguments)
    # Passes the schema, and the reason it fails reaches the model as written.
    with pytest.raises(ToolError, match="at least one word"):
        await server.call_tool(TOOL, {"query": "   "})


async def test_the_tool_is_read_only_and_says_its_limit(tmp_path, host_claude):
    server = build_server(
        {"consult": consult_block(database_path=str(tmp_path / "consultations.sqlite3"))}
    )

    tool = next(t for t in await server.list_tools() if t.name == TOOL)

    assert tool.annotations.read_only_hint is True
    assert tool.input_schema["properties"]["limit"]["maximum"] == MAX_HITS
    assert tool.input_schema["required"] == ["query"]
