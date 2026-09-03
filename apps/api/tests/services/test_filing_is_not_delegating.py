"""A work item is a record. It does not wake the bot named in its title.

Reported verbatim: *"nothing was sent to either sales nor leads"*. What the
chief of staff had actually done, from the transcript:

    create_work_item  "Lead Generator: Generate 20 qualified leads…"
    create_work_item  "Sales: Prepare to close deals…"
    update_work_item  → waiting
    update_work_item  → waiting
    task_complete     "Routed main goal tasks…"

It reported that it had *routed* the work. Nothing had started. A work item is a
row in the customer's own records; the bot named in its title never sees it, and
`services/work_items.py` is explicit that even a real `transfer_work_item` is a
durable change of owner rather than a hand-off. `delegate_to_bot` is the only
thing that starts another bot.

Both existing guards were blind to it, which is what makes it worth its own
file:

* `_announces_action` only runs on a turn that produced **no** tool calls. This
  turn produced five.
* `_addresses_another_bot` reads **prose**. Here the teammates were named inside
  tool arguments — a title that reads like a to-do entry, not a sentence
  addressed to anybody.

So the run cost five model calls, looked like work, produced a confident summary
and started nobody. That is the most expensive version of this failure: the
person reads "Routed main goal tasks" and waits.

The fix is checked at the *close* rather than at the file, because filing a
record and then delegating is correct and common — the item is how the receiving
bot finds the lead again. Only the ending was ever wrong.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select

from app.models import Bot, Run
from app.services import orchestrator as orch
from app.services.agent_work_items import TOOL_CREATE_WORK_ITEM
from app.services.orchestrator import TOOL_DELEGATE_TO_BOT, TOOL_TASK_COMPLETE
from tests.services.conftest import acts, call

# ---------------------------------------------------------------------------
# Harness — the same shape `test_delegation.py` uses
# ---------------------------------------------------------------------------


async def turn_as(orchestrator, db, user, thread, bot, content: str = "start on the goal"):
    """One turn with `bot` pinned as the responder — see test_delegation.py."""
    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db, user=user, thread=thread, content=content, mention_bot_ids=[bot.id]
        )
    ]
    done = next((data for name, data in frames if name == "done"), {})
    return frames, done


async def _seeded(db, slug: str) -> Bot:
    from decimal import Decimal

    bot = (await db.execute(select(Bot).where(Bot.slug == slug))).scalar_one()
    bot.daily_budget_usd = Decimal("500.00")
    await db.commit()
    return bot


async def _runs_for(db, bot: Bot) -> list[Run]:
    rows = await db.execute(select(Run).where(Run.bot_id == bot.id))
    return list(rows.scalars().all())


def _tool_texts(router) -> list[str]:
    return [
        str(message.get("content") or "")
        for request in router.seen
        for message in request
        if message.get("role") == "tool"
    ]


@pytest_asyncio.fixture
async def avery(make_user):
    return await make_user(email="avery@nesqualtech.test", display_name="Avery V")


@pytest_asyncio.fixture
async def lead_bot(db):
    return await _seeded(db, "lead_generator")


@pytest_asyncio.fixture
async def sales_bot(db):
    return await _seeded(db, "sales")


# ---------------------------------------------------------------------------
# 1. Said at the moment of the mistake
# ---------------------------------------------------------------------------


async def test_filing_a_row_about_another_bot_says_it_is_not_a_hand_off(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    orchestrator = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_CREATE_WORK_ITEM,
                    type="task",
                    title="Sales: close the deals from the new leads",
                ),
            ),
            acts(
                "",
                call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close the two replied leads."),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Closed one, one pending.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Sales is on it.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    filed = next(
        (text for text in _tool_texts(orchestrator.router) if "record, not a hand-off" in text), ""
    )
    assert filed, "filing a row naming Sales never said it does not reach Sales"
    assert sales_bot.name in filed
    assert TOOL_DELEGATE_TO_BOT in filed


# ---------------------------------------------------------------------------
# 2. The close, which is where the reported run ended
# ---------------------------------------------------------------------------


async def test_a_run_that_only_files_rows_is_refused_its_close_once(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """The reported run, and the second chance it never got."""
    orchestrator = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_CREATE_WORK_ITEM,
                    type="task",
                    title="Sales: prepare to close deals in software dev",
                ),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Routed main goal tasks.")),
            # The second chance, used properly.
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close the replied leads.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Two calls booked.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Sales booked two calls.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    _frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    refusal = next((text for text in _tool_texts(orchestrator.router) if "Not closed." in text), "")
    assert refusal, "the run closed without being told that filing had started nobody"
    assert sales_bot.name in refusal
    # And the hand-off really happened on the retry, so the person hears about work.
    assert len(await _runs_for(db, sales_bot)) == 1
    assert "booked two calls" in done["message"]


async def test_a_run_that_still_will_not_delegate_says_nobody_was_started(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """One lesson per run. It may then end — but not while claiming it routed.

    "Routed main goal tasks" is the sentence that made this expensive.
    """
    orchestrator = agent_with(
        [
            acts(
                "",
                call(TOOL_CREATE_WORK_ITEM, type="task", title="Sales: close deals to hit 20k EUR"),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Routed main goal tasks.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Routed main goal tasks.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    _frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert orch.AGENT_MAX_CLOSE_NUDGES == 1
    assert not await _runs_for(db, sales_bot), "nobody should have been started"
    # The wording is one sentence for both shapes of this failure - filing rows,
    # and doing the work in the specialists' place - because what the person
    # needs to know is the same either way: nobody else started.
    assert "did not hand any of it over" in done["message"]
    assert "nobody else has started" in done["message"]
    assert sales_bot.name in done["message"]


# ---------------------------------------------------------------------------
# 3. What must stay quiet
# ---------------------------------------------------------------------------


async def test_filing_after_a_real_hand_off_is_not_lectured(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """Delegate, then record what it is about. The correct order, undisturbed.

    The work item is exactly how the receiving bot finds the lead again, so
    filing one is good practice; punishing it would trade one bad habit for
    another.
    """
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Booked Thursday.")),
            acts(
                "",
                call(
                    TOOL_CREATE_WORK_ITEM,
                    type="lead",
                    title="Rita at Acme - with Sales, demo Thursday",
                ),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Rita is with Sales, demo Thursday.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    _frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert not any("Not closed." in text for text in _tool_texts(orchestrator.router))
    assert "did not hand it over" not in done["message"]
    assert "demo Thursday" in done["message"]


async def test_a_lead_that_merely_mentions_nothing_is_not_a_teammate(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """A single-bot thread has nobody to delegate to, so nothing to warn about."""
    orchestrator = agent_with(
        [
            acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Star Dental - booking page")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Logged Star Dental.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot])

    _frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert not any("record, not a hand-off" in text for text in _tool_texts(orchestrator.router))
    assert "Logged Star Dental" in done["message"]


# ---------------------------------------------------------------------------
# 4. The unit underneath it
# ---------------------------------------------------------------------------


class _FakeBot:
    def __init__(self, name: str, slug: str) -> None:
        self.name, self.slug = name, slug


def test_a_teammate_named_in_a_tool_argument_is_recognised():
    """Prose rules find nothing in these strings, which is the whole point."""
    orchestrator = orch.Orchestrator()
    targets = [_FakeBot("Sales", "sales"), _FakeBot("Lead Generator", "lead_generator")]

    assert orchestrator._teammates_named_in(
        {"type": "task", "title": "Lead Generator: Generate 20 qualified leads"}, targets
    ) == {"Lead Generator"}

    assert orchestrator._teammates_named_in(
        {"title": "Hand to sales once Lead Generator finishes"}, targets
    ) == {"Sales", "Lead Generator"}

    # An ordinary lead has nothing to do with the roster.
    assert (
        orchestrator._teammates_named_in(
            {"type": "lead", "title": "Star Dental - needs a booking page"}, targets
        )
        == set()
    )
    # Non-string values must not blow it up, and must not match either.
    assert orchestrator._teammates_named_in({"detail": {"stage": "replied"}}, targets) == set()
    assert orchestrator._teammates_named_in({}, targets) == set()


# ---------------------------------------------------------------------------
# 5. Doing the work instead of handing it over
# ---------------------------------------------------------------------------
#
# Reported the same day the filing fix shipped: *"now i asked the chief of staff
# the same thing and instead of delegating the work, it started doing it
# himself"*. Told that filing a row starts nobody, it drew the wrong conclusion
# and worked the whole goal alone - on a thread where the person had tagged the
# two specialists who own the accounts it needed.
#
# Partly self-inflicted: the prompt said "hand it the work again with a narrower
# brief, or do it yourself", and REPROMPT_FOR_DELEGATION offered self-execution
# as a plain alternative. Under "act, do not announce" pressure, every escape
# hatch pointed at the same place. Those are gone from the prompt; this is the
# code half.
#
# The guard reads a *tag* as the statement of intent it is. Naming @Sales on a
# message is the person saying whose job they think this is, so a run that ends
# having done that work in their place has not routed anything either.


async def test_working_the_goal_alone_when_teammates_were_tagged_is_refused_once(
    agent_with, db, avery, make_thread, lead_bot, sales_bot, varying_screens
):
    """No work items at all this time - just a bot doing the job itself."""
    orchestrator = agent_with(
        [
            acts("", call("start_desktop")),
            acts("", call("type", text='site:linkedin.com/jobs "sales operations" Milan')),
            acts("", call(TOOL_TASK_COMPLETE, summary="Found four accounts myself.")),
            # The second chance, used properly.
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Work these four accounts.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Two calls booked.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Sales booked two calls.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db,
            user=avery,
            thread=thread,
            content="get 20 leads and close them",
            mention_bot_ids=[lead_bot.id, sales_bot.id],
        )
    ]
    done = next((data for name, data in frames if name == "done"), {})

    refusal = next(
        (text for text in _tool_texts(orchestrator.router) if "did this work yourself" in text), ""
    )
    assert refusal, "a run that did the specialists' work itself was never questioned"
    assert sales_bot.name in refusal
    assert "signed into the accounts" in refusal
    assert len(await _runs_for(db, sales_bot)) == 1, "the retry should have woken Sales"
    assert "booked two calls" in done["message"]


async def test_an_untagged_thread_lets_a_bot_get_on_with_it(
    agent_with, db, avery, make_thread, lead_bot, sales_bot, varying_screens
):
    """Nobody was named, so nothing was owed to anybody. No interrogation.

    This is the case that keeps the guard honest: a bot doing work on its own
    initiative is the product working, and the only thing that makes it wrong
    is the person having said whose job it was.
    """
    orchestrator = agent_with(
        [
            acts("", call("start_desktop")),
            acts("", call("type", text='"we track deals in a spreadsheet" site:reddit.com')),
            acts("", call(TOOL_TASK_COMPLETE, summary="Four accounts with the signal.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    _frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot, "find some leads")

    assert not any("Not closed." in text for text in _tool_texts(orchestrator.router))
    assert "Four accounts" in done["message"]
    assert "nobody else has started" not in done["message"]


async def test_a_question_answered_on_a_tagged_thread_is_not_interrogated(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """The run did nothing, so there is nothing it did instead of delegating.

    `task_complete` on the opening turn is a legitimate answer - "your budget is
    $8 a day" - and a person who tagged two bots while asking a question should
    not have their answer held up over it.
    """
    orchestrator = agent_with([acts("", call(TOOL_TASK_COMPLETE, summary="Pipeline is 4 deals."))])
    thread = await make_thread(avery, [lead_bot, sales_bot])

    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db,
            user=avery,
            thread=thread,
            content="how is the pipeline?",
            mention_bot_ids=[lead_bot.id, sales_bot.id],
        )
    ]
    done = next((data for name, data in frames if name == "done"), {})

    assert done["message"] == "Pipeline is 4 deals."
    assert not any("Not closed." in text for text in _tool_texts(orchestrator.router))
