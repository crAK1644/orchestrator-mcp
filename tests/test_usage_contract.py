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


def test_a_malformed_first_spelling_does_not_take_the_slot():
    """`usage_count(a or b)` resolves the alias by truthiness before anything checks
    the winner is a number, so a present-but-unreadable first spelling wins the `or`
    and the good second one is never reached."""
    assert usage_any("N/A", 1200) == 1200
    assert usage_any(None, 1200) == 1200
    assert usage_any(1200, 999) == 1200
    # Zero is a count a runtime reported, not an absence to fall through. A turn that
    # generated nothing has to stay zero rather than picking up the alias behind it.
    assert usage_any(0, 999) == 0


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
