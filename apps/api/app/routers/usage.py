"""Spend reporting, evals, run history, and the audit log."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.models import AuditEvent, Bot, CostLedger, Routine, Run, User
from app.routers.deps import (
    bot_visibility_clause,
    get_visible_bot,
    get_visible_run,
    model_router,
    run_visibility_stmt,
    visible_bot_ids_subquery,
)
from app.schemas import (
    AuditEventOut,
    EvalCaseIn,
    EvalSuiteIn,
    EvalSuiteOut,
    RunOut,
    RunStatusIn,
    UsageOut,
)

logger = logging.getLogger("nesqbot.usage")

router = APIRouter(tags=["usage"])

EVAL_SYSTEM_PROMPT = "You are Nesq Bot under eval. Be concise and accurate."


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@router.get("/usage", response_model=list[UsageOut])
async def usage(
    days: int = Query(default=1, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[UsageOut]:
    """Per-bot spend for the visible bots over the last `days` calendar days."""
    bots = (await db.execute(select(Bot).where(bot_visibility_clause(user)))).scalars().all()
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=days - 1)

    out: list[UsageOut] = []
    for bot in bots:
        spent = await model_router.spent_today_usd(db, bot.id)
        entries_q = await db.execute(
            select(CostLedger)
            .where(CostLedger.bot_id == bot.id, CostLedger.created_at >= start)
            .order_by(CostLedger.created_at.desc())
            .limit(50)
        )
        entries = [
            {
                "tier": e.tier,
                "input_tokens": e.input_tokens,
                "output_tokens": e.output_tokens,
                "cost_usd": float(e.cost_usd),
                "created_at": e.created_at.isoformat(),
            }
            for e in entries_q.scalars().all()
        ]
        out.append(
            UsageOut(
                bot_id=bot.id,
                bot_name=bot.name,
                spent_usd_today=float(spent),
                budget_usd=float(bot.daily_budget_usd),
                entries=entries,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Evals
# ---------------------------------------------------------------------------


async def _run_case(case: EvalCaseIn) -> dict[str, Any]:
    result = await model_router.chat(
        task="agent_turn",
        messages=[
            {"role": "system", "content": EVAL_SYSTEM_PROMPT},
            {"role": "user", "content": case.prompt},
        ],
    )
    missing = [s for s in case.expect_contains if s.lower() not in result.content.lower()]
    return {
        "name": case.name,
        "passed": not missing,
        "missing": missing,
        "tier": result.tier,
        "cost_usd": float(result.cost_usd),
        "output": result.content,
    }


@router.post("/evals/run")
async def run_eval(
    body: EvalCaseIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Cheap mini-tier eval for promoting routines - no flagship."""
    return await _run_case(body)


@router.post("/evals/suite", response_model=EvalSuiteOut)
async def run_eval_suite(
    body: EvalSuiteIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EvalSuiteOut:
    results = [await _run_case(case) for case in body.cases]
    return EvalSuiteOut(
        passed=sum(1 for r in results if r["passed"]),
        total=len(results),
        results=results,
        cost_usd=round(sum(float(r["cost_usd"]) for r in results), 6),
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    thread_id: uuid.UUID | None = Query(default=None),
    bot_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Run]:
    # Runs are scoped by the human behind them - thread owner, routine owner, or
    # custom-bot owner - never by bot visibility: the system bots are shared, so
    # a bot fallback would list another user's run to everyone. A run with no
    # resolvable owner is listed to nobody; see `run_visibility_stmt`.
    stmt = run_visibility_stmt(user)
    if thread_id is not None:
        stmt = stmt.where(Run.thread_id == thread_id)
    if bot_id is not None:
        await get_visible_bot(db, bot_id, user)
        stmt = stmt.where(Run.bot_id == bot_id)
    if status:
        stmt = stmt.where(Run.status == status)
    stmt = stmt.order_by(Run.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Run:
    return await get_visible_run(db, run_id, user)


#: Statuses that close a run out and stamp `finished_at`.
TERMINAL_RUN_STATUSES = {
    "completed",
    "succeeded",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "timed_out",
    "terminated",
    "expired",
}


@router.post("/runs/{run_id}/status", response_model=RunOut)
async def update_run_status(
    run_id: uuid.UUID,
    body: RunStatusIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RunOut:
    """Worker callback that makes routine failures visible in the UI."""
    run = await get_visible_run(db, run_id, user)

    run.status = body.status
    if body.workflow_id:
        run.temporal_workflow_id = body.workflow_id
    # Only link a routine that still exists: routines.id is an FK, and a routine
    # deleted mid-run would otherwise make the terminal callback fail forever.
    # The ledger keeps the id either way.
    if body.routine_id is not None and hasattr(run, "routine_id"):
        if await db.get(Routine, body.routine_id) is not None:
            run.routine_id = body.routine_id
        else:
            logger.warning(
                "run %s reported routine_id %s which no longer exists; ledger only",
                run_id,
                body.routine_id,
            )

    terminal = body.status in TERMINAL_RUN_STATUSES
    if hasattr(run, "error"):
        run.error = body.error
    if terminal and hasattr(run, "finished_at"):
        run.finished_at = datetime.now(timezone.utc)

    # `detail` is the routine summary dict; prefer the column, else the ledger.
    has_detail_column = hasattr(run, "detail")
    if body.detail is not None and has_detail_column:
        run.detail = body.detail

    ledger: dict[str, Any] = dict(run.context_ledger or {})
    if body.detail is not None and not has_detail_column:
        ledger["detail"] = body.detail
    if body.error is not None and not hasattr(run, "error"):
        ledger["error"] = body.error
    if body.routine_id is not None:
        ledger["routine_id"] = str(body.routine_id)
    if body.workflow_id:
        ledger["workflow_id"] = body.workflow_id
    if terminal:
        ledger["finished_at"] = datetime.now(timezone.utc).isoformat()
    run.context_ledger = ledger

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=body.bot_id or run.bot_id,
            event_type="run_status",
            detail={
                "run_id": str(run_id),
                "status": body.status,
                "error": body.error,
                "routine_id": str(body.routine_id) if body.routine_id else None,
                "workflow_id": body.workflow_id,
            },
        )
    )
    await db.commit()
    await db.refresh(run)
    return RunOut.model_validate(run)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=list[AuditEventOut])
async def list_audit(
    bot_id: uuid.UUID | None = Query(default=None),
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    before: datetime | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AuditEvent]:
    """Newest-first audit trail for the caller and the bots they can see."""
    stmt = (
        select(AuditEvent)
        .where(
            or_(
                AuditEvent.actor_user_id == user.id,
                AuditEvent.bot_id.in_(visible_bot_ids_subquery(user)),
            )
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
    )
    if bot_id is not None:
        await get_visible_bot(db, bot_id, user)
        stmt = stmt.where(AuditEvent.bot_id == bot_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if before is not None:
        stmt = stmt.where(AuditEvent.created_at < before)
    result = await db.execute(stmt)
    return list(result.scalars().all())
