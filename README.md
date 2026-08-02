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

```bash
uv sync
```

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`, export the API keys it references, then check it loads:

```bash
ORCHESTRATOR_CONFIG=config.yaml uv run python -c "from orchestrator_mcp.server import build_server; build_server(); print('config ok')"
```

A bad config fails here rather than at request time: every deployment must route to a
declared capability, every capability must have a deployment behind it, and every
fallback must name a real capability.

Register it with a client:

```bash
claude mcp add orchestrator --env ORCHESTRATOR_CONFIG=$PWD/config.yaml -- uv run --directory $PWD orchestrator-mcp
```

`config.yaml` is gitignored — it holds your endpoints. Only the example is tracked.
Because the file is LiteLLM's own config schema, `litellm --config config.yaml` runs
on it unchanged.

## Tools

### `ask`

| Argument | Notes |
|---|---|
| `capability` | Enum, built from your config. Bad values are rejected by the protocol layer. |
| `prompt` | Required. Capped by `limits.max_prompt_chars`. |
| `context` | Source material. When set, the model is told to answer only from it and to abstain otherwise. |
| `system` | Extra instructions. Applied *before* the server's own directives, so it cannot disable them. |
| `response_schema` | JSON Schema (`"type": "object"`). Switches on structured mode. |
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
`timeout`, `content_filtered`, `auth_failed` — so callers branch on a value instead of
matching substrings.

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
- **Boundaries reject early.** Unknown capability, oversized prompt, empty prompt, and
  malformed `response_schema` all fail before a provider is called.

One known gap: the MCP SDK drops unknown arguments before the handler sees them, so an
unrecognized key is ignored at the protocol layer rather than rejected. Direct calls
into `Orchestrator.ask` do reject it.

## Tests

```bash
uv run pytest -q
```

37 tests, no network — deployments are stubbed with LiteLLM's `mock_response`,
including the rate-limit-then-fallback path.

## Not included

Semantic/embedding routing and RouteLLM-style predictive routing (the caller states
its capability); Redis-backed distributed cooldown state (single process — LiteLLM
enables it via config when you need a second node); streaming (MCP tool results return
whole); `sampling/createMessage` loops; PII redaction and telemetry callbacks
(available as LiteLLM callbacks when a requirement names one).
