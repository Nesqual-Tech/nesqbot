"""Searching for a footprint instead of for an opinion.

The reported failure, verbatim: *"If i tell the leads agent to search for
companies that needs CRMs on linkedin and instagram that is the exact query that
the bot does: 'Companies that need CRM'."*

Nothing was broken. The desktop came up, the browser opened, the query was
typed, results came back and the bot reported them. Every guard in this codebase
passed, because every one of them is about whether a bot *acted* — and this bot
acted. What failed is upstream of any of that: a search index matches strings
that appear on pages, and no company publishes that it needs something, so the
query cannot return an account no matter how well the loop runs.

Two things are asserted here, and they are deliberately unequal in weight:

* **The standing instruction.** `RESEARCH_TRADECRAFT` is in the block every bot
  gets on every request, so the translation from *judgement* to *footprint* is
  something the model is told before it types anything.
* **The one refusal.** When a judgement query is typed anyway, the step does not
  run and the model is asked once, with its own query quoted back. Bounded by
  `AGENT_MAX_QUERY_NUDGES`, model-facing only, and no help at all to a task
  whose ask genuinely reads that way — which is why the pattern it fires on is
  narrow and why the tests below spend as much effort on what must *not* trip it.
"""

from __future__ import annotations

import pytest

from app.services import orchestrator as orch
from app.services.orchestrator import (
    AGENT_MAX_QUERY_NUDGES,
    RESEARCH_TRADECRAFT,
    TOOL_TASK_COMPLETE,
    desktop_static_block,
)
from tests.services.conftest import actions_in, acts, call, turn

# ---------------------------------------------------------------------------
# 1. What the pattern catches, and what it must leave alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        # The reported one, and the shapes a model reaches for around it.
        "companies that need CRM",
        "Companies that need a CRM",
        "companies that need crm software linkedin",
        "small businesses that need a website",
        "startups that don't have a CRM",
        "restaurants that need social media management",
        "companies looking for a new supplier",
        "agencies that could use automation",
        "shops that have no online store",
        "business owners who want more leads",
        "clients that require bookkeeping help",
    ],
)
def test_a_query_that_states_a_judgement_is_recognised(query):
    assert orch.Orchestrator()._is_a_judgement_query(query)


@pytest.mark.parametrize(
    "query",
    [
        # Footprints. Every one of these is a string that is really on pages,
        # and a bot re-prompted for typing one has been told off for doing the
        # job properly — which is worse than the bug this guard exists for.
        "companies that use Salesforce site:linkedin.com/company",
        '"we track deals in a spreadsheet" site:reddit.com',
        "site:linkedin.com/jobs sales operations manager Milan",
        "dentists in Milan instagram",
        "series A funding announcement fintech Berlin March 2026",
        "hubspot alternative migrating away from",
        "companies hiring their first SDR",
        # Not a search at all: the two things a bot types most often.
        "rita@acme.test",
        "",
        "   ",
    ],
)
def test_an_ordinary_query_is_left_alone(query):
    assert not orch.Orchestrator()._is_a_judgement_query(query)


def test_a_query_hidden_in_a_url_is_read_as_a_query():
    """A model that knows the URL shape skips the search box entirely."""
    agent = orch.Orchestrator()
    typed = agent._query_in(
        "browser_navigate",
        {"url": "https://www.google.com/search?q=companies+that+need+a+CRM&hl=en"},
    )
    assert "companies that need a CRM" in typed
    assert agent._is_a_judgement_query(typed)


def test_an_argument_that_cannot_carry_a_query_is_not_inspected():
    agent = orch.Orchestrator()
    assert agent._query_in("browser_click", {"ref": "e9"}) == ""
    assert agent._query_in("screenshot", {}) == ""


# ---------------------------------------------------------------------------
# 2. The standing instruction, which is what has to work most of the time
# ---------------------------------------------------------------------------


def test_every_bot_is_told_how_to_search_before_it_types_anything():
    """In the static block, so it is not per-bot and not per-task.

    The same argument `DESKTOP_CAPABILITY` is there for: every bot has a browser
    and every bot has this problem, and a paragraph copied into five YAML files
    drifts in five directions. It is also the half of the prompt Azure's cache
    pays for — see `test_prompt_cache_prefix.py`.
    """
    assert RESEARCH_TRADECRAFT in desktop_static_block()
    text = RESEARCH_TRADECRAFT.lower()
    # The mechanism, not just the instruction: *why* the obvious query fails.
    assert "not intent" in text
    assert "footprint" in text
    # And the two habits the reported thread was missing.
    assert "site:" in text
    assert "url" in text


def test_the_reported_query_is_the_example_in_the_prompt():
    """The failure is named in the words it happened in, not paraphrased."""
    assert "companies that need a CRM" in RESEARCH_TRADECRAFT


# ---------------------------------------------------------------------------
# 3. The refusal, in a real run
# ---------------------------------------------------------------------------


async def test_a_judgement_query_is_not_run_and_the_bot_is_asked_again(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The step does not happen, and the second query is the one that does."""
    orchestrator = agent_with(
        [
            acts("", call("type", text="companies that need a CRM")),
            acts(
                "",
                call("type", text='"we track deals in a spreadsheet" site:linkedin.com/posts'),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Four named accounts, with sources.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "find companies needing a CRM")

    # One typed step, and it is the good one. The judgement query never ran.
    assert actions_in(frames) == ["type"]

    # The model was told why, with its own query quoted back at it.
    told = [
        str(message.get("content") or "")
        for request in orchestrator.router.seen
        for message in request
        if message.get("role") == "tool"
    ]
    nudge = next((text for text in told if "was not run" in text), "")
    assert nudge, "the refused query was never explained to the model"
    assert "companies that need a CRM" in nudge
    assert "footprint" in nudge

    # And none of it reaches the person, who asked for companies and not for a
    # commentary on their bot's search technique. The step log is the loop's own
    # account of what ran, so the refused query must be absent from it too: it
    # is not a failed step, it is a step that never happened.
    assert done["message"].startswith("Four named accounts, with sources.")
    assert "companies that need a CRM" not in done["message"]
    assert done["message"].count("Typed") == 1


async def test_the_second_judgement_query_of_a_run_goes_through(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """One lesson per run. A guard that can fire forever can trap a run forever.

    The ask really can be shaped like a judgement — a person may genuinely want
    that phrase typed into a site with its own semantic search — so the
    allowance is spent and then the bot gets on with it and answers for whatever
    comes back.
    """
    assert AGENT_MAX_QUERY_NUDGES == 1
    orchestrator = agent_with(
        [
            acts("", call("type", text="companies that need a CRM")),
            acts("", call("type", text="businesses that need a CRM")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Two searches run; results are thin.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "search it anyway")

    # The first was refused, the second ran — one `type`, and it is the second.
    assert actions_in(frames) == ["type"]
    assert done["message"].startswith("Two searches run; results are thin.")
    assert 'Typed "businesses that need a CRM"' in done["message"]
