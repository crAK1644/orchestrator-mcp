"""The `review:` block, from either file it can live in.

Every refusal here is a boot error naming the fix, for the reason the rest of the
config works that way: a server that half-understands who reviews is worse than
one that will not start. And the managed file has to reach `ReviewConfig` at all,
which is the bug the two-file merge was extended to fix.
"""

from __future__ import annotations

import pytest
import yaml

from orchestrator_mcp.consult.config import ConsultConfig, load_consult_config
from orchestrator_mcp.consult.managed import read_managed, read_managed_document, write_managed
from orchestrator_mcp.contract import ConfigError

from .conftest import agent, consult_block


# --- the shape of the block -------------------------------------------------


def test_a_configured_pair_of_reviewers_loads():
    loaded = ConsultConfig(
        **consult_block(review={"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol"]})
    )
    assert loaded.review.reviewers == ["codex-sol"]


def test_no_review_block_leaves_the_review_surface_off():
    """A server with no reviewers should not advertise a `review` tool that can only
    refuse."""
    assert ConsultConfig(**consult_block()).review is None


@pytest.mark.parametrize(
    "review, expected",
    [
        ({"reviewers": ["codex-sol", "claude-opus"]}, "exactly one"),
        ({"reviewers": []}, "exactly one"),
        ({"reviewers": ["codex-sol"], "deep_reviewers": []}, "1 to 5"),
        (
            {"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol", "codex-sol"]},
            "more than once",
        ),
        ({"reviewers": ["nobody"], "deep_reviewers": ["codex-sol"]}, "not a configured agent"),
    ],
)
def test_a_malformed_reviewer_list_refuses_and_names_the_fix(review, expected):
    with pytest.raises(Exception) as exc:
        ConsultConfig(**consult_block(review=review))
    assert expected in str(exc.value)


def test_six_deep_reviewers_are_refused():
    """Not an arbitrary limit: each one is a paid request, run in parallel, against
    material the user approved once."""
    agents = {f"a{n}": agent("codex", f"m{n}") for n in range(6)}
    with pytest.raises(Exception, match="1 to 5"):
        ConsultConfig(
            **consult_block(
                agents=agents,
                review={"reviewers": ["a0"], "deep_reviewers": list(agents)},
            )
        )


def test_a_disabled_reviewer_is_refused():
    with pytest.raises(Exception, match="disabled"):
        ConsultConfig(
            **consult_block(
                agents={"off": agent("codex", enabled=False)}, review={"reviewers": ["off"]}
                | {"deep_reviewers": ["off"]}
            )
        )


def test_a_reviewer_that_scores_zero_for_review_is_refused():
    """Named reviewers do the choosing, but the score is still what says the agent
    is meant to be asked this kind of question."""
    with pytest.raises(Exception, match="scores 0 for `review`"):
        ConsultConfig(
            **consult_block(
                agents={"weak": agent("codex", scores={"coding": 90})},
                review={"reviewers": ["weak"], "deep_reviewers": ["weak"]},
            )
        )


# --- the two files ----------------------------------------------------------


def test_a_review_block_in_the_managed_file_reaches_the_config(tmp_path, host_claude):
    """The bug the merge was widened for: before this, a `review:` saved by the
    dashboard was read and then dropped on the floor."""
    managed = tmp_path / "agents.yaml"
    write_managed(managed, {}, {"reviewers": ["codex-sol"], "deep_reviewers": ["claude-opus"]})

    loaded = load_consult_config(
        {"consult": consult_block(
            database_path=str(tmp_path / "c.sqlite3"), managed_agents_path=str(managed)
        )}
    )
    assert loaded.review.reviewers == ["codex-sol"]
    assert loaded.review.deep_reviewers == ["claude-opus"]


def test_a_review_block_in_both_files_refuses_to_boot(tmp_path, host_claude):
    """A precedence rule is how an edit comes to save successfully and do nothing."""
    managed = tmp_path / "agents.yaml"
    write_managed(
        managed, {}, {"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol"]}
    )

    with pytest.raises(ConfigError, match="defined in both"):
        load_consult_config(
            {"consult": consult_block(
                database_path=str(tmp_path / "c.sqlite3"),
                managed_agents_path=str(managed),
                review={"reviewers": ["claude-opus"]},
            )}
        )


def test_saving_reviewers_does_not_drop_the_agents(tmp_path):
    """Both keys are written together, so one save cannot erase the other half."""
    managed = tmp_path / "agents.yaml"
    write_managed(managed, {"codex-sol": agent()}, {"reviewers": ["codex-sol"]})

    document = read_managed_document(managed)
    assert set(document["agents"]) == {"codex-sol"}
    assert document["review"] == {"reviewers": ["codex-sol"]}


def test_a_file_with_no_reviewers_writes_no_review_key(tmp_path):
    """A file the dashboard has only ever saved agents into stays byte-identical to
    what earlier versions wrote."""
    managed = tmp_path / "agents.yaml"
    write_managed(managed, {"codex-sol": agent()})
    assert set(yaml.safe_load(managed.read_text())) == {"agents"}
    assert read_managed_document(managed)["review"] is None


def test_the_old_single_key_reader_still_works(tmp_path):
    """`read_managed` is kept as a wrapper, because the dashboard and its tests call
    it and this change was not supposed to reach them."""
    managed = tmp_path / "agents.yaml"
    write_managed(managed, {"codex-sol": agent()}, {"reviewers": ["codex-sol"]})
    assert set(read_managed(managed)) == {"codex-sol"}


def test_a_review_key_that_is_not_a_mapping_is_refused(tmp_path):
    managed = tmp_path / "agents.yaml"
    managed.write_text("agents: {}\nreview: [codex-sol]\n")
    with pytest.raises(ConfigError, match="`review:`"):
        read_managed_document(managed)


def test_the_managed_file_is_read_once_per_load(tmp_path, host_claude, monkeypatch):
    """Two reads can straddle a dashboard save and merge the agents from before it
    with the reviewers from after -- a combination nobody saved and no validator
    downstream could spot."""
    managed = tmp_path / "agents.yaml"
    write_managed(
        managed, {}, {"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol"]}
    )

    reads = []
    original = read_managed_document

    def counted(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr("orchestrator_mcp.consult.config.read_managed_document", counted)
    load_consult_config(
        {"consult": consult_block(
            database_path=str(tmp_path / "c.sqlite3"), managed_agents_path=str(managed)
        )}
    )
    assert len(reads) == 1


@pytest.mark.parametrize(
    "review",
    [
        {},
        {"reviewers": ["codex-sol"]},
        {"deep_reviewers": ["codex-sol"]},
    ],
)
def test_review_defaults_are_validated_instead_of_booting_an_empty_surface(review):
    with pytest.raises(Exception):
        ConsultConfig(**consult_block(review=review))


def test_an_explicit_null_review_disables_the_managed_block(tmp_path, host_claude):
    managed = tmp_path / "agents.yaml"
    write_managed(
        managed, {}, {"reviewers": ["codex-sol"], "deep_reviewers": ["codex-sol"]}
    )

    loaded = load_consult_config(
        {
            "consult": consult_block(
                database_path=str(tmp_path / "c.sqlite3"),
                managed_agents_path=str(managed),
                review=None,
            )
        }
    )
    assert loaded.review is None


def test_an_explicit_empty_managed_review_is_preserved_for_validation(tmp_path):
    managed = tmp_path / "agents.yaml"
    write_managed(managed, {}, {})
    assert "review" in yaml.safe_load(managed.read_text())
    with pytest.raises(Exception):
        load_consult_config(
            {
                "consult": consult_block(
                    database_path=str(tmp_path / "c.sqlite3"),
                    managed_agents_path=str(managed),
                )
            }
        )
