"""What each delegated step asks for, and how the reply comes back as an artifact.

Nothing here writes contract text. The consult path's `SYSTEM_CONTRACT` is still the
first thing every delegated step sends -- it already forbids editing files, running
commands and spawning agents, which is exactly right for a step whose output is a
document or a diff -- and what this module produces goes in the `task` and `context`
fields of the JSON payload underneath it. A step whose worker genuinely edits
(`isolated_write`) needs a different contract, and it will get one in its own
package rather than by loosening this one.

The reply is validated, not trusted: every step names the model its answer has to
parse into, and a step that cannot produce one fails rather than storing prose under
an artifact's name.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from ..review.contract import ReviewSummary
from .contract import (
    ExecutionBrief,
    ImplementationPlan,
    ResearchBrief,
    Step,
)

# What a delegated step's answer must parse into. `implement` and `fix` are absent
# on purpose: their answer is a unified diff, which is text and is validated by the
# host that applies it. `review` is absent because a workflow review goes through
# `ReviewService`, not through here.
REPLY_MODELS: dict[Step, type[BaseModel]] = {
    "research": ResearchBrief,
    "plan": ImplementationPlan,
    "author_execution_prompt": ExecutionBrief,
    "synthesize": ReviewSummary,
}

INSTRUCTIONS: dict[Step, str] = {
    "research": (
        "Research what this goal requires. Report what you found, and what you could "
        "not establish -- an open question is a result, and a confident guess in its "
        "place is the failure this step exists to avoid."
    ),
    "plan": (
        "Write an implementation plan for this goal. Name the files to change and "
        "the intent of each change, the order to make them in, how the result gets "
        "validated, what could go wrong, and what would make it done. Plan only; "
        "write no code."
    ),
    "author_execution_prompt": (
        "Turn this plan into a brief for whoever implements it: the objective, the "
        "constraints they must hold to, the steps in order, and the conditions that "
        "make it finished. Write the brief, not the code."
    ),
    "implement": (
        "Implement this change and reply with a single unified diff and nothing "
        "else. You cannot see the repository -- work only from the material below, "
        "and put anything you had to assume about code you were not shown in "
        "`assumptions`. Paths in the diff are relative to the repository root. If "
        "the material is not enough to write a correct patch, say so in `answer` "
        "rather than guessing at a file you have not seen."
    ),
    "fix": (
        "Fix the findings below and reply with a single unified diff and nothing "
        "else. Same rules as the implementation step: you cannot see the repository, "
        "paths are relative to the repository root, and a finding you cannot address "
        "from the material shown belongs in `uncertainties` rather than in a guessed "
        "patch."
    ),
    # Only reachable under `isolated_write`, because a test result is an exit code
    # somebody watched and nothing else. There is no consultation form of this step:
    # an agent with no filesystem telling you the tests passed is a sentence, not a
    # result.
    "test": (
        "Run the project's tests in the worktree you were given and report exactly "
        "what happened: the command, the working directory, the exit code, and the "
        "tail of the output. Do not summarise a run you did not perform."
    ),
    "synthesize": (
        "Combine the reviews below into one conclusion. Every Critical and Important "
        "finding any reviewer raised must appear in `combined_findings` with the "
        "reviewer it came from in `source_finding_ids` -- dropping one is the single "
        "thing this step must never do. Mark a finding `rejected` or `accepted_risk` "
        "only with a `disposition_reason` saying why."
    ),
}


def step_prompt(
    step: Step, goal: str, inputs: dict[str, Any], material: str = ""
) -> tuple[str, str | None]:
    """The `task` and `context` for one delegated step.

    The inputs go in `context` rather than in the task text so they land in the
    payload as data, on the far side of the contract, the way a consultation's
    document does. `material` is whatever the host chose to show this step -- the
    source of the files being changed, most of the time -- and lands in the same
    payload under its own key: a step that cannot see the repository can only work
    from what it was handed, and before this there was no way to hand it anything.
    """
    parts = [INSTRUCTIONS[step], f"Goal:\n{goal.strip()}"]
    model = REPLY_MODELS.get(step)
    if model is not None:
        parts.append(
            "Return a JSON object matching this schema, as the last thing in your "
            "answer:\n" + json.dumps(model.model_json_schema(), sort_keys=True)
        )
    payload = dict(inputs)
    if material:
        payload["host_material"] = material
    context = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        if payload
        else None
    )
    return "\n\n".join(parts), context


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_reply(step: Step, answer: str) -> BaseModel:
    """The artifact this step's answer carries, or a `ValueError` naming what failed.

    Later blocks first: a model that quotes the required shape early and answers at
    the end would otherwise have its example accepted as the answer. Same reason and
    same ordering as the review parser, which learned it the expensive way.
    """
    model = REPLY_MODELS[step]
    blocks = [match.group(1) for match in _FENCE.finditer(answer)]
    start, depth = answer.find("{"), 0
    for index in range(start, len(answer)) if start != -1 else ():
        if answer[index] == "{":
            depth += 1
        elif answer[index] == "}":
            depth -= 1
            if depth == 0:
                blocks.append(answer[start : index + 1])
                break
    for chunk in reversed(blocks):
        try:
            return model.model_validate_json(chunk)
        except (ValidationError, ValueError):
            continue
    raise ValueError(
        f"the `{step}` step's answer carried no `{model.__name__}` this server could "
        "read; the step is recorded as failed rather than stored as an artifact"
    )
