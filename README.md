# Orchestrator MCP

Use Codex from Claude Code, or Claude Code from Codex, through the subscriptions you already have.

[![PyPI](https://img.shields.io/pypi/v/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Tests](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml)

If you pay for more than one AI subscription, Orchestrator MCP helps you use each model for what it does best. It runs the installed Codex or Claude Code CLI under your existing login, routes the work automatically, prevents an agent from consulting itself, and keeps a record of every consultation.

**No provider API key is required, anywhere.** Authentication stays inside the Codex and Claude Code command-line apps on your computer. Orchestrator only checks whether they are signed in — it never reads, stores, or returns a credential, and there is no code path in it that talks to a provider endpoint.

With Orchestrator MCP, your agent can:

- ask another vendor's agent for coding, research, writing, reasoning, or review help;
- continue a consultation across several turns;
- choose the best configured agent automatically;
- review consultation history in a local dashboard.

## Quick start

### 1. Install with Homebrew

```bash
brew tap crAK1644/tap
brew install orchestrator-mcp-server
```

Apple Silicon uses a prebuilt package. Intel macOS and Linux build the dependencies from source, which can take about 15 minutes. On those systems, the `uvx` setup shown below is usually faster and does not install the server permanently.

### 2. Sign in to the agent CLIs

Sign in to each agent you want Orchestrator to use:

```bash
codex login
claude auth login
```

These commands use the normal Codex and Claude Code account login. Orchestrator only checks whether the CLI is signed in; it does not read or store the login credentials.

### 3. Create a configuration file

Create `config.yaml`:

```yaml
consult:
  database_path: ~/.orchestrator-mcp/consultations.sqlite3
  timeout_s: 180
  agents:
    codex:
      runtime: codex
      command: codex
      model: gpt-5.6-sol
      priority: 10
      web_search: true
      scores: { coding: 95, research: 90, reasoning: 95, review: 90 }

    claude:
      runtime: claude
      command: claude
      model: claude-opus-4-6
      priority: 10
      web_search: true
      scores: { coding: 90, research: 95, writing: 95, review: 95 }
```

The full annotated example is in [`config.example.yaml`](config.example.yaml).

### 4. Add it to your MCP client

For Claude Code:

```bash
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$PWD/config.yaml \
  --env ORCHESTRATOR_HOST_RUNTIME=claude \
  -- orchestrator-mcp-server
```

For Codex, add this to `~/.codex/config.toml`:

```toml
[mcp_servers.orchestrator]
command = "orchestrator-mcp-server"
env = { ORCHESTRATOR_CONFIG = "/absolute/path/to/config.yaml", ORCHESTRATOR_HOST_RUNTIME = "codex" }
```

Restart your MCP client after changing its configuration.

### Install with `uvx` instead

You can run the same server without installing it permanently:

```bash
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$PWD/config.yaml \
  --env ORCHESTRATOR_HOST_RUNTIME=claude \
  -- uvx orchestrator-mcp-server
```

For Codex:

```toml
[mcp_servers.orchestrator]
command = "uvx"
args = ["orchestrator-mcp-server"]
env = { ORCHESTRATOR_CONFIG = "/absolute/path/to/config.yaml", ORCHESTRATOR_HOST_RUNTIME = "codex" }
```

The project is published to PyPI as `orchestrator-mcp-server`. The shorter PyPI name belongs to another project.

## What it does

Orchestrator has two paths, and both of them reach a model through a CLI you have already logged into. There is no API key anywhere.

| Path | Talks to | Needs an API key | Tools |
|---|---|---|---|
| Consultation | The Codex or Claude Code CLI on your computer, under its own login | No | `orchestrator_consult`, `orchestrator_list_consult_agents`, `orchestrator_get_consultation` |
| Review | The same CLIs, several at once, with the calling agent writing the synthesis | No | `orchestrator_review`, `orchestrator_review_run`, `orchestrator_finalize_review`, and the rest of the review set |

The review path is an opt-in on top of the first: no `review:` block, no review tools.

Direct API routing through LiteLLM was removed in 0.4. A config that still carries `capabilities:`, `model_list:`, `router_settings:` or `limits:` is a boot error that names the block; pin `orchestrator-mcp-server<0.4` if you need that path.

## The consultation path

The `orchestrator_consult` tool starts the Codex or Claude Code command-line app already installed and signed in on your computer. Claude Code can ask Codex, and Codex can ask Claude Code, using the subscriptions already connected to those CLIs. A third runtime, `antigravity`, is available as an experiment; see below for what it does not guarantee.

The consulted agent can answer, but it cannot change files, run commands, use MCP tools, or start subagents. Orchestrator also removes agents that use the same runtime as the caller, which prevents consultation loops.

`ORCHESTRATOR_HOST_RUNTIME` tells Orchestrator which agent is making the request. It must be `claude`, `codex`, or `antigravity`. The server excludes that runtime from the available targets, so a host can never consult itself. The value comes from the environment only; a calling model cannot set it as a tool argument.

### The `antigravity` runtime (experimental)

Google's Antigravity CLI (`agy`) can be configured as a consult target. It authenticates the same way as the other two: through its own login, cached in your operating system's keyring. Orchestrator never reads, copies, refreshes, or stores that credential, and there is no `api_key` or credential path to configure.

Three things about it differ from `codex` and `claude`, and you should know them before enabling it:

- **The isolation guarantee is weaker.** `agy` inherits the MCP servers configured in your own `agy` settings, and Orchestrator has no flag that can switch them off. What actually stops a consulted agent from using them is that `agy` denies tool permissions by default in headless mode — a default that lives in a file you own, not in anything this server controls. Orchestrator refuses the consultation the moment the CLI reports a tool step, so a permitted tool use fails the call rather than passing silently. But that is a detection, not a prevention. If you have loosened `agy`'s headless permissions, do not enable this runtime.
- **The prompt travels in the argument list, not on standard input.** `agy` reads neither stdin nor a prompt file. Linux caps a single argument at 128 KiB, so a prompt larger than that is split and sent across several turns of one conversation before the question is asked. Nothing is ever run through a shell, and no prompt is written to a file the model reads. But an argument list is public on the machine it runs on: for as long as the process lives, anyone else logged into the same computer can read the whole prompt — including whatever you passed as `context` — out of `ps` or `/proc`. On the other two runtimes the prompt goes to standard input, which is not readable that way. If you consult sensitive material on a shared machine, use `codex` or `claude` for it.
- **There is no way to check whether it is signed in.** `agy` has no login or status subcommand, so `orchestrator_list_consult_agents` reports it as authenticated with a detail saying that is unverified. A login problem surfaces as a failed consultation, not as a preflight failure.

Pick a Gemini slug if your prompts can exceed 128 KiB. `agy` also offers Claude and open-weight models, and those work normally on anything that fits in one argument — but the split-and-reassemble transport above is, structurally, what a prompt injection looks like: a large padded block with instructions spread across several turns. Live runs of `claude-sonnet-4-6` refused it on those grounds partway through, at a different fragment each time. The consultation fails rather than answering on a prompt with a hole in it, and the error quotes what the model said instead, but it does fail. `gemini-3.6-flash-high` and `gemini-3.1-pro-high` reassemble a 200 KB prompt correctly.

`reasoning_effort` is refused for this runtime because the effort level is part of the model name, and `agy` treats passing both as an error. Web mode is not offered.

### `orchestrator_consult`

What you send:

| Field | Required | Meaning |
|---|---|---|
| `capability` | yes | One of `coding`, `research`, `writing`, `reasoning`, `review`. |
| `prompt` | yes | The task or question. Up to 100,000 characters. |
| `context` | no | Source material. Up to 1,000,000 characters. Treated as evidence to read, never as instructions to follow. |
| `source_mode` | no | `auto`, `document`, `web`, or `model`. Defaults to `auto`. |
| `consultation_id` | no | Omit on the first call. Send the returned ID back on later calls to continue the same session. |
| `target_agent` | no | An explicit agent ID, which overrides the scores. The tool advertises the configured IDs as a fixed list, so a calling model cannot name an agent you did not configure. |
| `conversation_label` | no | A free-text label saved with the consultation. Up to 200 characters. |

Source modes:

| Mode | What the consulted agent gets |
|---|---|
| `auto` | Picks `document` when `context` is set, and `model` otherwise. Resolved before the request is sent; a target never receives `auto`. |
| `document` | Your `context`, with every tool switched off. The agent is instructed to answer from that material or say it cannot. |
| `web` | The target CLI's own web search, and nothing else. Bounded by `web_turn_limit`, which defaults to 8 assistant turns. |
| `model` | Neither. The agent answers from what it already knows. |

What you get back. Every outcome uses the same envelope:

| Field | Meaning |
|---|---|
| `ok` | False exactly when `error` is set. A failed consultation never carries answer text, so check this before reading `content`. |
| `consultation_id` | The handle for continuing this conversation. Null only when the call failed before a consultation existed. |
| `content.answer` | The agent's answer. |
| `content.assumptions` | What it assumed to answer. |
| `content.uncertainties` | What it was not sure about. |
| `content.follow_up_questions` | What it would ask you next. |
| `content.sources` | Title, locator, and type (`document`, `web`, or `model`) for each source it used. |
| `route` | Which agent answered: ID, runtime, model, capability score, priority, and whether you chose it explicitly. |
| `usage` | Token counts, when the CLI reports them. |
| `latency_ms` | Elapsed time. |
| `error` | A code, a message, the agent involved, and sometimes a `required_action` command for you to run. |

All five `content` fields are required of the consulted agent. A list may be empty but never missing: "no uncertainties" is a claim the agent has to make, not something you infer from an absent key.

Consultation error codes: `agent_not_installed`, `connection_required`, `configured_model_unavailable`, `agent_unavailable`, `session_not_found`, `session_busy`, `session_target_mismatch`, `protocol_validation_failed`, `web_search_unavailable`, `transport_error`, `timeout`, `invalid_request`, `no_agent_available`.

Behaviour worth knowing:

- Routing is predictable: the highest capability score wins, then the lowest priority number, then the agent ID. A score of 0, or a capability left out of the map, means that agent is not eligible.
- If the chosen agent fails, that is the answer. Orchestrator does not quietly try the next one.
- A consultation is pinned to the agent, runtime, and model that started it. Naming a different `target_agent` later returns `session_target_mismatch` rather than switching.
- If the CLI answers as a different model than the one configured, the consultation fails with `configured_model_unavailable` instead of returning an answer from a model nobody chose. For Codex this is checked against the session log the CLI writes under `~/.codex/sessions`, because `codex exec --json` on 0.146 does not name the model anywhere in its output. If neither source names one, the configured name is reported unverified — absent metadata is not treated as evidence of substitution, so a quiet release of either CLI does not become an outage.
- Two processes cannot advance the same consultation at once. The second gets `session_busy`.
- Nothing is ever run through a shell. Every CLI call is an argument list, and the prompt is written to the process's standard input — except on `antigravity`, which does not read standard input and takes the prompt as an argument instead.

### `orchestrator_list_consult_agents`

Returns the host runtime and one row per configured agent: `agent_id`, `runtime`, `model`, `priority`, `enabled`, `scores`, `web_search`, `excluded_as_host`, plus `installed`, `authenticated`, and a `detail` string from the last status check.

### `orchestrator_get_consultation`

Takes a `consultation_id` and returns the stored consultation: target agent, runtime and model, capability, the source modes used, label, status, whether a native session is still bound, timestamps, every turn, and the routing decision that picked the agent.

### Agent options

Each entry under `consult.agents` accepts:

| Option | Default | Meaning |
|---|---|---|
| `runtime` | required | `codex`, `claude`, or `antigravity`. |
| `command` | required | Executable name or absolute path. Resolved on `PATH`; an absolute path is safest for GUI-launched clients. |
| `model` | required | The model to ask for, and the one the answer is checked against. |
| `priority` | 100 | Lower wins a tie. |
| `enabled` | true | Set false to keep an agent configured but out of routing. |
| `scores` | none | 0–100 per capability. Missing means 0, which means not eligible. |
| `web_search` | false | Allows `source_mode: web` for this agent. Asking for `web` against an agent without it returns `web_search_unavailable` rather than quietly answering without a search. |
| `reasoning_effort` | unset | `low`, `medium`, `high`, `xhigh`, or `max`. Codex only. Setting it on a `claude` agent refuses to start, because that runtime would ignore it silently; on an `antigravity` agent because the effort level belongs in the model name there. |

### Consultation settings

| Setting | Default | Meaning |
|---|---|---|
| `database_path` | `~/.orchestrator-mcp/consultations.sqlite3` | Where consultations are stored. |
| `managed_agents_path` | `~/.orchestrator-mcp/agents.yaml` | The file the dashboard writes. |
| `timeout_s` | 180 | Time limit for one consultation turn. Raise it for slow, high-effort reviews. |
| `web_turn_limit` | 8 | Assistant turns allowed in `web` mode before the child process is stopped. |
| `store_full_content` | true | Set false to store only metadata and routing information. |
| `dashboard` | off | See below. |

## The review path

A consultation asks one agent. A review asks one or more of them the same question over the same approved material, groups the answers under a `review_id`, and hands your own AI everything it needs to write one conclusion. It is off unless you configure reviewers:

```yaml
consult:
  review:
    reviewers: [codex-sol]                    # `standard`: exactly one
    deep_reviewers: [codex-sol, gemini-x]     # `deep`: one to five
```

Each id must be a configured agent that is enabled and scores above 0 for `review`. A `review:` block in both the config file and the dashboard's `agents.yaml` is a startup error rather than a precedence rule, because a precedence rule is how an edit saves successfully and does nothing.

### The handshake

Reviews are two calls, not one:

1. **`orchestrator_review`** returns a plan and sends nothing: which reviewers, how much material, whether web access was requested, how many requests it will cost, and the lines where something credential-shaped was found — positions only, never values. Show it to the user.
2. **`orchestrator_review_run`** spends the one-time `confirm_token` from that plan and asks every reviewer in parallel.

It stops at `awaiting_synthesis`. External models replying is not a finished review; **`orchestrator_finalize_review`** records your AI's combined conclusion and is the only path to `complete`. Every Critical finding in a reviewer's **findings block** must be referenced there, **including one only a single reviewer raised while the others disagree** — that is checked, and the call is refused otherwise.

The findings block is the review, and the check can only be over the block. Prose around it is not parsed and not certified: a reviewer that argues a Critical in prose and then sends `{"findings": []}` will finalize cleanly, because nothing machine-readable said otherwise. `REVIEWER_INSTRUCTIONS` tells every reviewer this, so it is a contract rather than a gap — but a human reading a review still reads the prose.

By default, `orchestrator_review_run` uses `secrets="mask"` and sends the redacted
copy. `secrets="send_as_is"` is an explicit escape hatch for a credential-shaped
fixture or false positive: it requires the exact original goal and context in `raw`,
sends those originals to every reviewer, and still stores only the redacted copy.
Those originals can remain in each reviewer's own CLI history, which this project
cannot erase. A retry of such a review requires the same `raw` material again and
hash-checks it against the original plan.

Finalization is refused when a reviewer answered only in unparseable prose, when
findings were truncated, or when `store_full_content: false` discarded them. In
those states the server cannot prove that every Critical survived, so it does not
treat an empty stored finding set as evidence that there were none.

| Tool | What it does |
|---|---|
| `orchestrator_review` | Plans a review and shows what would be sent. Sends nothing. |
| `orchestrator_review_run` | Spends the token and asks every reviewer. |
| `orchestrator_retry_review` | Re-runs only failed reviewers. A `send_as_is` review requires its original `raw` material again. |
| `orchestrator_finalize_review` | Records the synthesis. The only path to `complete`. |
| `orchestrator_cancel_review` | Marks a review cancelled and keeps answers already given. It waits for work launched by this server process; another process's subprocesses cannot be signalled and deletion waits for their lease. |
| `orchestrator_apply_fixes` | Pulls up the findings you selected, with the steps around them. Changes nothing. |
| `orchestrator_record_fix_round` | Logs what a round of fixing did, after you did it. |
| `orchestrator_test_reviewers` | Checks the reviewers are installed and logged in. No project material leaves the machine. |
| `orchestrator_get_review` / `orchestrator_list_reviews` | Read one review, or recent ones newest first. |
| `orchestrator_delete_review` | Deletes a review, its rechecks, and every consultation under either. |
| `orchestrator_request_delete_all` / `orchestrator_delete_all_reviews` | Counts what deleting all history would remove, then deletes exactly that snapshot. |

### Fixing what a review found

`orchestrator_apply_fixes` is bookkeeping around work your own AI does. It returns the findings you selected and the steps that go with them — make a safety point first, apply the changes, run the tests, keep or undo — and it changes nothing itself. **This server never edits a file and never runs a command**, and a reviewer never sees your repository at all.

Two things it does enforce. A finding id no reviewer raised is refused rather than recorded against nobody, and `criticals_omitted` names every Critical your selection leaves out — the same rule as the synthesis check, one stage later.

`orchestrator_record_fix_round` then logs what happened (`applied`, `partial`, `reverted`, `skipped`) beside the findings it names. It is your AI's account of the round; nothing here can verify it, which is why the dashboard labels the rounds as claims.

To re-review, plan a new review with `parent_review_id` set to the original and the diff as `context`. A recheck is an ordinary review: same preview, same secret scan, same approval. The parent's page links to it.

### What a review does not do

- **No web access unless you ask for it.** `web: false` is the default for every review.
- **Reviewers cannot act.** Same answer-only mode as `orchestrator_consult`: no file changes, no commands, no requests for more material.
- **No automatic fixes.** Findings are returned; editing is your AI's job, with your approval.
- **Credential-shaped values are replaced before every insert**, in the goal, the context, the manifest, and every reviewer's answer. Detection is best-effort pattern matching — see [Not included](#not-included) for what it cannot cover.
- **`send_as_is` changes what leaves the machine, not what is stored.** It sends the
  hash-verified original goal and context to reviewers and their CLI histories while
  the database continues to receive only the redacted copy.

## Dashboard and consultation history

The dashboard is off by default because it can display every stored consultation.

Enable it in `config.yaml`:

```yaml
consult:
  dashboard:
    enabled: true
    editable: false
```

Start it separately:

```bash
ORCHESTRATOR_CONFIG=config.yaml orchestrator-mcp-dashboard
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The dashboard only listens on your computer. It shows configured agents, recent consultations, prompts, answers, routing, usage, latency, and errors. `host` accepts only a loopback address, and a request whose `Host` header names anything else is refused.

The dashboard runs as its own process and reads the configuration once, at startup — the same as the MCP server. Neither one notices a change the other made. It never runs a login command; the connect commands on the page are text for you to copy.

### Editing agents from the browser

Set `editable: true` and open `/agents`. This is a second flag on purpose: turning the dashboard on gets you a window, and editing is a different thing to agree to.

What the form can change: consult agents only — runtime, command, model, priority, enabled, the five capabilities, web search, and reasoning effort. Nothing else is editable from the browser. `capabilities`, `model_list`, `router_settings`, `limits`, `timeout_s`, `web_turn_limit`, `store_full_content`, and the dashboard's own host and port are config-file settings.

Capabilities are ticks on the form, not the 0–100 numbers the config file holds: the question is which work an agent should be offered, and among agents ticked for the same capability `priority` already decides the order. A newly ticked box saves 100. A score you wrote by hand to break a tie is preserved — the form carries it back out and saves it unchanged, so editing an unrelated field does not flatten it.

Agents you add here are written to `~/.orchestrator-mcp/agents.yaml`, with `0600` permissions in a `0700` directory. The dashboard never writes `config.yaml`. Agents defined there are listed on the page but not editable, and the page says so.

The two files are merged at startup and neither one wins. **An agent ID defined in both files stops the server from starting**, and the error names the ID and both paths. There is no precedence rule, because a precedence rule is how you get an edit that saves and then does nothing. The form refuses a save that would create that state, and it checks `config.yaml` as it is on disk rather than as it was when the dashboard started — so an agent you add to that file by hand is refused here right away, and one you delete from it stops being refused. Only the IDs are re-read, so it works in one direction: an agent added to `config.yaml` while the page is open does not appear in the read-only table until the dashboard restarts, but it is still enough to block a save the next startup would reject.

If `config.yaml` is empty or half-written when the page reads it — the state an editor leaves for a moment while saving — the check falls back to the agents the dashboard started with. Reading that moment as "this file defines no agents" is exactly how a duplicate would slip through.

An agent that exists in both files keeps its row and its delete button, since deleting the copy here is the only fix available from the browser.

Changes take effect when the MCP server next starts. The page says so after every save, and it warns you when the running server is on an older configuration than the one on disk.

The agent table's status column is the newest recorded preflight, labelled **last checked**. The page runs nothing: it reads whatever the last check wrote, which may be days old. `orchestrator_test_reviewers` is the tool that actually asks.

### Reviews in the dashboard

`/reviews` lists reviews newest first, and `/reviews/<id>` shows one: its status and outcome, every reviewer with the consultation it ran under, all stored findings sorted worst-first across reviewers, the synthesis in the problem / seriousness / who-agreed / proposed-action shape, and each stored reviewer answer folded into a `<details>`. Findings and answers are present only with `store_full_content: true`. Rechecks link back to the review they came from.

Recorded fix rounds appear at the bottom, labelled as claims: nothing in this server edits a file or runs a command, so a round is an account of work done elsewhere.

What you see is what was stored, which is the redacted copy — a credential-shaped value was replaced before the insert, and the page reports only that one was found and on which line.

There is no delete button. The dashboard opens the database read-only, so deleting is `orchestrator_delete_review` and `orchestrator_delete_all_reviews`, from your AI.

With `editable: true`, `/reviewers` sets who reviews: one agent for `review`, one to five for `deep_review`. It writes the same `~/.orchestrator-mcp/agents.yaml`, both blocks together, so a reviewer save cannot drop your agents. A `review:` block in `config.yaml` makes the page read-only and says which file to edit — the same reason the agents have no precedence rule.

A database created before this version has no review tables, and the read-only connection cannot add them. The page says to restart the MCP server, which migrates at startup.

By default, consultation prompts and answers are saved in SQLite. Set `store_full_content: false` to save only metadata and routing information.

## Configuration

`ORCHESTRATOR_CONFIG` points to the configuration file. If it is not set, the server looks for `config.yaml` in its working directory.

Important sections:

| Section | Purpose |
|---|---|
| `consult` | Configures logged-in CLI agents, history, and the dashboard. |
| `consult.agents` | The CLIs to route to: runtime, command, model, and per-capability scores. |
| `consult.review` | Who reviews. Absent means the review tools are not advertised at all. |
| `consult.dashboard` | The local history page. Off unless you turn it on. |

`consult` is the only top-level section. A config that still carries `capabilities`, `model_list`, `router_settings`, or `limits` was written for the direct routing path removed in 0.4, and the server refuses to start on it rather than quietly advertising fewer tools than the file asks for.

Invalid configuration is rejected when the server starts instead of failing during a request.

## Guardrails and limits

Orchestrator MCP checks request and response structure. It does not know whether a model's factual claims are true.

It does enforce these rules:

- Unknown capabilities and oversized requests are rejected before any CLI is started.
- Truncated, filtered, malformed, and failed answers are returned as errors, not partial answers.
- Errors use stable codes such as `connection_required`, `timeout`, and `session_busy`.
- The response always identifies which agent and model answered.
- Consulted agents run in answer-only mode and may not act on your computer.
- A consulted agent cannot route work back to the same agent runtime.
- Login credentials are not read or stored. Authentication stays in the vendor's CLI.

Important limits:

- Treat caller-provided JSON Schemas as trusted input. A complex regular expression can use a large amount of CPU.
- CLI error text is shortened and common secret formats are redacted, but unusual secrets may still appear. Do not forward errors to an untrusted place.
- The consultation database may contain full prompts and answers. Keep it private or disable full-content storage.

## System requirements

- macOS or Linux. Windows is not tested; the Homebrew instructions are macOS only, and the server has only been run on POSIX systems.
- Python 3.11, 3.12, or 3.13
- Homebrew or [`uv`](https://docs.astral.sh/uv/)
- An MCP client that supports stdio, such as Claude Code or Codex
- The Codex or Claude Code CLI installed and signed in

## Testing

Run the offline test suite:

```bash
uv sync
uv run pytest -q
```

The tests use fake CLI agents. They do not need a network connection or spend money.

To test real Codex and Claude Code consultations:

```bash
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_consult_live.py
```

And a real review, end to end:

```bash
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_review_live.py
```

The smoke tests make real requests and may use paid capacity from your configured services. Do not run them in CI unless that is intentional.

## Troubleshooting

| Problem | What to do |
|---|---|
| `config not found: config.yaml` | Set `ORCHESTRATOR_CONFIG` to an absolute path. MCP clients may start the server from a different directory. |
| Startup names a block removed in 0.4 | Delete `capabilities`, `model_list`, `router_settings`, and `limits`; they configured the direct routing path. |
| `no_agent_available` | Every configured agent scores 0 for the capability, is disabled, or shares the caller's runtime. |
| `timeout` on a long review | Raise `consult.timeout_s`; a reviewer thinking at `xhigh` over a real diff can need far more than the 180s default. |
| `consult` is missing | Add the `consult` section and check that the client loaded the correct config file. |
| Host runtime error at startup | Set `ORCHESTRATOR_HOST_RUNTIME` to `claude`, `codex`, or `antigravity` in the MCP client's environment. |
| `agent_not_installed` | Use an absolute path in the agent's `command`; GUI apps may have a smaller `PATH` than your shell. |
| `connection_required` | Run the login command shown in the error in your own terminal, then try again. |
| Every consultation starts over | Return the previous `consultation_id` with the next call. |
| A dashboard change does not appear | Restart the MCP server; it reads configuration at startup. |

## Bug reports

[Open an issue](https://github.com/crAK1644/orchestrator-mcp/issues) and include the returned response envelope. Remove paths, credentials, and other private information before attaching your configuration.

## Contributing

Issues and pull requests are welcome.

1. Fork the repository and create a branch.
2. Make the change and add a test that fails without it.
3. Run `uv run pytest -q`.
4. Open a pull request.

Keep private configuration, login data, and consultation databases out of commits.

### Releasing

1. Update `version` in `pyproject.toml`.
2. Create a GitHub Release tagged `vX.Y.Z`.
3. The release workflow runs the tests, checks the version, and publishes to PyPI using Trusted Publishing.
4. Update the formula in the [`homebrew-tap`](https://github.com/crAK1644/homebrew-tap) repository and rebuild its Apple Silicon bottle.

## Not included

Deliberately out of scope for now:

- **Consulted agents cannot act.** No file changes, no commands, no MCP tools, no subagents. Answers only.
- **No streaming.** A consultation returns one complete envelope.
- **No consultations from the dashboard.** The page reads history and edits agent configuration; it has never started an agent process, and that is worth keeping until there is a reason to give it up.
- **No runtime settings in the dashboard.** Timeouts, storage, and the dashboard's own host and port are config-file settings. A page that can change the port it is served on is a footgun that deserves its own design.
- **No accounts on the dashboard.** Its protection is a loopback bind, a `Host` header check, and a per-process token — enough for one person on one machine, not for a shared host.
- **Redaction covers this database only.** Credential-shaped values are replaced before every insert, and detection is best-effort — a secret with no recognizable shape survives it. Material sent to a reviewer also lands in that reviewer's own CLI history (Codex writes `~/.codex/sessions/`, and the others keep their own logs). Nothing here can reach those files.
- **No automatic restart.** Both processes read the configuration once, at startup.
- **No multi-user or shared state.** One SQLite file, local to your computer.
- **No direct API routing.** Every model is reached through a CLI you have logged into yourself. There is no provider SDK, no API key, and no endpoint to configure; the LiteLLM path that offered that was removed in 0.4.

## License and support

This project uses the [MIT License](LICENSE).

- [GitHub issues](https://github.com/crAK1644/orchestrator-mcp/issues)
- [PyPI releases](https://pypi.org/project/orchestrator-mcp-server/)
- [Model Context Protocol](https://modelcontextprotocol.io)

Built with [Pydantic](https://docs.pydantic.dev) and the [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
