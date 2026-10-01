# Privacy policy

Effective 2026-09-25. Applies to `orchestrator-mcp-server` and to the `orchestrator` Claude Code plugin that runs it.

Orchestrator is software that runs on your machine. The author runs no server for it. It has no telemetry, no analytics, no crash reporting and no accounts, so nothing you do with it reaches the author.

## What leaves your machine

Orchestrator's reach beyond the machine goes only through the agent CLIs you have installed and logged into:

- Codex (OpenAI)
- Claude Code (Anthropic)
- Antigravity (Google)
- OpenCode, for the provider behind the model you configure
- GitHub Copilot (GitHub, and the model provider behind the model it picks)

It does not contact any of them directly, and it holds no API key.

When you consult an agent, run a review or run a workflow step, Orchestrator hands that CLI the material for the call. That material is the question, and any context, file contents or diff that you or your assistant supply. The CLI sends it to its provider under your account. From there the provider's own terms and privacy policy apply, and so does its billing. With web access requested, which is off unless asked for, the agent may also search the web.

Plugin installs run with Claude Code as the host, so they never send anything to Claude Code as an agent. The server leaves out the host's own runtime by construction.

Credential-shaped values are masked on a best-effort basis, but when masking happens depends on the path:

- **Reviews** send the masked copy unless you choose `secrets="send_as_is"`.
- **Workflow steps** are masked before they are stored and before they are sent.
- **Ordinary consultations** send your material as given. Only the stored copy is masked.

Pattern matching can miss a secret that has no recognizable shape, so do not rely on it to catch one. A review's preview also lists lines holding a long random-looking token as `suspect_hits`, a guess that only warns.

Each agent CLI also keeps its own history, for example `~/.codex/sessions/`. Orchestrator cannot redact or delete those files. Use the vendor's own tools for them. GitHub Copilot's history sits under Orchestrator's own directory, in `~/.orchestrator-mcp/copilot/home`, with the prompts and answers unmasked. Orchestrator removes a consultation's session directory from it when it deletes the consultation, but not the copy of each prompt and answer in the CLI's own store, `session-store.db`; [Retention and deletion](#retention-and-deletion) says what stays.

The GitHub Copilot CLI also sends its own telemetry to GitHub: GitHub's help for `COPILOT_OFFLINE` lists telemetry among the network access that setting turns off. Orchestrator does not turn it off and does not see what it carries.

## What is stored on your machine

- **`~/.orchestrator-mcp/consultations.sqlite3`**, or wherever `consult.database_path` points. It holds:
  - consultations: prompts, answers, usage, and why an agent was chosen;
  - reviews: plans, reviewer results, your synthesis, and fix rounds;
  - workflows: steps, artifacts and test reports.

  Every stored copy has credential-shaped values masked. `store_full_content: false` keeps metadata only, except a review's goal and context, which are stored either way because the approved plan is read back to send it. Workflows need full content.
- **Configuration**: `~/.orchestrator-mcp/config.yaml`, plus `~/.orchestrator-mcp/agents.yaml` if you use the dashboard editor.
- **OpenCode working directories**: `~/.orchestrator-mcp/opencode/<agent>`, which hold only the configuration Orchestrator writes for that runtime.
- **GitHub Copilot's state**: `~/.orchestrator-mcp/copilot`.
  - `home` is the `COPILOT_HOME` Copilot runs under: its configuration, logs and session store (`session-store.db`), one session directory for each consultation, and, on a host with no system keychain, the sign-in token where GitHub's documentation puts it. The session directories and the store each hold the prompts and answers as sent, unmasked. Deleting a consultation removes its session directory and the lock beside it, and nothing else in `home`, so the store's copy stays.
  - Each run also gets a scratch directory beside it, empty, which is deleted when the run ends.

  Both are mode `0700`. Your own `~/.copilot` is neither read nor written by Orchestrator. Put nothing in `home` but the sign-in. Orchestrator writes one setting there, `disableAllHooks` in `settings.json` (mode `0600`, keeping whatever else that file holds), so that a hook does not run on a consultation, and it refuses to run over a `settings.json` it cannot read as a JSON object. An MCP server that `mcp-config.json` lists is switched off for each run, by name. Orchestrator neither looks for plugins nor limits them.
- **`isolated_write` workflow steps** use `~/.orchestrator-mcp/worktrees` for two things:
  - **A throwaway worktree for each step.** It is removed when the step ends. The exception is a worktree whose diff could not be captured, which is kept as the only copy of that work.
  - **A private copy of each step's raw patch**, mode `0600`, kept so a lost response can be recovered. Unlike the database, this copy is not masked, because a masked patch does not apply.

  The server deletes both once they are more than 7 days old.

The database directory is mode `0700`. The database and the managed agents file are `0600`.

Logs go to standard error, which your MCP client captures. Orchestrator writes no log file of its own.

The optional dashboard is off by default. When it is on, it listens on loopback only.

## What it reads

Orchestrator reads its own configuration and database, plus:

- the files you name in `context_paths`, which must sit under a configured `review.roots` entry (for `orchestrator_consult`, a `context_roots` entry);
- the committed diff you name in a review's `diff_ref`, read by running `git diff` in a repository under a configured `review.roots` entry. Both ends must be commits: it reads git objects, not your working tree or index, and runs no program the repository's config names. The repository's git data, including any alternates, must sit under those roots too;
- a workflow's `workdir`, which must be a git repository under a configured `workflow.roots` entry;
- two fields from agent CLI history, both to identify the model that answered:
  - from the Codex session it just ran, the model, plus Codex's latest rate-limit figures;
  - from `opencode export` of the OpenCode session it just ran, the model;
- nothing from GitHub Copilot's history: the model that answered comes from the output of the run itself, the turn's token counts from a usage file that run writes into its scratch directory, and a session it resumes is checked for and not opened. It also opens `mcp-config.json` in Copilot's `home` and uses only the names of the servers listed, to switch each off; nothing else in the file is kept or sent on.

It does not read your assistant's conversation history or memory. What it knows of a conversation is what arrives as tool arguments.

## Retention and deletion

The `isolated_write` files above expire after 7 days. Database records stay until you delete them, unless you set `retention_days` in the `consult:` block: then, at each server start and roughly daily while it runs, finished consultations, reviews and workflows with no activity for that many days are deleted. Delete them yourself with these tools:

- `orchestrator_delete_consultation`
- `orchestrator_delete_review`
- `orchestrator_delete_workflow`

For bulk deletion, `orchestrator_request_delete_all_consultations`, `orchestrator_request_delete_all` and `orchestrator_request_delete_all_workflows` each preview a delete and return a token, which you pass to the matching `delete_all` tool. To remove everything at once, delete the database file.

A deleted consultation's GitHub Copilot session directory goes with it, however it was deleted: by one of these tools, with the review or workflow that owned it, or by `retention_days`. That is its directory under `~/.orchestrator-mcp/copilot/home` and the lock beside it, removed once the delete has committed. One that cannot be removed is left, and the delete still succeeds. Nothing else in `home` is touched:

- `session-store.db`, the CLI's own store, which keeps a second copy of each prompt and answer, unmasked. The copy was still there after a delete when checked on CLI 1.0.89. Orchestrator does not open the file, and `retention_days` does not reach it;
- what else the CLI keeps there, which Orchestrator does not open either;
- a session whose consultation is not in the database, such as one left by a consultation deleted before this release, or by deleting the database file.

Delete `home` to clear all of it. On a host with no keychain that takes the sign-in with it, and a consultation that used it can then no longer be continued.

The `usage`, `history`, `scorecard`, `strengths`, `search` and `export` commands of `orchestrator-mcp-server` read that database from a terminal and print it, `export` prints the prompts and answers of one record, and `search` prints short excerpts of the ones that hold the words you give it. The `orchestrator_search_consultations` tool returns the same excerpts to the host model, so old prompts and answers can reach it through a search as they can through `orchestrator_get_consultation`; it only reads, and keeps no index of its own. Stored text is masked as it is printed, which also covers records written before a pattern existed, but masking is best effort: treat the output like the database itself.

Uninstalling the plugin leaves `~/.orchestrator-mcp/` in place. Delete that directory to remove all of Orchestrator's data.

## Changes and contact

Changes to this policy are made in this file, and its history on GitHub is the changelog. For questions, open an issue at https://github.com/crAK1644/orchestrator-mcp/issues. For security problems, see [SECURITY.md](SECURITY.md).
