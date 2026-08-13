"""The slash commands, served over the same stdio connection as the tools.

An MCP prompt is text, not an action. Nothing here consults, reviews or starts a
workflow: each one expands into an instruction that reaches the host's conversation
as if the user had typed it, and the host then calls the tools. That is the whole
mechanism, and it is why this file installs nothing -- no command directory, no
generated markdown, nothing written to the machine at all. A client that speaks
`prompts/list` gets them; one that does not is unaffected.

They exist because the valuable flows here are handshakes. `orchestrator_review`
plans and sends nothing; a second call with a one-time token sends. A workflow is
plan-step, run-step, repeat. A host driving those from the tool descriptions alone
tends to skip the checkpoint that makes them worth having, and the user is the one
who pays for that. Writing the sequence down once, here, is cheaper than hoping.

Named for what a person types, not for the tool underneath: clients render these as
`/mcp__orchestrator__consult` and similar, and a name that already says
`orchestrator` reads twice.
"""

from __future__ import annotations

from mcp.server import MCPServer

# Repeated in every command that ends in a paid call. The checkpoint is advisory --
# this server has no channel of its own to a human, as the review tools' docstrings
# already say -- so the one thing that makes it real is the host actually stopping.
_CHECKPOINT = (
    "Show the user the plan and wait for their answer before spending the token. "
    "This server cannot tell an approval from a host that did not ask."
)


def add_commands(server: MCPServer, *, reviews: bool, workflow: bool) -> None:
    """Advertise the commands whose tools this server actually has.

    Gated exactly like the tools are: a `/review` that can only reply "no reviewers
    are configured" is worse than no command, because it costs a round trip and reads
    like a bug rather than a configuration choice.
    """

    @server.prompt(name="consult", description="Ask another vendor's coding agent a question.")
    async def consult(question: str = "", agent: str = "") -> str:
        target = (
            f"Route it to the agent named `{agent}`."
            if agent
            else "Let the server route it: pass a `capability` rather than an agent name, and "
            "call `orchestrator_list_consult_agents` first only if the user asked who is available."
        )
        asked = question or "<the user has not said yet -- ask them what to consult about>"
        return (
            f"Consult another agent about: {asked}\n\n"
            f"{target}\n\n"
            "Call `orchestrator_consult`. Send the context the other agent needs in the "
            "question itself -- it runs with no filesystem and cannot see this repository, "
            "so a path or a symbol name it cannot open is worth nothing to it.\n\n"
            "Keep the `consultation_id` from the response. Every later call about this same "
            "topic must carry it, or the other side starts a fresh conversation that "
            "remembers none of this one.\n\n"
            "Report the answer with its disagreements intact. A second opinion that has been "
            "smoothed into agreement with your own was not worth the call."
        )

    if reviews:

        @server.prompt(name="review", description="Have external agents review the current work.")
        async def review(goal: str = "", deep: str = "") -> str:
            mode = "deep" if deep.strip().lower() in {"1", "true", "yes", "deep"} else "standard"
            extra = (
                "\n\n`mode=\"deep\"` was asked for: it needs your own findings first, passed to "
                "`orchestrator_review_run` as `host_findings`. Do that review yourself before "
                "planning this one."
                if mode == "deep"
                else ""
            )
            return (
                "Run an external review of the current work"
                + (f", against this goal: {goal}" if goal else "")
                + ".\n\n"
                "If the user did not say what to review, default to the working branch's diff "
                "against its merge base and tell them that is what you chose.\n\n"
                "1. Write the material to a file and pass `context_paths`, rather than pasting a "
                "diff into `context`. A branch diff runs to hundreds of kilobytes; the reviewer "
                "sees the bytes either way, and this is the form that fits in one call.\n"
                f"2. Call `orchestrator_review` with `mode=\"{mode}\"`. It sends nothing.{extra}\n"
                "3. Show the returned plan: which reviewers, how much material, the request count, "
                "and `secret_hits` -- lines where something credential-shaped was found. Detection "
                "is pattern matching and a credential with no recognizable shape survives it, so "
                "read the flagged lines rather than trusting the count.\n"
                f"4. {_CHECKPOINT} Then `orchestrator_review_run` with the `confirm_token`.\n"
                "5. `orchestrator_finalize_review` to combine the findings.\n\n"
                "Do not drop a finding because you disagree with it. Argue with it in your summary "
                "and leave it on the record -- the combined findings are what the user reads."
            )

    if workflow:

        @server.prompt(
            name="workflow", description="Run a goal through research, implementation and review."
        )
        async def workflow_command(goal: str = "", workdir: str = "") -> str:
            where = (
                f"Use `{workdir}` as the workdir."
                if workdir
                else "Use the repository you are working in as the workdir, and say which path you "
                "chose. It must resolve beneath a configured `workflow.roots:` entry, and the "
                "server refuses a dirty tree unless you pass `allow_dirty=True` -- ask before "
                "you do, because it is what makes 'what changed' answerable afterwards."
            )
            return (
                "Run this goal through the three-phase workflow"
                + (f": {goal}" if goal else " -- ask the user what the goal is first")
                + ".\n\n"
                f"{where}\n\n"
                "1. `orchestrator_workflow_start`. It resolves and freezes every step's routing "
                "now, so a step nobody can staff fails here rather than after three paid steps. "
                "It sends nothing and returns no token.\n"
                "2. `orchestrator_workflow_plan_step` for the step the status names as next. It "
                "returns that step's preview and a one-time token, and sends nothing.\n"
                f"3. {_CHECKPOINT} A step's token approves that step and no other.\n"
                "4. `orchestrator_workflow_run_step` if an agent runs the step, or "
                "`orchestrator_workflow_record_host_step` if you do.\n"
                "5. `orchestrator_workflow_status` between steps. It reports the state, the "
                "artifacts so far and which actions are allowed next -- do not guess the order "
                "from this message.\n\n"
                "The steps bound to you are yours to actually do. When you record a test run, "
                "record the exit code you observed; a coding agent's claim that the tests passed "
                "is stored as a claim and is never the result."
            )

    @server.prompt(name="status", description="Show what orchestrator work is in flight.")
    async def status(workflow_id: str = "") -> str:
        lines = ["Report what orchestrator work is outstanding.\n"]
        if reviews:
            lines.append(
                "- `orchestrator_list_reviews` for reviews, and `orchestrator_get_review` for any "
                "that is unfinished."
            )
        if workflow:
            lines.append(
                f"- `orchestrator_workflow_status` for workflow `{workflow_id}`."
                if workflow_id
                else "- `orchestrator_workflow_status` takes a workflow id and there is no tool "
                "that lists them, so ask the user for the id if they want one and did not say "
                "which."
            )
        lines.append(
            "\nSummarize as state and what it is waiting on. An unfinished review or workflow is "
            "usually waiting on a decision from the user, not on a process."
        )
        return "\n".join(lines)
