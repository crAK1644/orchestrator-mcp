# Privacy policy

Effective 2026-09-25. Applies to `orchestrator-mcp-server` and to the `orchestrator` Claude Code plugin that runs it.

Orchestrator is software that runs on your machine. The author runs no server for it. It has no telemetry, no analytics, no crash reporting and no accounts, so nothing you do with it reaches the author.

## What leaves your machine

Orchestrator's reach beyond the machine goes only through the agent CLIs you have installed and logged into:

- Codex (OpenAI)
- Claude Code (Anthropic)
- Antigravity (Google)
- OpenCode, for the provider behind the model you configure

It does not contact any of them directly, and it holds no API key.

When you consult an agent, run a review or run a workflow step, Orchestrator hands that CLI the material for the call. That material is the question, and any context, file contents or diff that you or your assistant supply. The CLI sends it to its provider under your account. From there the provider's own terms and privacy policy apply, and so does its billing. With web access requested, which is off unless asked for, the agent may also search the web.

Plugin installs run with Claude Code as the host, so they never send anything to Claude Code as an agent. The server leaves out the host's own runtime by construction.

Credential-shaped values are masked on a best-effort basis, but when masking happens depends on the path:

- **Reviews** send the masked copy unless you choose `secrets="send_as_is"`.
- **Workflow steps** are masked before they are stored and before they are sent.
- **Ordinary consultations** send your material as given. Only the stored copy is masked.

Pattern matching can miss a secret that has no recognizable shape, so do not rely on it to catch one.

Each agent CLI also keeps its own history, for example `~/.codex/sessions/`. Orchestrator cannot redact or delete those files. Use the vendor's own tools for them.

## What is stored on your machine

- **`~/.orchestrator-mcp/consultations.sqlite3`**, or wherever `consult.database_path` points. It holds:
  - consultations: prompts, answers, usage, and why an agent was chosen;
  - reviews: plans, reviewer results, your synthesis, and fix rounds;
  - workflows: steps, artifacts and test reports.

  Every stored copy has credential-shaped values masked. `store_full_content: false` keeps metadata only, but workflows need full content.
- **Configuration**: `~/.orchestrator-mcp/config.yaml`, plus `~/.orchestrator-mcp/agents.yaml` if you use the dashboard editor.
- **OpenCode working directories**: `~/.orchestrator-mcp/opencode/<agent>`, which hold only the configuration Orchestrator writes for that runtime.
- **`isolated_write` workflow steps** use `~/.orchestrator-mcp/worktrees` for two things:
  - **A throwaway worktree for each step.** It is removed when the step ends. The exception is a worktree whose diff could not be captured, which is kept as the only copy of that work.
  - **A private copy of each step's raw patch**, mode `0600`, kept so a lost response can be recovered. Unlike the database, this copy is not masked, because a masked patch does not apply.

  The server deletes both once they are more than 7 days old.

The database directory is mode `0700`. The database and the managed agents file are `0600`.

Logs go to standard error, which your MCP client captures. Orchestrator writes no log file of its own.

The optional dashboard is off by default. When it is on, it listens on loopback only.

## What it reads

Orchestrator reads its own configuration and database, plus:

- the files you name in `context_paths`, which must sit under a configured `review.roots` entry;
- a workflow's `workdir`, which must be a git repository under a configured `workflow.roots` entry;
- two fields from agent CLI history, both to identify the model that answered:
  - from the Codex session it just ran, the model, plus Codex's latest rate-limit figures;
  - from `opencode export` of the OpenCode session it just ran, the model.

It does not read your assistant's conversation history or memory. What it knows of a conversation is what arrives as tool arguments.

## Retention and deletion

Only the `isolated_write` files above expire, after 7 days. Database records stay until you delete them, with these tools:

- `orchestrator_delete_consultation`
- `orchestrator_delete_review`
- `orchestrator_delete_workflow`

For bulk deletion, `orchestrator_request_delete_all_consultations`, `orchestrator_request_delete_all` and `orchestrator_request_delete_all_workflows` each preview a delete and return a token, which you pass to the matching `delete_all` tool. To remove everything at once, delete the database file.

Uninstalling the plugin leaves `~/.orchestrator-mcp/` in place. Delete that directory to remove all of Orchestrator's data.

## Changes and contact

Changes to this policy are made in this file, and its history on GitHub is the changelog. For questions, open an issue at https://github.com/crAK1644/orchestrator-mcp/issues. For security problems, see [SECURITY.md](SECURITY.md).
