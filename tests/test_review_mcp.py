"""The MCP surface of the review path.

Two things are guarded here. That the review tools appear only when reviewers are
configured -- a server advertising `review` with nobody to review is a tool that
can only refuse. And that the guardrails a calling model reads live in the
docstrings, because the docstring *is* the schema description a host sends to a
model, and a promise made only in this repo's prose reaches nobody.
"""

from __future__ import annotations

import pytest

from orchestrator_mcp.server import build_server

from .conftest import consult_block

REVIEW_TOOLS = {
    "orchestrator_review",
    "orchestrator_review_run",
    "orchestrator_retry_review",
    "orchestrator_finalize_review",
    "orchestrator_apply_fixes",
    "orchestrator_record_fix_round",
    "orchestrator_cancel_review",
    "orchestrator_test_reviewers",
    "orchestrator_get_review",
    "orchestrator_list_reviews",
    "orchestrator_delete_review",
    "orchestrator_request_delete_all",
    "orchestrator_delete_all_reviews",
}


def config(tmp_path, **overrides):
    return {
        "consult": consult_block(
            database_path=str(tmp_path / "consultations.sqlite3"), **overrides
        )
    }


def reviewed(tmp_path, **overrides):
    return config(
        tmp_path,
        review={"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol", "claude-opus"]},
        **overrides,
    )


async def tools(server):
    return {t.name: t for t in await server.list_tools()}


# --- what is advertised -----------------------------------------------------


async def test_configuring_reviewers_adds_the_review_tools(tmp_path, host_claude):
    without = set(await tools(build_server(config(tmp_path))))
    with_review = set(await tools(build_server(reviewed(tmp_path))))

    assert with_review - without == REVIEW_TOOLS


async def test_a_consult_config_with_no_reviewers_advertises_none_of_them(tmp_path, host_claude):
    """A `review` tool with nobody configured behind it can only refuse, and a
    calling model has no way to tell that from a refusal it caused."""
    assert set(await tools(build_server(config(tmp_path)))) & REVIEW_TOOLS == set()


async def test_the_consult_tools_are_untouched_by_the_review_block(tmp_path, host_claude):
    plain = await tools(build_server(config(tmp_path)))
    reviewing = await tools(build_server(reviewed(tmp_path)))

    for name in ("orchestrator_consult", "orchestrator_list_consult_agents", "orchestrator_get_consultation"):
        assert reviewing[name].input_schema == plain[name].input_schema
        assert reviewing[name].description == plain[name].description


# --- the schemas a model reads ----------------------------------------------


async def test_planning_needs_only_a_goal(tmp_path, host_claude):
    schema = (await tools(build_server(reviewed(tmp_path))))["orchestrator_review"].input_schema
    assert schema["required"] == ["goal"]
    assert set(schema["properties"]["mode"]["enum"]) == {"standard", "deep"}


async def test_running_needs_the_id_and_the_token_together(tmp_path, host_claude):
    """The handshake is the whole point: an id without the token it was issued with
    would let a second call spend an approval it never saw."""
    schema = (await tools(build_server(reviewed(tmp_path))))["orchestrator_review_run"].input_schema
    assert set(schema["required"]) == {"review_id", "confirm_token"}
    assert set(schema["properties"]["secrets"]["enum"]) == {"mask", "send_as_is"}


async def test_retry_exposes_raw_for_a_send_as_is_review(tmp_path, host_claude):
    schema = (await tools(build_server(reviewed(tmp_path))))[
        "orchestrator_retry_review"
    ].input_schema
    assert "raw" in schema["properties"]


async def test_web_access_is_off_unless_it_is_asked_for(tmp_path, host_claude):
    schema = (await tools(build_server(reviewed(tmp_path))))["orchestrator_review"].input_schema
    assert schema["properties"]["web"]["default"] is False


async def test_the_synthesis_must_state_its_scope(tmp_path, host_claude):
    """`checked` and `not_checked` are required in the schema, not merely requested
    in the prose. An optional scope is a scope that gets left out."""
    schema = (await tools(build_server(reviewed(tmp_path))))["orchestrator_finalize_review"].input_schema
    assert {"checked", "not_checked"} <= set(schema["required"])
    assert {"review_id", "summary", "recommendation"} <= set(schema["required"])


async def test_deleting_everything_takes_a_token_it_cannot_invent(tmp_path, host_claude):
    """An omitted argument must never mean "erase all history"."""
    surface = await tools(build_server(reviewed(tmp_path)))
    assert surface["orchestrator_request_delete_all"].input_schema.get("required", []) == []
    assert surface["orchestrator_delete_all_reviews"].input_schema["required"] == ["confirm_token"]


# --- the guardrails a model actually sees -----------------------------------


@pytest.mark.parametrize(
    "tool, promise",
    [
        ("orchestrator_review", "Sends nothing"),
        ("orchestrator_review", "best-effort"),
        ("orchestrator_review", "reviewer's own CLI history"),
        ("orchestrator_review", "not a separate payload verifier"),
        ("orchestrator_review_run", "never shown to any reviewer"),
        ("orchestrator_review_run", "reviewers replying is not a finished review"),
        ("orchestrator_retry_review", "exact original goal and context"),
        ("orchestrator_finalize_review", "single\n        reviewer raised while the others disagree"),
        ("orchestrator_finalize_review", "refuses finalization"),
        ("orchestrator_apply_fixes", "Changes nothing"),
        ("orchestrator_apply_fixes", "no file is edited and no command is run here"),
        ("orchestrator_record_fix_round", "A log entry, not an action"),
        ("orchestrator_test_reviewers", "no project material leaves this machine"),
        ("orchestrator_cancel_review", "cannot be signalled from here"),
        ("orchestrator_delete_review", "Refused while a review is running"),
        ("orchestrator_request_delete_all", "Deletes nothing"),
    ],
)
async def test_the_docstring_carries_the_guardrail(tmp_path, host_claude, tool, promise):
    """These strings are the description a host sends to the model. A guardrail
    documented only in this repo reaches nobody who calls the tool."""
    description = (await tools(build_server(reviewed(tmp_path))))[tool].description
    assert promise.replace("\n        ", " ") in " ".join(description.split())
