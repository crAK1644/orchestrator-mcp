"""Ready-made personas: emphases that exist without any `consult.personas:` entry.

Each is `(one line for the tool description, the text that joins the system half)`.
They are code, so trusted, and are passed through `redact` at compile time like an
operator's own. A config entry of the same name replaces the wording.

`case-for` and `case-against` are what `orchestrator_consult_many(both_sides=True)`
hands its two agents. The wording asks for an honest case, not a performance: a side
forced to win an argument it believes lost will invent, so each is told to keep its
doubts in `uncertainties` and to name the point it finds hardest to answer.
"""

from __future__ import annotations

STYLES: dict[str, tuple[str, str]] = {
    "skeptic": (
        "doubt the premise first",
        "Doubt the premise before answering. Say what evidence would change your mind.",
    ),
    "security": (
        "read as an attacker would",
        "Read as an attacker would. Rank findings by what an attacker gains, and name "
        "the input that reaches each one. Say what you could not check.",
    ),
    "simplify": (
        "the smallest change that works",
        "Prefer the smallest change that works. Say what can be deleted, what already "
        "exists that does the job, and what you would leave alone. Name any abstraction "
        "with a single use.",
    ),
    "plain": (
        "plain words for a newcomer",
        "Answer in plain words for someone new to this. Define a term the first time "
        "you use it, and lead with what to do. Put the detail after.",
    ),
    "case-for": (
        "argue the strongest honest case for",
        "Argue the strongest honest case for the proposal in the task. Use only claims "
        "you believe are true, and put what you doubt in `uncertainties`. Do not soften "
        "the case to seem balanced: the case against is argued elsewhere. Finish by "
        "naming the one objection you find hardest to answer.",
    ),
    "case-against": (
        "argue the strongest honest case against",
        "Argue the strongest honest case against the proposal in the task. Use only "
        "claims you believe are true, and put what you doubt in `uncertainties`. Do not "
        "soften the case to seem balanced: the case for is argued elsewhere. Finish by "
        "naming the one point in its favour you find hardest to answer.",
    ),
}
