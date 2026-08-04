# orchestrator-mcp

#### Capability-routed MCP server: send each request to the model configured for that kind of work

[![PyPI](https://img.shields.io/pypi/v/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Tests](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml)

**[Quick Start](#quick-start)** · **[How It Works](#how-it-works)** · **[Tools](#tools)** ·
**[Consult](#consulting-another-agent)** · **[Configuration](#configuration)** ·
**[Guardrails](#guardrails-and-their-limits)** · **[Troubleshooting](#troubleshooting)** ·
**[Contributing](#contributing)**

Research to one model, coding to another, cheap extraction to a third — and every answer
comes back through the same validated envelope. Pointing a capability at your own
deployment is a YAML edit. There is no code to change.

Since 0.2.0 there is a second path: [**consult**](#consulting-another-agent) routes a
question to another vendor's *coding agent* — a Codex or Claude Code CLI on your machine,
under its own account — so Claude Code can ask GPT and Codex can ask Claude, in either
direction, with no second API key.

> Published to PyPI as **`orchestrator-mcp-server`** — the shorter name is an empty
> registered project owned by someone else. The import package is `orchestrator_mcp`.

## Quick Start

Write a `config.yaml` — start from [`config.example.yaml`](config.example.yaml) — and
check that it loads:

```bash
ORCHESTRATOR_CONFIG=config.yaml uvx --from orchestrator-mcp-server python -c "from orchestrator_mcp.server import build_server; build_server(); print('config ok')"
```

A bad config fails here rather than at request time: every deployment must route to a
declared capability, every capability must have a deployment behind it, and every
fallback must name a real capability.

### Claude Code

```bash
claude mcp add orchestrator --env ORCHESTRATOR_CONFIG=$PWD/config.yaml -- uvx orchestrator-mcp-server
```

### Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.orchestrator]
command = "uvx"
args = ["orchestrator-mcp-server"]
env = { ORCHESTRATOR_CONFIG = "/absolute/path/to/config.yaml" }
```

Both speak stdio, and every tool result is returned as structured content *and* as
JSON text, so a client that reads only one of the two still gets the whole envelope.

> **Note**
> Provider keys are read from the environment the *server* process gets, which is the
> client's environment — not your shell's. If a capability returns `auth_failed` while
> the same config works from a terminal, add the key to the client's `env` block.

### Homebrew

```bash
brew tap crAK1644/tap
brew install orchestrator-mcp-server
```

That puts `orchestrator-mcp-server` on your `PATH`, so a client can call it directly
instead of going through `uvx`:

```bash
claude mcp add orchestrator --env ORCHESTRATOR_CONFIG=$PWD/config.yaml -- orchestrator-mcp-server
```

> **Note**
> Apple Silicon pours a prebuilt bottle. Everything else builds every Python
> dependency from source, including several Rust crates, which takes around fifteen
> minutes — `uvx orchestrator-mcp-server` is the same program from a prebuilt wheel in
> about a second.

### From a checkout

```bash
uv sync && uv run pytest -q
```

```bash
claude mcp add orchestrator --env ORCHESTRATOR_CONFIG=$PWD/config.yaml -- uv run --directory $PWD orchestrator-mcp-server
```

## Documentation

Everything lives in this file and in the annotated config. The links below are the
fast path to a specific answer.

**Getting started**
- [Quick Start](#quick-start) — install into Claude Code or Codex
- [Homebrew](#homebrew) — `brew tap crAK1644/tap`
- [Configuration](#configuration) — capabilities, deployments, limits
- [`config.example.yaml`](config.example.yaml) — a commented starting point
- [System Requirements](#system-requirements)

**Using it**
- [Tools](#tools) — `ask` and `list_capabilities`
- [The response envelope](#the-response-envelope) — what every call returns
- [Error codes](#error-codes) — the closed set callers branch on
- [Consulting another agent](#consulting-another-agent) — `consult`, and the CLI path
- [The dashboard](#the-dashboard) — local page over stored consultations, and a form for
  [configuring agents](#configuring-agents-from-the-browser)

**Understanding it**
- [How It Works](#how-it-works) — why capabilities and not a classifier
- [Guardrails, and their limits](#guardrails-and-their-limits) — what is enforced, and what is not
- [Not included](#not-included) — deliberate omissions

**Development**
- [Tests](#tests) — offline suite and the live smoke script
- [Contributing](#contributing) · [Releasing](#releasing)
- [Troubleshooting](#troubleshooting) · [Bug Reports](#bug-reports)

## How It Works

A **capability** is a LiteLLM `model_name` alias group. Several deployments can share
one name, and `litellm.Router` already load-balances, retries, cools down, and falls
back across them. So the routing engine is the config file:

```yaml
model_list:
  - model_name: coding                  # capability, not a model
    litellm_params:
      model: anthropic/claude-sonnet-4-5
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: coding                  # same capability, your own box
    litellm_params:
      model: openai/qwen-coder
      api_base: http://vllm.internal:8000/v1
      api_key: os.environ/LOCAL_VLLM_KEY
```

The pieces:

- **The config file is the router.** Capabilities, deployments, fallbacks, and retry
  policy are all LiteLLM's own schema, so `litellm --config config.yaml` runs on it
  unchanged.
- **The caller states its capability.** There is no intent classifier — the caller is
  already a language model and knows whether it is asking a coding question. Paying a
  second model to guess what the first one already knows buys a cost increase and a
  new failure mode.
- **One envelope for every outcome.** Success, refusal, truncation, and timeout all
  return the same shape, so callers branch on a field instead of parsing prose.

## Tools

### `ask`

| Argument | Notes |
|---|---|
| `capability` | Enum, built from your config. Bad values are rejected by the protocol layer. |
| `prompt` | Required. Capped by `limits.max_prompt_chars`. |
| `context` | Source material. When set, the model is told to answer only from it and to abstain otherwise. |
| `system` | Extra instructions. Applied *before* the server's own directives, so it cannot disable them. Capped by `limits.max_system_chars`. |
| `response_schema` | JSON Schema (`"type": "object"`), as an object or a JSON string. Switches on structured mode. Capped by `limits.max_schema_chars` — it is inlined into the prompt verbatim. |
| `temperature` | Pinned to `0` whenever `response_schema` is set. |
| `max_output_tokens` | Capped by `limits.max_output_tokens`. |

### `list_capabilities`

What each capability is for, the deployments behind it, and where it falls back.

### The response envelope

```json
{
  "ok": true,
  "content": "…",
  "data": null,
  "insufficient_context": false,
  "capability_requested": "coding",
  "model_used": "anthropic/claude-sonnet-4-5",
  "fallback_used": false,
  "finish_reason": "stop",
  "usage": { "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost_usd": 0.0002 },
  "latency_ms": 412,
  "error": null
}
```

`content` holds the prose answer and is `null` in structured mode and on failure;
`data` holds the validated object and is set only in structured mode; `error` is
`{ code, message }` whenever `ok` is `false`.

`finish_reason` rides along on failures too, when a provider replied at all — it is
diagnosis rather than an answer, and `length` tells you to raise `max_output_tokens`
instead of going looking for a bug. It is `null` when nothing was received.

### Error codes

A closed set, so callers branch on a value instead of matching substrings.

| Code | Means |
|---|---|
| `invalid_request` | Rejected at the boundary, before any provider was called. |
| `no_deployment` | Every deployment for the capability is cooled down or absent. |
| `upstream_error` | The provider failed, or returned no usable completion. |
| `rate_limited` | The provider rate-limited the call. |
| `context_exceeded` | The request did not fit the model's context window. |
| `schema_validation_failed` | The reply never matched your schema, repairs included. |
| `timeout` | `limits.request_timeout_s` elapsed for the whole call. |
| `content_filtered` | The provider filtered the completion. |
| `auth_failed` | Bad or missing credentials. |
| `output_truncated` | The model hit the output limit mid-answer. |

`error.message` is bounded at 500 characters and never quotes the rejected output
back at you.

## Consulting another agent

`ask` talks to a **model** over HTTP. `consult` talks to an **agent runtime** — the
`codex` or `claude` CLI installed on the same machine, launched as a subprocess, already
logged into its own account. That difference is the whole point: it brings its own
subscription, its own web search, and its own conversation state, so a multi-turn
consultation with a *different vendor's* agent needs no key you do not already have.

Text goes out, structured text comes back. The consulted agent runs with its tools off,
its sandbox read-only, and no MCP config, so it can answer and nothing else.

### Enabling it

Two things: a `consult:` block (see [`config.example.yaml`](config.example.yaml), which
ships one commented out) and `ORCHESTRATOR_HOST_RUNTIME`, naming the runtime this
installation *is*.

```yaml
consult:
  database_path: ~/.orchestrator-mcp/consultations.sqlite3
  timeout_s: 180
  agents:
    codex-sol:
      runtime: codex
      command: codex
      model: gpt-5.6-sol
      priority: 10
      web_search: true
      scores: { coding: 95, research: 85, reasoning: 90 }
```

```bash
claude mcp add orchestrator --env ORCHESTRATOR_CONFIG=$PWD/config.yaml --env ORCHESTRATOR_HOST_RUNTIME=claude -- uvx orchestrator-mcp-server
```

The same server installed under Codex sets `ORCHESTRATOR_HOST_RUNTIME=codex`, and the
routing table reverses itself. That variable is read from the environment and **never**
from a tool argument: an agent that could name the host runtime could name someone
else's and hand the work straight back to itself. Configure `consult:` without it and the
server refuses to start.

No `consult:` block, no change: the server advertises exactly `ask` and
`list_capabilities`, and a snapshot test asserts that schema byte for byte so the
second path cannot reshape the first one under it. The snapshot is a regression guard
generated from the current code, not a copy of 0.1.2's — the `ask` schema did move
once between them, deliberately: `response_schema`'s string arm now advertises
`maxLength: max_schema_chars`, and that cap is enforced before the JSON is parsed
rather than after. A 0.1.2 caller sending a schema inside the cap sees no difference.

### Tools

| Tool | What it does |
|---|---|
| `consult` | Route a question to a configured agent and return one envelope. |
| `list_consult_agents` | Configured agents, their scores, and whether each is installed and logged in. |
| `get_consultation` | A stored consultation: its turns, usage, and why that agent was chosen. |

`consult` arguments:

| Argument | Notes |
|---|---|
| `capability` | One of `coding`, `research`, `writing`, `reasoning`, `review`. A fixed vocabulary, not your `capabilities:` block — a score only means something against a name that means the same thing everywhere. |
| `prompt` | Required. Capped at 100,000 characters. |
| `context` | Source material, framed to the target as untrusted evidence rather than instructions. Capped at 1,000,000. |
| `source_mode` | `auto` (default) → `document` when `context` is set, else `model`. `document` grounds the answer in `context` with every tool off. `web` enables only the target's own web search. `model` uses neither. |
| `consultation_id` | Omit on the first call; send back the one you got to continue the same session. |
| `target_agent` | Optional explicit agent id, advertised as an enum of *your* configured ids. |
| `conversation_label` | Free text, recorded with the consultation. |

Routing is deterministic and operator-controlled: disabled agents are dropped, every
agent whose runtime equals the host's is dropped, then score descending, priority
ascending, id ascending. There is no fallback to a second candidate — if the chosen
agent is not logged in, that is the answer you get, not a quiet substitution.

### Keep the `consultation_id`

MCP gives a server no reliable identifier for the host's own conversation, so the
returned `consultation_id` **is** the binding mechanism. Send it back on every later call
about the same topic and the same native CLI session continues; omit it and each call
starts a fresh one that remembers nothing. The tool description says so to the calling
model, but a host that drops the field will silently get single-turn behaviour.

A consultation is pinned to the agent, runtime, and model it started on. Pointing a
resumed one at a different target is `session_target_mismatch`, never a silent restart
somewhere else.

### The envelope

```json
{
  "ok": true,
  "consultation_id": "6f1c…",
  "content": {
    "answer": "…",
    "assumptions": [],
    "uncertainties": ["whether the index is hot"],
    "follow_up_questions": ["how large is the table?"],
    "sources": [{ "title": "model", "locator": "internal", "source_type": "model" }]
  },
  "capability_requested": "coding",
  "source_mode_used": "model",
  "route": { "agent_id": "codex-sol", "runtime": "codex", "model": "gpt-5.6-sol",
             "capability_score": 95, "priority": 10, "explicitly_selected": false },
  "usage": { "prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020 },
  "latency_ms": 8412,
  "error": null
}
```

Every content field is required, and arrays may be empty but never absent: "no
assumptions" is a claim the consulted agent has to make, not one you have to infer from a
missing key. When `ok` is `false`, `content` is `null` — a failed consultation never
carries text that could read as an answer.

### Consult error codes

| Code | Means |
|---|---|
| `invalid_request` | Rejected at the boundary — `document` mode with no `context`, an unknown agent id. |
| `no_agent_available` | No enabled, non-host agent scores above zero for that capability. |
| `agent_not_installed` | The configured `command` is not on `PATH`. |
| `connection_required` | The CLI is installed but not logged in. Carries `required_action.command` — for *you* to run, never the server. |
| `configured_model_unavailable` | The CLI answered as a different model than the one configured. |
| `agent_unavailable` | The CLI reported the turn failed, or exited nonzero. |
| `session_not_found` | The native session behind that `consultation_id` is gone. Start a new consultation. |
| `session_busy` | Another process holds the lease on that consultation. |
| `session_target_mismatch` | A resumed consultation was pointed at a different agent, runtime, or model. |
| `protocol_validation_failed` | The reply did not match the contract, or the agent tried to act — a command, a file change, an MCP call, a subagent. |
| `web_search_unavailable` | `source_mode: web` against an agent without `web_search: true`. |
| `transport_error` | The CLI produced nothing usable. |
| `timeout` | `consult.timeout_s` elapsed. The child's whole process group is killed. |

### What this path enforces

- **It never consults itself.** The host runtime is excluded at routing *and* refused on
  resume, so a stored consultation cannot become a loop back into the caller.
- **No silent substitution.** Not of the agent — there is no fallback candidate — and not
  of the model: both adapters check what the CLI reports it actually used against what
  you configured, and disagreement is `configured_model_unavailable`. Names are compared
  as token runs, so `claude-sonnet-4` does not match `claude-sonnet-4-5` and `gpt-5.1`
  does not match `gpt-5.10`; a dated snapshot of the version you pinned does match, and a
  bare alias like `opus` has no version to disagree with.
- **The consulted agent may not act.** Codex runs `--sandbox read-only
  --ignore-user-config --ignore-rules` with `approval_policy="never"`, shell, and
  subagents off — as `-c` config keys, because a 0.146 build rejects
  `--ask-for-approval` on `exec` outright; Claude Code runs `--safe-mode --strict-mcp-config --tools ""`. Any action event in
  the stream fails the turn closed rather than being ignored.
- **No credentials, ever.** Preflight reads one bit: `codex login status`'s exit code, or
  the `loggedIn` boolean out of `claude auth status --json`. Codex's login output is not
  even captured, and no part of either payload has a column to be stored in.
  Authentication is something you do in your own terminal — the server only ever tells
  you which command to run.
- **Web mode is bounded by turns, not just by time.** Claude Code has no `--max-turns`,
  so the event stream is counted here and the child is killed past `web_turn_limit`. The
  turn that spends the last of the budget is allowed to finish and answer; the one after
  it is not, and a consultation stopped there returns an error rather than an answer
  assembled from partial turns.
- **No shell.** Every invocation is `create_subprocess_exec` with an argument list, and
  the prompt travels over stdin, so there is neither an injection surface nor an argv
  ceiling. Children get their own process group and a timeout kills the group.
- **Failures are envelopes.** Including the ones that are not this server's fault.

The database at `database_path` holds every prompt and every answer verbatim — directory
`0700`, file `0600`, set before anything is written. Set `store_full_content: false` to
keep metadata and routing only. Nothing from a subprocess environment is stored at all.

### The dashboard

Off by default. `dashboard.enabled: true`, then:

```bash
ORCHESTRATOR_CONFIG=config.yaml orchestrator-mcp-dashboard
```

A page over the same SQLite file: agents and their last status check, the last 200
consultations, and per turn the compiled prompt, the answer, latency, usage, and any
error. It opens the database `mode=ro`, refuses to bind anywhere but loopback, and
rejects a request whose `Host` is not one — because what it serves is every prompt you
have ever sent.

### Configuring agents from the browser

Writing an agent by hand means knowing that `scores` uses a fixed five-word vocabulary,
that `reasoning_effort` is codex-only, and that the Codex CLI ships inside ChatGPT.app
rather than on `PATH`. The form knows all three. Turn it on with a second flag:

```yaml
consult:
  dashboard:
    enabled: true
    editable: true
```

Then `http://127.0.0.1:8765/agents` can add, change and delete agents.

**It never edits `config.yaml`.** Agents you add here are written to
`~/.orchestrator-mcp/agents.yaml` (`managed_agents_path`), so a click cannot reformat
your config or drop its comments. The two files are merged at boot and everything
downstream — routing, the config hash, the store — sees one set of agents. An id defined
in both is a **startup error**, not a merge: a precedence rule is how an edit comes to
save successfully and do nothing. Agents from `config.yaml` are listed read-only.

The MCP server reads its config once at boot and the dashboard is a separate process, so
a save takes effect when that server next starts — restart Claude Code. The page says so,
and it can tell when it matters: every consultation records the config hash that produced
it, so if the last one ran on a different configuration you get a banner rather than a
guess.

What a save can do is deliberately small. It validates the agent exactly as boot would,
checks the command resolves — a `which`, not a subprocess — and writes one file
atomically, `0600`, in a directory it creates `0700`. A directory that already exists
keeps the mode it has: `managed_agents_path` can name any file, its parent might be
`$HOME`, and tightening someone else's directory behind their back is not this program's
call. A save never runs a login command, never starts a consultation, and never touches
anything else in your config. Writes carry a per-process token, because loopback is not a
boundary a browser respects.

It also refuses to write a file the server could not then boot on. If a hand-edit left a
blank id, a malformed entry, or an id that `config.yaml` also defines beside the agent you
are changing, the save says so and changes nothing — rewriting the file would put the
dashboard's name on an entry it cannot fix, and the next start would refuse.

The duplicate half of that check reads `config.yaml` again rather than trusting what this
process loaded, so an agent you add to that file by hand is refused here without a restart,
and one you delete from it stops being refused. The read-only table follows the same file:
an agent you delete from `config.yaml` stops being listed there, which is what makes moving
one into the dashboard leave a single row rather than two contradictory ones. Only the ids
are re-read, so the traffic goes one way — an agent *added* to `config.yaml` while the page
is open is not in that table until the dashboard restarts, but it is enough to keep a save
from writing something the next boot rejects. The row the dashboard owns stays visible and
deletable while a duplicate exists, since deleting it is the only fix available from here.

An empty or half-written `config.yaml` — the state an editor leaves for a moment when it
truncates before writing — is not read as "that file defines no agents". The check falls
back to what the server booted with, because guessing "empty" during that window is exactly
how it would wave through the duplicate it exists to catch.

One dashboard writes that file. The lock that makes two simultaneous saves safe belongs to
the process holding it, so pointing two dashboards at one `managed_agents_path` is not a
supported arrangement and can still lose an edit between them.

## System Requirements

- Python 3.11, 3.12, or 3.13
- [`uv`](https://docs.astral.sh/uv/) — or any installer, if you would rather use pip
- An MCP client that speaks stdio (Claude Code, Codex, or your own)
- At least one provider you hold credentials for, or a local endpoint
- Network access to whatever your `config.yaml` points at — nothing else phones home
- For [`consult`](#consulting-another-agent) only: the `codex` and/or `claude` CLI
  installed and logged in, and `ORCHESTRATOR_HOST_RUNTIME` set. Nothing else changes if
  you leave that block out.

A local Ollama endpoint works and costs nothing, which makes it a reasonable way to
try the server before wiring up paid providers:

```yaml
model_list:
  - model_name: fast
    litellm_params:
      model: ollama_chat/qwen2.5:7b
      api_base: http://localhost:11434
```

## Configuration

`ORCHESTRATOR_CONFIG` points at the file; it defaults to `config.yaml` in the working
directory. Keep it out of version control — it holds your endpoints.

```yaml
capabilities:
  coding: "Writing, refactoring, reviewing, and debugging code."
  fast: "Cheap, low-latency answers. Classification, extraction, short replies."

model_list:
  - model_name: coding
    litellm_params:
      model: anthropic/claude-sonnet-4-5
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: fast
    litellm_params:
      model: anthropic/claude-haiku-4-5-20251001
      api_key: os.environ/ANTHROPIC_API_KEY

router_settings:
  num_retries: 2
  cooldown_time: 60
  fallbacks:
    - coding: [fast]
```

Keys are referenced as `os.environ/NAME` and read at request time. Never inline the
value.

### Limits

Every one of these is a boundary the caller cannot cross, checked before a provider is
called. A nonsensical value fails at startup rather than mid-request.

| Key | Default | What it bounds |
|---|---|---|
| `max_prompt_chars` | `100000` | The `prompt` argument |
| `max_context_chars` | `400000` | The `context` argument |
| `max_system_chars` | `10000` | Caller instructions, which reach the prompt verbatim |
| `max_schema_chars` | `20000` | `response_schema`, which is inlined verbatim |
| `max_output_tokens` | `4096` | Completion length |
| `request_timeout_s` | `120` | The **whole** call — retries, fallback, and repairs share it |
| `schema_repair_attempts` | `1` | Retries given to a schema-violating reply |

## Guardrails, and their limits

This server sees a prompt and a completion. It has no ground truth, so it **cannot**
verify factual claims, and nothing here should be read as a hallucination detector.
What it does enforce:

- **Shape is validated, not assumed.** Structured replies are checked against your
  schema locally with `jsonschema`, regardless of whether the provider claims to
  enforce `response_format`. A violation is a failure, not a payload.
- **Bounded repair.** An invalid structured reply gets `limits.schema_repair_attempts`
  retries carrying the validator's complaint, then fails as
  `schema_validation_failed`. Never a best-effort half-parsed object.
- **An unfinished answer is a failure, not a short answer.** A completion cut off by
  the token limit comes back as `output_truncated` with `content: null`, and one the
  provider filtered as `content_filtered`. Neither is returned as prose, because a
  half answer reads exactly like a whole one. The same applies to a reply that stopped
  to call a tool (this server exposes none) and to the provider reasons LiteLLM
  normalizes to a plain `stop` — a malformed function call, an unspecified reason —
  which are read from the native reason it keeps alongside.
- **The error tells you what broke, not what the model wrote.** `error.message` gives
  the failing path and constraint (`schema violation at answer/city: failed the
  'maxLength' constraint`) and is capped at 500 characters. The rejected value itself
  goes only back to the model that produced it, in the repair turn. Anything
  credential-shaped in that message — an API key, a `Bearer` header, a PEM block a
  provider quoted back — is replaced with `[redacted]` on both paths before the
  envelope leaves. It is a second line of defence and not a guarantee: a secret with
  no recognizable shape survives it.
- **`request_timeout_s` bounds the call.** Retries, cross-capability fallback, and
  repair turns all spend from one budget, so `120` cannot become 360. Each leg is
  given slightly less than what is left of that budget, so a hung deployment times out
  inside LiteLLM — which counts the failure and cools it down — instead of being
  cancelled from outside, which would leave the next request to pick it again. What it
  does not bound is CPU spent inside this process: validating a reply against a
  caller-supplied `response_schema` runs `jsonschema`, and a pathological pattern in
  that schema can backtrack for far longer than the deadline. `re` holds the GIL, so
  no timeout and no thread rescues it. `max_schema_chars` limits the size of what a
  caller can send, not what it can cost — treat `response_schema` as trusted input.
- **Abstention is typed.** With `context` set, the model is given an explicit way to
  say the material does not support an answer; it arrives as `insufficient_context`,
  not as prose you have to pattern-match.
- **The server never ghostwrites.** When `ok` is `false`, `content` and `data` are
  both `null`. It will not put a "Sorry, I couldn't…" string where a model's answer
  goes, because callers cannot tell those apart. Enforced by an assertion on every
  response and covered by tests.
- **Degradation is visible.** `fallback_used` and `model_used` always ride along, so
  an answer served by the backup after the primary died never passes as the intended
  one.
- **The caller cannot smuggle a model.** There is no free-form model parameter, only
  the capability enum. Routing stays operator-controlled.
- **Boundaries reject early.** Unknown capability, oversized prompt or `system`, empty
  prompt, and a malformed or oversized `response_schema` all fail before a provider is
  called.

Three known gaps. The MCP SDK drops unknown arguments before the handler sees them, so
an unrecognized key is ignored at the protocol layer rather than rejected — direct
calls into `Orchestrator.ask` do reject it. A `response_schema` containing a
pathological `pattern` can burn CPU on the event loop during validation: the schema is
size-capped but not analyzed, so treat schema authorship as a trusted operation. And
when a provider call raises, `error.message` carries the first 500 characters of the
provider's own exception text, which some providers fill with the API base, the
offending request fragment, or a credential's environment variable name — and during a
repair turn the request contains the model's previously rejected output. That text is
diagnosis worth keeping for a caller that already reads your config; it is not safe to
forward somewhere your config is not.

## Tests

```bash
uv run pytest -q
```

315 tests, no network and no CLI installed. Deployments are stubbed with LiteLLM's
`mock_response`, and the shapes it cannot express (no choices, null content, a truncated
or filtered reply) are stubbed as raw `ModelResponse` objects. Includes the
rate-limit-then-fallback path and the cooled-down-group path.

The consult path is stubbed the same way: `codex` and `claude` are replaced with small
Python scripts on `PATH` that replay recorded event streams — success, resume, a
substituted model, a tool-use event, a stream that never ends, one that ignores `SIGTERM`
so the process-group kill has to be proven, and one that exits at once leaving a
grandchild behind, because a reaped leader is exactly the case a kill routed by pid
lookup misses. Cross-process behaviour (leases, migrations)
is tested with real OS processes rather than tasks, because an in-process lock would pass
either way.

Because all of that is stubbed, it proves this server's logic and nothing about your
providers or your CLIs. For those:

```bash
uv run python smoke_live.py
```

Real calls against your `config.yaml`, roughly four short ones per capability, so it
costs a little money — run it deliberately, not in CI. It checks the things only a
live endpoint can answer: whether `response_format` survives the round trip, whether
the model honours the abstention path instead of inventing, and what the provider
actually sends as `finish_reason` when it runs out of room. Name capabilities as
arguments to check only some (`uv run python smoke_live.py fast`).

```bash
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_consult_live.py
```

The same deal for the consult path, and the only thing that confirms the real CLI flags
and event names: it needs both CLIs installed and logged in, and it spends whatever your
subscriptions charge. Also not for CI.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `config not found: config.yaml` | No config where the server is looking. Set `ORCHESTRATOR_CONFIG` to an absolute path — a client launches the server from its own working directory, not yours. |
| `auth_failed` from the client, but the same config works in a terminal | The key is in your shell, not the client's. Add it to the client's `env` block. |
| Startup fails naming a capability | A capability has no deployment, a deployment names an undeclared capability, or a fallback points at one that does not exist. |
| `no_deployment` | Every deployment in the group is cooling down after failures. `router_settings.cooldown_time` controls how long. |
| `output_truncated` | The answer did not fit. Raise `max_output_tokens`, in the request or in `limits`. |
| `schema_validation_failed` after repairs | The model cannot produce your schema. Simplify it, or route the capability at a model that supports `response_format` natively. |
| `timeout` on a local model | `request_timeout_s` covers the entire call including model load. A 7B model on a laptop wants more than the default. |

Consult path:

| Symptom | Cause |
|---|---|
| `ORCHESTRATOR_HOST_RUNTIME must be set to codex or claude` at startup | The `consult:` block is present and the variable is not. Add it to the *client's* `env` block, not your shell. |
| `consult` is not advertised at all | No `consult:` block in the config the server actually loaded. Check `ORCHESTRATOR_CONFIG` is an absolute path. |
| `agent_not_installed` for a CLI you can run yourself | The client's `PATH` is not your shell's — a GUI-launched client often has neither `~/.local/bin` nor a version manager's shims. Use an absolute path in `command`. |
| `connection_required` | Run the command in `error.required_action.command` in your own terminal, then retry. The server will not log anything in on your behalf. |
| `no_agent_available` with agents configured | Every candidate is disabled, scores 0 for that capability, or runs the host's own runtime. `list_consult_agents` shows which. |
| `configured_model_unavailable` | The CLI answered as something other than `model:`. Either it fell back internally, or the configured string does not match what that CLI calls the model. |
| Each call starts over, no memory of the last one | The host is not sending `consultation_id` back. It is the only binding there is — see [Keep the `consultation_id`](#keep-the-consultation_id). |
| `session_busy` | Another process holds the lease on that consultation, or one died mid-turn. Leases expire; wait it out or start a new consultation. |
| `protocol_validation_failed` mentioning a tool or command | The consulted agent tried to act instead of answer. The turn is refused on purpose; the answer it produced alongside is not returned. |
| Dashboard exits with `dashboard is disabled` | `consult.dashboard.enabled` is `false`. It serves every stored prompt, so it stays off until you say otherwise. |
| `/agents` answers 403 `Read-only` | `consult.dashboard.editable` is `false`. Viewing and editing are separate opt-ins. |
| Server refuses to boot: agent defined in both the config and `agents.yaml` | The same id is in `config.yaml` and the dashboard's file. Delete one — the server will not pick, because the copy it ignored would look saved to whoever wrote it. |
| Saved an agent, `list_consult_agents` does not show it | The MCP server read its config at boot. Restart Claude Code. |
| A save is refused naming another agent in `agents.yaml` | A hand-edit left an entry that will not boot. The dashboard will not rewrite the file around it — fix that entry, or delete it, and save again. |
| A save is refused because the agent is "already configured here" | You are on **Add an agent** with an id that already exists. Use its **edit** link; adding it again would replace it without saying so. |

## Bug Reports

Open an issue with the envelope you got back — it carries `error.code`, `model_used`,
`fallback_used`, `finish_reason`, and `latency_ms`, which is most of a diagnosis
already. Add your `config.yaml` with the keys removed.

For anything that looks like a routing or retry problem, LiteLLM's own trace is the
useful attachment:

```bash
LITELLM_LOG=DEBUG uv run python smoke_live.py 2>debug.log
```

It writes to stderr only, so stdout stays clean and the MCP stream is unaffected.

## Contributing

Issues and pull requests are welcome. The bar for a change is a test that fails
without it — the suite runs offline, so there is no key to obtain and no cost to pay.
Keep `config.yaml` out of your commits.

1. Fork and branch.
2. Make the change.
3. Add the test that fails without it.
4. `uv run pytest -q`.
5. Open the pull request.

If you are adding a capability to your own setup, you do not need a pull request: it
is a `model_list` entry.

### Releasing

Bump `version` in `pyproject.toml`, then publish a GitHub Release tagged `vX.Y.Z`.
[`release.yml`](.github/workflows/release.yml) runs the suite, checks the tag against
the packaged version, and uploads to PyPI.

There is no API token in this repo. PyPI is configured as a Trusted Publisher for this
workflow, so it mints a short-lived credential from the job's OIDC identity — nothing
to store, nothing to rotate, nothing to leak.

Once the release is on PyPI, the [tap](https://github.com/crAK1644/homebrew-tap) needs
the new version too. In a branch of that repo, point the formula's `url` and `sha256` at
the new sdist and delete the `bottle do` block — its `root_url` names the previous
version's release, so leaving it sends installs to a tarball that is not there. Open a
pull request: `brew test-bot` builds the bottle and uploads it as an artifact. Then run
the `brew pr-pull` workflow with that pull request's number, which writes the new
`bottle do` block, attaches the bottle to a release named `orchestrator-mcp-server-X.Y.Z`,
and pushes to the tap's `main`.

Bottles are Apple Silicon only. x86 macOS reports "unbottled dependencies, so a bottle
will not be built" — homebrew-core no longer bottles parts of this dependency tree for
it — and both it and Linux build from source instead.

## Not included

Semantic/embedding routing and RouteLLM-style predictive routing (the caller states
its capability); Redis-backed distributed cooldown state (single process — LiteLLM
enables it via config when you need a second node); streaming (MCP tool results return
whole); `sampling/createMessage` loops; PII redaction and telemetry callbacks
(available as LiteLLM callbacks when a requirement names one).

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it in something commercial. The
only thing it asks is that the copyright notice travels with the code.

## Support

- [Issues](https://github.com/crAK1644/orchestrator-mcp/issues) — bugs and feature requests
- [PyPI](https://pypi.org/project/orchestrator-mcp-server/) — releases
- [LiteLLM docs](https://docs.litellm.ai/docs/routing) — routing, fallbacks, and cooldowns
- [Model Context Protocol](https://modelcontextprotocol.io) — the protocol itself

---

Built on [LiteLLM](https://github.com/BerriAI/litellm), [Pydantic](https://docs.pydantic.dev),
and the [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
