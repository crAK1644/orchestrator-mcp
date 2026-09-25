---
description: Write a starter orchestrator config and check every agent login
---

Set up the MCP server this plugin registers. Run each command below with Bash and show the user its output.

1. Write a starter config at `~/.orchestrator-mcp/config.yaml` from the agent CLIs installed here:

   ```
   uvx orchestrator-mcp-server@0.7.2 init --host claude
   ```

   On success it ends by printing a `claude mcp add orchestrator ...` command. Do not run it: this plugin already registers the server, and adding it again would run two. A `review: left out` line needs no action: `~/.orchestrator-mcp/agents.yaml` already names the reviewers.

   If it says the file already exists, that is fine; go on to step 2. If it refuses for any other reason -- no reviewer CLI found, or a reviewer that `~/.orchestrator-mcp/agents.yaml` names and the starter config does not define -- quote the refusal and say what fixes it, then go on to step 2 anyway. `init` has no OpenCode template: if OpenCode is the only other agent here, point the user at the `opencode` agent in https://github.com/crAK1644/orchestrator-mcp/blob/main/config.example.yaml to copy into the config by hand.

2. Check it with the same environment the plugin gives the server:

   ```
   ORCHESTRATOR_CONFIG="$HOME/.orchestrator-mcp/config.yaml" ORCHESTRATOR_HOST_RUNTIME=claude uvx orchestrator-mcp-server@0.7.2 doctor
   ```

3. Quote every `FAIL` line with what fixes it: install the CLI it names, log in to it, or edit the config key it names. Do not edit the config yourself unless the user asks.

4. Finish by telling the user to reconnect `plugin:orchestrator:orchestrator` in `/mcp`, or to restart Claude Code. `/reload-plugins` is not enough: it keeps the server that started before the config existed.
