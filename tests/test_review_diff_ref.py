"""`diff_ref`: a review's material read from git objects, in a real repository.

The point of most of these is what `git` is *not* allowed to do: run a program a
repository's config names, take a flag from a ref, or read outside the directories the
operator listed. Each of those is proved by watching for the side effect, not by
inspecting the argument list.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import orchestrator_mcp.review.diff as diff_module
from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.errors import ConsultErrorCode

from .test_review_service import REVIEWERS, StubAdapter, StubService

SECRET = "ghp_Zx9Qw3Rt7Yu1Io5Pa8Sd"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.strip()


def commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"change {name}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """main: c1 -> c3 (a.py rewritten); topic: c1 -> c2 (adds t.py)."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


@pytest.fixture
def history(repo):
    c1 = commit(repo, "a.py", "one\n")
    git(repo, "checkout", "-q", "-b", "topic")
    c2 = commit(repo, "t.py", "topic\n")
    git(repo, "checkout", "-q", "main")
    c3 = commit(repo, "a.py", "two\n")
    return {"c1": c1, "c2": c2, "c3": c3}


@pytest.fixture
def build(tmp_path, repo):
    async def make(roots: list[Path] | None = None, **overrides):
        adapters = {aid: StubAdapter() for aid in REVIEWERS}
        config = ConsultConfig(
            database_path=str(tmp_path / "c.sqlite3"),
            agents=dict(REVIEWERS),
            review={
                "reviewers": ["codex-sol"],
                "deep_reviewers": list(REVIEWERS),
                "roots": [str(r) for r in (roots if roots is not None else [repo])],
            },
            **overrides,
        )
        return await StubService(config, "claude", adapters=adapters).open()

    return make


async def plan(service, ref, **overrides):
    return await service.plan(goal="review the change", diff_ref=ref, **overrides)


async def test_the_three_forms_read_the_expected_hunks(build, history):
    service = await build()

    merge_base = await plan(service, "main...topic")
    two_dot = await plan(service, "main..topic")
    single = await plan(service, "topic")

    for response in (merge_base, two_dot, single):
        assert response.error is None, response.error
        assert response.plan.material_verified is True
    # `...` diffs against the merge base, so main's own rewrite of a.py is not in it.
    assert merge_base.plan.context_chars == single.plan.context_chars
    assert two_dot.plan.context_chars > merge_base.plan.context_chars

    item = merge_base.plan.material[0]
    assert (item.label, item.kind) == ("git diff main...topic", "text")
    # The two tips are what is pinned; the merge base is git's to compute from them.
    assert item.locator == f"{history['c3'][:12]}...{history['c2'][:12]}"
    assert single.plan.material[0].locator == f"{history['c1'][:12]}..{history['c2'][:12]}"


async def test_the_reviewer_gets_the_diff_and_no_repository_path(build, repo, history):
    service = await build()

    response = await plan(service, "main...topic")
    run = await service.run(response.review_id, response.plan.confirm_token)

    assert run.status == "awaiting_synthesis"
    sent = next(iter(service.adapters.values())).prompts[0]
    assert "+++ b/t.py" in sent and "+topic" in sent
    assert "a.py" not in sent
    assert str(repo) not in sent


@pytest.mark.parametrize(
    "ref",
    ["--output=OUT", "main..--output=OUT", "-p", "main -p", "main\n-p", "main..", "..topic", "a..b..c"],
)
async def test_a_hostile_or_malformed_ref_is_refused_before_git_sees_it(
    build, tmp_path, history, ref
):
    out = tmp_path / "out.txt"
    service = await build()

    response = await plan(service, ref.replace("OUT", str(out)))

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert not out.exists()


async def test_a_programs_named_by_the_repo_config_are_not_run(build, repo, history, tmp_path):
    marker = tmp_path / "marker"
    script = tmp_path / "drv.sh"
    script.write_text(f'#!/bin/sh\necho ran >> "{marker}"\ncat "$1" 2>/dev/null\n')
    script.chmod(0o755)
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attributes")
    git(repo, "config", "diff.external", str(script))
    git(repo, "config", "diff.evil.textconv", str(script))

    # The trap is live: a plain `git diff` would run it. Without this, the assertion
    # below would pass just as well if the script were broken.
    subprocess.run(["git", "diff", "HEAD~1", "HEAD~2"], cwd=repo, capture_output=True)
    subprocess.run(["git", "diff", "topic", "main"], cwd=repo, capture_output=True)
    assert marker.exists()
    marker.unlink()

    service = await build()
    response = await plan(service, "main..topic")

    assert response.error is None, response.error
    assert not marker.exists()


async def test_a_repository_outside_the_roots_is_refused(build, tmp_path, repo, history):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    service = await build(roots=[elsewhere])

    response = await plan(service, "main..topic", diff_repo=str(repo))

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "consult.review.roots" in response.error.message


async def test_a_symlink_to_a_repository_outside_the_roots_is_refused(
    build, tmp_path, repo, history
):
    roots = tmp_path / "roots"
    roots.mkdir()
    (roots / "link").symlink_to(repo)
    service = await build(roots=[roots])

    response = await plan(service, "main..topic", diff_repo=str(roots / "link"))

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST


async def test_a_subdirectory_of_a_repository_does_not_open_the_whole_repository(
    build, repo, history
):
    sub = repo / "sub"
    sub.mkdir()
    service = await build(roots=[sub])

    response = await plan(service, "main..topic")

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert str(repo) in response.error.message and "outside" in response.error.message


async def test_a_directory_that_is_not_a_repository_is_refused(build, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    service = await build(roots=[plain])

    response = await plan(service, "main..topic")

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "not inside a git repository" in response.error.message


@pytest.mark.parametrize(
    "ref, needle",
    [
        ("HEAD:a.py", "not a commit"),
        ("HEAD^{tree}", "not a commit"),
        ("no-such-branch", "not a commit"),
        ("main..no-such-branch", "not a commit"),
        ("main..main", "no changes"),
    ],
)
async def test_something_that_is_not_a_committed_change_is_refused(build, history, ref, needle):
    service = await build()

    response = await plan(service, ref)

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert needle in response.error.message


async def test_the_root_commit_has_no_parent_to_diff_against(build, history):
    service = await build()

    response = await plan(service, history["c1"])

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "no parent commit" in response.error.message


async def test_uncommitted_changes_are_not_in_the_diff(build, repo, history):
    (repo / "a.py").write_text("uncommitted-edit\n")
    service = await build()

    response = await plan(service, "main...topic")
    run = await service.run(response.review_id, response.plan.confirm_token)

    assert run.status == "awaiting_synthesis"
    assert "uncommitted-edit" not in next(iter(service.adapters.values())).prompts[0]


async def test_diff_ref_is_exclusive_with_the_other_sources(build, history, tmp_path):
    path = tmp_path / "repo" / "a.py"
    service = await build()

    with_context = await plan(service, "main..topic", context="typed")
    with_paths = await plan(service, "main..topic", context_paths=[str(path)])
    repo_alone = await service.plan(goal="review", diff_repo=str(tmp_path / "repo"))

    for response in (with_context, with_paths, repo_alone):
        assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "not a mix" in with_context.error.message
    assert "only means something with `diff_ref`" in repo_alone.error.message


async def test_the_only_root_is_the_default_repo_and_several_need_a_choice(
    build, tmp_path, repo, history
):
    other = tmp_path / "other"
    other.mkdir()

    single = await plan(await build(), "main..topic")
    several = await plan(await build(roots=[repo, other]), "main..topic")
    named = await plan(await build(roots=[repo, other]), "main..topic", diff_repo=str(repo))
    none = await plan(await build(roots=[]), "main..topic")

    assert single.error is None and named.error is None
    assert "`diff_repo` is required" in several.error.message
    assert "disabled until `consult.review.roots:`" in none.error.message


async def test_moving_the_branch_changes_what_the_approval_covers(build, repo, history):
    service = await build()

    first = await plan(service, "main..topic")
    again = await plan(service, "main..topic")
    git(repo, "checkout", "-q", "topic")
    commit(repo, "t.py", "topic, moved\n")
    git(repo, "checkout", "-q", "main")
    moved = await plan(service, "main..topic")

    assert first.plan.material_sha256 == again.plan.material_sha256
    assert moved.plan.material_sha256 != first.plan.material_sha256
    assert moved.plan.material[0].locator != first.plan.material[0].locator


async def test_a_range_over_the_limit_is_refused(build, history, monkeypatch):
    monkeypatch.setattr(diff_module, "MAX_CONTEXT_CHARS", 10)
    service = await build()

    response = await plan(service, "main..topic")

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "narrow the range" in response.error.message


async def test_a_credential_in_the_diff_shows_up_in_the_preview(build, repo, history):
    git(repo, "checkout", "-q", "topic")
    commit(repo, "settings.py", f"TOKEN = '{SECRET}'\n")
    git(repo, "checkout", "-q", "main")
    service = await build()

    response = await plan(service, "main...topic")

    assert response.error is None, response.error
    assert [h.field for h in response.plan.secret_hits] == ["context"]
    assert SECRET not in response.model_dump_json()


def promisor_clone(tmp_path: Path, upstream: Path, name: str, marker: Path) -> Path:
    """A blobless clone whose remote program leaves `marker` behind whenever it is run."""
    script = tmp_path / "upload.sh"
    script.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexec git upload-pack "$@"\n')
    script.chmod(0o755)
    clone = tmp_path / name
    git(tmp_path, "clone", "-q", "--filter=blob:none", "--no-checkout", f"file://{upstream}", str(clone))
    git(clone, "config", "remote.origin.uploadpack", str(script))
    return clone


async def test_a_partial_clone_does_not_run_its_remote_to_fetch_a_blob(build, repo, history, tmp_path):
    git(repo, "config", "uploadpack.allowFilter", "true")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    marker = tmp_path / "marker"

    # The trap is live: plain `git diff` in a blobless clone runs the remote's program.
    trap = promisor_clone(tmp_path, repo, "trap", marker)
    subprocess.run(["git", "diff", "HEAD~1", "HEAD"], cwd=trap, capture_output=True)
    assert marker.exists()
    marker.unlink()

    clone = promisor_clone(tmp_path, repo, "clone", marker)
    service = await build(roots=[clone])
    response = await plan(service, "HEAD~1..HEAD")

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert not marker.exists()


async def test_replacement_refs_do_not_change_what_the_pinned_shas_diff(build, repo, history):
    git(repo, "checkout", "-q", "-b", "other", "main")
    fake = commit(repo, "t.py", "REPLACED\n")
    git(repo, "checkout", "-q", "main")
    git(repo, "replace", history["c2"], fake)

    # The trap is live: with replacement objects on, the topic tip is not what it was.
    assert "REPLACED" in git(repo, "diff", "main..topic")

    service = await build()
    response = await plan(service, "main..topic")
    run = await service.run(response.review_id, response.plan.confirm_token)

    sent = next(iter(service.adapters.values())).prompts[0]
    assert run.status == "awaiting_synthesis"
    assert "+topic" in sent and "REPLACED" not in sent


async def test_a_worktree_whose_git_data_is_outside_the_roots_is_refused(
    build, tmp_path, repo, history
):
    roots = tmp_path / "roots"
    roots.mkdir()
    git(repo, "worktree", "add", "-q", str(roots / "wt"), "topic")
    service = await build(roots=[roots])

    response = await plan(service, "HEAD~1..HEAD", diff_repo=str(roots / "wt"))

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "keeps git data" in response.error.message


async def test_a_clone_borrowing_objects_from_outside_the_roots_is_refused(
    build, tmp_path, repo, history
):
    roots = tmp_path / "roots"
    roots.mkdir()
    git(tmp_path, "clone", "-q", "--shared", str(repo), str(roots / "shared"))
    service = await build(roots=[roots])

    response = await plan(service, "HEAD~1..HEAD", diff_repo=str(roots / "shared"))

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "keeps git data" in response.error.message


async def test_a_worktree_whose_main_repository_is_inside_the_roots_is_accepted(
    build, tmp_path, repo, history
):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(linked), "topic")
    service = await build(roots=[tmp_path])

    response = await plan(service, "main..topic", diff_repo=str(linked))

    assert response.error is None, response.error


async def test_an_empty_ref_is_refused_by_the_ref_check(build, history):
    service = await build()

    response = await plan(service, "")

    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert "`diff_ref` must be 1 to" in response.error.message


async def test_a_host_manifest_keeps_the_pinned_endpoints_in_the_plan(build, history):
    service = await build()
    note = {"label": "the topic branch", "kind": "text"}

    both = await plan(service, "main..topic", material=[note])
    alone = await plan(service, "main..topic")

    assert [m.label for m in both.plan.material] == ["git diff main..topic", "the topic branch"]
    assert both.plan.material[0].locator == alone.plan.material[0].locator
    assert both.plan.material_verified is False and alone.plan.material_verified is True
