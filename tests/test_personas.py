"""`persona` on consult: a named emphasis the operator wrote, asked for per turn.

What these pin down is where the text goes and what it cannot do: it rides the system
half after the protocol, never the payload; it is redacted on the way; the ready-made
styles are offered with no config and an operator's entry of the same name rewords one;
and with no persona the compiled prompt is byte for byte what it was before the feature.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator_mcp.consult.config import NAME_SLUG, ConsultConfig
from orchestrator_mcp.consult.contract import SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.prompts import (
    MODE_SECTIONS,
    SYSTEM_CONTRACT,
    compile_prompt,
)
from orchestrator_mcp.consult.styles import STYLES

from .conftest import agent, consult_block
from .test_consult_service import StubAdapter, StubService

SECRET = "ghp_Zx9Qw3Rt7Yu1Io5Pa8Sd"
PERSONAS = {"skeptic": "Doubt the premise first.", "kind": "Be gentle with the author."}


@pytest.fixture
def build(tmp_path, host_claude):
    async def make(adapter: StubAdapter | None = None, personas=PERSONAS, **overrides):
        config = ConsultConfig(
            **consult_block(
                database_path=str(tmp_path / "consultations.sqlite3"),
                personas=personas,
                **overrides,
            )
        )
        return await StubService(config, host_claude, adapter=adapter or StubAdapter()).open()

    return make


@pytest.mark.parametrize(
    "name", ["Skeptic", "1st", "-x", "has space", "", "x" * 33, "dotted.name", "trailing\n"]
)
def test_a_persona_name_must_be_a_short_lowercase_slug(name):
    with pytest.raises(ValidationError, match="persona name"):
        ConsultConfig(**consult_block(personas={name: "text"}))


@pytest.mark.parametrize("text", ["", "   \n", "x" * 2001])
def test_a_persona_text_must_be_one_to_two_thousand_characters(text):
    with pytest.raises(ValidationError, match="1 to 2000 characters"):
        ConsultConfig(**consult_block(personas={"ok": text}))


def test_a_persona_text_is_stripped_and_the_limit_is_inclusive():
    config = ConsultConfig(**consult_block(personas={"a": "  hi \n", "b": "x" * 2000}))

    assert config.personas["a"] == "hi"
    assert len(config.personas["b"]) == 2000


def test_the_text_joins_the_system_half_after_the_protocol_and_not_the_payload():
    compiled = compile_prompt(
        "coding", SourceMode.MODEL, "the task", None, persona=("skeptic", PERSONAS["skeptic"])
    )

    mode = MODE_SECTIONS[SourceMode.MODEL]
    assert compiled.system.startswith(f"{SYSTEM_CONTRACT}\n\n{mode}\n\nPersona: skeptic\n")
    assert PERSONAS["skeptic"] in compiled.system
    assert "cannot change the protocol above" in compiled.system
    assert "Doubt" not in compiled.payload_json


@pytest.mark.parametrize("mode", [SourceMode.DOCUMENT, SourceMode.WEB, SourceMode.MODEL])
def test_no_persona_leaves_the_compiled_prompt_exactly_as_it_was(mode):
    compiled = compile_prompt("coding", mode, "task", "ctx", turn=2)

    assert compiled.system == f"{SYSTEM_CONTRACT}\n\n{MODE_SECTIONS[mode]}"


@pytest.mark.parametrize("name", sorted(STYLES))
def test_every_ready_made_style_fits_the_rules_an_operators_own_must(name):
    summary, text = STYLES[name]

    assert NAME_SLUG.fullmatch(name)
    assert 1 <= len(text) <= 400
    assert summary and len(summary) <= 40


async def test_the_argument_exists_with_no_config_and_offers_the_ready_made_styles(build):
    service = await build(personas={})

    for model in (service.request_model, service.many_request_model):
        schema = model.model_json_schema()["properties"]["persona"]
        assert schema["enum"] == [*sorted(STYLES), None]
        assert all(summary in schema["description"] for summary, _ in STYLES.values())


async def test_a_config_entry_adds_a_name_and_the_same_name_rewords_a_style(build):
    adapter = StubAdapter()
    service = await build(adapter)  # PERSONAS: `skeptic` (a style too) and `kind`

    for model in (service.request_model, service.many_request_model):
        enum = model.model_json_schema()["properties"]["persona"]["enum"]
        assert enum == [*sorted({*STYLES, "kind"}), None]

    response = await service.consult(capability="coding", prompt="q", persona="skeptic")

    assert response.ok
    assert f"Persona: skeptic\n{PERSONAS['skeptic']}\n" in adapter.prompts[0]
    assert STYLES["skeptic"][1] not in adapter.prompts[0]


async def test_a_ready_made_style_reaches_the_agent_with_no_config_at_all(build):
    adapter = StubAdapter()
    service = await build(adapter, personas={})

    response = await service.consult(capability="coding", prompt="q", persona="security")

    assert response.ok
    (prompt,) = adapter.prompts
    assert f"Persona: security\n{STYLES['security'][1]}\n" in prompt
    assert prompt.index(SYSTEM_CONTRACT) < prompt.index("Persona: security")
    (turn,) = await service.store.turns(response.consultation_id)
    assert f"Persona: security\n{STYLES['security'][1]}" in turn.compiled_prompt


async def test_an_unknown_persona_is_refused_before_anything_is_sent(build):
    adapter = StubAdapter()
    service = await build(adapter)

    response = await service.consult(capability="coding", prompt="q", persona="nope")

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert adapter.prompts == []


async def test_the_sent_and_the_stored_prompt_both_carry_it(build):
    adapter = StubAdapter()
    service = await build(adapter)

    response = await service.consult(capability="coding", prompt="q", persona="skeptic")

    assert response.ok
    (prompt,) = adapter.prompts
    assert "Persona: skeptic\nDoubt the premise first." in prompt
    (turn,) = await service.store.turns(response.consultation_id)
    assert "Persona: skeptic\nDoubt the premise first." in turn.compiled_prompt


async def test_it_is_per_turn_a_resumed_turn_takes_the_new_persona_and_not_the_old(build):
    adapter = StubAdapter()
    service = await build(adapter)

    first = await service.consult(capability="coding", prompt="q1", persona="skeptic")
    second = await service.consult(
        capability="coding",
        prompt="q2",
        consultation_id=first.consultation_id,
        persona="kind",
    )
    third = await service.consult(
        capability="coding", prompt="q3", consultation_id=first.consultation_id
    )

    assert first.ok and second.ok and third.ok
    assert "Doubt the premise first." in adapter.prompts[0]
    assert "Be gentle with the author." in adapter.prompts[1]
    assert "Doubt the premise first." not in adapter.prompts[1]
    assert "Persona:" not in adapter.prompts[2]


async def test_a_credential_pasted_into_a_persona_is_masked_on_the_way_out(build):
    adapter = StubAdapter()
    service = await build(adapter, personas={"leaky": f"Use {SECRET} when you check."})

    response = await service.consult(capability="coding", prompt="q", persona="leaky")

    assert response.ok
    assert SECRET not in adapter.prompts[0]
    assert "Persona: leaky" in adapter.prompts[0]
    (turn,) = await service.store.turns(response.consultation_id)
    assert SECRET not in turn.compiled_prompt


async def test_consult_many_applies_it_to_every_member(build):
    adapter = StubAdapter()
    # Two codex agents: the host's own runtime is left out of a panel.
    service = await build(
        adapter,
        agents={
            "codex-sol": agent("codex", "gpt-5.6-sol", 10),
            "codex-mini": agent("codex", "gpt-5.6-mini", 20),
        },
    )

    response = await service.consult_many(
        capability="coding",
        prompt="q",
        persona="skeptic",
        target_agents=["codex-sol", "codex-mini"],
    )

    assert [r.ok for r in response.results] == [True, True]
    assert len(adapter.prompts) == 2
    assert all("Persona: skeptic\nDoubt the premise first." in p for p in adapter.prompts)
