"""Inline routine execution — the fallback when Temporal is unreachable.

One traversal, two outcomes. `walk_steps` is the only implementation of step
dispatch in this lane: `run_inline` drives it to perform work, and
`services.simulation.dry_run_routine` drives the *same* function inside a
`SimulationContext` so every effect records its intent instead. A copy-pasted
dry run would be worse than none, because it would drift from what actually
executes and the plan a human approved would stop describing reality.

The split of responsibilities is deliberate:

* this module knows **step shapes** — it turns a taught step into an `Effect`
  and, when the gate fires, into an approval payload;
* `services.simulation.perform` knows **effects** — it classifies risk, runs
  preflight, and either records or performs.

Nothing here calls a connector, an MCP server or a desktop directly.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Routine, Run, User
from app.services import approvals as approvals_service
from app.services import simulation
from app.services.simulation import Effect

logger = logging.getLogger(__name__)

STEP_TYPES = ("desktop", "connector", "mcp", "approval")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _step_type(step: dict) -> str:
    return str(step.get("type") or "desktop").strip().lower()


def transient_routine(
    *,
    bot_id: uuid.UUID,
    steps: list[dict[str, Any]],
    name: str = "ad-hoc plan",
    owner_user_id: uuid.UUID | None = None,
) -> Routine:
    """An unsaved `Routine` that carries steps through the shared traversal.

    Used by `dry_run_action` and by executing a saved plan that never belonged
    to a routine. It is never added to the session; `id` stays `None`, which is
    how the rest of this module recognises it.
    """
    return Routine(
        id=None,
        bot_id=bot_id,
        owner_user_id=owner_user_id,
        name=name,
        description="",
        steps=list(steps or []),
        version=1,
        enabled=True,
    )


def _approval_payload(routine: Routine, step: dict, index: int) -> dict[str, Any]:
    """Held-payload shape from docs/API.md, matching the worker's builder."""
    payload = dict(step.get("payload") or {})
    payload.setdefault("kind", step.get("kind", "message_only"))
    for field in ("connector_id", "action", "input", "draft"):
        if step.get(field) is not None:
            payload.setdefault(field, step[field])
    thread_id = step.get("thread_id")
    if thread_id:
        payload.setdefault("thread_id", str(thread_id))
    if routine.id is not None:
        payload.setdefault("routine_id", str(routine.id))
    payload.setdefault("step_index", index)
    return payload


async def _hold(
    db: AsyncSession,
    routine: Routine,
    run: Run | None,
    step: dict,
    index: int,
    *,
    risk: str,
    title: str,
    summary: str,
    payload: dict[str, Any],
    requester: uuid.UUID | None,
) -> dict[str, Any]:
    # Requester scoping. setdefault, never overwrite: a step payload that already
    # names a requester wins, matching worker.activities._approval_request.
    if requester is not None:
        payload.setdefault("requested_by", str(requester))
    else:
        logger.warning(
            "routine %s step %d raises an approval with no requester — "
            "it will fall back to bot visibility",
            routine.id,
            index,
        )
    approval = await approvals_service.create_approval(
        db,
        run_id=getattr(run, "id", None),
        bot_id=routine.bot_id,
        risk=risk,
        title=title,
        summary=summary,
        payload=payload,
    )
    return {
        "ok": True,
        "index": index,
        "type": _step_type(step),
        "awaiting_approval": str(approval.id),
        "result": {"approval_id": str(approval.id), "title": approval.title},
    }


# ---------------------------------------------------------------------------
# Step shape -> Effect
# ---------------------------------------------------------------------------


def _step_effect(routine: Routine, step: dict, index: int) -> Effect:
    """Resolve one taught step into the effect it wants to perform.

    Raises `ValueError` for a step the dispatcher cannot make sense of — the
    same failures the previous per-branch code raised, in the same words.
    """
    step_type = _step_type(step)

    if step_type == "approval":
        return Effect(
            kind="approval",
            bot_id=routine.bot_id,
            action="approval",
            input_data=_approval_payload(routine, step, index),
            step_index=index,
            declared_risk=str(step.get("risk", "send")),
        )

    if step_type == "desktop":
        return Effect(
            kind="desktop",
            bot_id=routine.bot_id,
            action=str(step.get("action", "click")),
            input_data=dict(step.get("args") or {}),
            step_index=index,
            declared_risk=step.get("risk"),
        )

    if step_type == "connector":
        connector_id = step.get("connector_id") or step.get("connector")
        action = step.get("action")
        if not connector_id or not action:
            raise ValueError(f"connector step {index} needs connector_id and action")
        return Effect(
            kind="connector",
            bot_id=routine.bot_id,
            action=str(action),
            input_data=dict(step.get("input") or step.get("args") or {}),
            step_index=index,
            connector_id=str(connector_id),
            declared_risk=step.get("risk"),
        )

    if step_type == "mcp":
        mcp_id = step.get("mcp_id") or step.get("mcp")
        tool = step.get("tool") or step.get("action")
        if not mcp_id or not tool:
            raise ValueError(f"mcp step {index} needs mcp_id and tool")
        try:
            mcp_uuid = mcp_id if isinstance(mcp_id, uuid.UUID) else uuid.UUID(str(mcp_id))
        except ValueError as exc:
            raise ValueError(f"mcp step {index} has an invalid mcp_id {mcp_id!r}") from exc
        return Effect(
            kind="mcp",
            bot_id=routine.bot_id,
            action=str(tool),
            input_data=dict(step.get("arguments") or step.get("args") or {}),
            step_index=index,
            mcp_id=mcp_uuid,
            # Same escalate-only declared risk the desktop and connector branches
            # pass. Dropping it here made escalate-only hold over HTTP (the worker
            # forwards `risk`) but not inline - a per-executor divergence, which is
            # the exact failure the single-classifier rule exists to prevent.
            declared_risk=step.get("risk"),
        )

    raise ValueError(f"unknown routine step type {step_type!r} at index {index}")


def _hold_request(
    routine: Routine,
    step: dict,
    index: int,
    effect: Effect,
) -> tuple[str, str, dict[str, Any]]:
    """Title, summary and held payload for a step the gate stopped."""
    if effect.kind == "approval":
        return (
            str(step.get("title") or f"Approve routine step {index}"),
            str(step.get("summary") or step.get("description") or ""),
            _approval_payload(routine, step, index),
        )
    if effect.kind == "connector":
        payload = {
            "kind": "connector_action",
            "connector_id": effect.connector_id,
            "action": effect.action,
            "input": effect.input_data,
            "step_index": index,
        }
        title = f"Approve {effect.connector_id}: {effect.action}"
    elif effect.kind == "mcp":
        # `mcp_tool` is one of the four documented approval kinds and
        # `approvals.execute_approved` already dispatches it, so an approved MCP
        # call runs back through the same path an approved connector call does.
        payload = {
            "kind": "mcp_tool",
            "mcp_id": str(effect.mcp_id),
            "tool": effect.action,
            "arguments": effect.input_data,
            "step_index": index,
        }
        title = f"Approve MCP tool: {effect.action}"
    else:
        payload = {
            "kind": "desktop_steps",
            "steps": [{"action": effect.action, "args": effect.input_data}],
            "step_index": index,
        }
        title = f"Approve desktop action: {effect.action}"
    if routine.id is not None:
        payload["routine_id"] = str(routine.id)
    return title, f"Routine {routine.name} step {index}", payload


async def _run_step(
    db: AsyncSession,
    routine: Routine,
    run: Run | None,
    step: dict,
    index: int,
    *,
    requester: uuid.UUID | None,
) -> dict[str, Any]:
    """Execute one step. Mirrors worker.activities.run_step_activity semantics."""
    step_type = _step_type(step)
    effect = _step_effect(routine, step, index)
    # `_step_effect` only knows the step shape, not which run is executing it.
    # Without this the action-log rows a routine or a plan writes carry a NULL
    # run_id, so `GET /action-log?run_id=` cannot find them and an operator
    # cannot ask "what did this run actually do?" - which is most of the point
    # of having the log.
    effect = dataclasses.replace(
        effect,
        run_id=getattr(run, "id", None),
        actor_user_id=requester,
    )

    outcome = await simulation.perform(db, effect)
    if outcome.gated:
        title, summary, payload = _hold_request(routine, step, index, effect)
        return await _hold(
            db,
            routine,
            run,
            step,
            index,
            risk=outcome.risk,
            title=title,
            summary=summary,
            payload=payload,
            requester=requester,
        )
    return {"ok": outcome.ok, "index": index, "type": step_type, "result": outcome.result}


# ---------------------------------------------------------------------------
# The traversal both paths share
# ---------------------------------------------------------------------------


async def walk_steps(
    db: AsyncSession,
    routine: Routine,
    run: Run | None,
    *,
    requester: uuid.UUID | None,
    halt_on_failure: bool = True,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Visit every step in order. Returns `(results, status, error)`.

    `halt_on_failure=True` is the real run: it stops at the first gate or
    failure, because everything after it depends on work that has not happened.
    `halt_on_failure=False` is the rehearsal: nothing happened, so the traversal
    keeps going and the reviewer sees the whole plan rather than its first
    problem.

    Both settings visit the same steps in the same order with the same resolved
    inputs — that equivalence is the whole value of the dry run, and
    `tests/services/test_simulation.py` asserts it directly.
    """
    results: list[dict[str, Any]] = []
    status = "completed"
    error: str | None = None

    for index, raw_step in enumerate(routine.steps or []):
        step = raw_step if isinstance(raw_step, dict) else {}
        if not step:
            status = "failed"
            error = f"step {index} is not an object"
            simulation.record_problem(step_index=index, kind="unknown", message=error)
            results.append({"ok": False, "index": index, "type": "unknown", "error": error})
            if halt_on_failure:
                break
            continue

        try:
            outcome = await _run_step(db, routine, run, step, index, requester=requester)
        except Exception as exc:  # noqa: BLE001 - inline runs report, never raise
            logger.exception("inline routine %s failed at step %d", routine.id, index)
            status = "failed"
            error = f"step {index}: {exc}"
            simulation.record_problem(step_index=index, kind=_step_type(step), message=str(exc))
            results.append(
                {"ok": False, "index": index, "type": _step_type(step), "error": str(exc)}
            )
            if halt_on_failure:
                break
            continue

        results.append(outcome)
        if outcome.get("awaiting_approval"):
            status = "awaiting_approval"
            if halt_on_failure:
                break
            continue
        if not outcome.get("ok"):
            status = "failed"
            error = f"step {index}: {(outcome.get('result') or {}).get('error', 'step failed')}"
            if halt_on_failure:
                break

    return results, status, error


async def run_inline(
    db: AsyncSession,
    routine: Routine,
    *,
    user: User | None = None,
) -> dict:
    """Execute a routine's steps in-process. Never raises.

    Returns `{"run_id", "status", "results"}` where status is one of
    `completed`, `failed`, or `awaiting_approval`.
    """
    # The authenticated human who triggered this run, else the routine's owner.
    # Only genuinely unattended runs end up with None.
    requester = getattr(user, "id", None) or routine.owner_user_id

    run = Run(
        thread_id=None,
        routine_id=routine.id,
        bot_id=routine.bot_id,
        status="running",
        # Keep the ledger key: existing rows and the worker callback use it.
        context_ledger={
            "routine_id": str(routine.id) if routine.id else None,
            "routine_name": routine.name,
            "version": routine.version,
            "inline": True,
            "requested_by": str(requester) if requester else None,
        },
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    steps = list(routine.steps or [])
    results, status, error = await walk_steps(
        db, routine, run, requester=requester, halt_on_failure=True
    )

    run.status = status
    run.error = error
    run.finished_at = _now()
    run.detail = {
        "inline": True,
        "steps_total": len(steps),
        "steps_completed": sum(
            1 for r in results if r.get("ok") and not r.get("awaiting_approval")
        ),
        "results": results,
    }
    db.add(
        AuditEvent(
            actor_user_id=getattr(user, "id", None),
            bot_id=routine.bot_id,
            event_type="routine_run",
            detail={
                "routine_id": str(routine.id) if routine.id else None,
                "run_id": str(run.id),
                "status": status,
                "inline": True,
                "steps_total": len(steps),
                "error": error,
            },
        )
    )
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not finalise inline run %s: %s", run.id, exc)
        await db.rollback()

    return {"run_id": str(run.id), "status": status, "results": results, "error": error}
