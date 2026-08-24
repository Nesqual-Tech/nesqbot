"""Inbound: the unauthenticated front door, and the owner-scoped surface behind it.

This module has two halves with opposite postures and that is the whole shape of
it.

**`POST /inbound/hooks/{source_slug}` is written for a stranger.** It takes no
bearer token — the sender has none — so it authenticates with an HMAC, refuses
everything it cannot verify with one indistinguishable answer, caps what it will
read before it reads it, and returns a byte-identical body whether the reply
matched a live lead, matched five, matched nothing at all, or was a replay of a
delivery already on the record. Nothing an unauthenticated caller can observe
depends on this tenant's data.

**Everything else is written for the owner.** Sources and events are scoped to
`owner_user_id` and answer 404 for someone else's rows, per `deps.py`. That is
where the real outcome of a delivery is legible: which work item it matched, the
other candidates when there were several, and the queue of replies that matched
nothing — which exists because an unplaceable reply is a product event, not an
error to swallow.

Why the webhook returns 202 and not 200
---------------------------------------
Because the work is not done when it answers. Resolution and the event row are
committed synchronously — the record that a reply arrived must survive a crash
even if the run does not — and the agent run is a background task. A webhook that
blocked for a full agent turn would time out at every provider that has a retry
policy, and its response time would leak how much work the payload caused.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import SessionLocal, get_db
from app.errors import AppError
from app.models import AuditEvent, Connector, InboundEvent, InboundSource, User
from app.routers.deps import get_visible_bot
from app.schemas import (
    InboundAckOut,
    InboundEventOut,
    InboundPollOut,
    InboundSourceIn,
    InboundSourceOut,
    OkOut,
    UpdateInboundSourceIn,
)
from app.services import connectors as connectors_service
from app.services import inbound as inbound_service
from app.services.secrets import parse_ref

logger = logging.getLogger("nesqbot.inbound")

router = APIRouter(tags=["inbound"])

#: Mounted under `/api`, so this is what an owner pastes into a provider.
HOOK_PREFIX = "/api/inbound/hooks"

#: The one answer every rejected delivery gets, whatever was actually wrong.
#:
#: "No such source", "that source is switched off", "your clock is nine minutes
#: out" and "your digest is wrong" are four different facts about this server,
#: and a sender that can tell them apart can map the surface — starting with
#: which hook URLs exist. One code, one status, one sentence.
REJECTED = ("invalid_signature", "This delivery could not be verified")


# ---------------------------------------------------------------------------
# Owner-scoped helpers
# ---------------------------------------------------------------------------


async def _get_owned_source(db: AsyncSession, source_id: uuid.UUID, user: User) -> InboundSource:
    """Load a source the caller owns, or 404 — never 403, per ``deps.py``."""
    source = await db.get(InboundSource, source_id)
    if source is None or source.owner_user_id != user.id:
        raise AppError(404, "inbound_source_not_found", "Inbound source not found")
    return source


def _render_source(source: InboundSource) -> InboundSourceOut:
    return InboundSourceOut(
        id=source.id,
        slug=source.slug,
        hook_path=f"{HOOK_PREFIX}/{source.slug}",
        name=source.name,
        kind=source.kind,
        channel=source.channel,
        owner_user_id=source.owner_user_id,
        bot_id=source.bot_id,
        bot_ids=[uuid.UUID(str(b)) for b in (source.bot_ids or [])],
        secret_ref=source.secret_ref,
        connector_id=source.connector_id,
        config=source.config or {},
        enabled=source.enabled,
        last_event_at=source.last_event_at,
        last_polled_at=source.last_polled_at,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _render_event(event: InboundEvent) -> InboundEventOut:
    candidates: list[uuid.UUID] = []
    for raw in event.candidate_ids or []:
        try:
            candidates.append(uuid.UUID(str(raw)))
        except (TypeError, ValueError):  # pragma: no cover - written by this lane only
            continue
    return InboundEventOut(
        id=event.id,
        source_id=event.source_id,
        owner_user_id=event.owner_user_id,
        channel=event.channel,
        address=event.address,
        external_id=event.external_id,
        via=event.via,
        status=event.status,
        subject=event.subject,
        body=event.body,
        work_item_id=event.work_item_id,
        candidate_ids=candidates,
        thread_id=event.thread_id,
        run_id=event.run_id,
        detail=event.detail or {},
        created_at=event.created_at,
        handled_at=event.handled_at,
    )


def _validated_secret_ref(value: str | None, *, kind: str) -> str | None:
    """Refuse anything that is not a resolvable reference.

    Two failures this closes, and the second is the one that matters. A webhook
    source with no key is an unauthenticated endpoint that starts agent runs, so
    it is refused outright. And a caller who pastes the key itself into this
    field would have it echoed back by `GET /inbound/sources` forever after —
    `parse_ref` accepts `env://NAME`, `kv://vault/name` and a bare name against
    the configured vault, and rejects the shapes a real secret takes.
    """
    ref = (value or "").strip()
    if not ref:
        if kind == "webhook":
            raise AppError(
                422,
                "signing_key_required",
                "A webhook source needs secret_ref: a reference to its signing key "
                "(env://NAME or kv://vault/name). An unsigned hook that starts agent "
                "runs is a way to spend your budget.",
            )
        return None
    if parse_ref(ref) is None:
        raise AppError(
            422,
            "invalid_secret_ref",
            "secret_ref must be a reference to a secret (env://NAME, kv://vault/name, "
            "or a bare name against AZURE_KEY_VAULT_URL) — never the secret itself.",
        )
    return ref


async def _validated_roster(db: AsyncSession, bot_ids: list[uuid.UUID], user: User) -> list[str]:
    """Every roster entry, checked for visibility at configuration time.

    Checked here *and* again when a bot is actually seated
    (`inbound._visible_bot_ids`). Not belt and braces: this check gives the
    person a 404 while they are looking at the form, and the later one covers a
    bot deleted between then and a lead answering six weeks afterwards.
    """
    seen: list[str] = []
    for bot_id in bot_ids:
        bot = await get_visible_bot(db, bot_id, user)
        if str(bot.id) not in seen:
            seen.append(str(bot.id))
    return seen


async def _validated_poll_config(
    db: AsyncSession,
    *,
    connector_id: str | None,
    config: dict[str, Any],
) -> None:
    """A poll reads. Refuse a source configured to do anything else.

    Not a second risk classifier — it asks `connectors.action_risk`, the same
    table `simulation.perform` consults, and simply declines to build a source
    around a non-`observe` action. `execute_connector_action` would gate it at
    call time anyway; refusing at configuration time means the owner finds out
    while they are creating the thing rather than the first time it fires.
    """
    if not connector_id:
        raise AppError(
            422, "connector_required", "A poll source needs connector_id and a bound bot"
        )
    connector = await db.get(Connector, connector_id)
    if connector is None:
        raise AppError(404, "connector_not_found", "Connector not found")
    action = str((config or {}).get("action") or "list_inbox")
    if connectors_service.action_spec(connector, action) is None:
        raise AppError(
            422, "unknown_action", f"{connector_id} has no action named {action!r}"
        )
    risk = connectors_service.action_risk(connector, action)
    if risk != "observe":
        raise AppError(
            422,
            "poll_must_read",
            f"{connector_id}.{action} is classified {risk!r}. A poll source may only "
            "run a read-only action.",
        )


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@router.get("/inbound/sources", response_model=list[InboundSourceOut])
async def list_inbound_sources(
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[InboundSourceOut]:
    """The caller's inbound sources, newest first."""
    stmt = select(InboundSource).where(InboundSource.owner_user_id == user.id)
    if kind is not None:
        stmt = stmt.where(InboundSource.kind == kind)
    rows = await db.execute(stmt.order_by(InboundSource.created_at.desc()).limit(limit))
    return [_render_source(row) for row in rows.scalars().all()]


@router.post("/inbound/sources", response_model=InboundSourceOut)
async def create_inbound_source(
    body: InboundSourceIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InboundSourceOut:
    """Create a way in, and mint its unguessable hook URL.

    The response carries `hook_path` and `slug`. That is the only time the caller
    has to go and fetch them from anywhere — they are also on `GET
    /inbound/sources`, because a URL a person cannot retrieve is a URL they
    re-create by making a second source.
    """
    if body.kind not in inbound_service.SOURCE_KINDS:
        raise AppError(422, "unknown_kind", f"kind must be one of {inbound_service.SOURCE_KINDS}")

    bot_id: uuid.UUID | None = None
    if body.bot_id is not None:
        bot_id = (await get_visible_bot(db, body.bot_id, user)).id
    roster = await _validated_roster(db, body.bot_ids, user)
    secret_ref = _validated_secret_ref(body.secret_ref, kind=body.kind)

    if body.kind == "poll":
        if bot_id is None:
            raise AppError(422, "bot_required", "A poll source needs a bot to run as")
        await _validated_poll_config(
            db, connector_id=body.connector_id, config=body.config or {}
        )

    source = InboundSource(
        slug=inbound_service.new_slug(),
        name=body.name,
        kind=body.kind,
        channel=body.channel.strip().lower(),
        owner_user_id=user.id,
        bot_id=bot_id,
        bot_ids=roster,
        secret_ref=secret_ref,
        connector_id=body.connector_id,
        config=dict(body.config or {}),
        enabled=body.enabled,
    )
    db.add(source)
    await db.flush()
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="inbound_source_created",
            # Never `slug` and never `secret_ref`. The slug is a capability URL
            # and the ref points at a signing key; an audit event is read by more
            # people than the row it describes.
            detail={
                "source_id": str(source.id),
                "kind": source.kind,
                "channel": source.channel,
                "roster": len(roster),
                "connector_id": source.connector_id,
            },
        )
    )
    await db.commit()
    await db.refresh(source)
    return _render_source(source)


@router.patch("/inbound/sources/{source_id}", response_model=InboundSourceOut)
async def update_inbound_source(
    source_id: uuid.UUID,
    body: UpdateInboundSourceIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InboundSourceOut:
    """Update a source. `slug` and `kind` are not editable.

    `slug` because a hook URL that can be edited is one that can be edited onto
    a value another tenant is about to be given; `kind` because a webhook source
    and a poll source validate differently and a half-migrated row would be a
    poll with a signing key or a webhook without one.
    """
    source = await _get_owned_source(db, source_id, user)
    changes = body.model_dump(exclude_unset=True)

    if "bot_id" in changes:
        source.bot_id = (
            (await get_visible_bot(db, body.bot_id, user)).id if body.bot_id is not None else None
        )
    if "bot_ids" in changes and body.bot_ids is not None:
        source.bot_ids = await _validated_roster(db, body.bot_ids, user)
    if "secret_ref" in changes:
        source.secret_ref = _validated_secret_ref(body.secret_ref, kind=source.kind)
    if "connector_id" in changes:
        source.connector_id = body.connector_id
    if "config" in changes and body.config is not None:
        source.config = dict(body.config)
    if "channel" in changes and body.channel is not None:
        source.channel = body.channel.strip().lower()
    if "name" in changes and body.name is not None:
        source.name = body.name
    if "enabled" in changes and body.enabled is not None:
        source.enabled = body.enabled

    if source.kind == "poll":
        await _validated_poll_config(
            db, connector_id=source.connector_id, config=source.config or {}
        )

    db.add(source)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=source.bot_id,
            event_type="inbound_source_updated",
            detail={
                "source_id": str(source.id),
                "fields": sorted(changes.keys()),
                "enabled": source.enabled,
            },
        )
    )
    await db.commit()
    await db.refresh(source)
    return _render_source(source)


@router.delete("/inbound/sources/{source_id}", response_model=OkOut)
async def delete_inbound_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    """Delete a source. **Its events survive.**

    `inbound_events` carries no foreign key to `inbound_sources`, exactly like
    `audit_events` and `work_item_transfers`: deleting the hook must not delete
    the record of the replies that came through it, or the delete becomes the
    way to erase what a customer actually said.
    """
    source = await _get_owned_source(db, source_id, user)
    await db.delete(source)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=source.bot_id,
            event_type="inbound_source_deleted",
            detail={"source_id": str(source_id), "kind": source.kind},
        )
    )
    await db.commit()
    return OkOut(ok=True, detail="deleted")


# ---------------------------------------------------------------------------
# Events — "nothing was dropped on the floor"
# ---------------------------------------------------------------------------


@router.get("/inbound/events", response_model=list[InboundEventOut])
async def list_inbound_events(
    status: str | None = Query(default=None),
    work_item_id: uuid.UUID | None = Query(default=None),
    source_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[InboundEventOut]:
    """Everything that arrived for this caller, newest first.

    **This endpoint is the promise that nothing is silently discarded.**
    `?status=unmatched` is the queue of replies that resolved to no work item —
    a real person answering from a second address, an outreach nobody recorded —
    and it is a list a human works, not a log line nobody reads.
    `?status=ambiguous` is the shorter and more interesting list: replies whose
    address matched several items, where the first was taken and the rest are on
    the row.

    Scoped by `owner_user_id`, which is stamped from the source rather than
    joined through the work item — which is what lets an *unmatched* event,
    whose work item is by definition unknown, still belong to exactly one person.
    """
    stmt = select(InboundEvent).where(InboundEvent.owner_user_id == user.id)
    if status is not None:
        stmt = stmt.where(InboundEvent.status == status)
    if work_item_id is not None:
        stmt = stmt.where(InboundEvent.work_item_id == work_item_id)
    if source_id is not None:
        stmt = stmt.where(InboundEvent.source_id == source_id)
    rows = await db.execute(stmt.order_by(InboundEvent.created_at.desc()).limit(limit))
    return [_render_event(row) for row in rows.scalars().all()]


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


@router.post("/inbound/sources/{source_id}/poll", response_model=InboundPollOut)
async def poll_inbound_source(
    source_id: uuid.UUID,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InboundPollOut:
    """Pull from the bound connector now, and treat what comes back as inbound.

    The other way in. Everything after the fetch is the identical code path a
    webhook takes — `inbound.ingest`, the same 0/1/N resolution, the same replay
    indexes, the same wake — because two pipelines is how a reply that arrives
    by email gets handled and the same reply pulled from a mailbox does not.

    Unlike the webhook, this answers with real numbers: the caller is
    authenticated and it is their own data.
    """
    source = await _get_owned_source(db, source_id, user)
    if source.kind != "poll":
        raise AppError(
            409, "not_a_poll_source", "This source is delivered to, not fetched from"
        )
    if not source.enabled:
        raise AppError(409, "source_disabled", "This source is disabled")

    outcomes, error = await inbound_service.poll_source(db, source)
    counts: dict[str, int] = {}
    event_ids: list[uuid.UUID] = []
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        if outcome.event_id is not None:
            event_ids.append(outcome.event_id)
        if outcome.should_wake:
            _schedule_wake(background, outcome.event_id)

    return InboundPollOut(
        ok=error is None,
        source_id=source.id,
        fetched=len(outcomes),
        matched=counts.get(inbound_service.STATUS_MATCHED, 0),
        ambiguous=counts.get(inbound_service.STATUS_AMBIGUOUS, 0),
        unmatched=counts.get(inbound_service.STATUS_UNMATCHED, 0),
        unroutable=counts.get(inbound_service.STATUS_UNROUTABLE, 0),
        duplicates=counts.get(inbound_service.STATUS_DUPLICATE, 0),
        event_ids=event_ids,
        detail=error,
    )


# ---------------------------------------------------------------------------
# Push — the unauthenticated front door
# ---------------------------------------------------------------------------


def _schedule_wake(background: BackgroundTasks, event_id: uuid.UUID | None) -> None:
    """Run the agent after the response has gone out, on its own session.

    Same reason `routers/threads.py` opens its own `SessionLocal` for the SSE
    producer: FastAPI closes `yield` dependencies before a background task runs,
    so the request's session is gone by then. The event row is already committed,
    so all this task needs is an id.
    """
    if event_id is None:
        return

    async def _run(target: uuid.UUID = event_id) -> None:
        async with SessionLocal() as session:
            await inbound_service.wake_for_event(session, target)

    background.add_task(_run)


async def _read_capped(request: Request) -> bytes:
    """Read at most `MAX_BODY_BYTES`, then refuse. Never trusts `Content-Length`.

    `await request.body()` would buffer whatever the sender sends, so the cap has
    to be applied while reading. `Content-Length` is checked first only because
    an honest sender saying "12MB" can be refused before a byte of it is read;
    the streamed count is what actually enforces the limit, because a dishonest
    sender's header is worth nothing.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > inbound_service.MAX_BODY_BYTES:
        raise AppError(413, "payload_too_large", "This delivery is too large")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > inbound_service.MAX_BODY_BYTES:
            raise AppError(413, "payload_too_large", "This delivery is too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/inbound/hooks/{source_slug}", response_model=InboundAckOut, status_code=202)
async def receive_inbound(
    source_slug: str,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> InboundAckOut:
    """Accept one delivery from outside. **No bearer token; an HMAC instead.**

    The sender is a mail provider or a CRM, not a user, so there is nothing to
    authenticate as. The order below is the order it has to be in:

    1. **Rate limit**, before anything is read or queried, so a flood costs a
       dictionary lookup. Keyed on the slug — and on the client address when the
       slug does not resolve, so spraying random paths cannot grow the map.
    2. **Cap the body while reading it**, so a 4GB POST is refused at 256KB
       rather than after it has been buffered.
    3. **Verify the signature** — HMAC-SHA256 over `v1:{timestamp}:{body}`,
       `secrets.compare_digest`, inside a five-minute window. Unknown slug,
       disabled source, unresolvable key, stale timestamp and wrong digest all
       answer `401 invalid_signature` and all do the HMAC work, because a path
       that answers faster is a path that says "this URL is not real".
    4. **Ingest and commit**, so the record that a reply arrived survives a crash
       in the run that follows.
    5. **Answer `202` with a constant body**, and wake the bot afterwards.

    Step 5 is the anti-enumeration property, and it is worth being explicit: this
    endpoint's response is byte-identical whether the reply matched a live lead,
    matched five, matched none, or was a replay. A caller cannot use it to learn
    which addresses this tenant is working. The owner sees all of it at
    `GET /inbound/events`.
    """
    client_key = request.client.host if request.client else "unknown"
    # The limit is read here rather than taken as a default argument so that
    # changing the constant — at boot, or in a test — actually changes the limit.
    if not inbound_service.rate_limit_ok(
        f"hook:{source_slug[:64]}:{client_key}",
        limit=inbound_service.RATE_LIMIT_PER_MINUTE,
    ):
        raise AppError(
            429,
            "rate_limited",
            "Too many deliveries; retry shortly",
            headers={"Retry-After": "60"},
        )

    body = await _read_capped(request)

    result = await db.execute(
        select(InboundSource).where(InboundSource.slug == source_slug)
    )
    source = result.scalar_one_or_none()

    if source is None or not source.enabled:
        # Do the work an authentic delivery would have cost, then give the same
        # answer a wrong digest gets. See `inbound.burn_time`.
        inbound_service.burn_time(body)
        logger.info("inbound delivery for an unknown or disabled hook slug")
        raise AppError(401, *REJECTED)

    secret = await inbound_service.resolve_signing_key(source)
    reason = inbound_service.verify_signature(
        secret=secret,
        timestamp=request.headers.get(inbound_service.TIMESTAMP_HEADER, ""),
        body=body,
        presented=request.headers.get(inbound_service.SIGNATURE_HEADER, ""),
    )
    if reason:
        # The reason stays here. It names which check failed, which is exactly
        # what an attacker would like to be told.
        logger.warning("inbound delivery on source %s refused: %s", source.id, reason)
        raise AppError(401, *REJECTED)

    # Past this line the sender is authenticated, so a specific error is safe:
    # only somebody holding the signing key can provoke it.
    try:
        payload = _json_object(body)
    except ValueError as exc:
        raise AppError(400, "invalid_payload", str(exc)) from exc

    message = inbound_service.message_from_payload(
        payload,
        source,
        # The presented signature, digested, is the replay key: it covers the
        # timestamp and the body together, so the same delivery twice is one
        # event and a re-signed one is a new one.
        delivery_hash=inbound_service.digest_of(
            request.headers.get(inbound_service.SIGNATURE_HEADER, "")
        ),
    )
    outcome = await inbound_service.ingest(db, source, message)
    if outcome.should_wake:
        _schedule_wake(background, outcome.event_id)
    return InboundAckOut(ok=True, status="accepted")


def _json_object(body: bytes) -> dict[str, Any]:
    if not body.strip():
        raise ValueError("The delivery had an empty body")
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError("The delivery body was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("The delivery body must be a JSON object")
    return parsed
