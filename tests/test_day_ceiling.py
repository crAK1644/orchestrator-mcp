"""The daily ceilings' rule, apart from the services that ask it.

`day_refusal` is handed the window's turns oldest first and decides two things: whether
the next turn may run, and if not, when it could. The first is `refusal`'s rule over a
window rather than a record -- reached, not exceeded, and an unpriced turn named rather
than counted as free. The second is what a record's ceiling never needed: it stays
reached until somebody raises it, where the day's comes undone as its turns age out.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator_mcp.consult.store import _now
from orchestrator_mcp.spend import DAY, day_cutoff, day_refusal

T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def at(minutes: int) -> str:
    """A ledger timestamp `minutes` after `T0`, in the shape `_now` writes."""
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def window(*costs: float | None, agent: str = "codex-sol") -> list[tuple[str, float | None, str]]:
    """One turn per cost, a minute apart, oldest first."""
    return [(at(minute), cost, agent) for minute, cost in enumerate(costs)]


def lifted(minute: int) -> str:
    """How a refusal names the moment the turn recorded at `minute` stops counting."""
    return f"try again after {T0 + DAY + timedelta(minutes=minute):%Y-%m-%d %H:%M:%S} UTC"


def test_a_window_under_both_ceilings_lets_the_next_turn_run():
    assert day_refusal(window(None, None), None, 3) is None
    assert day_refusal(window(0.4, 0.5), 1.0, None) is None
    assert day_refusal(window(0.4, 0.5), 1.0, 3) is None


def test_no_ceiling_refuses_nothing_however_much_was_spent():
    assert day_refusal(window(*[100.0] * 50), None, None) is None


def test_the_turn_ceiling_is_reached_not_exceeded():
    """Three turns against a ceiling of three refuses the fourth: the third was the
    last one allowed, which is how every other turn ceiling here reads too."""
    assert day_refusal(window(None, None), None, 3) is None
    message = day_refusal(window(None, None, None), None, 3)
    assert message is not None
    assert message.startswith("the last 24 hours have used 3 of the 3 daily turn ceiling;")
    assert "raise `consult.spend`" in message


def test_the_money_ceiling_is_reached_not_exceeded():
    assert day_refusal(window(0.5, 0.49), 1.0, None) is None
    message = day_refusal(window(0.5, 0.5), 1.0, None)
    assert message is not None
    assert message.startswith("the last 24 hours have spent $1.00 of the $1.00 daily ceiling;")


def test_an_empty_window_is_never_refused():
    """Nothing in the last day is nothing spent, whatever the ceilings say."""
    assert day_refusal([], 0.01, 1) is None


def test_the_turn_refusal_lifts_when_enough_of_the_oldest_turns_age_out():
    """Five turns against a ceiling of three: two ageing out leaves three, which is
    still the ceiling, so the third oldest is the one whose age decides."""
    message = day_refusal(window(*[None] * 5), None, 3)
    assert message is not None
    assert lifted(2) in message


def test_the_money_refusal_lifts_once_enough_spend_has_aged_out():
    """$11 against $5: losing the $5 turn leaves $6, losing the next $1 leaves exactly
    $5 -- still reached -- and only the third leaves $4. Reached, not exceeded, holds
    for the moment it lifts as much as for the refusal itself."""
    message = day_refusal(window(5.0, 1.0, 1.0, 4.0), 5.0, None)
    assert message is not None
    assert "$11.00 of the $5.00 daily ceiling" in message
    assert lifted(2) in message


def test_the_time_given_is_one_at_which_neither_ceiling_refuses():
    """The money lifts when the first turn ages out and the turns only when the second
    does. Naming the first would send the host back at a moment the other ceiling
    refuses instead -- so the message is the money one, and the time is the turns'."""
    message = day_refusal(window(2.0, None, None, None), 2.0, 3)
    assert message is not None
    assert "$2.00 of the $2.00 daily ceiling" in message
    assert lifted(1) in message


def test_an_unpriced_turn_is_named_and_not_counted_as_free():
    turns = [(at(0), 1.0, "codex-sol"), (at(1), None, "gemini-x"), (at(2), None, "flash")]
    message = day_refusal(turns, 1.0, None)
    assert message is not None
    assert (
        "$1.00 of the $1.00 daily ceiling, and 2 more spent an unpriced amount beyond that "
        "(flash, gemini-x);"
    ) in message
    assert "unpriced" not in day_refusal(window(0.5, 0.5), 1.0, None)


def test_the_turn_refusal_names_no_price_it_did_not_count():
    """A turn ceiling counts turns whatever they cost, so an unpriced one is not a
    caveat on it."""
    assert "unpriced" not in day_refusal(window(None, None), 5.0, 2)


@pytest.fixture
def east_of_utc(monkeypatch):
    """A local zone nine hours from UTC, so a time read as local is visibly wrong."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_timestamp_the_ledger_did_not_write_still_counts(east_of_utc):
    """Read the way the dashboard reads the same column. A row this server did not write
    keeps counting -- the ceiling is about turns, not about tidy rows -- and only the
    moment it lifts goes unsaid. One with no offset is the ledger's own UTC, never the
    local time of whichever machine reads it."""
    message = day_refusal([("not a time", None, "codex-sol")], None, 1)
    assert message is not None
    assert message.endswith("try again later")
    naive = day_refusal([("2026-10-17T09:00:00", None, "codex-sol")], None, 1)
    assert naive is not None
    assert naive.endswith(lifted(0))


def test_the_cutoff_is_a_day_back_in_the_ledgers_own_shape():
    """Compared as text in SQL, so it has to be the shape `_now` writes or the order
    the comparison finds is not the order of the times."""
    assert DAY == timedelta(hours=24)
    assert day_cutoff(T0) == "2026-10-16T09:00:00+00:00"
    shape = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00")
    assert shape.fullmatch(day_cutoff()) and shape.fullmatch(_now())


def test_a_turn_stops_counting_the_instant_after_the_moment_named():
    """A refusal over a turn stamped T says "after T + 24h". At T + 24h the turn is
    exactly a day old and still counts; half a second later it must not, though the
    ledger's seconds never move. Floored, the cutoff kept it for that half second, and
    a host trying again when it was told to was refused again."""
    stamp = at(0)
    assert day_refusal([(stamp, None, "codex-sol")], None, 1).endswith(lifted(0))

    assert stamp >= day_cutoff(T0 + DAY)
    assert not stamp >= day_cutoff(T0 + DAY + timedelta(milliseconds=500))
    assert day_cutoff(T0 + DAY + timedelta(milliseconds=500)) == "2026-10-17T09:00:01+00:00"
