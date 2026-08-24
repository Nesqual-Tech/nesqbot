"""Bot Desktop lifecycle, risk-gated actions, sidecar proxies, and the stream proxy."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketState

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.errors import AppError
from app.models import AuditEvent, Bot, BotDesktop, User
from app.routers.deps import (
    REQUESTED_BY_KEY,
    can_see_bot,
    create_gated_approval,
    desktop_mgr,
    get_visible_bot,
    optional_service,
)
from app.schemas import (
    DesktopActionIn,
    DesktopOut,
    DesktopStreamTicketOut,
    DesktopWindowsOut,
    PendingApprovalOut,
    ScreenshotOut,
)

# classify_action_risk is the single source of truth for desktop action risk,
# shared with the inline routine path so both gate identically.
from app.services.browser import is_browser_action
from app.services.connectors import requires_approval
from app.services.desktop import (
    ACI_VNC_PASSWORD,
    DESKTOP_STREAM_CONNECT_TIMEOUT_SECONDS,
    DESKTOP_STREAM_DEFAULT_ASSET,
    DESKTOP_STREAM_IDLE_TIMEOUT_SECONDS,
    DESKTOP_STREAM_WS_UPSTREAM_PATH,
    StreamTicketError,
    classify_action_risk,
    filter_proxy_request_headers,
    filter_proxy_response_headers,
    max_risk,
    negotiate_stream_subprotocol,
    stream_asset_url,
    stream_origin,
    stream_tickets,
    stream_ws_url,
)

logger = logging.getLogger("nesqbot.desktop")

router = APIRouter(tags=["desktop"])


def _out(desktop: BotDesktop) -> DesktopOut:
    return DesktopOut(
        bot_id=desktop.bot_id,
        state=desktop.state,
        stream_url=desktop.stream_url,
        control_url=desktop.control_url,
        container_id=desktop.container_id,
        last_error=desktop.last_error,
    )


async def _desktop_call(name: str, db: AsyncSession, bot_id: uuid.UUID, **kwargs: Any) -> Any:
    """Invoke a desktop capability, from the manager or the module, whichever exists."""
    method = getattr(desktop_mgr, name, None)
    if method is not None:
        return await method(db, bot_id, **kwargs)
    service = optional_service("desktop")
    fn = getattr(service, name, None) if service else None
    if fn is None:
        raise AppError(
            503,
            "not_implemented",
            f"desktop.{name} is not implemented in this build",
        )
    return await fn(db, bot_id, **kwargs)


@router.get("/bots/{bot_id}/desktop", response_model=DesktopOut)
async def desktop_get(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesktopOut:
    await get_visible_bot(db, bot_id, user)
    return _out(await desktop_mgr.get(db, bot_id))


@router.post("/bots/{bot_id}/desktop/start", response_model=DesktopOut)
async def desktop_start(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesktopOut:
    bot = await get_visible_bot(db, bot_id, user)
    return _out(await desktop_mgr.start(db, bot))


@router.post("/bots/{bot_id}/desktop/stop", response_model=DesktopOut)
async def desktop_stop(
    bot_id: uuid.UUID,
    wipe: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesktopOut:
    await get_visible_bot(db, bot_id, user)
    return _out(await desktop_mgr.stop(db, bot_id, wipe=wipe))


@router.post("/bots/{bot_id}/desktop/suspend", response_model=DesktopOut)
async def desktop_suspend(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesktopOut:
    await get_visible_bot(db, bot_id, user)
    return _out(await desktop_mgr.suspend(db, bot_id))


@router.post("/bots/{bot_id}/desktop/resume", response_model=DesktopOut)
async def desktop_resume(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesktopOut:
    await get_visible_bot(db, bot_id, user)
    return _out(await _desktop_call("resume", db, bot_id))


@router.post("/bots/{bot_id}/desktop/action")
async def desktop_action(
    bot_id: uuid.UUID,
    body: DesktopActionIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Run a computer-use action. send/spend/delete actions become approvals."""
    await get_visible_bot(db, bot_id, user)
    payload = body.model_dump(exclude_none=True)
    action = payload.pop("action")
    declared = payload.pop("risk", None)

    # `browser_*` names classify in `services.risk` like every other action, so
    # without this they would pass the gate and then be POSTed to the sidecar's
    # pixel `/action`, which has never heard of them — a 422 from two layers
    # down, dressed up as an action failure. This route is the pixel surface;
    # the DOM surface reaches Chromium through the agent's browser tools and
    # `simulation.perform`, and there is deliberately no second HTTP path to it.
    if is_browser_action(action):
        raise AppError(
            400,
            "not_a_pixel_action",
            f"'{action}' drives the browser over CDP, not the pixel API. It is an agent "
            "tool and runs through the risk gate on a chat turn; this route takes the "
            "pixel primitives only.",
        )
    # Escalate-only, same rule as McpCallIn and the routine step branches: the
    # caller may raise the classification, never lower it.
    risk = max_risk(classify_action_risk(action), str(declared or "observe"))

    if requires_approval(risk):
        approval = await create_gated_approval(
            db,
            bot_id=bot_id,
            risk=risk,
            title=f"Desktop action: {action}",
            summary=f"Bot Desktop wants to run '{action}' (risk={risk})",
            payload={
                "kind": "desktop_steps",
                "steps": [{"action": action, **payload}],
                REQUESTED_BY_KEY: str(user.id),
            },
            actor=user,
        )
        db.add(
            AuditEvent(
                actor_user_id=user.id,
                bot_id=bot_id,
                event_type="desktop_action_held",
                detail={"action": action, "risk": risk, "approval_id": str(approval.id)},
            )
        )
        await db.commit()
        response.status_code = status.HTTP_201_CREATED
        return PendingApprovalOut(
            approval_id=approval.id,
            status="pending_approval",
            risk=risk,
            title=approval.title,
        ).model_dump(mode="json")

    result = await desktop_mgr.computer_action(db, bot_id, action, payload)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="desktop_action",
            detail={"action": action, "result_ok": result.get("ok")},
        )
    )
    await db.commit()
    return result


@router.get("/bots/{bot_id}/desktop/screenshot", response_model=ScreenshotOut)
async def desktop_screenshot(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScreenshotOut:
    """Proxy the sidecar `/screenshot`; mock mode returns a placeholder PNG."""
    await get_visible_bot(db, bot_id, user)
    result = await _desktop_call("screenshot", db, bot_id)
    if isinstance(result, ScreenshotOut):
        return result
    if not isinstance(result, dict):
        raise AppError(502, "upstream_error", "Unexpected screenshot payload from the sidecar")
    return ScreenshotOut(**{k: v for k, v in result.items() if k in ScreenshotOut.model_fields})


@router.get("/bots/{bot_id}/desktop/windows", response_model=DesktopWindowsOut)
async def desktop_windows(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesktopWindowsOut:
    """Proxy the sidecar `/windows`."""
    await get_visible_bot(db, bot_id, user)
    result = await _desktop_call("windows", db, bot_id)
    if isinstance(result, DesktopWindowsOut):
        return result
    if not isinstance(result, dict):
        raise AppError(502, "upstream_error", "Unexpected windows payload from the sidecar")
    return DesktopWindowsOut(
        **{k: v for k, v in result.items() if k in DesktopWindowsOut.model_fields}
    )


# ---------------------------------------------------------------------------
# Stream proxy
#
# `desktop.stream_url` points at a private address inside the delegated subnet
# (`http://10.60.4.x:6901`). That is deliberate - per-bot network isolation is
# the product claim - and it is also why a laptop could never render it. These
# three routes are the supported way to see a Bot Desktop:
#
#   POST /bots/{bot_id}/desktop/stream/ticket                  mint a capability
#   GET  /bots/{bot_id}/desktop/stream/{ticket}/{asset_path}   noVNC's HTML/JS/CSS
#   WS   /bots/{bot_id}/desktop/stream/{ticket}/websockify     the VNC transport
#
# The WebSocket leg is not optional. noVNC fetches its assets over HTTP and then
# opens an RFC 6455 connection for every pixel; without it the page loads and
# never paints.
#
# Authorization is the same as everywhere else in this router - `get_visible_bot`
# on the mint, and re-checked against the ticket's user on each redeem, so a
# ticket does not outlive the access that produced it. See
# `app.services.desktop` for why the capability is a signed path segment rather
# than a header, a cookie or the session JWT.
# ---------------------------------------------------------------------------

#: WebSocket close codes. 1000-2999 are reserved by the protocol, so the
#: application range mirrors the HTTP status the same failure would have had.
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_NOT_FOUND = 4404
WS_CLOSE_NOT_RUNNING = 4409
WS_CLOSE_UPSTREAM = 4502
WS_CLOSE_IDLE = 4408


def _stream_prefix(bot_id: uuid.UUID, ticket: str) -> str:
    """Path of one viewing session, relative to the API root (the `/api` mount)."""
    return f"/bots/{bot_id}/desktop/stream/{ticket}"


async def _resolve_ticket(
    db: AsyncSession,
    bot_id: uuid.UUID,
    ticket: str,
    *,
    consume: bool,
) -> tuple[BotDesktop, str]:
    """Authorize one proxied request and return `(desktop, upstream origin)`.

    Three checks, in this order, because each is cheaper than the next and each
    can fail on its own:

    1. the ticket verifies - signature, expiry, and it names *this* bot;
    2. the user it was minted for still exists and can still see this bot, so a
       ticket cannot outlive the access that produced it;
    3. the desktop is running and has an address to proxy to.

    `consume=True` burns the ticket. Only the WebSocket does that: the assets
    would be unloadable if every GET spent it.
    """
    try:
        claims = (
            await stream_tickets.redeem(ticket, bot_id=bot_id)
            if consume
            else stream_tickets.verify(ticket, bot_id=bot_id)
        )
    except StreamTicketError as exc:
        # One indistinguishable rejection, as with the Entra failures in
        # `app.auth`: naming the failed check helps only a forger.
        logger.info("desktop stream ticket rejected for bot %s: %s", bot_id, exc.reason)
        raise AppError(401, "invalid_stream_ticket", "Stream ticket is invalid or expired") from exc

    user = await db.get(User, claims.user_id)
    if user is None:
        raise AppError(401, "invalid_stream_ticket", "Stream ticket is invalid or expired")
    bot = await db.get(Bot, bot_id)
    if bot is None or not can_see_bot(bot, user):
        raise AppError(404, "bot_not_found", "Bot not found")

    desktop = await desktop_mgr.get(db, bot_id)
    origin = stream_origin(desktop)
    if desktop.state != "running" or not origin:
        raise AppError(409, "desktop_not_running", "This bot's desktop is not running")
    return desktop, origin


@router.post("/bots/{bot_id}/desktop/stream/ticket", response_model=DesktopStreamTicketOut)
async def desktop_stream_ticket(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesktopStreamTicketOut:
    """Mint a short-lived capability to view this bot's desktop.

    This is the only authenticated step in the flow, so it carries the whole
    authorization decision: an unauthenticated caller gets 401 from
    `get_current_user`, and someone else's bot gets 404 from `get_visible_bot`,
    exactly like every other route here.
    """
    await get_visible_bot(db, bot_id, user)
    desktop = await desktop_mgr.get(db, bot_id)
    origin = stream_origin(desktop)
    if desktop.state != "running" or not origin:
        raise AppError(
            409,
            "desktop_not_running",
            "Start this bot's desktop before asking for a stream ticket",
        )

    ticket = stream_tickets.mint(bot_id=bot_id, user_id=user.id)
    prefix = _stream_prefix(bot_id, ticket.token)
    return DesktopStreamTicketOut(
        ticket=ticket.token,
        expires_at=ticket.expires_at,
        expires_in=ticket.expires_in,
        stream_path=f"{prefix}/{DESKTOP_STREAM_DEFAULT_ASSET}",
        ws_path=f"{prefix}/{DESKTOP_STREAM_WS_UPSTREAM_PATH}",
        # Handed over so the viewer can answer the VNC server's prompt. Every
        # driver bakes the same `VNC_PW` into the image and nothing on the read
        # side has ever had a way to learn a per-bot one (see ACI_VNC_PASSWORD),
        # so withholding it here would only mean a password box no human can
        # fill in. It is not the boundary; the private IP and this ticket are.
        vnc_password=ACI_VNC_PASSWORD,
    )


@router.get("/bots/{bot_id}/desktop/stream/{ticket}/{asset_path:path}")
async def desktop_stream_asset(
    bot_id: uuid.UUID,
    ticket: str,
    asset_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Relay one noVNC asset from the desktop's private endpoint.

    Streamed, never buffered: `aiter_raw` hands the client bytes as they arrive
    and the `finally` closes both the upstream response and its client, so a
    viewer that navigates away does not leave a socket open into the VNet.
    """
    _desktop, origin = await _resolve_ticket(db, bot_id, ticket, consume=False)

    try:
        url = stream_asset_url(origin, asset_path, request.url.query)
    except ValueError as exc:
        raise AppError(400, "invalid_stream_path", str(exc)) from exc

    timeout = get_settings().sidecar_timeout_seconds
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        upstream = await client.send(
            client.build_request("GET", url, headers=filter_proxy_request_headers(request.headers)),
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001 - an unreachable desktop is a 502, not a 500
        await client.aclose()
        logger.warning("desktop stream asset %s failed: %s", asset_path, exc)
        raise AppError(502, "upstream_error", "The bot desktop stream is not reachable") from exc

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=filter_proxy_response_headers(upstream.headers),
    )


async def _relay(websocket: WebSocket, upstream: Any, idle_timeout: float) -> int:
    """Pump frames both ways until either side stops, or nothing moves for a while.

    Three tasks race: client->upstream, upstream->client, and a watchdog over a
    shared last-activity stamp. Whichever finishes first ends the session and the
    other two are cancelled - which is what makes *both* disconnect directions
    tear the whole thing down instead of leaking the surviving half.

    The idle timer is deliberately over the pair, not per direction: a user
    reading a static screen sends nothing for minutes while the desktop keeps
    talking, and killing that would be a bug, not a timeout.
    """
    from websockets.exceptions import ConnectionClosed

    last_activity = time.monotonic()

    async def client_to_upstream() -> None:
        nonlocal last_activity
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            last_activity = time.monotonic()
            payload = message.get("bytes")
            if payload is not None:
                await upstream.send(payload)
                continue
            text = message.get("text")
            if text is not None:
                await upstream.send(text)

    async def upstream_to_client() -> None:
        nonlocal last_activity
        async for frame in upstream:
            last_activity = time.monotonic()
            if isinstance(frame, (bytes, bytearray, memoryview)):
                await websocket.send_bytes(bytes(frame))
            else:
                await websocket.send_text(str(frame))

    async def watchdog() -> None:
        tick = max(min(idle_timeout / 4, 1.0), 0.05)
        while True:
            await asyncio.sleep(tick)
            if time.monotonic() - last_activity >= idle_timeout:
                return

    idle = asyncio.create_task(watchdog(), name="desktop-ws-idle")
    tasks = [
        asyncio.create_task(client_to_upstream(), name="desktop-ws-up"),
        asyncio.create_task(upstream_to_client(), name="desktop-ws-down"),
        idle,
    ]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for task in done:
        exc = task.exception()
        if exc is not None and not isinstance(exc, (ConnectionClosed, WebSocketDisconnect)):
            raise exc
    # A session the watchdog ended is a different event from one either peer
    # ended, and the close code has to say so or an operator reading a browser
    # console cannot tell a timeout from a hang-up.
    return WS_CLOSE_IDLE if idle in done else 1000


@router.websocket("/bots/{bot_id}/desktop/stream/{ticket}/" + DESKTOP_STREAM_WS_UPSTREAM_PATH)
async def desktop_stream_ws(
    websocket: WebSocket,
    bot_id: uuid.UUID,
    ticket: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """The VNC transport, relayed frame for frame.

    Closing *before* `accept()` fails the handshake outright, which is what a
    rejected ticket deserves: the browser sees a failed connection rather than an
    open socket that then hangs up. The upstream is dialled before the client is
    accepted for the same reason - an accepted-then-immediately-closed socket
    reads to noVNC as a working server that hated the request.
    """
    # Imported here rather than at module scope, like the docker and azure SDKs
    # in `app.services.desktop`: nothing else in the API dials a WebSocket, and
    # the whole client stack should not load for a deployment that never streams.
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import InvalidHandshake
    from websockets.typing import Subprotocol

    try:
        _desktop, origin = await _resolve_ticket(db, bot_id, ticket, consume=True)
    except AppError as exc:
        code = {401: WS_CLOSE_UNAUTHORIZED, 404: WS_CLOSE_NOT_FOUND}.get(
            exc.status_code, WS_CLOSE_NOT_RUNNING
        )
        await websocket.close(code=code, reason=exc.code)
        return

    subprotocol = negotiate_stream_subprotocol(websocket.scope.get("subprotocols"))
    url = stream_ws_url(origin)
    try:
        upstream = await ws_connect(
            url,
            subprotocols=[Subprotocol(subprotocol)] if subprotocol else None,
            open_timeout=DESKTOP_STREAM_CONNECT_TIMEOUT_SECONDS,
            close_timeout=5,
            # A framebuffer update is arbitrarily large and must not be capped,
            # and websockify never answers our pings - the idle watchdog below is
            # the liveness check instead.
            max_size=None,
            ping_interval=None,
        )
    except (OSError, InvalidHandshake, TimeoutError) as exc:
        logger.warning("desktop stream upstream %s refused the relay: %s", url, exc)
        await websocket.close(code=WS_CLOSE_UPSTREAM, reason="upstream_error")
        return

    await websocket.accept(subprotocol=subprotocol)
    close_code = 1000
    try:
        close_code = await _relay(websocket, upstream, DESKTOP_STREAM_IDLE_TIMEOUT_SECONDS)
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        if websocket.client_state is WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close(code=close_code)
