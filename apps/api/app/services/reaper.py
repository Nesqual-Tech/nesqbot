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

from app.models import AuditEvent, Run

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
        select(Run.id, Run.bot_id, Run.status).where(
            Run.status.in_(ACTIVE_STATUSES),
            or_(Run.updated_at < cutoff, Run.updated_at.is_(None)),
        )
    )
    rows = found.all()
    if not rows:
        return []

    claimed: list[str] = []
    for run_id, bot_id, was in rows:
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
        claimed.append(str(run_id))

    await db.commit()
    if claimed:
        logger.warning(
            "reaped %d run(s) with no live process: %s", len(claimed), ", ".join(claimed)
        )
    return claimed
