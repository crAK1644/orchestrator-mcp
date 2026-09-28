# Changelog

One line per change a user would notice. The full notes for each version are on its
[GitHub release](https://github.com/crAK1644/orchestrator-mcp/releases).

## Unreleased

- A failure no service anticipated now reaches the model as a coded refusal naming the
  exception type, not an opaque "Error executing tool".
- `orchestrator_list_reviews` takes `limit` between 1 and 100.
- `orchestrator_list_workflows` and `orchestrator_list_consultations`; the status
  command no longer asks you for a workflow id.
- Consultations, reviews and workflows read as `orchestrator://` resources, and prompt
  arguments and resource ids complete from what exists.
- `orchestrator_consult_many` asks several agents the same question at once.
- `ORCHESTRATOR_CONFIG` may start with `~`, which GUI hosts pass through unexpanded.
- Listed in the official MCP registry as `io.github.crAK1644/orchestrator-mcp`.
- README: a quick start, and config blocks for Claude Desktop, VS Code, Cursor and OpenCode.

## 0.7.4 — 2026-09-28

- `init` finds Codex in the current ChatGPT app's bundle.
- Review plans nobody ran are dropped after a day.

## 0.7.3 — 2026-09-25

- The Claude Code plugin is renamed `orchestrator-mcp`.

## 0.7.2 — 2026-09-25

- Installable as a Claude Code plugin; the server starts without a config and names the
  setup step.

## 0.7.1 — 2026-09-24

- Rechecks send only what changed, plans carry cost estimates, and `init` / `doctor`
  write and check a config.

## 0.7.0 — 2026-09-24

- OpenCode can run `isolated_write` steps under an OS sandbox, and refusals reach the
  model again on mcp 2.1+.
