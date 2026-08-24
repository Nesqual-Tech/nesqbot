"""Approval queue: list, inspect, decide (executing on approve), expire.

Deciding is not the end of the story. A held action almost always sits in the
middle of a task — step 24 of a 36-step run — and the run that asked for it is
parked in `awaiting_approval` with everything needed to carry on. So a decision,
either way, hands the task back to the bot: approved means "it ran (or honestly
did not), keep going", refused means "a person said no, take another route or
stop and say what is left". Before this, approving executed one click and the
whole task simply stopped, and the person had to re-drive it from the start.

The continuation reuses the takeover machinery rather than a parallel one: the
same persisted `runs.detail` state, the same conversation rebuild, the same
conditional-`UPDATE` claim that makes a double-press a no-op.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi import status as status_codes
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.errors import AppError
from app.models import Approval, AuditEvent, Run, StandingApproval, User
from app.routers.deps import (
    approval_visibility_stmt,
    create_gated_approval,
    get_visible_approval,
    get_visible_bot,
    optional_service,
    orchestrator,
)
from app.schemas import (
    ApprovalDecisionIn,
    ApprovalOut,
    CreateApprovalIn,
    StandingApprovalListOut,
    StandingApprovalOut,
)
from app.services import standing_approvals
from app.services.orchestrator import (
    RUN_AGENT_KEY,
    RUN_AWAITING_APPROVAL,
    plain_place,
    standing_permission_announcement,
    step_intent,
)

logger = logging.getLogger("nesqbot.approvals")

router = APIRouter(tags=["approvals"])


def _approval_out(approval: Approval, execution: dict[str, Any] | None = None) -> ApprovalOut:
    out = ApprovalOut.model_validate(approval)
    if execution is not None:
        out.execution = execution
    note = (approval.payload or {}).get("decision_note")
    if note and out.note is None:
        out.note = note
    return out


async def _execute_approved(db: AsyncSession, approval: Approval, user: User) -> dict[str, Any]:
    """Run the held action via the service layer.

    A failing side effect is reported in the `execution` envelope rather than
    turned into a 500 - the decision itself still stands.
    """
    service = optional_service("approvals")
    fn = getattr(service, "execute_approved", None) if service else None
    if fn is None:
        logger.error("services.approvals.execute_approved unavailable; approval %s not executed", approval.id)
        return {"ok": False, "error": "approval execution service unavailable"}
    try:
        return await fn(db, approval, user)
    except Exception as exc:  # noqa: BLE001
        logger.exception("execute_approved failed for approval %s", approval.id)
        return {"ok": False, "error": str(exc)}


def _permits(rule: StandingApproval) -> str:
    """`click "Message" on linkedin.com/in/andrei-pop` — what one rule allows.

    Built from `step_intent`, which is the function the chat reply and the
    approval card already use to say what an action is. A standing permission
    described in its own private wording would be a third dialect for the same
    sentence, and the person reading the list has already read the other two.
    """
    described = f'{rule.ref_role} "{rule.ref_name}"'
    intent = step_intent(
        {"action": rule.action, "input": {"ref": "-", "ref_label": described}}
    )
    place = plain_place(rule.url_key)
    return f"{intent} on {place}" if place else intent


def _standing_out(rule: StandingApproval) -> StandingApprovalOut:
    return StandingApprovalOut(
        id=rule.id,
        bot_id=rule.bot_id,
        action=rule.action,
        risk=rule.risk,
        element=f'{rule.ref_role} "{rule.ref_name}"',
        url=rule.url_key,
        place=plain_place(rule.url_key),
        permits=_permits(rule),
        origin=rule.origin,
        note=rule.note_text or "",
        source_approval_ids=[str(i) for i in (rule.source_approval_ids or [])],
        used=int(rule.use_count or 0),
        last_used_at=rule.last_used_at,
        granted_at=rule.created_at,
        revoked_at=rule.revoked_at,
    )


async def _learn_standing_rule(
    db: AsyncSession, approval: Approval, user: User, execution: dict | None
) -> StandingApproval | None:
    """Create the standing permission this decision earns, if it earns one.

    Every gate is in `services.standing_approvals.learn_from_decision`; this is
    only the call site, and it is deliberately a single one. A rule that could be
    created from two places would be a rule whose provenance depends on which
    place created it.

    Never raises. A permission that could not be created is a person who gets
    asked again — mildly annoying, and strictly the safe direction — whereas a
    decision endpoint that 500s because of it has lost the decision itself.
    """
    try:
        return await standing_approvals.learn_from_decision(
            db, approval, decided_by=user.id, execution=execution
        )
    except Exception:  # noqa: BLE001 - the decision stands whatever this does
        logger.exception("could not learn a standing approval from %s", approval.id)
        await db.rollback()
        return None


async def _continue_run(
    db: AsyncSession,
    approval: Approval,
    user: User,
    decision: str,
    execution: dict | None,
    announce: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Hand the task back to the bot now that its held step has an answer.

    Returns None when there is nothing to continue, which is the common case and
    not a problem: an approval created by a routine step or straight off
    `POST /approvals` has no parked agent run behind it.

    The claim is a single conditional `UPDATE` on `runs.status`, the same shape
    `POST /runs/{run_id}/resume` uses. Two decisions racing — a double-click, or
    an approve arriving next to a sweeper's expire — must not produce two loops
    driving the same browser session, and the database is the only place that
    can arbitrate that.

    A continuation that fails is logged and swallowed. The decision itself
    stands and is already recorded; turning "the bot could not carry on" into a
    500 on the decide endpoint would make it look as though the approval had not
    been registered, which is worse than a task that stopped.
    """
    if approval.run_id is None:
        return None
    run = await db.get(Run, approval.run_id)
    if run is None:
        return None
    agent_state = (run.detail or {}).get(RUN_AGENT_KEY) or {}
    if str(agent_state.get("approval_id") or "") != str(approval.id):
        # This run is not parked on *this* approval. Never continue a run on the
        # strength of a decision about something else.
        return None

    claimed = await db.execute(
        update(Run)
        .where(Run.id == run.id, Run.status == RUN_AWAITING_APPROVAL)
        .values(status="running", updated_at=datetime.now(timezone.utc))
        .returning(Run.id)
    )
    if claimed.scalar_one_or_none() is None:
        current = (
            await db.execute(select(Run.status).where(Run.id == run.id))
        ).scalar_one_or_none() or "gone"
        logger.info("run %s not continued after decision: status is %s", run.id, current)
        return {"continued": False, "run_id": str(run.id), "status": current}
    # Commit the claim before the loop runs: it can take minutes and a second
    # decision has to see the transition immediately.
    await db.commit()
    await db.refresh(run)

    try:
        out = await orchestrator.continue_after_decision(
            db,
            user=user,
            run=run,
            approval=approval,
            decision=decision,
            execution=execution,
            announce=announce,
        )
    except Exception as exc:  # noqa: BLE001 - the decision stands either way
        logger.exception("continuing run %s after decision failed", run.id)
        return {"continued": False, "run_id": str(run.id), "error": str(exc)}
    return {
        "continued": bool(out.get("resumed")),
        "run_id": str(run.id),
        "status": out.get("status"),
        "outcome": out.get("outcome"),
        "message_id": out.get("message_id"),
    }


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    status: str = "pending",
    bot_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ApprovalOut]:
    stmt = approval_visibility_stmt(user)
    if status and status != "all":
        stmt = stmt.where(Approval.status == status)
    if bot_id is not None:
        await get_visible_bot(db, bot_id, user)
        stmt = stmt.where(Approval.bot_id == bot_id)
    stmt = stmt.order_by(Approval.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return [_approval_out(a) for a in result.scalars().all()]


@router.post("/approvals", response_model=ApprovalOut, status_code=status_codes.HTTP_201_CREATED)
async def create_approval(
    body: CreateApprovalIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApprovalOut:
    """Create an approval directly - used by routine steps of `type: "approval"`.

    `run_id` is optional: a routine step can park an approval outside a chat run.
    The owner is resolved and stamped into the held payload as `requested_by`
    (explicit payload value, then the thread behind `run_id`, then a custom bot
    owner). Pass `requested_by` yourself when acting on behalf of a human the API
    cannot infer - otherwise the approval stays decidable by anyone who can see
    the bot.
    """
    await get_visible_bot(db, body.bot_id, user)
    approval = await create_gated_approval(
        db,
        bot_id=body.bot_id,
        run_id=body.run_id,
        risk=body.risk,
        title=body.title,
        summary=body.summary,
        payload=body.payload,
        actor=user,
    )
    return _approval_out(approval)


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
async def get_approval(
    approval_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApprovalOut:
    approval = await get_visible_approval(db, approval_id, user)
    return _approval_out(approval)


@router.post("/approvals/{approval_id}/decide", response_model=ApprovalOut)
async def decide_approval(
    approval_id: uuid.UUID,
    body: ApprovalDecisionIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApprovalOut:
    """Approve or reject. Approving executes the held action before returning."""
    approval = await get_visible_approval(db, approval_id, user, for_decision=True)
    if approval.status != "pending":
        raise AppError(
            409,
            "approval_not_pending",
            f"Approval is already {approval.status}",
        )

    approval.status = body.decision
    approval.decided_by = user.id
    approval.decided_at = datetime.now(timezone.utc)
    if hasattr(approval, "note"):
        approval.note = body.note
    elif body.note:
        # No dedicated column in this build - stash it beside the held payload.
        approval.payload = {**(approval.payload or {}), "decision_note": body.note}

    execution: dict[str, Any] | None = None
    rule: StandingApproval | None = None
    if body.decision == "approved":
        execution = await _execute_approved(db, approval, user)
        if hasattr(approval, "execution"):
            approval.execution = execution or {}
        # After the action ran, and only if it ran. An approved click that
        # refused itself — the element was gone, two matched, the tab had
        # navigated — is not evidence that this element resolves uniquely on
        # this page, and that is the one thing a standing permission needs.
        rule = await _learn_standing_rule(db, approval, user, execution)

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=approval.bot_id,
            event_type="approval_decision",
            detail={
                "approval_id": str(approval.id),
                "decision": body.decision,
                "executed_ok": None if execution is None else bool(execution.get("ok")),
            },
        )
    )
    await db.commit()
    await db.refresh(approval)

    # A permission acquired by this decision is announced in the reply of the
    # turn that acquired it. Composed here, from the row that was actually
    # written, and handed to the continuation as text — not left to the model,
    # which is not a reliable narrator of a permission it benefits from.
    announce: tuple[str, ...] = ()
    granted: StandingApprovalOut | None = None
    if rule is not None:
        await db.refresh(rule)
        granted = _standing_out(rule)
        announce = (
            standing_permission_announcement(
                described=step_intent(
                    {"action": rule.action, "input": {"ref": "-", "ref_label": granted.element}}
                ),
                place=granted.place,
                origin=rule.origin,
                note=rule.note_text or "",
            ),
        )

    # …and then the bot carries on. A decision is one step of a task, not the
    # end of it: the run that asked for this is parked with everything needed to
    # continue, and both answers are answers. This happens after the commit, so
    # the decision is durable whatever the continuation does.
    continuation = await _continue_run(db, approval, user, body.decision, execution, announce)
    if continuation is not None:
        execution = {**(execution or {}), "continuation": continuation}
        await db.refresh(approval)

    if granted is not None:
        # Also on the decision's own response, because there is not always a
        # parked run to carry the sentence into a reply — a routine-created
        # approval has none — and "announced" cannot mean "announced when the
        # architecture happens to allow it".
        execution = {
            **(execution or {}),
            "standing_approval": granted.model_dump(mode="json"),
            "standing_announcement": announce[0],
        }

    out = _approval_out(approval, execution)
    if body.note:
        out.note = body.note
    return out


# ---------------------------------------------------------------------------
# Standing permissions
# ---------------------------------------------------------------------------
#
# The list, and one call to take one back.
#
# They live in this module rather than in one of their own because they are the
# same governance surface: a standing permission is an approval the person has
# already given, and splitting "what is waiting on you" from "what you have
# already allowed" across two routers would be splitting one question into two
# screens.


@router.get("/standing-approvals", response_model=StandingApprovalListOut)
async def list_standing_approvals(
    include_revoked: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StandingApprovalListOut:
    """Every standing permission this person has granted, newest first.

    Owner-scoped by the column, not by a bot's visibility: a permission is one
    human's consent, and a shared system bot must never show one person's grants
    to another. `include_revoked` is how the record outlives the permission —
    the rows are never deleted, so "what was this allowed to do in March" stays
    answerable.
    """
    rules = await standing_approvals.list_for_user(
        db, user.id, include_revoked=include_revoked
    )
    return StandingApprovalListOut(
        items=[_standing_out(rule) for rule in rules],
        always_asks=standing_approvals.MONEY_AND_DESTRUCTION_ALWAYS_ASK,
    )


@router.post("/standing-approvals/{rule_id}/revoke", response_model=StandingApprovalOut)
async def revoke_standing_approval(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StandingApprovalOut:
    """Take one permission back. One call, and it stops applying immediately.

    "Immediately" is structural rather than a promise: the gate reads the live
    rows on every held-risk step, and `revoked_at` is part of the partial unique
    index it reads, so there is no cache to invalidate and no window in which a
    revoked rule still matches.

    404 rather than 403 for somebody else's rule — the same rule the rest of this
    API follows, so the response does not confirm that the id exists.
    """
    rule = await db.get(StandingApproval, rule_id)
    if rule is None or rule.owner_user_id != user.id:
        raise AppError(404, "not_found", "No such standing approval")
    await standing_approvals.revoke(db, rule, actor_user_id=user.id)
    await db.commit()
    await db.refresh(rule)
    return _standing_out(rule)


@router.post("/approvals/{approval_id}/expire", response_model=ApprovalOut)
async def expire_approval(
    approval_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApprovalOut:
    """Mark a pending approval as expired (used by the sweeper)."""
    approval = await get_visible_approval(db, approval_id, user, for_decision=True)
    if approval.status != "pending":
        raise AppError(409, "approval_not_pending", f"Approval is already {approval.status}")
    approval.status = "expired"
    approval.decided_at = datetime.now(timezone.utc)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=approval.bot_id,
            event_type="approval_expired",
            detail={"approval_id": str(approval.id)},
        )
    )
    await db.commit()
    await db.refresh(approval)
    return _approval_out(approval)
