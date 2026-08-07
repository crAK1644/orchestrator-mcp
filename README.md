# Orchestrator MCP

Use Codex from Claude Code, or Claude Code from Codex, through the subscriptions you already have.

[![PyPI](https://img.shields.io/pypi/v/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Tests](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml)

If you pay for more than one AI subscription, Orchestrator MCP helps you use each model for what it does best. It runs the installed Codex or Claude Code CLI under your existing login, routes the work automatically, prevents an agent from consulting itself, and keeps a record of every consultation.

**No provider API key is required for this setup.** Authentication stays inside the Codex and Claude Code command-line apps on your computer. Orchestrator only checks whether they are signed in — it never reads, stores, or returns a credential. The optional direct routing path described later is the only part that talks to a provider endpoint, and therefore the only part that needs a key.

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

Orchestrator has two independent paths. They can be used together or one at a time.

| Path | Talks to | Needs an API key | Tools |
|---|---|---|---|
| Consultation | The Codex or Claude Code CLI on your computer, under its own login | No | `consult`, `list_consult_agents`, `get_consultation` |
| Direct routing | A model endpoint through LiteLLM | Yes, unless the endpoint is local and unauthenticated | `ask`, `list_capabilities` |

The consultation path is the reason this project exists. The direct routing path is older and optional. Configure the `consult` section only, and the two `ask` tools are never registered.

## The consultation path

The `consult` tool starts the Codex or Claude Code command-line app already installed and signed in on your computer. Claude Code can ask Codex, and Codex can ask Claude Code, using the subscriptions already connected to those CLIs. A third runtime, `antigravity`, is available as an experiment; see below for what it does not guarantee.

The consulted agent can answer, but it cannot change files, run commands, use MCP tools, or start subagents. Orchestrator also removes agents that use the same runtime as the caller, which prevents consultation loops.

`ORCHESTRATOR_HOST_RUNTIME` tells Orchestrator which agent is making the request. It must be `claude`, `codex`, or `antigravity`. The server excludes that runtime from the available targets, so a host can never consult itself. The value comes from the environment only; a calling model cannot set it as a tool argument.

### The `antigravity` runtime (experimental)

Google's Antigravity CLI (`agy`) can be configured as a consult target. It authenticates the same way as the other two: through its own login, cached in your operating system's keyring. Orchestrator never reads, copies, refreshes, or stores that credential, and there is no `api_key` or credential path to configure.

Three things about it differ from `codex` and `claude`, and you should know them before enabling it:

- **The isolation guarantee is weaker.** `agy` inherits the MCP servers configured in your own `agy` settings, and Orchestrator has no flag that can switch them off. What actually stops a consulted agent from using them is that `agy` denies tool permissions by default in headless mode — a default that lives in a file you own, not in anything this server controls. Orchestrator refuses the consultation the moment the CLI reports a tool step, so a permitted tool use fails the call rather than passing silently. But that is a detection, not a prevention. If you have loosened `agy`'s headless permissions, do not enable this runtime.
- **The prompt travels in the argument list, not on standard input.** `agy` reads neither stdin nor a prompt file. Linux caps a single argument at 128 KiB, so a prompt larger than that is split and sent across several turns of one conversation before the question is asked. Nothing is ever run through a shell, and no prompt is written to a file the model reads. But an argument list is public on the machine it runs on: for as long as the process lives, anyone else logged into the same computer can read the whole prompt — including whatever you passed as `context` — out of `ps` or `/proc`. On the other two runtimes the prompt goes to standard input, which is not readable that way. If you consult sensitive material on a shared machine, use `codex` or `claude` for it.
- **There is no way to check whether it is signed in.** `agy` has no login or status subcommand, so `list_consult_agents` reports it as authenticated with a detail saying that is unverified. A login problem surfaces as a failed consultation, not as a preflight failure.

Pick a Gemini slug if your prompts can exceed 128 KiB. `agy` also offers Claude and open-weight models, and those work normally on anything that fits in one argument — but the split-and-reassemble transport above is, structurally, what a prompt injection looks like: a large padded block with instructions spread across several turns. Live runs of `claude-sonnet-4-6` refused it on those grounds partway through, at a different fragment each time. The consultation fails rather than answering on a prompt with a hole in it, and the error quotes what the model said instead, but it does fail. `gemini-3.6-flash-high` and `gemini-3.1-pro-high` reassemble a 200 KB prompt correctly.

`reasoning_effort` is refused for this runtime because the effort level is part of the model name, and `agy` treats passing both as an error. Web mode is not offered.

### `consult`

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

### `list_consult_agents`

Returns the host runtime and one row per configured agent: `agent_id`, `runtime`, `model`, `priority`, `enabled`, `scores`, `web_search`, `excluded_as_host`, plus `installed`, `authenticated`, and a `detail` string from the last status check.

### `get_consultation`

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

## The direct routing path

`ask` and `list_capabilities` route a request to a model deployment through LiteLLM. This is separate from the consultation path and, for any hosted provider, it is the part that needs an API key.

The `ask` tool routes a named capability, such as `coding` or `research`, to the deployment assigned to it. LiteLLM handles load balancing, retries, cooldowns, and fallbacks. A deployment can be a local model or another endpoint already configured for LiteLLM.

What you send:

| Field | Required | Meaning |
|---|---|---|
| `capability` | yes | One of your configured capability names, advertised as a fixed list. |
| `prompt` | yes | The task or question. |
| `context` | no | Source material. When set, the model is told to answer only from it and to say so otherwise. |
| `system` | no | Extra instructions placed before the conversation. Orchestrator's own instructions are always applied last, so they cannot be turned off from here. |
| `response_schema` | no | A JSON Schema, as an object or as a JSON string. The reply is validated against it locally. |
| `temperature` | 0.2 | Forced to 0 whenever `response_schema` is set. |
| `max_output_tokens` | from `limits` | Per-request override. |

What you get back:

| Field | Meaning |
|---|---|
| `ok` | False exactly when `error` is set. A failed call carries neither `content` nor `data`. |
| `content` | The answer text. Null when `response_schema` was used. |
| `data` | The validated structured answer, when `response_schema` was used. |
| `insufficient_context` | True when the model said the provided context did not answer the question. |
| `model_used` | Which deployment answered. |
| `fallback_used` | Whether a fallback capability handled it. |
| `finish_reason`, `usage`, `latency_ms` | Stop reason, tokens, elapsed time. |
| `error` | A code and a message. |

Routing error codes: `invalid_request`, `no_deployment`, `upstream_error`, `rate_limited`, `context_exceeded`, `schema_validation_failed`, `timeout`, `content_filtered`, `auth_failed`, `output_truncated`.

`list_capabilities` returns each configured capability with its description, the deployments behind it, and the capabilities it falls back to.

You can leave `capabilities` and `model_list` out of `config.yaml` if you only want subscription-based consultations. In that setup, Orchestrator only shows the three consultation tools.

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

By default, consultation prompts and answers are saved in SQLite. Set `store_full_content: false` to save only metadata and routing information.

## Configuration

`ORCHESTRATOR_CONFIG` points to the configuration file. If it is not set, the server looks for `config.yaml` in its working directory.

Important sections:

| Section | Purpose |
|---|---|
| `consult` | Configures logged-in CLI agents, history, and the dashboard. |
| `capabilities` | Names and explains the work types available to `ask`. |
| `model_list` | Connects each capability to one or more LiteLLM models. |
| `router_settings` | Controls retries, cooldowns, and fallbacks. |
| `limits` | Sets request size, output, repair, and timeout limits for `ask`. Consultations have their own caps and their own `timeout_s`. |

Several deployments may use the same capability name. LiteLLM will balance requests between them.

You may configure only `ask`, only `consult`, or both. If `consult` is missing, the consultation tools are not shown. If `capabilities` and `model_list` are both missing, the model tools are not shown.

The default `limits` block, which applies to `ask` only:

| Setting | Key | Default |
|---|---|---:|
| Prompt | `max_prompt_chars` | 100,000 characters |
| Context | `max_context_chars` | 400,000 characters |
| System instructions | `max_system_chars` | 10,000 characters |
| JSON Schema | `max_schema_chars` | 20,000 characters |
| Output | `max_output_tokens` | 4,096 tokens |
| Whole request | `request_timeout_s` | 120 seconds |
| Schema repair attempts | `schema_repair_attempts` | 1 |

Invalid configuration is rejected when the server starts instead of failing during a request.

## Guardrails and limits

Orchestrator MCP checks request and response structure. It does not know whether a model's factual claims are true.

It does enforce these rules:

- Unknown capabilities and oversized requests are rejected before contacting a provider.
- Structured replies are checked locally against your JSON Schema.
- Truncated, filtered, malformed, and failed answers are returned as errors, not partial answers.
- Errors use stable codes such as `connection_required`, `timeout`, and `session_busy`.
- The response always identifies the model and whether a fallback was used.
- Consulted agents run in answer-only mode and may not act on your computer.
- A consulted agent cannot route work back to the same agent runtime.
- Login credentials are not read or stored. Authentication stays in the vendor's CLI.

Important limits:

- Treat caller-provided JSON Schemas as trusted input. A complex regular expression can use a large amount of CPU.
- Provider error text is shortened and common secret formats are redacted, but unusual secrets may still appear. Do not forward errors to an untrusted place.
- The consultation database may contain full prompts and answers. Keep it private or disable full-content storage.

## System requirements

- macOS or Linux. Windows is not tested; the Homebrew instructions are macOS only, and the server has only been run on POSIX systems.
- Python 3.11, 3.12, or 3.13
- Homebrew or [`uv`](https://docs.astral.sh/uv/)
- An MCP client that supports stdio, such as Claude Code or Codex
- The Codex or Claude Code CLI installed and signed in
- For optional direct routing: a model endpoint configured through LiteLLM

## Testing

Run the offline test suite:

```bash
uv sync
uv run pytest -q
```

The tests use fake providers and CLI agents. They do not need a network connection or spend money.

To test your real model configuration:

```bash
uv run python smoke_live.py
```

To test real Codex and Claude Code consultations:

```bash
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_consult_live.py
```

The smoke tests make real requests and may use paid capacity from your configured services. Do not run them in CI unless that is intentional.

## Troubleshooting

| Problem | What to do |
|---|---|
| `config not found: config.yaml` | Set `ORCHESTRATOR_CONFIG` to an absolute path. MCP clients may start the server from a different directory. |
| Startup names a missing capability | Make sure every capability has a deployment and every fallback names a real capability. |
| `no_deployment` | All deployments are unavailable or cooling down. Check the provider and `cooldown_time`. |
| `output_truncated` | Raise `max_output_tokens`. |
| `schema_validation_failed` | Simplify the schema or use a model with stronger structured-output support. |
| `timeout` on a local model | Increase `request_timeout_s`; model loading is included in the time limit. |
| `consult` is missing | Add the `consult` section and check that the client loaded the correct config file. |
| Host runtime error at startup | Set `ORCHESTRATOR_HOST_RUNTIME` to `claude`, `codex`, or `antigravity` in the MCP client's environment. |
| `agent_not_installed` | Use an absolute path in the agent's `command`; GUI apps may have a smaller `PATH` than your shell. |
| `connection_required` | Run the login command shown in the error in your own terminal, then try again. |
| Every consultation starts over | Return the previous `consultation_id` with the next call. |
| A dashboard change does not appear | Restart the MCP server; it reads configuration at startup. |

## Bug reports

[Open an issue](https://github.com/crAK1644/orchestrator-mcp/issues) and include the returned response envelope. Remove paths, credentials, and other private information before attaching your configuration.

For routing or retry problems, a LiteLLM debug log is useful:

```bash
LITELLM_LOG=DEBUG uv run python smoke_live.py 2>debug.log
```

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
- **No automatic restart.** Both processes read the configuration once, at startup.
- **No multi-user or shared state.** One SQLite file, local to your computer.
- **No semantic intent routing**, streaming tool results, automatic PII removal, or shared Redis state on the routing path. LiteLLM can provide some of these through its own configuration and callbacks.

## License and support

This project uses the [MIT License](LICENSE).

- [GitHub issues](https://github.com/crAK1644/orchestrator-mcp/issues)
- [PyPI releases](https://pypi.org/project/orchestrator-mcp-server/)
- [LiteLLM routing documentation](https://docs.litellm.ai/docs/routing)
- [Model Context Protocol](https://modelcontextprotocol.io)

Built with [LiteLLM](https://github.com/BerriAI/litellm), [Pydantic](https://docs.pydantic.dev), and the [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
