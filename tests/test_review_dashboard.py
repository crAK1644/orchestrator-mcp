"""The review pages, driven over real HTTP like the rest of the dashboard.

Three properties carry the weight here: a database that predates the review tables
renders a sentence about restarting rather than a traceback, a planted credential
reaches no page, and saving reviewers writes the one file the dashboard owns without
dropping the agents that share it.
"""

from __future__ import annotations

import sqlite3

import pytest
import yaml

from orchestrator_mcp.consult.managed import read_managed_document, write_managed
from orchestrator_mcp.consult.store import MIGRATIONS

from .conftest import agent
from .test_consult_dashboard import _Client, config, serve  # noqa: F401 -- fixtures
from .test_review_service import FINDINGS, REVIEWERS, StubAdapter, StubService

# The shape `redact` knows, planted where a review would carry it: the goal, the
# context, and the answer a reviewer sent back.
SECRET = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"

SUMMARY = {
    "summary": "The parser reads an unbounded file.",
    "recommendation": "Stream it.",
    "checked": ["a.py lines 1-40"],
    "not_checked": ["everything that calls it"],
}


@pytest.fixture
def review_config(config, host_claude):  # noqa: F811 -- the imported fixture
    """The dashboard's config, with reviewers configured.

    Agents on `codex` and `antigravity` because the host runtime is `claude` here and
    `plan` refuses a reviewer running the host's own runtime.
    """

    def build(**overrides):
        return config(
            agents=dict(REVIEWERS),
            review={"reviewers": ["codex-sol"], "deep_reviewers": list(REVIEWERS)},
            **overrides,
        )

    return build


async def make_review(consult_config, *, goal="review the parser", context="def parse(): ...",
                      answer=FINDINGS, finalize=True):
    """One real review in the store the dashboard reads. Returns its id."""
    adapters = {agent_id: StubAdapter(answer) for agent_id in consult_config.agents}
    service = await StubService(consult_config, "claude", adapters=adapters).open()
    try:
        planned = await service.plan(
            mode="deep",
            goal=goal,
            material=[{"label": "a.py", "kind": "file", "locator": "lines 1-40", "chars": 40}],
            context=context,
        )
        assert planned.error is None, planned.error
        ran = await service.run(
            planned.review_id,
            planned.plan.confirm_token,
            host_findings=["the read is unbounded"],
        )
        assert ran.error is None, ran.error
        if finalize:
            criticals = [f for result in ran.results for f in result.findings]
            done = await service.finalize(
                planned.review_id,
                SUMMARY
                | {
                    "combined_findings": [
                        {
                            "problem": "the whole file is read into memory",
                            "severity": "critical",
                            "location": "a.py:1",
                            "agreed_by": sorted(consult_config.agents),
                            "source_finding_ids": [f.finding_id for f in criticals],
                            "proposed_action": "stream it",
                        }
                    ]
                },
            )
            assert done.error is None, done.error
        return planned.review_id
    finally:
        await service.close()


def old_database(path) -> None:
    """The store as an earlier version left it: every migration but the review one.

    The real ledger rather than a hand-built table, because what this stands in for is
    a running server that simply has not been restarted -- everything else on the page
    still has to work.
    """
    database = sqlite3.connect(path)
    with database:
        for statement in MIGRATIONS:
            if "CREATE TABLE reviews" in statement:
                break
            database.executescript(statement)
    database.close()


# --- an un-migrated database ------------------------------------------------


async def test_the_reviews_page_asks_for_a_restart_rather_than_raising(serve, review_config):  # noqa: F811
    """The read-only connection cannot create the table, so the page has to say so."""
    consult_config = review_config()
    old_database(consult_config.database_path)
    get, _ = serve(consult_config)

    status, body = get("/reviews")

    assert status == 200
    assert "restart the MCP server" in body
    assert "no such table" not in body


async def test_a_review_page_on_an_un_migrated_database_is_a_sentence_not_a_500(
    serve, review_config  # noqa: F811
):
    consult_config = review_config()
    old_database(consult_config.database_path)
    get, _ = serve(consult_config)

    status, body = get("/reviews/6f1c9d20-0000-0000-0000-000000000000")

    assert status == 404
    assert "restart the MCP server" in body


async def test_the_index_carries_the_notice_when_reviews_are_configured(serve, review_config):  # noqa: F811
    consult_config = review_config()
    old_database(consult_config.database_path)
    get, _ = serve(consult_config)

    _, body = get("/")

    assert "<h2>Reviews</h2>" in body
    assert "restart the MCP server" in body


async def test_a_database_that_does_not_exist_yet_is_not_a_restart(serve, review_config):  # noqa: F811
    """Nothing has been consulted here at all, and no restart would conjure a review
    into a file that does not exist."""
    get, _ = serve(review_config())

    status, body = get("/reviews")

    assert status == 200
    assert "No reviews recorded yet." in body
    assert "restart" not in body


async def test_an_install_that_does_not_review_gets_no_reviews_section(serve):  # noqa: F811
    """No `review:` block and no history: the index does not advertise a feature
    nobody configured."""
    get, _ = serve()

    _, body = get("/")

    assert "<h2>Reviews</h2>" not in body


# --- history and detail -----------------------------------------------------


async def test_a_review_appears_in_the_list_with_its_mode_and_outcome(serve, review_config):  # noqa: F811
    get, consult_config = serve(review_config())
    review_id = await make_review(consult_config)

    status, body = get("/reviews")

    assert status == 200
    assert str(review_id)[:8] in body
    assert "deep" in body and "complete" in body and "all" in body


async def test_the_detail_page_shows_every_reviewer_and_its_findings(serve, review_config):  # noqa: F811
    get, consult_config = serve(review_config())
    review_id = await make_review(consult_config)

    status, body = get(f"/reviews/{review_id}")

    assert status == 200
    for agent_id in REVIEWERS:
        assert agent_id in body
    # The finding each stub reviewer returned, at the severity it claimed.
    assert "unbounded read" in body and "critical" in body
    # The host's own opinion, formed first and shown to no reviewer.
    assert "the read is unbounded" in body
    # The synthesis, in the four columns the review format promises.
    assert "The parser reads an unbounded file." in body
    assert "proposed action" in body and "stream it" in body
    # And the original answers, folded away.
    assert "<details>" in body


async def test_the_detail_page_says_a_synthesis_is_still_missing(serve, review_config):  # noqa: F811
    """Reviewers replying is not a finished review, and the page says which it is."""
    get, consult_config = serve(review_config())
    review_id = await make_review(consult_config, finalize=False)

    _, body = get(f"/reviews/{review_id}")

    assert "awaiting_synthesis" in body
    assert "finalize_review" in body


async def test_a_reviewer_that_answered_prose_still_renders(serve, review_config):  # noqa: F811
    """`findings_parsed=False` is a usable review, not a broken page."""
    get, consult_config = serve(review_config())
    review_id = await make_review(
        consult_config, answer="No fenced block from me, just an opinion.", finalize=False
    )

    _, body = get(f"/reviews/{review_id}")

    assert "No findings recorded." in body
    assert "just an opinion" in body


# --- the credential ---------------------------------------------------------


async def test_a_planted_credential_reaches_no_review_page(serve, review_config):  # noqa: F811
    """What the store holds is the redacted copy, and the page can only show that."""
    get, consult_config = serve(review_config())
    review_id = await make_review(
        consult_config,
        goal=f"review this, the key is {SECRET}",
        context=f"TOKEN = '{SECRET}'",
        answer=f"the key {SECRET} is hardcoded\n\n{FINDINGS}",
    )

    pages = [get("/")[1], get("/reviews")[1], get(f"/reviews/{review_id}")[1]]

    assert all(SECRET not in page for page in pages)
    # And the detail page still says a secret-shaped string was there, by position.
    assert "secret-shaped" in pages[2]
    assert "[redacted]" in pages[2]


# --- configuring reviewers --------------------------------------------------


@pytest.fixture
def editable(review_config):
    def build(**overrides):
        consult_config = review_config(**overrides)
        consult_config.dashboard.editable = True
        return consult_config

    return build


async def test_the_reviewers_page_is_not_served_when_editing_is_off(serve, review_config):  # noqa: F811
    get, _ = serve(review_config())

    status, body = get("/reviewers")

    assert status == 403
    assert "editable" in body


async def test_the_form_comes_up_on_who_reviews_now(serve, editable):  # noqa: F811
    get, _ = serve(editable())

    status, body = get("/reviewers")

    assert status == 200
    # The standard reviewer selected, and every agent offered for deep review.
    assert "<option value='codex-sol' selected>" in body
    assert "name='deep.codex-sol' checked" in body
    assert "name='deep.gemini-x' checked" in body


async def test_saving_reviewers_keeps_the_agents_in_the_same_file(serve, editable):  # noqa: F811
    """One file, two blocks, written together -- a reviewer save that dropped the
    agents would be a save that succeeded and lost."""
    get, consult_config = serve(editable())
    # An id `config.yaml` does not also hold: naming one in both files is the startup
    # error the agents page already refuses, and not what this test is about.
    write_managed(consult_config.managed_agents_path, {"extra-one": agent("codex")})

    status, _, location = get.post(
        "/reviewers",
        {"_token": get.token, "reviewer": "codex-sol", "deep.codex-sol": "on",
         "deep.gemini-x": "on"},
    )

    assert (status, location) == (303, "/reviewers?saved=1")
    document = read_managed_document(consult_config.managed_agents_path)
    assert "extra-one" in document["agents"]
    assert document["review"] == {
        "reviewers": ["codex-sol"],
        "deep_reviewers": ["codex-sol", "gemini-x"],
    }


async def test_saving_an_agent_keeps_the_reviewers(serve, editable):  # noqa: F811
    get, consult_config = serve(editable())
    write_managed(
        consult_config.managed_agents_path,
        {},
        {"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol"]},
    )

    status, _, location = get.post(
        "/agents",
        # `python3` because the save resolves the command on PATH before writing it.
        {"_token": get.token, "id": "new-one", "runtime": "codex", "command": "python3",
         "model": "gpt-5.6-sol", "priority": "10", "enabled": "on", "score.review": "90"},
    )

    assert (status, location) == (303, "/agents?saved=new-one")
    document = read_managed_document(consult_config.managed_agents_path)
    assert "new-one" in document["agents"]
    assert document["review"]["reviewers"] == ["codex-sol"]


async def test_more_than_one_standard_reviewer_is_refused_by_the_same_rule_boot_uses(
    serve, editable  # noqa: F811
):
    """A second opinion nobody compared is a slower first one; `deep_reviewers` is
    the list for asking several."""
    get, _ = serve(editable())

    status, body, _ = get.post("/reviewers", {"_token": get.token, "reviewer": "",
                                              "deep.codex-sol": "on"})

    assert status == 200
    assert "exactly one agent" in body


@pytest.fixture
def unconfigured(config):  # noqa: F811
    """Editable, with no `review:` block yet -- the state the form exists to leave.

    Not built through `review_config`, because these agents are exactly the ones a
    boot would refuse as reviewers, and the block would refuse with them in it.
    """

    def build(**overrides):
        consult_config = config(**overrides)
        consult_config.dashboard.editable = True
        return consult_config

    return build


async def test_an_agent_that_is_not_offered_review_work_cannot_be_named(serve, unconfigured):  # noqa: F811
    agents = {
        "codex-sol": agent("codex", "gpt-5.6-sol", 10, scores={"coding": 90}),
        "gemini-x": agent("antigravity", "gemini-3.6", 20),
    }
    get, _ = serve(unconfigured(agents=agents))

    status, body, _ = get.post(
        "/reviewers", {"_token": get.token, "reviewer": "codex-sol", "deep.gemini-x": "on"}
    )

    assert status == 200
    assert "not offered `review` work" in body


async def test_a_disabled_agent_cannot_be_named(serve, unconfigured):  # noqa: F811
    agents = {
        "codex-sol": agent("codex", "gpt-5.6-sol", 10),
        "gemini-x": agent("antigravity", "gemini-3.6", 20, enabled=False),
    }
    get, _ = serve(unconfigured(agents=agents))

    status, body, _ = get.post(
        "/reviewers",
        {"_token": get.token, "reviewer": "codex-sol", "deep.gemini-x": "on"},
    )

    assert status == 200
    assert "is disabled" in body


async def test_reviewers_written_in_the_operators_own_file_are_shown_not_edited(
    tmp_path, serve, editable  # noqa: F811
):
    """`review:` in both files is a startup error, not a merge -- so this page has to
    refuse rather than write a file the next boot rejects."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"consult": {"review": {"reviewers": ["codex-sol"],
                                    "deep_reviewers": ["codex-sol"]}}}
        )
    )
    get, _ = serve(editable(), config_path)

    status, body = get("/reviewers")
    posted, refusal, _ = get.post("/reviewers", {"_token": get.token, "reviewer": "gemini-x"})

    assert status == 200 and str(config_path) in body
    assert posted == 409 and "Delete it there first" in refusal
