# Antigravity (`agy`) compatibility spike

Run 2026-08-06 against `agy` **1.1.10** on macOS 15 (darwin arm64), binary at
`~/.local/bin/agy`, authenticated via the OS keyring from a prior interactive login.

**Verdict: viable as experimental, via chunked prompt transport.** No production code
written yet. Details below; raw captures inline.

The verdict moved twice as probing went on, so the history matters:

1. Gate A (prompt transport) failed — no stdin, no `--prompt-file`, and Linux caps a
   single argv value at 128 KiB. Initially read as a hard block.
2. Gate C (isolation) looked like a second hard block, then probed directly and
   **downgraded to a documented risk**: `agy` does inherit user MCP servers, but
   headless mode denies tool permissions by default and the repo-read attempt was
   refused. The caveat is that the default lives in a user-owned settings file the
   adapter cannot verify or enforce.
3. Gate A was then **worked around**: splitting the prompt across turns on one
   `--conversation` id gave 6/6 verbatim canary recall at ~49 KB per fragment.
4. `--output-format stream-json` turned out to solve the schema, model-verification,
   and tool-visibility problems together — none of which `--output-format json` can do.

Gate C (isolation) was initially read as a second hard blocker, then probed directly
and **downgraded to a documented risk**: `agy` does inherit user MCP servers, but
headless mode denies tool permissions by default, and the repo-read attempt was
refused. The caveat is that this default lives in a user-owned settings file the
adapter cannot verify or enforce.

---

## Phase 0 — install and version

Installer (`https://antigravity.google/cli/install.sh`) verifies a SHA512 from a
per-platform manifest before writing `~/.local/bin/agy`, then runs `agy install` to
edit shell rc files. Binary was already present, so the script early-exited.

```
$ agy --version
1.1.10
```

Gate: **pass** (≥ 1.1.1, so [upstream issue 76](https://github.com/google-antigravity/antigravity-cli/issues/76)
— captured-pipe hangs with empty stdout — does not apply).

Authentication confirmed working under captured pipes: real prompts return
`"status":"SUCCESS"` with no browser interaction (see T3).

---

## Gate A — prompt transport: **FAIL**

### A2. No prompt-file flag

Full `agy --help` flag list, verbatim:

```
  --add-dir, --agent, -c, --continue, --conversation,
  --dangerously-skip-permissions, --disable-slash-commands, --effort,
  -i, --json-schema, --log-file, --mode, --model, --new-project,
  --output-format, -p, --print, --print-timeout, --project,
  --prompt, --prompt-interactive, --sandbox
```

There is no `--prompt-file` or any equivalent. `--json-schema` *does* accept a file
path ("Optional JSON schema string or path to a schema file"), but the prompt itself
has no file form.

### A1. Stdin is not read

**T1** — empty `-p` value, prompt piped on stdin:

```
$ echo "Reply with just the number: 2+2" | agy -p "" --output-format json
exit=1
{"conversation_id":"","status":"ERROR","response":"",
 "error":"Error: empty prompt. Usage: agy --print \"your prompt here\"",
 "duration_seconds":0.000006958,"num_turns":0,...}
```

**T2** — `-p -`, the usual stdin convention, prompt piped on stdin:

```
$ echo "Reply with just the number: 2+2" | agy -p - --output-format json --model gemini-3.6-flash-low
exit=0
{"conversation_id":"9d69a6ce-...","status":"SUCCESS",
 "response":"Hello! How can I help you today? ...","num_turns":1,
 "usage":{"input_tokens":18363,...}}
```

`-` was taken as a **literal prompt** — the model answered the dash, not the piped
text. Stdin is ignored in both forms. The prompt can only travel as an argv value.

### A3. Argv ceiling

macOS total is workable but the portable per-argument limit is not:

```
getconf ARG_MAX: 1048576
  100000 bytes: OK    400000 bytes: OK    1000000 bytes: OK
```

macOS enforces only a **total** argv+env budget of 1 MiB. Linux additionally enforces
`MAX_ARG_STRLEN` — a hard **128 KiB cap on any single argument**, independent of
`ARG_MAX` and not raisable. So a 1 MB prompt in one `-p` value cannot work on Linux at
all, and on macOS it would sit right at the total ceiling once the system contract,
the other flags, and `child_env()` are added.

Against this project's limits ([contract.py:39-41](../orchestrator_mcp/consult/contract.py:39)):

| Limit | Value | Via `-p` on Linux | Via `-p` on macOS |
|---|---|---|---|
| `MAX_PROMPT_CHARS` | 100 KB | fits, barely | fits |
| `MAX_CONTEXT_CHARS` | 1 MB | **impossible** | at/over the ceiling |

This is exactly the case the stdin invariant exists to prevent
([base.py:1-17](../orchestrator_mcp/consult/adapters/base.py:1)).

**Per the agreed decision, this blocks the integration.** The rejected alternatives
stay rejected: silently shrinking the context limit, and staging the prompt in the
sandbox for the model to read (that needs a file-read tool enabled, contradicting the
adapter's own rule against unexpected tool activity).

---

## Gate C — configuration isolation: **DOWNGRADED to documented risk** (see probe below)

There is no `--ignore-user-config`, no strict-config mode, and no `--strict-mcp-config`
equivalent in the flag list above. `agy plugin` exists as a subcommand, so user-level
plugins are a real, loadable surface with no documented way to suppress them for a
single run. The closest control is `--disable-slash-commands`, which only stops slash
and skill expansion in print mode.

Corroborating evidence that a run inherits substantial ambient context: a one-character
prompt billed **18,363 input tokens** (T2), and a short prompt billed 10,228 with 8,140
cache-read (T3). Something large is being injected before the prompt.

This matters because orchestrator-mcp is itself reachable over MCP: a consulted CLI that
can load user MCP config could consult back into the host. That risk is the stated
reason for `--strict-mcp-config` on the Claude adapter
([claude_cli.py:110](../orchestrator_mcp/consult/adapters/claude_cli.py:110)).

Also note `--sandbox` is documented as "terminal restrictions", not filesystem
isolation, and `--add-dir`/`--project`/`--new-project` imply a workspace model rather
than the empty-scratch-dir model the Codex adapter relies on.

### Follow-up probe — is the inheritance actually exploitable?

Run via `scratchpad/probe_isolation.py`, which mirrors the adapter transport exactly:
curated env, fresh empty `TemporaryDirectory` as cwd, `start_new_session=True`,
captured pipes, `--sandbox`, no `--dangerously-skip-permissions`.

**1. MCP inheritance is real — confirmed on disk, not just self-reported.**

Asked to enumerate its tools, `agy` answered:

```
ask_question, call_mcp_tool, codegraph_explore, define_subagent, generate_image,
grep_search, invoke_subagent, list_dir, list_resources, manage_subagents,
manage_task, multi_replace_file_content, read_resource, read_url_content,
replace_file_content, run_command, schedule, search_web, send_message,
view_file, write_to_file
```

`codegraph_explore` is not an Antigravity builtin — it is this user's own MCP server.
A model listing its own tools can confabulate, so it was verified on disk:

```
~/.gemini/antigravity-cli/mcp/codegraph/codegraph_explore.json
~/.gemini/antigravity-cli/mcp/codegraph/instructions.md
```

The CLI keeps a per-server cache of MCP tool schemas. So a consulted `agy` really does
inherit user MCP servers, and there is no per-run flag to suppress them.

**2. But tool calls are denied by default in headless mode.**

Both attempts to reach outside the scratch cwd were refused, on stderr:

```
jetski: no output produced — a tool required the "command" permission that headless
mode cannot prompt for, so it was auto-denied. Add an allow-rule under
permissions.allow in settings.json ...

jetski: no output produced — a tool required the "read_file" permission that headless
mode cannot prompt for, so it was auto-denied. ...
```

The `repo-read` check — asking it to read `pyproject.toml` from the user's repo by
absolute path — was denied. No CLI `settings.json` carrying a `permissions` key exists
on this machine, so the deny-by-default holds today.

**Why this is a documented risk and not a solved problem:** the denial is a *permission
default living in a user-owned file*, not an isolation guarantee the adapter can
assert. There is a flag to open the gate (`--dangerously-skip-permissions`) and no flag
to force it shut. Any operator who later adds `permissions.allow` entries — or an
upstream change to the default — silently grants tool execution to every consultation,
with no signal at the orchestrator boundary. The adapter would be depending on a
condition it cannot verify or enforce.

**3. Escaping cwd is attempted.** Every run emitted
`Shell cwd was reset to /Users/ayberkkarataban/orchestrator-mcp` on stderr, despite
being launched with an empty temp dir as cwd. The permission denial is what stopped it
from mattering here — the empty-scratch-dir trick alone does not contain this CLI.

**4. Protocol landmine — `SUCCESS` with an empty response.** Both denied runs returned
**exit 0** and `"status":"SUCCESS"` with `"response":""`. Terminal status is therefore
not trustworthy on its own: an adapter must reject an empty `response` regardless of
`status`, or a silently-denied run would surface as a successful consultation with no
content.

---

## Gate B — authentication probe: **no usable signal**

`agy` has **no** `login`, `logout`, `auth`, or `status` subcommand. Subcommands are:
`agent(s)`, `changelog`, `help`, `install`, `models`, `plugin(s)`, `update`. Logout is
an in-session slash command (`/logout`), not a CLI verb.

`agy models` exits **0** and prints a static-looking list:

```
gemini-3.6-flash-high     gemini-3.5-flash-high     gemini-3.1-pro-high
gemini-3.6-flash-medium   gemini-3.5-flash-medium   gemini-3.1-pro-low
gemini-3.6-flash-low      gemini-3.5-flash-low      claude-sonnet-4-6
claude-opus-4-6-thinking  gpt-oss-120b-medium
```

Whether this exits nonzero when logged out was **not** established — the machine was
already authenticated when the spike began, and logging out to find out would destroy a
credential this server is contractually forbidden to manage. So `preflight()` currently
has no verified way to distinguish "installed but logged out" from "ready", which is a
third, softer problem.

Worth noting for any future design: reasoning effort is **baked into the model slug**
(`-high` / `-medium` / `-low`), and `gemini-3.1-pro` offers only high and low. A separate
`--effort` flag therefore overlaps the model name, and the planned
`low|medium|high` config field would need to reconcile with that rather than sit
alongside it.

---

## Observed output contract

### `--output-format json` — unusable for this contract

```json
{"conversation_id":"a9533c3c-...","status":"SUCCESS","response":"OK\n",
 "duration_seconds":2.326966,"num_turns":1,
 "usage":{"input_tokens":10228,"output_tokens":4,"thinking_tokens":0,
          "cache_read_tokens":8140,"total_tokens":10232}}
```

`conversation_id` is present on turn one, and `usage` maps onto `Usage` (no cost
field). But with `--json-schema` the `response` came back as **prose followed by the
JSON object**:

```
The Fizzbin protocol requires **exactly three** handshake rounds and a **40ms** ...
{"answer":"The Fizzbin protocol requires exactly three handshake rounds...","assumptions":[],...}
```

The project's own `parse_content` rejects that outright:
`AdapterError: the agent did not return JSON`. This mode names no model either. **Do
not use it.**

### `--output-format stream-json` — the mode the adapter must use

JSONL. Three event kinds observed: `init`, `step_update`, `result`.

```json
{"event":"init","conversation_id":"6f0903c5-...",
 "init":{"model":"gemini-3.6-flash-low","cwd":"/private/tmp/.../scratchpad","tools":[...]}}
{"event":"step_update","step_update":{"step_index":2,"state":"DONE",
 "step_type":"agent_response","text_delta":"...","usage":{...}}}
{"event":"result","result":{"conversation_id":"...","status":"SUCCESS","response":"<prose + json>",
 "structured_output":{"answer":"...","assumptions":[],"follow_up_questions":[],
 "sources":[],"uncertainties":[]},"json_schema":{...},"usage":{...}}}
```

This resolves three problems at once:

1. **`result.structured_output`** carries clean, schema-conformant JSON, separate from
   the prose-contaminated `response`. The adapter must read `structured_output` and
   never `response`. Validated against the real `consultation_content_schema()` — it
   round-trips through `parse_content` cleanly.
2. **`init.model` names the model actually used** — so `check_model` works and
   `model_verified=True` is achievable, with no rollout-file fallback of the kind the
   Codex adapter needed. (An earlier reading of this spike said the model is never
   reported; that was true only of `--output-format json`.)
3. **Tool use is visible.** A denied `run_command` produced:

```json
{"event":"step_update","step_update":{"step_index":3,"state":"ACTIVE","step_type":"tool",
 "tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"echo hello"}}}}
{"event":"step_update","step_update":{"step_index":3,"state":"ERROR","step_type":"tool",
 "tool_name":"run_command","tool_info":{...,"error":{"type":"TOOL_ERROR",
 "message":"User denied permission to run command:\necho hello"}}}}
```

`step_type == "tool"` is a clean refusal hook — any such event can terminate the run,
Codex-style. Note the full builtin surface is always advertised in `init.tools`
(`run_command`, `browser_*`, `call_mcp_tool`, `invoke_subagent`, `write_to_file`, …),
so a static check on `init.tools` would reject every run; the check must be on actual
`tool` step events, not on availability.

**Landmine, reconfirmed here:** the denied-tool run ended `"status":"SUCCESS"` with
`"response":""` and `"structured_output":null`, exit 0. Terminal status is not
trustworthy — the adapter must reject a missing/empty `structured_output` regardless of
`status`.

---

## Chunked prompt transport — viable (probe `scratchpad/probe_chunking.py`)

Since the prompt can only travel in argv, the proposed workaround is to split it across
turns on one `--conversation` id and have the model reassemble it. Tested with
non-guessable canary tokens buried **between blocks of filler**, not stacked at the
head, so the classic lost-in-the-middle failure is actually exercised.

| Run | Fragments | Bytes per `-p` arg | Verbatim recall |
|---|---|---|---|
| small | 3 | 1,546 | **6/6** |
| realistic | 3 | 49,399 (~148 KB total) | **6/6** |

Turn-by-turn on the realistic run, showing history is re-sent but cached:

```
turn 1: input=24,658  cache_read=8,155
turn 2: input=33,102  cache_read=32,636
turn 3: input=40,437  cache_read=65,291
recall: input=43,971  cache_read=102,019   -> 6/6 exact
```

Findings:

- **`--conversation <id>` resume works through captured pipes** — previously unverified,
  now confirmed across four separate process invocations.
- Recall was exact at both scales, including canaries buried mid-fragment.
- Cost is real but bounded: ~142 K billed input tokens to move ~148 KB of payload,
  most of it cache-read. Each turn re-sends history.
- Every run carries a fixed ~8–18 K token ambient-context overhead before any payload.
- **Fencing is inconsistent** — the same prompt returned ` ```json `-fenced output on
  one run and bare JSON on another. `parse_content` already strips fences, which is
  what makes this survivable; `structured_output` sidesteps it entirely.

Chunk size must stay under Linux's `MAX_ARG_STRLEN` (128 KiB per single argument,
not raisable); ~100 KB is a safe ceiling.

**Caveat worth stating plainly:** recall was 6/6 on a `gemini-3.6-flash-low` run with
six canaries. That is evidence the mechanism works, not a guarantee that a 1 MB
consultation reassembles losslessly. Verbatim recall of short tokens is an easier task
than faithfully using a large chunked context, and a consultation that silently drops
part of its input is worse than one that refuses. Any rollout should keep a canary
check in the compiled prompt, or cap total chunked size well below 1 MB.

## Lifecycle (probe `scratchpad/probe_lifecycle.py`)

Run through the project's **own** `run_process` and `child_env`, so the code path under
test is the one that would ship.

### Unknown model slug — clean refusal

Exit **1**, and a `result` event with `status: "ERROR"` on stdout (stderr empty):

```
invalid model selection (--model "gemini-9.9-nonexistent" --effort ""): model
gemini-9.9-nonexistent is not recognized as a known model or custom model in settings
Available models: Gemini 3.6 Flash (High) ...
```

Errors arrive as a `result` event, not on stderr — the adapter's failure path must read
the event stream, not `stderr`.

### `--effort` conflicts with effort-bearing slugs — **exit 1**

```
invalid model selection (--model "gemini-3.6-flash-low" --effort "high"):
--model gemini-3.6-flash-low conflicts with --effort=high
```

`gemini-3.1-pro-low --effort medium` also exits 1. Every Gemini slug `agy models`
returns carries a baked-in `-high`/`-medium`/`-low`, as does `gpt-oss-120b-medium`, so
for practically every usable model the two settings are **mutually exclusive**, not
complementary.

This reverses the original plan's decision to allow `reasoning_effort` of
`low|medium|high` for antigravity. Passing both is a hard error, and the slug already
expresses the level. The simplest correct rule is to **reject `reasoning_effort` for
antigravity entirely** — keeping `_effort_is_codex_only` as-is, with only its error
message reworded to explain that antigravity encodes effort in the model name.

### Timeout and process-group kill — **clean**

A 6 s external timeout against a deliberately long generation:

```
agy processes before: 0
AdapterError('`~/.local/bin/agy` did not answer within 6s and was terminated')
agy processes after: 0   leaked pids: none
VERDICT: CLEAN
```

`run_process` needs no modification: `start_new_session=True` plus the `os.killpg`
escalation reaps `agy` and its children, and a long `--print-timeout` does not keep the
group alive. The external `asyncio.timeout` stays authoritative, as intended.

### Usage / subscription reporting — none

`usage` token counts appear per `step_update` and on the final `result`
(`input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens`,
`total_tokens`). There is **no cost field and no quota/subscription surface** — no
equivalent of `codex_rate_limit`. The dashboard's rate-limit line stays codex-only.

## Still not probed

Whether `agy models` exits nonzero when logged out — untestable without destroying the
user's credential, which this design is contractually forbidden to manage.

## What upstream would still improve

1. A `--prompt-file` flag, or stdin support — would retire the chunking machinery
   entirely and restore single-shot 1 MB prompts.
2. A strict-config / no-plugins / no-MCP flag, to make isolation provable rather than
   dependent on a permission default in a user-owned file.
3. A non-consuming auth probe with a meaningful exit code, so `preflight()` can tell
   "logged out" from "ready".

## Consequences for the adapter design

Superseding the original Phase 4 sketch:

- `--output-format stream-json`, parsed via `run_streaming`. Not `json`.
- Read `result.structured_output`; never `result.response`.
- Reject empty/missing `structured_output` even when `status == "SUCCESS"`.
- Verify the model from `init.model` → `model_verified=True` is reachable.
- Refuse on any `step_update.step_type == "tool"` event; do **not** gate on
  `init.tools`, which always advertises the full builtin surface.
- Prompt transport is multi-turn chunking at ≤100 KB per `-p` value, all turns on one
  `--conversation` id, with the schema applied only to the final turn.
- `preflight()` has no verified auth signal; `agy models` exits 0 regardless.
- **Never pass `--effort`.** It is a hard error alongside any effort-bearing slug, and
  every usable slug carries one. `reasoning_effort` stays rejected for antigravity.
- Failures arrive as a `result` event with `status: "ERROR"` and exit 1, with stderr
  empty — parse the stream for errors, do not read `stderr`.
- `run_process` needs no changes: timeout and process-group kill are already clean.
- No cost or quota data exists; `Usage.cost_usd` stays unset and the dashboard
  rate-limit line stays codex-only.
