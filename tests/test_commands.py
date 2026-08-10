"""What the slash commands advertise, and what they expand to.

A command is text the host acts on, so the failure mode is not an exception -- it is
a `/review` that quietly stops telling the host to show the plan first. These assert
the instructions that are load-bearing, and deliberately not the prose around them.
"""

from __future__ import annotations

import pytest

from orchestrator_mcp.server import build_server

from .conftest import consult_block

REVIEW = {"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol", "claude-opus"]}
WORKFLOW = {"bindings": {"research": {"agent": "codex-sol"}}}


def server(**blocks):
    return build_server({"consult": consult_block(**blocks)})


async def expand(srv, name: str, arguments: dict | None = None) -> str:
    result = await srv.get_prompt(name, arguments or {})
    return "\n".join(
        m.content.text for m in result.messages if getattr(m.content, "text", None) is not None
    )


async def test_a_consult_only_server_offers_no_review_command(host_claude):
    """Gated like the tools: a `/review` whose only possible answer is "no reviewers"
    costs a round trip and reads like a bug rather than a configuration choice."""
    assert {p.name for p in await server().list_prompts()} == {"consult", "status"}


async def test_reviewers_without_a_workflow_add_only_the_review_command(host_claude):
    names = {p.name for p in await server(review=REVIEW).list_prompts()}
    assert names == {"consult", "review", "status"}


async def test_every_command_is_advertised_when_everything_is_configured(host_claude):
    srv = server(review=REVIEW, workflow=WORKFLOW)
    assert {p.name for p in await srv.list_prompts()} == {
        "consult",
        "review",
        "workflow",
        "status",
    }


async def test_every_argument_is_optional(host_claude):
    """A client that invokes a bare `/review` must get a usable expansion. Requiring
    an argument would turn the common case -- typing the command and nothing else --
    into an error the user has to decode."""
    srv = server(review=REVIEW, workflow=WORKFLOW)
    for prompt in await srv.list_prompts():
        for argument in prompt.arguments or []:
            assert not argument.required, f"{prompt.name}.{argument.name}"
        assert await expand(srv, prompt.name)


async def test_a_command_with_no_argument_says_to_ask_rather_than_invent(host_claude):
    """The empty case is where a host is most likely to make something up and spend a
    token on it."""
    srv = server(review=REVIEW, workflow=WORKFLOW)
    assert "ask the user what the goal is" in await expand(srv, "workflow")
    assert "has not said yet" in await expand(srv, "consult")


@pytest.mark.parametrize(
    ("name", "must_mention"),
    [
        pytest.param("review", "orchestrator_review_run", id="review"),
        pytest.param("workflow", "orchestrator_workflow_run_step", id="workflow"),
    ],
)
async def test_a_command_that_spends_a_token_names_the_checkpoint_first(
    host_claude, name, must_mention
):
    """The reason these exist. Both flows plan first and send second, and a host that
    runs them from the tool descriptions alone tends to collapse the two -- as the
    first draft of the workflow command did, putting plan and run in one bullet."""
    text = await expand(server(review=REVIEW, workflow=WORKFLOW), name)
    assert must_mention in text
    assert text.index("Show the user the plan") < text.index(must_mention)


async def test_the_review_command_keeps_the_secret_warning_honest(host_claude):
    """`secret_hits` is pattern matching. A command that told the host to trust the
    count would be worse than one that never mentioned it."""
    text = await expand(server(review=REVIEW), "review")
    assert "secret_hits" in text
    assert "read the flagged lines rather than trusting the count" in text


async def test_the_review_command_carries_the_deep_mode_precondition(host_claude):
    """`mode="deep"` is refused without `host_findings`, so a command that selects it
    without saying so sets the host up for a failed call."""
    srv = server(review=REVIEW)
    deep = await expand(srv, "review", {"deep": "yes"})
    assert 'mode="deep"' in deep and "host_findings" in deep
    assert "host_findings" not in await expand(srv, "review")


async def test_the_workflow_command_does_not_let_a_claimed_test_pass_for_a_result(host_claude):
    """The guarantee the whole test step exists for, restated where the host reads it."""
    text = await expand(server(review=REVIEW, workflow=WORKFLOW), "workflow")
    assert "record the exit code you observed" in text


async def test_the_status_command_admits_workflows_cannot_be_listed(host_claude):
    """There is no `orchestrator_list_workflows`. Saying so beats a host inventing an
    id or reporting that there are none."""
    text = await expand(server(review=REVIEW, workflow=WORKFLOW), "status")
    assert "no tool that lists them" in text
    assert "wf-7" in await expand(
        server(review=REVIEW, workflow=WORKFLOW), "status", {"workflow_id": "wf-7"}
    )


async def test_the_status_command_names_only_the_configured_surfaces(host_claude):
    text = await expand(server(), "status")
    assert "orchestrator_list_reviews" not in text
    assert "orchestrator_workflow_status" not in text
