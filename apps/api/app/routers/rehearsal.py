"""Rehearsal and reversibility over HTTP — dry runs, plans, and the undo log.

a competing agent product's own documentation says of its test runs that "a test run performs
real work: it can navigate websites, change files and call connected tools",
and that an approval "controls the proposed action, it does not reverse work
already completed". This module is where the opposite promise becomes a
product surface.

Three groups of routes, one service lane behind them:

* **dry runs** (`services.simulation.dry_run_*`) rehearse a routine or a single
  connector action and return a `PlanOut`. Nothing is performed and nothing is
  written — the traversal runs inside a `SimulationContext`, and the chokepoint
  records intent instead of acting.
* **plans** persist a rehearsal so a human can approve *the plan itself*.
  Execution re-derives the plan and compares its content hash; a plan that no
  longer describes the same work is refused **409 `plan_drifted`** rather than
  executing something nobody reviewed.
* **the action log** lists executed effects with their reversibility and runs a
  compensator on request.

The plan a client saves is never the plan a client sent. `POST /plans` takes
the *inputs* to a rehearsal (a routine, or a bot plus steps) and re-runs it
server-side, so a forged `content_hash` cannot be laundered into an approval.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.errors import AppError
from app.models import ActionLog, Bot, Connector, PlanRecord, User
from app.routers.deps import (
    bot_visibility_clause,
    get_visible_bot,
    get_visible_routine,
)
from app.schemas import (
    ActionLogOut,
    DryRunActionIn,
    PlanOut,
    PlanRecordOut,
    ReversibilityRowOut,
    RoutineRunOut,
    SavePlanIn,
    UndoResultOut,
)
from app.services import simulation
from app.services import undo as undo_service
from app.services.routines import transient_routine

logger = logging.getLogger("nesqbot.rehearsal")

router = APIRouter(tags=["rehearsal"])

#: Service error code -> HTTP status, for `POST /action-log/{id}/undo`.
UNDO_STATUS: dict[str, int] = {
    "not_found": 404,
    "already_undone": 409,
    "not_reversible": 422,
}

#: Service error code -> HTTP status, for `POST /plans/{id}/execute`. The code
#: itself is preserved in the envelope: `plan_drifted` is the signal that a plan
#: a human approved was mutated underneath them, and it must reach the client.
EXECUTE_STATUS: dict[str, int] = {
    "plan_drifted": 409,
    "already_executed": 409,
    "routine_gone": 404,
}


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


async def _get_visible_plan(db: AsyncSession, plan_id: uuid.UUID, user: User) -> PlanRecord:
    """Load a plan the caller may see, or 404.

    Scoped by the human who produced it, mirroring approvals rather than bots:
    a plan can hang off a *shared* system bot, so bot visibility alone would
    hand one user's rehearsal — and the ability to execute it — to every
    authenticated caller. A plan with no knowable author falls back to bot
    visibility. Always 404, never 403.
    """
    record = await db.get(PlanRecord, plan_id)
    if record is None:
        raise AppError(404, "plan_not_found", "Plan not found")
    if record.created_by is not None:
        if record.created_by != user.id:
            raise AppError(404, "plan_not_found", "Plan not found")
        return record
    await get_visible_bot(db, record.bot_id, user)
    return record


def _plan_is_visible(record: PlanRecord, user: User, visible_bot_ids: set[uuid.UUID]) -> bool:
    """The listing mirror of ``_get_visible_plan``."""
    if record.created_by is not None:
        return record.created_by == user.id
    return record.bot_id in visible_bot_ids


async def _get_visible_action_log(db: AsyncSession, entry_id: uuid.UUID, user: User) -> ActionLog:
    """Load an action-log entry the caller may see, or 404.

    Same rule as a plan: an entry attributed to a human belongs to that human,
    and an unattributed one (a routine step, a scheduled run) falls back to the
    visibility of the bot that performed it.
    """
    entry = await db.get(ActionLog, entry_id)
    if entry is None:
        raise AppError(404, "action_log_not_found", "Action log entry not found")
    if entry.actor_user_id is not None:
        if entry.actor_user_id != user.id:
            raise AppError(404, "action_log_not_found", "Action log entry not found")
        return entry
    await get_visible_bot(db, entry.bot_id, user)
    return entry


async def _visible_bot_ids(db: AsyncSession, user: User) -> set[uuid.UUID]:
    rows = await db.execute(select(Bot.id).where(bot_visibility_clause(user)))
    return set(rows.scalars().all())


# ---------------------------------------------------------------------------
# Dry runs
# ---------------------------------------------------------------------------


@router.post("/routines/{routine_id}/dry-run", response_model=PlanOut)
async def dry_run_routine(
    routine_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Rehearse a routine. Performs nothing, writes nothing, sends nothing.

    Walks the same step dispatcher the real run walks, so the plan describes
    what would actually happen rather than a second implementation of it.
    """
    routine = await get_visible_routine(db, routine_id, user)
    plan = await simulation.dry_run_routine(db, routine, user=user)
    return plan.as_dict()


@router.post(
    "/bots/{bot_id}/connectors/{connector_id}/actions/{action}/dry-run",
    response_model=PlanOut,
)
async def dry_run_connector_action(
    bot_id: uuid.UUID,
    connector_id: str,
    action: str,
    body: DryRunActionIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Rehearse one connector action. The sibling of the execute route above it.

    An unregistered connector is a 404 here, exactly as it is on the execute
    route — a bad URL is not a rehearsal finding. Everything the rehearsal *can*
    tell you (a missing input, an unbound credential, whether the call would
    reach the vendor or a mock) comes back inside the plan.
    """
    await get_visible_bot(db, bot_id, user)
    if await db.get(Connector, connector_id) is None:
        raise AppError(404, "connector_not_found", "Connector not found")
    plan = await simulation.dry_run_action(
        db,
        bot_id=bot_id,
        connector_id=connector_id,
        action=action,
        input_data=body.input,
        user=user,
    )
    return plan.as_dict()


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@router.post("/plans", response_model=PlanRecordOut)
async def save_plan(
    body: SavePlanIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlanRecord:
    """Persist a rehearsal so a human can approve the plan and then execute it.

    The rehearsal is re-run here rather than trusted from the body: the saved
    `content_hash` must be one this server produced, or the drift check that
    guards execution would be checking a client's arithmetic.
    """
    if body.routine_id is not None:
        # Not renamed from `body.name`: this row is in the session, and a plan
        # must never be able to edit the routine it rehearses.
        routine = await get_visible_routine(db, body.routine_id, user)
    elif body.bot_id is not None and body.steps is not None:
        await get_visible_bot(db, body.bot_id, user)
        routine = transient_routine(
            bot_id=body.bot_id,
            name=body.name or "ad-hoc plan",
            steps=body.steps,
            owner_user_id=user.id,
        )
    else:
        raise AppError(
            400,
            "plan_source_required",
            "Provide either routine_id, or bot_id together with steps",
        )

    plan = await simulation.dry_run_routine(db, routine, user=user)
    if body.expected_content_hash and body.expected_content_hash != plan.content_hash:
        raise AppError(
            409,
            "plan_drifted",
            "the plan no longer matches the one this hash was taken over",
            extra={
                "expected_hash": body.expected_content_hash,
                "actual_hash": plan.content_hash,
            },
        )
    return await simulation.save_plan(db, plan, user=user, status=body.status)


@router.get("/plans", response_model=list[PlanRecordOut])
async def list_plans(
    bot_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PlanRecord]:
    """Saved plans, newest first, scoped to the caller."""
    if bot_id is not None:
        await get_visible_bot(db, bot_id, user)
    records = await simulation.list_plans(db, bot_id=bot_id, limit=limit)
    visible = await _visible_bot_ids(db, user)
    return [r for r in records if _plan_is_visible(r, user, visible)]


@router.get("/plans/{plan_id}", response_model=PlanRecordOut)
async def get_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlanRecord:
    return await _get_visible_plan(db, plan_id, user)


@router.post("/plans/{plan_id}/execute", response_model=RoutineRunOut)
async def execute_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RoutineRunOut:
    """Execute a saved plan, but only if it still describes the same work.

    `plan_drifted` is surfaced as **409** with the code intact. A plan approved
    by a human and then mutated — by editing the routine underneath it, or by a
    re-classified risk — is precisely the attack the content hash exists to
    stop, and the client has to be able to tell that apart from a plan that
    simply ran already.
    """
    record = await _get_visible_plan(db, plan_id, user)
    outcome = await simulation.execute_plan(db, record, user=user)

    code = str(outcome.get("code") or "")
    if code:
        extra = {k: v for k, v in outcome.items() if k not in ("ok", "code", "error")}
        raise AppError(
            EXECUTE_STATUS.get(code, 409),
            code,
            str(outcome.get("error") or code.replace("_", " ")),
            extra=extra,
        )

    return RoutineRunOut(
        run_id=str(outcome["run_id"]) if outcome.get("run_id") else None,
        inline=True,
        status=str(outcome.get("status") or "completed"),
        detail=outcome.get("error"),
    )


# ---------------------------------------------------------------------------
# The undo log
# ---------------------------------------------------------------------------


@router.get("/action-log", response_model=list[ActionLogOut])
async def list_action_log(
    bot_id: uuid.UUID | None = Query(default=None),
    run_id: uuid.UUID | None = Query(default=None),
    reversible_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ActionLog]:
    """Executed effects with their reversibility, newest first.

    `reversible_only=true` is what the "take it back" view reads: it drops
    entries that were never reversible and entries already undone.
    """
    if bot_id is not None:
        await get_visible_bot(db, bot_id, user)
    entries = await undo_service.list_action_log(
        db,
        bot_id=bot_id,
        run_id=run_id,
        reversible_only=reversible_only,
        limit=limit,
    )
    visible = await _visible_bot_ids(db, user)
    return [
        e
        for e in entries
        if (e.actor_user_id == user.id if e.actor_user_id is not None else e.bot_id in visible)
    ]


@router.post("/action-log/{action_log_id}/undo", response_model=UndoResultOut)
async def undo_action(
    action_log_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UndoResultOut:
    """Run the compensator for one logged action.

    Idempotent by construction: the service claims the entry with a conditional
    UPDATE before the compensator runs, so a second call is refused **409
    `already_undone`** rather than performing the inverse twice. An action that
    never had an inverse is **422 `not_reversible`**, with the honest reason
    attached.
    """
    await _get_visible_action_log(db, action_log_id, user)
    outcome = await undo_service.undo(db, action_log_id, user=user)

    if not outcome.get("ok") and outcome.get("code"):
        code = str(outcome["code"])
        extra = {k: v for k, v in outcome.items() if k not in ("ok", "code", "error")}
        raise AppError(
            UNDO_STATUS.get(code, 409),
            code,
            str(outcome.get("error") or code.replace("_", " ")),
            extra=extra,
        )

    return UndoResultOut(
        ok=bool(outcome.get("ok")),
        action_log_id=uuid.UUID(str(outcome.get("action_log_id"))),
        kind=str(outcome.get("kind") or ""),
        action=str(outcome.get("action") or ""),
        compensator=outcome.get("compensator"),
        result=outcome.get("result") or {},
        error=outcome.get("error"),
    )


@router.get("/reversibility", response_model=list[ReversibilityRowOut])
async def reversibility(
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """The reversibility matrix, so the UI can say what can be taken back **before** a
    human acts rather than after.

    Being honest about the irreversible half is the point: a sent email is sent,
    and a matrix that quietly omitted it would read as a promise.
    """
    return undo_service.reversibility_matrix()
