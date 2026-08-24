"""The one rule for reading a spend rollup against a ceiling.

Shared by the review and workflow layers because the awkward part is the same in
both: an agent on a free tier reports no price, so a total built from its turns is a
*floor*. Counting what is known and saying what could not be counted is the honest
handling; presenting the floor as a sum is not.

The refusal this produces is deliberately narrow. A request cannot be priced before
it is made, so what a ceiling buys is that the next request after it is *reached* is
refused -- not that spend never exceeds it. Reached, not exceeded: spending exactly
the ceiling refuses the next request rather than allowing one more.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .contract import Usage


@dataclass(frozen=True, slots=True)
class Spend:
    """One rollup, read two different ways.

    `usage` is for showing: its `cost_usd` is `None` unless every contributing turn
    was priced, because a floor displayed as a sum is a lie. `known_cost_usd` is for
    enforcing: the turns that *did* carry a price, which is money that was really
    spent whether or not the rest of the group could be priced.

    Kept as two fields rather than one, because reading the unknown as zero is how a
    single free-tier reviewer buys an unbounded workflow -- and rendering the floor
    as the total is the opposite mistake. Neither number can do both jobs.
    """

    usage: Usage
    known_cost_usd: float
    # Always countable, unlike money. An agent that reports no price still reports a
    # turn, which is what lets a turn ceiling bound work a dollar ceiling cannot see.
    # Required rather than defaulted: a rollup built without its count would read as
    # zero turns, and a ceiling that silently counts nothing is the bug this field
    # exists to fix. A caller that has to pass it cannot forget it.
    turns: int


# The suffix `tallied` writes, read back so it can be folded rather than repeated.
# Anchored at the end because that is the only place this function ever puts one.
_TALLY = re.compile(r" \(x(\d+)\)$")


def tallied(notes: Iterable[str]) -> list[str]:
    """Each distinct reason once, in first-appearance order, with how many said it.

    De-duplicated because five reviewers behind one CLI that stopped reporting its
    counts are one problem, and printing it five times buries whatever else went
    wrong. Counted because collapsing them to one line loses that it was all five,
    which is the difference between a fluke and an outage.

    Whole reasons, never fragments of one. That is what makes them worth carrying as a
    list this far: two reasons joined into a string cannot be told apart from one
    reason containing the separator, so a joined form de-duplicates against itself and
    emits the shared half twice.

    A count already on a reason is added into the new one rather than appended after
    it, because this runs at three levels over the same reasons -- once across the
    fields of a turn, once across the turns of a rollup, once across the agents of a
    review -- and each level is handed what the level below it wrote. Appending gives
    `(x2) (x2)`, which is four occurrences rendered as something that reads like a
    formatting bug and cannot be added up by anyone reading it. One number that means
    the whole depth is the only form worth carrying.
    """
    seen: Counter[str] = Counter()
    for note in notes:
        found = _TALLY.search(note)
        # A reason of this server's own wording never ends in the suffix pattern, and
        # a runtime value quoted inside one is quoted mid-sentence rather than last.
        seen[note[: found.start()] if found else note] += int(found[1]) if found else 1
    return [note if n == 1 else f"{note} (x{n})" for note, n in seen.items()]


def caveats(used: Iterable[Usage]) -> list[str]:
    """Every reason the parts of a rollup are not straight measurements.

    Carried up rather than recomputed, because the sum is exactly where it stops
    being visible: a reviewer whose counts were substituted contributes a number that
    looks like all the others, and the total is the figure anyone reads. The same
    argument as `cost_usd` one field over, with the difference that a token total is
    worth showing anyway -- so it is labelled where money is withheld.
    """
    return tallied(note for usage in used for note in usage.counts_incomplete)


def counted(spend: Mapping[str, Spend]) -> tuple[float, list[str]]:
    """`(what is known to have been spent, the keys whose price is incomplete)`."""
    total = sum(s.known_cost_usd for s in spend.values())
    return float(total), sorted(k for k, s in spend.items() if s.usage.cost_usd is None)


def refusal(
    spend: Mapping[str, Spend],
    ceiling: float | None,
    subject: str,
    max_turns: int | None = None,
) -> str | None:
    """Why this must not run, or `None` to go ahead.

    `subject` names what is being bounded ("review `abc`") and lands in the message,
    which is the only place the caller learns both numbers.

    The money ceiling is checked first because its message is the more useful one
    when there is a price to report. `max_turns` is the bound that still works when
    there is not: an agent on a flat-rate plan reports no per-turn price, so a
    dollar ceiling over one of those never reaches any number but zero, while the
    turns behind it are counted the same as anyone else's.
    """
    total, unpriced = counted(spend)
    if ceiling is not None and total >= ceiling:
        # The unpriced part is named rather than dropped: a caller who reads "$5.10
        # of $5.00" without it will read the total as complete, and it is a floor.
        # Those keys may also have contributed to the total already -- a group is
        # listed here when *any* of its turns went unpriced, not when all of them did.
        unknown = (
            f", and {len(unpriced)} more spent an unpriced amount beyond that "
            f"({', '.join(unpriced)})"
            if unpriced
            else ""
        )
        return (
            f"{subject} has spent ${total:.2f} of its ${ceiling:.2f} ceiling{unknown}; "
            "raise `consult.spend` or stop here"
        )
    if max_turns is not None:
        turns = sum(s.turns for s in spend.values())
        if turns >= max_turns:
            return (
                f"{subject} has used {turns} of its {max_turns} turn ceiling; "
                "raise `consult.spend` or stop here"
            )
    return None
