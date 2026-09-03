"""Assigned work starts working.

The complaint this module exists for, in the owner's words:

    "if i ask the chief of staff to delegate some work to some agents it will
    be like this: chief will create work items and tasks, tasks will be
    assigned to the actual bots that need to and start fucking working."

That is the right architecture and the product did not have it. A work item was
a record: `owner_bot_id` named a bot, and nothing ever told that bot. So a
chief of staff which decomposed a month-long goal into items and assigned them
had — accurately, and while reporting that it had "routed the work" — started
nobody. The prompt even said so out loud ("filing a work item does not reach
them"), which made the honest failure a documented one rather than a fixed one.

Synchronous delegation (`orchestrator._delegate`) is the other half and not a
substitute for this one. A hand-off runs inside the parent's turn: the person
waits, the chain is capped at three hops and a hundred and eighty seconds, and
nothing survives the request. A month-long goal needs the opposite properties —
durable, resumable, and indifferent to whether anybody is watching. So:

    chief files an item and assigns it   ->  one row, `dispatched_at IS NULL`
    the dispatcher claims it             ->  status `working`, run started
    the owner works it in its own run    ->  updates the item, reports in-thread
    the item ends up `waiting`/`closed`  ->  nothing re-wakes it

The queue is the table. There is no broker and no second datastore: "owned,
open, and not yet woken" is a partial index away, and a row that is claimed but
whose process dies is visible as `working` with a `dispatch_run_id` the run
reaper has already marked failed. Restart-safe by construction, which a
fire-and-forget `asyncio.create_task` inside a request handler is not.

`FOR UPDATE SKIP LOCKED` is what makes more than one API replica safe: two
dispatchers claim disjoint rows, and neither waits on the other.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Bot, Message, Thread, ThreadBot, User, WorkItem
from app.services import work_items as work_items_service

logger = logging.getLogger("nesqbot.work_dispatch")

#: How many items one pass may start. A chief of staff decomposing a quarter
#: into fourteen items should not turn into fourteen concurrent agent runs on
#: the same account inside one interval — the reason tier is rate limited and
#: every run costs the bot's own daily budget. The rest wait for the next pass,
#: which is seconds away.
DISPATCH_BATCH = 3


@dataclass(frozen=True)
class Dispatched:
    """One item that was started, for the log line and the tests."""

    work_item_id: uuid.UUID
    bot_slug: str
    thread_id: uuid.UUID
    run_id: str | None
    error: str | None = None


def wake_instruction(item: WorkItem, *, assigned_by: str | None) -> str:
    """What the owning bot is told when it is woken about an item.

    In this system's voice and deliberately short: the item itself carries the
    detail, and `find_work_items` is one call away if the bot wants the rest.

    Every sentence here answers a failure seen in production. "It is yours now"
    because a bot woken with a description tends to comment on it. "Do the work
    itself" because the previous behaviour of the whole product was to describe
    work instead of doing it. "Nobody is waiting on this reply" because the
    same bots ask clarifying questions into an empty room. And the closing
    instruction to update the item is what makes the next state transition
    happen at all — an item left `working` forever is the failure mode this
    design can actually have.
    """
    title = (item.title or "").strip()[:200]
    summary = (item.summary or "").strip()[:600]
    who = f" {assigned_by} assigned it to you." if assigned_by else ""
    return (
        f"You have been given a work item: {title}."
        f"{who} It is yours now.\n\n"
        f"{summary}\n\n"
        "Do the work itself, in this run, with the tools you have — do not reply with a "
        "plan and do not ask what to do next; nobody is waiting on this reply. When you "
        "have got as far as you can, update the item: `waiting` if it is now with "
        "somebody outside, `closed` with a resolution if it is done, and leave a summary "
        "on it that the next reader can act on. If part of it is somebody else's job, "
        "hand that part over."
    )


async def _thread_for(db: AsyncSession, item: WorkItem, user: User) -> Thread:
    """Where this item's work gets discussed.

    Reuses `item.thread_id` when it has one — the conversation the item was
    filed in is where the person is already looking. Only creates a thread when
    the item has none, which is the routine/inbound case rather than the
    delegation one.
    """
    if item.thread_id is not None:
        thread = await db.get(Thread, item.thread_id)
        if thread is not None:
            return thread
    thread = Thread(title=(item.title or "Work")[:200], owner_user_id=user.id)
    db.add(thread)
    await db.flush()
    item.thread_id = thread.id
    return thread


async def _seat(db: AsyncSession, thread: Thread, bot: Bot) -> None:
    """Put the owner on the thread so its work is visible where it happens.

    `ON CONFLICT DO NOTHING` for the same reason `orchestrator._seat_bot` uses
    it: the row may already be there and a composite primary key is the right
    place to settle that.
    """
    await db.execute(
        pg_insert(ThreadBot)
        .values(thread_id=thread.id, bot_id=bot.id)
        .on_conflict_do_nothing(index_elements=["thread_id", "bot_id"])
    )


async def claim_pending(db: AsyncSession, limit: int = DISPATCH_BATCH) -> list[WorkItem]:
    """Take up to `limit` items that have an owner and have not been woken.

    Claiming is the UPDATE, not the SELECT: `dispatched_at` is stamped here,
    before the model call, so a crash between claim and run leaves an item that
    looks started rather than one that gets started again on the next pass. The
    alternative — stamp on success — turns one slow run into a duplicate run,
    and duplicated agent work on somebody's real pipeline is worse than a
    stalled row a person can see and re-assign.
    """
    rows = await db.execute(
        select(WorkItem)
        .where(
            WorkItem.dispatched_at.is_(None),
            WorkItem.owner_bot_id.is_not(None),
            WorkItem.status == "open",
        )
        .order_by(WorkItem.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    items = list(rows.scalars().all())
    now = datetime.now(timezone.utc)
    for item in items:
        item.dispatched_at = now
        work_items_service.apply_status(item, "working")
        db.add(item)
    if items:
        await db.commit()
    return items


async def dispatch(db: AsyncSession, item: WorkItem) -> Dispatched | None:
    """Wake the bot that owns `item` and let it work.

    Returns None when the item cannot be dispatched at all — a deleted owner, a
    deleted user — which is a state a person has to fix with a transfer and not
    something to retry every twenty seconds.

    The run is started through `Orchestrator.handle_user_message`, the same door
    an inbound email uses (`services.inbound`) and the same door the app uses.
    That is deliberate: a woken bot gets the identical loop, tool set, risk gate
    and approval behaviour as one a person messaged, so there is no second,
    less-guarded path into the agent.
    """
    # Imported here rather than at module scope: `services.orchestrator`
    # imports `agent_work_items`, which would close a cycle through this
    # module's triggers.
    from app.services.orchestrator import Orchestrator

    bot = await db.get(Bot, item.owner_bot_id) if item.owner_bot_id else None
    user = await db.get(User, item.owner_user_id)
    if bot is None or user is None:
        logger.warning(
            "work item %s cannot be dispatched: owner bot or user is gone", item.id
        )
        return None

    assigned_by = str((item.detail or {}).get("assigned_by") or "") or None
    thread = await _thread_for(db, item, user)
    await _seat(db, thread, bot)
    db.add(
        Message(
            thread_id=thread.id,
            user_id=None,
            bot_id=None,
            role="user",
            content=wake_instruction(item, assigned_by=assigned_by),
            meta={
                "work_item_id": str(item.id),
                "work_item_dispatch": True,
                "assigned_by": assigned_by,
            },
        )
    )
    await db.commit()

    run_id: str | None = None
    error: str | None = None
    try:
        # `mention_bot_ids` pins the answer to the owner. Without it
        # `_select_bot` would route on words found in the item's own title,
        # which is how "Sales: close the Acme deal" gets answered by the bot
        # whose name is in the title rather than by the bot that owns the row.
        out = await Orchestrator().handle_user_message(
            db,
            user=user,
            thread=thread,
            content=wake_instruction(item, assigned_by=assigned_by),
            mention_bot_ids=[bot.id],
        )
        run_id = out.get("run_id")
        error = out.get("error")
    except Exception as exc:  # noqa: BLE001 - a failed wake is an event, not a 500
        logger.exception("work item %s failed to wake %s", item.id, bot.slug)
        error = str(exc)

    fresh = await db.get(WorkItem, item.id)
    if fresh is not None:
        fresh.dispatch_run_id = uuid.UUID(run_id) if run_id else None
        if error:
            fresh.detail = {**(fresh.detail or {}), "dispatch_error": str(error)[:500]}
        db.add(fresh)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot.id,
            event_type="work_item_dispatched",
            detail={
                "work_item_id": str(item.id),
                "thread_id": str(thread.id),
                "run_id": run_id,
                "assigned_by": assigned_by,
                "ok": error is None,
            },
        )
    )
    await db.commit()
    return Dispatched(
        work_item_id=item.id,
        bot_slug=bot.slug,
        thread_id=thread.id,
        run_id=run_id,
        error=error,
    )


async def dispatch_pending(db: AsyncSession, limit: int = DISPATCH_BATCH) -> list[Dispatched]:
    """One pass: claim what is owed a bot, then run each one.

    Claim first and in one transaction, then dispatch one at a time. Doing it
    the other way — a long model call inside the transaction that holds the
    claim — is the `idle_in_transaction_session_timeout` failure this codebase
    has already been bitten by once (see `db.release_transaction`).
    """
    items = await claim_pending(db, limit)
    started: list[Dispatched] = []
    for item in items:
        result = await dispatch(db, item)
        if result is not None:
            started.append(result)
    return started


def mark_for_dispatch(item: WorkItem, *, assigned_by: str | None = None) -> None:
    """Say that `item`'s owner has not been woken about it yet.

    Called by every path that sets or changes an owner — the two agent tools,
    and `POST /work-items` / the transfer route. Deliberately a two-line helper
    rather than four copies of the same two lines: an assignment that forgets to
    null `dispatched_at` is an assignment that silently does nothing, which is
    precisely the bug this module exists to end.

    `status` is put back to `open` because that is what the dispatcher's queue
    selects on, and because an item being reassigned is by definition not being
    worked by its previous owner any more.
    """
    item.dispatched_at = None
    item.dispatch_run_id = None
    if item.status not in work_items_service.TERMINAL_STATUSES:
        work_items_service.apply_status(item, "open")
    if assigned_by:
        item.detail = {**(item.detail or {}), "assigned_by": assigned_by}
