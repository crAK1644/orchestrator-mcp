# Security policy

## Reporting a vulnerability

Report it privately at https://github.com/crAK1644/orchestrator-mcp/security/advisories/new. Please don't open a public issue for it.

Include:
- the version, from `orchestrator-mcp-server --version`;
- the host you run it under;
- the smallest config and steps that show the problem.

## Supported versions

Only the latest release gets fixes. The Claude Code plugin pins that release, so updating the plugin picks up the fix.

## Scope

The README's [Security model](README.md#security-model) lists what Orchestrator guarantees, and the limits of each guarantee. A way around any of them is in scope. For example:
- a consulted agent writing to your working tree;
- a tool call overriding the host runtime;
- a credential reaching the database unmasked.

What a vendor's CLI or service does with material sent to it is out of scope.
