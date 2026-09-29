"""`context_paths` on consult: files this server reads for the caller.

Review has had the argument since 0.6; these prove the consult side of the same
reader. The differences are the ones that matter: the argument only exists once
`consult.context_roots:` is set, and a file that looks like it holds a credential is
refused, because consult sends at once and there is no preview to read first.
"""

from __future__ import annotations

import os

import pytest

import orchestrator_mcp.consult.service as service_module
from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.contract import SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode

from .conftest import agent, consult_block
from .test_consult_service import StubAdapter, StubService

SECRET = "ghp_Zx9Qw3Rt7Yu1Io5Pa8Sd"


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "ctx"
    path.mkdir()
    return path


@pytest.fixture
def build(tmp_path, host_claude):
    async def make(adapter: StubAdapter | None = None, **overrides):
        config = ConsultConfig(
            **consult_block(database_path=str(tmp_path / "consultations.sqlite3"), **overrides)
        )
        return await StubService(config, host_claude, adapter=adapter or StubAdapter()).open()

    return make


async def test_files_reach_the_agent_and_the_record(build, root):
    (root / "a.py").write_text("alpha = 1\n")
    (root / "b.py").write_text("beta = 2\n")
    adapter = StubAdapter()
    service = await build(adapter, context_roots=[str(root)])

    paths = [str(root / "a.py"), str(root / "b.py")]
    response = await service.consult(capability="coding", prompt="read", context_paths=paths)

    assert response.ok
    # Files are material to answer from, so the mode is `document`, not `model`.
    assert response.source_mode_used is SourceMode.DOCUMENT
    (prompt,) = adapter.prompts
    assert f"===== {paths[0]} =====" in prompt and "alpha = 1" in prompt
    assert f"===== {paths[1]} =====" in prompt and "beta = 2" in prompt
    # The stored compiled prompt is the record of what the agent was sent.
    (turn,) = await service.store.turns(response.consultation_id)
    assert "beta = 2" in turn.compiled_prompt


async def test_the_argument_does_not_exist_until_roots_are_configured(build, root):
    (root / "a.py").write_text("alpha = 1\n")
    without = await build()
    with_roots = await build(context_roots=[str(root)])

    assert "context_paths" not in without.request_model.model_json_schema()["properties"]
    assert "context_paths" not in without.many_request_model.model_json_schema()["properties"]
    assert "context_paths" in with_roots.request_model.model_json_schema()["properties"]
    assert "context_paths" in with_roots.many_request_model.model_json_schema()["properties"]

    refused = await without.consult(
        capability="coding", prompt="read", context_paths=[str(root / "a.py")]
    )
    assert refused.error.code is ConsultErrorCode.INVALID_REQUEST


async def test_a_path_outside_the_roots_is_refused_by_name(build, root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    adapter = StubAdapter()
    service = await build(adapter, context_roots=[str(root)])

    response = await service.consult(
        capability="coding", prompt="read", context_paths=[str(outside)]
    )

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "resolves outside `consult.context_roots`" in response.error.message
    assert str(outside) in response.error.message
    assert adapter.prompts == []


async def test_a_symlink_out_of_the_roots_is_refused(build, root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("do-not-send")
    link = root / "link.txt"
    link.symlink_to(outside)
    service = await build(context_roots=[str(root)])

    response = await service.consult(
        capability="coding", prompt="read", context_paths=[str(link)]
    )

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "do-not-send" not in response.error.message


@pytest.mark.parametrize("kind", ["directory", "missing", "fifo"])
async def test_something_that_is_not_a_readable_file_is_refused(build, root, kind):
    target = root / "thing"
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    service = await build(context_roots=[str(root)])

    # A FIFO must be refused, not waited on: this would hang if it were opened for
    # reading without `O_NONBLOCK`.
    response = await service.consult(
        capability="coding", prompt="read", context_paths=[str(target)]
    )

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert str(target) in response.error.message


async def test_a_file_holding_a_credential_is_refused_without_quoting_it(build, root):
    path = root / "settings.py"
    path.write_text(f"DEBUG = True\ntoken = '{SECRET}'\n")
    adapter = StubAdapter()
    service = await build(adapter, context_roots=[str(root)])

    response = await service.consult(
        capability="coding", prompt="read", context_paths=[str(path)]
    )

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert str(path) in response.error.message
    assert "on line 2" in response.error.message
    assert SECRET not in response.error.message
    # Nothing was sent.
    assert adapter.prompts == []


async def test_context_and_context_paths_together_are_refused(build, root):
    path = root / "a.py"
    path.write_text("alpha = 1\n")
    service = await build(context_roots=[str(root)])

    response = await service.consult(
        capability="coding", prompt="read", context="typed", context_paths=[str(path)]
    )

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "not both" in response.error.message


async def test_material_over_the_limit_is_refused(build, root, monkeypatch):
    path = root / "big.txt"
    path.write_text("x" * 100)
    monkeypatch.setattr(service_module, "MAX_CONTEXT_CHARS", 50)
    service = await build(context_roots=[str(root)])

    response = await service.consult(
        capability="coding", prompt="read", context_paths=[str(path)]
    )

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "send fewer or smaller files" in response.error.message


async def test_a_panel_reads_the_files_once_and_asks_every_member_the_same(
    build, root, monkeypatch
):
    path = root / "a.py"
    path.write_text("alpha = 1\n")
    reads: list[list[str]] = []
    real = service_module.read_files

    def counting(paths, *args, **kwargs):
        reads.append(list(paths))
        return real(paths, *args, **kwargs)

    monkeypatch.setattr(service_module, "read_files", counting)
    adapter = StubAdapter()
    service = await build(
        adapter,
        agents={
            "codex-a": agent("codex", "gpt-5.6-sol", 10),
            "codex-b": agent("codex", "gpt-5.6-terra", 20),
        },
        context_roots=[str(root)],
    )

    response = await service.consult_many(
        capability="coding",
        prompt="read",
        target_agents=["codex-a", "codex-b"],
        context_paths=[str(path)],
    )

    assert [r.ok for r in response.results] == [True, True]
    assert reads == [[str(path)]]
    assert len(adapter.prompts) == 2
    assert all("alpha = 1" in prompt for prompt in adapter.prompts)
    assert adapter.prompts[0] == adapter.prompts[1]


async def test_a_panel_with_an_unreadable_file_returns_one_refusal(build, root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    adapter = StubAdapter()
    service = await build(
        adapter,
        agents={
            "codex-a": agent("codex", "gpt-5.6-sol", 10),
            "codex-b": agent("codex", "gpt-5.6-terra", 20),
        },
        context_roots=[str(root)],
    )

    response = await service.consult_many(
        capability="coding",
        prompt="read",
        target_agents=["codex-a", "codex-b"],
        context_paths=[str(outside)],
    )

    (only,) = response.results
    assert only.error.code is ConsultErrorCode.INVALID_REQUEST
    assert adapter.prompts == []


def test_a_context_root_must_be_an_absolute_directory_name():
    with pytest.raises(ValueError, match="must be absolute"):
        ConsultConfig(**consult_block(context_roots=["relative/dir"]))
    with pytest.raises(ValueError, match="filesystem root"):
        ConsultConfig(**consult_block(context_roots=["/"]))
