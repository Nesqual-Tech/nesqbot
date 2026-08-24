"""What the system prompt has to look like for Azure's prompt cache to hit.

Azure OpenAI caches prompt prefixes automatically: above **1,024 tokens** of
prefix it stores one, matches in 128-token increments beyond that, and bills
the matched part at **50% of the input rate**. There is one condition and it is
unforgiving -- the prefix has to be *byte-identical* between requests. The
first character that differs ends the match, and everything after it is billed
in full.

Measured on this codebase before the ordering was changed, off three real turns
of the same bot (`compose_system_prompt`'s docstring has the arithmetic):

    system prompt per request      2,392 - 2,449 tokens
    longest common prefix                 143 chars = 35 tokens
    Azure threshold                     1,024 tokens
    cache entries ever stored                       0

The bot's own prompt led, and the RAG memory block sat immediately behind it.
Seeded bot prompts are 85-400 tokens, so the stable head could never reach the
threshold on its own, and the memory block is re-ranked against every user
message -- so the prefix died 143 characters in, on every request the product
had ever made. Roughly 2,400 tokens of near-static text were being paid for at
full rate, forever.

Leading with `desktop_static_block()` fixes it because that block is 2,174
tokens by itself and identical for every bot, every thread and every user. The
same three turns now share a 2,210-token prefix.

These tests are the guard on that ordering. The failure they exist to catch is
not a crash -- it is somebody adding a block in the obvious place, at the top
near the bot's prompt, and the bill quietly doubling with every test still
green. If one of these fails, the question to ask is "is the block I just added
the same on every request?"; if it is not, it belongs at the bottom.
"""

from __future__ import annotations

import pytest

from app.services.model_router import ChatResult, cached_prompt_tokens
from app.services.orchestrator import (
    CACHE_PREFIX_MIN_TOKENS,
    compose_system_prompt,
    desktop_static_block,
)

#: The repo counts a token as four characters everywhere else it estimates one
#: (`count_request_tokens`, `count_text_tokens`), so these numbers are
#: comparable with the ones in `test_agent_context_budget.py`.
CHARS_PER_TOKEN = 4


def _tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _shared_prefix_tokens(a: str, b: str) -> int:
    """Length of the byte-identical head of two prompts, in tokens.

    This is the quantity Azure actually decides on: not how similar the two
    prompts are overall, but how far in they agree before the first character
    that differs.
    """
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index] == b[index]:
        index += 1
    return index // CHARS_PER_TOKEN


#: A bot prompt at the small end of what is actually seeded. `sales.yaml` is 85
#: tokens; the point of the fixture is that the stable head must clear the
#: threshold *without* help from the bot's own prompt.
SMALL_BOT_PROMPT = "You are Sales. You qualify inbound leads and book demos. Be brief."


# ---------------------------------------------------------------------------
# 1. The prefix is long enough to be cached at all
# ---------------------------------------------------------------------------


def test_the_static_head_clears_the_threshold_on_its_own():
    """2,174 tokens against a 1,024-token floor, before any bot text is added.

    Asserted on `desktop_static_block()` alone rather than on an assembled
    prompt because that is the claim that has to survive: the cacheable prefix
    must not depend on which bot is talking. A bot seeded with an 85-token
    prompt gets the same cache entry as one with a 400-token prompt.
    """
    assert _tokens(desktop_static_block()) > CACHE_PREFIX_MIN_TOKENS


def test_the_static_head_is_the_same_string_every_time():
    """A prefix that is rebuilt per request is only cacheable if it is stable.

    `desktop_protocol_block()` renders itself from the `DESKTOP_ACTIONS` and
    `BROWSER_ACTIONS` tables on every call. Dict order is insertion order, so
    this holds -- but it holds by a language guarantee rather than by anything
    the module says out loud, which is worth one assertion.
    """
    assert desktop_static_block() == desktop_static_block()


# ---------------------------------------------------------------------------
# 2. The ordering: stable first, volatile last
# ---------------------------------------------------------------------------


def test_the_prompt_opens_with_the_block_that_never_changes():
    """The guard. A volatile block placed in front of this would fail here."""
    system = compose_system_prompt(
        bot_prompt=SMALL_BOT_PROMPT,
        connector_block="Connectors available to you:\n- crm: create_lead(mutate)",
        memory_block="Memories:\n- (fact) The pipeline id is 4.",
        ledger_block='Shared context ledger: {"last_bot": "sales"}',
        desktop_state="Right now your desktop is 'running'.",
        delegation_block="\n\n### Handing work to another bot\n- ops",
    )
    assert system.startswith(desktop_static_block())


@pytest.mark.parametrize(
    "volatile",
    [
        pytest.param({"memory_block": "Memories:\n- (fact) x"}, id="memories"),
        pytest.param({"ledger_block": "Shared context ledger: {}"}, id="ledger"),
        pytest.param({"desktop_state": "Right now your desktop is 'running'."}, id="desktop"),
        pytest.param({"delegation_block": "\n\n### Handing work\n- ops"}, id="delegation"),
    ],
)
def test_every_volatile_block_lands_after_the_static_head(volatile):
    """Each one, on its own, so a failure names the block that moved.

    A block is "volatile" here if two requests a minute apart can disagree
    about it: memories are re-ranked per message, the ledger is rewritten per
    turn, the desktop state changes mid-run, and the delegation allowance
    counts down as a chain spends it.
    """
    system = compose_system_prompt(bot_prompt=SMALL_BOT_PROMPT, **volatile)
    body = next(iter(volatile.values())).strip()
    assert system.index(body) > len(desktop_static_block())


def test_two_turns_that_differ_only_in_memories_still_share_a_cacheable_prefix():
    """The end-to-end property, and the one the money actually depends on.

    Before the reorder this was 143 characters. The two prompts here differ in
    exactly the way two real turns of one bot differ -- same bot, same
    connectors, different RAG hits -- and the shared head has to stay over the
    threshold.
    """
    common = {
        "bot_prompt": SMALL_BOT_PROMPT,
        "connector_block": "Connectors available to you:\n- crm: create_lead(mutate)",
        "desktop_state": "Right now your desktop is 'running'.",
    }
    first = compose_system_prompt(
        memory_block="Memories:\n- (fact) The pipeline id is 4.", **common
    )
    second = compose_system_prompt(
        memory_block="Memories:\n- (fact) Demos are booked in Pipedrive stage 12.", **common
    )

    assert first != second, "the fixture is not exercising a difference"
    assert _shared_prefix_tokens(first, second) > CACHE_PREFIX_MIN_TOKENS


def test_a_bot_with_no_connectors_still_shares_the_head_with_one_that_has_them():
    """Two different bots, one cache entry for the part they have in common.

    Worth asserting because it is the reason the desktop vocabulary leads
    rather than the bot's prompt: a per-bot head would need every bot to earn
    its own 1,024 tokens before anything was cached at all.
    """
    plain = compose_system_prompt(bot_prompt="You are Ops.")
    wired = compose_system_prompt(
        bot_prompt="You are Sales.",
        connector_block="Connectors available to you:\n- crm: create_lead(mutate)",
    )
    assert _shared_prefix_tokens(plain, wired) > CACHE_PREFIX_MIN_TOKENS


# ---------------------------------------------------------------------------
# 3. Nothing was lost in the reordering
# ---------------------------------------------------------------------------


def test_reordering_did_not_drop_a_block():
    """The other half of the change: same text, different order.

    A reorder that silently stopped sending the ledger would make every cache
    test above pass and would change what the model knows, which is the one
    thing this lane was not allowed to do.
    """
    blocks = {
        "bot_prompt": "You are Sales. You qualify inbound leads.",
        "connector_block": "Connectors available to you:\n- crm: create_lead(mutate)",
        "memory_block": "Memories:\n- (fact) The pipeline id is 4.",
        "ledger_block": 'Shared context ledger: {"last_bot": "sales"}',
        "desktop_state": "Right now your desktop is 'running'.",
        "delegation_block": "\n\n### Handing work to another bot\n- ops",
    }
    system = compose_system_prompt(**blocks)
    for name, text in blocks.items():
        assert text.strip() in system, name
    assert desktop_static_block() in system


def test_an_absent_block_leaves_no_hole():
    """Most turns have no ledger, no connectors and nobody to delegate to.

    An empty block must not become a run of blank lines: whitespace is as much
    a part of a byte-identical prefix as anything else, and a prompt whose
    shape depends on which optional blocks happened to be empty is a prompt
    with several cache entries where it should have one.
    """
    system = compose_system_prompt(
        bot_prompt="You are Ops.",
        memory_block="No stored memories yet.",
    )
    assert "\n\n\n" not in system
    assert system.endswith("No stored memories yet.")


# ---------------------------------------------------------------------------
# 4. The hit rate is observable
# ---------------------------------------------------------------------------
#
# None of the above proves the cache *did* engage -- only Azure can say that,
# in `usage.prompt_tokens_details.cached_tokens`. Nothing in this codebase read
# that field, so the hit rate was unobservable and a regression in the ordering
# would have shown up as a bill rather than as a number anybody was watching.


class _Details:
    def __init__(self, cached_tokens):
        self.cached_tokens = cached_tokens


class _Usage:
    def __init__(self, details=None):
        self.prompt_tokens = 2_400
        if details is not None:
            self.prompt_tokens_details = details


def test_a_reported_cache_hit_is_read_off_the_usage_block():
    assert cached_prompt_tokens(_Usage(_Details(2_048))) == 2_048


def test_the_field_is_also_read_when_the_sdk_hands_back_a_dict():
    assert cached_prompt_tokens(_Usage({"cached_tokens": 1_920})) == 1_920


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(_Usage(), id="no-details-older-api-version"),
        pytest.param(_Usage(_Details(None)), id="details-present-value-null"),
        pytest.param(_Usage(_Details(0)), id="reported-miss"),
    ],
)
def test_an_unreported_cache_reads_as_zero_rather_than_raising(usage):
    """Absent means "nothing was reported", which is the same as a miss here.

    The keyless mock path, older `api-version` values and non-Azure endpoints
    all omit it, and none of those is an error worth failing a chat turn over.
    """
    assert cached_prompt_tokens(usage) == 0


def test_cached_tokens_are_a_subset_of_the_input_tokens_not_an_addition():
    """The same rule `image_tokens` follows, for the same reason.

    The ledger and the daily budget read `input_tokens`; this field only says
    what that number was made of. A reader who added the two would double-count
    the cheap half of the prompt.
    """
    result = ChatResult("hi", "reason", 2_400, 12, __import__("decimal").Decimal("0"))
    assert result.cached_tokens == 0
    result.cached_tokens = 2_048
    assert result.cached_tokens < result.input_tokens
