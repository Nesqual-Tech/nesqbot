"""Work-item state, ownership transfer, and the inbound-identity resolver.

Why the logic is here and not in the router
-------------------------------------------
Two callers need to move a work item between bots and neither of them is an
HTTP request. The delegation lane's ``delegate_to_bot`` tool runs inside a turn,
and the inbound-events lane's webhook handler will want to re-home an item when
a reply arrives on a channel the current owner does not work. If the transfer
lived in ``routers/work_items.py`` the second and third callers would each write
their own version, and the third would forget the ledger row — which is the one
part that cannot be forgotten, because the ledger *is* the product claim (see
``docs/competitive-analysis.md``: the competitor's audit view is still "coming").

So there is exactly one function that changes ``work_items.owner_bot_id``, and
it cannot do so without writing ``work_item_transfers``.

What is deliberately not here
-----------------------------
No risk classification. ``services.risk.classify_action_risk`` is the single
classifier and ``simulation.perform`` the single chokepoint, and both exist for
*outbound effects* — things that leave the tenant and cannot be taken back. A
transfer reaches nothing outside: it moves a row from one of the customer's own
bots to another, it is fully reversible by transferring back, and both halves
are recorded. Gating it would put a human in front of an action whose entire
purpose is to remove the human from the loop, and would add a second place where
risk is decided. If that judgement is ever revisited, the class belongs in
``risk.py``'s tables and nowhere else.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, WorkItem, WorkItemKey, WorkItemTransfer
from app.schemas import WorkItemStatus

logger = logging.getLogger("nesqbot.work_items")

#: Runtime mirror of the ``WorkItemStatus`` Literal in ``schemas.py``. Derived
#: rather than retyped so the two cannot drift; a test asserts it anyway,
#: because ``get_args`` silently returns ``()`` for a non-Literal.
WORK_ITEM_STATUSES: tuple[str, ...] = tuple(get_args(WorkItemStatus))

#: States from which nothing more is expected. Only ``closed`` stamps
#: ``closed_at`` and only ``closed`` refuses a transfer.
TERMINAL_STATUSES: frozenset[str] = frozenset({"closed"})

#: Ledger ``source`` values this module writes. The delegation lane stamps its
#: own (``"delegation"``) so the ledger says how a handover was triggered, not
#: merely that one was.
SOURCE_CREATE = "create"
SOURCE_API = "api"


def normalise_key(channel: str, value: str) -> tuple[str, str]:
    """Canonical form of an external identity, for both writes and lookups.

    Trim and lowercase, nothing cleverer. Per-channel normalisation is a
    genuinely hard problem — E.164 for phone numbers, gmail's dot-folding, a
    LinkedIn URL with a tracking suffix — and guessing at it here would make
    lookups fail in ways nobody could see. Both the writer and the reader call
    this one function, so at worst a match is missed and the inbound lane falls
    back to its own routing; a wrong normalisation would instead attach one
    customer's reply to another customer's lead.
    """
    return (channel or "").strip().lower(), (value or "").strip().lower()


async def keys_for(db: AsyncSession, work_item_id: uuid.UUID) -> list[WorkItemKey]:
    result = await db.execute(
        select(WorkItemKey)
        .where(WorkItemKey.work_item_id == work_item_id)
        .order_by(WorkItemKey.channel, WorkItemKey.value)
    )
    return list(result.scalars().all())


async def resolve_by_key(
    db: AsyncSession,
    channel: str,
    value: str,
    *,
    owner_user_id: uuid.UUID | None = None,
    include_closed: bool = False,
) -> list[WorkItem]:
    """Work items an inbound event on ``(channel, value)`` could belong to.

    **This is the seam for the inbound-events lane.** A reply lands carrying an
    address, a number or a record id; this turns that into the work item it is
    about, using the ``idx_work_item_keys_lookup`` index.

    It returns a *list*, in a defined order, rather than a single row, and that
    is the deliberate part. ``work_item_keys`` has no unique constraint on
    ``(channel, value)`` because the same person legitimately maps to more than
    one item — two sellers working the same account, or a lead that closed in
    March and came back in August. A unique index would move that collision to
    the moment of *writing the key*, where it would surface as an IntegrityError
    inside a webhook and throw away a real customer reply to defend a modelling
    assumption.

    Ordering, so the caller's behaviour is defined and not left to chance:

    1. still open before closed (a live conversation beats a finished one);
    2. then most recent outside contact (``last_event_at``);
    3. then most recently created.

    Callers wanting the single obvious answer take ``[0]``; callers that can ask
    a human, or that know the tenant from the connector binding the webhook
    arrived on, should pass ``owner_user_id`` and inspect the rest.
    """
    channel, value = normalise_key(channel, value)
    return await _candidates(
        db,
        channel=channel,
        value=value,
        owner_user_id=owner_user_id,
        include_closed=include_closed,
    )


async def resolve_by_value(
    db: AsyncSession,
    value: str,
    *,
    owner_user_id: uuid.UUID,
    include_closed: bool = False,
) -> list[WorkItem]:
    """The same lookup with the channel left unsaid, for a caller that has one string.

    An agent holds "sarah@acme.test" or a LinkedIn URL; it does not hold the
    word ``email``. The obvious fix — guess the channel from the shape of the
    string — is the one thing ``normalise_key``'s docstring warns against, and
    it fails in the direction that hurts: a guess of ``email`` for an address
    that was logged under ``work_email`` finds nothing, silently, and the model
    concludes the lead is new and logs it twice.

    So the channel is simply not part of the predicate. ``owner_user_id`` is
    **required** here rather than optional, because without a channel the
    leading column of ``idx_work_item_keys_owner`` is the only thing keeping
    this off a full scan of every tenant's addresses — and a cross-tenant
    value-only search is not a query anything should be able to ask.

    Ordering is `resolve_by_key`'s, from the same helper, so "most recent
    activity first" means one thing however the caller got here.
    """
    _, value = normalise_key("", value)
    return await _candidates(
        db,
        channel=None,
        value=value,
        owner_user_id=owner_user_id,
        include_closed=include_closed,
    )


async def _candidates(
    db: AsyncSession,
    *,
    channel: str | None,
    value: str,
    owner_user_id: uuid.UUID | None,
    include_closed: bool,
) -> list[WorkItem]:
    """The ordered candidate list both resolvers return. One statement, one order."""
    if not value:
        return []
    stmt = (
        select(WorkItem)
        .join(WorkItemKey, WorkItemKey.work_item_id == WorkItem.id)
        .where(WorkItemKey.value == value)
    )
    if channel is not None:
        stmt = stmt.where(WorkItemKey.channel == channel)
    if owner_user_id is not None:
        stmt = stmt.where(WorkItem.owner_user_id == owner_user_id)
        if channel is None:
            # Only on the value-only path, and only as an index hint: without a
            # channel, `idx_work_item_keys_lookup` cannot be used at all, and
            # this predicate puts the lookup on `idx_work_item_keys_owner`
            # instead of a scan of every tenant's addresses. Adding it to the
            # channel path as well would change a query the inbound lane
            # depends on, to buy nothing.
            stmt = stmt.where(WorkItemKey.owner_user_id == owner_user_id)
    if not include_closed:
        stmt = stmt.where(WorkItem.status.notin_(tuple(TERMINAL_STATUSES)))
    stmt = stmt.order_by(
        WorkItem.closed_at.is_(None).desc(),
        WorkItem.last_event_at.desc().nullslast(),
        WorkItem.created_at.desc(),
    )
    result = await db.execute(stmt)
    return list(result.scalars().unique().all())


async def mark_inbound_event(
    db: AsyncSession,
    item: WorkItem,
    *,
    at: datetime | None = None,
) -> WorkItem:
    """Stamp ``last_event_at`` — "the outside world just did something".

    Separate from ``updated_at`` on purpose. ``updated_at`` moves on any edit,
    including a bot rewriting its own summary, so it cannot answer "has the lead
    replied yet?". This column can, which is what makes a stalled-outreach sweep
    a single indexed query rather than a scan of the message table.

    Does **not** commit, and does not decide what the reply means. Advancing
    ``status``, posting into ``thread_id`` and deciding whether a reply warrants
    a handover are the inbound lane's calls, not this function's.
    """
    item.last_event_at = at or datetime.now(timezone.utc)
    db.add(item)
    return item


async def transfer_work_item(
    db: AsyncSession,
    item: WorkItem,
    *,
    to_bot_id: uuid.UUID,
    reason: str,
    actor_user_id: uuid.UUID | None = None,
    actor_bot_id: uuid.UUID | None = None,
    source: str = SOURCE_API,
    detail: dict[str, Any] | None = None,
) -> WorkItemTransfer | None:
    """Move ownership to ``to_bot_id`` and record that it happened.

    Returns the ledger row, or ``None`` when the target bot already holds the
    item. That second case is a retry, not an error: a model can call a
    delegation tool twice, and a second ledger row would assert a handover that
    never occurred — the ledger has to be *true* before it is complete.

    Callers own the transaction. Nothing here commits, so a transfer that is
    part of a larger unit of work (create the item, hand it over, post a
    message) either lands whole or not at all.

    Visibility of ``to_bot_id`` is the caller's responsibility — the HTTP lane
    checks it with ``get_visible_bot`` — because an in-process caller such as
    ``delegate_to_bot`` has already resolved the target from a roster the user
    can see, and re-deriving "who is asking" down here would mean guessing.
    """
    if item.owner_bot_id == to_bot_id:
        return None

    row = WorkItemTransfer(
        work_item_id=item.id,
        owner_user_id=item.owner_user_id,
        from_bot_id=item.owner_bot_id,
        to_bot_id=to_bot_id,
        actor_user_id=actor_user_id,
        actor_bot_id=actor_bot_id,
        reason=reason,
        source=source,
        detail=dict(detail or {}),
    )
    item.owner_bot_id = to_bot_id
    # Read per row, not per transaction: a create writes the item and this row
    # together, and `now()` would give both the same instant.
    item.transferred_at = datetime.now(timezone.utc)
    db.add(row)
    db.add(item)
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            bot_id=to_bot_id,
            event_type="work_item_transferred",
            # Ids, the reason and the trigger. Never `item.detail` or the
            # caller's `detail` blob: both are free-form and can hold whatever a
            # connector put there, and an audit event is read by more people
            # than the record it describes.
            detail={
                "work_item_id": str(item.id),
                "type": item.type,
                "from_bot_id": str(row.from_bot_id) if row.from_bot_id else None,
                "to_bot_id": str(to_bot_id),
                "reason": reason,
                "source": source,
            },
        )
    )
    return row


def apply_status(item: WorkItem, status: str) -> None:
    """Set ``status`` and keep ``closed_at`` honest about it.

    Reopening clears the stamp rather than leaving a closed date on an open row,
    which is the kind of detail a pipeline report gets wrong for a quarter
    before anyone notices.
    """
    item.status = status
    if status in TERMINAL_STATUSES:
        if item.closed_at is None:
            item.closed_at = datetime.now(timezone.utc)
    else:
        item.closed_at = None
