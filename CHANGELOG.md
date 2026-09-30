# Changelog

One line per change a user would notice. The full notes for each version are on its
[GitHub release](https://github.com/crAK1644/orchestrator-mcp/releases).

## Unreleased

- `runtime: copilot` consults the GitHub Copilot CLI, for answers only. Every tool is switched
  off and the run happens in an empty scratch directory, under a `COPILOT_HOME` of its own in
  `~/.orchestrator-mcp/copilot` that your `~/.copilot` never touches. `isolated_write` and web
  mode are refused. Sign in once with the command a signed-out consultation returns; the
  readiness check costs no request.
- A free Copilot plan serves the model `auto` only, and the response names the model Copilot
  routed the call to. Copilot reports no price, so its cost reads as unknown and a dollar
  ceiling cannot count it; a turn ceiling can. Its session history keeps prompts and answers
  unmasked and is never pruned (see PRIVACY.md). `copilot` is also accepted as
  `ORCHESTRATOR_HOST_RUNTIME`, and joins the runtime enums, so the tool schema changes once.

## 0.10.2 — 2026-09-30

- `search WORD... [--days N] [--limit N] [--json]` and the read-only tool
  `orchestrator_search_consultations` find the stored prompts and answers that hold every
  word, best match first, as masked excerpts with the ids to read the rest. Nothing is
  written to the database and no migration is needed; the index is built in memory for
  each call, so a credential in an old row is neither printed nor searchable.
- `strengths [--days N] [--json]` shows, for each kind of question, agent and model, how
  many turns were asked, answered and failed, how long an answer took and what it cost,
  with the scorecard's hit rate on `review` rows. Under the table it says when an agent
  failed at least 30% of five or more asks, or when another configured agent answered at
  least twice as fast at no higher failure rate than the one your config routes to. It only
  prints: nothing is changed, and it does not say whose answers were better.
- The report commands print `cannot open the database at PATH: ...` when `database_path` names
  a directory or a file this user cannot read. `usage`, `history`, `scorecard` and `export`
  used to end in a traceback there, and `orchestrator_search_consultations` returns the same
  sentence as its note.

## 0.10.1 — 2026-09-30

- `persona` no longer waits for `consult.personas`. Six ready-made styles ship in the
  server: `skeptic`, `security`, `simplify`, `plain`, `case-for` and `case-against`. A
  `consult.personas` entry of the same name replaces the wording. The argument is now in
  the schema of both consult tools for everyone, so the tool schema changes once.
- `orchestrator_consult_many` takes `both_sides`: exactly two agents argue opposite sides of
  the proposal in `prompt`, the first for and the second against, each answering alone.
  `sides` in the response says who argued which. The server never picks a winner.

## 0.10.0 — 2026-09-30

- `orchestrator_review` takes `diff_ref` (`A..B`, `A...B` or one commit) and `diff_repo`: the
  server reads the committed diff from git objects under `review.roots`, so a branch review
  no longer needs a diff written to a file first. The preview lists the resolved SHAs. It
  reads no working tree or index and runs no program the repository's config names.
- `orchestrator_consult` and `orchestrator_consult_many` take `context_paths`, read from the
  directories in the new `consult.context_roots`. The argument does not exist until that is
  set. A file holding a credential-shaped value is refused, by path and line.
- `consult.personas` names emphases (`skeptic: "Doubt the premise first."`). Both consult
  tools take `persona`, per turn, once any are configured. The text joins the system half
  and cannot change the protocol.
- `workflow.presets` names sets of step bindings, and `orchestrator_workflow_start` takes
  `preset`. A preset sits between the config's `bindings` and the call's. It is checked at
  config load, and a running workflow is unmoved by editing it.
- The review preview lists `suspect_hits`: lines holding a long random-looking token that no
  pattern names. Advisory only: it never blocks, never changes the confirm hash and is
  not stored. Hex, UUIDs, integrity hashes, snake_case, kebab-case and plain words are left
  alone; a key of letters only, or of hex, is not caught.
- Adding config fields widens `config_hash`, so the dashboard shows its stale-row banner once
  after upgrading.

## 0.9.0 — 2026-09-29

- `orchestrator_apply_fixes` lists the Important findings a selection leaves out in
  `fix_plan.importants_omitted`, as it already did for Critical ones. A finding the
  synthesis rejected on purpose is listed too: it prompts a look, not a fix.
- `usage`, `history`, `scorecard` and `export ID` subcommands print the database's
  spend, recent records, per-reviewer results and a masked JSON copy of one record. All
  are read-only: they never migrate the database (a WAL database may gain `-wal` and
  `-shm` sidecar files while they read). A price nobody reported prints as `unknown`,
  never `$0`.
- The dashboard has a `/scorecard` page: the same per-reviewer numbers as `scorecard`.
- `retention_days` now also sweeps roughly daily while the server runs, not only at start-up,
  so a server left up for weeks no longer keeps history past the setting.

## 0.8.1 — 2026-09-29

- Private keys are now masked in review material, stored history and error text. The
  pattern never matched a real `-----BEGIN ... PRIVATE KEY-----` header, so a key in a
  review's context reached the reviewers and the local database as written, and the
  preview did not flag it. History stored before 0.8.1 is not rescanned;
  `orchestrator_delete_all_reviews` clears it.
- `DB_PASSWORD=`, `OPENAI_API_KEY=`, `GITHUB_TOKEN=`, `AWS_SECRET_ACCESS_KEY=` and other
  prefixed names are masked, with more token shapes and URL passwords. Words such as
  `task-management-system` no longer are.
- `doctor` notes that Antigravity takes the prompt on its command line, where other users
  on the machine can read it while a turn runs.
- README and `config.example.yaml` now say what the code does: `isolated_write` reaches
  OpenCode where the sandbox holds, and finalization keeps every Critical and Important
  finding.

## 0.8.0 — 2026-09-28

- A failure no service anticipated now reaches the model as a coded refusal naming the
  exception type, not an opaque "Error executing tool".
- `orchestrator_list_reviews` takes `limit` between 1 and 100.
- `orchestrator_list_workflows` and `orchestrator_list_consultations`; the status
  command no longer asks you for a workflow id.
- Consultations, reviews and workflows read as `orchestrator://` resources, and prompt
  arguments and resource ids complete from what exists.
- `orchestrator_consult_many` asks several agents the same question at once.
- `consult.retention_days` deletes finished history nobody has touched in that many days.
- Reviews and workflows render as a table in hosts with MCP Apps, such as Claude Desktop.
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
