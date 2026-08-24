"""Approvals — hold risky work, then execute it once a human says yes.

Executing an approved action does not mean reaching for the connector directly:
it goes back through `services.simulation.perform`, the same chokepoint the
routine runner uses, with `pre_approved=True` to skip the gate a human has
already cleared. That is what makes an approved send land in the undo log
attributed to the approval that authorised it.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Approval, AuditEvent, Message, User
from app.services import browser as browser_ops
from app.services import notifications, simulation
from app.services.simulation import Effect

logger = logging.getLogger(__name__)

KINDS = ("connector_action", "mcp_tool", "desktop_steps", "message_only")


async def create_approval(
    db: AsyncSession,
    *,
    bot_id: uuid.UUID,
    risk: str,
    title: str,
    summary: str,
    payload: dict[str, Any],
    run_id: uuid.UUID | None = None,
) -> Approval:
    """Persist a pending approval and ping the owner's devices.

    `run_id` is optional: routine steps of type "approval" create approvals
    outside any chat run.
    """
    approval = Approval(
        run_id=run_id,
        bot_id=bot_id,
        risk=risk,
        title=title,
        summary=(summary or "")[:2000],
        payload=payload or {},
        status="pending",
    )
    db.add(approval)
    db.add(
        AuditEvent(
            bot_id=bot_id,
            event_type="approval_created",
            detail={
                "run_id": str(run_id) if run_id else None,
                "risk": risk,
                "kind": (payload or {}).get("kind"),
                "title": title,
            },
        )
    )
    await db.commit()
    await db.refresh(approval)
    await notifications.notify_approval(db, approval)
    return approval


async def execute_approved(db: AsyncSession, approval: Approval, user: User) -> dict:
    """Run the held action. Always returns a dict — never raises."""
    payload = approval.payload or {}
    kind = payload.get("kind") or "message_only"

    try:
        if kind == "connector_action":
            outcome = await _run_connector_action(db, approval, payload, user)
        elif kind == "mcp_tool":
            outcome = await _run_mcp_tool(db, approval, payload, user)
        elif kind == "desktop_steps":
            outcome = await _run_desktop_steps(db, approval, payload, user)
        elif kind == "message_only":
            outcome = await _post_held_message(db, approval, payload)
        else:
            outcome = {"ok": False, "error": f"unknown approval kind '{kind}'"}
    except Exception as exc:  # noqa: BLE001 - an approval must never 500 the API
        logger.exception("approval %s execution failed", approval.id)
        outcome = {"ok": False, "error": str(exc)}

    outcome.setdefault("kind", kind)
    await _record(db, approval, user, outcome)
    return outcome


def _effect(approval: Approval, user: User | None, **kwargs: Any) -> Effect:
    """An `Effect` stamped with the approval that authorised it."""
    return Effect(
        bot_id=approval.bot_id,
        pre_approved=True,
        run_id=approval.run_id,
        approval_id=approval.id,
        actor_user_id=getattr(user, "id", None),
        **kwargs,
    )


async def _run_connector_action(
    db: AsyncSession, approval: Approval, payload: dict, user: User | None = None
) -> dict:
    connector_id = payload.get("connector_id")
    action = payload.get("action")
    if not connector_id or not action:
        return {"ok": False, "error": "approval payload missing connector_id/action"}
    # pre_approved: the risk gate already ran, and a human cleared it.
    outcome = await simulation.perform(
        db,
        _effect(
            approval,
            user,
            kind="connector",
            connector_id=str(connector_id),
            action=str(action),
            input_data=dict(payload.get("input") or {}),
        ),
    )
    result = outcome.result
    if result.get("ok"):
        return {"ok": True, "result": result}
    return {"ok": False, "error": result.get("error", "connector action failed"), "result": result}


async def _run_mcp_tool(
    db: AsyncSession, approval: Approval, payload: dict, user: User | None = None
) -> dict:
    mcp_id = payload.get("mcp_id")
    tool = payload.get("tool")
    if not mcp_id or not tool:
        return {"ok": False, "error": "approval payload missing mcp_id/tool"}
    try:
        mcp_uuid = mcp_id if isinstance(mcp_id, uuid.UUID) else uuid.UUID(str(mcp_id))
    except ValueError:
        return {"ok": False, "error": f"invalid mcp_id '{mcp_id}'"}

    outcome = await simulation.perform(
        db,
        _effect(
            approval,
            user,
            kind="mcp",
            mcp_id=mcp_uuid,
            action=str(tool),
            input_data=dict(payload.get("arguments") or payload.get("input") or {}),
        ),
    )
    result = outcome.result
    if result.get("ok"):
        return {"ok": True, "result": result}
    return {"ok": False, "error": result.get("error", "mcp call failed"), "result": result}


async def _run_desktop_steps(
    db: AsyncSession, approval: Approval, payload: dict, user: User | None = None
) -> dict:
    steps = payload.get("steps") or []
    if not steps:
        return {"ok": False, "error": "approval payload has no steps"}

    results: list[dict] = []
    for index, step in enumerate(steps):
        action = step.get("action") or step.get("type") or "click"
        args = step.get("args") or {k: v for k, v in step.items() if k not in ("action", "type")}
        outcome = await simulation.perform(
            db,
            _effect(approval, user, kind="desktop", action=str(action), input_data=dict(args)),
        )
        step_result = outcome.result
        results.append({"step": index, "action": action, **step_result})
        if not step_result.get("ok"):
            return {
                "ok": False,
                "error": _step_failure(str(action), step_result)
                or f"step {index} ({action}) failed",
                "results": results,
            }
    return {"ok": True, "results": results}


def _step_failure(action: str, result: dict) -> str:
    """Why a held step did not run, in words a person can act on.

    This string lands on `Approval.execution.error`, which is what the owner
    sees after pressing Approve. A bare code is no use to them, and a browser
    refusal in particular is a whole sentence worth reading: an approved click
    is re-resolved against the page as it is now, so "the element you approved
    is not there any more" and "two things now match it" are both outcomes a
    person has to be able to tell apart.
    """
    if browser_ops.is_browser_action(action):
        return browser_ops.short_failure(result)
    return str(result.get("error") or "")


async def _post_held_message(db: AsyncSession, approval: Approval, payload: dict) -> dict:
    thread_id = payload.get("thread_id")
    draft = payload.get("draft") or approval.summary
    if not thread_id:
        return {"ok": False, "error": "approval payload missing thread_id"}
    try:
        thread_uuid = thread_id if isinstance(thread_id, uuid.UUID) else uuid.UUID(str(thread_id))
    except ValueError:
        return {"ok": False, "error": f"invalid thread_id '{thread_id}'"}

    message = Message(
        thread_id=thread_uuid,
        bot_id=approval.bot_id,
        role="assistant",
        content=draft,
        meta={"approved": True, "approval_id": str(approval.id)},
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return {"ok": True, "message_id": str(message.id), "thread_id": str(thread_uuid)}


async def _record(db: AsyncSession, approval: Approval, user: User, outcome: dict) -> None:
    """Stamp the outcome on the approval and write the audit trail."""
    try:
        approval.execution = outcome
        db.add(
            AuditEvent(
                actor_user_id=getattr(user, "id", None),
                bot_id=approval.bot_id,
                event_type="approval_executed",
                detail={
                    "approval_id": str(approval.id),
                    "kind": outcome.get("kind"),
                    "ok": bool(outcome.get("ok")),
                    "error": outcome.get("error"),
                },
            )
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record approval execution %s: %s", approval.id, exc)
        await db.rollback()
