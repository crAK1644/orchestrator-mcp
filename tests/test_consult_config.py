"""The `consult:` block: what it accepts, and what it refuses to boot on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from orchestrator_mcp.contract import ConfigError
from orchestrator_mcp.consult.config import HOST_RUNTIME_ENV, ConsultConfig, host_runtime, load_consult_config
from orchestrator_mcp.consult.contract import ConsultationContent, consultation_content_schema

from .conftest import agent, base_config, consult_block


def test_no_consult_block_is_not_an_error():
    assert load_consult_config(base_config()) is None


def test_the_block_parses_with_defaults():
    config = load_consult_config(base_config() | {"consult": consult_block()})
    assert config.protocol_version == "consult-v1"
    assert config.timeout_s == 180
    assert config.store_full_content is True
    assert config.agents["codex-sol"].agent_id == "codex-sol"
    assert config.database_path.is_absolute(), "`~` must be expanded at load, not at open"


@pytest.mark.parametrize(
    "block",
    [
        pytest.param({"agents": {}}, id="no agents"),
        pytest.param({"agents": {"a": agent(runtime="gemini")}}, id="unknown runtime"),
        pytest.param({"agents": {"a": agent(scores={"coding": 101})}}, id="score above 100"),
        pytest.param({"agents": {"a": agent(scores={"coding": -1})}}, id="negative score"),
        pytest.param({"agents": {"a": agent(scores={"cooking": 50})}}, id="unknown capability"),
        pytest.param({"agents": {"a": agent(model="")}}, id="blank model"),
        pytest.param({"agents": {"a": agent(nonsense=1)}}, id="unknown agent key"),
        pytest.param({"agents": {"a": agent(reasoning_effort="xtreme")}}, id="unknown effort"),
        pytest.param(
            {"agents": {"a": agent(runtime="claude", reasoning_effort="xhigh")}},
            id="effort on a runtime that ignores it",
        ),
        pytest.param(consult_block(protocol_version="consult-v2"), id="wrong protocol"),
        pytest.param(consult_block(timeout_s=0), id="zero timeout"),
        pytest.param(consult_block(dashboard={"host": "0.0.0.0"}), id="non-loopback dashboard"),
        pytest.param(consult_block(unknown_key=True), id="unknown top-level key"),
        pytest.param("not a mapping", id="not a mapping"),
    ],
)
def test_a_bad_block_refuses_to_boot(block):
    with pytest.raises(ConfigError):
        load_consult_config(base_config() | {"consult": block})


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_every_reasoning_level_the_cli_accepts_is_configurable(effort):
    config = load_consult_config(
        base_config() | {"consult": {"agents": {"a": agent(reasoning_effort=effort)}}}
    )
    assert config.agents["a"].reasoning_effort == effort


def test_reasoning_effort_defaults_to_unset_rather_than_a_level():
    """Unset must stay unset: the adapter passes `--ignore-user-config`, so choosing a
    default here would override the model's own with a number nobody picked."""
    config = load_consult_config(base_config() | {"consult": {"agents": {"a": agent()}}})
    assert config.agents["a"].reasoning_effort is None


def test_an_omitted_capability_scores_zero():
    config = load_consult_config(base_config() | {"consult": {"agents": {"a": agent(scores={"coding": 50})}}})
    assert config.agents["a"].score_for("coding") == 50
    assert config.agents["a"].score_for("research") == 0


def test_the_host_runtime_is_excluded_from_the_eligible_set():
    config = load_consult_config(base_config() | {"consult": consult_block()})
    assert [a.agent_id for a in config.eligible("claude")] == ["codex-sol"]
    assert [a.agent_id for a in config.eligible("codex")] == ["claude-opus"]


def test_a_disabled_agent_is_never_eligible():
    block = consult_block()
    block["agents"]["codex-sol"]["enabled"] = False
    config = load_consult_config(base_config() | {"consult": block})
    assert config.eligible("claude") == []


def test_the_config_hash_tracks_the_agents_and_not_their_order():
    a = ConsultConfig(**consult_block())
    b = ConsultConfig(**{"agents": dict(reversed(list(consult_block()["agents"].items())))})
    assert a.config_hash() == b.config_hash()

    changed = consult_block()
    changed["agents"]["codex-sol"]["model"] = "gpt-5.6-something-else"
    assert ConsultConfig(**changed).config_hash() != a.config_hash()


def test_the_host_runtime_must_be_set_and_known(monkeypatch):
    monkeypatch.delenv(HOST_RUNTIME_ENV, raising=False)
    with pytest.raises(ConfigError, match=HOST_RUNTIME_ENV):
        host_runtime()

    monkeypatch.setenv(HOST_RUNTIME_ENV, "gemini")
    with pytest.raises(ConfigError):
        host_runtime()

    monkeypatch.setenv(HOST_RUNTIME_ENV, " Claude ")
    assert host_runtime() == "claude"


def test_the_content_schema_is_generated_from_the_model():
    """What the target CLI is told to produce is what we validate against."""
    schema = consultation_content_schema()
    assert set(schema["required"]) == {
        "answer",
        "assumptions",
        "uncertainties",
        "follow_up_questions",
        "sources",
    }
    assert schema["additionalProperties"] is False


def test_every_object_in_the_schema_closes_itself_to_extra_keys():
    """OpenAI's structured outputs refuse a schema where any object -- nested `$defs`
    included -- omits `additionalProperties: false`. The refusal is a 400 from the
    provider, which no fixture executable can produce, so only this catches it."""
    schema = consultation_content_schema()

    def objects(node, path="()"):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield path, node
            for key, value in node.items():
                yield from objects(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from objects(value, f"{path}[{index}]")

    open_objects = [path for path, obj in objects(schema) if obj.get("additionalProperties") is not False]
    assert not open_objects


def test_every_content_field_is_required_even_when_empty():
    with pytest.raises(ValueError):
        ConsultationContent(answer="hi", assumptions=[], uncertainties=[], follow_up_questions=[])

    content = ConsultationContent(
        answer="hi", assumptions=[], uncertainties=[], follow_up_questions=[], sources=[]
    )
    assert content.sources == []


def test_the_commented_consult_block_in_the_example_config_still_loads():
    """The example is documentation that can rot. Uncomment it and it must validate,
    or the first thing a new user copies is a startup error."""
    text = (Path(__file__).parent.parent / "config.example.yaml").read_text()
    block = "consult:" + text.split("# consult:", 1)[1]
    doc = yaml.safe_load("\n".join(re.sub(r"^# ?", "", line) for line in block.splitlines()))
    config = load_consult_config(doc)
    assert sorted(config.agents) == ["claude-opus", "codex-sol"]
    assert config.dashboard.enabled is False
