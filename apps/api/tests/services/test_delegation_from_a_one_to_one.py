"""A bot can hand work to its team without the person assembling a room first.

The production failure this exists for, in the person's own words and the
bot's. Sent from an ordinary one-to-one chat with the orchestrator:

    "Maya, I give you the challenge to make 25k euros by end of the month. Use
    Jordan to capture leads on Instagram and LinkedIn and Sales to close the
    deals. We need software development contracts. I count on you"

Answered:

    "I did not act beyond logging and checking the two work items. No leads
    were captured, no accounts were contacted, and no desktop or web action was
    taken. The work items remain open for Lead Generator and Sales to execute."

Nothing was misconfigured and the prompt was not weak. Every thread in that
database had exactly one bot in it — a one-to-one chat is what the app creates
when you click a teammate — so `_delegate_targets` returned `[]`,
`_can_delegate` was False, `delegate_to_bot` was never advertised, and filing a
work item was the only thing the orchestrator *could* do. Every guard written
to catch a bot that files instead of handing over is gated on `_can_delegate`,
so all of them stayed correctly and uselessly silent.

The boundary is now the person's own team rather than the thread roster, and a
hand-off seats the recipient so their answer lands in the conversation the work
came from. What did *not* move is visibility: the candidate list is built
server-side from this person's own bots, so a slug a model invented still
cannot reach another tenant's — `test_delegation.py` holds that line.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select

from app.models import AuditEvent, Bot, Run, ThreadBot
from app.services.agent_work_items import TOOL_CREATE_WORK_ITEM
from app.services.orchestrator import TOOL_DELEGATE_TO_BOT, TOOL_TASK_COMPLETE, Orchestrator
from tests.services.conftest import acts, call


async def turn_as(orchestrator, db, user, thread, bot, content: str = "use Jordan and Sales"):
    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db, user=user, thread=thread, content=content, mention_bot_ids=[bot.id]
        )
    ]
    return frames, next((data for name, data in frames if name == "done"), {})


async def _seeded(db, slug: str) -> Bot:
    from decimal import Decimal

    bot = (await db.execute(select(Bot).where(Bot.slug == slug))).scalar_one()
    bot.daily_budget_usd = Decimal("500.00")
    await db.commit()
    return bot


async def _roster(db, thread_id) -> set[str]:
    rows = await db.execute(
        select(Bot.slug).join(ThreadBot, ThreadBot.bot_id == Bot.id).where(
            ThreadBot.thread_id == thread_id
        )
    )
    return set(rows.scalars().all())


async def _runs_for(db, bot: Bot) -> list[Run]:
    rows = await db.execute(select(Run).where(Run.bot_id == bot.id))
    return list(rows.scalars().all())


@pytest_asyncio.fixture
async def avery(make_user):
    return await make_user(email="avery@nesqualtech.test", display_name="Avery V")


@pytest_asyncio.fixture
async def cos(db):
    return await _seeded(db, "chief_of_staff")


@pytest_asyncio.fixture
async def lead_bot(db):
    return await _seeded(db, "lead_generator")


@pytest_asyncio.fixture
async def sales_bot(db):
    return await _seeded(db, "sales")


# ---------------------------------------------------------------------------
# The reported failure
# ---------------------------------------------------------------------------


async def test_a_one_to_one_thread_can_still_hand_work_over(
    agent_with, db, avery, make_thread, cos, lead_bot
):
    """The whole bug: one bot in the room used to mean nobody to delegate to."""
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="lead_generator", brief="Pull ten leads.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Ten leads, sources attached.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Jordan has the list.")),
        ]
    )
    thread = await make_thread(avery, [cos])
    assert await _roster(db, thread.id) == {"chief_of_staff"}

    _frames, done = await turn_as(orchestrator, db, avery, thread, cos)

    assert len(await _runs_for(db, lead_bot)) == 1, "the teammate was never started"
    assert "Jordan has the list" in done["message"]


async def test_the_teammate_is_seated_so_the_answer_lands_in_the_conversation(
    agent_with, db, avery, make_thread, cos, lead_bot
):
    """A delegated run posts under its own name — into a room it is now in.

    Seating on the hand-off rather than up front keeps the roster a record of
    who actually worked on something instead of a list of who might.
    """
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="lead_generator", brief="Pull ten leads.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Handed over.")),
        ]
    )
    thread = await make_thread(avery, [cos])

    await turn_as(orchestrator, db, avery, thread, cos)

    assert await _roster(db, thread.id) == {"chief_of_staff", "lead_generator"}


async def test_the_seating_is_audited_and_names_what_did_it(
    agent_with, db, avery, make_thread, cos, lead_bot
):
    """"Who added Sales to this thread" has one place to look, mention or hand-off."""
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="lead_generator", brief="Pull ten leads.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Handed over.")),
        ]
    )
    thread = await make_thread(avery, [cos])

    await turn_as(orchestrator, db, avery, thread, cos)

    rows = await db.execute(
        select(AuditEvent).where(AuditEvent.event_type == "thread_bot_seated")
    )
    events = [e for e in rows.scalars().all() if e.detail.get("bot_slug") == "lead_generator"]
    assert events, "seating a teammate mid-run left no audit trail"
    assert events[0].detail["via"] == "chief_of_staff"
    assert events[0].detail["thread_id"] == str(thread.id)


async def test_seating_twice_in_one_turn_is_not_an_error(
    agent_with, db, avery, make_thread, cos, lead_bot
):
    """Two briefs to the same teammate is ordinary. A composite primary key is
    the right place to settle that, not application logic."""
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="lead_generator", brief="Pull ten leads.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Ten.")),
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="lead_generator", brief="Now ten more.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Ten more.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Twenty in total.")),
        ]
    )
    thread = await make_thread(avery, [cos])

    _frames, done = await turn_as(orchestrator, db, avery, thread, cos)

    assert len(await _runs_for(db, lead_bot)) == 2
    assert await _roster(db, thread.id) == {"chief_of_staff", "lead_generator"}
    assert "Twenty in total" in done["message"]


async def test_the_prompt_names_the_team_and_says_the_call_brings_them_in(
    db, avery, make_thread, cos, lead_bot, sales_bot
):
    """A capability the model is not told about is one it will not use.

    The old block opened "These bots are on this thread with you", which on a
    one-to-one thread was both true and useless. It has to say that the call
    itself reaches them.
    """
    import uuid as _uuid

    from app.services.orchestrator import DelegationChain

    thread = await make_thread(avery, [cos])
    chain = DelegationChain(
        actor_user_id=avery.id,
        actor_label="avery",
        path=(cos.slug,),
        root_run_id=_uuid.uuid4(),
    )
    orchestrator = Orchestrator()

    targets = await orchestrator._delegate_targets(db, thread, cos, chain, avery)
    block = orchestrator._delegation_block(targets, chain)

    assert "lead_generator" in block and "sales" in block
    assert "your teammates" in block.lower()
    assert "the call brings them in" in block


# ---------------------------------------------------------------------------
# The guards that were silent are now live
# ---------------------------------------------------------------------------


async def test_filing_instead_of_delegating_is_now_caught_on_a_one_to_one(
    agent_with, db, avery, make_thread, cos, lead_bot, sales_bot
):
    """The close guard is gated on being *able* to delegate.

    On a single-bot thread it could never fire, which is why the reported turn
    filed two rows, reported that it had routed the work, and started nobody.
    """
    orchestrator = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_CREATE_WORK_ITEM,
                    type="lead",
                    title="Lead Generator: capture leads on LinkedIn",
                ),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Routed the work to Jordan and Sales.")),
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="lead_generator", brief="Capture ten leads.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Ten leads.")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Jordan captured ten; Sales is next.")),
        ]
    )
    thread = await make_thread(avery, [cos])

    _frames, done = await turn_as(orchestrator, db, avery, thread, cos)

    assert len(await _runs_for(db, lead_bot)) == 1, "the nudge did not turn filing into a hand-off"
    assert "Jordan captured ten" in done["message"]
