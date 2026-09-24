"""What a send is likely to cost, from what the same agent's past turns cost.

An estimate, never a quote. It is shown beside a plan so the approval is not blind
to money, and it decides nothing: the ceilings in `spend` still refuse on what was
actually spent. The same rule as there applies to what is shown -- a price is given
only when enough turns carried one, because a guess built from a free tier's
missing prices would read as a total and be a floor.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from statistics import StatisticsError, fmean, linear_regression, median

from pydantic import BaseModel, ConfigDict, Field

# Used when no stored prompt can calibrate the agent: `store_full_content: false`
# keeps no prompt text, so there is nothing to fit against. A common rule of thumb
# for English and code, and only ever the fallback.
TOKENS_PER_CHAR = 0.25
# Fewer priced turns than this and the price is noise.
MIN_PRICED_TURNS = 3
HISTORY_TURNS = 50


class Estimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    # `None` for an agent that reports no price or has too few priced turns.
    cost_usd: float | None = None
    # How many past turns this was read from; the reader's measure of how much to
    # trust it.
    basis_turns: int = Field(ge=0)


def _fit(xs: list[float], ys: list[float], x: float) -> float:
    """`ys` at `x` on a least-squares line through the history.

    A line and not a ratio because a CLI's input carries a fixed cost the prompt does
    not: codex bills ~14K tokens of its own before the first character of ours, so a
    ratio overprices big prompts and underprices small ones. The mean when no line
    fits (one point, all the same size) or the slope comes out negative (noise).
    """
    try:
        slope, intercept = linear_regression(xs, ys)
    except StatisticsError:
        return fmean(ys)
    return max(0.0, intercept + slope * x) if slope >= 0 else fmean(ys)


def estimate(
    agent_id: str,
    prompt_chars: int,
    history: Sequence[tuple[int | None, int, int, float | None]],
) -> Estimate | None:
    """`history` is `(stored prompt chars, input, output, cost)` per past turn,
    newest first, errors excluded. `None` when there is no history at all.

    Cost is fitted on its own rather than derived from the tokens: Claude reports
    cache reads outside `input_tokens`, so its token counts say ~2 while its price
    still tracks the prompt size.
    """
    if not history:
        return None
    sized = [(float(c), i) for c, i, _, _ in history if c]
    input_tokens = (
        _fit([c for c, _ in sized], [i for _, i in sized], prompt_chars)
        if sized
        else prompt_chars * TOKENS_PER_CHAR
    )
    priced = [(c, cost) for c, _, _, cost in history if cost is not None]
    cost = None
    if len(priced) >= MIN_PRICED_TURNS:
        sized_priced = [(float(c), p) for c, p in priced if c]
        cost = round(
            _fit([c for c, _ in sized_priced], [p for _, p in sized_priced], prompt_chars)
            if sized_priced
            else median(p for _, p in priced),
            4,
        )
    return Estimate(
        agent_id=agent_id,
        input_tokens=round(input_tokens),
        output_tokens=round(median(o for _, _, o, _ in history)),
        cost_usd=cost,
        basis_turns=len(history),
    )


def total(estimates: Iterable[Estimate | None]) -> float | None:
    """The sum, or `None` if any part could not be priced -- a floor is not a total."""
    costs = [e.cost_usd if e else None for e in estimates]
    return None if not costs or None in costs else round(sum(costs), 4)


def ceiling_warning(
    known: float, estimated: float | None, ceiling: float | None, subject: str
) -> str | None:
    """Advisory only: the refusal is still `spend.refusal`, on what was spent."""
    if estimated is None or ceiling is None or known + estimated < ceiling:
        return None
    return (
        f"{subject} has spent ${known:.2f} and this is estimated at ${estimated:.2f}, "
        f"which would reach its ${ceiling:.2f} ceiling"
    )


async def for_agents(
    store, agents: Iterable[tuple[str, str]], prompt_chars: int
) -> tuple[list[Estimate], float | None]:
    """One estimate per `(agent_id, model)` that has history, and their total --
    `None` when any agent has no history or no price."""
    found = [
        estimate(agent_id, prompt_chars, await store.turn_history(agent_id, model, HISTORY_TURNS))
        for agent_id, model in agents
    ]
    return [e for e in found if e is not None], total(found)
