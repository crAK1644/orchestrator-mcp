"""Configuring agents from the browser: the write surface, over real HTTP.

The read-only suite proves this server will not write. This one proves the one thing
it now can write is reachable only the intended way -- and, where a request is
refused, that the file on disk is untouched rather than merely the status code being
right.
"""

from __future__ import annotations

import os
import stat
from typing import get_args

import pytest
import yaml

from orchestrator_mcp.consult.config import load_consult_config
from orchestrator_mcp.consult.contract import Runtime
from orchestrator_mcp.consult.dashboard import MODEL_PRESETS
from orchestrator_mcp.contract import ConfigError

from .conftest import agent, base_config, consult_block
from .test_consult_dashboard import config, consult, serve  # noqa: F401 -- fixtures


def form(**overrides) -> dict:
    return {
        "id": "codex-luna",
        "runtime": "codex",
        "command": "python3",  # resolvable on any machine that can run this suite
        "model": "gpt-5.6-luna",
        "reasoning_effort": "xhigh",
        "priority": "5",
        "enabled": "on",
        "score.review": "95",
        **overrides,
    }


@pytest.fixture
def editable(tmp_path):
    """A dashboard configured to write, and the file it writes to.

    Built through `load_consult_config` rather than the constructor, because the merge
    with the managed file is what a real boot does -- and calling it again is exactly
    how a test restarts the server after a save."""
    path = tmp_path / "managed" / "agents.yaml"

    def build(**overrides):
        consult_config = load_consult_config(
            base_config()
            | {
                "consult": consult_block(
                    database_path=str(tmp_path / "consultations.sqlite3"),
                    managed_agents_path=str(path),
                    **overrides,
                )
            }
        )
        consult_config.dashboard.enabled = True
        consult_config.dashboard.editable = True
        # Assignment is not re-validated, which is what lets the test ask the OS for a
        # free port rather than guessing one.
        consult_config.dashboard.port = 0
        return consult_config

    build.path = path
    return build


def written(path):
    return (yaml.safe_load(path.read_text()) or {}).get("agents") or {}


# --- getting to the form ----------------------------------------------------


async def test_the_editing_pages_do_not_exist_unless_editing_is_on(serve):  # noqa: F811
    get, _ = serve()
    for path in ("/agents", "/agents/new", "/agents/codex-sol"):
        status, body = get(path)
        assert status == 403, path
        assert "editable" in body, "the refusal has to name the flag that lifts it"

    _, index = get("/")
    assert "/agents" not in index, "no link to a page the config turned off"


async def test_the_index_links_to_the_form_when_editing_is_on(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    _, body = get("/")
    assert "/agents" in body

    status, body = get("/agents/new")
    assert status == 200
    assert "<form" in body and "name=command" in body


async def test_the_form_offers_every_capability_and_every_reasoning_level(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    _, body = get("/agents/new")
    for capability in ("coding", "research", "writing", "reasoning", "review"):
        assert f"score.{capability}" in body
    for level in ("low", "medium", "high", "xhigh", "max"):
        assert f">{level}<" in body


async def test_the_form_suggests_a_model_for_every_runtime_without_closing_the_field(  # noqa: F811
    serve, editable
):
    """A `datalist`, not a `select`. Presets save the operator from remembering that the
    antigravity slugs carry their reasoning level, but a slug that ships tomorrow has to
    be typeable today rather than wait for this list to catch up."""
    get, _ = serve(editable())
    _, body = get("/agents/new")

    assert "<datalist id=model-presets>" in body
    assert "list=model-presets" in body
    # Every runtime the contract offers has something to pick, so adding one to the
    # literal without adding its models leaves a runtime selectable and unguessable.
    for runtime in get_args(Runtime):
        assert MODEL_PRESETS.get(runtime), f"no model presets for runtime `{runtime}`"
        for slug in MODEL_PRESETS[runtime]:
            assert f"value='{slug}' label='{runtime}'" in body


async def test_an_agent_from_config_yaml_is_shown_but_not_editable(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    _, body = get("/agents")
    assert "config.yaml" in body and "codex-sol" in body

    status, body = get("/agents/codex-sol")
    assert status == 403
    assert "config.yaml" in body


# --- refusing a write -------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, header",
    [
        pytest.param(lambda f: f | {"_token": ""}, {}, id="no token"),
        pytest.param(lambda f: f | {"_token": "x" * 43}, {}, id="wrong token"),
        pytest.param(lambda f: f, {"origin": "https://evil.example.com"}, id="foreign origin"),
        pytest.param(lambda f: f, {"origin": "null"}, id="an opaque origin"),
        pytest.param(lambda f: f, {"host": "dashboard.example.com"}, id="foreign host"),
    ],
)
async def test_a_write_that_is_not_ours_is_refused_and_changes_nothing(
    serve, editable, mutate, header  # noqa: F811
):
    """The status code is the easy half. The file is the half that matters: a refusal
    that still wrote would be indistinguishable from a success on the next page load."""
    get, _ = serve(editable())
    body = mutate(form() | {"_token": get.token})

    status, _, _ = get.post("/agents", body, **header)
    assert status == 403
    assert not editable.path.exists(), "nothing may reach disk before the token checks out"


async def test_a_post_is_refused_outright_when_editing_is_off(serve, editable):  # noqa: F811
    """The token is a real one -- the flag is what refuses, not a stale form."""
    get, _ = serve()
    status, body, _ = get.post("/agents", form() | {"_token": get.token})
    assert status == 403
    assert "editable" in body
    assert not editable.path.exists()


async def test_an_oversized_body_is_refused_before_it_is_read(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    status, _, _ = get.post(
        "/agents", form(model="m" * (64 * 1024), _token=get.token)
    )
    assert status == 403
    assert not editable.path.exists()


@pytest.mark.parametrize("length", ["banana", "-1"], ids=["not a number", "negative"])
async def test_a_content_length_that_is_not_a_length_is_refused(serve, editable, length):  # noqa: F811
    """`int()` used to run on it before anything had been checked, so the answer to a
    header like this was a traceback in a request thread rather than a page. `-1` is the
    other half: `rfile.read(-1)` reads until the client stops sending."""
    from http.client import HTTPConnection

    get, _ = serve(editable())
    connection = HTTPConnection("127.0.0.1", get.port, timeout=5)
    connection.putrequest("POST", "/agents", skip_host=True)
    connection.putheader("Host", f"127.0.0.1:{get.port}")
    connection.putheader("Content-Type", "application/x-www-form-urlencoded")
    connection.putheader("Content-Length", length)
    connection.endheaders()

    assert connection.getresponse().status == 403
    connection.close()
    assert not editable.path.exists()


async def test_an_unknown_post_path_is_a_404_and_not_a_write(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    status, _, _ = get.post("/agents/anything", form(_token=get.token))
    assert status == 404
    assert not editable.path.exists()


# --- saving -----------------------------------------------------------------


async def test_the_referrer_policy_leaves_the_origin_header_intact(serve, editable):  # noqa: F811
    """`no-referrer` was the obvious choice and it broke saving: a browser serializes
    `Origin` as `null` under it, so every form post looked cross-site. Nothing on this
    page can reach another origin, so `same-origin` gives up nothing."""
    import http.client

    get, _ = serve(editable())
    connection = http.client.HTTPConnection("127.0.0.1", get.port, timeout=5)
    connection.request("GET", "/agents")
    response = connection.getresponse()
    assert response.getheader("Referrer-Policy") == "same-origin"
    assert "form-action 'self'" in response.getheader("Content-Security-Policy")
    connection.close()


async def test_a_save_writes_the_agent_privately_and_redirects(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    status, _, location = get.post("/agents", form(_token=get.token))

    assert status == 303, "a refresh must not be a resubmit"
    assert location == "/agents?saved=codex-luna"

    saved = written(editable.path)["codex-luna"]
    assert saved == {
        "runtime": "codex",
        "command": "python3",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "xhigh",
        "priority": 5,
        "scores": {"review": 95},
    }, "defaults are left out, so the file says only what was chosen"
    assert stat.S_IMODE(editable.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(editable.path.parent.stat().st_mode) == 0o700


async def test_a_saved_agent_is_on_the_page_the_save_redirects_to(serve, editable):  # noqa: F811
    """Found live: the table used to be built from the config this process loaded at
    boot, so a save landed on `Saved codex-luna` above `No agents configured here yet`,
    with no way to edit or delete it until the dashboard was restarted."""
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))

    _, body = get("/agents?saved=codex-luna")
    assert "No agents configured here yet" not in body
    assert "gpt-5.6-luna" in body
    assert "/agents/codex-luna" in body, "and it is editable without a restart"

    status, _ = get("/agents/codex-luna")
    assert status == 200, "which means the edit page has to serve it too"


async def test_what_was_saved_is_what_the_server_loads_next_boot(serve, editable):  # noqa: F811
    """The point of the whole feature: the round trip through the file has to produce
    a routable agent, not just a plausible-looking YAML document."""
    consult_config = editable()
    get, _ = serve(consult_config)
    get.post("/agents", form(_token=get.token))

    reloaded = load_consult_config(
        base_config()
        | {"consult": consult_block(managed_agents_path=str(editable.path))}
    )
    saved = reloaded.agents["codex-luna"]
    assert saved.model == "gpt-5.6-luna"
    assert saved.reasoning_effort == "xhigh"
    assert saved.score_for("review") == 95
    assert saved.enabled is True and saved.managed is True


async def test_an_unchecked_box_saves_as_off(serve, editable):  # noqa: F811
    """An unchecked checkbox is absent from the body rather than false, which is the
    classic way a form silently ignores being turned off."""
    get, _ = serve(editable())
    body = form(_token=get.token)
    body.pop("enabled")
    get.post("/agents", body)

    assert written(editable.path)["codex-luna"]["enabled"] is False


async def test_a_blank_score_is_omitted_rather_than_zero(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token, **{"score.coding": ""}))
    assert written(editable.path)["codex-luna"]["scores"] == {"review": 95}


async def test_editing_an_agent_shows_what_is_stored_and_replaces_it(serve, editable):  # noqa: F811
    consult_config = editable()
    get, _ = serve(consult_config)
    get.post("/agents", form(_token=get.token))

    # A fresh server, because the running one parsed its config before the save.
    get, _ = serve(editable())
    status, body = get("/agents/codex-luna")
    assert status == 200
    assert "gpt-5.6-luna" in body and "value='95'" in body
    assert "name=_editing" in body, "which is what tells the save this is not a new agent"

    get.post("/agents", form(_token=get.token, _editing="codex-luna", model="gpt-5.6-sol"))
    assert written(editable.path)["codex-luna"]["model"] == "gpt-5.6-sol"


async def test_a_capability_is_a_tick_rather_than_a_number_to_choose(serve, editable):  # noqa: F811
    """Which work the agent is offered, not how good it is at it. The number the router
    ranks on still exists, but nobody is asked to invent one: ties go to `priority`."""
    get, _ = serve(editable())
    _, body = get("/agents/new")

    for capability in ("coding", "research", "writing", "reasoning", "review"):
        assert f"type=checkbox name='score.{capability}' value='100'" in body
    assert "type=number name='score." not in body
    assert " checked> coding" not in body, "a new agent is offered nothing until asked"

    get.post("/agents", form(_token=get.token, **{"score.coding": "100"}))
    assert written(editable.path)["codex-luna"]["scores"] == {"coding": 100, "review": 95}


async def test_a_hand_written_score_survives_a_save_that_was_not_about_it(  # noqa: F811
    serve, editable
):
    """The tick carries the stored number back out in the checkbox's own `value`, so
    `review: 95` -- written by hand to break a tie -- is not flattened to 100 by an
    operator who opened this form to change the model."""
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))

    get, _ = serve(editable())
    _, body = get("/agents/codex-luna")
    assert "type=checkbox name='score.review' value='95' checked" in body
    assert "name='score.coding' value='100'> coding" in body, "untouched, so not ticked"

    # What the browser submits from that page: the ticked box sends its own value.
    get.post("/agents", form(_token=get.token, _editing="codex-luna", model="gpt-5.6-sol"))
    stored = written(editable.path)["codex-luna"]
    assert stored["scores"] == {"review": 95} and stored["model"] == "gpt-5.6-sol"


async def test_a_score_of_zero_on_disk_comes_back_unticked(serve, editable):  # noqa: F811
    """`0` and absent mean the same thing to the router -- ineligible -- so they have to
    look the same on the form. A ticked box that submits `0` would read as offered and
    route nowhere."""
    editable.path.parent.mkdir(parents=True)
    stored = agent("codex", "gpt-5.6-luna") | {"scores": {"review": 0}}
    editable.path.write_text(yaml.safe_dump({"agents": {"zeroed": stored}}))

    get, _ = serve(editable())
    _, body = get("/agents/zeroed")
    assert "name='score.review' value='100'> review" in body


async def test_a_save_leaves_the_agents_it_did_not_touch_alone(serve, editable):  # noqa: F811
    editable.path.parent.mkdir(parents=True)
    editable.path.write_text(yaml.safe_dump({"agents": {"other": agent("codex", "m")}}))

    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))
    assert sorted(written(editable.path)) == ["codex-luna", "other"]


# --- refusing a bad save ----------------------------------------------------


@pytest.mark.parametrize(
    "bad, expected",
    [
        pytest.param({"id": "Codex Luna"}, "agent id", id="spaces and capitals"),
        pytest.param({"id": ""}, "agent id", id="blank"),
        pytest.param({"id": "../../etc/passwd"}, "agent id", id="a path"),
        pytest.param({"id": "codex-sol"}, "config.yaml", id="already in config.yaml"),
        pytest.param({"model": ""}, "model", id="no model"),
        pytest.param({"runtime": "gemini"}, "runtime", id="unknown runtime"),
        pytest.param(
            {"runtime": "claude", "reasoning_effort": "xhigh"},
            "codex-only",
            id="effort on a runtime that ignores it",
        ),
        pytest.param({"score.review": "101"}, "scores", id="score above 100"),
        # These two used to save. `.isdigit()` is false for both, and the fallback was
        # 100 -- so a typo in a routing tie-break was accepted at a number nobody typed.
        pytest.param({"priority": "-1"}, "priority", id="a negative priority"),
        pytest.param({"priority": "high"}, "priority", id="a priority that is not a number"),
    ],
)
async def test_a_bad_save_re_renders_the_form_and_writes_nothing(
    serve, editable, bad, expected  # noqa: F811
):
    get, _ = serve(editable())
    status, body, _ = get.post("/agents", form(_token=get.token, **bad))

    assert status == 200, "a form error is a form, not a 500 and not a redirect"
    assert expected in body
    assert "<form" in body, "and it comes back with what was typed, not an empty page"
    assert not editable.path.exists()


async def test_a_command_that_does_not_resolve_is_refused_by_name(serve, editable):  # noqa: F811
    """The mistake this catches is the real one: a CLI that is installed but not on
    PATH. Resolving is a lookup -- nothing is executed to find out."""
    get, _ = serve(editable())
    status, body, _ = get.post(
        "/agents", form(_token=get.token, command="/nowhere/codex-typo")
    )

    assert status == 200
    assert "codex-typo" in body
    assert not editable.path.exists()


async def test_a_failed_save_leaves_an_existing_file_byte_identical(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))
    before = editable.path.read_bytes()

    get.post("/agents", form(_token=get.token, id="also-fine", model=""))
    assert editable.path.read_bytes() == before


async def test_a_failed_edit_comes_back_as_an_edit(serve, editable):  # noqa: F811
    """It used to come back as `New agent`, with the id no longer `readonly`. Correcting
    the field the error named and saving then filed a second agent under whatever the id
    box happened to say."""
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))

    get, _ = serve(editable())
    status, body, _ = get.post(
        "/agents", form(_token=get.token, _editing="codex-luna", model="")
    )
    assert status == 200
    assert "Edit agent" in body and "New agent" not in body
    assert "readonly" in body


async def test_an_edit_cannot_be_turned_into_a_rename(serve, editable):  # noqa: F811
    """`readonly` is the browser's half. This is the half that holds when the post did
    not come from the browser: renaming this way would leave the old agent in place."""
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))

    get, _ = serve(editable())
    status, body, _ = get.post(
        "/agents", form(_token=get.token, _editing="codex-luna", id="codex-sol-2")
    )
    assert status == 200
    assert "codex-luna" in body
    assert sorted(written(editable.path)) == ["codex-luna"]


async def test_adding_an_agent_that_is_already_here_is_refused_rather_than_replacing_it(
    serve, editable  # noqa: F811
):
    """Only `config.yaml` ids used to be checked, so `Add an agent` with an id already
    in this file overwrote it and reported a save."""
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))

    get, _ = serve(editable())
    status, body, _ = get.post("/agents", form(_token=get.token, model="gpt-5.6-sol"))
    # 409 and not 200: the same refusal the delete beside it answers with, carrying the
    # form back so nothing typed is lost.
    assert status == 409
    assert "already configured here" in body
    assert written(editable.path)["codex-luna"]["model"] == "gpt-5.6-luna"


async def test_a_priority_field_left_empty_is_the_default_and_not_an_error(serve, editable):  # noqa: F811
    """A cleared number input is dropped from the body entirely, which is what tells
    the difference between "not set" and "set to nonsense"."""
    get, _ = serve(editable())
    body = form(_token=get.token)
    body.pop("priority")
    status, _, _ = get.post("/agents", body)

    assert status == 303
    assert "priority" not in written(editable.path)["codex-luna"], "the default, unwritten"


async def test_a_managed_file_that_cannot_be_read_refuses_the_write(serve, editable):  # noqa: F811
    """The page falls back to the boot-time config so a broken file still renders. The
    write must not use that fallback: saving it back would overwrite whatever the
    operator broke, including the part they opened the file to fix.

    Broken after the server is up, because a file that is already broken refuses the
    boot -- which is why this is reachable only from a hand-edit while it runs."""
    get, _ = serve(editable())
    editable.path.parent.mkdir(parents=True)
    editable.path.write_text("agents: [not, a, mapping]")
    before = editable.path.read_bytes()

    status, _ = get("/agents")
    assert status == 200, "reading the page still works"

    status, body, _ = get.post("/agents", form(_token=get.token))
    assert status == 409, "a form again -- not a traceback in the request thread"
    assert "mapping" in body and "<form" in body

    status, body, _ = get.post("/agents/delete", {"_token": get.token, "id": "codex-luna"})
    assert status == 409
    assert "mapping" in body

    assert editable.path.read_bytes() == before


async def test_a_hand_edited_entry_the_form_cannot_fix_blocks_the_write(serve, editable):  # noqa: F811
    """A save rewrites the whole file, so it puts this process's name on every entry in
    it -- including one someone else broke. Writing that back means the next start
    refuses on a file the page reported as saved."""
    get, _ = serve(editable())
    editable.path.parent.mkdir(parents=True)
    editable.path.write_text(yaml.safe_dump({"agents": {"": agent("codex", "m")}}))

    status, body, _ = get.post("/agents", form(_token=get.token))
    assert status == 409
    assert "blank or non-text id" in body
    assert written(editable.path) == {"": agent("codex", "m")}, "left as it was found"

    _, page = get("/agents")
    assert "No agents configured here yet" in page, "and not offered for editing"


async def test_a_file_with_two_kinds_of_key_refuses_the_write_rather_than_crashing(
    serve, editable  # noqa: F811
):
    """`1:` is an int key and `alpha:` is a string one, and the two cannot be compared,
    so sorting them raised out of the request thread past every catch around it."""
    get, _ = serve(editable())
    editable.path.parent.mkdir(parents=True)
    editable.path.write_text(
        yaml.safe_dump({"agents": {1: agent("codex", "m"), "alpha": agent("codex", "m")}})
    )
    before = editable.path.read_bytes()

    status, body, _ = get.post("/agents", form(_token=get.token))
    assert status == 409
    assert "non-text id" in body
    assert editable.path.read_bytes() == before

    status, _ = get("/agents")
    assert status == 200, "and the page still renders"


async def test_an_entry_that_spells_out_its_own_agent_id_is_not_refused(serve, editable):  # noqa: F811
    """A boot accepts `agent_id:` inside an entry and overwrites it from the key. This
    page has to accept it too, or it refuses a file the server starts on fine -- and
    refuses it while showing a table that looks perfectly normal."""
    get, _ = serve(editable())
    editable.path.parent.mkdir(parents=True)
    editable.path.write_text(
        yaml.safe_dump({"agents": {"alpha": agent("codex", "m") | {"agent_id": "alpha"}}})
    )

    _, page = get("/agents")
    assert "alpha" in page, "shown, because the next boot shows it too"

    status, _, location = get.post("/agents", form(_token=get.token))
    assert status == 303, "and saving alongside it is not a conflict"
    assert location == "/agents?saved=codex-luna"
    assert sorted(written(editable.path)) == ["alpha", "codex-luna"]


async def test_an_agent_id_of_the_wrong_type_is_refused_rather_than_dropped(serve, editable):  # noqa: F811
    """The entry's own `agent_id` is dropped before validating, because a boot accepts it
    and overwrites it from the key. A boot does not accept *any* type for it, so dropping
    it unexamined waved through the one file this check exists to catch."""
    get, _ = serve(editable())
    editable.path.parent.mkdir(parents=True)
    editable.path.write_text(
        yaml.safe_dump({"agents": {"alpha": agent("codex", "m") | {"agent_id": []}}})
    )
    before = editable.path.read_bytes()

    status, body, _ = get.post("/agents", form(_token=get.token))
    assert status == 409
    assert "not text" in body
    assert editable.path.read_bytes() == before

    with pytest.raises(ConfigError):
        editable()  # the boot this was predicting does refuse it


async def test_an_id_in_both_files_blocks_the_write_before_the_next_boot_refuses_it(
    serve, editable  # noqa: F811
):
    """Two files naming one agent is a startup error. Writing that file back would mean
    this page reporting a save on a config that will not start."""
    get, _ = serve(editable())
    editable.path.parent.mkdir(parents=True)
    editable.path.write_text(yaml.safe_dump({"agents": {"codex-sol": agent("codex", "m")}}))
    before = editable.path.read_bytes()

    status, body, _ = get.post("/agents", form(_token=get.token))
    assert status == 409
    assert "config.yaml" in body
    assert editable.path.read_bytes() == before


def source_config(path, agents: dict) -> None:
    """Write the `config.yaml` the dashboard re-reads, with `agents` in its consult
    block. Not the same file as `editable.path` -- this is the operator's own, which
    this page never writes and only ever reads ids out of."""
    path.write_text(yaml.safe_dump(base_config() | {"consult": consult_block(agents=agents)}))


async def test_an_id_added_to_config_yaml_after_boot_still_blocks_the_write(
    serve, editable, tmp_path  # noqa: F811
):
    """The case a boot snapshot cannot see. `codex-luna` is not in `config.yaml` when
    this dashboard starts, so nothing it loaded makes the save a duplicate -- the file
    does, and only if it is read again. Saving anyway writes a file the next start
    refuses, from the check that exists to stop exactly that."""
    source = tmp_path / "config.yaml"
    source_config(source, {"codex-sol": agent()})
    get, _ = serve(editable(), source)

    source_config(source, {"codex-sol": agent(), "codex-luna": agent()})
    status, body, _ = get.post("/agents", form(_token=get.token))

    assert status == 200 and "config.yaml" in body
    assert not editable.path.exists(), "and nothing was written on the way to saying so"


async def test_an_id_deleted_from_config_yaml_after_boot_stops_blocking_the_write(
    serve, editable, tmp_path  # noqa: F811
):
    """The other half of the same read. A snapshot refuses `codex-sol` for as long as
    the process lives; the file is what the next boot reads, and it no longer has it."""
    source = tmp_path / "config.yaml"
    source_config(source, {"codex-sol": agent()})
    get, _ = serve(editable(), source)

    source_config(source, {"claude-opus": agent("claude", "opus")})
    status, _, location = get.post("/agents", form(_token=get.token, id="codex-sol"))

    assert status == 303 and location == "/agents?saved=codex-sol"
    assert "codex-sol" in written(editable.path)


async def test_a_config_yaml_that_cannot_be_read_falls_back_to_what_booted(
    serve, editable, tmp_path  # noqa: F811
):
    """Moved, or half-written by an editor that truncates before it saves. Stale is a
    worse answer than fresh and a much better one than treating the file as empty,
    which would let every id in it through."""
    missing = tmp_path / "not-here.yaml"
    get, _ = serve(editable(), missing)
    status, body, _ = get.post("/agents", form(_token=get.token, id="codex-sol"))
    assert status == 200 and str(missing) in body, "and the refusal names the file it read"

    source = tmp_path / "config.yaml"
    source.write_text("consult: {agents: [")
    get, _ = serve(editable(), source)
    status, body, _ = get.post("/agents", form(_token=get.token, id="codex-sol"))
    assert status == 200 and str(source) in body


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="truncated to nothing"),
        pytest.param("capabilities: {}\n", id="truncated above the consult block"),
        pytest.param("consult:\n", id="a consult block with nothing under it"),
    ],
)
async def test_a_config_yaml_caught_mid_write_does_not_read_as_no_agents(
    serve, editable, tmp_path, content  # noqa: F811
):
    """An editor that truncates before it writes leaves the file empty for a moment. Read
    as "config.yaml defines nobody", that window is when this check waves through the
    duplicate it exists to catch -- and the operator's editor finishes a keystroke later,
    putting the id back and leaving a file the next start refuses."""
    source = tmp_path / "config.yaml"
    source_config(source, {"codex-sol": agent()})
    get, _ = serve(editable(), source)

    source.write_text(content)
    status, body, _ = get.post("/agents", form(_token=get.token, id="codex-sol"))

    assert status == 200 and str(source) in body
    assert not editable.path.exists()


async def test_a_config_yaml_that_really_has_no_agents_is_taken_at_its_word(
    serve, editable, tmp_path  # noqa: F811
):
    """The other side of that: every agent living in the managed file is a supported
    config, not a truncated read, so an empty `agents:` under a real `consult:` must not
    be second-guessed into refusing a save."""
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump(base_config() | {"consult": {"timeout_s": 60}}))
    get, _ = serve(editable(), source)

    status, _, location = get.post("/agents", form(_token=get.token, id="codex-sol"))
    assert status == 303 and location == "/agents?saved=codex-sol"


async def test_an_id_in_both_files_still_leaves_the_row_that_can_delete_it(
    serve, editable, tmp_path  # noqa: F811
):
    """The duplicate is only fixable from here by deleting the managed copy, so the page
    that reports it has to keep the row with that button on it. Dropping the whole
    editable table over a duplicate leaves the operator the refusal and no way to act on
    it."""
    source = tmp_path / "config.yaml"
    source_config(source, {"claude-opus": agent("claude", "opus")})
    get, _ = serve(editable(), source)
    get.post("/agents", form(_token=get.token))  # saved while config.yaml had no codex-luna

    source_config(source, {"claude-opus": agent("claude", "opus"), "codex-luna": agent()})
    status, body = get("/agents")

    assert status == 200
    assert "/agents/codex-luna" in body, "the row is still there, and still editable"
    assert "codex-luna" in written(editable.path)


async def test_an_agent_moved_out_of_config_yaml_is_not_listed_as_still_being_in_it(
    serve, editable, tmp_path  # noqa: F811
):
    """Moving an agent between the files is the whole point of allowing the save. Listing
    it in both tables afterwards says the config is in the state the server refuses to
    start on, which is the opposite of what just happened."""
    source = tmp_path / "config.yaml"
    source_config(source, {"codex-sol": agent()})
    get, _ = serve(editable(), source)

    source_config(source, {"claude-opus": agent("claude", "opus")})
    get.post("/agents", form(_token=get.token, id="codex-sol"))
    _, body = get("/agents")

    assert "/agents/codex-sol" in body, "editable, because that is where it lives now"
    _, read_only = body.split("Defined in config.yaml")
    assert "claude-opus" in read_only, "the section is rendered, so the next line means something"
    assert "codex-sol" not in read_only, "and does not still claim the agent that moved out"


async def test_a_managed_file_that_is_not_text_refuses_the_write(serve, editable):  # noqa: F811
    """Bytes that are not text read fine and fail at decoding, which is not an
    `OSError` and used to escape the only catch that was there."""
    get, _ = serve(editable())
    editable.path.parent.mkdir(parents=True)
    editable.path.write_bytes(b"\xff\xfe agents:")
    before = editable.path.read_bytes()

    status, body, _ = get.post("/agents", form(_token=get.token))
    assert status == 409
    assert "<form" in body, "a page, not a traceback"
    assert editable.path.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a directory it has no bit for")
async def test_a_managed_file_that_cannot_be_written_says_so(serve, editable):  # noqa: F811
    """Readable and unwritable is a state the read guard says nothing about: the file
    parses, the entry validates, and the failure lands on `os.replace`."""
    get, _ = serve(editable())
    editable.path.parent.mkdir(parents=True)
    editable.path.write_text(yaml.safe_dump({"agents": {}}))
    os.chmod(editable.path.parent, 0o500)
    try:
        status, body, _ = get.post("/agents", form(_token=get.token))
    finally:
        os.chmod(editable.path.parent, 0o700)

    assert status == 500
    assert "could not be written" in body
    assert written(editable.path) == {}


async def test_an_agent_deleted_while_the_form_was_open_is_not_recreated_by_saving_it(
    serve, editable  # noqa: F811
):
    """The duplicate and existence checks used to run against the page's reading of the
    file, which is a guess by the time the form comes back."""
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))
    editable.path.write_text(yaml.safe_dump({"agents": {}}))

    status, body, _ = get.post("/agents", form(_token=get.token, _editing="codex-luna"))
    assert status == 409
    assert "no longer in this file" in body
    assert written(editable.path) == {}


async def test_two_saves_at_once_both_survive(serve, editable, monkeypatch):  # noqa: F811
    """Each request gets its own thread, and a save is a read, an edit and a write. Two
    of them interleaved used to end with whichever wrote second having dropped the
    other's agent -- both redirecting to `?saved=`, one of them a lie."""
    import threading
    import time

    from orchestrator_mcp.consult import dashboard as module

    real = module.read_managed

    def slow(path):
        agents = real(path)
        # Wide enough that both requests are certain to have read before either writes,
        # which is the interleaving. The lock is what has to make it harmless.
        time.sleep(0.2)
        return agents

    monkeypatch.setattr(module, "read_managed", slow)

    get, _ = serve(editable())
    threads = [
        threading.Thread(target=get.post, args=("/agents", form(_token=get.token, id=f"agent-{n}")))
        for n in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(written(editable.path)) == ["agent-0", "agent-1"]


async def test_an_agent_id_containing_markup_never_reaches_the_page_as_markup(
    serve, editable  # noqa: F811
):
    get, _ = serve(editable())
    status, body, _ = get.post("/agents", form(_token=get.token, id="<script>x</script>"))
    assert status == 200
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body


# --- deleting ---------------------------------------------------------------


async def test_delete_removes_the_managed_agent_and_leaves_the_written_one(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))

    get, consult_config = serve(editable())
    status, _, location = get.post("/agents/delete", {"_token": get.token, "id": "codex-luna"})
    assert status == 303
    assert location == "/agents?deleted=codex-luna"
    assert written(editable.path) == {}
    assert "codex-sol" in consult_config.agents, "config.yaml is not this page's to edit"


async def test_delete_cannot_reach_an_agent_defined_in_config_yaml(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    status, _, _ = get.post("/agents/delete", {"_token": get.token, "id": "codex-sol"})
    assert status == 404
    assert not editable.path.exists()


async def test_delete_without_the_token_changes_nothing(serve, editable):  # noqa: F811
    get, _ = serve(editable())
    get.post("/agents", form(_token=get.token))
    before = editable.path.read_bytes()

    status, _, _ = get.post("/agents/delete", {"_token": "nope", "id": "codex-luna"})
    assert status == 403
    assert editable.path.read_bytes() == before


# --- telling the truth about restarts ---------------------------------------


async def test_a_stale_config_hash_in_the_store_says_to_restart(serve, editable, tmp_path):  # noqa: F811
    """The MCP server read its config at boot and this is a different process, so the
    only evidence available is what the last consultation recorded."""
    consult_config = editable()
    await consult(consult_config, capability="review", prompt="anything")

    get, _ = serve(consult_config)
    _, body = get("/agents")
    assert "Restart" not in body, "nothing has changed yet"

    # Exactly what a save does: the file gains an agent, and the process that recorded
    # that consultation has not reread it.
    get.post("/agents", form(_token=get.token))
    get, _ = serve(editable())
    _, body = get("/agents")
    assert "Restart" in body
