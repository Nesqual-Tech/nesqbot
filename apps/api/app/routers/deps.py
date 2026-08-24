"""Shared router helpers: service loading, ownership checks, SSE plumbing.

Authorization model
-------------------
System bots (``bots.is_system``) are shared and visible to every authenticated
user. Custom bots are visible only to their ``owner_user_id``. Most things
hanging off a bot (routines, memories, desktops, connector bindings) inherit
that visibility. When the caller may not see an object we raise **404**, never
403, so the API does not leak the existence of another tenant's rows.

**Approvals and runs are the exceptions.** Both carry per-user work and both can
hang off a *shared* system bot, so inheriting bot visibility would hand one
user's row to every authenticated caller. They are scoped by the human behind
them instead - ``resolve_approval_owner`` and ``resolve_run_owner`` - and a row
with no knowable human is not therefore everyone's.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
from collections.abc import AsyncIterator
from types import ModuleType
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import Select, Text, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import Approval, AuditEvent, Bot, Routine, Run, Thread, User
from app.services.desktop import DesktopManager
from app.services.model_router import ModelRouter
from app.services.orchestrator import Orchestrator

logger = logging.getLogger("nesqbot.routers")

#: Advertised API version - also used for the FastAPI app version and /health.
#:
#: This is the *contract* number and moves only when docs/API.md gains, loses or
#: reshapes a route. It says nothing about which image is running - /health
#: reports that separately as "build". Bumped 0.2.0 -> 0.3.0 for the human
#: handoff surface (the `takeover` event, POST /runs/{id}/resume) and the
#: browser tool set.
API_VERSION = "0.3.0"

#: Payload key naming the human who triggered a risk-gated action. Approvals
#: carrying it are only visible to that user; routine-created ones omit it and
#: fall back to bot visibility.
REQUESTED_BY_KEY = "requested_by"

#: ``runs.context_ledger`` key naming the human who triggered a run.
#: ``services.routines.run_inline`` stamps it (triggering user, else the
#: routine's owner), so it keeps working once the routine or thread is gone.
RUN_REQUESTED_BY_KEY = "requested_by"

#: Payload key naming the agent (worker service identity) that filed the
#: approval. It may READ the approval it created so `wait_for_approval_activity`
#: can poll, but it may never decide one - only the requester can.
CREATED_BY_KEY = "created_by"

# Shared singletons - cheap, stateless wrappers around config.
desktop_mgr = DesktopManager()
orchestrator = Orchestrator()
model_router = ModelRouter()

_SERVICE_CACHE: dict[str, ModuleType] = {}


# ---------------------------------------------------------------------------
# Service-layer loading
# ---------------------------------------------------------------------------


def optional_service(name: str) -> ModuleType | None:
    """Import ``app.services.<name>`` lazily, returning None when absent.

    The service lane ships some modules (approvals, rag, events, temporal_client)
    independently of this HTTP lane; routes degrade to a 503 or to a local
    fallback rather than breaking app import when one is not on disk yet.
    """
    cached = _SERVICE_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        module = importlib.import_module(f"app.services.{name}")
    except ImportError as exc:  # noqa: BLE001 - module genuinely unavailable
        logger.debug("service module app.services.%s unavailable: %s", name, exc)
        return None
    _SERVICE_CACHE[name] = module
    return module


def require_service(name: str) -> ModuleType:
    module = optional_service(name)
    if module is None:
        raise AppError(
            503,
            "service_unavailable",
            f"service app.services.{name} is not available in this build",
        )
    return module


def require_attr(module: ModuleType, attr: str) -> Any:
    fn = getattr(module, attr, None)
    if fn is None:
        raise AppError(
            503,
            "not_implemented",
            f"{module.__name__}.{attr} is not implemented in this build",
        )
    return fn


def optional_model(name: str) -> Any | None:
    """Fetch an ORM class from ``app.models`` that may not exist yet."""
    models = importlib.import_module("app.models")
    return getattr(models, name, None)


def require_model(name: str, code: str = "not_implemented") -> Any:
    model = optional_model(name)
    if model is None:
        raise AppError(503, code, f"model {name} is not available in this build")
    return model


# ---------------------------------------------------------------------------
# Visibility / ownership
# ---------------------------------------------------------------------------


def bot_visibility_clause(user: User):
    """SQL predicate for the bots this user may see."""
    return or_(Bot.is_system.is_(True), Bot.owner_user_id == user.id)


def visible_bot_ids_subquery(user: User) -> Select:
    return select(Bot.id).where(bot_visibility_clause(user))


def can_see_bot(bot: Bot, user: User) -> bool:
    return bool(bot.is_system) or bot.owner_user_id == user.id


async def get_visible_bot(db: AsyncSession, bot_id: UUID, user: User) -> Bot:
    bot = await db.get(Bot, bot_id)
    if bot is None or not can_see_bot(bot, user):
        raise AppError(404, "bot_not_found", "Bot not found")
    return bot


async def get_owned_thread(db: AsyncSession, thread_id: UUID, user: User) -> Thread:
    thread = await db.get(Thread, thread_id)
    if thread is None or thread.owner_user_id != user.id:
        raise AppError(404, "thread_not_found", "Thread not found")
    return thread


async def resolve_approval_owner(
    db: AsyncSession,
    *,
    run_id: UUID | None,
    payload: dict[str, Any] | None,
    bot: Bot | None = None,
) -> UUID | None:
    """Identify the human behind an approval, if there is one.

    Precedence: an explicit ``requested_by`` in the held payload, then the owner
    of the thread behind ``run_id``, then the owner of a custom bot. Returns None
    only when no human requester is knowable (a routine step against a shared
    system bot) - those are the sole approvals that stay open to any caller.
    """
    requested_by = (payload or {}).get(REQUESTED_BY_KEY)
    if requested_by:
        try:
            return UUID(str(requested_by))
        except (TypeError, ValueError):
            logger.warning(
                "approval payload carries an unparseable %s: %r", REQUESTED_BY_KEY, requested_by
            )

    if run_id is not None:
        run = await db.get(Run, run_id)
        if run is not None and run.thread_id is not None:
            thread = await db.get(Thread, run.thread_id)
            if thread is not None:
                return thread.owner_user_id

    if bot is not None and not bot.is_system and bot.owner_user_id is not None:
        return bot.owner_user_id
    return None


async def approval_owner(db: AsyncSession, approval: Approval) -> UUID | None:
    """Resolve the owner of an approval that already exists."""
    bot = await db.get(Bot, approval.bot_id)
    return await resolve_approval_owner(
        db, run_id=approval.run_id, payload=approval.payload, bot=bot
    )


async def get_visible_approval(
    db: AsyncSession,
    approval_id: UUID,
    user: User,
    *,
    for_decision: bool = False,
) -> Approval:
    """Load an approval the caller may see, or decide when ``for_decision``.

    Approvals are scoped by **requester**, not by bot. System bots are shared, so
    bot visibility alone would let any authenticated user decide another persons
    send/spend/delete approval - the exact gate this product sells.

    * the resolved owner may read and decide
    * the agent that filed it (``created_by``, a worker service identity) may
      read it so it can poll for the outcome, but may never decide
    * an approval with no knowable owner falls back to bot visibility

    Always 404, never 403, so existence stays private.
    """
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise AppError(404, "approval_not_found", "Approval not found")

    owner = await approval_owner(db, approval)
    if owner is not None:
        if owner == user.id:
            return approval
        if not for_decision:
            created_by = (approval.payload or {}).get(CREATED_BY_KEY)
            if created_by and str(created_by) == str(user.id):
                return approval
        raise AppError(404, "approval_not_found", "Approval not found")

    # No knowable requester (routine step against a shared system bot).
    await get_visible_bot(db, approval.bot_id, user)
    return approval


def approval_visibility_stmt(user: User) -> Select:
    """Approvals the user may read - the SQL mirror of ``get_visible_approval``."""
    owner_expr = func.coalesce(
        Approval.payload[REQUESTED_BY_KEY].astext,
        cast(Thread.owner_user_id, Text),
        cast(case((Bot.is_system.is_(False), Bot.owner_user_id), else_=None), Text),
    )
    return (
        select(Approval)
        .outerjoin(Run, Run.id == Approval.run_id)
        .outerjoin(Thread, Thread.id == Run.thread_id)
        .outerjoin(Bot, Bot.id == Approval.bot_id)
        .where(
            or_(
                owner_expr == str(user.id),
                Approval.payload[CREATED_BY_KEY].astext == str(user.id),
                and_(
                    owner_expr.is_(None),
                    Approval.bot_id.in_(visible_bot_ids_subquery(user)),
                ),
            )
        )
    )


async def get_visible_routine(db: AsyncSession, routine_id: UUID, user: User) -> Routine:
    routine = await db.get(Routine, routine_id)
    if routine is None:
        raise AppError(404, "routine_not_found", "Routine not found")
    await get_visible_bot(db, routine.bot_id, user)
    return routine


async def resolve_run_owner(db: AsyncSession, run: Run) -> UUID | None:
    """Identify the human behind a run, if there is one.

    Precedence mirrors ``resolve_approval_owner``: an explicit ``requested_by``
    stamped into ``context_ledger``, then the owner of the thread behind
    ``thread_id``, then the owner of the routine behind ``routine_id``, then the
    owner of a *custom* bot. A shared system bot is never an owner.

    Returns None only for a genuinely unattributable run - no thread, no routine
    owner, and a system bot. Those belong to nobody, which is not the same thing
    as belonging to everybody: see ``get_visible_run``.
    """
    requested_by = (run.context_ledger or {}).get(RUN_REQUESTED_BY_KEY)
    if requested_by:
        try:
            return UUID(str(requested_by))
        except (TypeError, ValueError):
            logger.warning(
                "run %s carries an unparseable ledger %s: %r",
                run.id,
                RUN_REQUESTED_BY_KEY,
                requested_by,
            )

    if run.thread_id is not None:
        thread = await db.get(Thread, run.thread_id)
        if thread is not None:
            return thread.owner_user_id

    routine_id = getattr(run, "routine_id", None)
    if routine_id is not None:
        routine = await db.get(Routine, routine_id)
        owner = getattr(routine, "owner_user_id", None) if routine is not None else None
        if owner is not None:
            return owner

    bot = await db.get(Bot, run.bot_id)
    if bot is not None and not bot.is_system and bot.owner_user_id is not None:
        return bot.owner_user_id
    return None


async def get_visible_run(db: AsyncSession, run_id: UUID, user: User) -> Run:
    """Load a run the caller may see, or 404.

    Runs are scoped by the human behind them, never by bot. The five system bots
    are shared by every authenticated user, so a bot-visibility fallback would
    publish one user's run - status, ``detail``, and the free-text ``error`` the
    orchestrator and the worker write - to the whole tenant. It gets worse after
    a thread delete: ``runs.thread_id`` is ``ON DELETE SET NULL``, so an orphaned
    chat run is indistinguishable from an unattended routine run, and any
    fallback that exposes the second exposes the first.

    An unattributable run (no thread, no routine owner, system bot) is therefore
    visible to **nobody** here. Unattended routine history stays reachable
    through ``GET /routines/{routine_id}/runs``, which is gated on routine
    visibility rather than on a claim of ownership no one can make.

    Always 404, never 403, so existence stays private.
    """
    run = await db.get(Run, run_id)
    if run is None:
        raise AppError(404, "run_not_found", "Run not found")
    owner = await resolve_run_owner(db, run)
    if owner is None or owner != user.id:
        raise AppError(404, "run_not_found", "Run not found")
    return run


def run_visibility_stmt(user: User) -> Select:
    """Runs the user may read - the SQL mirror of ``get_visible_run``.

    Same precedence, expressed as a coalesce over the joined rows. Runs with no
    resolvable owner match nobody, so they are absent from every user-facing
    listing.
    """
    owner_expr = func.coalesce(
        Run.context_ledger[RUN_REQUESTED_BY_KEY].astext,
        cast(Thread.owner_user_id, Text),
        cast(Routine.owner_user_id, Text),
        cast(case((Bot.is_system.is_(False), Bot.owner_user_id), else_=None), Text),
    )
    return (
        select(Run)
        .outerjoin(Thread, Thread.id == Run.thread_id)
        .outerjoin(Routine, Routine.id == Run.routine_id)
        .outerjoin(Bot, Bot.id == Run.bot_id)
        .where(owner_expr == str(user.id))
    )


# ---------------------------------------------------------------------------
# SSE plumbing
# ---------------------------------------------------------------------------

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Stop nginx / Front Door from buffering the stream.
    "X-Accel-Buffering": "no",
}

#: Heartbeat interval in seconds - sse-starlette emits a `: ping` comment.
SSE_PING_SECONDS = 15


def sse_event(event: str, data: Any) -> dict[str, str]:
    """Serialise one SSE frame for ``EventSourceResponse``."""
    if not isinstance(data, str):
        data = json.dumps(data, default=str)
    return {"event": event, "data": data}


def normalise_stream_chunk(chunk: Any) -> tuple[str, Any]:
    """Accept the several shapes a producer may yield and return (event, data).

    Supported: ``(event, data)`` pairs, ``{"event": ..., "data": ...}`` mappings,
    ``{"type": ..., **payload}`` mappings, and bare strings (token deltas).
    """
    if isinstance(chunk, (tuple, list)) and len(chunk) == 2:
        event, data = chunk
        return str(event), data
    if isinstance(chunk, dict):
        if "event" in chunk:
            payload = chunk.get("data", {k: v for k, v in chunk.items() if k != "event"})
            return str(chunk["event"]), payload
        if "type" in chunk:
            payload = chunk.get("data", {k: v for k, v in chunk.items() if k != "type"})
            return str(chunk["type"]), payload
        return "token", chunk
    if isinstance(chunk, str):
        return "token", {"delta": chunk}
    return "token", {"delta": str(chunk)}


async def iter_until_disconnect(
    request: Request,
    source: AsyncIterator[Any],
    *,
    poll_seconds: float = 1.0,
) -> AsyncIterator[Any]:
    """Yield from ``source`` but stop as soon as the client goes away.

    ``request.is_disconnected()`` is a non-blocking poll, so each ``__anext__``
    is raced against a short timeout and the disconnect flag is re-checked
    between items. The source iterator is always closed on the way out.
    """
    iterator = source.__aiter__()
    pending: asyncio.Task | None = None
    try:
        while True:
            if await request.is_disconnected():
                return
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=poll_seconds)
            if not done:
                continue
            current, pending = pending, None
            try:
                item = current.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(BaseException):
                await aclose()


# ---------------------------------------------------------------------------
# Risk gating
# ---------------------------------------------------------------------------


async def create_gated_approval(
    db: AsyncSession,
    *,
    bot_id: UUID,
    risk: str,
    title: str,
    summary: str,
    payload: dict[str, Any],
    run_id: UUID | None = None,
    actor: User | None = None,
) -> Approval:
    """Park a risky action in the approval queue.

    Stamps the resolved human owner into the held payload so the approval stays
    scoped to its requester even if the originating thread is deleted later.
    Prefers ``app.services.approvals.create_approval`` (contract signature) and
    falls back to writing the row directly.
    """
    payload = dict(payload or {})
    bot = await db.get(Bot, bot_id)
    owner = await resolve_approval_owner(db, run_id=run_id, payload=payload, bot=bot)
    if owner is not None:
        payload[REQUESTED_BY_KEY] = str(owner)
    else:
        logger.warning(
            "approval for bot %s has no knowable requester; it stays decidable by "
            "any user who can see the bot",
            bot_id,
        )
    # An agent filing on someone elses behalf keeps read access so it can poll.
    if actor is not None and actor.id != owner:
        payload[CREATED_BY_KEY] = str(actor.id)

    service = optional_service("approvals")
    factory = getattr(service, "create_approval", None) if service else None
    if factory is not None:
        return await factory(
            db,
            run_id=run_id,
            bot_id=bot_id,
            risk=risk,
            title=title,
            summary=summary,
            payload=payload,
        )

    approval = Approval(
        run_id=run_id,
        bot_id=bot_id,
        risk=risk,
        title=title,
        summary=summary,
        payload=payload,
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
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - runs.run_id may still be NOT NULL
        await db.rollback()
        raise AppError(
            503,
            "approval_unavailable",
            f"Could not queue an approval for this action: {exc}",
        ) from exc
    await db.refresh(approval)
    return approval
