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
  <a href="https://pypi.org/project/orchestrator-mcp-server/"><img src="https://img.shields.io/pypi/v/orchestrator-mcp-server?style=flat" alt="PyPI version"></a>
  <a href="https://pypi.org/project/orchestrator-mcp-server/"><img src="https://img.shields.io/pypi/pyversions/orchestrator-mcp-server?style=flat" alt="Python versions"></a>
  <a href="https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml"><img src="https://github.com/crAK1644/orchestrator-mcp/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/crAK1644/orchestrator-mcp?style=flat" alt="MIT License"></a>
</p>

<p align="center">
  <a href="#before--after">See it</a> ·
  <a href="#install">Install</a> ·
  <a href="#what-you-get">What you get</a> ·
  <a href="#reviews-with-a-checkpoint">Reviews</a> ·
  <a href="#the-three-phase-workflow">Workflow</a> ·
  <a href="#security-model">Security</a> ·
  <a href="#local-dashboard">Dashboard</a>
</p>

---

Orchestrator MCP is a local [Model Context Protocol](https://modelcontextprotocol.io) server that lets one coding agent consult another. It launches the Codex, Claude Code, OpenCode, or experimental Antigravity CLI already installed and authenticated on your machine, routes the request, and returns a structured answer.

It does not ask for a provider key, proxy provider traffic, or silently switch models. Authentication remains inside each vendor's CLI.

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
 Any host          ──►  Orchestrator MCP  ──►  OpenCode CLI (DeepSeek, Qwen, local…)
 Any host          ──►  Orchestrator MCP  ──►  Antigravity CLI (experimental)

                         local routing
                    no provider API keys
                   no same-runtime loops
```

The host's own runtime is always excluded. Claude Code cannot consult Claude Code through this server, and Codex cannot consult Codex.

## Install

**Homebrew:**

```bash
brew tap crAK1644/tap
brew install orchestrator-mcp-server
```

Apple Silicon uses a prebuilt package. Intel macOS and Linux build dependencies from source; use the [`uvx` option](#run-with-uvx-instead) if you want a faster, temporary install.

### 1. Sign in to the agent CLIs

Sign in to each agent you want Orchestrator to use:

```bash
codex login
claude auth login
```

These are the normal Codex and Claude Code login flows. Orchestrator checks readiness, but never reads or stores their credentials.

For OpenCode, sign in once with `opencode auth login` for whichever provider you plan to consult. Hosted providers only — this server does not run a model on your machine. See the [OpenCode runtime](#opencode-runtime--deepseek-qwen-kimi) section below.

### 2. Create `config.yaml`

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

See [`config.example.yaml`](config.example.yaml) for every option, an OpenCode agent, and an experimental Antigravity example.

### 3. Add the server to your MCP client

<details open>
<summary><strong>Claude Code</strong></summary>

<br>

```bash
claude mcp add orchestrator \
  --env ORCHESTRATOR_CONFIG=$PWD/config.yaml \
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
env = { ORCHESTRATOR_CONFIG = "/absolute/path/to/config.yaml", ORCHESTRATOR_HOST_RUNTIME = "codex" }
```

</details>

Restart the MCP client after changing its configuration.

> [!TIP]
> Use an absolute `ORCHESTRATOR_CONFIG` path. GUI-launched clients often start in a different working directory and inherit a smaller `PATH` than your terminal.

### Run with `uvx` instead

<details>
<summary><strong>Show the temporary-install configuration</strong></summary>

<br>

No permanent server install is required:

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
| **Three-phase workflow** | Run a whole job — research and planning, implementation and testing, review and fixing — with any model bound to any step it scores for. |
| **Local history** | Store consultations and reviews in SQLite, with an optional loopback dashboard. |
| **Answer-only isolation** | Codex, Claude Code, and OpenCode are prevented from using tools; experimental Antigravity detects and fails reported tool use but cannot yet prevent it. |

### The consultation tools

| Tool | Purpose |
|---|---|
| `orchestrator_consult` | Start or continue a structured consultation. |
| `orchestrator_list_consult_agents` | Show configured agents, routing scores, installation, and login readiness. |
| `orchestrator_get_consultation` | Retrieve a stored consultation, its turns, usage, and routing decision. |
| `orchestrator_delete_consultation` | Delete one ordinary consultation and its local turns. |
| `orchestrator_request_delete_all_consultations` / `orchestrator_delete_all_consultations` | Preview and confirm deletion of an exact ordinary-history snapshot. |

These deletion tools remove local SQLite records only. They cannot erase a consulted
runtime's own CLI or provider history.

Three independent opt-ins: the consult tools are always advertised, the review tools only with a `consult.review` block, the workflow tools only with a `consult.workflow` block. Reviewers are not a workflow, and a workflow is not reviewers.

## How consultation works

`orchestrator_consult` selects the eligible agent with the highest capability score. Lower `priority` wins a score tie; agent ID breaks the final tie. A missing capability or a score of `0` makes an agent ineligible.

The selected CLI runs under its existing login and returns one response envelope:

| Field | Meaning |
|---|---|
| `ok` | False exactly when `error` is set. Check this before reading the answer. |
| `consultation_id` | Handle for continuing the same native conversation. |
| `content` | Answer, assumptions, uncertainties, follow-up questions, and sources. |
| `route` | Agent, runtime, model, score, priority, and whether it was selected explicitly. |
| `usage` | Token counts when the CLI reports them. |
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
| `capability` | yes | `coding`, `research`, `writing`, `reasoning`, or `review`. |
| `prompt` | yes | Task or question, up to 100,000 characters. |
| `context` | no | Evidence, up to 1,000,000 characters. |
| `source_mode` | no | `auto`, `document`, `web`, or `model`. |
| `consultation_id` | no | Return the previous ID to continue the conversation. |
| `target_agent` | no | Choose one configured agent instead of automatic routing. |
| `conversation_label` | no | Label stored with the consultation, up to 200 characters. |

Agent configuration:

| Option | Default | Meaning |
|---|---|---|
| `runtime` | required | `codex`, `claude`, `opencode`, or `antigravity`. |
| `command` | required | Executable name or absolute path. |
| `model` | required | Requested model and, where possible, verified responding model. |
| `priority` | `100` | Lower wins a score tie. |
| `enabled` | `true` | Keep the agent configured but out of routing when false. |
| `scores` | none | 0–100 per capability; missing means ineligible. |
| `web_search` | `false` | Permit `source_mode: web` for this agent. |
| `reasoning_effort` | unset | `low`, `medium`, `high`, `xhigh`, or `max`; Codex only. |

</details>

## Reviews, with a checkpoint

A consultation asks one agent. A review asks one or more configured reviewers the same question over the same material.

```text
 plan review          approve + run          synthesize
 sends nothing   ──►  reviewers answer  ──►  host records conclusion
      │                    in parallel                │
      └─ scope              one-time token            └─ every Critical kept
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
```

The workflow is deliberately split:

1. `orchestrator_review` creates a plan and **sends nothing**. The plan shows reviewers, material size, web access, request count, and locations of credential-shaped text.
2. Show that plan to the user. `orchestrator_review_run` spends its one-time token and asks reviewers in parallel.
3. Read every result and call `orchestrator_finalize_review`. Reviewer replies alone leave the review at `awaiting_synthesis`.

The checkpoint binds the scope and makes the token single-use, but it is advisory:
MCP gives the server no separate human channel, so it cannot prove who saw the plan.
Human approval depends on the calling client's tool-confirmation experience or an
external gate.

Finalization must preserve every machine-readable Critical finding, even when other reviewers disagree with it. Deep mode also requires the host agent to record its own findings before seeing the reviewers' answers.

> [!IMPORTANT]
> Material sent to a reviewer may remain in that vendor CLI's own history. Orchestrator cannot erase Codex, Claude Code, OpenCode, or Antigravity session logs.

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
| `orchestrator_apply_fixes` | Return selected findings and fix steps. Changes no files. |
| `orchestrator_record_fix_round` | Record the host's claim about a fix round. |
| `orchestrator_test_reviewers` | Check installation and login readiness without sending project material. |
| `orchestrator_get_review` / `orchestrator_list_reviews` | Read one review or recent review metadata. |
| `orchestrator_delete_review` | Delete a review, its rechecks, and linked consultations. |
| `orchestrator_request_delete_all` / `orchestrator_delete_all_reviews` | Preview and confirm deletion of an exact history snapshot. |

Reviews default to `web: false`. Reviewers cannot change files or run commands. `orchestrator_apply_fixes` is a plan for work the host agent performs; it never applies a patch itself.

Credential-shaped values are masked before storage. `secrets="send_as_is"` is an explicit escape hatch for a false positive: it requires the exact original goal and context again, sends those originals to the reviewers, and still stores only the redacted copy.

`store_full_content: false` does not apply here in full. A review's goal and context are stored either way — the second half of the approval handshake reads them back to send what was approved — and reviewer answers and findings are not. That leaves nothing to prove every Critical survived synthesis, so `orchestrator_finalize_review` refuses, and the review stays at `awaiting_synthesis`. Finalization is refused on the same grounds when a reviewer answered only in unparseable prose, or when its findings were truncated.

</details>

## The three-phase workflow

A consultation is one question. A review is one body of material. A **workflow** is a
whole job held together: research and planning, implementation and testing, review and
fixing, the last two in a capped loop, with every consultation and review it produces
hanging off one `workflow_id`.

```text
 research → plan → author_execution_prompt → implement → test → review → synthesize
                                                 ▲                          │
                                                 └────────── fix ◄──────────┘
                                                   capped at max_fix_rounds
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
      implement: {agent: deepseek-flash, execution: patch}
      apply_patch: {executor: host}
      test:      {executor: host}
      review:    {agents: [codex-sol, claude-opus]}
      synthesize: {executor: host}
      fix:       {agent: codex-sol, execution: patch}
```

`workflow:` requires `store_full_content: true` and refuses at startup otherwise. A
workflow *is* its stored plans, briefs, patches and reports, and the review step cannot
finalize without them — the failure belongs at boot, not several paid steps in.

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
host, which is the conservative default.

Steps route on the four capabilities added for this: `planning`, `prompt_authoring`,
`testing` and `synthesis`, alongside `coding`, `research` and `review`.

Bindings are resolved and **snapshotted at workflow creation**. Editing `config.yaml`
does not reroute a running workflow; that takes `orchestrator_workflow_plan_replan` and
its own approval.

### Execution modes, and what each one can reach

`execution_modes` on an agent is **operator trust, not capability**. What actually
happens is the intersection of that list with what the runtime can be made to do, and a
refusal names which side said no.

| Mode | The agent gets | Repository access |
|---|---|---|
| `consultation` | The read-only consult path, unchanged. | `context_only` |
| `patch` | The same read-only path; it returns a unified diff. The host applies it. | `context_only` |
| `isolated_write` | A disposable git worktree outside your repository, checked out at the workflow's baseline. The agent edits and runs commands there; Orchestrator reads the diff back out of git and the host applies it. **Codex only.** | `worktree` |
| `executor: host` | Not an agent at all: the host edits its own checkout. | `active_tree` |

| Runtime | `isolated_write` | Why |
|---|---|---|
| `codex` | **supported** | `sandbox_mode: workspace-write` with `approval_policy: never` is enforced by the CLI at OS level: a command aimed outside the worktree comes back `Operation not permitted` from the kernel, not from the model declining. Network is off, `/tmp` and `$TMPDIR` are excluded from the writable set. |
| `opencode` | refused | Its permission set isolates *configuration*, not filesystem effects: an allowed shell command can leave the worktree. |
| `claude` | refused | No contained executor yet — same bar as OpenCode. |
| `antigravity` | refused | Writing needs `--dangerously-skip-permissions`, the one flag the adapter refuses by construction. |

A root allowlist and a prompt instruction are not containment. An agent that declares
`isolated_write` on a runtime that cannot be contained is refused **at startup**, not at
routing time.

**No delegated agent writes to your working tree.** In `patch` mode the agent never sees
your checkout — only the context the host sent it, exactly as a reviewer does — and the
diff comes back for the host to apply. In `isolated_write` it sees a *copy*: a worktree
under `~/.orchestrator-mcp/worktrees/<workflow_id>/<step_id>/`, checked out at the latest
applied result (the workflow's baseline until the host has applied anything) and deleted
as soon as the diff is captured. Either way the step ends in
`awaiting_host_apply` and the host owns the branch. `ConsultAdapter` gained nothing for
any of this: it is still three verbs with no way to ask for anything agentic, and write
capability lives in a separate package behind a separate protocol.

Three things about a contained run are worth knowing before you use one:

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

A failed test returns to `fixing` rather than advancing, unless `advance_on_failed_test`
is set.

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

A fix round carries the findings that are still open, read back from the review row
rather than from workflow storage, so the round is an answer to the review and not a
second pass at the goal. A re-review gets them too.

### The execution contract, honestly scoped

The coding prompt is assembled by this code: our `EXECUTION_CONTRACT` first, then the
scope, accepted plan, authored brief and prior findings as a JSON payload. An authored
brief is data inside that payload, and no field turns it into contract text.

That is **code ownership, not a transport-level enforcement boundary**. Claude Code has
a real system-prompt channel; Codex and OpenCode receive one compiled text, so there the
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
| `orchestrator_workflow_status` | State, artifacts, selected agents, round count, and what may happen next. |
| `orchestrator_workflow_plan_replan` / `orchestrator_workflow_replan` | Change the binding snapshot under the same preview-and-approve handshake. |
| `orchestrator_workflow_cancel` | Cancel pending work and terminate a child this process owns, with the same caveat `orchestrator_cancel_review` carries about another process's children. |

A review step's reviewers come from its binding, so a workflow needs no `review:` block
unless that binding is left to `auto` — then it falls back to the configured reviewers
and refuses without them.

A workflow's consultations are not reachable from the public tool: resuming one by its
`consultation_id` is refused with `workflow_owned_session`. Steps take a lease, so a
crashed run resolves rather than leaving a status that outlives its process.

**Patch integrity.** Redaction can rewrite credential-shaped text, and a rewritten patch
is a corrupt patch. So the raw diff is returned to the host **once**, what is stored is a
sanitized audit copy plus the sha256 of the raw patch, and the sanitized copy is never
offered for application. After the host applies it, the resulting commit is the review
artifact.

</details>

## Security model

| Property | Guarantee |
|---|---|
| **Credentials** | No provider key setting exists. Orchestrator never reads, stores, returns, or refreshes a CLI's own credential. A credential you put in a prompt is material, not a credential here — see the warning below. |
| **Process launch** | Commands are executed as argument lists, never through a shell. |
| **Self-consultation** | `ORCHESTRATOR_HOST_RUNTIME` comes from the environment and cannot be overridden by a tool call. |
| **Agent permissions** | Consulted agents are answer-only, except for the target CLI's bounded search in explicit web mode. |
| **Model identity** | A detected mismatch fails with `configured_model_unavailable`. Missing CLI metadata is reported as unverified, not invented. |
| **Storage** | SQLite directory permissions are `0700`; the database and managed agent file are `0600`. |
| **Dashboard** | Loopback only, with host-header checks and a per-process token. |
| **Review checkpoint** | Plans bind the scope to a one-time token before reviewer requests are made. The server cannot independently verify human approval. |
| **Workflow write surface** | No delegated agent writes to your working tree. `patch` mode returns a diff over the read-only consult path; `isolated_write` runs codex inside its own OS-level sandbox, in a disposable worktree outside your repository, with the network off. Both end in `awaiting_host_apply`: the host owns application. |
| **Workflow checkpoints** | One token per side-effecting step, spent in the statement that starts it. There is no workflow-level token, and a token proves snapshot integrity rather than human approval. |
| **Workflow identity** | The host execution identity comes from startup configuration only. A candidate that cannot be *proven* a different model from the host is refused. |
| **Workflow scope** | A workdir must resolve beneath a configured root; `/` is refused and nothing is inferred from the working directory. A dirty tree needs explicit acknowledgement. |

> [!WARNING]
> **Redaction covers every retained database copy, and only this database.** Credential-shaped values are replaced before every insert while the target CLI still receives the original material. Detection is best-effort pattern matching rather than a scanner with perfect recall, so a secret with no recognizable shape can survive it. Keep the database private, or set `store_full_content: false`.
>
> **Vendor history is outside all of this.** Material sent to a reviewer also lands in that reviewer's own CLI history — Codex writes `~/.codex/sessions/`, and the others keep their own logs. Orchestrator cannot redact or erase those files. It does read from them, in three places and for two fields: the Codex adapter opens the rollout file for the session it just ran to recover the model identity the CLI does not otherwise report, and opens the newest rollout to read the latest Codex CLI rate-limit numbers; the OpenCode adapter runs `opencode export` on the session it just ran, for the same reason — the model identity is absent from that runtime's event stream. Nothing else is taken from any of them.

Two more limits worth knowing:

- CLI error text is shortened and common secret formats are redacted, but an unusual one may still appear in a returned error. Do not forward a raw error envelope somewhere untrusted.
- A caller-supplied JSON Schema is trusted input. A pathological regular expression in one can consume a large amount of CPU.

Orchestrator checks structure, routing, permissions, and model identity where observable. It cannot prove that a model's factual claims are true.

<details>
<summary><strong>OpenCode runtime — DeepSeek, Qwen, Kimi</strong></summary>

<br>

[OpenCode](https://opencode.ai) is one CLI in front of many hosted providers, which is how models the other three runtimes do not carry become reachable without Orchestrator holding a key. Models are addressed as `provider/model`:

```yaml
    deepseek-flash:
      runtime: opencode
      command: opencode
      model: opencode/deepseek-v4-flash-free
      scores: { coding: 60, reasoning: 60 }
```

**Hosted providers only. Orchestrator does not run a model on your machine, or on anyone's.** Depending on the selected model, this runtime consults a subscription you already hold or OpenCode's anonymous free tier. A locally served model — Ollama, LM Studio, your own endpoint — cannot be reached through it, and that is enforced by construction rather than by a check: a consultation runs under a configuration with no `provider` block at all, and a provider block is the only place an endpoint outside OpenCode's own catalogue is ever named. Verified against the CLI: under this configuration `--model ollama/qwen2.5:7b` fails with `ProviderModelNotFoundError` and the local server is never contacted. The constraint is real — if the model you want is one you host yourself, this runtime cannot consult it.

**Readiness is asked under that same configuration.** Orchestrator runs `opencode models` with your global config out of reach and checks that the agent's `provider/model` is listed, so readiness answers the question the consultation will actually ask rather than reporting a model that would then fail to resolve. A provider that needs a credential wants `opencode auth login` once; stored keys and OAuth tokens live in OpenCode's data directory, which Orchestrator neither reads nor relocates. The child gets a fixed allowlist of environment variables — `HOME`, `PATH`, `LANG` and a few more — and every `*_API_KEY` in this server's own environment is excluded from it.

Each consultation runs under a configuration Orchestrator writes and nothing crosses over from your own: permissions denied outright, no MCP servers, no plugins, no instructions, no project agents, no providers, sharing and auto-update off. `OPENCODE_CONFIG` merges rather than replaces, so `XDG_CONFIG_HOME` is pointed at an empty directory as well — your global config is out of reach, not merely outranked.

The working directory is `~/.orchestrator-mcp/opencode/<agent>`, mode `0700` with the configuration files `0600`, holding nothing else. One per agent rather than one shared: the files are identical for every agent today, and one release that gives an agent something of its own to write would turn that into a race between a consultation starting and reading its configuration. Under `$HOME` rather than `/tmp` deliberately: Orchestrator refuses to run if it finds an `opencode.json` or `opencode.jsonc` above that directory — a parent's permissions outrank its own — and that check can only run *before* OpenCode reads the chain, so every ancestor needs to be a directory no one else can write to. It is not deleted afterwards, and cannot be: OpenCode records a session's directory and re-resolves it on resume, so a per-run temporary directory would break every follow-up turn.

Two limits worth knowing before you enable it:

- **Web search is not supported in this runtime.** `source_mode: web` is refused whatever `web_search` is set to, rather than being served a model-mode answer under a web-mode contract.
- **`opencode run` exits 0 even when it fails**, so success is judged from the event stream. A run that produces no answer is reported as a failure rather than as an empty one.

There is no schema flag on this runtime, unlike the other three, so the response shape is stated in the prompt. A model that returns malformed JSON is asked once more in the same session, and a second failure ends the consultation.

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

The optional dashboard shows agents, routing decisions, prompts, answers, usage, latency, errors, reviews, and recorded fix rounds. It is off by default because it can display everything stored in the consultation database.

```yaml
consult:
  dashboard:
    enabled: true
    editable: false
```

Start it separately:

```bash
ORCHESTRATOR_CONFIG=/absolute/path/to/config.yaml orchestrator-mcp-dashboard
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

Set `editable: true` to manage consult agents and reviewer selection in the browser. Browser-managed agents are written to `~/.orchestrator-mcp/agents.yaml`; the dashboard never rewrites `config.yaml`, runs login commands, or starts consultations.

Both the MCP server and dashboard read configuration at startup. Restart them to pick up changes.

## Configuration

`ORCHESTRATOR_CONFIG` points to the YAML file. If unset, the server looks for `config.yaml` in its working directory.

| Setting | Default | Meaning |
|---|---|---|
| `database_path` | `~/.orchestrator-mcp/consultations.sqlite3` | Consultation and review history. |
| `managed_agents_path` | `~/.orchestrator-mcp/agents.yaml` | Agents written by the dashboard. |
| `timeout_s` | `180` | Limit for one consultation turn. |
| `web_turn_limit` | `8` | Assistant turns allowed in web mode. Enforced by the Claude runtime only. |
| `store_full_content` | `true` | Set false to keep metadata and routing only — except a review's goal and context, which are stored either way. Reviews cannot be finalized under it — see below. |
| `review` | absent | Configured reviewers; absent means no review tools. |
| `workflow` | absent | The three-phase workflow; absent means no workflow tools. Requires `store_full_content: true`. |
| `host` | runtime from the environment | Asserted host runtime, and the host model that makes same-runtime routing possible. |
| `dashboard` | off | Loopback history UI and optional agent editor. |

`consult` is the only top-level section. Configuration from releases before 0.4 containing `capabilities`, `model_list`, `router_settings`, or `limits` is rejected at startup because direct API routing was removed.

## System requirements

- macOS or Linux. Windows is not currently tested.
- Python 3.11, 3.12, or 3.13.
- Homebrew or [`uv`](https://docs.astral.sh/uv/).
- A stdio MCP client such as Claude Code or Codex.
- At least one other supported agent CLI installed and signed in.

## Test it

The offline suite uses fake CLI agents. It needs no network and spends no model capacity:

```bash
uv sync
uv run pytest -q
```

Live smoke tests use the agents in your configuration:

```bash
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_consult_live.py
ORCHESTRATOR_HOST_RUNTIME=claude uv run python smoke_review_live.py
```

Live tests make real requests and may use paid capacity. Do not run them in CI unless that is intentional.

## Troubleshooting

| Problem | Fix |
|---|---|
| `config not found: config.yaml` | Set `ORCHESTRATOR_CONFIG` to an absolute path. |
| `no_agent_available` | Give an enabled, non-host agent a positive score for the requested capability. |
| `agent_not_installed` | Use an absolute path for `command`; GUI apps often inherit a smaller `PATH`. |
| `connection_required` | Run the login command returned in `required_action`, then retry. |
| Host runtime error | Set `ORCHESTRATOR_HOST_RUNTIME` to `claude`, `codex`, `opencode`, or `antigravity`. |
| Every consultation starts over | Return the previous `consultation_id` on the next call. |
| `timeout` during a review | Raise `consult.timeout_s`; high-effort review can take much longer than 180 seconds. |
| Dashboard changes do not appear | Restart the MCP server; configuration is loaded at startup. |
| Startup names a removed block | Delete pre-0.4 direct-routing keys: `capabilities`, `model_list`, `router_settings`, and `limits`. |

## Deliberately not included

- No direct provider API routing or provider API-key configuration.
- No file edits, shell commands, MCP tools, or subagents for consulted agents.
- No automatic fixes; the host agent owns edits and tests. A workflow records and validates the phases, it does not run the job unattended.
- No delegated write to your actual working tree. `isolated_write` runs in a throwaway worktree and is codex-only; every other runtime refuses it, and the host applies every patch.
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

[MIT](LICENSE) · [PyPI](https://pypi.org/project/orchestrator-mcp-server/) · [GitHub issues](https://github.com/crAK1644/orchestrator-mcp/issues)

Built with [Pydantic](https://docs.pydantic.dev) and the [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
