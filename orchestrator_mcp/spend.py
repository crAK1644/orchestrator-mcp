"""The one rule for reading a spend rollup against a ceiling.

Shared by the review and workflow layers because the awkward part is the same in
both: an agent on a free tier reports no price, so a total built from its turns is a
*floor*. Counting what is known and saying what could not be counted is the honest
handling; presenting the floor as a sum is not.

The refusal this produces is deliberately narrow. A request cannot be priced before
it is made, so what a ceiling buys is that the next request after it is crossed is
refused -- not that spend never exceeds it.
"""

from __future__ import annotations

from collections.abc import Mapping
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


def counted(spend: Mapping[str, Spend]) -> tuple[float, list[str]]:
    """`(what is known to have been spent, the keys whose price is incomplete)`."""
    total = sum(s.known_cost_usd for s in spend.values())
    return float(total), sorted(k for k, s in spend.items() if s.usage.cost_usd is None)


def refusal(spend: Mapping[str, Spend], ceiling: float | None, subject: str) -> str | None:
    """Why this must not run, or `None` to go ahead.

    `subject` names what is being bounded ("review `abc`") and lands in the message,
    which is the only place the caller learns both numbers.
    """
    if ceiling is None:
        return None
    total, unpriced = counted(spend)
    if total < ceiling:
        return None
    # The unpriced part is named rather than dropped: a caller who reads "$5.10 of
    # $5.00" without it will read the total as complete, and it is a floor.
    unknown = (
        f", and {len(unpriced)} more spent an unpriced amount on top ({', '.join(unpriced)})"
        if unpriced
        else ""
    )
    return (
        f"{subject} has spent ${total:.2f} of its ${ceiling:.2f} ceiling{unknown}; "
        "raise `consult.spend` or stop here"
    )
