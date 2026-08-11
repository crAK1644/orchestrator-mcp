"""The one write path: a contained run in a throwaway worktree.

Everything here is offline, against a stub `codex` on PATH. What a stub cannot prove
is the containment itself -- no test in this file demonstrates that the sandbox holds,
because the sandbox is the CLI's and the only honest check of it was the live spike
that ran a command aimed outside the worktree and got `Operation not permitted` back.

What these tests do pin is the part that is ours: the flags that ask for the sandbox,
the worktree being made from the baseline and removed afterwards, the capture seeing
files the model created rather than only ones it edited, and the fact that the record
of a step comes from git and not from anything the model said.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from orchestrator_mcp.code import service as code_service
from orchestrator_mcp.code.adapters.codex_cli import CodexWriteAdapter
from orchestrator_mcp.code.registry import CodeError, code_adapter_for
from orchestrator_mcp.code.service import run_contained, worktree_path
from orchestrator_mcp.consult.config import AgentConfig, ConsultConfig
from orchestrator_mcp.consult.prompts import compile_execution_prompt

from .fixtures import agent_stub

THREAD = "thread_01JWRITE"


def jsonl(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def transcript(summary: str = "Added greet.py and extended app.py.", **extra) -> str:
    return jsonl(
        {"type": "thread.started", "thread_id": THREAD, "model": "gpt-5.6-sol"},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": ["pytest", "-q"],
                "exit_code": 0,
                "aggregated_output": "1 passed",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "file_change", "changes": {"greet.py": {"kind": "add"}}},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": summary}},
        {"type": "turn.completed", "usage": {"input_tokens": 400, "output_tokens": 90}} | extra,
    )


def agent(runtime: str = "codex", **overrides) -> AgentConfig:
    return AgentConfig(
        agent_id=f"{runtime}-writer",
        runtime=runtime,  # type: ignore[arg-type]
        command=runtime,
        model="gpt-5.6-sol",
        scores={"coding": 95},
        execution_modes=["consultation", "patch", "isolated_write"],
        **overrides,
    )


def config() -> ConsultConfig:
    return ConsultConfig(agents={"codex-writer": agent()}, timeout_s=30)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real git repository with one commit. Nothing here is mocked, because what is
    being tested is what git actually reports about a worktree."""
    import subprocess

    path = tmp_path / "project"
    path.mkdir()
    (path / "app.py").write_text("def main():\n    return 1\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "test"],
        ["add", "-A"],
        ["commit", "-qm", "first"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture
def worktrees() -> Path:
    """Where `_no_real_worktrees` in conftest sent them: under the test's own tmp_path.

    Named as a fixture anyway, so a test that reaches the code service says in its
    signature that it does.
    """
    return code_service.WORKTREE_ROOT


async def baseline_of(repo: Path) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


async def run(repo: Path, tmp_path, monkeypatch, *, runs=None, step_id="step-1"):
    record = agent_stub.install("codex", tmp_path, monkeypatch, runs=runs or [{
        "stdout": transcript(),
        "append": {"greet.py": "def greet():\n    return 'hi'\n", "app.py": "# touched\n"},
    }])
    result = await run_contained(
        config(),
        agent(),
        compile_execution_prompt("add a greeting", None),
        repo=repo,
        baseline_commit=await baseline_of(repo),
        workflow_id="wf-1",
        step_id=step_id,
        timeout_s=30.0,
    )
    return result, record


# --- the registry -----------------------------------------------------------


def test_only_codex_has_a_write_adapter():
    assert isinstance(code_adapter_for(agent("codex"), config()), CodexWriteAdapter)


def test_an_unknown_runtime_is_refused_rather_than_defaulted():
    class Odd:
        runtime = "somethingelse"

    with pytest.raises(CodeError, match="not a runtime this installation implements"):
        code_adapter_for(Odd(), config())


# --- the sandbox flags ------------------------------------------------------


async def test_the_run_asks_for_the_sandbox_it_was_promised(repo, worktrees, tmp_path, monkeypatch):
    _, record = await run(repo, tmp_path, monkeypatch)
    argv = agent_stub.calls(record)[0]["argv"]
    flags = " ".join(argv)
    assert 'sandbox_mode="workspace-write"' in flags
    assert 'approval_policy="never"' in flags
    assert "sandbox_workspace_write.network_access=false" in flags
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in flags
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in flags
    assert "web_search=disabled" in flags
    assert "agents.enabled=false" in flags
    # On, unlike a consultation: the step is expected to run its own tests.
    assert "features.shell_tool=true" in flags
    # The directory it is confined to is the worktree, never the user's repository.
    assert argv[argv.index("-C") + 1] == str(worktree_path("wf-1", "step-1"))
    assert str(repo) not in flags


async def test_the_prompt_travels_on_stdin_with_the_write_contract_first(
    repo, worktrees, tmp_path, monkeypatch
):
    _, record = await run(repo, tmp_path, monkeypatch)
    call = agent_stub.calls(record)[0]
    assert call["argv"][-1] == "-"
    assert call["stdin"].startswith("You are an execution endpoint")
    assert "add a greeting" in call["stdin"]
    # The read-only contract must not travel with a step whose job is to write.
    assert "Do not edit files" not in call["stdin"]


async def test_no_api_key_reaches_a_writing_child(repo, worktrees, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-travel")
    _, record = await run(repo, tmp_path, monkeypatch)
    assert "ANTHROPIC_API_KEY" not in agent_stub.calls(record)[0]["env"]


# --- the worktree -----------------------------------------------------------


async def test_the_agent_never_works_in_the_users_repository(repo, worktrees, tmp_path, monkeypatch):
    result, record = await run(repo, tmp_path, monkeypatch)
    assert result.changed
    # The files the stub wrote exist only in the diff: the real tree is untouched.
    assert not (repo / "greet.py").exists()
    assert (repo / "app.py").read_text() == "def main():\n    return 1\n"


async def test_the_worktree_is_removed_and_deregistered(repo, worktrees, tmp_path, monkeypatch):
    import subprocess

    await run(repo, tmp_path, monkeypatch)
    assert not worktree_path("wf-1", "step-1").exists()
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert "wf-1" not in listed.stdout


async def test_a_finished_workflow_leaves_no_directory_of_its_own(
    repo, worktrees, tmp_path, monkeypatch
):
    """Found by the live run: the step's worktree went, its parent stayed forever.

    Nothing but this code creates that directory, so nothing but this code removes it,
    and one empty directory per workflow accumulates under the user's home.
    """
    await run(repo, tmp_path, monkeypatch)

    assert not worktree_path("wf-1", "step-1").parent.exists()


async def test_a_sibling_step_still_running_keeps_the_directory(
    repo, worktrees, tmp_path, monkeypatch
):
    """`rmdir` and not a recursive delete: a fix round can be live while this one ends."""
    sibling = worktree_path("wf-1", "step-2")
    sibling.mkdir(parents=True)

    await run(repo, tmp_path, monkeypatch)

    assert sibling.exists()


async def test_the_worktree_directories_are_private_to_this_user(
    repo, worktrees, tmp_path, monkeypatch
):
    """`mkdir` honours the umask, so a lax one published the user's checkout.

    A worktree is their repository plus, between the run and the capture, the only
    copy of the step's work -- readable by every local account under a 0o755 root, and
    a file in it swappable underneath the diff.
    """
    worktrees.mkdir(parents=True)
    worktrees.chmod(0o755)

    await run(repo, tmp_path, monkeypatch)

    # Tightened rather than refused: it is ours, and refusing would strand a server
    # over a directory it created itself under a different umask.
    assert stat.S_IMODE(worktrees.stat().st_mode) == 0o700


async def test_a_symlink_standing_in_for_a_worktree_directory_is_refused(
    repo, worktrees, tmp_path, monkeypatch
):
    """`lstat`, so the link fails here rather than being followed somewhere writable."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    worktrees.mkdir(parents=True)
    (worktrees / "wf-1").symlink_to(elsewhere)

    with pytest.raises(CodeError, match="not a directory this user owns"):
        await run(repo, tmp_path, monkeypatch)


@pytest.mark.parametrize("kind", ["file", "dangling"])
async def test_something_that_is_not_a_directory_is_refused_by_name(
    repo, worktrees, tmp_path, monkeypatch, kind
):
    """`mkdir(exist_ok=True)` raised past the check for anything `is_dir()` denies.

    A symlink to a real directory reached the refusal; a plain file or a link to
    nothing raised `FileExistsError` out of `mkdir` first and arrived as a transport
    failure carrying an exception string, which does not say what to remove.
    """
    worktrees.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        worktrees.write_text("not a directory")
    else:
        worktrees.symlink_to(tmp_path / "nothing-here")

    with pytest.raises(CodeError, match="not a directory this user owns"):
        await run(repo, tmp_path, monkeypatch)


async def test_something_arriving_between_the_check_and_the_mkdir_is_refused_the_same_way(
    repo, worktrees, tmp_path, monkeypatch
):
    """The loser of the race got the exception, not the refusal.

    `lstat` first closed the ordinary case, but between it and the `mkdir` another
    process can put a plain file at the same path -- and `mkdir(exist_ok=True)` raises
    on that, so the check underneath was never reached. Staged here by making the first
    `lstat` report the file as missing, which is what losing the race looks like.
    """
    worktrees.parent.mkdir(parents=True, exist_ok=True)
    worktrees.write_text("planted by whoever got there first")
    real, missed = Path.lstat, set()

    def lstat(self):
        if self == worktrees and self not in missed:
            missed.add(self)
            raise FileNotFoundError(self)
        return real(self)

    monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(CodeError, match="not a directory this user owns"):
        await run(repo, tmp_path, monkeypatch)


async def test_two_steps_of_one_workflow_get_separate_directories():
    assert worktree_path("wf-1", "step-1") != worktree_path("wf-1", "step-2")
    assert worktree_path("wf-1", "step-1") != worktree_path("wf-2", "step-1")


async def test_a_leftover_directory_is_refused_rather_than_reused(
    repo, worktrees, tmp_path, monkeypatch
):
    """Two runs mixed into one diff would attribute one step's work to another."""
    stale = worktree_path("wf-1", "step-1")
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("from a crashed attempt")
    with pytest.raises(CodeError, match="already exists"):
        await run(repo, tmp_path, monkeypatch)


# --- what comes back --------------------------------------------------------


async def test_the_capture_sees_files_the_model_created(repo, worktrees, tmp_path, monkeypatch):
    """`git diff <baseline>..` alone would report nothing here: the new file is
    untracked, and most steps create at least one."""
    result, _ = await run(repo, tmp_path, monkeypatch)
    assert "greet.py" in result.files
    assert "app.py" in result.files
    assert "+def greet():" in result.patch
    assert result.baseline_commit == await baseline_of(repo)


async def test_a_run_that_changed_nothing_says_so(repo, worktrees, tmp_path, monkeypatch):
    result, _ = await run(
        repo, tmp_path, monkeypatch,
        runs=[{"stdout": transcript("I looked at it and decided nothing was needed.")}],
    )
    assert not result.changed
    assert result.patch.strip() == ""
    assert result.files == []


async def test_the_summary_is_the_models_word_and_the_patch_is_not(
    repo, worktrees, tmp_path, monkeypatch
):
    """A model that claims to have written a file it did not write changes the
    summary and nothing else."""
    result, _ = await run(
        repo, tmp_path, monkeypatch,
        runs=[{
            "stdout": transcript("I rewrote the entire authentication layer."),
            "append": {"greet.py": "x = 1\n"},
        }],
    )
    assert result.summary == "I rewrote the entire authentication layer."
    assert result.files == ["greet.py"]


async def test_commands_are_reported_as_a_partial_account(repo, worktrees, tmp_path, monkeypatch):
    result, _ = await run(repo, tmp_path, monkeypatch)
    assert [c.command for c in result.commands] == ["pytest -q"]
    assert result.commands[0].exit_code == 0
    assert result.usage.total_tokens == 490


async def test_a_failed_turn_is_not_captured_as_work(repo, worktrees, tmp_path, monkeypatch):
    """Raised before git is read, so a half-finished edit never becomes a step's
    output -- and the worktree is still removed."""
    with pytest.raises(CodeError, match="failed turn"):
        await run(
            repo, tmp_path, monkeypatch,
            runs=[{
                "stdout": jsonl(
                    {"type": "thread.started", "thread_id": THREAD},
                    {"type": "turn.failed", "error": "the model gave up"},
                ),
                "append": {"half.py": "incomplete\n"},
            }],
        )
    assert not worktree_path("wf-1", "step-1").exists()


async def test_a_nonzero_exit_is_a_failure_however_the_tree_looks(
    repo, worktrees, tmp_path, monkeypatch
):
    with pytest.raises(CodeError, match="exited 1"):
        await run(
            repo, tmp_path, monkeypatch,
            runs=[{"stdout": transcript(), "stderr": "killed", "returncode": 1}],
        )


async def test_the_network_coming_back_on_is_noticed(repo, worktrees, tmp_path, monkeypatch):
    """`network_access=false` is what prevents it; this is what says so if that key
    ever stops meaning what it means."""
    with pytest.raises(CodeError, match="reached the network"):
        await run(
            repo, tmp_path, monkeypatch,
            runs=[{
                "stdout": jsonl(
                    {"type": "thread.started", "thread_id": THREAD},
                    {"type": "item.completed", "item": {"type": "web_search", "query": "how to"}},
                ),
            }],
        )


async def test_a_repository_created_inside_the_worktree_does_not_destroy_the_step(
    repo, worktrees, tmp_path, monkeypatch
):
    """One nested repository makes `git add -A` refuse the whole tree, exit 128.

    Found by a review, then reproduced: capture raised, the `finally` force-removed the
    worktree, and every file the step had written went with it -- over an ordinary
    scaffolded subproject, not an attack. The worktree now survives a capture failure,
    because at that moment it is the only copy of the work.
    """
    with pytest.raises(CodeError, match="only copy of this step's work"):
        await run(
            repo, tmp_path, monkeypatch,
            runs=[{
                "stdout": transcript(),
                "append": {
                    "greet.py": "def greet():\n    return 'hi'\n",
                    # What git looks for before it calls a directory a repository. A
                    # real `git init` in a subproject leaves all three and more.
                    "sub/.git/HEAD": "ref: refs/heads/main\n",
                    "sub/.git/objects/.keep": "",
                    "sub/.git/refs/.keep": "",
                    "sub/app.py": "print('hi')\n",
                },
            }],
        )

    kept = worktree_path("wf-1", "step-1")
    assert (kept / "greet.py").exists()
    assert (kept / "sub" / "app.py").exists()


async def test_a_file_the_patch_cannot_carry_comes_back_named(
    repo, worktrees, tmp_path, monkeypatch
):
    """`git add -A` skips ignored paths by design, and no diff can carry them.

    So the step wrote a file, the patch says it did not, and the worktree that held it
    is gone. Nothing here recovers the file -- what changed is that the result says
    which one, instead of the omission being invisible.
    """
    result, _ = await run(
        repo, tmp_path, monkeypatch,
        runs=[{
            "stdout": transcript(),
            "append": {
                ".gitignore": "build/\n",
                "build/out.txt": "generated\n",
                "greet.py": "def greet():\n    return 'hi'\n",
            },
        }],
    )

    assert result.ignored == ["build/out.txt"]
    assert "build/out.txt" not in result.patch
    assert sorted(result.files) == [".gitignore", "greet.py"]


async def test_capture_reads_the_git_directory_it_pinned_not_the_one_left_behind(
    repo, worktrees, tmp_path, monkeypatch
):
    """A worktree's `.git` is a one-line file naming its git directory.

    Capture used to rediscover the repository by reading that line -- after the agent
    had spent the whole step writing into the directory it sits in. Pointed somewhere
    else it is a file the step controls that decides what the step's diff is of. The
    gitdir is now read at worktree creation, before the agent starts, and passed
    explicitly, so what is written here is simply ignored.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=decoy, check=True)

    result, _ = await run(
        repo, tmp_path, monkeypatch,
        runs=[{
            "stdout": transcript(),
            "append": {"greet.py": "def greet():\n    return 'hi'\n"},
            "write": {".git": f"gitdir: {decoy / '.git'}\n"},
        }],
    )

    assert "+def greet():" in result.patch
    assert result.files == ["greet.py"]


async def test_a_substituted_model_is_refused(repo, worktrees, tmp_path, monkeypatch):
    with pytest.raises(CodeError, match="the answer came from `gpt-4o-mini`"):
        await run(
            repo, tmp_path, monkeypatch,
            runs=[{
                "stdout": jsonl(
                    {"type": "thread.started", "thread_id": THREAD, "model": "gpt-4o-mini"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                ),
            }],
        )
