"""One turn, five parsers, one set of numbers.

`Usage` fixes what its three fields count -- see the docstring on the model -- because
the rollups add them across agents and the dashboard prints the sum in one column. Each
CLI reports something different: Claude calls the uncached remainder the input, Codex
folds cache and reasoning into its two headline figures as breakdowns, Antigravity and
Opencode report theirs disjoint, and Antigravity's own total leaves out the thinking it
just reported. An adapter that passes any of those through produces a number that
cannot be compared with the one beside it.

So the test is not that each parser reads its own envelope correctly -- the per-runtime
files cover that. It is that the *same turn*, described the way each runtime describes
it, comes back as the same three numbers.
"""

from __future__ import annotations

import logging

import pytest

from orchestrator_mcp.code.adapters import codex_cli as codex_code
from orchestrator_mcp.consult.adapters import (
    antigravity_cli,
    claude_cli,
    codex_cli,
    opencode_cli,
)
from orchestrator_mcp.consult.adapters.base import (
    check_reported_total,
    usage_any,
    usage_count,
)
from orchestrator_mcp.contract import Usage
from orchestrator_mcp.spend import caveats, tallied

# A 2000-token prompt, 1800 of it served from cache, answered with 500 generated tokens
# of which 400 were reasoning. Every runtime below is describing this turn.
PROMPT, COMPLETION = 2000, 500

TURN = {
    # Cache reported separately from `input_tokens`, thinking inside `output_tokens`.
    "claude": (
        claude_cli._usage,
        {"usage": {"input_tokens": 200, "output_tokens": 500,
                   "cache_read_input_tokens": 1800, "cache_creation_input_tokens": 0}},
    ),
    # Cache and thinking both separate, and `total_tokens` counts neither.
    "antigravity": (
        antigravity_cli._usage,
        {"usage": {"input_tokens": 200, "output_tokens": 100, "thinking_tokens": 400,
                   "cache_read_tokens": 1800, "total_tokens": 300}},
    ),
    # Four disjoint counts, and a total that agrees with all of them.
    "opencode": (
        opencode_cli._usage,
        {"tokens": {"total": 2500, "input": 200, "output": 100,
                    "reasoning": 400, "cache": {"write": 0, "read": 1800}}},
    ),
    # The other direction: cache and reasoning are breakdowns *of* the two headline
    # figures here, so adding them would count the same tokens twice.
    "codex": (
        codex_cli._usage,
        [{"usage": {"input_tokens": 2000, "cached_input_tokens": 1800,
                    "cache_write_input_tokens": 0, "output_tokens": 500,
                    "reasoning_output_tokens": 400}}],
    ),
    "codex-code": (
        codex_code._usage,
        [{"usage": {"input_tokens": 2000, "cached_input_tokens": 1800,
                    "cache_write_input_tokens": 0, "output_tokens": 500,
                    "reasoning_output_tokens": 400}}],
    ),
}


@pytest.mark.parametrize("runtime", sorted(TURN))
def test_one_turn_reads_the_same_whichever_runtime_answered(runtime):
    parse, payload = TURN[runtime]
    usage = parse(payload)

    # The cached share is prompt that was sent and billed. A runtime reporting 200 here
    # is reporting the part that missed cache, which on a consultation is the small part.
    assert usage.prompt_tokens == PROMPT
    # Reasoning and thinking are generated and billed at the output rate.
    assert usage.completion_tokens == COMPLETION
    assert usage.total_tokens == PROMPT + COMPLETION


@pytest.mark.parametrize("runtime", sorted(TURN))
def test_a_total_always_equals_its_own_parts(runtime):
    """Derived by every adapter, never read from the CLI.

    Antigravity's envelope above says 300 and Opencode's says 2500; only one of those
    is the turn. A total taken from whichever CLI answered is a number the rollups
    cannot add, and Antigravity's came out smaller than the two fields beside it.
    """
    parse, payload = TURN[runtime]
    usage = parse(payload)

    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens


# The same five envelopes with a count each runtime could plausibly get wrong: a string
# where a number belonged, a null, a list. Not hypothetical -- Antigravity writes `N/A`.
GARBAGE = {
    "claude": (claude_cli._usage, {"usage": {"input_tokens": "N/A", "output_tokens": None,
                                             "cache_read_input_tokens": ["?"]}}),
    "antigravity": (antigravity_cli._usage, {"usage": {"input_tokens": "N/A", "output_tokens": None,
                                                       "cache_read_tokens": ["?"],
                                                       "total_tokens": ["?"]}}),
    "opencode": (opencode_cli._usage, {"tokens": {"input": "N/A", "output": None,
                                                  "reasoning": ["?"],
                                                  "cache": {"read": "N/A", "write": None}}}),
    "codex": (codex_cli._usage, [{"usage": {"input_tokens": "N/A", "output_tokens": ["?"]}}]),
    "codex-code": (codex_code._usage, [{"usage": {"input_tokens": "N/A", "output_tokens": ["?"]}}]),
}


@pytest.mark.parametrize("runtime", sorted(GARBAGE))
def test_an_unreadable_count_is_not_what_loses_a_paid_answer(runtime):
    """Usage is reporting. Deriving the total means reading more fields than before --
    the cache and the reasoning, which every runtime spells differently -- and each one
    is a field that could end a consultation that already arrived, validated, and was
    billed. They are read tolerantly for that reason, which is also why the tolerant
    read is shared rather than sitting in the one adapter that happened to need it."""
    parse, payload = GARBAGE[runtime]
    usage = parse(payload)

    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (0, 0, 0)


@pytest.mark.parametrize("runtime", sorted(GARBAGE))
def test_a_substituted_count_reaches_the_caller_and_not_only_the_log(runtime):
    """A warning tells an operator, and only where the log level lets it through.

    The caller reading the response gets three integers, and an invented zero is
    indistinguishable in them from a measured one -- a small turn is a perfectly
    ordinary thing for a runtime to report. The answer is still returned, because
    losing a paid one over its receipt is worse; this field is how it stops arriving
    disguised as a measurement.
    """
    parse, payload = GARBAGE[runtime]
    usage = parse(payload)

    notes = usage.counts_incomplete
    assert notes and all("is not a token count" in note for note in notes)
    # One reason per distinct failure, not one per field it broke on: a runtime
    # failing the same way on two fields of one turn is one thing to go and look at.
    assert len(notes) == len(set(notes))


@pytest.mark.parametrize("runtime", sorted(TURN))
def test_a_turn_that_counted_cleanly_says_nothing(runtime):
    """The healthy path, which is every turn. A caveat on all of them is no caveat."""
    parse, payload = TURN[runtime]

    assert parse(payload).counts_incomplete == []


# --- what the helpers say when a count does not read -------------------------


@pytest.fixture
def warnings(caplog):
    """Warning lines from the usage helpers, whether or not a server was built first.

    `log.configure` sets `propagate = False` on the package logger so a host's root
    handler cannot pull records onto stdout, which is the MCP transport. `caplog`
    attaches its handler *to* the root, so this would capture nothing in a process
    where anything had already built a server. Propagation is restored for the
    duration rather than the test depending on a global another test may have flipped.
    """
    package = logging.getLogger("orchestrator_mcp")
    before = package.propagate
    package.propagate = True
    caplog.set_level(logging.WARNING, logger="orchestrator_mcp.consult.adapters.base")
    yield caplog
    package.propagate = before


def test_a_count_that_does_not_read_says_so_before_it_returns_zero(warnings):
    """The zero is the helper's invention, and it reads exactly like a measurement.

    Returning it is still right -- an answer that arrived, validated and was paid for
    must not be lost over its receipt -- but a total quietly missing a category is
    indistinguishable from a small turn, and nobody goes looking for a number that
    looks plausible. Saying so is the whole difference between an incomplete figure
    and a wrong one.
    """
    assert usage_count("N/A") == 0
    assert usage_count(-5) == 0

    lines = [record.getMessage() for record in warnings.records]
    assert len(lines) == 2
    assert "'N/A' is not a token count" in lines[0]
    assert "negative" in lines[1]


def test_an_absent_count_is_nothing_and_says_nothing(warnings):
    """A field a runtime does not fill is not a reporting failure. Antigravity reports
    no cache write and Codex no thinking figure; warning on either would put a line on
    every healthy turn, which is the same as not warning at all."""
    assert usage_count(None) == 0
    assert warnings.records == []


def test_a_malformed_first_spelling_does_not_take_the_slot(warnings):
    """`usage_count(a or b)` resolves the alias by truthiness before anything checks
    the winner is a number, so a present-but-unreadable first spelling wins the `or`
    and the good second one is never reached."""
    assert usage_any("N/A", 1200) == 1200
    assert usage_any(None, 1200) == 1200
    assert usage_any(1200, 999) == 1200
    # Zero is a count a runtime reported, not an absence to fall through. A turn that
    # generated nothing has to stay zero rather than picking up the alias behind it.
    assert usage_any(0, 999) == 0
    # A negative reads fine and still cannot be a count. Clamping it to 0 here would
    # hand the slot to an impossible quantity while a usable spelling sat beside it.
    assert usage_any(-5, 1200) == 1200

    # The rescue is of the number, not of the reporting: both malformed spellings are
    # still on the record, and the `None` that follows a bad one stays silent.
    lines = [record.getMessage() for record in warnings.records]
    assert len(lines) == 2
    assert "'N/A' is not a token count" in lines[0]
    assert "negative" in lines[1]


def test_every_alias_failing_is_zero_and_is_not_silent(warnings):
    """The fallback chain running out is the case the caller sees a 0 for, so it is
    the one that most needs saying. Only the present value is reported: a runtime that
    fills neither spelling has not failed at anything."""
    assert usage_any("N/A", None) == 0

    (line,) = [record.getMessage() for record in warnings.records]
    assert "'N/A' is not a token count" in line


def test_a_count_that_is_not_a_whole_number_is_not_a_count(warnings):
    """`int()` is too generous in three ways that each end in a plausible number.

    `bool` is a subclass of `int`, so a `"cached": true` bills as one token -- the
    same trap [`opencode_cli._usage`] already guards on the cost field, where
    `float(True)` charges a turn a dollar. A float truncates, so a fraction vanishes
    without comment. And an infinity raises `OverflowError`, not the `ValueError` a
    string raises, so it escapes a helper whose whole promise is that it never raises
    and takes a paid answer with it.
    """
    assert usage_count(True) == 0
    assert usage_count(12.9) == 0
    assert usage_count(float("inf")) == 0
    assert usage_count(float("nan")) == 0
    # What stays readable: a whole float, and a runtime that quotes its numbers.
    assert usage_count(12.0) == 12
    assert usage_count("1200") == 1200

    assert len([record.getMessage() for record in warnings.records]) == 4


def test_a_reported_total_that_disagrees_is_a_category_nobody_read(warnings):
    """`Usage.total_tokens` is derived and never read from a CLI, because each one
    totals a different set. The reported figure is still a checksum over the fields
    beside it -- it is how those fields were confirmed in the first place, and
    discarding it means a sixth token category arrives and nothing anywhere notices."""
    check_reported_total(1020, 1020, "antigravity")
    assert warnings.records == []

    check_reported_total(1500, 1020, "antigravity")
    (line,) = [record.getMessage() for record in warnings.records]
    assert "antigravity reported a total of 1500 where its own fields make 1020" in line


def test_a_total_that_is_absent_or_unreadable_is_not_a_disagreement(warnings):
    """Unlike a count, nothing displays this one. Codex's `exec` event carries no total
    at all, and a malformed one costs a comparison rather than a number -- there is no
    plausible-looking figure left behind for anyone to trust."""
    check_reported_total(None, 1020, "codex")
    check_reported_total("N/A", 1020, "codex")
    assert warnings.records == []


# --- the two directions, measured rather than assumed ------------------------
#
# The relationships below run opposite ways, which is the entire reason `Usage` has to
# state a meaning instead of letting each adapter pass its own CLI's numbers through.
# Neither was inferred from the single fixture that first showed it: consultations are
# fresh single-shot invocations that never hit cache, so every cache figure this server
# has ever stored is 0 and none of them can tell the two readings apart. The interactive
# session logs on the machine this was written on can, and did.


def test_codex_reports_its_cache_inside_its_input():
    """21,164 rollout envelopes carrying a positive cache, and every one satisfies
    `total == input + output`. Not one satisfies `total == input + cached + output`,
    and `cached_input_tokens` reaches `input_tokens` exactly without ever passing it.
    Adding the cache here would count 11,776 tokens twice."""
    usage = codex_cli._usage(
        [
            {
                "usage": {
                    "input_tokens": 12345,
                    "cached_input_tokens": 11776,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 456,
                    "reasoning_output_tokens": 384,
                    "total_tokens": 12801,
                }
            }
        ]
    )

    assert usage.prompt_tokens == 12345
    assert usage.completion_tokens == 456
    # 12801 is the envelope's own total, arrived at independently. The derived one
    # reproducing it is the evidence the parts were sorted the right way round.
    assert usage.total_tokens == 12801


def test_claude_reports_its_cache_beside_its_input():
    """The other direction, settled the same way. Across 39,465 envelopes carrying a
    positive `cache_read_input_tokens`, that field exceeds `input_tokens` every single
    time, and in 39,011 of them `input_tokens` is under 100 beside a cache read in the
    thousands. A reading where the cache sat inside the input is arithmetically
    impossible against those, so it has to be added."""
    usage = claude_cli._usage(
        {
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 14206,
                "cache_read_input_tokens": 17978,
                "output_tokens": 246,
            }
        }
    )

    assert usage.prompt_tokens == 2 + 14206 + 17978
    assert usage.completion_tokens == 246


def test_a_codex_total_that_stops_agreeing_is_reported(warnings):
    """What the check is for. If a future CLI starts counting a category these two
    fields do not cover, the derived total drifts silently -- the reported one is the
    only thing on the envelope that would notice."""
    codex_cli._usage(
        [{"usage": {"input_tokens": 2000, "output_tokens": 500, "total_tokens": 3000}}]
    )

    (line,) = [record.getMessage() for record in warnings.records]
    assert "codex reported a total of 3000 where its own fields make 2500" in line


# --- and through the sum, which is the number anyone reads --------------------


def test_a_caveat_survives_the_rollup_that_hides_it():
    """The review and workflow totals add these across agents, and that sum is where
    a caveat stops being visible: one reviewer whose counts were substituted
    contributes a figure indistinguishable from the rest. Carrying it up is what
    keeps the field from being defeated one level above where it is set."""
    note = "'N/A' is not a token count; counting it as 0"
    used = [
        Usage(prompt_tokens=2000, completion_tokens=500, total_tokens=2500),
        Usage(counts_incomplete=[note]),
    ]

    assert caveats(used) == [note]
    assert caveats(used[:1]) == []


def test_one_broken_runtime_behind_five_agents_is_one_caveat_and_a_count():
    """Five reviewers on the same CLI that stopped reporting its counts have one
    problem between them, and printing it five times buries whatever else went wrong.

    Counted, though, rather than only collapsed. One reviewer reporting nothing is a
    fluke and every reviewer reporting nothing is an outage, and a bare de-duplication
    renders them identically."""
    same = "'N/A' is not a token count; counting it as 0"
    other = "token count -5 is negative; counting it as 0"
    used = [Usage(counts_incomplete=[n]) for n in (same, other, same)]

    assert caveats(used) == [f"{same} (x2)", other]


def test_a_count_is_the_whole_depth_and_not_one_suffix_per_level():
    """The same reason tallied three times over must not render three counts.

    It is tallied three times because it passes three of them: across the fields of a
    turn, across the turns a rollup adds, across the agents a review adds. Each level
    is handed what the level below it already wrote, so a suffix that is appended
    rather than folded turns four occurrences into "(x2) (x2)" -- a number no reader
    can add up, on a line that reads like a formatting fault rather than an outage.
    """
    note = "'N/A' is not a token count; counting it as 0"
    # One turn that failed the same way on both of its count fields, twice over.
    turn = tallied([note, note])
    rollup = tallied(turn + turn)

    assert turn == [f"{note} (x2)"]
    assert rollup == [f"{note} (x4)"]
    assert caveats([Usage(counts_incomplete=rollup), Usage(counts_incomplete=[note])]) == [
        f"{note} (x5)"
    ]


def test_two_reasons_are_never_mistaken_for_one_reason_with_a_semicolon():
    """Why these travel as a list all the way to the display that joins them.

    Every reason here already contains "; " in its own wording, so a joined form
    cannot be split back into the reasons that made it. De-duplicating the joined
    strings then compares "A; B" against "A" as two unrelated notes and emits the
    shared half twice, which is exactly the noise the de-duplication was for.
    """
    a = "'N/A' is not a token count; counting it as 0"
    b = "token count -5 is negative; counting it as 0"
    used = [Usage(counts_incomplete=[a, b]), Usage(counts_incomplete=[a])]

    assert caveats(used) == [f"{a} (x2)", b]


def test_a_cache_bigger_than_the_prompt_holding_it_is_reported(warnings):
    """The one shape that separates Codex's two possible cache readings without a
    total to check against. A breakdown is contained by what it breaks down, and
    across 21,164 measured envelopes the cache reached the input and stopped. A cache
    that exceeds it is only possible if the two became disjoint -- at which point
    every prompt this adapter reports is short by the cached share."""
    from orchestrator_mcp.consult.adapters.base import check_cache_is_a_breakdown

    # Fully cached, which is ordinary, and says nothing.
    check_cache_is_a_breakdown(2000, 2000)
    # The warm session under the other reading: 1800 cached, 200 left to send.
    check_cache_is_a_breakdown(1800, 200)

    (line,) = [record.getMessage() for record in warnings.records]
    assert "1800 cached input tokens against a prompt of 200" in line
