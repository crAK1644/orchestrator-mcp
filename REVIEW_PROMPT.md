# Adversarial review request — Consult Protocol v1

You are reviewing a feature branch before release. Find real defects. Do not
summarize the code back to me, do not praise it, and do not propose stylistic
rewrites. I want the bugs.

## What the code is

`orchestrator-mcp-server` is an MCP server. Until now it had one path: an `ask`
tool that routes a request to a **model** through `litellm.Router`. This branch
adds a second, independent path — **Consult Protocol v1** — that routes a request
to an **agent runtime**: the Codex CLI or the Claude Code CLI, driven as a local
subprocess, over text only.

Why a subprocess and not an API call: a CLI runtime brings its own logged-in
account, its own web search, and its own conversation state, so Claude Code can
consult GPT-5.6 Sol and Codex can consult Opus without either side holding the
other vendor's API key.

The shape of one consultation:

1. Host (an agent, e.g. Claude Code) calls the `consult` MCP tool with a
   capability, a prompt, optional context, an optional source mode, and — on
   later turns — a `consultation_id`.
2. The service resolves the source mode, routes to an agent, creates or loads a
   consultation row, takes a cross-process lease, preflights the CLI, compiles a
   prompt, spawns the CLI, parses its output, validates it against a JSON schema,
   records the turn, and returns an envelope.
3. The envelope is `ok: true` with content, or `ok: false` with one of a closed
   set of error codes. It is never an exception across the MCP boundary.

## Invariants — a violation of any of these is a top-severity finding

1. **No self-delegation.** The host runtime comes from the `ORCHESTRATOR_HOST_RUNTIME`
   environment variable, never from a tool argument, and every agent whose runtime
   equals it is excluded from routing. A model that could name the host runtime
   could point the work straight back at itself.
2. **No silent substitution of the agent.** If the selected agent is uninstalled,
   logged out, or fails, the consultation stops and says so. It never falls
   through to the next-best candidate. Likewise, a consultation is pinned to its
   agent for life: continuing it against a different one is `session_target_mismatch`,
   never a new conversation wearing the old id.
3. **No silent substitution of the model.** Both CLIs can fall back internally. If
   the model actually used does not match the configured one, that is
   `configured_model_unavailable`, not an answer.
4. **No credentials, ever.** The orchestrator must never read, copy, return, log,
   or store OAuth credentials, tokens, or the output of an auth command. Preflight
   may learn *whether* an agent is logged in and nothing else. Environment
   variables are never persisted.
5. **No shell.** Every subprocess is `asyncio.create_subprocess_exec` with an
   argument list. The prompt goes in over stdin, never in argv.
6. **The consulted agent may not act.** It answers text. Any command execution,
   file change, MCP call, or subagent event in its output stream is
   `protocol_validation_failed` — the answer is discarded, not sanitized.
7. **Failures are envelopes.** A caller gets a code it can branch on. The only
   exception that may cross the MCP boundary is a schema violation the MCP layer
   itself raises.
8. **Backward compatibility is absolute.** A config with no `consult:` block must
   start a server that advertises exactly `ask` and `list_capabilities`, byte for
   byte identical to 0.1.2. There is a snapshot test asserting this.
9. **The dashboard is read-only and loopback-only.** No writes, no YAML editing,
   no subprocess spawned by a GET.

## Files, in the order they build on each other

```
orchestrator_mcp/consult/errors.py            closed set of error codes
orchestrator_mcp/consult/contract.py          request/response models, JSON schema shaping
orchestrator_mcp/consult/config.py            the `consult:` config block, host_runtime()
orchestrator_mcp/consult/routing.py           deterministic agent selection
orchestrator_mcp/consult/prompts.py           versioned prompt compiler
orchestrator_mcp/consult/store.py             SQLite: consultations, turns, routing, leases
orchestrator_mcp/consult/adapters/base.py     subprocess transport, model check, content parse
orchestrator_mcp/consult/adapters/codex_cli.py
orchestrator_mcp/consult/adapters/claude_cli.py
orchestrator_mcp/consult/adapters/__init__.py
orchestrator_mcp/consult/service.py           composes all of the above
orchestrator_mcp/consult/dashboard.py         read-only loopback HTTP view
orchestrator_mcp/server.py                    one conditional branch in build_server
```

Tests: `tests/test_consult_*.py`, `tests/test_{claude,codex}_adapter.py`,
`tests/fixtures/agent_stub.py`. 269 pass, 1 skipped, with neither CLI installed
and no network.

## Where I most expect to be wrong — attack these first

- **Subprocess lifecycle.** `run_streaming` in `adapters/base.py`. Children start
  with `start_new_session=True` and are killed by process group. Can a child
  survive a timeout, a cancellation, a callback rejection, or an exception in the
  stderr drain? Can the parent deadlock on a full stderr pipe? Is the stream line
  limit large enough for a real JSONL event, and what happens when it is exceeded?
- **Concurrency.** `store.lease()` uses `BEGIN IMMEDIATE` + `INSERT OR IGNORE` for
  a cross-process lock with a TTL. Can two processes advance the same native CLI
  session? Can a lease leak on a crash and wedge a consultation past its TTL? Is
  the SQLite connection safe to use from `asyncio.to_thread` workers concurrently
  (`check_same_thread=False`, `isolation_level=None`) — are there interleavings
  where two `to_thread` calls corrupt each other's transaction?
- **Migrations.** `_migrate()` splits a migration on `;` and runs it statement by
  statement inside `BEGIN IMMEDIATE`, because `executescript` implicitly commits.
  Where does that break?
- **Output parsing.** Both adapters parse a CLI's JSON/JSONL. What malformed,
  truncated, adversarial, or merely unexpected output produces a wrong answer
  rather than a `protocol_validation_failed`? Consider a consulted agent that is
  itself trying to break out — its output is untrusted.
- **The model check.** `check_model()` in `base.py` does case-insensitive
  containment in both directions, and treats missing metadata as "not evidence of
  substitution". Where does that let a substitution through?
- **Prompt injection.** Context supplied by the caller and content returned by the
  consulted agent are both untrusted. Can either one redirect the protocol,
  disable the contract, or make a later turn misread?
- **Session binding.** MCP gives this server no reliable host conversation id, so
  the returned `consultation_id` is the only binding. What happens when a caller
  loses it, replays an old one, guesses one, or sends one from a different
  database?
- **The dashboard.** `mode=ro` connection, `Host` header check, `html.escape` on
  every value. Is there a path that writes, a value that reaches the page
  unescaped, an SSRF or rebinding angle, or a way to make a GET spawn a process?
- **Resource exhaustion.** Unbounded prompt size, unbounded context, unbounded
  stored rows, a web-mode consultation that spends money until the turn limit.

## Ground truth about the CLIs — do not "correct" these

Verified against the installed Claude Code 2.1.220 on the development machine:

- `--json-schema` takes **inline JSON**, not a file path.
- `--output-format stream-json` **requires** `--verbose`.
- There is **no `--max-turns`**. The web-mode turn budget is enforced by this code
  counting assistant events and killing the child.
- `claude auth status --json` exits 0 whether logged in or out, so preflight reads
  the JSON body, not the exit code. Subcommands are `login`, `logout`, `status`.

Codex is **not installed** on the development machine, so its flags and JSONL
event names come from documentation and are pinned only by fixtures. Treat the
Codex adapter as the less-verified one and say so if you spot a likely mismatch.

One deliberate deviation from the plan: `CODEX_HOME` is **not** relocated to a
scratch directory. Isolating it would take the user's saved credentials out of
scope and turn every consultation into a login prompt. Argue with this if you
think the isolation loss matters more, but know it was a decision, not an
oversight.

## What I do not want

- Style, naming, formatting, or comment-density opinions.
- "Consider adding type hints / logging / a docstring."
- Suggestions to add a dependency (web framework, `aiosqlite`, an HTTP client) —
  the dependency list is deliberately five packages.
- Restating what a function does.
- Findings you have not traced to a concrete failure.

## Output format

For each finding:

```
SEVERITY: critical | high | medium | low
FILE:LINE
CLAIM: one sentence — what is wrong
TRIGGER: the concrete input, state, or interleaving that causes it
CONSEQUENCE: what the user or the caller actually experiences
FIX: the smallest change that closes it
```

Order by severity. If an invariant above is violated, say which number.

End with two short lists: **what you verified and found sound**, and **what you
could not check without running the code**. If you found nothing critical, say
that plainly rather than promoting a medium finding to fill the slot.
