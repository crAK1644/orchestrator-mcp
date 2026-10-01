<h1 align="center">Orchestrator MCP</h1>

<p align="center">
  <strong>one agent. second opinion. same terminal.</strong>
</p>

<p align="center">
  Make Claude Code ask Codex. Make Codex ask Claude Code.<br>
  Use the subscriptions already signed in on your computer.<br>
  <strong>No provider API keys to configure.</strong>
</p>

<p align="center">
  <a href="https://github.com/crAK1644/orchestrator-mcp/stargazers"><img src="https://img.shields.io/github/stars/crAK1644/orchestrator-mcp?style=flat&color=yellow" alt="GitHub stars"></a>
  <a href="https://pypi.org/project/orchestrator-mcp-server/"><img src="https://img.shields.io/pypi/v/orchestrator-mcp-server?style=flat&cacheSeconds=3600" alt="PyPI version"></a>
  <a href="https://pypi.org/project/orchestrator-mcp-server/"><img src="https://img.shields.io/pypi/pyversions/orchestrator-mcp-server?style=flat" alt="Python versions"></a>
  <a href="https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml"><img src="https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/crAK1644/orchestrator-mcp?style=flat" alt="MIT License"></a>
</p>

<p align="center">
  <a href="#before--after">See it</a> ·
  <a href="#how-it-compares">Compare</a> ·
  <a href="#install">Install</a> ·
  <a href="#what-you-get">What you get</a> ·
  <a href="#reviews-with-a-checkpoint">Reviews</a> ·
  <a href="#the-three-phase-workflow">Workflow</a> ·
  <a href="#security-model">Security</a> ·
  <a href="#local-dashboard">Dashboard</a>
</p>

---

Orchestrator MCP is a local [Model Context Protocol](https://modelcontextprotocol.io) server that lets one coding agent consult another. It launches the Codex, Claude Code, OpenCode, GitHub Copilot, or experimental Antigravity CLI already installed and authenticated on your machine, routes the request, and returns a structured answer.

It does not ask for a provider key, proxy provider traffic, or silently switch models. Authentication remains inside each vendor's CLI.

<!-- mcp-name: io.github.crAK1644/orchestrator-mcp -->

**Quick start** from Claude Code, with Codex already logged in (other hosts: [Install](#install)):

```bash
uv tool install orchestrator-mcp-server              # or pipx, pip, or brew
orchestrator-mcp-server init --host claude           # writes ~/.orchestrator-mcp/config.yaml
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$HOME/.orchestrator-mcp/config.yaml \
  --env ORCHESTRATOR_HOST_RUNTIME=claude -- orchestrator-mcp-server
```

Then ask Claude Code for a second opinion from Codex.

## Before / After

<table>
<tr>
<th width="50%">Without Orchestrator</th>
<th width="50%">With Orchestrator</th>
</tr>
<tr>
<td valign="top">

1. Copy the prompt, diff, and context.
2. Open another coding agent.
3. Recreate the task and paste everything.
4. Bring the answer back.
5. Repeat when you need a follow-up.

</td>
<td valign="top">

1. Call `orchestrator_consult`.
2. Get the other agent's structured answer.
3. Reuse `consultation_id` for follow-ups.

The conversation stays connected from the same client.

</td>
</tr>
</table>

Same subscriptions. Less context shuffling.

```text
 Claude Code host  ──►  Orchestrator MCP  ──►  Codex CLI
 Codex host        ──►  Orchestrator MCP  ──►  Claude Code CLI
 Any host          ──►  Orchestrator MCP  ──►  OpenCode CLI (DeepSeek, Qwen, Kimi…)
 Any host          ──►  Orchestrator MCP  ──►  GitHub Copilot CLI
 Any host          ──►  Orchestrator MCP  ──►  Antigravity CLI (experimental)

                         local routing
                    no provider API keys
                no same-execution-identity loops
```

Ordinary `orchestrator_consult` calls exclude the host's entire runtime: Claude Code
cannot use that tool to consult Claude Code, and Codex cannot use it to consult Codex.
Reviews and workflows use the narrower `(runtime, model)` execution identity; when
`consult.host.model` names the host precisely, they may route to a provably different,
versioned model on the same runtime. The host can never route work back to its own
execution identity.

## How it compares

If Claude Code asking Codex is all you need, OpenAI's
[codex-plugin-cc](https://github.com/openai/codex-plugin-cc) does that, with background
jobs. [PAL MCP](https://github.com/BeehiveInnovations/pal-mcp-server) reaches many
models through provider API keys. Orchestrator is for the rest:

| | Orchestrator MCP | codex-plugin-cc | PAL MCP |
|---|---|---|---|
| Direction | Either way: Claude Code asks Codex, **Codex asks Claude Code**, and either asks OpenCode, GitHub Copilot or Antigravity | Claude Code asks Codex | Any MCP host asks API models; `clink` launches agent CLIs |
| Credentials | Each CLI's own login, no keys | The Codex CLI's login | Provider API keys (OpenRouter, Gemini, OpenAI, …) |
| Review | A [panel of reviewers](#reviews-with-a-checkpoint) from different vendors, answered in parallel, then one synthesis | Codex review and adversarial review | `codereview` and multi-model `consensus` |
| Delegated edits | In a [disposable worktree under an OS sandbox](#execution-modes-and-what-each-one-can-reach); the host applies the diff | `/codex:rescue` hands the task to Codex | — |
| Record | Every turn in a local database, with [cost estimates](#spending-ceilings) and a [dashboard](#local-dashboard) | Job status and result | Conversation threads across models |

Calls block the turn that makes them, often for minutes. To keep working meanwhile in
Claude Code, have a background subagent make the call.

Google's Gemini CLI stopped serving free and Google AI Pro/Ultra accounts on June 18,
2026; Google models are reached here through its successor, Antigravity CLI.

## Install

**Claude Code: install as a plugin.** Needs [`uv`](https://docs.astral.sh/uv/) on your `PATH`, and at least one of Codex, Antigravity, OpenCode or GitHub Copilot installed and logged in ([step 1](#1-sign-in-to-the-agent-clis) below). What you consult them about — prompts, diffs, file contents — goes to those providers under your own logins, and the usage is billed to your accounts with them; see the [privacy policy](PRIVACY.md) and the [Security model](#security-model). In Claude Code:

```text
/plugin marketplace add crAK1644/orchestrator-mcp
/plugin install orchestrator-mcp@orchestrator-mcp
/orchestrator-mcp:setup
```

`/orchestrator-mcp:setup` writes `~/.orchestrator-mcp/config.yaml` from the agent CLIs it finds, then runs `doctor` on it. It finds Codex, Antigravity and GitHub Copilot; OpenCode goes into the config by hand, from its agent in [`config.example.yaml`](config.example.yaml). Then reconnect `plugin:orchestrator-mcp:orchestrator` in `/mcp`, or restart Claude Code: `/reload-plugins` keeps the server that started without a config.

If you added the server earlier with `claude mcp add orchestrator`, remove that entry (`claude mcp remove orchestrator -s <scope>`, with the scope `claude mcp get orchestrator` shows), or two copies of the server run side by side.

Every other host, and Claude Code without the plugin, installs the server itself:

**Homebrew:**

```bash
brew tap crAK1644/tap
brew install orchestrator-mcp-server
```

Apple Silicon uses a prebuilt package. Intel macOS and Linux build dependencies from source; use the [`uvx` option](#run-with-uvx-instead) if you want a faster, temporary install.

**uv, pipx or pip:** `uv tool install orchestrator-mcp-server`, `pipx install orchestrator-mcp-server`, or `pip install orchestrator-mcp-server` into a virtualenv. Python 3.11 or newer.

### 1. Sign in to the agent CLIs

Sign in to each agent you want Orchestrator to use:

```bash
codex login
claude auth login
```

These are the normal Codex and Claude Code login flows. Orchestrator checks readiness, but never reads or stores their credentials.

For OpenCode, sign in once with `opencode auth login` for whichever provider you plan to consult. Hosted providers only — this server does not run a model on your machine. See the [OpenCode runtime](#opencode-runtime--deepseek-qwen-kimi) section below.

For GitHub Copilot, sign in to the copy of its state that Orchestrator runs it under, not to your own: `COPILOT_HOME=$HOME/.orchestrator-mcp/copilot/home copilot login`. `init` prints that command when it writes a Copilot agent, and a consultation that finds it signed out returns it in `required_action`. It needs a Copilot plan; see the [GitHub Copilot runtime](#github-copilot-runtime) section below.

### 2. Write a starter config

```bash
orchestrator-mcp-server init --host claude   # or codex: the client you run it under
```

`init` finds the Codex, Claude Code, GitHub Copilot and Antigravity CLIs installed here, writes
`~/.orchestrator-mcp/config.yaml` (mode `0600`, never over an existing file; `--path`
picks another), and prints the exact line for step 3. It picks the best reviewer
that is not the host. Copilot is scored below the others, so it is the reviewer only
when it is the only CLI beside the host, and it comes last under `deep_reviewers`. Its
agent is written on `model: auto`, the one model every plan serves, so the name cannot go
stale, and `init` prints the sign-in command from step 1. It writes no `workflow:` block,
because only you can choose the directories a workflow may work in, and no OpenCode
agent, because its model names change too often for a template; add one by hand from
[`config.example.yaml`](config.example.yaml).

<details>
<summary><strong>Or write it by hand</strong></summary>

<br>

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
      model: claude-opus-5-5
      priority: 10
      web_search: true
      scores: { coding: 90, research: 95, writing: 95, review: 95 }
```

The key each agent is filed under is its id -- the name callers pass as `target_agent`,
and the name the dashboard writes into URLs. It has to be lowercase, start with a letter
or digit, and use only letters, digits, dots, dashes and underscores, up to 64
characters. Anything else is refused at startup with a message naming the key.

See [`config.example.yaml`](config.example.yaml) for a broader annotated configuration,
an OpenCode agent, a GitHub Copilot agent, and an experimental Antigravity example.

</details>

### 3. Add the server to your MCP client

`init` printed this with your paths filled in.

<details open>
<summary><strong>Claude Code</strong></summary>

<br>

```bash
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$HOME/.orchestrator-mcp/config.yaml \
  --env ORCHESTRATOR_HOST_RUNTIME=claude \
  -- orchestrator-mcp-server
```

</details>

<details>
<summary><strong>Codex</strong></summary>

<br>

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.orchestrator]
command = "orchestrator-mcp-server"
env = { ORCHESTRATOR_CONFIG = "~/.orchestrator-mcp/config.yaml", ORCHESTRATOR_HOST_RUNTIME = "codex" }
```

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

<br>

Add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), with the
`command` that `which orchestrator-mcp-server` prints: Desktop does not read your shell's
`PATH`.

```json
{
  "mcpServers": {
    "orchestrator": {
      "command": "/opt/homebrew/bin/orchestrator-mcp-server",
      "env": {
        "ORCHESTRATOR_CONFIG": "~/.orchestrator-mcp/config.yaml",
        "ORCHESTRATOR_HOST_RUNTIME": "claude"
      }
    }
  }
}
```

</details>

<details>
<summary><strong>VS Code, Cursor</strong></summary>

<br>

VS Code reads `.vscode/mcp.json` in the workspace; Cursor reads `~/.cursor/mcp.json`
with the same entry under `"mcpServers"` instead of `"servers"`.

```json
{
  "servers": {
    "orchestrator": {
      "type": "stdio",
      "command": "orchestrator-mcp-server",
      "env": {
        "ORCHESTRATOR_CONFIG": "~/.orchestrator-mcp/config.yaml",
        "ORCHESTRATOR_HOST_RUNTIME": "claude"
      }
    }
  }
}
```

Neither editor is one of the five runtimes, so name the one whose models its chat runs:
`claude` for Claude, `codex` for GPT. That agent is then left out of the routing, and a
question is never handed back to the model that asked it.

</details>

<details>
<summary><strong>OpenCode</strong></summary>

<br>

In `opencode.json`:

```json
{
  "mcp": {
    "orchestrator": {
      "type": "local",
      "command": ["orchestrator-mcp-server"],
      "environment": {
        "ORCHESTRATOR_CONFIG": "~/.orchestrator-mcp/config.yaml",
        "ORCHESTRATOR_HOST_RUNTIME": "opencode"
      }
    }
  }
}
```

</details>

Restart the MCP client after changing its configuration.

> [!TIP]
> Give `ORCHESTRATOR_CONFIG` an absolute path or one under `~`. GUI-launched clients often start in a different working directory and inherit a smaller `PATH` than your terminal.

### 4. Check it

```bash
ORCHESTRATOR_CONFIG=~/.orchestrator-mcp/config.yaml ORCHESTRATOR_HOST_RUNTIME=claude \
  orchestrator-mcp-server doctor
```

One `ok` or `FAIL` line per check: the config loads, the database opens, and each
agent's CLI is installed and logged in. It exits 1 if anything failed. The only
things it runs are the CLIs' own login checks, so no project material leaves the
machine.

The client spawns the server and talks to it over stdin, so `orchestrator-mcp-server`
is not a command you start yourself. (The [dashboard](#local-dashboard) is the other
half of this distribution and *is* started by hand.) Two flags answer questions from
outside a client, besides `init` and `doctor` above:

```bash
orchestrator-mcp-server --version   # which build the client will spawn
orchestrator-mcp-server --help      # what the environment variables have to say
```

Both answer and exit without reading your configuration. Anything else on the command
line, besides the [reports](#reports-from-the-terminal) below, is refused rather than
ignored. To make the server read the configuration, run it
with no arguments: a file it cannot accept leaves as a message naming the key -- one
line for most mistakes, several for a schema violation, never a traceback.

### Reports from the terminal

Six read-only commands print what the database already holds. They run instead of the
server, read the same config for its `database_path`, and open the file read-only: they
never migrate it, so a database from an older version gets a sentence telling you to
start the server once.

```bash
orchestrator-mcp-server usage [--days 30] [--json]      # turns, tokens and cost per agent and model
orchestrator-mcp-server history [--limit 20] [--kind review] [--json]   # recent records, with their ids
orchestrator-mcp-server scorecard [--days 30] [--json]  # how each reviewer answered, and what became of its findings
orchestrator-mcp-server strengths [--days 30] [--json]  # how each agent answered each kind of question, how fast, at what cost
orchestrator-mcp-server search WORD... [--days N] [--limit 10] [--json]   # stored prompts and answers that hold every word
orchestrator-mcp-server export ID                       # one record and everything it owns, as JSON
```

A price is shown only when every turn in the group reported one; otherwise it reads
`unknown` beside the sum of the prices that were reported, never as zero. The scorecard
counts a finding as kept when the host marked it `fixed` or `accepted_risk` and
rejected when it marked it `rejected`; those are dispositions the host reported when it
finalized a review, which this server does not check, and a later recheck does not
update them. A reviewer's hit rate appears once ten findings are decided. Recheck
reviews are left out so the same finding is not counted twice, and a review with no
synthesis on record (not finalized yet, or `store_full_content: false`, under which
finalizing is refused) is counted as asked but not judged.

`strengths` answers "who is good at what?". Per kind of question (`coding`, `research`,
`review` and so on), agent and model it counts the turns asked, answered and failed, the
average time an answer took, and the cost. Every consultation counts, a reviewer's and a
workflow step's included; a turn that never started, or that the spend ceiling stopped,
does not. A failure rate appears from five asks, and a `review` row also carries the
scorecard's hit rate. Under the table, lines name what may be worth a look and change
nothing: an agent that failed at least 30% of five or more asks for a kind of question,
with its commonest error (a setup error such as `connection_required` is called setup and
points at `doctor`); and a kind of question that your config routes to an agent which took
at least twice as long per answer as another configured agent that failed no more often,
five answers each, on the models configured today. An agent or model the config no longer
holds is never suggested. The routing is worked out as the server would, leaving out the
host's own runtime when `ORCHESTRATOR_HOST_RUNTIME` names one. None of this says whose
answers were better: only `review` rows carry a signal of that, and agents were not asked
the same questions.

`search` answers "which agent told me that?". A stored prompt or answer matches when it
holds every word, best match first, and a word also matches its other endings (`search`
finds `searched`). Each hit shows a short excerpt with the matching words in «marks», the
agent and model, the date, and the id to `export`. A reviewer's hit names its review.
Words are only words: `AND`, `NEAR`, `*`, `:` and quotes mean nothing special, and a query
is at most 200 characters. `--limit` is at most 25 and `--days` defaults to all of it.
The index is built in memory for each call from the text as it would be printed, so a
credential in an old row can be neither read nor searched for, and nothing is added to the
database. That costs about a fifth of a second for each megabyte of stored text. It finds
nothing when `store_full_content` is `false`, which keeps no text, and says so. The same
search is the read-only tool `orchestrator_search_consultations`.

`export` takes a full id or a unique prefix of at least eight characters from `history`.
It prints the record as stored, run through the same masking as everything else, without
session ids or confirm-token hashes; a review or workflow names the consultations it owns
by id, and you `export` those to read them. What it prints is your prompts and the
answers, so treat it as you would the database. Opening a WAL database read-only can
create `-wal` and `-shm` files beside it; the database file itself is never written.

### Run with `uvx` instead

<details>
<summary><strong>Show the temporary-install configuration</strong></summary>

<br>

No permanent server install is required:

```bash
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$HOME/.orchestrator-mcp/config.yaml \
  --env ORCHESTRATOR_HOST_RUNTIME=claude \
  -- uvx orchestrator-mcp-server
```

For Codex:

```toml
[mcp_servers.orchestrator]
command = "uvx"
args = ["orchestrator-mcp-server"]
env = { ORCHESTRATOR_CONFIG = "~/.orchestrator-mcp/config.yaml", ORCHESTRATOR_HOST_RUNTIME = "codex" }
```

The PyPI distribution is named `orchestrator-mcp-server`; the shorter PyPI name belongs to another project.

</details>

## What you get

| Capability | What it does |
|---|---|
| **Second opinion** | Ask another vendor's coding agent about code, research, writing, reasoning, or review. |
| **Connected follow-ups** | Continue the native CLI session by returning its `consultation_id`. |
| **Predictable routing** | Rank configured agents by capability score, priority, then agent ID. |
| **Explicit model choice** | Verify the responding model when the CLI exposes that information; fail on a detected substitution. |
| **Review panel** | Ask one reviewer, or up to five in deep mode, over the same approved material. |
| **Three-phase workflow** | Run a whole job — research and planning, implementation and testing, review and fixing — with eligible models bound to steps their runtime and configured execution mode permit. |
| **Slash commands** | Drive consultations, reviews and workflows by name, with their checkpoints written down rather than hoped for. |
| **Local history** | Store consultations, reviews, and workflows in SQLite, with an optional loopback dashboard. |
| **Answer-only isolation** | Codex, Claude Code, OpenCode and GitHub Copilot are prevented from using action tools; explicit web mode enables only the target runtime's web-search facility, and Copilot has none. Copilot's lockdown is layered, and any tool use its stream reports fails the turn. Experimental Antigravity detects and fails reported tool use but cannot yet prevent it. |

### The consultation tools

| Tool | Purpose |
|---|---|
| `orchestrator_consult` | Start or continue a structured consultation. |
| `orchestrator_consult_many` | Ask 2 to 5 agents the same question at once — named, or the top `count` by score — and get one envelope each for the host to merge. Costs one consultation per agent; each is resumable by its own `consultation_id`, and all share the label `group <group_id>`. `both_sides` makes it a two-sided argument (see below). |
| `orchestrator_list_consult_agents` | Show configured agents, routing scores, installation, and login readiness. |
| `orchestrator_get_consultation` | Retrieve a stored consultation, its turns, usage, and routing decision. |
| `orchestrator_list_consultations` | Recent ordinary consultations, newest first. Metadata only. |
| `orchestrator_search_consultations` | Find past prompts and answers, reviews and workflow steps included, that hold some words. Best match first, with masked excerpts and the ids to read the rest. Read-only. |
| `orchestrator_delete_consultation` | Delete one ordinary consultation and its local turns. |
| `orchestrator_request_delete_all_consultations` / `orchestrator_delete_all_consultations` | Preview and confirm deletion of an exact ordinary-history snapshot. |

These deletion tools remove local SQLite records only. They cannot erase a consulted
runtime's own CLI or provider history.

Three independent opt-ins: the consult tools are always advertised, the review tools only with a `consult.review` block, the workflow tools only with a `consult.workflow` block. Reviewers are not a workflow, and a workflow is not reviewers.

### Slash commands

The server also serves MCP prompts, which a client that speaks `prompts/list` renders as slash commands. In Claude Code they appear as `/mcp__<server-name>__<command>`, where the server name is whatever you called it in your MCP client config — `/mcp__orchestrator__review` for the `orchestrator` entry shown above. Installed as the plugin, the server is named `plugin_orchestrator-mcp_orchestrator`, so the same command is `/mcp__plugin_orchestrator-mcp_orchestrator__review`.

| Command | Arguments | What it expands to |
|---|---|---|
| `consult` | `question`, `agent` | Ask another agent, keep the `consultation_id`, and report the disagreements rather than smoothing them out. |
| `review` | `goal`, `deep` | Plan the review, show the plan and `secret_hits`, stop for the user, then run and finalize. |
| `workflow` | `goal`, `workdir` | Start the workflow, then plan-step, stop, run-step, check status, one step at a time. |
| `status` | `workflow_id` | Report which reviews and workflows are unfinished and what each is waiting on. |

Every argument is optional; a command with none expands into an instruction to ask you for the missing part. A client that speaks `completion/complete` offers the configured agents for `agent` and recent ids for `workflow_id`. `review` and `workflow` are advertised only when their tools are, on the same two answers — a command that could only reply "no reviewers are configured" costs a round trip and reads like a bug.

Two things worth being clear about. **Nothing is installed.** These arrive over the same stdio connection as the tools: no command directory, no generated markdown, nothing written to your machine, and a client that does not speak `prompts/list` is unaffected. **A prompt is text, not an action.** Expanding one consults nobody, sends nothing, and starts no workflow — it reaches the host's conversation as if you had typed it, and the host then calls the tools, checkpoints and all. They exist because the flows worth having here are handshakes, and a host driving them from tool descriptions alone tends to skip the checkpoint that makes them worth having.

### Resources

Each record is also a resource template, for a client that attaches resources rather than calling tools: `orchestrator://consultation/{consultation_id}`, `orchestrator://review/{review_id}` and `orchestrator://workflow/{workflow_id}` read as the JSON the matching get tool returns, and the id completes from recent history. Templates list no instances; the list tools are how you find an id. Each is advertised on the same opt-in as its tools.

### The review and workflow view

In a host that renders MCP Apps, such as Claude Desktop, `orchestrator_get_review`, `orchestrator_finalize_review` and `orchestrator_workflow_status` show their result as a table: findings by severity, reviewer and location, or a workflow's steps and what can run next. The page ships in the package as `ui://orchestrator/view.html`, loads nothing from the network, and puts reviewer text on the page as text, never as markup. Other hosts get the same text result as before.

## How consultation works

`orchestrator_consult` selects the eligible agent with the highest capability score. Lower `priority` wins a score tie; agent ID breaks the final tie. Set `consult.score_margin` to let priority decide among agents within that many points of the top score: give a cheap or free agent a low priority and a margin of 5, and its 92 beats a paid agent's 95. The default `0` keeps the plain score order. A missing capability or a score of `0` makes an agent ineligible.

The selected CLI runs under its existing login and returns one response envelope:

| Field | Meaning |
|---|---|
| `ok` | False exactly when `error` is set. Check this before reading the answer. |
| `consultation_id` | Handle for continuing the same native conversation. |
| `content` | Answer, assumptions, uncertainties, follow-up questions, and sources. |
| `route` | Agent, runtime, model, score, priority, and whether it was selected explicitly. |
| `usage` | Token counts and, where the CLI reports a price, `cost_usd` (null when it does not). `counts_incomplete` lists every reason a count is not a straight measurement, such as a zero this server put in place of a count it could not read. |
| `latency_ms` | End-to-end elapsed time. |
| `error` | Stable error code, message, agent, and sometimes a command the user must run. |

If the chosen agent fails, Orchestrator returns that failure. It does not quietly fall through to a different model.

### Choose the evidence source

| `source_mode` | What the consulted agent receives |
|---|---|
| `auto` | `document` when context is present; otherwise `model`. |
| `document` | Only the supplied context, with action tools disabled. |
| `web` | The target CLI's own web search. `web_turn_limit` bounds it on Claude; Codex is bounded by `timeout_s` alone. |
| `model` | No context and no web search; answer from model knowledge. |

<details>
<summary><strong>Consult request fields and agent options</strong></summary>

<br>

Request fields:

| Field | Required | Meaning |
|---|---|---|
| `capability` | yes | `coding`, `research`, `writing`, `reasoning`, `review`, `planning`, `prompt_authoring`, `testing`, or `synthesis`. |
| `prompt` | yes | Task or question, up to 100,000 characters. |
| `context` | no | Evidence, up to 1,000,000 characters. |
| `context_paths` | no | Up to 50 files this server reads instead of `context`; not together with it. Exists only once `consult.context_roots` is set. |
| `source_mode` | no | `auto`, `document`, `web`, or `model`. |
| `consultation_id` | no | Return the previous ID to continue the conversation. |
| `target_agent` | no | Choose one configured agent instead of automatic routing. |
| `persona` | no | A ready-made style or one of the names under `consult.personas`, for this turn only. |
| `conversation_label` | no | Label stored with the consultation, up to 200 characters. |

Agent configuration:

| Option | Default | Meaning |
|---|---|---|
| `runtime` | required | `codex`, `claude`, `opencode`, `copilot`, or `antigravity`. |
| `command` | required | Executable name or absolute path. |
| `model` | required | Requested model and, where possible, verified responding model. |
| `priority` | `100` | Lower wins a score tie, or any pick within `consult.score_margin`. |
| `enabled` | `true` | Keep the agent configured but out of routing when false. |
| `scores` | none | 0–100 per capability; missing means ineligible. |
| `web_search` | `false` | Permit `source_mode: web` for this agent. |
| `reasoning_effort` | unset | `low`, `medium`, `high`, `xhigh`, or `max`; Codex only. |
| `timeout_s` | unset | Limit for one turn with this agent, overriding `consult.timeout_s`. |
| `execution_modes` | `[consultation]` | What a workflow step may ask this agent to do: `consultation`, `patch` or `isolated_write`. Operator trust, not capability: a step gets a mode only if it is listed here and the runtime can do it. See [Execution modes](#execution-modes-and-what-each-one-can-reach). |

`context_paths` uses the same reader as a review's (strict resolve, `O_NOFOLLOW` walk, no
FIFOs, no symlinks out), against its own list:

```yaml
consult:
  context_roots: [~/src]   # empty: the argument is not in the tool schema at all
```

A review previews what it will send and can mask a hit; a consultation sends at once. So a
file with something credential-shaped in it is refused, naming the path and line numbers
but never the value. Pass a masked excerpt through `context` instead. The file's path is
the heading the agent sees, so an absolute path can disclose a username or directory
layout. `orchestrator_consult_many` reads the files once and gives every member the same
bytes.

`persona` asks an agent to lean a named way for one turn. Six styles are built in, with no
config: `skeptic` (doubt the premise first), `security` (read as an attacker would),
`simplify` (the smallest change that works), `plain` (plain words for a newcomer), and
`case-for` and `case-against` (argue one side honestly, keeping doubts in `uncertainties`).
Write your own once in config, or reword a built-in by using its name:

```yaml
consult:
  personas:
    skeptic: Doubt the premise before answering. Say what evidence would change your mind.
    migrations: Check every schema change for a lock and a way back.
```

The text joins the system half of the prompt after the protocol and states that it cannot
change the protocol, the required fields, or what the agent may do. It applies to that turn
only: a resumed consultation's agent remembers earlier turns, so leaving `persona` out later
does not unsay it. It is not on reviews or workflow steps, which have prompts of their own.
The stored turn's compiled prompt shows exactly what was sent, and a credential-shaped value
pasted into a persona is masked before it goes out.

`orchestrator_consult_many` with `both_sides: true` asks exactly two agents to argue a
proposal from opposite sides. The first gets the `case-for` persona and the second
`case-against`; the task and context are otherwise the same bytes, and neither sees the
other. Name the two in `target_agents`, or omit it and the top two by score are asked
(`count` is ignored). The first named always argues "for", so reverse the list to swap: the
order can matter, so for a decision that does, ask both ways. The response adds `sides`, a
map from agent id to `for` or `against` that is present even when a side failed. A side
failing leaves the other's answer, and `persona` is refused alongside it. The server never
picks a winner: each side was told which case to make, so read the two as arguments, not as
either agent's verdict.

</details>

## Reviews, with a checkpoint

A consultation asks one agent. A review asks one or more configured reviewers the same question over the same material.

```text
 plan review          approve + run          synthesize
 sends nothing   ──►  reviewers answer  ──►  host records conclusion
      │                    in parallel                │
      └─ scope              one-time token            └─ every Critical and Important kept
         reviewers
         secret hits
         request count
```

Enable reviews in `config.yaml`:

```yaml
consult:
  review:
    reviewers: [codex]          # standard: exactly one
    deep_reviewers: [codex, claude]  # deep: one to five
    roots: [~/src]              # context_paths and diff_ref are restricted to these trees
```

For a branch review, skip the file: `diff_ref` lets Orchestrator run the diff itself.

```text
orchestrator_review(goal="...", diff_ref="main...HEAD", diff_repo="~/src/myproject")
```

`A..B` diffs the two commits, `A...B` diffs from their merge base (what a pull request
shows), and a single commit `X` is `X^..X` (a merge means its first parent). `diff_repo`
may be left out when `roots` names exactly one directory. `diff_ref` cannot be combined
with `context` or `context_paths`.

- Both ends are resolved to commit SHAs before the diff runs, and the plan's manifest
  shows them. Moving the branch between the plan and the run changes the approval hash,
  so the token no longer fits.
- Only committed changes. The working tree and the index are not read; commit first or
  pass the diff through `context`.
- The repository, and the top level of the repository it sits in, must both be beneath
  `roots`. Pointing `diff_repo` at a subdirectory of a repository outside them is refused.
  So is a checkout whose git data lives elsewhere: a linked worktree of a repository
  outside `roots`, a `.git` file pointing out, or `objects/info/alternates` (a
  `git clone --shared`) naming a directory outside.
- `git` runs with external diff programs, `textconv` filters, replacement refs and lazy
  fetching off, so a repository whose config names a program cannot make this run it. In
  a partial clone a diff that needs a missing blob fails instead of fetching it. Binary
  files appear as "differ", not as bytes.
- A manifest you pass as `material` is kept, listed after the pinned endpoints, and leaves
  `material_verified` false.
- One limit: git opens the directory by name after the check, so someone who can already
  write inside a root and swap the repository for a symlink in that instant can redirect
  it. Closing that needs an OS sandbox around `git`.
- The reviewer receives the diff, whose headers carry file paths relative to the repository,
  never the repository's own location.

`context_paths` is a convenience for material too large to paste into a tool argument.
Orchestrator reads each named file beneath those roots and sends its contents to the
reviewers. The path string supplied by the caller is also included as the file heading
and manifest label, so an absolute path can disclose a username or directory layout. If
that is sensitive, pass the bytes through `context` with neutral labels instead.

Each root has to be an absolute path to a directory that exists. The server checks that
at startup and refuses to start naming the one it could not find, rather than starting
and failing the first `context_paths` call -- which is a reviewer's turn later.

The workflow is deliberately split:

1. `orchestrator_review` creates a plan and **sends nothing**. The plan shows reviewers, material size, web access, request count, and locations of credential-shaped text. `suspect_hits` adds the lines holding a long random-looking token that no pattern names (32 characters of letters and digits, say). It is a guess: hex, UUIDs, integrity hashes, names in snake_case or kebab-case, and a name of letters alone with a number on the end (`LoaderV2`) are left alone, and so is a line already in `secret_hits`. It never blocks and is not stored. A key of letters alone, or of hex, gets past it, so read the material yourself. A plan nobody runs within a day is dropped the next time a review is planned.
2. Show that plan to the user. `orchestrator_review_run` spends its one-time token and asks reviewers in parallel.
3. Read every result and call `orchestrator_finalize_review`. Reviewer replies alone leave the review at `awaiting_synthesis`.

The checkpoint binds the scope and makes the token single-use, but it is advisory:
MCP gives the server no separate human channel, so it cannot prove who saw the plan.
Human approval depends on the calling client's tool-confirmation experience or an
external gate.

Finalization must preserve every machine-readable Critical and Important finding, even when other reviewers disagree with it. Deep mode also requires the host agent to record its own findings before seeing the reviewers' answers.

> [!IMPORTANT]
> Material sent to a reviewer may remain in that vendor CLI's own history. Orchestrator cannot erase Codex, Claude Code, OpenCode, GitHub Copilot, or Antigravity session logs.

<details>
<summary><strong>Review tool reference</strong></summary>

<br>

| Tool | What it does |
|---|---|
| `orchestrator_review` | Plan a review and show what would be sent. Sends nothing. |
| `orchestrator_review_run` | Spend the token and ask reviewers in parallel. |
| `orchestrator_retry_review` | Re-run failed reviewers without discarding successful answers. |
| `orchestrator_finalize_review` | Record the host's synthesis; the only path to `complete`. |
| `orchestrator_cancel_review` | Cancel a review while retaining answers already received. |
| `orchestrator_apply_fixes` | Return selected findings and fix steps, and list the Critical and Important findings the selection leaves out. Changes no files. |
| `orchestrator_record_fix_round` | Record the host's claim about a fix round. |
| `orchestrator_test_reviewers` | Check installation and login readiness without sending project material. |
| `orchestrator_get_review` / `orchestrator_list_reviews` | Read one review or recent review metadata. |
| `orchestrator_delete_review` | Delete a review, its rechecks, and linked consultations. |
| `orchestrator_request_delete_all` / `orchestrator_delete_all_reviews` | Preview and confirm deletion of an exact history snapshot. |

Reviews default to `web: false`. Reviewers cannot change files or run commands. `orchestrator_apply_fixes` is a plan for work the host agent performs; it never applies a patch itself.

A recheck is a review planned with `parent_review_id`. The server appends the parent's open findings and a recheck brief, so the host sends only the diff instead of the whole tree again. Set `review.recheck_reviewers: raised` to ask again only the reviewers behind an open finding (at least one); the plan lists the others in `reviewers_skipped`. The default `all` asks everyone. A recheck starts a fresh reviewer session rather than resuming the old one: a resumed session re-bills its whole transcript once the provider's prompt cache has expired.

A reviewer's prose comes back once, with the call that ran it, and its `findings` -- parsed out of that prose -- come back every time. `orchestrator_get_review` is the call that returns the prose again. That keeps a review's later calls from re-sending the same reviewer answers into your agent's context, where they would be charged for on every turn that follows.

Credential-shaped values are masked before storage. `secrets="send_as_is"` is an explicit escape hatch for a false positive: it requires the exact original goal and context again, sends those originals to the reviewers, and still stores only the redacted copy.

`store_full_content: false` does not apply here in full. A review's goal and context are stored either way — the second half of the approval handshake reads them back to send what was approved — and reviewer answers and findings are not. That leaves nothing to prove every Critical and Important survived synthesis, so `orchestrator_finalize_review` refuses, and the review stays at `awaiting_synthesis`. Finalization is refused on the same grounds when a reviewer answered only in unparseable prose, or when its findings were truncated.

</details>

## The three-phase workflow

A consultation is one question. A review is one body of material. A **workflow** is a
whole job held together: research and planning, implementation and testing, review and
fixing, with failed tests and open serious findings feeding a capped fix loop. Every
consultation and review it produces hangs off one `workflow_id`.

```text
 research? → plan → author_execution_prompt → implement
                                               └→ [apply_patch if delegated] → test

 test
 ├─ failed by default → fix → [apply_patch if delegated] → test
 └─ passed / advance_on_failed_test → review → synthesize

 synthesize
 ├─ clean → completed
 └─ open serious findings → fix

 `?` means research may be skipped. Fixing is capped at `max_fix_rounds`.
```

Enable it in `config.yaml`. Without a `workflow:` block the workflow tools are not
advertised at all, the same rule the review tools follow:

```yaml
consult:
  host:
    runtime: claude              # asserted against ORCHESTRATOR_HOST_RUNTIME
    model: claude-opus-5         # optional; see "Host identity" below
  workflow:
    max_fix_rounds: 5
    roots: [~/src]
    advance_on_failed_test: false
    review_policy:
      different_from_implementer: true
      different_from_planner: false
    bindings:
      research:  {agent: codex-sol}
      plan:      {agent: codex-sol}
      author_execution_prompt: {executor: host}
      implement: {agent: nemotron-ultra, execution: patch}
      apply_patch: {executor: host}
      test:      {executor: host}
      review:    {agents: [codex-sol, claude-opus]}
      synthesize: {executor: host}
      fix:       {agent: codex-sol, execution: patch}
```

`workflow:` requires `store_full_content: true` and refuses at startup otherwise. A
workflow *is* its stored plans, briefs, patches and reports, and the review step cannot
finalize without them — the failure belongs at boot, not several paid steps in.

`roots:` follows the same rule as `review.roots:` — absolute after `~`/`$VAR` expansion,
and a directory that exists, checked at startup. A relative root is refused rather than
resolved against wherever the client spawned the server, which would make the allowlist
something other than what the file says.

### Models are not tied to phases

A binding is one of three shapes, and mixing them is a startup error:

| Binding | Meaning |
|---|---|
| `{executor: host}` | The calling agent does this step itself and records the result. |
| `{agent: x, execution: patch}` | That agent, in that execution mode. |
| `{agents: [x, y]}` | Several agents. `review` is the only step that takes more than one. |

Leaving `agent:` out means `auto`: routed by capability score for the step, ties broken
by `priority` then agent id, the same rule `orchestrator_consult` uses. GPT, Claude,
DeepSeek, Gemini, Qwen — any model reachable through a supported runtime can take any
step it scores for and is permitted the mode for. A step with no binding falls to the
host, which is the conservative default — with one exception. `review` cannot be the
host's: its product is a review row that only the review service writes, and a
host-recorded outcome would be a synthesis written straight into a step. Because unbound
steps default to the host, a config that simply never mentions `review` is refused at
`workflow_start`, before anything has been spent, rather than at the review step after
research, planning and implementation have all been paid for.

Steps route on the four capabilities added for this: `planning`, `prompt_authoring`,
`testing` and `synthesis`, alongside `coding`, `research` and `review`.

Bindings are resolved and **snapshotted at workflow creation**, and so is the policy
they run under — the round cap, `advance_on_failed_test` and the review policy. Editing
`config.yaml` does not reroute a running workflow or move its cap; that takes
`orchestrator_workflow_plan_replan` and its own approval. A replan re-decides the steps
you name and leaves every other one on the routing the workflow already had.

### Presets

A preset is a named set of bindings for the steps you would rather not spell out at every
start. `workflow_start(preset="cheap")` layers it over the rest, lowest to highest: the
host default, `bindings:`, the preset, then the `bindings` argument of that call. A preset
names only the steps it changes.

```yaml
    presets:
      cheap:
        research: {agent: flash}
        plan:     {agent: flash}
```

A preset is checked when the config loads, exactly as `bindings:` is: an unknown agent, a
mode the step does not take, or a mix of shapes refuses the boot and names
`workflow.presets.<name>.<step>`. A name is lowercase, `[a-z][a-z0-9_-]{0,31}`. An unknown
preset at start is refused and the configured names are listed. The preset name is not
stored: what a workflow runs under is the resolved snapshot, so editing a preset later does
not move a running workflow.

### Execution modes, and what each one can reach

`execution_modes` on an agent is **operator trust, not capability**. What actually
happens is the intersection of that list with what the runtime can be made to do, and a
refusal names which side said no.

| Mode | The agent gets | Repository access |
|---|---|---|
| `consultation` | The read-only consult path, unchanged. | `context_only` |
| `patch` | The same read-only path; it returns a unified diff. The host applies it. | `context_only` |
| `isolated_write` | A disposable git worktree outside your repository, checked out at the workflow's baseline. The agent edits and runs commands there; Orchestrator reads the diff back out of git and the host applies it. **Codex, and OpenCode where the sandbox holds.** | `worktree` |
| `executor: host` | Not an agent at all: the host edits its own checkout. | `active_tree` |

| Runtime | `isolated_write` | Why |
|---|---|---|
| `codex` | **supported** | `sandbox_mode: workspace-write` with `approval_policy: never` is enforced by the CLI at OS level: a command aimed outside the worktree comes back `Operation not permitted` from the kernel, not from the model declining. Network is off, `/tmp` and `$TMPDIR` are excluded from the writable set. |
| `opencode` | **supported where the sandbox holds** | Its own permission set isolates *configuration*, not filesystem effects, so the bound is Orchestrator's OS-level sandbox (seatbelt on macOS): writes are held to the worktree, and its runtime state is redirected into it and removed before the diff is read. **The network is open**, because the model is hosted: weaker than Codex, whose network is off. Stored `opencode auth` credentials are not carried in, so only providers that need none (such as OpenCode's free catalogue) work. Bubblewrap cannot grant that network yet, so Linux still refuses, and the refusal at startup says why. |
| `claude` | refused | Its permission modes are requests, not kernel bounds, so it would need Orchestrator's OS-level sandbox as OpenCode does. Where that sandbox holds (macOS) it is still refused, because Orchestrator has no write adapter for Claude Code; Linux also cannot grant it the network. Use `patch`. |
| `antigravity` | refused | Writing needs `--dangerously-skip-permissions`, the one flag the adapter refuses by construction. |
| `copilot` | refused | Its adapter consults with every tool disabled and has no write path. Use `patch` and apply the diff on the host. |

A root allowlist and a prompt instruction are not containment. An agent that declares
`isolated_write` on a runtime that cannot be contained is refused **at startup**, not at
routing time.

**No delegated agent writes to your working tree.** In `patch` mode the agent never sees
your checkout — only the context the host sent it, exactly as a reviewer does — and the
diff comes back for the host to apply. In `isolated_write` it sees a *copy*: a worktree
under `~/.orchestrator-mcp/worktrees/<workflow_id>/<step_id>/`, checked out at the latest
applied result (the workflow's baseline until the host has applied anything). It is
normally deleted after the diff is captured and its private recovery copy is written.
It is retained when capture fails or recovery cannot safely preserve the patch, because
the worktree may then hold the only usable copy. Either way the successful step ends in
`awaiting_host_apply` and the host owns the branch. `ConsultAdapter` gained nothing for
any of this: it is still three verbs with no way to ask for anything agentic, and write
capability lives in a separate package behind a separate protocol.

Four things about a contained run are worth knowing before you use one:

- **The diff is the record, not the reply.** Orchestrator runs `git add -A` in the
  worktree and takes the staged diff against the baseline, so files the agent *created*
  are captured too — a plain `git diff <baseline>..` would miss them. The model's summary
  is stored beside the patch as a description of its work, never as the account of it.
  Its list of commands is stored the same way, and is knowingly incomplete: codex omits
  sandbox-denied commands from its event stream entirely.
- **The agent cannot commit.** A worktree's git directory lives outside its sandbox, so
  `git add`, `commit`, `stash` and `checkout` all fail from inside. It leaves the work in
  the tree and this server records it. The step's timeout is
  `consult.workflow.execution_timeout_s` (900s by default), not `consult.timeout_s`,
  which is sized for a question.
- **Two things a diff cannot carry, and neither passes in silence.** A repository
  created *inside* the worktree — a scaffolded subproject, a vendored fixture, any
  `git init` — makes `git add -A` refuse the whole tree. The step fails and the worktree
  is **kept**, with its path in the error, because at that moment it holds the only copy
  of the work. Ignored files are skipped by `git add -A` by design and can never appear
  in a patch; they come back listed on the step's `ignored` field rather than vanishing
  with the worktree.
- **The repository capture reads is the one pinned before the agent started.** A
  worktree's `.git` is a one-line file naming its git directory, and it sits in the
  directory the agent spent the whole step writing to. That gitdir is read at worktree
  creation and passed explicitly to every later git call, so what capture diffs is not
  decided by a file the step could rewrite.

### Host identity

`(runtime, model)` is an execution identity, not an agent id, and it comes from trusted
startup configuration only — never a tool argument. `runtime` still comes from
`ORCHESTRATOR_HOST_RUNTIME`, which stays the authority; naming it under `host:` is an
assertion, and a mismatch refuses the boot.

`model` is the part the environment cannot supply. With it, a *different* model on the
host's own runtime becomes routable. Without it, every agent on that runtime is excluded
— today's consult behaviour. Write the versioned name: identity must be **provably
different**, so `opus` and `claude-opus` are treated as `claude-opus-5` and refused, and
a name with no version at all is refused for being unprovable rather than assumed
distinct.

`review_policy` is checked against this resolved identity too, so two agent ids pointing
at one model are one reviewer however they are spelled.

### Two calls per step

```text
plan_step                    run_step | record_host_step
returns a preview       ──►  spends that step's token
and a one-time token         and runs or records it
```

There is no one-call form and **no workflow-level token**. One approval covering
research, implementation, every fix round and every review would be an approval of
nothing in particular. What a token proves is snapshot integrity — that the step being
run is the step that was previewed, with the same agent, mode and inputs. It cannot
prove a human saw anything, because MCP gives this server no channel to one; that
depends on your client's tool-confirmation experience or an external gate.

A workdir must resolve beneath a configured root. `/` is never accepted and nothing is
inferred from the working directory. A dirty tree is refused without `allow_dirty`.

### A delegated step sees only what the host hands it

Nothing here reads your repository on an agent's behalf, so a delegated step starts with
no code in front of it. `orchestrator_workflow_plan_step(workflow_id, step, context)`
takes that material — the source of the files being changed, most of the time — and the
host decides what goes in it. Without it, an implementation step has the plan and the
brief and nothing to patch, and the honest models say exactly that instead of inventing
a file they were never shown.

The material is redacted with the same scrubber as everything else *before* it is
stored and *before* it is sent, and it is covered by the step's prompt hash, so the text
the preview described is the text that goes out. A review step gets the same material
its coding steps did.

### Tests are observed, not claimed

A coding agent's statement that it ran the tests is retained as reported information; it
is never the test result. A `TestReport` carries the exact command, working directory,
exit code, bounded output, duration and the commit tested, plus `reported_by`, which the
service assigns from *which tool wrote the row* and never reads out of a caller's
payload. A host-written report is host-attested, and its provenance says so.

`reported_by: orchestrator` has exactly one source: a `test` step bound to
`isolated_write`, where the exit codes come out of the CLI's own event stream rather
than out of anything the model wrote. Read what that does and does not claim. It means
every command the run *reported* returned zero — codex omits sandbox-denied commands
from that stream, so it is not a claim that the project's suite ran. A contained test
step that edited files while testing lists them on the report's `changed_files`; the
worktree is deleted either way, so nothing there is applicable, but "the tests pass" and
"the code was edited until they did" no longer look identical.

A failed test returns to `fixing` rather than advancing, unless `advance_on_failed_test`
is set.

Two fields are cross-checked rather than stored side by side. A report cannot be
`passed` with a non-zero exit code or with none at all — a command whose exit code was
never read is `skipped`, which is what a denied command or a killed process produces —
and it cannot be `failed` with a zero. The commit is stamped by the service from what
the workflow currently holds, not taken from the payload: `loop_done` compares them, so
a pass from an earlier round cannot carry a later one.

`apply_patch` is the one step whose whole job is a side effect on your tree, and the
only evidence it happened is a commit that was not there before. Recorded without one,
or with the commit it started from, the step is refused and marked `failed` rather than
advancing — there is no `applied: false` to write, because a step that did not do its
work is a failed step. Plan it again once the patch is applied and committed.

### Where the loop stops

The review step goes through the review service — it does not write a synthesis
straight into workflow storage, which would route around the guarantee that every
serious finding survives. Findings gained a `disposition` (`open`, `fixed`, `rejected`,
`accepted_risk`), because "unresolved finding" was previously not representable;
rejecting or accepting the risk of a critical or important finding without a reason is
refused.

`loop_done` is computed here, never asked of a reviewer. It is true only when the
authoritative tests passed for the current commit, every reviewer's findings parsed and
were retained, no critical or important finding is still `open`, `missing_serious`
passed, and the workflow is in no exceptional state. Reaching `max_fix_rounds` with
serious findings still open ends the workflow `needs_attention` — not `completed`.

A fix round after review carries the findings that are still open, read back from the
review row rather than from workflow storage, so the round is an answer to the review
and not a second pass at the goal. A fix triggered by a failed test before review instead
carries that failed `TestReport`. A re-review is a recheck of the previous round's review
(`parent_review_id`): the server appends that review's open findings and tells the
reviewers to confirm or drop each one and to look for new problems only in the change.
Send the fix's diff as the step's `context`, not the whole tree again.

### The execution contract, honestly scoped

The coding prompt is assembled by this code: our `EXECUTION_CONTRACT` first, then the
scope, accepted plan, authored brief and prior findings as a JSON payload. An authored
brief is data inside that payload, and no field turns it into contract text.

That is **code ownership, not a transport-level enforcement boundary**. Claude Code has
a real system-prompt channel; Codex, OpenCode, GitHub Copilot and Antigravity receive one compiled text, so there the
ordering is a prompt convention a determined model could argue with. Saying so is more
useful than overclaiming.

<details>
<summary><strong>Workflow tool reference</strong></summary>

<br>

| Tool | What it does |
|---|---|
| `orchestrator_workflow_start` | Create a workflow: validate the workdir and root, resolve and snapshot bindings, record the git baseline. Sends nothing and returns no execution token. |
| `orchestrator_workflow_plan_step` | Preview one step and mint its one-time token, with the optional `context` the step is shown. A review step returns the review plan's own token rather than an unrelated second approval. |
| `orchestrator_workflow_run_step` | Spend the token and run the step through its bound agent. |
| `orchestrator_workflow_record_host_step` | Record work the host did itself. The token is consumed as the host's attestation. |
| `orchestrator_workflow_status` | State, artifacts, selected agents, round count, spend, and what may happen next. |
| `orchestrator_list_workflows` | Recent workflows, newest first: id, goal, state. |
| `orchestrator_workflow_plan_replan` / `orchestrator_workflow_replan` | Change the binding snapshot under the same preview-and-approve handshake. |
| `orchestrator_workflow_cancel` | Cancel pending work and terminate a child this process owns, with the same caveat `orchestrator_cancel_review` carries about another process's children. |
| `orchestrator_delete_workflow` | Delete one workflow with its steps, consultations and reviews. Refused while the workflow is open or a step's lease is live. |
| `orchestrator_request_delete_all_workflows` / `orchestrator_delete_all_workflows` | Preview and confirm deletion of an exact workflow snapshot. |

A workflow deletes whole or not at all. Its consultations are excluded from every
consultation delete path and `orchestrator_delete_review` refuses a review that is a
workflow step, because a step pointing at a row that is gone still reads as intact —
and the next fix round would answer from the goal instead of from the review. So these
three tools are the only way any of those rows leave the database.

`orchestrator_workflow_status` is also the call that returns every step's `output`.
The tools that advance a workflow return the body of the step they touched and the
shape of the rest -- a plan, a patch and a test log do not change because a later
step ran, and re-sending them on every call is the same bytes accumulating in your
agent's context. Ask status when you want an earlier step's body back.

`orchestrator_workflow_status` reports what the workflow spent: per step, and totalled
over the workflow with its reviewers included. The numbers are rebuilt from the
consultations' turn ledgers at read time, so a step that took two turns counts both and
a re-read counts neither twice. A host step reports no usage — nothing was spent on it
here, which is not the same as it having cost zero. `cost_usd` is set only when every
turn behind it was priced: an agent on a free tier reports no price, and one unpriced
turn makes the total a floor rather than a sum, so it comes back unknown instead. The
dashboard shows the same numbers on the workflow list and per step.

A review step's reviewers come from its binding, so a workflow needs no `review:` block
unless that binding is left to `auto` — then it falls back to the configured reviewers
and refuses without them.

A workflow's consultations are not reachable from the public tool: resuming one by its
`consultation_id` is refused with `workflow_owned_session`. Steps take a lease, so a
crashed run resolves rather than leaving a status that outlives its process.

**Patch integrity.** Redaction can rewrite credential-shaped text, and a rewritten patch
is a corrupt patch. SQLite stores only a sanitized audit copy plus the sha256 of the raw
patch. The applicable patch is kept in a private `0600` recovery file and returned by
workflow status until `apply_patch` is recorded, then every raw patch from that workflow
round is removed. If the recovery copy cannot be written or trusted, the raw response is
still returned and `recovery_warning` explains that status cannot recover it later. After
the host applies it, the resulting code commit becomes the workflow's review input.
Recovery files and retained failure worktrees expire after seven days. The first
workflow-service open schedules a bounded, best-effort background sweep of owned
artifacts older than that window, so abandoned work does not become indefinite raw
storage or request latency.

</details>

## Security model

| Property | Guarantee |
|---|---|
| **Credentials** | No provider key setting exists. Orchestrator never reads, stores, returns, or refreshes a CLI's own credential. A credential you put in a prompt is material, not a credential here — see the warning below. |
| **Process launch** | Commands are executed as argument lists, never through a shell. |
| **Prompt visibility** | Codex, Claude Code, OpenCode and GitHub Copilot read the prompt from stdin. Antigravity's CLI reads none, so its prompt is a command-line argument that other users on the machine can read in the process list while a turn runs. `doctor` says so for each such agent; do not send it material you would not put in `ps` output on a shared machine. |
| **Self-consultation** | `ORCHESTRATOR_HOST_RUNTIME` comes from the environment and cannot be overridden by a tool call. |
| **Agent permissions** | Consulted agents are answer-only, except for the target CLI's bounded search in explicit web mode. |
| **Model identity** | A detected mismatch fails with `configured_model_unavailable`. Missing CLI metadata is reported as unverified, not invented. |
| **Storage** | SQLite directory permissions are `0700`; the database and managed agent file are `0600`. |
| **Dashboard** | Loopback only, with host-header checks and a per-process token. |
| **Review checkpoint** | Plans bind the scope to a one-time token before reviewer requests are made. The server cannot independently verify human approval. |
| **Workflow write surface** | No delegated agent writes to your working tree. `patch` mode returns a diff over the read-only consult path; `isolated_write` runs in a disposable worktree outside your repository, inside an OS-level sandbox: Codex's own, with the network off, or Orchestrator's for OpenCode, whose network stays open. Both end in `awaiting_host_apply`: the host owns application. |
| **Workflow checkpoints** | One token per side-effecting step, spent in the statement that starts it. There is no workflow-level token, and a token proves snapshot integrity rather than human approval. |
| **Workflow identity** | The host execution identity comes from startup configuration only. A candidate that cannot be *proven* a different model from the host is refused. |
| **Workflow scope** | A workdir must resolve beneath a configured root; `/` is refused and nothing is inferred from the working directory. A dirty tree needs explicit acknowledgement. |

> [!WARNING]
> **Redaction covers every retained database copy, but what gets transmitted depends on the flow.** An ordinary consultation sends its original material while storing a scrubbed copy. Reviews normally send the masked copy; `secrets="send_as_is"` is the explicit path that sends the original. Workflow step material is redacted before both storage and transmission. Detection is best-effort pattern matching rather than a scanner with perfect recall, so a secret with no recognizable shape can survive it. Keep the database private, or set `store_full_content: false` where the selected feature permits it.
>
> **Vendor history is outside all of this.** Material sent to a reviewer also lands in that reviewer's own CLI history — Codex writes `~/.codex/sessions/`, and the others keep their own logs. Orchestrator cannot redact or erase those files. It does read from them, in three places and for two fields: the Codex adapter opens the rollout file for the session it just ran to recover the model identity the CLI does not otherwise report, and opens the newest rollout to read the latest Codex CLI rate-limit numbers; the OpenCode adapter runs `opencode export` on the session it just ran, for the same reason — the model identity is absent from that runtime's event stream. Nothing else is taken from any of them. GitHub Copilot's is kept apart: its state lives under Orchestrator's own `~/.orchestrator-mcp/copilot/home`, one session directory per consultation, which Orchestrator looks for before resuming a session and removes, with its lock, when it deletes that consultation, by a delete tool or by `retention_days`. It removes nothing else there. In that home it also reads `mcp-config.json` for the names of MCP servers to switch off, and writes one setting, `disableAllHooks`, into `settings.json`.

Two more limits worth knowing:

- CLI error text is shortened and common secret formats are redacted, but an unusual one may still appear in a returned error. Do not forward a raw error envelope somewhere untrusted.
- A caller-supplied JSON Schema is trusted input. A pathological regular expression in one can consume a large amount of CPU.

Orchestrator checks structure, routing, permissions, and model identity where observable. It cannot prove that a model's factual claims are true.

<a id="opencode-runtime--deepseek-qwen-kimi"></a>

<details>
<summary><strong>OpenCode runtime — DeepSeek, Qwen, Kimi</strong></summary>

<br>

[OpenCode](https://opencode.ai) is one CLI in front of many hosted providers, which is how models the other runtimes do not carry become reachable without Orchestrator holding a key. Models are addressed as `provider/model`:

```yaml
    nemotron-ultra:
      runtime: opencode
      command: opencode
      model: opencode/nemotron-3-ultra-free
      scores: { coding: 60, reasoning: 60 }
```

OpenCode's free models (`opencode/*-free`) rotate — the one above can stop being offered, and an agent naming a model that is gone fails preflight with ``is not among the models opencode offers``. Run `opencode models` and pick one it lists today.

**Hosted providers only. Orchestrator does not run a model on your machine, or on anyone's.** Depending on the selected model, this runtime consults a subscription you already hold or OpenCode's anonymous free tier. A locally served model — Ollama, LM Studio, your own endpoint — cannot be reached through it, and that is enforced by construction rather than by a check: a consultation runs under a configuration with no `provider` block at all, and a provider block is the only place an endpoint outside OpenCode's own catalogue is ever named. Verified against the CLI: under this configuration `--model ollama/qwen2.5:7b` fails with `ProviderModelNotFoundError` and the local server is never contacted. The constraint is real — if the model you want is one you host yourself, this runtime cannot consult it.

**Readiness is asked under that same configuration.** Orchestrator runs `opencode models` with your global config out of reach and checks that the agent's `provider/model` is listed, so readiness answers the question the consultation will actually ask rather than reporting a model that would then fail to resolve. A provider that needs a credential wants `opencode auth login` once; stored keys and OAuth tokens live in OpenCode's data directory, which Orchestrator neither reads nor relocates. The child gets a fixed allowlist of environment variables — `HOME`, `PATH`, `LANG` and a few more — and every `*_API_KEY` in this server's own environment is excluded from it.

Each consultation runs under a configuration Orchestrator writes and nothing crosses over from your own: permissions denied outright, no MCP servers, no plugins, no instructions, no project agents, no providers, sharing and auto-update off. `OPENCODE_CONFIG` merges rather than replaces, so `XDG_CONFIG_HOME` is pointed at an empty directory as well — your global config is out of reach, not merely outranked.

The working directory is `~/.orchestrator-mcp/opencode/<agent>`, mode `0700` with the configuration files `0600`, holding nothing else. One per agent rather than one shared: the files are identical for every agent today, and one release that gives an agent something of its own to write would turn that into a race between a consultation starting and reading its configuration. Under `$HOME` rather than `/tmp` deliberately: Orchestrator refuses to run if it finds an `opencode.json` or `opencode.jsonc` above that directory — a parent's permissions outrank its own — and that check can only run *before* OpenCode reads the chain, so every ancestor needs to be a directory no one else can write to. It is not deleted afterwards, and cannot be: OpenCode records a session's directory and re-resolves it on resume, so a per-run temporary directory would break every follow-up turn.

Two limits worth knowing before you enable it:

- **Web search is not supported in this runtime.** `source_mode: web` is refused whatever `web_search` is set to, rather than being served a model-mode answer under a web-mode contract.
- **`opencode run` exits 0 even when it fails**, so success is judged from the event stream. A run that produces no answer is reported as a failure rather than as an empty one.

There is no schema flag on this runtime, unlike Codex, Claude Code and Antigravity, so the response shape is stated in the prompt. A model that returns malformed JSON is asked once more in the same session, and a second failure ends the consultation.

</details>

<a id="github-copilot-runtime"></a>

<details>
<summary><strong>GitHub Copilot runtime</strong></summary>

<br>

[GitHub Copilot CLI](https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli) (`copilot`) reaches the models your Copilot plan includes, through the login you already have. It is consulted for answers only:

```yaml
    copilot:
      runtime: copilot
      command: copilot
      model: auto
      scores: { coding: 60, reasoning: 60 }
```

**Which model answers is your plan's decision.** On the free plan this was tested against, every explicit model name was refused and only `auto` was served, which lets Copilot pick a model for each call. A plan that offers named models takes them; one that does not fails the consultation as `configured_model_unavailable`, naming the model. `auto` names nothing to check an answer against, so the response reports the model Copilot says answered, and that can change from one call to the next. A concrete name is compared with the answering model, and a mismatch fails as `configured_model_unavailable`.

**Readiness costs no request.** Orchestrator asks the CLI to answer under a model name nobody has. Signed in, the CLI says the model is not available; signed out, it says there is no authentication information. That proves the sign-in and spends nothing. It does not prove that Copilot is reachable or that your quota is left, so a consultation that finds otherwise fails then. The CLI opens a session before it looks at the model, so the check names its own and removes it afterwards.

**Its state is not yours.** Copilot runs with `COPILOT_HOME` pointed at `~/.orchestrator-mcp/copilot/home` (mode `0700`), so its config and sessions are kept apart from your own `~/.copilot`, which Orchestrator neither reads nor writes. Whether the CLI itself looks in `~/.copilot` when told to use another home was not checked: under a different `HOME` it is signed out, so the test could not run. Sign in once against that home with `COPILOT_HOME=$HOME/.orchestrator-mcp/copilot/home copilot login`. On macOS the login lives in the system keychain, so one you already have carries over. Where there is no keychain, GitHub's documentation puts the token in a file under that home, which is why the command names it; that path has not been exercised here. The token variables `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` and `GITHUB_TOKEN` are not passed on, like every other credential: the child gets a fixed allowlist of environment variables.

**Answer-only, in layers.** The CLI has no read-only mode. Each consultation runs with every built-in tool switched off, the shell, write and url kinds denied, no custom instructions, the built-in MCP servers disabled and no remote sessions, and `--allow-all-tools` is never passed. The working directory is a fresh empty one, deleted afterwards; a `.mcp.json` or `.github/mcp.json` planted in it or beside it was neither listed nor started. The switch-off relies on an allowlist naming one tool that does not exist, which the CLI accepts and GitHub does not document, so the stream is checked as well: any `tool.*` event, or a message that asks for a tool, fails the turn as `protocol_validation_failed` rather than being returned as an answer. That check knows the events CLI 1.0.89 emits for a tool call and nothing else. A prompt asking it to create a file created nothing.

**What the layers do not cover.** The allowlist switches off a server's tools, not the server. An MCP server added to that home with `copilot mcp add` was still launched and connected on every run, and its tools were listed as disabled, so the model could not call it. Orchestrator therefore passes `--disable-mcp-server` for each server that home's `mcp-config.json` lists, on the readiness check too. The flag takes a name and no pattern, so a server that file does not list is not stopped. A hook in that home ran on a consultation under the same flags, whether it sat in `settings.json` or in `config.json` (the CLI moves those settings into `settings.json` itself), and no flag stops one. The setting `disableAllHooks` does, so Orchestrator writes it into that home's `settings.json` before every run and readiness check, keeping whatever else the file holds, and no hook in either file ran after that. A `settings.json` it cannot read as a JSON object, comments included, is named and left alone, and the run is refused rather than made with hooks on. Plugins were not examined, nor whether the setting covers what one brings. Keep that home for the login and the sessions and put nothing else in it.

**Sessions.** The prompt goes over stdin. `consultation_id` continues the same Copilot session: Orchestrator names each session with a UUID of its own and resumes it by that name, after checking that the session is still in that home. Without the check Copilot would start an empty one and answer as if the conversation had never happened, so a session removed from the home comes back as `session_not_found`. Session state grows by one directory per consultation and holds each prompt and answer unmasked. Deleting the consultation removes its directory and the lock beside it, and so does deleting the review or workflow that owns it, or the `retention_days` sweep. A directory that cannot be removed is left, and the delete still succeeds. A first turn the CLI refuses, such as one over a model your plan does not offer, leaves an empty directory that goes the same way. Nothing else in the home is removed: not what else the CLI keeps there, and not a session whose consultation is no longer in the database, such as one from before this release. Deleting `~/.orchestrator-mcp/copilot/home` clears all of it, and a consultation that used it can then no longer be continued.

**Spend.** Copilot reports premium requests and no price, so a turn's cost reads as unknown, never `$0.00`, and a dollar ceiling cannot count it. Set a turn ceiling ([Spending ceilings](#spending-ceilings)). Token counts are the turn's own, not the session's running total, and a turn whose stream shows more than one model call says so in `counts_incomplete`.

Limits worth knowing before you enable it:

- **Web search is not supported in this runtime.** `source_mode: web` is refused whatever `web_search` is set to, rather than being served a model-mode answer under a web-mode contract.
- **Consultation only.** `isolated_write` is refused for it; `patch` returns a diff for the host to apply.
- **There is no schema flag**, so the response shape is stated in the prompt and a malformed reply is asked for once more in the same session, as on OpenCode.
- **It leans on behavior GitHub does not document**: exit codes, the event stream, and the messages the readiness check reads, all checked against CLI 1.0.89. A turn fails when its stream has no closing `result` event, when that event names another session or a non-zero exit code, when a line that opens like an event is not JSON, or when there is no answer. An event type it does not know is skipped rather than judged, so something new in the stream passes unseen.
- **What you send is processed by GitHub** and the model provider behind the model Copilot picks, and the CLI sends GitHub its own telemetry, which Orchestrator does not turn off. See the [privacy policy](PRIVACY.md).

</details>

<details>
<summary><strong>Experimental Antigravity runtime</strong></summary>

<br>

Antigravity (`agy`) uses its own login and OS keyring, but its isolation is weaker than Codex or Claude Code:

- It inherits MCP servers from your `agy` settings. Headless mode denies tools by default, and Orchestrator fails the consultation if a tool step is reported, but this is detection rather than prevention. Do not enable it if you loosened headless permissions.
- It accepts prompts in process arguments rather than standard input. Other users on a shared machine may be able to read those arguments while the process runs.
- It has no login-status command, so readiness is reported as unverified until a real request succeeds or fails.

Large prompts are split across turns because Linux limits one argument to 128 KiB. Gemini models have handled this transport in testing; some non-Gemini models may reject the fragments as prompt injection. `reasoning_effort` and web mode are not available for this runtime.

</details>

## Local dashboard

The optional dashboard shows agents, routing decisions, prompts, answers, usage, latency, errors, reviews, recorded fix rounds, and workflows. It is off by default because it can display everything stored in the consultation database.

```yaml
consult:
  dashboard:
    enabled: true
    editable: false
    host: 127.0.0.1   # loopback only; `localhost` also works
    port: 8765        # 1-65535
```

A `host` that is not a loopback address is refused when the config loads, because the pages show every stored prompt and answer.

Start it separately:

```bash
ORCHESTRATOR_CONFIG=/absolute/path/to/config.yaml orchestrator-mcp-dashboard
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765), or wherever `host` and `port` point. The navigation has Monitor, Workflows, Reviews and Scorecard, all read-only; Agents and Reviewers appear only with `editable: true`.

`/workflows` lists every workflow; a workflow's page shows its state, fix rounds against the cap, the bindings frozen at start, and the step timeline in the order it happened, with each step linking to its consultation and its review. Those consultations are reachable nowhere else. The workflow pages are read-only: deletion stays on the MCP tools, where the confirmation token is.

`/scorecard` adds up each reviewer's answers and what became of its findings, the same numbers as `orchestrator-mcp-server scorecard` (`?days=90` widens the window from 30). It only reads, so `editable` does not gate it.

Set `editable: true` to manage consult agents and reviewer selection in the browser. Browser-managed agents are written to `~/.orchestrator-mcp/agents.yaml`; the dashboard never rewrites `config.yaml`, runs login commands, or starts consultations.

Both the MCP server and dashboard read configuration at startup. Restart them to pick up changes.

## Configuration

`ORCHESTRATOR_CONFIG` points to the YAML file. If unset, the server looks for `config.yaml` in its working directory.

| Setting | Default | Meaning |
|---|---|---|
| `agents` | required | The consult agents, keyed by id; at least one. Their options are under [How consultation works](#how-consultation-works), in the collapsed *Consult request fields and agent options*. |
| `database_path` | `~/.orchestrator-mcp/consultations.sqlite3` | Consultation, review, and workflow history. |
| `managed_agents_path` | `~/.orchestrator-mcp/agents.yaml` | Agents written by the dashboard. |
| `timeout_s` | `180` | Limit for one consultation turn. |
| `preflight_ttl_s` | `300` | How long a *ready* login check is reused before the CLI is probed again. Only a ready answer is cached; `0` probes once per turn. |
| `web_turn_limit` | `8` | Assistant turns allowed in web mode. Enforced by the Claude runtime only. |
| `score_margin` | `0` | How many points below the top capability score an agent may be and still win on `priority`, 0 to 100. `0` keeps the plain score order. See [How consultation works](#how-consultation-works). |
| `store_full_content` | `true` | Set false to keep metadata and routing only — except a review's goal and context, which are stored either way. Reviews cannot be finalized under it — see below. |
| `retention_days` | absent | Days of no activity after which finished history is deleted, at each server start and then roughly daily while it runs: workflows that are completed, failed, cancelled or `needs_attention`; reviews not running; ordinary consultations. A running or leased record stays. Absent keeps everything. |
| `context_roots` | empty | Directories `context_paths` may read on a consultation. Empty means the argument is not in the tool schema at all. |
| `personas` | none | Your own named emphases for `persona`, or a reworded built-in of the same name. A name is a lowercase slug of up to 32 characters; its text is 1 to 2,000. The six ready-made styles need no config. |
| `review` | absent | Configured reviewers; absent means no review tools. |
| `workflow` | absent | The three-phase workflow; absent means no workflow tools. Requires `store_full_content: true`. |
| `host` | runtime from the environment | Asserted host runtime, and the host model that makes same-runtime routing possible. |
| `spend` | no ceiling | Dollar and turn ceilings, per consultation, per review, and per workflow. See below. |
| `dashboard` | off | Loopback history UI (`host`, `port`) and optional agent editor. See [Local dashboard](#local-dashboard). |

### Spending ceilings

```yaml
consult:
  spend:
    max_cost_usd_per_consultation: 2.0
    max_cost_usd_per_review: 5.0
    max_cost_usd_per_workflow: 25.0
    max_turns_per_consultation: 8
    max_turns_per_review: 12
    max_turns_per_workflow: 40
```

All six are optional and all are absent by default, which is no ceiling and no change
in behavior. A consultation's ceiling is checked before each turn on it; a review's
before each round of reviewers; a workflow's before each step, reviewers included,
since the workflow is what paid for them. The check happens before the one-time token
is spent and before any subprocess starts, so a refusal costs nothing and the same
token still works once the ceiling is raised.

The three scopes nest, and a turn is refused by whichever it reaches first. The
consultation pair is the one that bounds `orchestrator_consult`: that tool takes a
`consultation_id` and resumes the session behind it, so every call after the first
spends another turn against the same paid CLI. Reviewers and workflow steps are
consultations too, so they are now bounded by their own ceiling as well as the ones
above them.

What this buys is bounded, and the bound is the point: a request cannot be priced
before it is made, so the guarantee is that **the next request after the ceiling is
crossed is refused**, not that spend never exceeds the ceiling. A fan-out of five
reviewers is one request in that sense.

An agent that reports no price contributes nothing to the total, which makes the
total a floor. The refusal names those agents rather than presenting the floor as a
sum. The error code is `spend_limit_reached`.

**Set a turn ceiling too if anything in your routing is on a flat-rate plan.** Codex,
GitHub Copilot and Antigravity report no per-turn price, so a dollar ceiling over them counts nothing
and never leaves `$0.00` -- it reads as a bound and is not one. Turns are counted for
every agent whatever it charges, so `max_turns_per_review` and `max_turns_per_workflow`
bound the work that money cannot see. They are checked at the same moments, refuse with
the same error code, and count the same way spend does: rebuilt from the turn ledger, so
a retried reviewer counts every attempt exactly once.

A consultation with no turns yet is never refused. Nothing has been spent on it, so
there is no ceiling for it to have reached -- what these stop is the turn after one.

**Plans carry an estimate.** A review plan and a workflow step preview show
`estimates` (tokens and cost per agent) and `estimated_cost_usd`, fitted from that
agent's last 50 successful turns on the same model. It is an estimate, not a quote:
`basis_turns` says how many turns it rests on, an agent with no history gets none,
and the cost is `null` for an agent with fewer than 3 priced turns -- so the total is
`null` whenever any agent is unpriced, the same rule as above. When the estimate would
reach a dollar ceiling the plan says so in `ceiling_warning`. That is advice only;
refusals still read what was actually spent.

### Watching a run

`ORCHESTRATOR_LOG_LEVEL` turns on stderr logging: routing decisions, child
processes started and exited, leases taken and lost, reviewer fan-out, and
workflow step transitions. `WARNING` by default, so an ordinary server is quiet;
`INFO` or `DEBUG` when you want to see what a slow run is doing.

```bash
ORCHESTRATOR_LOG_LEVEL=INFO
```

Records go to **stderr only**, never stdout — stdout is the MCP transport, and a
log line there is a corrupt protocol frame. Credential-shaped text is masked in
the rendered line before it is written, on the same best-effort basis as the
database copy.

The tools that can run for minutes — `orchestrator_consult`,
`orchestrator_consult_many`, `orchestrator_review_run`, `orchestrator_retry_review` and
`orchestrator_workflow_run_step` — also emit MCP progress notifications: a
heartbeat every 15 seconds carrying elapsed time against the configured timeout,
and agent or reviewer counts as each one answers. Clients that ask for progress see them;
clients that do not are unaffected.

`consult` is the only top-level section. Configuration from releases before 0.4 containing `capabilities`, `model_list`, `router_settings`, or `limits` is rejected at startup because direct API routing was removed.

## System requirements

- macOS or Linux. Windows is not currently tested.
- Python 3.11 or newer. CI currently tests 3.11 through 3.14 against the lockfile, and
  3.14 against the newest release of every dependency.
- Homebrew or [`uv`](https://docs.astral.sh/uv/).
- A stdio MCP client such as Claude Code or Codex.
- At least one eligible configured agent. Ordinary consultation requires another
  runtime; reviews and workflows may use a provably different versioned model on the
  host's runtime.

## Test it

The offline suite uses fake CLI agents. It needs no network and spends no model capacity:

```bash
uv sync
uv run pytest -q -n auto
```

Live smoke tests use the agents in your configuration:

```bash
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_consult_live.py
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_review_live.py
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_workflow_live.py
```

Live tests make real requests and may use paid capacity. Do not run them in CI unless that is intentional.

## Troubleshooting

| Problem | Fix |
|---|---|
| `config not found: config.yaml` | Set `ORCHESTRATOR_CONFIG` to an absolute path. |
| The server connects, but its only tool is `orchestrator_setup` | There is no file at the config path. The tool's reply names the path it read and the `init --path` command that writes it there; if your config is somewhere else, point `ORCHESTRATOR_CONFIG` at it. Reconnect after either. |
| `no_agent_available` | Give an enabled, non-host agent a positive score for the requested capability. |
| `agent_not_installed` | Use an absolute path for `command`; GUI apps often inherit a smaller `PATH`. |
| `connection_required` | Run the login command returned in `required_action`, then retry. |
| Host runtime error | Set `ORCHESTRATOR_HOST_RUNTIME` to `claude`, `codex`, `opencode`, `copilot`, or `antigravity`. |
| The client lists the server as failed | Read the client's own stderr first; it carries the real message. `orchestrator-mcp-server --version` tells you only whether *that* shell can find the command, which a GUI client's smaller `PATH` may not. Running it with no arguments is what exercises the configuration: it either names the key to fix, or goes quiet waiting on stdin because the configuration is fine. |
| Every consultation starts over | Return the previous `consultation_id` on the next call. |
| `timeout` during a review | Raise `consult.timeout_s`; high-effort review can take much longer than 180 seconds. |
| Dashboard changes do not appear | Restart the MCP server; configuration is loaded at startup. |
| Startup names a removed block | Delete pre-0.4 direct-routing keys: `capabilities`, `model_list`, `router_settings`, and `limits`. |

## Deliberately not included

- No direct provider API routing or provider API-key configuration.
- No file edits, shell commands, MCP tools, or subagents for ordinary consulted agents;
  explicit web mode enables only the target runtime's web-search facility.
- No automatic fixes; the host agent owns edits and tests. A workflow records and validates the phases, it does not run the job unattended.
- No delegated write to your actual working tree. `isolated_write` runs in a throwaway worktree, and only where the runtime can be contained: Codex, and OpenCode where Orchestrator's OS-level sandbox holds. Claude Code, GitHub Copilot and Antigravity refuse it. The host applies every patch.
- No streaming; each consultation returns one complete envelope.
- No dashboard-initiated consultations.
- No automatic configuration reload.
- No multi-user or shared state.
- No account system for the loopback dashboard.

## Contributing

Issues and pull requests are welcome.

1. Fork the repository and create a branch.
2. Make the change and add a test that fails without it.
3. Run `uv run pytest -q`.
4. Open a pull request.

Keep private configuration, login data, and consultation databases out of commits. For bugs, [open an issue](https://github.com/crAK1644/orchestrator-mcp/issues) with the response envelope after removing paths, credentials, and other private information.

## License

[MIT](LICENSE) · [PyPI](https://pypi.org/project/orchestrator-mcp-server/) · [GitHub issues](https://github.com/crAK1644/orchestrator-mcp/issues) · [Privacy](PRIVACY.md) · [Security](SECURITY.md)

Built with [Pydantic](https://docs.pydantic.dev) and the [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
