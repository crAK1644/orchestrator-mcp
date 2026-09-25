# Orchestrator for Claude Code

Ask other coding agents for help without leaving Claude Code. The agents are Codex, Antigravity and OpenCode, and there are three ways to use them:

- a second opinion from one of them;
- a code review from several at once;
- a workflow that runs in phases.

Each agent runs through its own CLI, signed in under your account. Orchestrator holds no API key and runs no service of its own.

## Before you install

- [`uv`](https://docs.astral.sh/uv/) must be on your `PATH`. The plugin starts its server with `uvx`.
- At least one of these must be installed and logged in:
  - [Codex CLI](https://github.com/openai/codex)
  - [OpenCode](https://opencode.ai)
  - [Antigravity](https://antigravity.google/), which is experimental

What you send an agent goes to that agent's provider under your login, and the usage is billed to your account there. Before you install, read:

- the [privacy policy](https://github.com/crAK1644/orchestrator-mcp/blob/main/PRIVACY.md);
- the [Security model](https://github.com/crAK1644/orchestrator-mcp#security-model).

## Install

```
/plugin marketplace add crAK1644/orchestrator-mcp
/plugin install orchestrator-mcp@orchestrator-mcp
/orchestrator-mcp:setup
```

`/orchestrator-mcp:setup` writes a starter config at `~/.orchestrator-mcp/config.yaml` from the Codex and Antigravity CLIs it finds, then checks each one's login. OpenCode goes in by hand: copy its agent from [`config.example.yaml`](https://github.com/crAK1644/orchestrator-mcp/blob/main/config.example.yaml).

When it finishes, reconnect `plugin:orchestrator-mcp:orchestrator` in `/mcp`, or restart Claude Code. Until you do, the server offers one tool, `orchestrator_setup`, which tells you the same thing.

## Examples

- **Second opinion.** "Ask Codex whether the retry loop in `billing/client.py` can charge a card twice."
  - Claude calls `orchestrator_consult` and shows Codex's answer, with its assumptions and open questions.
  - A follow-up continues the same conversation.
- **Code review.** "Review the changes on this branch with the configured reviewers."
  - `orchestrator_review` plans the review and sends nothing. The plan says who reviews, how much material goes, and which lines look like secrets.
  - Claude shows you that plan before running the review.
  - Claude then writes one synthesis from every reviewer's findings.
- **Phased workflow.** "Start a workflow to add rate limiting to the upload endpoint: research and plan with Codex, then implement and review."
  - Each step is previewed before it runs.
  - An agent returns a patch rather than editing your files. Claude applies the patch and runs the tests.
  - Workflows need a `workflow:` block that names the directories they may work in. See [The three-phase workflow](https://github.com/crAK1644/orchestrator-mcp#the-three-phase-workflow).
- **Housekeeping.** "Which agents can I consult, and are they logged in?" Or: "Delete my consultation history."
  - Deleting everything shows you a count before anything is deleted.

The server also adds prompts:

- always: `/mcp__plugin_orchestrator-mcp_orchestrator__consult` and `/mcp__plugin_orchestrator-mcp_orchestrator__status`;
- once configured: `/mcp__plugin_orchestrator-mcp_orchestrator__review` and `/mcp__plugin_orchestrator-mcp_orchestrator__workflow`.

## Support

- Documentation: the [README](https://github.com/crAK1644/orchestrator-mcp#readme).
- Questions and bugs: [GitHub issues](https://github.com/crAK1644/orchestrator-mcp/issues).
- Vulnerabilities: see [SECURITY.md](https://github.com/crAK1644/orchestrator-mcp/blob/main/SECURITY.md).

MIT licensed.
