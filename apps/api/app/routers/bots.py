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
    model_router,
)
from app.schemas import (
    BotOut,
    BudgetIn,
    CreateCustomBotIn,
    OkOut,
    ProviderCredentialIn,
    ProviderCredentialOut,
    ProviderCredentialsOut,
    ProviderModelsOut,
    ProvidersOut,
    UpdateBotIn,
)
from app.services import mcp_registry, provider_credentials
from app.services.provider_credentials import KNOWN_PROVIDERS
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
        model_provider=bot.model_provider,
        model_name=bot.model_name,
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
        model_provider=body.model_provider,
        model_name=body.model_name,
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


@router.get("/bots/providers", response_model=ProvidersOut)
async def list_available_providers(
    user: User = Depends(get_current_user),
) -> ProvidersOut:
    """Which of `azure`/`openai`/`anthropic`/`google` this deployment can
    actually reach — a live credential resolved, not just an accepted config
    value. Drives the setup wizard's and the Builder's provider picker: a
    bot should never be offered a provider that will silently mock.
    """
    return ProvidersOut(
        azure=model_router.provider_available("azure"),
        openai=model_router.provider_available("openai"),
        anthropic=model_router.provider_available("anthropic"),
        google=model_router.provider_available("google"),
    )


@router.get("/bots/providers/{provider}/models", response_model=ProviderModelsOut)
async def list_provider_models(
    provider: str,
    user: User = Depends(get_current_user),
) -> ProviderModelsOut:
    """Model/deployment names live-queried from `provider` itself, for the
    Builder's model dropdown — not a hardcoded list, so it is only ever as
    stale as the account it asks. 400 on an unrecognised provider name; 502
    when the provider has no live credential or the provider's own API call
    fails, carrying that failure's own detail rather than swallowing it into
    an empty list a user would misread as "you have no models."
    """
    if provider not in KNOWN_PROVIDERS:
        raise AppError(400, "unknown_provider", f"provider must be one of {sorted(KNOWN_PROVIDERS)}")
    try:
        models = await model_router.list_models(provider)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        raise AppError(502, "provider_unreachable", f"could not list {provider} models: {exc}") from exc
    return ProviderModelsOut(provider=provider, models=models)  # type: ignore[arg-type]


def _key_hint(value: str) -> str:
    """Enough to recognise a key by eye, never enough to use it."""
    tail = value[-4:] if len(value) >= 4 else value
    return f"…{tail}"


@router.get("/bots/providers/credentials", response_model=ProviderCredentialsOut)
async def list_provider_credentials(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProviderCredentialsOut:
    """Provider keys typed into the app — never the keys themselves, only
    whether one is set. Env-configured providers (`OPENAI_API_KEY` and
    friends) do not appear here even when live; this is only the app-writable
    layer `provider_credentials.py` adds on top. Cross-reference with
    `GET /bots/providers` for the full picture of what is actually reachable.
    """
    await provider_credentials.maybe_reload(db)
    rows = {row.provider: row for row in await provider_credentials.list_credentials(db)}
    out: list[ProviderCredentialOut] = []
    for provider in KNOWN_PROVIDERS:
        row = rows.get(provider)
        if row is None:
            out.append(ProviderCredentialOut(provider=provider, configured=False))
            continue
        override = provider_credentials.get_override(provider)
        out.append(
            ProviderCredentialOut(
                provider=provider,
                configured=True,
                key_hint=_key_hint(override["api_key"]) if override else None,
                base_url=row.base_url,
                updated_at=row.updated_at,
            )
        )
    return ProviderCredentialsOut(credentials=out)


#: POST, not PUT, despite this being a plain upsert — PUT is not in
#: `infra/azure/main.bicep`'s Container Apps ingress `corsPolicy.allowedMethods`
#: (`['GET', 'POST', 'PATCH', 'DELETE', 'OPTIONS']`, set before this endpoint
#: existed), which is a separate, edge-level CORS check ahead of the FastAPI
#: `CORSMiddleware` in main.py — the app's own `allow_methods=["*"]` never even
#: sees a PUT preflight the ingress has already answered without it. Changing
#: the bicep and redeploying the whole template is the "real" fix; POST avoids
#: it entirely and ships as a plain image update.
@router.post("/bots/providers/{provider}/credential", response_model=ProviderCredentialOut)
async def set_provider_credential(
    provider: str,
    body: ProviderCredentialIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProviderCredentialOut:
    if provider not in KNOWN_PROVIDERS:
        raise AppError(400, "unknown_provider", f"provider must be one of {sorted(KNOWN_PROVIDERS)}")
    api_key = body.api_key.strip()
    if not api_key:
        raise AppError(400, "empty_api_key", "api_key cannot be blank")
    base_url = (body.base_url or "").strip() or None
    row = await provider_credentials.set_credential(
        db, provider=provider, api_key=api_key, base_url=base_url, user_id=user.id
    )
    return ProviderCredentialOut(
        provider=provider,  # type: ignore[arg-type]  # narrowed by the `not in KNOWN_PROVIDERS` raise above
        configured=True,
        key_hint=_key_hint(api_key),
        base_url=row.base_url,
        updated_at=row.updated_at,
    )


@router.delete("/bots/providers/{provider}/credential", response_model=OkOut)
async def delete_provider_credential(
    provider: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    if provider not in KNOWN_PROVIDERS:
        raise AppError(400, "unknown_provider", f"provider must be one of {sorted(KNOWN_PROVIDERS)}")
    await provider_credentials.delete_credential(db, provider=provider)
    return OkOut(ok=True)


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
        if field in ("model_provider", "model_name"):
            # `None` is meaningful here, unlike every other field: it clears
            # the override and reverts this bot to tier routing.
            setattr(bot, field, value)
            continue
        if value is not None:
            setattr(bot, field, value)

    if ("model_provider" in changes or "model_name" in changes) and bool(bot.model_provider) != bool(
        bot.model_name
    ):
        # Checked against the *resulting* row, not the request body: a PATCH
        # touching only one of the two fields is legitimate when the other
        # was already set from an earlier request (see UpdateBotIn's
        # validator), so only a genuinely inconsistent outcome is rejected.
        raise AppError(
            422,
            "incomplete_model_override",
            "model_provider and model_name must both be set or both be null",
        )

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
