"""Bot CRUD and per-bot budget."""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import assert_admin_for, get_current_user, require_admin
from app.db import get_db
from app.errors import AppError
from app.models import (
    Approval,
    AuditEvent,
    Bot,
    BotConnector,
    Connector,
    InboundSource,
    McpServer,
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
    BotConnectorSummary,
    BotInboxSummary,
    BotOut,
    BotPersonaOut,
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
        email=bot.email,
        voice=bot.voice,
        signature=bot.signature,
        desktop_habits=bot.desktop_habits,
        created_at=bot.created_at,
    )


#: Fields where `null` means "clear this", not "leave it alone".
#:
#: `update_bot` skips `None` for everything else, because a PATCH body is
#: mostly "the fields I am changing" and a client that helpfully sends
#: `role: null` should not blank the role. These are the exceptions: clearing
#: a model override is how a bot goes back to tier routing, and removing a
#: persona field is a thing a person does on purpose.
_NULLABLE_BOT_FIELDS = (
    "model_provider",
    "model_name",
    "email",
    "voice",
    "signature",
    "desktop_habits",
)



#: Config keys an inbound source may carry its address under.
#:
#: `inbound_sources.config` is provider-shaped JSON rather than a fixed
#: schema - a Graph subscription, a mail poller and a webhook each name the
#: same thing differently - so the address is looked for rather than assumed,
#: and a source with none simply has no address to show.
_INBOX_ADDRESS_KEYS = ("address", "email", "mailbox", "from_address", "to", "user")


def _inbox_address(config: dict | None) -> str | None:
    """The address an inbound source listens on, if it names one.

    Only ever a plain string from a known key: `config` can hold anything a
    provider needed, and rendering an arbitrary value into a persona card is
    how a secret ends up on screen.
    """
    for key in _INBOX_ADDRESS_KEYS:
        value = (config or {}).get(key)
        if isinstance(value, str) and value.strip() and "@" in value:
            return value.strip()
    return None

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
    # `connector_ids` and `mcp_ids` land in `bot_connectors.connector_id` and
    # `bot_mcp.mcp_id`, both foreign keys, and were going in unchecked — an id
    # the catalog does not have (a connector removed since the client cached the
    # list, an MCP server deleted in another window) failed at the commit below,
    # and `app.errors`' catch-all turns an IntegrityError into
    # `{"detail": "internal_error", "code": "internal_error", …}`. That is a 500
    # with no usable code on a routine "create a bot" click. Checked before the
    # bot row is written so a rejected request also leaves nothing behind.
    #
    # MCP visibility matches `routers/integrations.py::_get_mcp`: a server with
    # no owner is shared, otherwise only its owner may see it — so this is also
    # what stops one user attaching another user's private MCP server, whose
    # tools their bot would then be able to call.
    for cid in dict.fromkeys(body.connector_ids):
        if await db.get(Connector, cid) is None:
            raise AppError(404, "connector_not_found", f"Connector {cid!r} not found")
    for mid in dict.fromkeys(body.mcp_ids):
        mcp = await db.get(McpServer, mid)
        if mcp is None or mcp.owner_user_id not in (None, user.id):
            raise AppError(404, "mcp_not_found", "MCP server not found")

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
        # `or None` rather than the value: these arrive from text inputs, and
        # an untouched one sends "" — which would print an empty line in the
        # persona block the orchestrator builds.
        email=body.email or None,
        voice=body.voice or None,
        signature=body.signature or None,
        desktop_habits=body.desktop_habits or None,
    )
    db.add(bot)
    await db.commit()
    await db.refresh(bot)
    # `dict.fromkeys` for the same reason as the checks above: both link tables
    # have a composite primary key, so a repeated id in the request body was
    # itself a 500. Order is preserved, unlike `set()`.
    for cid in dict.fromkeys(body.connector_ids):
        db.add(BotConnector(bot_id=bot.id, connector_id=cid, status="disconnected"))
    for mid in dict.fromkeys(body.mcp_ids):
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
    user: User = Depends(require_admin),
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


@router.get("/bots/{bot_id}/persona", response_model=BotPersonaOut)
async def get_bot_persona(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BotPersonaOut:
    """Who this bot is: its prompt, its connectors, its inboxes, its spend.

    The gap this closes was total. `system_prompt` was write-only across the
    entire API - `CreateCustomBotIn` and `UpdateBotIn` accept one and nothing
    ever returned one - so the desktop app could show a name and a one-line
    role and nothing else. Reported as "the bots have personas, with emails and
    so on but on the desktop app, i can't see that", which was exactly true:
    every field here was already in the database and none of it was reachable.

    Separate from `GET /bots/{id}` rather than folded into `BotOut` because the
    sidebar's list request draws five bots on every launch and does not need
    five system prompts to do it.

    Secrets stay references. `secret_ref` is the pointer `services.secrets`
    resolves in-process; the resolved value never reaches a row or a response,
    and this endpoint does not resolve anything.
    """
    bot = await get_visible_bot(db, bot_id, user)

    bound = (
        (
            await db.execute(
                select(BotConnector, Connector)
                .join(Connector, Connector.id == BotConnector.connector_id)
                .where(BotConnector.bot_id == bot.id)
                .order_by(Connector.name)
            )
        )
        .all()
    )
    connectors = [
        BotConnectorSummary(
            connector_id=link.connector_id,
            name=connector.name,
            status=link.status,
            secret_ref=link.secret_ref,
        )
        for link, connector in bound
    ]

    # An inbound source routes to a roster (`bot_ids`) or to a single bot
    # (`bot_id`, the older shape). Both are checked, because a deployment
    # configured before the roster existed still has live sources.
    sources = (
        (
            await db.execute(
                select(InboundSource).where(
                    InboundSource.owner_user_id == user.id,
                    or_(
                        InboundSource.bot_id == bot.id,
                        InboundSource.bot_ids.contains([str(bot.id)]),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    inboxes = [
        BotInboxSummary(
            slug=source.slug,
            name=source.name,
            kind=source.kind,
            channel=source.channel,
            address=_inbox_address(source.config),
            enabled=source.enabled,
            last_event_at=source.last_event_at,
        )
        for source in sources
    ]

    return BotPersonaOut(
        **_bot_out(bot).model_dump(),
        system_prompt=bot.system_prompt,
        connectors=connectors,
        inboxes=inboxes,
        spent_usd_today=float(await model_router.spent_today_usd(db, bot.id)),
    )


@router.patch("/bots/{bot_id}", response_model=BotOut)
async def update_bot(
    bot_id: uuid.UUID,
    body: UpdateBotIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BotOut:
    bot = await get_visible_bot(db, bot_id, user)
    changes = body.model_dump(exclude_unset=True)

    # A system bot is everybody's. Its name, role, persona, model pin and
    # budget are deployment-wide settings, so changing them is an admin
    # action; a custom bot stays owner-gated exactly as before.
    if bot.is_system and changes:
        await assert_admin_for(db, user)

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
        if field in _NULLABLE_BOT_FIELDS:
            # `None` is meaningful for these, unlike every other field — see
            # `_NULLABLE_BOT_FIELDS`. An empty string is treated as a clear
            # too: it arrives from a text input somebody emptied, and storing
            # "" would make the persona block below print a blank line.
            setattr(bot, field, value or None)
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
    if bot.is_system:
        await assert_admin_for(db, user)
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
