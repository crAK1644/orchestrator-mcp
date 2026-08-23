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

import pytest

from orchestrator_mcp.code.adapters import codex_cli as codex_code
from orchestrator_mcp.consult.adapters import (
    antigravity_cli,
    claude_cli,
    codex_cli,
    opencode_cli,
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
