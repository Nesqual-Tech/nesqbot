"""`RateLimitMiddleware`: a token bucket per caller, inert at zero.

Exercised against a tiny ASGI app rather than the real one: the production
app registers the middleware at import time with whatever the settings say,
and what these tests are about is the bucket arithmetic and the 429 shape,
not the wiring — `test_the_app_registers_it_inert` covers that end.
"""

from __future__ import annotations

import httpx
import pytest

from app.middleware import RateLimitMiddleware


async def _hello(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"hi"})


def _client(per_minute: int, burst: int | None = None, headers=None):
    limited = RateLimitMiddleware(_hello, per_minute=per_minute, burst=burst)
    transport = httpx.ASGITransport(app=limited, client=("10.0.0.1", 1234))
    return httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers or {})


async def test_zero_disables_it():
    async with _client(0) as c:
        for _ in range(50):
            assert (await c.get("/")).status_code == 200


async def test_the_burst_is_allowed_then_429_with_the_error_envelope():
    async with _client(60, burst=3) as c:
        for _ in range(3):
            ok = await c.get("/")
            assert ok.status_code == 200
            assert ok.headers["x-ratelimit-limit"] == "60"
        blocked = await c.get("/")
        assert blocked.status_code == 429
        body = blocked.json()
        assert body["code"] == "rate_limited"
        assert "detail" in body
        assert int(blocked.headers["retry-after"]) >= 1
        assert blocked.headers["x-ratelimit-remaining"] == "0"


async def test_the_remaining_header_counts_down():
    async with _client(60, burst=3) as c:
        seen = [(await c.get("/")).headers["x-ratelimit-remaining"] for _ in range(3)]
    assert seen == ["2", "1", "0"]


async def test_buckets_are_per_bearer_token_not_per_address():
    async with _client(60, burst=1, headers={"Authorization": "Bearer alpha"}) as a:
        assert (await a.get("/")).status_code == 200
        assert (await a.get("/")).status_code == 429
    # Same address, different token: a fresh bucket.
    async with _client(60, burst=1, headers={"Authorization": "Bearer beta"}) as b:
        assert (await b.get("/")).status_code == 200


async def test_the_bucket_refills_over_time(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.middleware.time.monotonic", lambda: now[0])
    async with _client(60, burst=1) as c:  # one token a second
        assert (await c.get("/")).status_code == 200
        assert (await c.get("/")).status_code == 429
        now[0] += 1.1
        assert (await c.get("/")).status_code == 200


def test_eviction_keeps_the_table_bounded():
    limited = RateLimitMiddleware(_hello, per_minute=60, burst=1, max_keys=5)
    for i in range(50):
        limited._take(f"ip:{i}", 1000.0)
    limited._evict(1000.0 + 3600)
    assert len(limited._buckets) <= 5


@pytest.mark.parametrize("per_minute, burst, expected", [(60, None, 60), (10, 25, 25), (0, None, 1)])
def test_burst_defaults_to_the_rate(per_minute, burst, expected):
    assert RateLimitMiddleware(_hello, per_minute=per_minute, burst=burst).burst == expected


async def test_the_app_registers_it_inert(client):
    """The default settings leave the limit at 0, so the suite is never throttled."""
    from app.main import app as fastapi_app

    stack = [m.cls for m in fastapi_app.user_middleware]
    assert RateLimitMiddleware in stack
    assert (await client.get("/api/health")).status_code == 200
