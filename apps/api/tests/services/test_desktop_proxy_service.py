"""Unit tests for the stream-proxy primitives in `app.services.desktop`.

The route-level suite (`tests/test_desktop_stream_proxy.py`) proves the proxy
works end to end against a fake websockify. This module pins the pieces it is
built from, where the failure modes are cheap to state and expensive to notice:
the ticket's signature, its binding, its expiry, its single redemption, and the
URL/header handling that decides where a proxied request is actually aimed.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services.desktop import (
    DESKTOP_STREAM_DEFAULT_ASSET,
    StreamTicketError,
    filter_proxy_request_headers,
    filter_proxy_response_headers,
    negotiate_stream_subprotocol,
    normalise_stream_path,
    stream_asset_url,
    stream_origin,
    stream_tickets,
    stream_ws_url,
)


class _Desktop:
    def __init__(self, stream_url):
        self.stream_url = stream_url


@pytest.fixture(autouse=True)
def _fresh_claims():
    stream_tickets._claimed.clear()
    yield
    stream_tickets._claimed.clear()


# ---------------------------------------------------------------------------
# Where the proxy points
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("http://10.60.4.7:6901", "http://10.60.4.7:6901"),
        ("http://localhost:6947", "http://localhost:6947"),
        # The mock driver writes a query string onto it; only the origin matters.
        ("http://localhost:6901/?bot=sales", "http://localhost:6901"),
        ("https://desktop.internal:6901/vnc.html", "https://desktop.internal:6901"),
        ("10.60.4.7:6901", "http://10.60.4.7:6901"),
        ("", ""),
        (None, ""),
        # Not a transport this proxy can speak; refuse rather than improvise.
        ("file:///etc/passwd", ""),
    ],
)
def test_stream_origin_reduces_whatever_the_driver_stored(stored, expected):
    assert stream_origin(_Desktop(stored)) == expected


def test_stream_origin_of_a_row_without_the_attribute_is_empty():
    assert stream_origin(object()) == ""


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("vnc.html", "vnc.html"),
        ("/vnc.html", "vnc.html"),
        ("app/ui.js", "app/ui.js"),
        ("app/images/icons/novnc.svg", "app/images/icons/novnc.svg"),
        ("", DESKTOP_STREAM_DEFAULT_ASSET),
        ("/", DESKTOP_STREAM_DEFAULT_ASSET),
        (".", DESKTOP_STREAM_DEFAULT_ASSET),
        (None, DESKTOP_STREAM_DEFAULT_ASSET),
        # A dot segment that does not escape is harmless.
        ("app/./ui.js", "app/./ui.js"),
    ],
)
def test_normalise_stream_path_accepts_what_novnc_asks_for(given, expected):
    assert normalise_stream_path(given) == expected


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "app/../../secret",
        "..",
        "//evil.example.com/",
        "http://evil.example.com/",
        "app\\ui.js",
        "vnc.html\r\nX-Injected: 1",
        "vnc.html\x00",
    ],
)
def test_normalise_stream_path_refuses_anything_that_leaves_the_desktop(hostile):
    """This proxy is a hole through the VNet boundary; the path is validated, not quoted."""
    with pytest.raises(ValueError):
        normalise_stream_path(hostile)


def test_stream_asset_url_keeps_the_query_string():
    url = stream_asset_url("http://10.0.0.4:6901", "vnc.html", "resize=scale")
    assert url == "http://10.0.0.4:6901/vnc.html?resize=scale"


def test_stream_ws_url_matches_the_origins_transport():
    assert stream_ws_url("http://10.0.0.4:6901") == "ws://10.0.0.4:6901/websockify"
    assert stream_ws_url("https://10.0.0.4:6901") == "wss://10.0.0.4:6901/websockify"


# ---------------------------------------------------------------------------
# Header handling
# ---------------------------------------------------------------------------


def test_only_harmless_request_headers_reach_the_desktop():
    forwarded = filter_proxy_request_headers(
        {
            "Authorization": "Bearer super-secret",
            "Cookie": "session=super-secret",
            "Host": "api.example.com",
            "Accept": "text/html",
            "X-Nesq-Dev": "1",
        }
    )
    assert forwarded == {"accept": "text/html", "accept-encoding": "identity"}


def test_only_harmless_response_headers_reach_the_browser():
    returned = filter_proxy_response_headers(
        {
            "Content-Type": "text/html",
            "Content-Length": "42",
            "Set-Cookie": "pwned=1",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'",
        }
    )
    assert returned == {
        "content-type": "text/html",
        "content-length": "42",
        "cache-control": "no-store",
    }


@pytest.mark.parametrize(
    ("offered", "expected"),
    [
        (["binary"], "binary"),
        (["base64"], "base64"),
        (["base64", "binary"], "binary"),
        (["chat"], "chat"),
        ([], None),
        (None, None),
    ],
)
def test_the_subprotocol_echoed_back_is_one_the_client_offered(offered, expected):
    """RFC 6455: answering with a subprotocol the client did not offer fails the handshake."""
    assert negotiate_stream_subprotocol(offered) == expected


# ---------------------------------------------------------------------------
# The ticket
# ---------------------------------------------------------------------------


def test_a_minted_ticket_verifies():
    bot_id, user_id = uuid.uuid4(), uuid.uuid4()
    ticket = stream_tickets.mint(bot_id=bot_id, user_id=user_id)
    seen = stream_tickets.verify(ticket.token, bot_id=bot_id)
    assert seen.bot_id == bot_id
    assert seen.user_id == user_id
    assert 0 < seen.expires_in <= 60


def test_a_ticket_is_opaque_and_url_path_safe():
    ticket = stream_tickets.mint(bot_id=uuid.uuid4(), user_id=uuid.uuid4())
    assert "/" not in ticket.token and "?" not in ticket.token and "#" not in ticket.token
    # The claims must not be legible from the wire form on their own.
    assert str(ticket.bot_id) not in ticket.token


def test_two_tickets_for_the_same_pair_differ():
    bot_id, user_id = uuid.uuid4(), uuid.uuid4()
    first = stream_tickets.mint(bot_id=bot_id, user_id=user_id)
    second = stream_tickets.mint(bot_id=bot_id, user_id=user_id)
    assert first.token != second.token, "a nonce-free ticket could not be single-use"


@pytest.mark.parametrize("token", ["", "nonsense", "a.b", "....", "abc."])
def test_a_ticket_that_is_not_a_ticket_is_refused(token):
    with pytest.raises(StreamTicketError):
        stream_tickets.verify(token, bot_id=uuid.uuid4())


def test_a_resigned_payload_is_refused():
    """The signature is over the encoded claims, so re-encoding them is not enough."""
    bot_id = uuid.uuid4()
    ticket = stream_tickets.mint(bot_id=bot_id, user_id=uuid.uuid4())
    payload, _, signature = ticket.token.partition(".")
    flipped = ("B" if signature[0] != "B" else "C") + signature[1:]
    with pytest.raises(StreamTicketError):
        stream_tickets.verify(f"{payload}.{flipped}", bot_id=bot_id)


def test_a_ticket_is_useless_against_another_bot():
    ticket = stream_tickets.mint(bot_id=uuid.uuid4(), user_id=uuid.uuid4())
    with pytest.raises(StreamTicketError):
        stream_tickets.verify(ticket.token, bot_id=uuid.uuid4())


async def test_a_ticket_expires():
    bot_id = uuid.uuid4()
    ticket = stream_tickets.mint(bot_id=bot_id, user_id=uuid.uuid4(), ttl_seconds=1)
    stream_tickets.verify(ticket.token, bot_id=bot_id)
    await asyncio.sleep(1.05)
    with pytest.raises(StreamTicketError):
        stream_tickets.verify(ticket.token, bot_id=bot_id)


async def test_a_ticket_can_only_be_redeemed_once():
    bot_id = uuid.uuid4()
    ticket = stream_tickets.mint(bot_id=bot_id, user_id=uuid.uuid4())
    assert (await stream_tickets.redeem(ticket.token, bot_id=bot_id)).bot_id == bot_id
    with pytest.raises(StreamTicketError):
        await stream_tickets.redeem(ticket.token, bot_id=bot_id)


async def test_concurrent_redemptions_of_one_ticket_produce_exactly_one_winner():
    """Two tabs racing on the same ticket must not both get a control connection."""
    bot_id = uuid.uuid4()
    ticket = stream_tickets.mint(bot_id=bot_id, user_id=uuid.uuid4())
    results = await asyncio.gather(
        *(stream_tickets.redeem(ticket.token, bot_id=bot_id) for _ in range(5)),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1, results


async def test_a_redeemed_ticket_still_verifies_for_static_assets():
    """Only the control connection is single-use; the noVNC files are not."""
    bot_id = uuid.uuid4()
    ticket = stream_tickets.mint(bot_id=bot_id, user_id=uuid.uuid4())
    await stream_tickets.redeem(ticket.token, bot_id=bot_id)
    assert stream_tickets.verify(ticket.token, bot_id=bot_id).bot_id == bot_id


def test_a_ticket_signed_with_a_different_secret_is_refused(monkeypatch):
    """Signed, not stored: the only thing standing between a forged ticket and a
    desktop is JWT_SECRET, so a ticket from a different secret must not verify."""
    from app.config import get_settings

    bot_id = uuid.uuid4()
    ticket = stream_tickets.mint(bot_id=bot_id, user_id=uuid.uuid4())

    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_secret", "a-completely-different-secret")
    with pytest.raises(StreamTicketError):
        stream_tickets.verify(ticket.token, bot_id=bot_id)
