# Adversarial review request — orchestrator-mcp-server, whole repository, pre-0.2.0

You are reviewing an entire small Python project immediately before a release to
PyPI. Find real defects. Do not summarize the code back to me, do not praise it,
do not propose stylistic rewrites. I want the bugs, and I want them traced to a
concrete failure.

Assume competence. Everything obvious has been looked at twice. The findings I
care about are the ones that need two facts held at once — an interleaving, a
lifecycle, a parse that succeeds on the wrong input, a document that promises
something the code does not do.

## What ships

`orchestrator-mcp-server` is an MCP server (stdio) with **two independent paths**.

**Path 1 — `ask`** (unchanged since 0.1.2, ~1,300 lines). A *capability* is a
LiteLLM `model_name` alias group; `litellm.Router` owns load balancing, retries,
cooldowns, and cross-capability fallback. The server adds a bounded envelope,
local JSON Schema validation with bounded repair turns, and a set of refusals: a
truncated or filtered completion is a failure, not a short answer.

**Path 2 — `consult`** (new in 0.2.0, ~2,500 lines). Routes a question to an
**agent runtime** — the Codex CLI or the Claude Code CLI installed on the same
machine, driven as a subprocess, over text only. A CLI runtime brings its own
logged-in account, its own web search, and its own conversation state, so Claude
Code can consult GPT and Codex can consult Claude with no second API key held by
anyone.

One consultation:

1. The host agent calls `consult` with a capability, a prompt, optional context,
   an optional source mode, and — on later turns — a `consultation_id`.
2. The service resolves the source mode, routes to an agent, creates or loads a
   consultation row, takes a cross-process lease, preflights the CLI, compiles a
   prompt, spawns the CLI, parses its output, validates it against a generated
   JSON schema, records the turn, releases the lease, and returns an envelope.
3. The envelope is `ok: true` with content, or `ok: false` with one of a closed
   set of error codes. Never an exception across the MCP boundary.

A third entry point, `orchestrator-mcp-dashboard`, serves a read-only loopback
page over the stored consultations.

## Invariants — violating any of these is a top-severity finding

1. **No self-delegation.** The host runtime comes from `ORCHESTRATOR_HOST_RUNTIME`,
   never from a tool argument, and every agent whose runtime equals it is excluded
   — at routing *and* on resume. An agent that could name the host runtime could
   hand the work straight back to itself.
2. **No silent substitution of the agent.** An uninstalled, logged-out, or failing
   agent stops the consultation. There is no second candidate. A consultation is
   pinned to its agent, runtime, and model for life; continuing it against a
   different one is `session_target_mismatch`, never a new conversation wearing
   the old id.
3. **No silent substitution of the model.** Both CLIs fall back internally. If the
   model actually used does not match the configured one, that is
   `configured_model_unavailable`, not an answer.
4. **No credentials, ever.** Never read, copy, return, log, or store OAuth
   credentials, tokens, or the body of an auth command's output. Preflight may
   learn *whether* an agent is logged in and nothing more. Environment variables
   are never persisted.
5. **No shell.** Every subprocess is `asyncio.create_subprocess_exec` with an
   argument list. The prompt goes in over stdin, never in argv.
6. **The consulted agent may not act.** It answers text. Any command execution,
   file change, MCP call, or subagent event in its stream is
   `protocol_validation_failed` — the answer is discarded, not sanitized.
7. **The server never ghostwrites.** On both paths, a failure carries no answer
   text: `ask` returns `content: null` and `data: null`, `consult` returns
   `content: null`. A caller cannot distinguish an apology string from an answer.
8. **Failures are envelopes.** A caller gets a code from a closed set. The only
   exception that may cross the MCP boundary is a schema violation the MCP layer
   itself raises before the handler runs.
9. **Backward compatibility is absolute.** A config with no `consult:` block must
   start a server advertising exactly `ask` and `list_capabilities`, byte for byte
   identical to 0.1.2. A snapshot test asserts this; tell me if anything can make
   it lie.
10. **The dashboard is read-only and loopback-only.** No writes, no config
    editing, no subprocess spawned by a GET, nothing bound off-loopback.

## Layout

```
orchestrator_mcp/server.py                    both tool sets; build_server()
orchestrator_mcp/contract.py                  ask/list_capabilities models, ErrorCode, Usage
orchestrator_mcp/consult/errors.py            closed set of consult error codes
orchestrator_mcp/consult/contract.py          consult models, JSON schema shaping
orchestrator_mcp/consult/config.py            the `consult:` block, host_runtime()
orchestrator_mcp/consult/routing.py           deterministic agent selection
orchestrator_mcp/consult/prompts.py           versioned prompt compiler
orchestrator_mcp/consult/store.py             SQLite: consultations, turns, routing, leases
orchestrator_mcp/consult/adapters/base.py     subprocess transport, model check, content parse
orchestrator_mcp/consult/adapters/codex_cli.py
orchestrator_mcp/consult/adapters/claude_cli.py
orchestrator_mcp/consult/dashboard.py         read-only loopback HTTP view
config.example.yaml                           annotated; the consult block ships commented out
README.md                                     the only documentation
pyproject.toml                                version, deps, two console scripts
.github/workflows/{test,release}.yml          CI, and PyPI via Trusted Publisher
```

Tests: `test_orchestrator.py` (the 0.1.2 suite, assertions untouched) plus
`tests/`. 297 pass and 1 skips with no network, no API key, and neither CLI
installed. `smoke_live.py` and `smoke_consult_live.py` spend real money and never
run in CI.

Dependencies are deliberately five: `mcp`, `litellm`, `pydantic`, `pyyaml`,
`jsonschema`. Everything else is stdlib, including the SQLite layer and the
dashboard's HTTP server.

## Attack these first

**Subprocess lifecycle** — `adapters/base.py`. `run_process` and `run_streaming`
both cap total output and kill by process group (`start_new_session=True` +
`os.killpg`). Can a child survive a timeout, a cancellation, a callback that
raises, or an exception in a reader task? Can the parent deadlock on a full pipe?
Is the per-line limit large enough for a real JSONL event, and is exceeding it
handled the same way in both functions?

**Concurrency** — `store.py`. One `sqlite3` connection, `check_same_thread=False`,
`isolation_level=None`, shared across `asyncio.to_thread` workers and serialized
by an `RLock`. Leases are `BEGIN IMMEDIATE` + `INSERT OR IGNORE` with a TTL and a
per-acquisition token. Can two processes advance the same native CLI session? Can
a lease leak past its TTL and wedge a consultation? Are there interleavings where
two `to_thread` calls corrupt each other's transaction, or where the `RLock` is
held across an `await`?

**Output parsing** — both adapters. What malformed, truncated, adversarial, or
merely unexpected CLI output yields a wrong answer rather than
`protocol_validation_failed`? Treat the consulted agent as hostile: it is another
vendor's model reading a prompt that a third party may have influenced.

**The model check** — `check_model()` in `base.py`. Case-insensitive containment
in both directions, plus a variant-token set (`mini`, `haiku`, `opus`, `thinking`,
…) that must match exactly on both sides. Missing metadata is treated as "not
evidence of substitution" and passes. Where does a real substitution slip through?

**Prompt injection** — `prompts.py`. Caller-supplied `context` and the consulted
agent's reply are both untrusted. Can either redirect the protocol, disable the
contract that the compiled prompt puts first, or poison a later turn of the same
session? The same question applies to `ask`: `system` is caller-supplied and the
server's grounding directive is appended after it.

**Session binding** — MCP hands this server no reliable host conversation id, so
the returned `consultation_id` is the only binding there is. What happens when a
caller loses it, replays an old one, guesses one, or sends one belonging to a
different database or a different agent?

**The dashboard** — `dashboard.py`. `mode=ro` URI connection, `Host` header
allowlist, `html.escape(..., quote=True)` on every value, `do_GET` only, a CSP of
`default-src 'none'; style-src 'unsafe-inline'`. Is there a path that writes, a
value that reaches the page unescaped, a DNS-rebinding or SSRF angle, a way to
make a GET spawn a process, or a way to bind off-loopback?

**Path 1 regressions** — the 0.1.2 code is not exempt. The timeout budget is
shared across retries, fallback, and repair turns; `finish_reason` is read from
the native reason LiteLLM keeps alongside its normalized one; schema repair feeds
the model's rejected output back to it. Anything there that a reviewer with fresh
eyes would catch is in scope.

**Resource exhaustion** — free-text caps (100k prompt / 1M context / 200 label),
unbounded stored rows, a web-mode consultation spending money until the turn
limit, a `response_schema` with a pathological `pattern` burning CPU on the event
loop.

**The documentation** — `README.md` and `config.example.yaml` are in scope as
code. Any claim either one makes that the implementation does not honour is a
finding, and I want it at the severity of the gap it hides, not "docs". The
commented `consult:` block in the example config is asserted loadable by a test;
the README's guarantee list is not asserted by anything.

**Release mechanics** — `pyproject.toml` and `.github/workflows/release.yml`.
Version is 0.2.0. Both console scripts must exist in the wheel, the packaged
version must match the tag, and nothing secret may reach PyPI or the sdist.

## Ground truth — do not "correct" these

Verified against the installed Claude Code 2.1.220 on the development machine:

- `--json-schema` takes **inline JSON**, not a file path.
- `--output-format stream-json` **requires** `--verbose`.
- There is **no `--max-turns`**. The web-mode turn budget is this code counting
  assistant events and killing the child.
- `claude auth status --json` exits 0 whether logged in or out, so preflight reads
  the `loggedIn` boolean from the body, not the exit code. Subcommands are
  `login`, `logout`, `status`.

Codex is **not installed** on the development machine, so its flags and JSONL
event names come from documentation and are pinned only by fixtures. Treat the
Codex adapter as the less-verified one and say so if you spot a likely mismatch.

Deliberate decisions, not oversights — argue with them only if you can name a
concrete failure they cause:

- `CODEX_HOME` is **not** relocated to a scratch directory. Isolating it would put
  the user's saved credentials out of scope and turn every consultation into a
  login prompt.
- SQLite is stdlib `sqlite3` behind `asyncio.to_thread`, not `aiosqlite`.
- The dashboard is stdlib `http.server`, not a web framework.
- Consult capabilities are a fixed vocabulary (`coding`, `research`, `writing`,
  `reasoning`, `review`), not the operator's `capabilities:` block.

## Already found and fixed — do not re-report these

Two previous review passes produced twenty-two findings; twenty-one were confirmed
and fixed, one was rejected. Report them again only if you can show the fix is
incomplete or introduced a new defect.

- Resume path could bind a consultation to the host's own runtime, or to a
  different model than it started on. Both now refused in `service._bind`.
- Unbounded `run_process` output; a child could flood memory. Now capped at 32 MB
  with the readers cancelled and the group killed.
- An oversized single line in `run_streaming` raised `ValueError` instead of an
  envelope.
- `codex login status` output was captured; now `capture=False`.
- A codex `turn.failed` event or a nonzero exit could still return an answer
  parsed from earlier in the stream. Both now `agent_unavailable`.
- Claude web mode checked only the init event's tool list, so a `tool_use` block
  mid-stream passed. Now every event is walked.
- `check_model` accepted `gpt-5` vs `gpt-5-mini` as a match. Variant tokens now
  have to agree.
- Two `store.open()` calls could race; the shared connection had no lock; the
  lease token was reused across acquisitions.
- Free-text fields were uncapped.
- `_migrate()` read the applied-versions set before `BEGIN IMMEDIATE` and never
  rechecked it, so two processes opening one new database raced into
  `table profiles already exists`. Rechecked inside the transaction now.
- `PRAGMA journal_mode=WAL` got an immediate `SQLITE_BUSY` under that same race:
  SQLite does not run the busy handler for a journal-mode conversion. Retried by
  hand for 5s now.

Second pass:

- Provider exception text reached `error.message` verbatim. `contract.redact()`
  now scrubs credential shapes on both paths' failure envelopes.
- `_signal_group` looked the group up with `os.getpgid(process.pid)`, which raises
  once the leader is reaped — exactly the case where a forked worker survives.
  `start_new_session=True` makes `process.pid` the group id, so it is used directly.
- codex 0.146 rejects `--ask-for-approval` on `exec`; sent as
  `-c approval_policy="never"` now.
- The Claude adapter parsed a stdout envelope without checking the exit code, so an
  abandoned answer could be returned as a delivered one.
- `check_model` matched raw substrings, so `gpt-5.1` matched `gpt-5.10`. Matching is
  on token runs now, with snapshot dates the only allowed suffix on a pinned version.
- An exception anywhere in `ConsultService.consult` — including `store.open()` on an
  unusable `database_path` — crossed the MCP boundary bare. There is an outer
  boundary now that returns a `transport_error` envelope carrying only the type name.
- `run_streaming` drained stderr with an uncapped `read()`, letting a child spend the
  output budget twice. Both streams share one `_Budget` now.
- The web turn limit was off by one: the turn that spent the last of the budget was
  killed before it could emit its result, making `web_turn_limit: 1` unusable.

Two more were documentation, not code: the README implied the advertised `ask`
schema was byte-identical to 0.1.2 (it is not — `response_schema` gained an
advertised `maxLength` and a pre-parse size check), and implied
`request_timeout_s` bounds everything (it cannot bound `jsonschema` backtracking on
a caller-supplied schema; `re` holds the GIL, so neither a timeout nor a thread
helps). Both corrected in the README.

Rejected, with reasoning — take it up again only with a new argument: the
dashboard renders each turn's raw CLI output, which was called an information
leak. Anyone who can GET the loopback page can read the SQLite file holding the
same bytes, and the MCP-facing surface (`get_consultation`) does not expose it.

## What I do not want

- Style, naming, formatting, or comment-density opinions.
- "Consider adding type hints / logging / a docstring."
- Suggestions to add a dependency.
- Restating what a function does.
- Findings you have not traced to a concrete failure.
- A medium promoted to critical to fill a slot.

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

End with three short lists: **what you verified and found sound**, **what you
could not check without running the code or the real CLIs**, and **what you did
not read**. If you found nothing critical, say so plainly.
