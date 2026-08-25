"""Bot CRUD and per-bot budget."""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.errors import AppError
from app.models import (
    Approval,
    AuditEvent,
    Bot,
    BotConnector,
    Message,
    Run,
    User,
)
from app.routers.deps import (
    bot_visibility_clause,
    desktop_mgr,
    get_visible_bot,
)
from app.schemas import BotOut, BudgetIn, CreateCustomBotIn, OkOut, UpdateBotIn
from app.services import mcp_registry
from app.services.seed import seed_system

logger = logging.getLogger("nesqbot.bots")

router = APIRouter(tags=["bots"])


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:48]


def _bot_out(bot: Bot) -> BotOut:
    return BotOut(
        id=bot.id,
        slug=bot.slug,
        name=bot.name,
        role=bot.role,
        is_system=bot.is_system,
        daily_budget_usd=float(bot.daily_budget_usd),
        desktop_profile=bot.desktop_profile,
        created_at=bot.created_at,
    )


@router.get("/bots", response_model=list[BotOut])
async def list_bots(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[BotOut]:
    """System bots plus the caller's own custom bots."""
    result = await db.execute(
        select(Bot).where(bot_visibility_clause(user)).order_by(Bot.is_system.desc(), Bot.name)
    )
    return [_bot_out(b) for b in result.scalars().all()]


@router.post("/bots", response_model=BotOut)
async def create_custom_bot(
    body: CreateCustomBotIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BotOut:
    slug = _slugify(body.name) or f"bot_{uuid.uuid4().hex[:8]}"
    existing = await db.execute(select(Bot).where(Bot.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}_{uuid.uuid4().hex[:4]}"
    bot = Bot(
        slug=slug,
        name=body.name,
        role=body.role,
        system_prompt=body.system_prompt,
        is_system=False,
        owner_user_id=user.id,
        desktop_profile=body.desktop_profile,
        daily_budget_usd=Decimal(str(body.daily_budget_usd)),
    )
    db.add(bot)
    await db.commit()
    await db.refresh(bot)
    for cid in body.connector_ids:
        db.add(BotConnector(bot_id=bot.id, connector_id=cid, status="disconnected"))
    for mid in body.mcp_ids:
        await mcp_registry.attach_mcp(db, bot.id, mid)
    await db.commit()
    return _bot_out(bot)


@router.post("/bots/system/reseed", response_model=OkOut)
async def reseed_system_bots(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    """Re-run system-bot seeding without restarting the API.

    Editing `bots/*.yaml` — a new bot, a changed prompt — only ever took
    effect on the next boot, because `seed_system` ran once, in `lifespan`.
    This calls the exact same function on demand: new slugs get created,
    existing system bots get their name/role/prompt/desktop_profile
    reconciled from the YAML, and a custom bot's owner-tuned budget is never
    touched — identical to what happens at boot, see `services.seed`.

    No RBAC exists yet (see `security.md`'s known gaps), so this is reachable
    by anyone authenticated, same as bot creation already is. Idempotent and
    additive-or-reconciling only — it cannot delete a bot — so the exposure is
    "someone re-applies YAML you already wrote", not a destructive action.
    """
    before = await db.execute(select(func.count()).select_from(Bot).where(Bot.is_system.is_(True)))
    await seed_system(db)
    after = await db.execute(select(func.count()).select_from(Bot).where(Bot.is_system.is_(True)))
    created = int(after.scalar_one()) - int(before.scalar_one())
    logger.info("system bots reseeded by %s (%d new)", user.id, created)
    return OkOut(ok=True, detail=f"reseeded — {created} new system bot(s)")


@router.get("/bots/{bot_id}", response_model=BotOut)
async def get_bot(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BotOut:
    bot = await get_visible_bot(db, bot_id, user)
    return _bot_out(bot)


@router.patch("/bots/{bot_id}", response_model=BotOut)
async def update_bot(
    bot_id: uuid.UUID,
    body: UpdateBotIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BotOut:
    bot = await get_visible_bot(db, bot_id, user)
    changes = body.model_dump(exclude_unset=True)

    if bot.is_system and ("system_prompt" in changes or "slug" in changes):
        raise AppError(
            403,
            "system_bot_immutable",
            "System bots cannot have their prompt or slug changed",
        )

    if changes.get("slug"):
        new_slug = _slugify(changes["slug"])
        if not new_slug:
            raise AppError(400, "invalid_slug", "slug must contain alphanumeric characters")
        clash = await db.execute(select(Bot).where(Bot.slug == new_slug, Bot.id != bot.id))
        if clash.scalar_one_or_none():
            raise AppError(409, "slug_taken", "Another bot already uses that slug")
        changes["slug"] = new_slug

    if "daily_budget_usd" in changes and changes["daily_budget_usd"] is not None:
        changes["daily_budget_usd"] = Decimal(str(changes["daily_budget_usd"]))

    for field, value in changes.items():
        if value is not None:
            setattr(bot, field, value)

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot.id,
            event_type="bot_updated",
            detail={"fields": sorted(changes.keys())},
        )
    )
    await db.commit()
    await db.refresh(bot)
    return _bot_out(bot)


@router.delete("/bots/{bot_id}", response_model=OkOut)
async def delete_bot(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    """Delete a custom bot. System bots are never deletable."""
    bot = await get_visible_bot(db, bot_id, user)
    slug = bot.slug
    if bot.is_system:
        raise AppError(403, "system_bot_undeletable", "System bots cannot be deleted")
    if bot.owner_user_id != user.id:
        raise AppError(403, "not_bot_owner", "Only the owner can delete this bot")

    # Tear the desktop down before dropping the row so no container is orphaned.
    try:
        await desktop_mgr.stop(db, bot.id, wipe=True)
    except Exception:  # noqa: BLE001 - deletion must not be blocked by a dead sidecar
        logger.exception("desktop stop failed while deleting bot %s", bot.id)

    # messages.bot_id / runs.bot_id / approvals.bot_id have no ON DELETE CASCADE.
    await db.execute(update(Message).where(Message.bot_id == bot.id).values(bot_id=None))
    await db.execute(delete(Approval).where(Approval.bot_id == bot.id))
    await db.execute(delete(Run).where(Run.bot_id == bot.id))
    await db.delete(bot)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="bot_deleted",
            detail={"slug": slug},
        )
    )
    await db.commit()
    return OkOut(ok=True, detail="deleted")


@router.patch("/bots/{bot_id}/budget", response_model=BotOut)
async def update_budget(
    bot_id: uuid.UUID,
    body: BudgetIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BotOut:
    bot = await get_visible_bot(db, bot_id, user)
    bot.daily_budget_usd = Decimal(str(body.daily_budget_usd))
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot.id,
            event_type="budget_updated",
            detail={"daily_budget_usd": body.daily_budget_usd},
        )
    )
    await db.commit()
    await db.refresh(bot)
    return _bot_out(bot)
