"""Spend reporting, evals, run history, and the audit log."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db, release_transaction
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


#: Ledger rows returned per bot. Unchanged from the per-bot `LIMIT 50` this
#: endpoint has always applied — see the note in `usage` about what it means for
#: the panel that reads it.
USAGE_ENTRY_LIMIT = 50


@router.get("/usage", response_model=list[UsageOut])
async def usage(
    days: int = Query(default=1, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[UsageOut]:
    """Per-bot spend for the visible bots over the last `days` calendar days.

    Three queries, not 2N+1. The old shape ran `spent_today_usd` plus a 50-row
    ledger select *per visible bot*, and `UsagePanel.tsx` documents its
    `refreshKey` as "the shell does it after every completed turn" — so the
    fan-out ran on every chat turn, growing with the roster. `work_items.py`
    names this mistake in its own words ("a query per row, which on the default
    page of 50 is 51 round trips to render a list nobody reads past the top of")
    and then does it correctly; this is the same fix, with the same payload.

    Two things this deliberately does *not* change, both visible in the UI and
    both wider than a query shape:

    * `spent_usd_today` ignores `days` — it is always the midnight-to-now total,
      as its name says, so `days=7` returns a today-only headline next to a
      seven-day entry list.
    * `entries` is capped at `USAGE_ENTRY_LIMIT` while the headline is an
      uncapped SUM, and `useUsage.ts`'s `breakdown()` derives per-tier calls and
      tokens by summing `entries`. For any bot past 50 ledger rows in the window
      the panel's tier costs cannot add up to its own total.

    Fixing either is a contract change to `UsageOut` (a `spent_usd_window`, or a
    server-side tier rollup) and belongs with the client work, not here.
    """
    bots = (await db.execute(select(Bot).where(bot_visibility_clause(user)))).scalars().all()
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=days - 1)
    bot_ids = [bot.id for bot in bots]
    if not bot_ids:
        return []

    # Query 2: today's spend for every bot at once. Same window and same
    # `coalesce`-to-zero semantics as `ModelRouter.spent_today_usd`, which is
    # what this replaces — a bot with no rows is absent from the result and
    # falls back to 0 below, which is the same answer.
    totals = await db.execute(
        select(CostLedger.bot_id, func.coalesce(func.sum(CostLedger.cost_usd), 0))
        .where(CostLedger.bot_id.in_(bot_ids), CostLedger.created_at >= midnight)
        .group_by(CostLedger.bot_id)
    )
    spent_by_bot = dict(totals.all())

    # Query 3: the newest `USAGE_ENTRY_LIMIT` rows *per bot*, which is a
    # per-group LIMIT and therefore a window function rather than a LIMIT. `id`
    # is a tiebreaker, not the ordering: `created_at` defaults to
    # `clock_timestamp()` so collisions are vanishingly unlikely, but a
    # row_number over a non-unique ordering is free to break ties differently
    # between the ranking and the output, which would put an entry in the list
    # at the wrong position rather than merely in an arbitrary one.
    ranked = (
        select(
            CostLedger.bot_id.label("bot_id"),
            CostLedger.tier.label("tier"),
            CostLedger.input_tokens.label("input_tokens"),
            CostLedger.output_tokens.label("output_tokens"),
            CostLedger.cost_usd.label("cost_usd"),
            CostLedger.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=CostLedger.bot_id,
                order_by=(CostLedger.created_at.desc(), CostLedger.id.desc()),
            )
            .label("rank"),
        )
        .where(CostLedger.bot_id.in_(bot_ids), CostLedger.created_at >= start)
        .subquery()
    )
    rows = await db.execute(
        select(ranked)
        .where(ranked.c.rank <= USAGE_ENTRY_LIMIT)
        .order_by(ranked.c.bot_id, ranked.c.rank)
    )
    entries_by_bot: dict[uuid.UUID, list[dict[str, Any]]] = {bot_id: [] for bot_id in bot_ids}
    for row in rows.all():
        entries_by_bot[row.bot_id].append(
            {
                "tier": row.tier,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cost_usd": float(row.cost_usd),
                "created_at": row.created_at.isoformat(),
            }
        )

    return [
        UsageOut(
            bot_id=bot.id,
            bot_name=bot.name,
            spent_usd_today=float(spent_by_bot.get(bot.id, 0)),
            budget_usd=float(bot.daily_budget_usd),
            entries=entries_by_bot[bot.id],
        )
        for bot in bots
    ]


# ---------------------------------------------------------------------------
# Evals
# ---------------------------------------------------------------------------


async def _run_case(db: AsyncSession, case: EvalCaseIn) -> dict[str, Any]:
    # `agent_turn` routes to `mini` (`route_task`), so this is not the
    # reason-tier call the incident in `db.release_transaction` clocked at 57.0
    # seconds. It is still one outbound model call with
    # `request_timeout_seconds = 60.0` and the SDK's default two retries, and
    # `run_eval_suite` below runs one per case in a serial list comprehension,
    # so a suite holds a single transaction for the sum of its cases. Released
    # per case rather than once before the loop, so a case that starts touching
    # `db` cannot silently re-arm it.
    await release_transaction(db)
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
    return await _run_case(db, body)


@router.post("/evals/suite", response_model=EvalSuiteOut)
async def run_eval_suite(
    body: EvalSuiteIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EvalSuiteOut:
    results = [await _run_case(db, case) for case in body.cases]
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
