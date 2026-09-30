"""`both_sides` on `orchestrator_consult_many`: two agents, one argues for, one against.

What these pin down: which agent is handed which side and which text; that the sides are
the only thing that differs between the two prompts; that a caller swaps them by
reversing `target_agents`; that a refusal happens before anything is sent; and that a
side failing leaves the other's answer, still labelled.
"""

from __future__ import annotations

import pytest

from orchestrator_mcp.consult.adapters.base import AdapterError
from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.styles import STYLES
from orchestrator_mcp.server import build_server

from .conftest import agent, consult_block
from .test_consult_mcp import config as mcp_config
from .test_consult_service import StubAdapter, StubService

TRIO = {
    "codex-sol": agent("codex", "gpt-5.6-sol", 10),
    "codex-mini": agent("codex", "gpt-5.6-mini", 20),
    "codex-nano": agent("codex", "gpt-5.6-nano", 30),
}
TASK = "Should we replace the queue with a cron job?"
PAIR = ["codex-sol", "codex-mini"]


class OneSideFails(StubAdapter):
    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing

    async def start(self, agent, prompt, source_mode, session_id=None):
        if agent.agent_id == self.failing:
            raise AdapterError(ConsultErrorCode.TIMEOUT, "the CLI never answered")
        return await super().start(agent, prompt, source_mode, session_id)


@pytest.fixture
def build(tmp_path, host_claude):
    async def make(adapter: StubAdapter | None = None, agents=TRIO, **overrides):
        config = ConsultConfig(
            **consult_block(
                database_path=str(tmp_path / "consultations.sqlite3"),
                agents=agents,
                **overrides,
            )
        )
        return await StubService(config, host_claude, adapter=adapter or StubAdapter()).open()

    return make


async def compiled(service, result) -> str:
    (turn,) = await service.store.turns(result.consultation_id)
    return turn.compiled_prompt


def side(name: str) -> str:
    return f"Persona: case-{name}\n{STYLES[f'case-{name}'][1]}"


async def test_the_first_agent_argues_for_and_the_second_against(build):
    service = await build()

    response = await service.consult_many(
        capability="coding", prompt=TASK, context="the queue costs $400 a month",
        both_sides=True, target_agents=PAIR,
    )

    assert response.sides == {"codex-sol": "for", "codex-mini": "against"}
    assert [r.route.agent_id for r in response.results] == PAIR
    for_prompt, against_prompt = [await compiled(service, r) for r in response.results]
    assert side("for") in for_prompt and side("against") not in for_prompt
    assert side("against") in against_prompt and side("for") not in against_prompt
    # The task and the evidence are the same bytes for both; only the side differs.
    assert TASK in for_prompt and "the queue costs $400 a month" in for_prompt
    assert for_prompt.replace(side("for"), "<side>") == against_prompt.replace(side("against"), "<side>")
    for result in response.results:
        record = await service.get_consultation(result.consultation_id)
        assert record.conversation_label == f"group {response.group_id}"


async def test_reversing_the_agents_swaps_the_sides(build):
    service = await build()

    response = await service.consult_many(
        capability="coding", prompt=TASK, both_sides=True, target_agents=PAIR[::-1]
    )

    assert response.sides == {"codex-mini": "for", "codex-sol": "against"}
    assert side("for") in await compiled(service, response.results[0])


async def test_with_no_agents_named_the_router_picks_two_and_the_top_one_argues_for(build):
    adapter = StubAdapter()
    service = await build(adapter)

    # `count` is for an ordinary panel; a pair is a pair.
    response = await service.consult_many(capability="coding", prompt=TASK, both_sides=True, count=3)

    assert response.sides == {"codex-sol": "for", "codex-mini": "against"}
    assert len(response.results) == 2 and len(adapter.prompts) == 2


async def test_a_config_entry_named_like_a_side_rewords_it(build):
    service = await build(personas={"case-for": "Argue for it, in verse."})

    response = await service.consult_many(
        capability="coding", prompt=TASK, both_sides=True, target_agents=PAIR
    )

    assert "Persona: case-for\nArgue for it, in verse." in await compiled(service, response.results[0])
    assert side("against") in await compiled(service, response.results[1])


async def test_an_ordinary_panel_has_no_sides(build):
    service = await build()

    response = await service.consult_many(capability="coding", prompt=TASK, target_agents=PAIR)

    assert response.sides is None


@pytest.mark.parametrize(
    "targets", [["codex-sol"], ["codex-sol", "codex-sol"], ["codex-sol", "codex-mini", "codex-nano"]]
)
async def test_other_than_two_different_agents_is_refused_before_anything_is_sent(build, targets):
    adapter = StubAdapter()
    service = await build(adapter)

    response = await service.consult_many(
        capability="coding", prompt=TASK, both_sides=True, target_agents=targets
    )

    [refusal] = response.results
    assert refusal.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "exactly two" in refusal.error.message
    assert response.sides is None and adapter.prompts == []


async def test_one_eligible_agent_is_not_a_pair(build):
    adapter = StubAdapter()
    service = await build(adapter, agents={"codex-sol": TRIO["codex-sol"]})

    response = await service.consult_many(capability="coding", prompt=TASK, both_sides=True)

    [refusal] = response.results
    assert refusal.error.code is ConsultErrorCode.INVALID_REQUEST
    assert response.sides is None and adapter.prompts == []


async def test_nobody_eligible_is_the_usual_refusal(build):
    service = await build(agents={"claude-opus": agent("claude", "opus", 30)})

    response = await service.consult_many(capability="coding", prompt=TASK, both_sides=True)

    [refusal] = response.results
    assert refusal.error.code is ConsultErrorCode.NO_AGENT_AVAILABLE


async def test_a_persona_alongside_both_sides_is_refused_before_anything_is_sent(build):
    adapter = StubAdapter()
    service = await build(adapter)

    response = await service.consult_many(
        capability="coding", prompt=TASK, both_sides=True, persona="skeptic", target_agents=PAIR
    )

    [refusal] = response.results
    assert refusal.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "persona" in refusal.error.message
    assert adapter.prompts == []


async def test_a_side_that_fails_leaves_the_other_answer_and_is_still_labelled(build):
    service = await build(OneSideFails("codex-sol"))

    response = await service.consult_many(
        capability="coding", prompt=TASK, both_sides=True, target_agents=PAIR
    )

    failed, answered = response.results
    assert failed.error.code is ConsultErrorCode.TIMEOUT and failed.content is None
    assert answered.ok and answered.content.answer == "blue"
    assert response.sides == {"codex-sol": "for", "codex-mini": "against"}


async def test_only_consult_many_has_the_argument(build):
    service = await build()

    assert "both_sides" in service.many_request_model.model_json_schema()["properties"]
    assert "both_sides" not in service.request_model.model_json_schema()["properties"]


async def test_through_the_tool_layer_the_defaults_it_passes_do_not_collide(tmp_path, host_claude):
    # The SDK hands the tool every field, `persona=None` included; the service must set
    # its own over that rather than pass it twice.
    missing = {"command": "definitely-not-installed-anywhere", "scores": {"coding": 90}}
    server = build_server(
        mcp_config(
            tmp_path,
            agents={
                "codex-a": {"runtime": "codex", "model": "m", **missing},
                "codex-b": {"runtime": "codex", "model": "n", **missing},
            },
        )
    )

    result = await server.call_tool(
        "orchestrator_consult_many",
        {"capability": "coding", "prompt": "q", "both_sides": True},
    )

    assert result.structured_content["sides"] == {"codex-a": "for", "codex-b": "against"}
    codes = [r["error"]["code"] for r in result.structured_content["results"]]
    assert codes == ["agent_not_installed", "agent_not_installed"]
