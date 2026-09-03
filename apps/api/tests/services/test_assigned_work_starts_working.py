"""An assigned work item starts the bot that owns it.

The complaint, verbatim:

    "if i ask the chief of staff to delegate some work to some agents it will
    be like this: chief will create work items and tasks, tasks will be
    assigned to the actual bots that need to and start fucking working. we are
    at version fucking 19.0 and we are in a worse position than 0.3.0"

That is the architecture, and the product had every part of it except the last
clause. `create_work_item` wrote the row. `transfer_work_item` moved the owner
and recorded who, to whom and why. And then nothing told the new owner, so a
chief of staff that decomposed a month-long goal into assigned items had
started nobody — while reporting, accurately by its own lights, that it had
routed the work.

`services.work_dispatch` closes it: an item that is `open`, owned, and whose
owner has not been woken is a queue of one row, drained on a timer, each item
run through the same `Orchestrator.handle_user_message` door a person's message
uses.

What these tests hold down:

* the queue predicate and the four things that must keep it out — no owner,
  already dispatched, `waiting`, `closed`;
* that claiming stamps *before* the model call, so a crash cannot double-run
  somebody's real pipeline;
* that a dispatch seats the owner, posts the instruction, and starts exactly
  one run;
* that a transfer re-queues, because a new owner has not been told either;
* that a batch is bounded, because fourteen items must not become fourteen
  concurrent runs on one rate-limited account.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy import select

from app.models import AuditEvent, Message, Run, ThreadBot, WorkItem
from app.services import work_dispatch


@pytest_asyncio.fixture
async def avery(make_user):
    return await make_user(email="avery@nesqualtech.test", display_name="Avery V")


async def _item(db, *, user, bot, thread=None, status="open", title="Acme") -> WorkItem:
    item = WorkItem(
        type="lead",
        title=title,
        summary="Ten qualified accounts, sources attached.",
        owner_bot_id=bot.id,
        owner_user_id=user.id,
        thread_id=thread.id if thread is not None else None,
        status=status,
        detail={},
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


async def test_an_open_owned_item_is_claimed(db, avery, make_bot):
    bot = await make_bot(avery, name="Jordan", slug="jordan_q1")
    item = await _item(db, user=avery, bot=bot)

    claimed = await work_dispatch.claim_pending(db)

    assert [c.id for c in claimed] == [item.id]


async def test_claiming_stamps_before_anything_runs(db, avery, make_bot):
    """Stamp on claim, not on success.

    A crash between claiming and running leaves an item that *looks* started
    and can be re-assigned by hand. Stamping on success instead would turn one
    slow run into two runs on the same row, and duplicated agent work on
    somebody's real pipeline is the worse of the two failures.
    """
    bot = await make_bot(avery, name="Jordan", slug="jordan_q2")
    item = await _item(db, user=avery, bot=bot)

    await work_dispatch.claim_pending(db)

    await db.refresh(item)
    assert item.dispatched_at is not None
    assert item.status == "working"
    assert await work_dispatch.claim_pending(db) == [], "a claimed item was claimable again"


async def test_an_unowned_item_is_never_claimed(db, avery, make_bot):
    """"These leads have no bot" is a real state a person fixes with a transfer."""
    bot = await make_bot(avery, name="Jordan", slug="jordan_q3")
    item = await _item(db, user=avery, bot=bot)
    item.owner_bot_id = None
    await db.commit()

    assert await work_dispatch.claim_pending(db) == []


async def test_waiting_and_closed_items_are_never_claimed(db, avery, make_bot):
    """`waiting` means it is with somebody outside. Waking a bot to stare at a
    row nobody has replied to yet is how a budget goes on nothing."""
    bot = await make_bot(avery, name="Jordan", slug="jordan_q4")
    await _item(db, user=avery, bot=bot, status="waiting", title="Waiting")
    await _item(db, user=avery, bot=bot, status="closed", title="Closed")

    assert await work_dispatch.claim_pending(db) == []


async def test_a_batch_is_bounded(db, avery, make_bot):
    """A quarter decomposed into fourteen items is not a request for fourteen
    concurrent runs on one rate-limited account. The rest wait one interval."""
    bot = await make_bot(avery, name="Jordan", slug="jordan_q5")
    for index in range(work_dispatch.DISPATCH_BATCH + 2):
        await _item(db, user=avery, bot=bot, title=f"Item {index}")

    claimed = await work_dispatch.claim_pending(db)

    assert len(claimed) == work_dispatch.DISPATCH_BATCH


async def test_the_oldest_item_goes_first(db, avery, make_bot):
    bot = await make_bot(avery, name="Jordan", slug="jordan_q6")
    first = await _item(db, user=avery, bot=bot, title="First")
    second = await _item(db, user=avery, bot=bot, title="Second")
    first.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await db.commit()

    claimed = await work_dispatch.claim_pending(db, limit=1)

    assert [c.id for c in claimed] == [first.id]
    assert second.id not in {c.id for c in claimed}


# ---------------------------------------------------------------------------
# The wake
# ---------------------------------------------------------------------------


async def test_dispatching_runs_the_owner_once_and_seats_it(
    agent_with, db, avery, make_thread, make_bot
):
    """The whole point, end to end: an assigned row becomes a run by that bot."""
    from tests.services.conftest import acts, call

    chief = await make_bot(avery, name="Chief", slug="chief_w1", daily_budget_usd=500.0)
    jordan = await make_bot(avery, name="Jordan", slug="jordan_w1", daily_budget_usd=500.0)
    thread = await make_thread(avery, [chief])
    item = await _item(db, user=avery, bot=jordan, thread=thread)

    orchestrator = agent_with([acts("", call("task_complete", summary="Ten accounts found."))])
    monkey = work_dispatch.Orchestrator if hasattr(work_dispatch, "Orchestrator") else None
    assert monkey is None, "work_dispatch must import Orchestrator lazily"

    import app.services.orchestrator as orch_module

    original = orch_module.Orchestrator
    orch_module.Orchestrator = lambda: orchestrator  # type: ignore[assignment]
    try:
        started = await work_dispatch.dispatch_pending(db)
    finally:
        orch_module.Orchestrator = original  # type: ignore[assignment]

    assert len(started) == 1
    assert started[0].bot_slug == "jordan_w1"
    assert started[0].error is None

    runs = (await db.execute(select(Run).where(Run.bot_id == jordan.id))).scalars().all()
    assert len(runs) == 1, "the owner was not run exactly once"

    seated = (
        await db.execute(select(ThreadBot.bot_id).where(ThreadBot.thread_id == thread.id))
    ).scalars().all()
    assert jordan.id in set(seated), "the owner worked a thread it was not on"

    messages = (
        await db.execute(select(Message).where(Message.thread_id == thread.id))
    ).scalars().all()
    wake = [m for m in messages if (m.meta or {}).get("work_item_dispatch")]
    assert wake, "nothing in the thread says why the bot started"
    assert str(item.id) in wake[0].meta["work_item_id"]

    audits = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "work_item_dispatched")
        )
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].detail["work_item_id"] == str(item.id)


async def test_an_item_with_no_thread_gets_one(agent_with, db, avery, make_bot):
    """A routine or an inbound reply files items with no conversation at all.
    The bot still has to have somewhere to report."""
    from tests.services.conftest import acts, call

    jordan = await make_bot(avery, name="Jordan", slug="jordan_w2", daily_budget_usd=500.0)
    item = await _item(db, user=avery, bot=jordan, thread=None)

    import app.services.orchestrator as orch_module

    original = orch_module.Orchestrator
    orch_module.Orchestrator = lambda: agent_with(
        [acts("", call("task_complete", summary="Done."))]
    )  # type: ignore[assignment]
    try:
        started = await work_dispatch.dispatch_pending(db)
    finally:
        orch_module.Orchestrator = original  # type: ignore[assignment]

    assert len(started) == 1
    await db.refresh(item)
    assert item.thread_id is not None
    assert started[0].thread_id == item.thread_id


async def test_an_owner_that_cannot_be_resolved_is_not_dispatched(db, avery):
    """The defensive branch, asserted directly.

    `work_items.owner_bot_id` is `ON DELETE SET NULL`, so a deleted bot leaves
    the item *unowned* rather than pointing at a ghost — which the queue
    already skips (see above). The branch still exists because `dispatch` is
    reachable from a timer that read its row seconds earlier, and returning
    None there is what stops a `NoneType` from becoming a stack trace in the
    dispatcher loop every fifteen seconds.
    """
    orphan = WorkItem(
        type="lead",
        title="Nobody owns this",
        owner_bot_id=uuid.uuid4(),
        owner_user_id=avery.id,
        status="open",
        detail={},
    )

    assert await work_dispatch.dispatch(db, orphan) is None


# ---------------------------------------------------------------------------
# Re-queueing
# ---------------------------------------------------------------------------


async def test_mark_for_dispatch_puts_a_worked_item_back_in_the_queue(
    db, avery, make_bot
):
    """A transfer means a new owner, and a new owner has not been told either."""
    jordan = await make_bot(avery, name="Jordan", slug="jordan_r1")
    item = await _item(db, user=avery, bot=jordan)
    await work_dispatch.claim_pending(db)
    await db.refresh(item)
    assert item.status == "working"

    work_dispatch.mark_for_dispatch(item, assigned_by="chief_of_staff")
    await db.commit()

    assert item.dispatched_at is None
    assert item.status == "open"
    assert item.detail["assigned_by"] == "chief_of_staff"
    claimed = await work_dispatch.claim_pending(db)
    assert [c.id for c in claimed] == [item.id]


async def test_a_closed_item_is_not_reopened_by_being_reassigned(db, avery, make_bot):
    """Reassigning a closed item is bookkeeping. Reopening it would be a
    decision, and not one a transfer gets to make."""
    jordan = await make_bot(avery, name="Jordan", slug="jordan_r2")
    item = await _item(db, user=avery, bot=jordan, status="closed")

    work_dispatch.mark_for_dispatch(item)
    await db.commit()

    assert item.status == "closed"
    assert await work_dispatch.claim_pending(db) == []


# ---------------------------------------------------------------------------
# What the bot is told
# ---------------------------------------------------------------------------


def test_the_wake_instruction_says_to_do_the_work_not_describe_it():
    item = WorkItem(
        type="lead",
        title="Acme Ltd",
        summary="Hiring their first sales rep.",
        status="open",
        detail={},
    )

    text = work_dispatch.wake_instruction(item, assigned_by="chief_of_staff")

    assert "Acme Ltd" in text
    assert "Hiring their first sales rep." in text
    assert "chief_of_staff assigned it to you" in text
    # The three failures this wording is aimed at, each asserted so a rewrite
    # cannot quietly drop one: describing instead of doing, asking a question
    # into an empty room, and leaving the item in whatever state it was found.
    assert "Do the work itself" in text
    assert "nobody is waiting on this reply" in text
    assert "update the item" in text
