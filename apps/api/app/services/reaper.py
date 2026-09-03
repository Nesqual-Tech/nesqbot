"""Reclaim runs whose process is gone.

An agent run lives in an asyncio task inside one API container. The row that
represents it lives in Postgres. When the container goes away — a deploy, a
scale-in, a crash — the task dies and the row does not, so the run stays
`running` for ever and the UI shows work in progress that will never progress.
Eight such rows were found in the live database, one over a day old, and the
product owner's report was "sometimes it gets blocked and we can't do anything".

Two rules keep this safe with more than one replica:

* **Only age decides.** Never "this process did not start it, so it is dead" —
  with `maxReplicas: 3` that would have one container reaping another's live
  work. `updated_at` is touched whenever the run row is written, which the loop
  does as it goes, so a genuinely active run keeps refreshing it.
* **The threshold is generous.** A desktop cold start alone is up to 180s and a
  long browse is minutes; the cost of reaping late is a stale row for an hour,
  and the cost of reaping early is killing work someone is waiting on. Those are
  not symmetric, so the default is deliberately loose.

`cancelled` is for a person's decision. This writes `interrupted`, which is a
different fact: nobody chose to stop it, we simply cannot prove it is alive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Bot, Message, Run, Thread

logger = logging.getLogger("nesqbot.reaper")

#: Statuses that mean "a process should be working on this right now".
#:
#: `awaiting_human` and `awaiting_approval` are deliberately NOT here: those are
#: parked on a person, and a person is allowed to take a week. Reaping them would
#: turn "I went to lunch" into a lost task.
ACTIVE_STATUSES = ("running", "queued")

#: How long a run may go without its row being written before it is presumed dead.
STALE_AFTER = timedelta(minutes=45)

INTERRUPTED_MESSAGE = (
    "This run stopped without finishing — the process running it went away, "
    "most likely a deploy or a restart. Nothing was left half-done that needs "
    "undoing: any action that had already run is in the audit log. Start it again "
    "when you are ready."
)


async def reap_orphaned_runs(
    db: AsyncSession, *, stale_after: timedelta = STALE_AFTER, now: datetime | None = None
) -> list[str]:
    """Mark presumed-dead runs `interrupted`. Returns the ids it claimed."""
    cutoff = (now or datetime.now(timezone.utc)) - stale_after

    found = await db.execute(
        select(Run.id, Run.bot_id, Run.status, Run.thread_id).where(
            Run.status.in_(ACTIVE_STATUSES),
            or_(Run.updated_at < cutoff, Run.updated_at.is_(None)),
        )
    )
    rows = found.all()
    if not rows:
        return []

    claimed: list[str] = []
    for run_id, bot_id, was, thread_id in rows:
        # One conditional UPDATE per run rather than one sweeping statement: two
        # replicas booting together would otherwise both "reap" the same rows and
        # both write an audit event. The status predicate makes the loser a no-op.
        result = await db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.in_(ACTIVE_STATUSES))
            .values(
                status="interrupted",
                finished_at=datetime.now(timezone.utc),
                error=INTERRUPTED_MESSAGE,
            )
            .returning(Run.id)
        )
        if result.scalar_one_or_none() is None:
            continue
        db.add(
            AuditEvent(
                bot_id=bot_id,
                actor_user_id=None,
                event_type="run_interrupted",
                detail={"run_id": str(run_id), "from_status": was, "reason": "process_gone"},
            )
        )
        await _say_so_in_the_thread(db, run_id=run_id, bot_id=bot_id, thread_id=thread_id)
        claimed.append(str(run_id))

    await db.commit()
    if claimed:
        logger.warning(
            "reaped %d run(s) with no live process: %s", len(claimed), ", ".join(claimed)
        )
    return claimed


async def _say_so_in_the_thread(
    db: AsyncSession, *, run_id, bot_id, thread_id
) -> None:
    """Tell the person, in the thread they are looking at, that it stopped.

    The gap this closes, measured on the live database: a user message written
    at 18:14:07, its run still `running` with no reply, and a second run from
    15:43 in the same state. The report was *"I messaged and nothing happened
    at all"* — which was exactly true. An assistant message is only written when
    a turn *finishes*, so a turn killed by a deploy, a scale-in or a stall
    leaves the transcript holding the person's own message and nothing else,
    for ever. Marking the row `interrupted` fixed the *bookkeeping* and changed
    nothing about what the person could see.

    So the reap writes the sentence the dead turn never got to write. It says
    what happened, that nothing more is coming, and what to do — which is the
    difference between a product that failed and a product that appears to
    ignore you.

    Skipped for a run with no thread (a routine, an inbound handler): there is
    nowhere to say it and nobody watching a transcript for it. Failures here are
    swallowed on purpose — this is the reaper's courtesy, not its job, and a
    thread that has since been deleted must not stop the run being reclaimed.
    """
    if thread_id is None:
        return
    try:
        thread = await db.get(Thread, thread_id)
        if thread is None:
            return
        bot = await db.get(Bot, bot_id) if bot_id else None
        who = bot.name if bot is not None else "The bot"
        db.add(
            Message(
                thread_id=thread_id,
                bot_id=bot_id,
                role="assistant",
                content=(
                    f"**{who} stopped mid-task and nothing more is coming.** The turn was "
                    "still in flight when its process went away — a deploy, a restart, or "
                    "a step that stalled — so whatever it had done is not written down "
                    "here and no work is continuing in the background.\n\n"
                    "Send the message again to start it over. If this keeps happening on "
                    "the same request, say so: a turn that dies in the same place twice is "
                    "a bug worth chasing rather than bad luck."
                ),
                meta={"interrupted_run_id": str(run_id), "reason": "process_gone"},
            )
        )
        thread.updated_at = datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001 - never let the courtesy block the reclaim
        logger.warning("could not post an interruption note for run %s", run_id, exc_info=True)
