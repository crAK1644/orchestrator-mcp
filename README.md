# orchestrator-mcp

#### Capability-routed MCP server: send each request to the model configured for that kind of work

[![PyPI](https://img.shields.io/pypi/v/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Tests](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml)

**[Quick Start](#quick-start)** · **[How It Works](#how-it-works)** · **[Tools](#tools)** ·
**[Configuration](#configuration)** · **[Guardrails](#guardrails-and-their-limits)** ·
**[Troubleshooting](#troubleshooting)** · **[Contributing](#contributing)**

Research to one model, coding to another, cheap extraction to a third — and every answer
comes back through the same validated envelope. Pointing a capability at your own
deployment is a YAML edit. There is no code to change.

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

## System Requirements

- Python 3.11, 3.12, or 3.13
- [`uv`](https://docs.astral.sh/uv/) — or any installer, if you would rather use pip
- An MCP client that speaks stdio (Claude Code, Codex, or your own)
- At least one provider you hold credentials for, or a local endpoint
- Network access to whatever your `config.yaml` points at — nothing else phones home

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
  half answer reads exactly like a whole one.
- **The error tells you what broke, not what the model wrote.** `error.message` gives
  the failing path and constraint (`schema violation at answer/city: failed the
  'maxLength' constraint`) and is capped at 500 characters. The rejected value itself
  goes only back to the model that produced it, in the repair turn.
- **`request_timeout_s` bounds the call.** Retries, cross-capability fallback, and
  repair turns all spend from one budget, so `120` cannot become 360.
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

Two known gaps. The MCP SDK drops unknown arguments before the handler sees them, so
an unrecognized key is ignored at the protocol layer rather than rejected — direct
calls into `Orchestrator.ask` do reject it. And a `response_schema` containing a
pathological `pattern` can burn CPU on the event loop during validation: the schema is
size-capped but not analyzed, so treat schema authorship as a trusted operation.

## Tests

```bash
uv run pytest -q
```

76 tests, no network — deployments are stubbed with LiteLLM's `mock_response`, and the
shapes it cannot express (no choices, null content, a truncated or filtered reply) are
stubbed as raw `ModelResponse` objects. Includes the rate-limit-then-fallback path and
the cooled-down-group path.

Because all of that is stubbed, it proves the orchestrator's logic and nothing about
your providers. For that:

```bash
uv run python smoke_live.py
```

Real calls against your `config.yaml`, roughly four short ones per capability, so it
costs a little money — run it deliberately, not in CI. It checks the things only a
live endpoint can answer: whether `response_format` survives the round trip, whether
the model honours the abstention path instead of inventing, and what the provider
actually sends as `finish_reason` when it runs out of room. Name capabilities as
arguments to check only some (`uv run python smoke_live.py fast`).

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
