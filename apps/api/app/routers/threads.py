"""Threads, messages, and the two SSE streams."""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.auth import get_current_user
from app.db import SessionLocal, get_db
from app.errors import AppError
from app.models import Approval, AuditEvent, Message, Run, Thread, ThreadBot, User
from app.routers.deps import (
    REQUESTED_BY_KEY,
    RUN_REQUESTED_BY_KEY,
    SSE_HEADERS,
    SSE_PING_SECONDS,
    get_owned_thread,
    iter_until_disconnect,
    normalise_stream_chunk,
    optional_service,
    orchestrator,
    sse_event,
)
from app.schemas import CreateThreadIn, MessageOut, OkOut, SendMessageIn, ThreadOut

logger = logging.getLogger("nesqbot.threads")

router = APIRouter(tags=["threads"])

TERMINAL_EVENTS = {"done", "error"}

# ---------------------------------------------------------------------------
# Idempotency (best effort, in-process)
# ---------------------------------------------------------------------------

IDEMPOTENCY_TTL_SECONDS = 300.0
IDEMPOTENCY_MAX_ENTRIES = 512
_idempotency_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def _idempotency_get(key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    entry = _idempotency_cache.get(key)
    if entry is None:
        return None
    stored_at, value = entry
    if now - stored_at > IDEMPOTENCY_TTL_SECONDS:
        _idempotency_cache.pop(key, None)
        return None
    _idempotency_cache.move_to_end(key)
    return value


def _idempotency_put(key: str, value: dict[str, Any]) -> None:
    now = time.monotonic()
    for stale in [k for k, (t, _) in _idempotency_cache.items() if now - t > IDEMPOTENCY_TTL_SECONDS]:
        _idempotency_cache.pop(stale, None)
    _idempotency_cache[key] = (now, value)
    _idempotency_cache.move_to_end(key)
    while len(_idempotency_cache) > IDEMPOTENCY_MAX_ENTRIES:
        _idempotency_cache.popitem(last=False)


async def _thread_bot_ids(db: AsyncSession, thread_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(select(ThreadBot.bot_id).where(ThreadBot.thread_id == thread_id))
    return list(rows.scalars().all())


@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ThreadOut]:
    result = await db.execute(
        select(Thread).where(Thread.owner_user_id == user.id).order_by(Thread.updated_at.desc())
    )
    out: list[ThreadOut] = []
    for t in result.scalars().all():
        out.append(
            ThreadOut(
                id=t.id,
                title=t.title,
                bot_ids=await _thread_bot_ids(db, t.id),
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
        )
    return out


@router.post("/threads", response_model=ThreadOut)
async def create_thread(
    body: CreateThreadIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThreadOut:
    if not body.bot_ids:
        raise AppError(400, "bot_ids_required", "bot_ids required")
    thread = Thread(title=body.title or "New thread", owner_user_id=user.id)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    for bid in body.bot_ids:
        db.add(ThreadBot(thread_id=thread.id, bot_id=bid))
    await db.commit()
    if body.initial_message:
        await orchestrator.handle_user_message(
            db, user=user, thread=thread, content=body.initial_message
        )
        await db.refresh(thread)
    return ThreadOut(
        id=thread.id,
        title=thread.title,
        bot_ids=body.bot_ids,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


#: Note stamped on an approval the thread deletion expired. Prefixed so the
#: reason is obvious in the queue and in the audit trail.
THREAD_DELETED_NOTE = "Expired automatically: the originating thread {thread_id} was deleted."


async def _expire_approvals_for_thread(
    db: AsyncSession, thread: Thread, actor: User
) -> list[Approval]:
    """Close out the approvals hanging off a thread that is about to disappear.

    Messages are conversation and may cascade away with the thread, but an
    approval is the record of what a bot was authorised to do and whether a human
    said yes; it has to outlive the conversation. `runs.thread_id` and
    `approvals.run_id` are both ON DELETE SET NULL so the rows survive - this
    fills in the two things the database cannot:

    * every **pending** approval is explicitly expired with a note, so it is
      never silently dropped from the queue. Already-decided approvals keep
      their status untouched.
    * the resolved owner is stamped into the payload as `requested_by` when it
      is not there yet. `resolve_approval_owner` would otherwise fall through
      the thread-owner branch once the thread is gone and land on the bot owner
      - or, for a shared system bot, on nobody at all, which under the scoping
      rules makes the approval readable by anyone who can see the bot.
    * the same stamp is written onto the orphaned **runs**, into
      `context_ledger`, for the identical reason on the `resolve_run_owner`
      side. Without it the owner loses sight of their own run history.

    Returns the approvals it expired. Nothing is committed here; the caller
    commits the expiry, the audit events and the delete together.
    """
    runs = list((await db.execute(select(Run).where(Run.thread_id == thread.id))).scalars().all())
    if not runs:
        return []

    # Same reasoning as the approvals below, for the runs themselves. Once
    # thread_id is NULL, resolve_run_owner has nothing left to resolve for a
    # system-bot run - which hides it from its own owner and, worse, makes an
    # orphaned chat run indistinguishable from an unattended cron run.
    for run in runs:
        ledger = dict(run.context_ledger or {})
        if not ledger.get(RUN_REQUESTED_BY_KEY):
            ledger[RUN_REQUESTED_BY_KEY] = str(thread.owner_user_id)
            run.context_ledger = ledger

    run_ids = [run.id for run in runs]

    rows = await db.execute(select(Approval).where(Approval.run_id.in_(run_ids)))
    expired: list[Approval] = []
    now = datetime.now(timezone.utc)

    for approval in rows.scalars().all():
        payload = dict(approval.payload or {})
        if not payload.get(REQUESTED_BY_KEY):
            # The thread is the only thing that still resolves this owner.
            payload[REQUESTED_BY_KEY] = str(thread.owner_user_id)
            approval.payload = payload

        if approval.status != "pending":
            continue

        approval.status = "expired"
        approval.decided_at = now
        approval.note = THREAD_DELETED_NOTE.format(thread_id=thread.id)
        expired.append(approval)
        db.add(
            AuditEvent(
                actor_user_id=actor.id,
                bot_id=approval.bot_id,
                event_type="approval_expired",
                detail={
                    "approval_id": str(approval.id),
                    "run_id": str(approval.run_id) if approval.run_id else None,
                    "reason": "thread_deleted",
                    "thread_id": str(thread.id),
                },
            )
        )

    return expired


@router.delete("/threads/{thread_id}", response_model=OkOut)
async def delete_thread(
    thread_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    """Delete a thread. Messages, thread_bots and the ledger cascade away with it.

    Runs and approvals do **not**: they are audit records. Their link to the
    thread is nulled, and any pending approval is expired with a note first so
    the human-in-the-loop gate leaves a trace instead of vanishing.
    """
    thread = await get_owned_thread(db, thread_id, user)
    expired = await _expire_approvals_for_thread(db, thread, user)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="thread_deleted",
            detail={
                "thread_id": str(thread_id),
                "expired_approval_ids": [str(a.id) for a in expired],
                "expired_approvals": len(expired),
            },
        )
    )
    await db.delete(thread)
    await db.commit()
    detail = "deleted"
    if expired:
        detail = f"deleted; {len(expired)} pending approval(s) expired"
    return OkOut(ok=True, detail=detail)


@router.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
async def list_messages(
    thread_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Message]:
    await get_owned_thread(db, thread_id, user)
    # `id` is a tiebreaker, not the fix: `created_at` now defaults to
    # `clock_timestamp()` so two messages cannot share an instant (see
    # `app.models`). It is here so this endpoint has a *total* order and can
    # never hand a client the same thread in two different sequences.
    result = await db.execute(
        select(Message)
        .where(Message.thread_id == thread_id)
        .order_by(Message.created_at, Message.id)
    )
    return list(result.scalars().all())


@router.post("/threads/{thread_id}/messages")
async def send_message(
    thread_id: uuid.UUID,
    body: SendMessageIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Non-streaming turn.

    Honours an optional `Idempotency-Key` (the worker sends
    `{workflow_id}:{run_id}:{activity_id}`) with a short-lived in-process cache so
    an activity retry replays the first response instead of re-running the turn.
    """
    thread = await get_owned_thread(db, thread_id, user)

    cache_key = f"{user.id}:{thread_id}:{idempotency_key}" if idempotency_key else None
    if cache_key:
        cached = _idempotency_get(cache_key)
        if cached is not None:
            logger.info("idempotent replay for key=%s thread=%s", idempotency_key, thread_id)
            return cached

    result = await orchestrator.handle_user_message(
        db,
        user=user,
        thread=thread,
        content=body.content,
        mention_bot_ids=body.mention_bot_ids or None,
    )
    if cache_key:
        _idempotency_put(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


@router.post("/threads/{thread_id}/messages/stream")
async def stream_message(
    thread_id: uuid.UUID,
    body: SendMessageIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EventSourceResponse:
    """Stream one assistant turn as `text/event-stream`.

    Events: `token` {delta}, `handoff` {bot_id,bot_name}, `tool` {connector,action,ok},
    `desktop` {phase,detail,outcome}, `approval` {approval_id,title},
    `takeover` {phase,run_id,reason,what_you_need,resume_url},
    `done` {message_id,bot_id,tier,cost_usd,run_id,awaiting_human},
    `error` {detail}. A terminal `done` or `error` is always emitted.

    `takeover` with `phase: "requested"` is the one a client must handle rather
    than log: the run is parked in `awaiting_human` and will stay there until
    someone signs in on the live desktop and posts to the `resume_url`.
    """
    await get_owned_thread(db, thread_id, user)
    if getattr(orchestrator, "handle_user_message_stream", None) is None:
        raise AppError(
            503,
            "streaming_unavailable",
            "orchestrator.handle_user_message_stream is not implemented in this build",
        )

    user_id = user.id
    content = body.content
    mentions = body.mention_bot_ids or None

    async def producer() -> AsyncIterator[Any]:
        # FastAPI closes `yield` dependencies before the streaming body runs, so
        # the stream owns its own session.
        async with SessionLocal() as sdb:
            sthread = await sdb.get(Thread, thread_id)
            suser = await sdb.get(User, user_id)
            if sthread is None or suser is None:
                yield ("error", {"detail": "thread not found"})
                return
            async for chunk in orchestrator.handle_user_message_stream(
                sdb,
                user=suser,
                thread=sthread,
                content=content,
                mention_bot_ids=mentions,
            ):
                yield chunk

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        saw_terminal = False
        try:
            async for chunk in iter_until_disconnect(request, producer()):
                event, data = normalise_stream_chunk(chunk)
                if event in TERMINAL_EVENTS:
                    saw_terminal = True
                yield sse_event(event, data)
        except Exception as exc:  # noqa: BLE001 - surface as a terminal SSE event
            logger.exception("message stream failed for thread %s", thread_id)
            saw_terminal = True
            yield sse_event("error", {"detail": str(exc)})
        finally:
            if not saw_terminal:
                yield sse_event("done", {"thread_id": str(thread_id)})

    return EventSourceResponse(
        event_stream(),
        headers=dict(SSE_HEADERS),
        ping=SSE_PING_SECONDS,
    )


@router.get("/threads/{thread_id}/events")
async def thread_events(
    thread_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EventSourceResponse:
    """Subscribe to events pushed onto this thread by the worker or routines."""
    await get_owned_thread(db, thread_id, user)
    events = optional_service("events")
    if events is None or getattr(events, "subscribe", None) is None:
        raise AppError(
            503,
            "events_unavailable",
            "app.services.events is not available in this build",
        )
    channel = events.thread_channel(thread_id)

    async def source() -> AsyncIterator[Any]:
        subscription = events.subscribe(channel)
        if hasattr(subscription, "__aiter__"):
            iterator = subscription
        else:  # a coroutine returning the iterator
            iterator = await subscription
        async for item in iterator:
            yield item

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        saw_terminal = False
        try:
            async for chunk in iter_until_disconnect(request, source()):
                event, data = normalise_stream_chunk(chunk)
                if event in TERMINAL_EVENTS:
                    saw_terminal = True
                yield sse_event(event, data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("thread event stream failed for %s", thread_id)
            saw_terminal = True
            yield sse_event("error", {"detail": str(exc)})
        finally:
            if not saw_terminal:
                yield sse_event("done", {"thread_id": str(thread_id), "reason": "closed"})

    return EventSourceResponse(
        event_stream(),
        headers=dict(SSE_HEADERS),
        ping=SSE_PING_SECONDS,
    )
