# orchestrator-mcp

An MCP server that routes each request to the model configured for that kind of work —
research to one model, coding to another — and returns every answer through a fixed,
validated envelope.

Pointing a capability at your own deployment is a YAML edit. There is no code to change.

## How it works

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

The calling agent states which capability it wants. There is no intent classifier —
the caller is already a language model and knows whether it is asking a coding
question; paying a second model to guess what the first one already knows buys a cost
increase and a new failure mode.

## Quick start

Write a `config.yaml` — start from [`config.example.yaml`](config.example.yaml) — and
check that it loads:

```bash
ORCHESTRATOR_CONFIG=config.yaml uvx --from orchestrator-mcp python -c "from orchestrator_mcp.server import build_server; build_server(); print('config ok')"
```

A bad config fails here rather than at request time: every deployment must route to a
declared capability, every capability must have a deployment behind it, and every
fallback must name a real capability.

Because the file is LiteLLM's own config schema, `litellm --config config.yaml` runs on
it unchanged. Keep it out of version control — it holds your endpoints.

### Claude Code

```bash
claude mcp add orchestrator --env ORCHESTRATOR_CONFIG=$PWD/config.yaml -- uvx orchestrator-mcp
```

### Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.orchestrator]
command = "uvx"
args = ["orchestrator-mcp"]
env = { ORCHESTRATOR_CONFIG = "/absolute/path/to/config.yaml" }
```

Both speak stdio, and every tool result is returned as structured content *and* as
JSON text, so a client that reads only one of the two still gets the whole envelope.

**Provider keys.** `config.yaml` references them as `os.environ/NAME`, and they are
read from the environment the *server* process gets — which is the client's
environment, not your shell's. If a capability comes back `auth_failed` while the same
config works from a terminal, add the key to the client's `env` block.

### From a checkout

```bash
uv sync && uv run pytest -q
```

```bash
claude mcp add orchestrator --env ORCHESTRATOR_CONFIG=$PWD/config.yaml -- uv run --directory $PWD orchestrator-mcp
```

## Tools

### `ask`

| Argument | Notes |
|---|---|
| `capability` | Enum, built from your config. Bad values are rejected by the protocol layer. |
| `prompt` | Required. Capped by `limits.max_prompt_chars`. |
| `context` | Source material. When set, the model is told to answer only from it and to abstain otherwise. |
| `system` | Extra instructions. Applied *before* the server's own directives, so it cannot disable them. Capped by `limits.max_system_chars`. |
| `response_schema` | JSON Schema (`"type": "object"`). Switches on structured mode. Capped by `limits.max_schema_chars` — it is inlined into the prompt verbatim. |
| `temperature` | Pinned to `0` whenever `response_schema` is set. |
| `max_output_tokens` | Capped by `limits.max_output_tokens`. |

Every call returns the same envelope:

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

`error.code` comes from a closed set — `invalid_request`, `no_deployment`,
`upstream_error`, `rate_limited`, `context_exceeded`, `schema_validation_failed`,
`timeout`, `content_filtered`, `auth_failed`, `output_truncated` — so callers branch
on a value instead of matching substrings. `error.message` is bounded at 500
characters and never quotes the rejected output back at you.

### `list_capabilities`

What each capability is for, the deployments behind it, and where it falls back.

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
  called. A nonsensical `limits:` block fails at startup instead.

Two known gaps. The MCP SDK drops unknown arguments before the handler sees them, so
an unrecognized key is ignored at the protocol layer rather than rejected — direct
calls into `Orchestrator.ask` do reject it. And a `response_schema` containing a
pathological `pattern` can burn CPU on the event loop during validation: the schema is
size-capped but not analyzed, so treat schema authorship as a trusted operation.

## Tests

```bash
uv run pytest -q
```

72 tests, no network — deployments are stubbed with LiteLLM's `mock_response`, and the
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

## Contributing

Issues and pull requests are welcome. The bar for a change is a test that fails
without it — the suite runs offline, so there is no key to obtain and no cost to pay.
Keep `config.yaml` out of your commits.

If you are adding a capability to your own setup, you do not need a pull request: it
is a `model_list` entry.

## Not included

Semantic/embedding routing and RouteLLM-style predictive routing (the caller states
its capability); Redis-backed distributed cooldown state (single process — LiteLLM
enables it via config when you need a second node); streaming (MCP tool results return
whole); `sampling/createMessage` loops; PII redaction and telemetry callbacks
(available as LiteLLM callbacks when a requirement names one).
