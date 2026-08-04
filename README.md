# Orchestrator MCP

Use different AI models and agent subscriptions through one MCP server.

[![PyPI](https://img.shields.io/pypi/v/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/orchestrator-mcp-server)](https://pypi.org/project/orchestrator-mcp-server/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Tests](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml)

If you pay for more than one AI service, Orchestrator MCP helps you use each one for what it does best. It routes work automatically, prevents an agent from consulting itself, and keeps a record of every cross-agent consultation.

It gives your MCP client two equally useful ways to get help:

- **Ask a model:** send coding, research, writing, or other work to the model you assigned to that capability.
- **Consult another agent:** let Claude Code ask Codex, or let Codex ask Claude Code, using the subscriptions already signed in on your computer.

An optional local dashboard lets you review consultations and manage agents.

## Quick start

### 1. Create a configuration file

Copy [`config.example.yaml`](config.example.yaml) to `config.yaml`, then add your models and API keys.

Here is a small example:

```yaml
capabilities:
  coding: "Write, review, and debug code."
  research: "Research and summarize information."

model_list:
  - model_name: coding
    litellm_params:
      model: anthropic/claude-sonnet-4-5
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: research
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
```

Use environment variable references such as `os.environ/OPENAI_API_KEY`. Do not put real keys in `config.yaml`.

### 2. Install with Homebrew

```bash
brew tap crAK1644/tap
brew install orchestrator-mcp-server
```

Apple Silicon uses a prebuilt package. Intel macOS and Linux build the dependencies from source, which can take about 15 minutes. On those systems, `uvx` is usually faster.

### 3. Add it to your MCP client

For Claude Code:

```bash
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$PWD/config.yaml \
  -- orchestrator-mcp-server
```

For Codex, add this to `~/.codex/config.toml`:

```toml
[mcp_servers.orchestrator]
command = "orchestrator-mcp-server"
env = { ORCHESTRATOR_CONFIG = "/absolute/path/to/config.yaml" }
```

Restart your MCP client after changing its configuration.

### Install with `uvx` instead

You can run the same server without installing it permanently:

```bash
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$PWD/config.yaml \
  -- uvx orchestrator-mcp-server
```

For Codex:

```toml
[mcp_servers.orchestrator]
command = "uvx"
args = ["orchestrator-mcp-server"]
env = { ORCHESTRATOR_CONFIG = "/absolute/path/to/config.yaml" }
```

The project is published to PyPI as `orchestrator-mcp-server`. The shorter PyPI name belongs to another project.

## What it does

### Ask the right model

The `ask` tool takes a capability such as `coding` or `research`. Orchestrator MCP sends the request to the model configured for that capability.

The caller chooses the type of work. Orchestrator does not use another model to guess. LiteLLM handles load balancing, retries, cooldowns, and fallbacks between deployments.

Every result uses the same response shape. It includes the answer or error, the model used, whether a fallback was used, token usage, cost when available, and latency.

Available tools:

| Tool | Purpose |
|---|---|
| `ask` | Ask a model assigned to a capability. |
| `list_capabilities` | Show the available capabilities, models, and fallbacks. |

`ask` can also:

- answer only from supplied `context`;
- return data that matches a JSON Schema;
- accept extra `system` instructions;
- limit temperature and output length.

### Consult another agent

The `consult` tool starts the Codex or Claude Code command-line app already installed and signed in on your computer. This lets one agent ask another vendor's agent without needing a second API key.

The consulted agent can answer, but it cannot change files, run commands, use MCP tools, or start subagents. Orchestrator also removes agents that use the same runtime as the caller, which prevents consultation loops.

Available tools:

| Tool | Purpose |
|---|---|
| `consult` | Ask a configured Codex or Claude Code agent. |
| `list_consult_agents` | Show configured agents, scores, and login status. |
| `get_consultation` | Read a saved consultation, including its turns and routing details. |

To enable these tools, add a `consult` section:

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
      scores:
        coding: 95
        research: 85
        reasoning: 90
```

Then tell the server which agent runtime is hosting it.

For Claude Code:

```bash
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$PWD/config.yaml \
  --env ORCHESTRATOR_HOST_RUNTIME=claude \
  -- orchestrator-mcp-server
```

For Codex:

```toml
[mcp_servers.orchestrator]
command = "orchestrator-mcp-server"
env = { ORCHESTRATOR_CONFIG = "/absolute/path/to/config.yaml", ORCHESTRATOR_HOST_RUNTIME = "codex" }
```

`ORCHESTRATOR_HOST_RUNTIME` must be `claude` or `codex`. If you enable `consult` without setting it, the server will not start.

The first `consult` call returns a `consultation_id`. Send that ID with later calls to continue the same conversation. Without it, every call starts a new conversation.

Consultations support five capabilities: `coding`, `research`, `writing`, `reasoning`, and `review`. Routing is predictable: the highest score wins, then the lowest priority number, then the agent ID. Orchestrator does not silently switch to another agent if the selected one fails.

See [`config.example.yaml`](config.example.yaml) for every consultation option.

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

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The dashboard only listens on your computer. It shows configured agents, recent consultations, prompts, answers, routing, usage, latency, and errors.

To add and edit agents from the browser, set `editable: true` and open `/agents`. Browser-managed agents are stored in `~/.orchestrator-mcp/agents.yaml`; the dashboard never rewrites `config.yaml` and never runs login commands. Restart the MCP server after saving an agent.

By default, consultation prompts and answers are saved in SQLite. Set `store_full_content: false` to save only metadata and routing information.

## Configuration

`ORCHESTRATOR_CONFIG` points to the configuration file. If it is not set, the server looks for `config.yaml` in its working directory.

Important sections:

| Section | Purpose |
|---|---|
| `capabilities` | Names and explains the work types available to `ask`. |
| `model_list` | Connects each capability to one or more LiteLLM models. |
| `router_settings` | Controls retries, cooldowns, and fallbacks. |
| `limits` | Sets request size, output, repair, and timeout limits. |
| `consult` | Configures CLI agents, history, and the dashboard. |

Several deployments may use the same capability name. LiteLLM will balance requests between them.

You may configure only `ask`, only `consult`, or both. If `consult` is missing, the consultation tools are not shown. If `capabilities` and `model_list` are both missing, the model tools are not shown.

The main default limits are:

| Setting | Default |
|---|---:|
| Prompt | 100,000 characters |
| Context | 400,000 characters |
| System instructions | 10,000 characters |
| JSON Schema | 20,000 characters |
| Output | 4,096 tokens |
| Whole request | 120 seconds |
| Schema repair attempts | 1 |

Invalid configuration is rejected when the server starts instead of failing during a request.

## Guardrails and limits

Orchestrator MCP checks request and response structure. It does not know whether a model's factual claims are true.

It does enforce these rules:

- Unknown capabilities and oversized requests are rejected before contacting a provider.
- Structured replies are checked locally against your JSON Schema.
- Truncated, filtered, malformed, and failed answers are returned as errors, not partial answers.
- Errors use stable codes such as `auth_failed`, `timeout`, and `output_truncated`.
- The response always identifies the model and whether a fallback was used.
- Consulted agents run in answer-only mode and may not act on your computer.
- A consulted agent cannot route work back to the same agent runtime.
- Login credentials are not read or stored. Authentication stays in the vendor's CLI.

Important limits:

- Treat caller-provided JSON Schemas as trusted input. A complex regular expression can use a large amount of CPU.
- Provider error text is shortened and common secret formats are redacted, but unusual secrets may still appear. Do not forward errors to an untrusted place.
- The consultation database may contain full prompts and answers. Keep it private or disable full-content storage.

## System requirements

- Python 3.11, 3.12, or 3.13
- Homebrew or [`uv`](https://docs.astral.sh/uv/)
- An MCP client that supports stdio, such as Claude Code or Codex
- For `ask`: provider credentials or a local LiteLLM-compatible model endpoint
- For `consult`: the Codex or Claude Code CLI installed and signed in

## Testing

Run the offline test suite:

```bash
uv sync
uv run pytest -q
```

The tests use fake providers and CLI agents. They do not need API keys or spend money.

To test your real model configuration:

```bash
uv run python smoke_live.py
```

To test real Codex and Claude Code consultations:

```bash
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_consult_live.py
```

The smoke tests make real requests and may use paid API or subscription capacity. Do not run them in CI unless that is intentional.

## Troubleshooting

| Problem | What to do |
|---|---|
| `config not found: config.yaml` | Set `ORCHESTRATOR_CONFIG` to an absolute path. MCP clients may start the server from a different directory. |
| `auth_failed` works in a terminal but not in the client | Add the provider key to the MCP client's environment. The client does not always inherit your shell. |
| Startup names a missing capability | Make sure every capability has a deployment and every fallback names a real capability. |
| `no_deployment` | All deployments are unavailable or cooling down. Check the provider and `cooldown_time`. |
| `output_truncated` | Raise `max_output_tokens`. |
| `schema_validation_failed` | Simplify the schema or use a model with stronger structured-output support. |
| `timeout` on a local model | Increase `request_timeout_s`; model loading is included in the time limit. |
| `consult` is missing | Add the `consult` section and check that the client loaded the correct config file. |
| Host runtime error at startup | Set `ORCHESTRATOR_HOST_RUNTIME` to `claude` or `codex` in the MCP client's environment. |
| `agent_not_installed` | Use an absolute path in the agent's `command`; GUI apps may have a smaller `PATH` than your shell. |
| `connection_required` | Run the login command shown in the error in your own terminal, then try again. |
| Every consultation starts over | Return the previous `consultation_id` with the next call. |
| A dashboard change does not appear | Restart the MCP server; it reads configuration at startup. |

## Bug reports

[Open an issue](https://github.com/crAK1644/orchestrator-mcp/issues) and include the returned response envelope. Remove API keys and other secrets from your configuration before attaching it.

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

Keep `config.yaml`, API keys, and consultation databases out of commits.

### Releasing

1. Update `version` in `pyproject.toml`.
2. Create a GitHub Release tagged `vX.Y.Z`.
3. The release workflow runs the tests, checks the version, and publishes to PyPI using Trusted Publishing.
4. Update the formula in the [`homebrew-tap`](https://github.com/crAK1644/homebrew-tap) repository and rebuild its Apple Silicon bottle.

## Not included

The project does not currently provide semantic intent routing, streaming tool results, automatic PII removal, or a shared Redis state by default. LiteLLM can provide some of these features through its own configuration and callbacks.

## License and support

This project uses the [MIT License](LICENSE).

- [GitHub issues](https://github.com/crAK1644/orchestrator-mcp/issues)
- [PyPI releases](https://pypi.org/project/orchestrator-mcp-server/)
- [LiteLLM routing documentation](https://docs.litellm.ai/docs/routing)
- [Model Context Protocol](https://modelcontextprotocol.io)

Built with [LiteLLM](https://github.com/BerriAI/litellm), [Pydantic](https://docs.pydantic.dev), and the [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
