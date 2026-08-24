"""Work items: owned, transferable units of work, and the ledger of who held them.

A work item is the thing that did not exist before this module. There was memory
and there was knowledge, but nothing with an owner and a state, so "the lead bot
hands this lead to the sales bot" had no object to hand over — only a sentence in
a chat thread, which no report can be run against and no reviewer can audit.

Scoping
-------
Owner-scoped to the human, like threads: ``work_items.owner_user_id`` is set at
creation and never moves. What moves is ``owner_bot_id``, and only through
``POST /work-items/{id}/transfer``. Not-yours is **404**, never 403, per
``deps.py``.

This entity needs none of the fallback chains ``resolve_run_owner`` and
``resolve_approval_owner`` carry. Those exist because a run or an approval can
genuinely have no knowable human — a cron-fired routine step against a shared
system bot. A work item always has one: it is created by an authenticated
request, and the one thing that will write to it unattended (an inbound reply,
third lane) attaches to a row that already has an owner. So the scope is a
single column comparison, which is also why it is a plain index rather than a
five-way coalesce.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.errors import AppError
from app.models import AuditEvent, User, WorkItem, WorkItemKey, WorkItemTransfer
from app.routers.deps import get_owned_thread, get_visible_bot
from app.schemas import (
    OkOut,
    UpdateWorkItemIn,
    WorkItemIn,
    WorkItemKeyOut,
    WorkItemOut,
    WorkItemTransferIn,
    WorkItemTransferOut,
    WorkItemTransferResultOut,
)
from app.services import work_items as work_items_service

logger = logging.getLogger("nesqbot.work_items")

router = APIRouter(tags=["work-items"])


async def _get_owned_work_item(db: AsyncSession, work_item_id: uuid.UUID, user: User) -> WorkItem:
    """Load a work item the caller owns, or 404.

    404 and not 403 on someone else's row: a 403 confirms the id exists, which
    is exactly the fact another tenant must not be able to probe for. Same rule
    and same reason as ``get_owned_thread``.
    """
    item = await db.get(WorkItem, work_item_id)
    if item is None or item.owner_user_id != user.id:
        raise AppError(404, "work_item_not_found", "Work item not found")
    return item


def _render(item: WorkItem, keys: list[WorkItemKey]) -> WorkItemOut:
    """Render one work item against keys the caller already loaded.

    Built by hand rather than through a relationship because ``models.py``
    declares no ``relationship()`` anywhere — every association in this schema is
    loaded explicitly, and a single lazy-loading attribute would be the one that
    raises ``MissingGreenlet`` under async the first time something touched it
    outside an awaited load.
    """
    return WorkItemOut(
        id=item.id,
        type=item.type,
        title=item.title,
        summary=item.summary,
        status=item.status,
        resolution=item.resolution,
        owner_bot_id=item.owner_bot_id,
        owner_user_id=item.owner_user_id,
        thread_id=item.thread_id,
        detail=item.detail or {},
        keys=[WorkItemKeyOut(channel=k.channel, value=k.value) for k in keys],
        created_at=item.created_at,
        updated_at=item.updated_at,
        transferred_at=item.transferred_at,
        last_event_at=item.last_event_at,
        closed_at=item.closed_at,
    )


async def _out(db: AsyncSession, item: WorkItem) -> WorkItemOut:
    return _render(item, await work_items_service.keys_for(db, item.id))


async def _out_many(db: AsyncSession, items: list[WorkItem]) -> list[WorkItemOut]:
    """Render a page of work items with **one** query for all of their keys.

    The obvious loop over ``_out`` is a query per row, which on the default page
    of 50 is 51 round trips to render a list nobody reads past the top of.
    """
    if not items:
        return []
    result = await db.execute(
        select(WorkItemKey)
        .where(WorkItemKey.work_item_id.in_([item.id for item in items]))
        .order_by(WorkItemKey.channel, WorkItemKey.value)
    )
    by_item: dict[uuid.UUID, list[WorkItemKey]] = {}
    for key in result.scalars().all():
        by_item.setdefault(key.work_item_id, []).append(key)
    return [_render(item, by_item.get(item.id, [])) for item in items]


async def _replace_keys(db: AsyncSession, item: WorkItem, keys) -> None:
    """Set the item's external identities to exactly ``keys``.

    Replace rather than merge: the caller sent the full set it believes in, and
    a merge would make removing a stale address impossible through this API.
    Duplicates within one request are collapsed — the primary key would reject
    the second insert and take the whole request with it, and a client listing
    the same address twice means one address, not an error.
    """
    await db.execute(delete(WorkItemKey).where(WorkItemKey.work_item_id == item.id))
    seen: set[tuple[str, str]] = set()
    for key in keys:
        channel, value = work_items_service.normalise_key(key.channel, key.value)
        if not channel or not value or (channel, value) in seen:
            continue
        seen.add((channel, value))
        db.add(
            WorkItemKey(
                work_item_id=item.id,
                channel=channel,
                value=value,
                owner_user_id=item.owner_user_id,
            )
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("/work-items", response_model=list[WorkItemOut])
async def list_work_items(
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    owner_bot_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[WorkItemOut]:
    """The caller's work items, newest first.

    ``owner_bot_id`` answers "what is this bot holding?" — the queue view. It is
    filtered rather than validated against bot visibility on purpose: the scope
    is already the caller's own rows, so a bot id they cannot see simply matches
    nothing, and a 404 here would confirm which bot ids exist.
    """
    stmt = select(WorkItem).where(WorkItem.owner_user_id == user.id)
    if type is not None:
        stmt = stmt.where(WorkItem.type == type)
    if status is not None:
        stmt = stmt.where(WorkItem.status == status)
    if owner_bot_id is not None:
        stmt = stmt.where(WorkItem.owner_bot_id == owner_bot_id)
    stmt = stmt.order_by(WorkItem.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return await _out_many(db, list(result.scalars().all()))


@router.post("/work-items", response_model=WorkItemOut)
async def create_work_item(
    body: WorkItemIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WorkItemOut:
    """Create a work item already owned by a bot.

    There is no unowned-then-assigned two-step. An item with no bot is the state
    a *deleted* bot leaves behind, not a state anyone should be able to create,
    and allowing it would mean the first ledger row is optional.

    The opening assignment is written to ``work_item_transfers`` with
    ``from_bot_id = NULL``. Reading the ledger therefore answers "who has held
    this" completely, rather than answering it with the first holder missing —
    which is the failure mode of every audit trail that only records changes.
    """
    bot = await get_visible_bot(db, body.owner_bot_id, user)
    if body.thread_id is not None:
        # Thread membership is the caller's, so an item cannot be pinned to
        # someone else's conversation and drag its updates in front of them.
        await get_owned_thread(db, body.thread_id, user)

    item = WorkItem(
        type=body.type,
        title=body.title,
        summary=body.summary,
        owner_bot_id=bot.id,
        owner_user_id=user.id,
        thread_id=body.thread_id,
        detail=dict(body.detail or {}),
    )
    work_items_service.apply_status(item, body.status)
    db.add(item)
    # Flush, not commit: the keys and the ledger row need `item.id`, and all
    # three have to land together or not at all.
    await db.flush()

    await _replace_keys(db, item, body.keys)
    # Written here rather than through `services.work_items.transfer_work_item`,
    # which correctly refuses a no-op: the item is *constructed* owned, so from
    # that function's point of view nothing moves. The opening row is the one
    # ledger entry that is not a change of hands, and it is written by the code
    # that knows creation happened.
    #
    # `transferred_at` stays NULL. It means "last handed over", and this item
    # never has been; `created_at` already says when it was assigned.
    db.add(
        WorkItemTransfer(
            work_item_id=item.id,
            owner_user_id=user.id,
            from_bot_id=None,
            to_bot_id=bot.id,
            actor_user_id=user.id,
            reason=body.reason,
            source=work_items_service.SOURCE_CREATE,
        )
    )
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot.id,
            event_type="work_item_created",
            detail={
                "work_item_id": str(item.id),
                "type": item.type,
                "status": item.status,
                "keys": len(body.keys),
            },
        )
    )
    await db.commit()
    await db.refresh(item)
    return await _out(db, item)


@router.get("/work-items/{work_item_id}", response_model=WorkItemOut)
async def get_work_item(
    work_item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WorkItemOut:
    return await _out(db, await _get_owned_work_item(db, work_item_id, user))


@router.patch("/work-items/{work_item_id}", response_model=WorkItemOut)
async def update_work_item(
    work_item_id: uuid.UUID,
    body: UpdateWorkItemIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WorkItemOut:
    """Update the item's own fields. Ownership is not one of them.

    ``owner_bot_id`` is **refused** here with a 422 pointing at ``/transfer``,
    not quietly dropped — see ``UpdateWorkItemIn``. Moving it through PATCH
    would bypass the ledger, and a ledger with a bypass is not a ledger; but
    ignoring the field would be worse than either, because the caller gets a 200
    and believes the handover happened.
    """
    item = await _get_owned_work_item(db, work_item_id, user)
    changes = body.model_dump(exclude_unset=True)

    if "thread_id" in changes and changes["thread_id"] is not None:
        await get_owned_thread(db, changes["thread_id"], user)

    for field, value in changes.items():
        if field == "keys":
            if value is not None:
                await _replace_keys(db, item, body.keys or [])
        elif field == "status":
            if value is not None:
                work_items_service.apply_status(item, value)
        elif field == "thread_id":
            # Explicit None means "detach", which is a real edit — so this one
            # field is assigned even when null, unlike the rest.
            item.thread_id = value
        elif value is not None:
            setattr(item, field, value)

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=item.owner_bot_id,
            event_type="work_item_updated",
            # Field names and the resulting state only. `detail` and `keys` hold
            # whatever a connector or a person put in them, and an audit event
            # is read more widely than the row it describes.
            detail={
                "work_item_id": str(item.id),
                "fields": sorted(changes.keys()),
                "status": item.status,
            },
        )
    )
    await db.commit()
    await db.refresh(item)
    return await _out(db, item)


@router.delete("/work-items/{work_item_id}", response_model=OkOut)
async def delete_work_item(
    work_item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    """Delete the item and its keys. **The transfer ledger survives.**

    ``work_item_transfers`` carries no foreign key to ``work_items`` precisely
    so this is true — same construction as ``audit_events`` and ``action_log``.
    Deleting a lead must not delete the record that someone handed it to Sales
    on a Tuesday, or the delete becomes the way to erase the audit trail.
    """
    item = await _get_owned_work_item(db, work_item_id, user)
    bot_id = item.owner_bot_id
    await db.delete(item)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="work_item_deleted",
            detail={"work_item_id": str(work_item_id), "type": item.type},
        )
    )
    await db.commit()
    return OkOut(ok=True, detail="deleted")


# ---------------------------------------------------------------------------
# Ownership transfer — the point of the entity
# ---------------------------------------------------------------------------


@router.post("/work-items/{work_item_id}/transfer", response_model=WorkItemTransferResultOut)
async def transfer_work_item(
    work_item_id: uuid.UUID,
    body: WorkItemTransferIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WorkItemTransferResultOut:
    """Hand the item to another bot, and record who, to whom, when and why.

    Not risk-gated, and that is a decision rather than an omission: a transfer
    reaches nothing outside the tenant, is undone by transferring back, and both
    halves are recorded. See ``services/work_items.py`` for the full argument.

    Idempotent. Transferring to the bot that already holds it returns
    ``transferred: false`` and writes no ledger row — a model calling the tool
    twice must not produce two handovers that did not happen.
    """
    item = await _get_owned_work_item(db, work_item_id, user)
    target = await get_visible_bot(db, body.to_bot_id, user)
    if body.actor_bot_id is not None:
        # A bot-initiated handover names the initiator. It is checked for
        # visibility so the ledger cannot be made to name a bot the caller
        # cannot see, which would be a way to read bot ids out of the audit log.
        await get_visible_bot(db, body.actor_bot_id, user)

    if item.status in work_items_service.TERMINAL_STATUSES:
        raise AppError(
            409,
            "work_item_closed",
            "This work item is closed; reopen it before transferring it",
        )

    row = await work_items_service.transfer_work_item(
        db,
        item,
        to_bot_id=target.id,
        reason=body.reason,
        actor_user_id=user.id,
        actor_bot_id=body.actor_bot_id,
        source=work_items_service.SOURCE_API,
        detail=body.detail,
    )
    if row is None:
        # No write happened; nothing to commit and nothing to record.
        return WorkItemTransferResultOut(
            ok=True,
            transferred=False,
            work_item=await _out(db, item),
            detail="That bot already owns this work item",
        )

    await db.commit()
    await db.refresh(item)
    await db.refresh(row)
    return WorkItemTransferResultOut(
        ok=True,
        transferred=True,
        work_item=await _out(db, item),
        transfer=WorkItemTransferOut.model_validate(row),
    )


@router.get("/work-items/{work_item_id}/transfers", response_model=list[WorkItemTransferOut])
async def list_work_item_transfers(
    work_item_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[WorkItemTransfer]:
    """The handover ledger for one item, newest first.

    Reachability is checked against the work item, not against the ledger rows:
    the rows carry no foreign key, so a stale row for a deleted item would
    otherwise be readable by anyone who guessed the id. ``owner_user_id`` on the
    row is what makes a cross-item ledger possible later without that hole.
    """
    item = await _get_owned_work_item(db, work_item_id, user)
    result = await db.execute(
        select(WorkItemTransfer)
        .where(
            WorkItemTransfer.work_item_id == item.id,
            WorkItemTransfer.owner_user_id == user.id,
        )
        .order_by(WorkItemTransfer.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
