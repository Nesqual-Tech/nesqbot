"""The Bot Desktop stream proxy: HTTP assets, the VNC WebSocket, and the ticket.

A Bot Desktop has no public address on purpose, so the only way a user ever sees
one is through the API. That makes these the most sensitive routes in the
product - a remote-control surface for a machine that may be signed into
corporate systems - and this module is where that claim is checked.

Everything here runs against a **fake upstream**: one `websockets` server that
serves noVNC-shaped static files for plain HTTP requests and speaks RFC 6455 for
upgrades, which is exactly the shape of the `websockify --web=<novnc root>`
process `infra/bot-desktop/entrypoint.sh` starts. No Azure, no container, no
network beyond loopback.

The WebSocket leg is driven by talking ASGI to the app directly, the same
technique `conftest.SSEProbe` uses for the event streams: `httpx.ASGITransport`
has no WebSocket support, and a real `TestClient` would need its own event loop
alongside the one holding the test transaction open.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
import pytest_asyncio

from app.services.desktop import stream_tickets

# ---------------------------------------------------------------------------
# A fake KasmVNC/websockify
# ---------------------------------------------------------------------------

#: What the fake serves over plain HTTP, keyed by request path.
FAKE_ASSETS: dict[str, tuple[bytes, str]] = {
    "/vnc.html": (b"<!doctype html><title>noVNC</title><script src='app/ui.js'></script>", "text/html"),
    "/app/ui.js": (b"// fake noVNC bootstrap\n", "application/javascript"),
}


class FakeDesktopUpstream:
    """One server standing in for `websockify --web=/usr/share/novnc 6901 ...`.

    Plain HTTP requests are answered from `FAKE_ASSETS` through `process_request`
    - websockets' hook for non-upgrade traffic, and the same one-port-serves-both
    arrangement the real websockify has. Upgrades reach `_session`, which greets,
    echoes, and hangs up when told to: enough to prove frames move in both
    directions and that each disconnect direction is handled.
    """

    def __init__(self) -> None:
        self.origin = ""
        self.received: list[Any] = []
        self.http_paths: list[str] = []
        self.http_headers: list[dict[str, str]] = []
        self.subprotocols: list[str | None] = []
        self.opened = 0
        self.closed = 0
        self._server: Any = None
        self._sessions_ended = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> FakeDesktopUpstream:
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._session,
            "127.0.0.1",
            0,
            process_request=self._process_request,
            # websockify takes `binary` when it is offered and is perfectly happy
            # without one; the library's default would fail the handshake on a
            # client that offers none, which real noVNC and old clients do.
            select_subprotocol=self._select_subprotocol,
        )
        self.origin = f"http://127.0.0.1:{self._port()}"
        return self

    def _port(self) -> int:
        sockets = getattr(self._server, "sockets", None)
        if not sockets:
            sockets = self._server.server.sockets
        return int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=5)

    # -- handlers ---------------------------------------------------------

    @staticmethod
    def _select_subprotocol(connection: Any, offered: Any) -> str | None:
        return "binary" if "binary" in list(offered or []) else None

    def _process_request(self, connection: Any, request: Any) -> Any:
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None  # let the handshake proceed

        self.http_paths.append(request.path)
        self.http_headers.append({k.lower(): v for k, v in request.headers.raw_items()})

        body, content_type = FAKE_ASSETS.get(request.path, (b"not here", "text/plain"))
        status, reason = (200, "OK") if request.path in FAKE_ASSETS else (404, "Not Found")
        headers = Headers()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        headers["Set-Cookie"] = "upstream=should-not-reach-the-browser"
        headers["Connection"] = "close"
        return Response(status, reason, headers, body)

    async def _session(self, connection: Any) -> None:
        from websockets.exceptions import ConnectionClosed

        self.opened += 1
        self.subprotocols.append(connection.subprotocol)
        try:
            await connection.send(b"HELLO")
            async for message in connection:
                self.received.append(message)
                if message == b"BYE":
                    await connection.close()
                    return
                payload = message if isinstance(message, bytes) else str(message).encode()
                await connection.send(b"echo:" + payload)
        except ConnectionClosed:
            pass
        finally:
            self.closed += 1
            self._sessions_ended.set()

    async def wait_for_session_end(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self._sessions_ended.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True


# ---------------------------------------------------------------------------
# Driving the app's WebSocket route over raw ASGI
# ---------------------------------------------------------------------------


class WSProbe:
    """A WebSocket client that speaks ASGI to the app in-process."""

    def __init__(self, path: str, *, subprotocols: tuple[str, ...] = ("binary",)) -> None:
        self.path = path
        self.subprotocols = list(subprotocols)
        self.sent: list[dict[str, Any]] = []
        self._inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._arrived = asyncio.Event()
        self._inbound.put_nowait({"type": "websocket.connect"})

    # -- ASGI callables ---------------------------------------------------

    @property
    def scope(self) -> dict[str, Any]:
        raw_path, _, query = self.path.partition("?")
        return {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": raw_path,
            "raw_path": raw_path.encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 50001),
            "server": ("testserver", 80),
            "subprotocols": self.subprotocols,
            "state": {},
        }

    async def receive(self) -> dict[str, Any]:
        return await self._inbound.get()

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        self._arrived.set()

    # -- client side ------------------------------------------------------

    def send_bytes(self, data: bytes) -> None:
        self._inbound.put_nowait({"type": "websocket.receive", "bytes": data})

    def send_text(self, data: str) -> None:
        self._inbound.put_nowait({"type": "websocket.receive", "text": data})

    def disconnect(self, code: int = 1000) -> None:
        self._inbound.put_nowait({"type": "websocket.disconnect", "code": code})

    # -- assertions -------------------------------------------------------

    @property
    def accept(self) -> dict[str, Any] | None:
        return next((m for m in self.sent if m["type"] == "websocket.accept"), None)

    @property
    def close(self) -> dict[str, Any] | None:
        return next((m for m in self.sent if m["type"] == "websocket.close"), None)

    @property
    def frames(self) -> list[Any]:
        out: list[Any] = []
        for message in self.sent:
            if message["type"] != "websocket.send":
                continue
            payload = message.get("bytes")
            out.append(payload if payload is not None else message.get("text"))
        return out

    async def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if predicate(self):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._arrived.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._arrived.wait(), timeout=min(remaining, 0.1))

    async def wait_for_frames(self, count: int, timeout: float = 5.0) -> bool:
        return await self.wait_for(lambda p: len(p.frames) >= count, timeout=timeout)

    async def wait_for_close(self, timeout: float = 5.0) -> bool:
        return await self.wait_for(lambda p: p.close is not None, timeout=timeout)


@contextlib.asynccontextmanager
async def open_ws(app_obj, path: str, *, subprotocols: tuple[str, ...] = ("binary",)):
    """Run one WebSocket session against the app; always tears the task down."""
    probe = WSProbe(path, subprotocols=subprotocols)
    task = asyncio.create_task(app_obj(probe.scope, probe.receive, probe.send))
    try:
        yield probe
    finally:
        probe.disconnect()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        for result in await asyncio.gather(task, return_exceptions=True):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_ticket_claims():
    """Replay claims are process-wide; keep them from leaking between tests."""
    stream_tickets._claimed.clear()
    yield
    stream_tickets._claimed.clear()


@pytest_asyncio.fixture
async def upstream():
    fake = FakeDesktopUpstream()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest_asyncio.fixture
async def running_desktop(db, bot_a, upstream):
    """`bot_a` with a desktop whose stream_url points at the fake upstream."""
    from app.models import BotDesktop

    row = BotDesktop(
        bot_id=bot_a.id,
        state="running",
        container_id="fake-group",
        stream_url=upstream.origin,
        control_url=upstream.origin,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def mint(authed, bot_id) -> dict[str, Any]:
    response = await authed.post(f"/api/bots/{bot_id}/desktop/stream/ticket")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Minting: the one authenticated step, so it carries the whole decision
# ---------------------------------------------------------------------------


async def test_minting_a_ticket_requires_authentication(anon, bot_a, running_desktop):
    response = await anon.post(f"/api/bots/{bot_a.id}/desktop/stream/ticket")
    assert response.status_code == 401


async def test_minting_a_ticket_for_someone_elses_bot_is_a_404(other, bot_a, running_desktop):
    """404, not 403: the existence of another tenant's bot is not public."""
    response = await other.post(f"/api/bots/{bot_a.id}/desktop/stream/ticket")
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


async def test_a_ticket_is_refused_while_the_desktop_is_not_running(authed, bot_a):
    response = await authed.post(f"/api/bots/{bot_a.id}/desktop/stream/ticket")
    assert response.status_code == 409
    assert response.json()["code"] == "desktop_not_running"


async def test_the_ticket_names_the_paths_the_viewer_should_use(authed, bot_a, running_desktop):
    body = await mint(authed, bot_a.id)
    prefix = f"/bots/{bot_a.id}/desktop/stream/{body['ticket']}"
    assert body["stream_path"] == f"{prefix}/vnc.html"
    assert body["ws_path"] == f"{prefix}/websockify"
    assert 0 < body["expires_in"] <= 60
    assert body["expires_at"]
    # The viewer has to be able to answer the VNC prompt without asking a human
    # for a password nobody was ever told.
    assert body["vnc_password"]


async def test_two_mints_are_two_different_tickets(authed, bot_a, running_desktop):
    first, second = await mint(authed, bot_a.id), await mint(authed, bot_a.id)
    assert first["ticket"] != second["ticket"]


# ---------------------------------------------------------------------------
# The HTTP leg
# ---------------------------------------------------------------------------


async def test_the_asset_proxy_serves_the_novnc_page(client, authed, bot_a, running_desktop):
    ticket = (await mint(authed, bot_a.id))["ticket"]
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket}/vnc.html")
    assert response.status_code == 200
    assert response.content == FAKE_ASSETS["/vnc.html"][0]
    assert response.headers["content-type"] == "text/html"
    assert response.headers["cache-control"] == "no-store"


async def test_the_asset_proxy_follows_relative_asset_paths(
    client, authed, bot_a, running_desktop, upstream
):
    """`vnc.html` pulls `app/ui.js` relative to itself, so the ticket prefix has to survive."""
    ticket = (await mint(authed, bot_a.id))["ticket"]
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket}/app/ui.js")
    assert response.status_code == 200
    assert response.content == FAKE_ASSETS["/app/ui.js"][0]
    assert upstream.http_paths[-1] == "/app/ui.js"


async def test_an_empty_path_lands_on_the_novnc_page(client, authed, bot_a, running_desktop, upstream):
    ticket = (await mint(authed, bot_a.id))["ticket"]
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket}/")
    assert response.status_code == 200
    assert upstream.http_paths[-1] == "/vnc.html"


async def test_the_asset_proxy_passes_an_upstream_404_through(client, authed, bot_a, running_desktop):
    ticket = (await mint(authed, bot_a.id))["ticket"]
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket}/nope.js")
    assert response.status_code == 404


async def test_the_asset_proxy_does_not_leak_the_upstreams_cookies(
    client, authed, bot_a, running_desktop
):
    """The desktop is driven by an LLM over hostile content; it does not get to set cookies."""
    ticket = (await mint(authed, bot_a.id))["ticket"]
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket}/vnc.html")
    assert "set-cookie" not in {k.lower() for k in response.headers}


async def test_the_asset_proxy_does_not_forward_the_callers_credentials(
    authed, bot_a, running_desktop, upstream
):
    """A session bearer must never reach a container an LLM is driving."""
    ticket = (await mint(authed, bot_a.id))["ticket"]
    response = await authed.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket}/vnc.html")
    assert response.status_code == 200
    forwarded = upstream.http_headers[-1]
    assert "authorization" not in forwarded
    assert "cookie" not in forwarded


async def test_the_asset_proxy_refuses_path_traversal(client, authed, bot_a, running_desktop, upstream):
    ticket = (await mint(authed, bot_a.id))["ticket"]
    response = await client.get(
        f"/api/bots/{bot_a.id}/desktop/stream/{ticket}/%2E%2E/%2E%2E/etc/passwd"
    )
    # 400 when the guard sees it; 404 if the client library normalised the dot
    # segments away first. Either way it must never reach the upstream.
    assert response.status_code in (400, 404)
    if response.status_code == 400:
        assert response.json()["code"] == "invalid_stream_path"
    assert upstream.http_paths == []


async def test_the_asset_proxy_refuses_an_unsigned_ticket(client, bot_a, running_desktop):
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/not-a-ticket/vnc.html")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_stream_ticket"


async def test_the_asset_proxy_refuses_a_tampered_ticket(client, authed, bot_a, running_desktop):
    ticket = (await mint(authed, bot_a.id))["ticket"]
    payload, _, signature = ticket.partition(".")
    forged = f"{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{forged}/vnc.html")
    assert response.status_code == 401


async def test_a_ticket_is_bound_to_the_bot_it_was_minted_for(
    client, authed, bot_a, bot_b, running_desktop
):
    """A valid signature is not enough - the ticket names one desktop."""
    ticket = (await mint(authed, bot_a.id))["ticket"]
    response = await client.get(f"/api/bots/{bot_b.id}/desktop/stream/{ticket}/vnc.html")
    assert response.status_code == 401


async def test_a_ticket_expires(client, bot_a, user_a, running_desktop):
    ticket = stream_tickets.mint(bot_id=bot_a.id, user_id=user_a.id, ttl_seconds=1)
    await asyncio.sleep(1.1)
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket.token}/vnc.html")
    assert response.status_code == 401


async def test_a_ticket_minted_for_a_user_who_cannot_see_the_bot_is_refused(
    client, bot_a, user_b, running_desktop
):
    """Authorization is re-checked on every redeem, not just at mint time."""
    ticket = stream_tickets.mint(bot_id=bot_a.id, user_id=user_b.id)
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket.token}/vnc.html")
    assert response.status_code == 404


async def test_a_ticket_for_a_bot_that_does_not_exist_is_refused(client, user_a):
    missing = uuid.uuid4()
    ticket = stream_tickets.mint(bot_id=missing, user_id=user_a.id)
    response = await client.get(f"/api/bots/{missing}/desktop/stream/{ticket.token}/vnc.html")
    assert response.status_code == 404


async def test_the_asset_proxy_answers_502_when_the_desktop_is_unreachable(
    client, authed, bot_a, running_desktop, upstream
):
    ticket = (await mint(authed, bot_a.id))["ticket"]
    await upstream.stop()
    response = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{ticket}/vnc.html")
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_error"


# ---------------------------------------------------------------------------
# The WebSocket leg
# ---------------------------------------------------------------------------


async def test_the_websocket_relays_frames_in_both_directions(
    app, authed, bot_a, running_desktop, upstream
):
    body = await mint(authed, bot_a.id)
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for(lambda p: p.accept is not None), ws.sent
        assert ws.accept["subprotocol"] == "binary"

        # Upstream -> client: the fake greets as soon as it is connected.
        assert await ws.wait_for_frames(1), ws.sent
        assert ws.frames[0] == b"HELLO"

        # Client -> upstream, and the echo back again.
        ws.send_bytes(b"RFB 003.008")
        assert await ws.wait_for_frames(2), ws.sent
        assert ws.frames[1] == b"echo:RFB 003.008"
        assert upstream.received == [b"RFB 003.008"]


async def test_the_websocket_relays_text_frames_too(app, authed, bot_a, running_desktop, upstream):
    body = await mint(authed, bot_a.id)
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for_frames(1), ws.sent
        ws.send_text("hello")
        assert await ws.wait_for_frames(2), ws.sent
        assert upstream.received == ["hello"]


async def test_the_websocket_ticket_is_single_use(app, authed, bot_a, running_desktop, upstream):
    body = await mint(authed, bot_a.id)
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for_frames(1), ws.sent

    async with open_ws(app, f"/api{body['ws_path']}") as replay:
        assert await replay.wait_for_close(), replay.sent
        assert replay.accept is None, "a redeemed ticket must not open a second relay"
        assert replay.close["code"] == 4401
    assert upstream.opened == 1


async def test_redeeming_a_ticket_leaves_the_page_it_is_serving_alive(
    app, client, authed, bot_a, running_desktop
):
    """noVNC keeps fetching after it connects; burning the assets would half-paint it.

    Single-use is a property of the *control* connection, which the test above
    covers. The static files stay reachable until the ticket expires.
    """
    body = await mint(authed, bot_a.id)
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for_frames(1), ws.sent

    after = await client.get(f"/api/bots/{bot_a.id}/desktop/stream/{body['ticket']}/app/ui.js")
    assert after.status_code == 200


async def test_the_websocket_refuses_an_unknown_ticket(app, bot_a, running_desktop, upstream):
    async with open_ws(app, f"/api/bots/{bot_a.id}/desktop/stream/rubbish/websockify") as ws:
        assert await ws.wait_for_close(), ws.sent
        assert ws.accept is None
        assert ws.close["code"] == 4401
    assert upstream.opened == 0, "the relay must not dial upstream before authorizing"


async def test_the_websocket_refuses_a_ticket_for_another_users_bot(
    app, bot_a, user_b, running_desktop, upstream
):
    ticket = stream_tickets.mint(bot_id=bot_a.id, user_id=user_b.id)
    async with open_ws(app, f"/api/bots/{bot_a.id}/desktop/stream/{ticket.token}/websockify") as ws:
        assert await ws.wait_for_close(), ws.sent
        assert ws.accept is None
        assert ws.close["code"] == 4404
    assert upstream.opened == 0


async def test_the_websocket_reports_an_unreachable_desktop(
    app, authed, bot_a, running_desktop, upstream
):
    body = await mint(authed, bot_a.id)
    await upstream.stop()
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for_close(timeout=15.0), ws.sent
        assert ws.accept is None, "never accept a socket we cannot serve"
        assert ws.close["code"] == 4502


async def test_the_websocket_refuses_a_desktop_that_stopped(app, authed, bot_a, running_desktop, db):
    body = await mint(authed, bot_a.id)
    running_desktop.state = "absent"
    running_desktop.stream_url = None
    await db.commit()
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for_close(), ws.sent
        assert ws.close["code"] == 4409


async def test_a_client_disconnect_tears_the_upstream_down(
    app, authed, bot_a, running_desktop, upstream
):
    body = await mint(authed, bot_a.id)
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for_frames(1), ws.sent
        ws.disconnect()
        assert await upstream.wait_for_session_end(), "the upstream socket outlived the viewer"
    assert upstream.closed == 1


async def test_an_upstream_disconnect_closes_the_client(app, authed, bot_a, running_desktop, upstream):
    body = await mint(authed, bot_a.id)
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for_frames(1), ws.sent
        ws.send_bytes(b"BYE")  # the fake hangs up on this
        assert await ws.wait_for_close(), ws.sent
        assert ws.close["code"] == 1000
    assert upstream.closed == 1


async def test_an_idle_relay_is_closed(app, authed, bot_a, running_desktop, upstream, monkeypatch):
    import app.routers.desktop as desktop_router

    monkeypatch.setattr(desktop_router, "DESKTOP_STREAM_IDLE_TIMEOUT_SECONDS", 0.2)
    body = await mint(authed, bot_a.id)
    async with open_ws(app, f"/api{body['ws_path']}") as ws:
        assert await ws.wait_for_frames(1), ws.sent
        assert await ws.wait_for_close(timeout=5.0), ws.sent
        assert ws.close["code"] == 4408
    assert upstream.closed == 1


async def test_the_websocket_accepts_a_client_that_offers_no_subprotocol(
    app, authed, bot_a, running_desktop
):
    body = await mint(authed, bot_a.id)
    async with open_ws(app, f"/api{body['ws_path']}", subprotocols=()) as ws:
        assert await ws.wait_for(lambda p: p.accept is not None), ws.sent
        assert ws.accept["subprotocol"] is None
        assert await ws.wait_for_frames(1), ws.sent
