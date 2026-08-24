"""Routine CRUD, Temporal schedule sync, manual runs, and run history."""

from __future__ import annotations

import inspect
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.errors import AppError
from app.models import AuditEvent, Bot, Routine, Run, User
from app.routers.deps import (
    bot_visibility_clause,
    get_visible_bot,
    get_visible_routine,
    optional_service,
)
from app.schemas import (
    OkOut,
    RoutineIn,
    RoutineOut,
    RoutineRunOut,
    RunOut,
    TeachRoutineIn,
    UpdateRoutineIn,
)

logger = logging.getLogger("nesqbot.routines")

router = APIRouter(tags=["routines"])


def _temporal():
    return optional_service("temporal_client")


def _accepts(fn: Any, name: str) -> bool:
    """True when ``fn`` takes a keyword argument called ``name``.

    The services lane is still widening some signatures; probing keeps this lane
    from TypeError-ing on a build where the parameter has not landed yet.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _sync_schedule(routine: Routine) -> str | None:
    """Best-effort Temporal schedule sync; never fails the HTTP request."""
    temporal = _temporal()
    fn = getattr(temporal, "sync_routine_schedule", None) if temporal else None
    if fn is None:
        return None
    try:
        return await fn(routine)
    except Exception:  # noqa: BLE001 - Temporal is optional locally
        logger.warning("temporal schedule sync failed for routine %s", routine.id, exc_info=True)
        return None


async def _delete_schedule(routine_id: uuid.UUID) -> None:
    temporal = _temporal()
    fn = getattr(temporal, "delete_routine_schedule", None) if temporal else None
    if fn is None:
        return
    try:
        await fn(routine_id)
    except Exception:  # noqa: BLE001
        logger.warning("temporal schedule delete failed for routine %s", routine_id, exc_info=True)


@router.get("/routines", response_model=list[RoutineOut])
async def list_routines(
    bot_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Routine]:
    stmt = (
        select(Routine)
        .join(Bot, Bot.id == Routine.bot_id)
        .where(bot_visibility_clause(user))
        .order_by(Routine.created_at.desc())
    )
    if bot_id is not None:
        await get_visible_bot(db, bot_id, user)
        stmt = stmt.where(Routine.bot_id == bot_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/routines", response_model=RoutineOut)
async def create_routine(
    body: RoutineIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Routine:
    await get_visible_bot(db, body.bot_id, user)
    row = Routine(
        bot_id=body.bot_id,
        name=body.name,
        description=body.description,
        steps=body.steps,
        schedule_cron=body.schedule_cron,
        owner_user_id=user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    if row.schedule_cron:
        await _sync_schedule(row)
    return row


@router.post("/routines/teach", response_model=RoutineOut)
async def teach_routine(
    body: TeachRoutineIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Routine:
    """Convert recorded Bot Desktop steps into a versioned routine."""
    await get_visible_bot(db, body.bot_id, user)
    steps = [
        {
            "type": s.get("type", "desktop"),
            "action": s.get("action", "click"),
            "args": {k: v for k, v in s.items() if k not in ("type", "action")},
        }
        for s in body.recorded_steps
    ]
    row = Routine(
        bot_id=body.bot_id,
        name=body.name,
        description=body.description or "Taught by demonstration",
        steps=steps,
        schedule_cron=body.schedule_cron,
        version=1,
        owner_user_id=user.id,
    )
    db.add(row)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=body.bot_id,
            event_type="routine_taught",
            detail={"name": body.name, "steps": len(steps)},
        )
    )
    await db.commit()
    await db.refresh(row)
    if row.schedule_cron:
        await _sync_schedule(row)
    return row


@router.get("/routines/{routine_id}", response_model=RoutineOut)
async def get_routine(
    routine_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Routine:
    return await get_visible_routine(db, routine_id, user)


@router.patch("/routines/{routine_id}", response_model=RoutineOut)
async def update_routine(
    routine_id: uuid.UUID,
    body: UpdateRoutineIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Routine:
    """Update a routine; changing steps bumps `version` and re-syncs the schedule."""
    routine = await get_visible_routine(db, routine_id, user)
    changes = body.model_dump(exclude_unset=True)

    steps_changed = "steps" in changes and changes["steps"] is not None and changes["steps"] != routine.steps
    schedule_changed = "schedule_cron" in changes or "enabled" in changes

    for field, value in changes.items():
        if field == "enabled":
            if value is not None:
                routine.enabled = value
        elif field == "schedule_cron":
            routine.schedule_cron = value
        elif value is not None:
            setattr(routine, field, value)

    if steps_changed:
        routine.version = (routine.version or 1) + 1

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=routine.bot_id,
            event_type="routine_updated",
            detail={
                "routine_id": str(routine.id),
                "fields": sorted(changes.keys()),
                "version": routine.version,
            },
        )
    )
    await db.commit()
    await db.refresh(routine)

    if steps_changed or schedule_changed:
        if routine.enabled and routine.schedule_cron:
            await _sync_schedule(routine)
        else:
            await _delete_schedule(routine.id)
    return routine


@router.delete("/routines/{routine_id}", response_model=OkOut)
async def delete_routine(
    routine_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    routine = await get_visible_routine(db, routine_id, user)
    bot_id = routine.bot_id
    await _delete_schedule(routine.id)
    await db.delete(routine)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="routine_deleted",
            detail={"routine_id": str(routine_id)},
        )
    )
    await db.commit()
    return OkOut(ok=True, detail="deleted")


@router.post("/routines/{routine_id}/run", response_model=RoutineRunOut)
async def run_routine(
    routine_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RoutineRunOut:
    """Start `RoutineWorkflow` now, falling back to an inline run when Temporal is down.

    A manual trigger has a human behind it, so the caller rides along in the
    workflow start payload as `user_id`. Without it the run looks unattended and
    any approval it raises against a shared system bot would stay decidable by
    anyone who can see that bot.
    """
    routine = await get_visible_routine(db, routine_id, user)

    temporal = _temporal()
    starter = getattr(temporal, "start_routine_now", None) if temporal else None
    handle: dict[str, Any] | None = None
    error: str | None = None
    if starter is not None:
        kwargs = {"user_id": str(user.id)} if _accepts(starter, "user_id") else {}
        if not kwargs:
            logger.warning(
                "temporal_client.start_routine_now takes no user_id in this build; "
                "routine %s will run unattributed",
                routine_id,
            )
        try:
            handle = await starter(routine, **kwargs)
        except Exception as exc:  # noqa: BLE001 - fall through to inline
            error = str(exc)
            logger.warning("temporal start failed for routine %s: %s", routine_id, exc)

    if handle:
        db.add(
            AuditEvent(
                actor_user_id=user.id,
                bot_id=routine.bot_id,
                event_type="routine_started",
                detail={"routine_id": str(routine_id), **{k: str(v) for k, v in handle.items()}},
            )
        )
        await db.commit()
        return RoutineRunOut(
            workflow_id=handle.get("workflow_id"),
            run_id=handle.get("run_id"),
            inline=False,
            status="started",
        )

    # Inline fallback: record the attempt so the UI has something to poll.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    workflow_id = f"routine-{routine_id}-{stamp}"
    inline_fn = None
    service = optional_service("routines")
    if service is not None:
        inline_fn = getattr(service, "run_inline", None)

    status_text = "queued"
    inline_run_id: str | None = None
    if inline_fn is not None:
        kwargs = {"user": user} if _accepts(inline_fn, "user") else {}
        try:
            outcome = await inline_fn(db, routine, **kwargs)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            status_text = "failed"
            logger.exception("inline routine run failed for %s", routine_id)
        else:
            # Report what the service actually did: a routine that parks an
            # approval comes back "awaiting_approval", not "completed".
            if isinstance(outcome, dict):
                status_text = str(outcome.get("status") or "completed")
                run_ref = outcome.get("run_id")
                inline_run_id = str(run_ref) if run_ref else None
            else:
                status_text = "completed"

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=routine.bot_id,
            event_type="routine_started_inline",
            detail={"routine_id": str(routine_id), "workflow_id": workflow_id, "status": status_text},
        )
    )
    await db.commit()
    return RoutineRunOut(
        workflow_id=workflow_id,
        run_id=inline_run_id,
        inline=True,
        status=status_text,
        detail=error or "Temporal unreachable - ran inline",
    )


@router.get("/routines/{routine_id}/runs", response_model=list[RunOut])
async def routine_runs(
    routine_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Run]:
    """Recent runs for this routine.

    Matches the indexed ``runs.routine_id`` column, OR-ed with the legacy
    ``context_ledger->>routine_id`` key so rows written before the column
    existed still show up.
    """
    routine = await get_visible_routine(db, routine_id, user)
    ledger_match = and_(
        Run.bot_id == routine.bot_id,
        Run.context_ledger["routine_id"].astext == str(routine_id),
    )
    column = getattr(Run, "routine_id", None)
    where = or_(column == routine_id, ledger_match) if column is not None else ledger_match
    stmt = select(Run).where(where).order_by(Run.created_at.desc()).limit(limit)
    try:
        result = await db.execute(stmt)
    except Exception as exc:  # noqa: BLE001 - non-postgres or missing column
        logger.warning("routine run lookup failed for %s: %s", routine_id, exc)
        raise AppError(503, "runs_unavailable", "Routine run history is unavailable") from exc
    return list(result.scalars().all())
