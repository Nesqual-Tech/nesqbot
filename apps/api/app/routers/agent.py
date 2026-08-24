"""Autonomous agent runs: the human-takeover handoff and the resume button.

An agent run that hits a login, an MFA prompt or a CAPTCHA does not fail and
does not hand the task back as a paragraph of advice. It parks itself in
``awaiting_human``, tells the UI why over SSE, and waits. The person finishes
that step on the live desktop stream and presses one button, which lands here.

Three properties this route has to have, and each one is a line of code you can
point at:

* **It survives a restart.** The resumable state is on ``runs.status`` and
  ``runs.detail``, not in a process. The button may be pressed an hour later,
  from a different device, against a different API replica.
* **It is authorised like everything else.** ``get_visible_run`` scopes a run to
  the human behind it and 404s otherwise, so a run is resumable only by its
  owner and its existence stays private.
* **It is idempotent.** The ``awaiting_human -> running`` transition is a single
  conditional ``UPDATE``. A double-click loses the race on the second press and
  gets ``resumed: false`` instead of a second loop on the same browser session.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.errors import AppError
from app.models import AuditEvent, Run, User
from app.routers.deps import get_visible_run, orchestrator
from app.schemas import CancelRunIn, CancelRunOut, ResumeRunIn, ResumeRunOut
from app.services.orchestrator import (
    RUN_AGENT_KEY,
    RUN_AWAITING_APPROVAL,
    RUN_AWAITING_HUMAN,
)

logger = logging.getLogger("nesqbot.agent")

router = APIRouter(tags=["agent"])


@router.post("/runs/{run_id}/resume", response_model=ResumeRunOut)
async def resume_run(
    run_id: uuid.UUID,
    body: ResumeRunIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ResumeRunOut:
    """"I've finished, continue" — pick the same task back up on the same screen.

    Returns ``resumed: false`` rather than an error when the run has already been
    resumed or has moved on: that is what a double-click looks like, and failing
    it would train people to press the button twice.
    """
    run = await get_visible_run(db, run_id, user)

    if not (run.detail or {}).get(RUN_AGENT_KEY):
        raise AppError(
            409,
            "run_not_resumable",
            "This run never asked for a human to take over, so there is nothing to resume.",
        )

    # The claim. Conditional on the status in the database, not on the copy this
    # request loaded, so two simultaneous presses cannot both win it.
    claimed = await db.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == RUN_AWAITING_HUMAN)
        .values(status="running", updated_at=datetime.now(timezone.utc))
        .returning(Run.id)
    )
    if claimed.scalar_one_or_none() is None:
        # Read the status back as a scalar rather than through the ORM object
        # loaded above: the point of the conditional UPDATE is that the database
        # is the authority on whether this press won, so the answer has to come
        # from the database too.
        current = (
            await db.execute(select(Run.status).where(Run.id == run_id))
        ).scalar_one_or_none() or "gone"
        logger.info("resume for run %s ignored: status is %s", run_id, current)
        return ResumeRunOut(
            ok=True,
            resumed=False,
            run_id=run_id,
            status=current,
            detail=(
                f"This run is '{current}', not waiting for a human. Nothing was started."
            ),
        )
    # Commit the claim before the loop runs: it may take minutes, and the
    # transition has to be visible to a second press immediately.
    await db.commit()
    await db.refresh(run)

    out = await orchestrator.resume_run(db, user=user, run=run, note=body.note)
    if not out.get("ok"):
        raise AppError(
            409,
            "run_not_resumable",
            str(out.get("detail") or "This run could not be resumed."),
        )

    approval_id = out.get("approval_id")
    return ResumeRunOut(
        ok=True,
        resumed=True,
        run_id=run_id,
        status=str(out.get("status") or run.status),
        thread_id=uuid.UUID(out["thread_id"]) if out.get("thread_id") else None,
        bot_id=uuid.UUID(out["bot_id"]) if out.get("bot_id") else None,
        message_id=uuid.UUID(out["message_id"]) if out.get("message_id") else None,
        message=out.get("message"),
        outcome=out.get("outcome"),
        approval_id=uuid.UUID(approval_id) if approval_id else None,
        cost_usd=out.get("cost_usd"),
    )


#: Statuses a run can be cancelled out of. Anything else has already stopped.
CANCELLABLE = ("queued", "running", RUN_AWAITING_HUMAN, RUN_AWAITING_APPROVAL)


@router.post("/runs/{run_id}/cancel", response_model=CancelRunOut)
async def cancel_run(
    run_id: uuid.UUID,
    body: CancelRunIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CancelRunOut:
    """Abandon a run that is not going to finish, and unblock the person.

    This exists because there was no way out. A run whose process died mid-step
    stays `running` for ever — nothing reconciles it — and a run parked in
    `awaiting_approval` whose approval was already decided waits for something
    that no longer exists. Both leave the UI showing work in progress that will
    never progress, and until now the only escape was a database edit. The
    product owner's words were "sometimes it gets blocked and we can't do
    anything", and they were exactly right.

    Deliberately permissive about *which* statuses it accepts. A cancel button
    that refuses because the run is in a state nobody anticipated is the same
    dead end wearing a different error message.

    Idempotent by the same conditional-UPDATE trick as `resume`, so a
    double-click gets `cancelled: false` rather than a second audit row.
    """
    run = await get_visible_run(db, run_id, user)

    claimed = await db.execute(
        update(Run)
        .where(Run.id == run_id, Run.status.in_(CANCELLABLE))
        .values(
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
            error=(body.reason or "Cancelled by the person who owns this run."),
        )
        .returning(Run.id)
    )
    if claimed.scalar_one_or_none() is None:
        await db.refresh(run)
        return CancelRunOut(
            ok=True,
            cancelled=False,
            run_id=run_id,
            status=run.status,
            detail=f"This run is already '{run.status}'. Nothing was changed.",
        )

    db.add(
        AuditEvent(
            bot_id=run.bot_id,
            actor_user_id=user.id,
            event_type="run_cancelled",
            detail={
                "run_id": str(run_id),
                "reason": body.reason or "",
                "from_status": run.status,
            },
        )
    )
    await db.commit()
    logger.info("run %s cancelled by %s (was %s)", run_id, user.id, run.status)

    return CancelRunOut(ok=True, cancelled=True, run_id=run_id, status="cancelled")
