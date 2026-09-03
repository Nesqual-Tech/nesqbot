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
* **It answers immediately.** The route claims the run and hands the loop to
  ``services.background``; it does not drive the agent inside the request. It
  used to, and the reported result was *"the button that i am done it just keeps
  loading and it does nothing else so the task remains like that hanging"* —
  minutes of spinner, and a client that disconnected first took the loop down
  with it and left the run ``running`` with nothing driving it. See
  ``services/background.py``.
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
from app.services import background
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

    ``resumed: true`` means **started**, not finished. The agent may work for
    minutes; it does that on its own task, and what it does appears in the
    thread over SSE exactly as a chat turn would. The alternative — holding the
    response open until the loop ends — is what this route used to do, and it
    made the button look broken and left runs hanging when the connection went
    away mid-loop.
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
    # Commit the claim before returning: the transition has to be visible to a
    # second press immediately, and to the task about to pick it up.
    await db.commit()
    await db.refresh(run)

    thread_id = run.thread_id
    bot_id = run.bot_id
    user_id = user.id
    note = body.note

    async def _carry_on(session: AsyncSession) -> None:
        """The loop, on its own session and after the response.

        Everything is re-loaded here rather than closed over: the objects above
        belong to the request's session, which is gone by the time this runs.
        """
        fresh_run = await session.get(Run, run_id)
        fresh_user = await session.get(User, user_id)
        if fresh_run is None or fresh_user is None:
            logger.warning("resume %s: run or user vanished before the loop started", run_id)
            return
        out = await orchestrator.resume_run(
            session, user=fresh_user, run=fresh_run, note=note
        )
        if not out.get("ok"):
            # `resume_run` reports this rather than raising, and there is no
            # response left to attach it to. It has already put the run into a
            # settled state; this is the line that says why.
            logger.warning("resume %s did not run: %s", run_id, out.get("detail"))

    background.run_detached(_carry_on, label=f"resume:{run_id}")

    return ResumeRunOut(
        ok=True,
        resumed=True,
        run_id=run_id,
        status="running",
        thread_id=thread_id,
        bot_id=bot_id,
        detail=(
            "Picking the task back up now. What it does will appear in the thread."
        ),
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
