"""Threads, messages, and the two SSE streams."""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import delete, select, update
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
    get_visible_bot,
    iter_until_disconnect,
    normalise_stream_chunk,
    optional_service,
    orchestrator,
    sse_event,
)
from app.schemas import (
    CreateThreadIn,
    MessageOut,
    MessageSearchHit,
    OkOut,
    SendMessageIn,
    ThreadBotsIn,
    ThreadOut,
    UpdateThreadIn,
)
from app.services import attachments as attachment_svc

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


async def _bot_ids_by_thread(
    db: AsyncSession, thread_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Roster for many threads in one round trip.

    This used to be a per-thread SELECT called from inside the `GET /threads`
    loop, which made the endpoint the desktop shell refetches most into 1+N
    queries — opening the app with 200 threads was 201 round trips to draw a
    sidebar. `routers/work_items.py` names this exact mistake in its own words
    and then does it correctly; this is the same fix.

    `bot_id` is ordered explicitly. The old query had no ORDER BY at all, so the
    order the client saw was whatever Postgres returned — in practice insertion
    order for a table this small, but nothing guaranteed it, and a grouped query
    changes the plan. Pinning it means the payload cannot start varying between
    two identical requests.
    """
    if not thread_ids:
        return {}
    rows = await db.execute(
        select(ThreadBot.thread_id, ThreadBot.bot_id)
        .where(ThreadBot.thread_id.in_(thread_ids))
        .order_by(ThreadBot.thread_id, ThreadBot.bot_id)
    )
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {tid: [] for tid in thread_ids}
    for thread_id, bot_id in rows.all():
        grouped.setdefault(thread_id, []).append(bot_id)
    return grouped



async def _thread_bot_ids(db: AsyncSession, thread_id: uuid.UUID) -> list[uuid.UUID]:
    """One thread's roster, ordered. See `_bot_ids_by_thread` for the list view."""
    rows = await db.execute(
        select(ThreadBot.bot_id)
        .where(ThreadBot.thread_id == thread_id)
        .order_by(ThreadBot.bot_id)
    )
    return list(rows.scalars().all())

@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ThreadOut]:
    # Pinned first, then most recently spoken in. A pin is a preference and
    # the sort is where it takes effect; nothing else reads the flag.
    result = await db.execute(
        select(Thread)
        .where(Thread.owner_user_id == user.id)
        .order_by(Thread.pinned.desc(), Thread.updated_at.desc())
    )
    threads = list(result.scalars().all())
    rosters = await _bot_ids_by_thread(db, [t.id for t in threads])
    return [
        ThreadOut(
            id=t.id,
            title=t.title,
            bot_ids=rosters.get(t.id, []),
            pinned=t.pinned,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in threads
    ]


@router.post("/threads", response_model=ThreadOut)
async def create_thread(
    body: CreateThreadIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThreadOut:
    if not body.bot_ids:
        raise AppError(400, "bot_ids_required", "bot_ids required")

    # Every `bot_ids` entry is checked before anything is written, because
    # `thread_bots.bot_id` was being trusted twice over:
    #
    # * as a foreign key — an id that does not exist (a bot deleted in another
    #   window, a stale client cache) reached the INSERT and came back as an
    #   IntegrityError, which `app.errors`' catch-all renders as
    #   `{"detail": "internal_error", "code": "internal_error", …}`. An ordinary
    #   user action is not an internal error, and the envelope exists so a client
    #   can tell the difference.
    # * as an authorization decision — nothing here asked whether the caller may
    #   *see* the bot, and membership is trusted at read time: the orchestrator's
    #   roster query joins `thread_bots` with no visibility predicate. Another
    #   user's custom bot, accepted here, becomes a participant that answers with
    #   its own system prompt and its own connector set. `routers/deps.py`
    #   declares visibility as the model of the whole API; this was the write
    #   side that never enforced it.
    #
    # Duplicates are dropped rather than rejected: `thread_bots` has a composite
    # primary key, so `[x, x]` was a second way to turn a request into a 500, and
    # "this bot twice" has one obvious meaning.
    bot_ids: list[uuid.UUID] = []
    for bid in body.bot_ids:
        if bid in bot_ids:
            continue
        await get_visible_bot(db, bid, user)  # 404 bot_not_found, never a 500
        bot_ids.append(bid)

    thread = Thread(title=body.title or "New thread", owner_user_id=user.id)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    for bid in bot_ids:
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
        bot_ids=bot_ids,
        pinned=thread.pinned,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


@router.patch("/threads/{thread_id}", response_model=ThreadOut)
async def update_thread(
    thread_id: uuid.UUID,
    body: UpdateThreadIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThreadOut:
    """Rename or pin a conversation.

    Neither touches `updated_at` on purpose: that column means "last spoken
    in" and drives the sidebar order. Renaming a thread you last used a month
    ago must not float it to the top as if somebody had just replied.
    """
    thread = await get_owned_thread(db, thread_id, user)
    changed: dict[str, Any] = {}
    if body.title is not None and body.title.strip() != thread.title:
        thread.title = body.title.strip()
        changed["title"] = thread.title
    if body.pinned is not None and body.pinned != thread.pinned:
        thread.pinned = body.pinned
        changed["pinned"] = thread.pinned
    if changed:
        # `onupdate=clock_timestamp()` fires on any UPDATE of this row unless
        # the column is part of the SET list; writing the old value back
        # explicitly is what keeps "last spoken in" honest.
        kept = thread.updated_at
        db.add(
            AuditEvent(
                actor_user_id=user.id,
                event_type="thread_updated",
                detail={"thread_id": str(thread.id), **changed},
            )
        )
        await db.flush()
        await db.execute(
            update(Thread).where(Thread.id == thread.id).values(updated_at=kept)
        )
        await db.commit()
        await db.refresh(thread)
    return ThreadOut(
        id=thread.id,
        title=thread.title,
        bot_ids=await _thread_bot_ids(db, thread.id),
        pinned=thread.pinned,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


#: `GET /threads/search` never returns more than this many hits, whatever
#: `limit` says. Search is for finding a conversation again, not for export.
SEARCH_MAX_LIMIT = 100
SEARCH_SNIPPET_RADIUS = 80


def _snippet(content: str, needle: str, radius: int = SEARCH_SNIPPET_RADIUS) -> str:
    """A window of `content` around the first case-insensitive `needle`."""
    text = " ".join((content or "").split())
    at = text.lower().find(needle.lower())
    if at < 0:
        return text[: radius * 2] + ("…" if len(text) > radius * 2 else "")
    start = max(0, at - radius)
    end = min(len(text), at + len(needle) + radius)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def _like_pattern(needle: str) -> str:
    """`%needle%` with LIKE's own metacharacters escaped (backslash escape)."""
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/threads/search", response_model=list[MessageSearchHit])
async def search_messages(
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=30, ge=1, le=SEARCH_MAX_LIMIT),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[MessageSearchHit]:
    """Find messages across every thread the caller owns.

    Substring match, case-insensitive, newest first — backed by the trigram
    index `sql/init.sql` creates on `messages.content`. Ownership is a join,
    not a post-filter: a message in somebody else's thread is never read.
    Tool output (`role = tool`) is excluded; nobody searches for a payload.
    """
    needle = q.strip()
    rows = await db.execute(
        select(Message, Thread.title)
        .join(Thread, Thread.id == Message.thread_id)
        .where(
            Thread.owner_user_id == user.id,
            Message.role != "tool",
            Message.content.ilike(_like_pattern(needle), escape="\\"),
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return [
        MessageSearchHit(
            thread_id=message.thread_id,
            thread_title=title,
            message_id=message.id,
            role=message.role,
            bot_id=message.bot_id,
            snippet=_snippet(message.content, needle),
            created_at=message.created_at,
        )
        for message, title in rows.all()
    ]


@router.get("/threads/{thread_id}/messages/{message_id}/attachments/{index}")
async def get_attachment(
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    index: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """The bytes of one attachment. `GET /messages` lists them without data."""
    await get_owned_thread(db, thread_id, user)
    message = await db.get(Message, message_id)
    if message is None or message.thread_id != thread_id:
        raise AppError(404, "message_not_found", "Message not found")
    found = attachment_svc.attachment_bytes(message.meta, index)
    if found is None:
        raise AppError(404, "attachment_not_found", "Attachment not found")
    data, media_type, name = found
    safe_name = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name)[:120] or "file"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Cache-Control": "private, max-age=3600",
            # Everything here is an <img> or a download, never a page: no
            # sniffing a text file into something that runs.
            "X-Content-Type-Options": "nosniff",
        },
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


@router.post("/threads/{thread_id}/bots", response_model=ThreadOut)
async def add_thread_bots(
    thread_id: uuid.UUID,
    body: ThreadBotsIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThreadOut:
    """Seat more bots on an existing thread.

    The roster is not decoration: it is the only thing that makes delegation
    possible. `orchestrator._delegate_targets` is "everyone else in this room",
    so a one-bot thread means `delegate_to_bot` is never even advertised, and a
    chief of staff asked to hand work over holds no tool that can. Until this
    endpoint existed a roster could only be set at creation, and
    `apps/desktop/src/components/ChatPane.tsx` creates every thread with exactly
    one bot - so in the shipped product no thread ever had a second
    participant, and every report of "it never delegated" was that.

    Additive and idempotent. A bot already seated is not an error and not a
    duplicate row (`thread_bots` has a composite primary key, so a second
    insert would be a 500), which matters because the obvious client behaviour
    is to send the whole intended roster rather than the difference.

    Visibility is checked per bot, the same way `create_thread` does it: a
    membership row is trusted at read time by a query with no visibility
    predicate in it, so this is the write side of that boundary. A bot the
    caller cannot see is a 404, never a silent skip.
    """
    thread = await get_owned_thread(db, thread_id, user)
    if not body.bot_ids:
        raise AppError(400, "bot_ids_required", "bot_ids required")

    seated = set(await _thread_bot_ids(db, thread.id))
    added: list[uuid.UUID] = []
    for bot_id in body.bot_ids:
        if bot_id in seated:
            continue
        await get_visible_bot(db, bot_id, user)  # 404 bot_not_found, never a 500
        db.add(ThreadBot(thread_id=thread.id, bot_id=bot_id))
        seated.add(bot_id)
        added.append(bot_id)

    if added:
        thread.updated_at = datetime.now(timezone.utc)
        db.add(
            AuditEvent(
                actor_user_id=user.id,
                event_type="thread_bots_added",
                detail={"thread_id": str(thread.id), "bot_ids": [str(b) for b in added]},
            )
        )
        await db.commit()
        await db.refresh(thread)

    return ThreadOut(
        id=thread.id,
        title=thread.title,
        bot_ids=await _thread_bot_ids(db, thread.id),
        pinned=thread.pinned,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


@router.delete("/threads/{thread_id}/bots/{bot_id}", response_model=ThreadOut)
async def remove_thread_bot(
    thread_id: uuid.UUID,
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThreadOut:
    """Unseat one bot, unless it is the last one.

    A thread with no bots cannot answer anything - `_turn` raises "thread has
    no bots" - so emptying the roster would turn a conversation into a dead
    page with no way back. Refused as a 409 rather than silently ignored,
    because the caller asked for something specific and got nothing.

    History stays. Messages that bot already wrote are what happened, and
    removing it from the roster does not un-say them.
    """
    thread = await get_owned_thread(db, thread_id, user)
    seated = await _thread_bot_ids(db, thread.id)
    if bot_id not in seated:
        raise AppError(404, "bot_not_in_thread", "that bot is not on this thread")
    if len(seated) <= 1:
        raise AppError(
            409,
            "last_bot_in_thread",
            "a thread needs at least one bot to answer in it - add another before "
            "removing this one, or delete the thread",
        )

    await db.execute(
        delete(ThreadBot).where(ThreadBot.thread_id == thread.id, ThreadBot.bot_id == bot_id)
    )
    thread.updated_at = datetime.now(timezone.utc)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="thread_bot_removed",
            detail={"thread_id": str(thread.id), "bot_id": str(bot_id)},
        )
    )
    await db.commit()
    await db.refresh(thread)
    return ThreadOut(
        id=thread.id,
        title=thread.title,
        bot_ids=await _thread_bot_ids(db, thread.id),
        pinned=thread.pinned,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


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

    attachments = attachment_svc.validate_attachments(body.attachments)
    result = await orchestrator.handle_user_message(
        db,
        user=user,
        thread=thread,
        content=body.content,
        mention_bot_ids=body.mention_bot_ids or None,
        attachments=attachments or None,
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
    # Rejected here, as a 400 the client can read, rather than inside the
    # stream where the only voice left is an `error` frame.
    attachments = attachment_svc.validate_attachments(body.attachments) or None

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
                attachments=attachments,
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
