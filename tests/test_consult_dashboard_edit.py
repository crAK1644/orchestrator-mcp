"""Configuring agents from the browser: the write surface, over real HTTP.

The read-only suite proves this server will not write. This one proves the one thing
it now can write is reachable only the intended way -- and, where a request is
refused, that the file on disk is untouched rather than merely the status code being
right.
"""

from __future__ import annotations

import stat

import pytest
import yaml

from orchestrator_mcp.consult.config import load_consult_config

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

    get.post("/agents", form(_token=get.token, model="gpt-5.6-sol"))
    assert written(editable.path)["codex-luna"]["model"] == "gpt-5.6-sol"


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
    assert status == 200, "a form again -- not a traceback in the request thread"
    assert "mapping" in body and "<form" in body

    status, body, _ = get.post("/agents/delete", {"_token": get.token, "id": "codex-luna"})
    assert status == 409
    assert "mapping" in body

    assert editable.path.read_bytes() == before


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
