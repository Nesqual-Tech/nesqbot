"""Cross-cutting ASGI middleware: request correlation id + request timing log.

Implemented as raw ASGI (not ``BaseHTTPMiddleware``) so that SSE responses stream
without being buffered or losing their disconnect signal.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("nesqbot.request")

REQUEST_ID_HEADER = "x-request-id"
RESPONSE_TIME_HEADER = "x-response-time-ms"


def get_request_id(scope: Scope) -> str | None:
    state = scope.get("state") or {}
    return state.get("request_id")


class RequestContextMiddleware:
    """Attach an ``X-Request-Id`` to every request/response and log timings.

    * accepts an inbound ``X-Request-Id``, otherwise mints a uuid4
    * stashes it on ``request.state.request_id``
    * echoes it back on the response
    * emits one structured log line per request with the elapsed milliseconds
    """

    def __init__(self, app: ASGIApp, *, header_name: str = REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(self.header_name)
        request_id = (inbound or "").strip() or str(uuid.uuid4())

        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id

        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[self.header_name] = request_id
                headers[RESPONSE_TIME_HEADER] = f"{(time.perf_counter() - started) * 1000:.2f}"
            await send(message)

        method = scope.get("method", "-")
        path = scope.get("path", "-")
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The catch-all handler in errors.py logs the traceback; keep this
            # line traceback-free so failures are not logged twice.
            logger.warning(
                "request raised method=%s path=%s request_id=%s duration_ms=%.2f",
                method,
                path,
                request_id,
                (time.perf_counter() - started) * 1000,
            )
            raise
        else:
            duration_ms = (time.perf_counter() - started) * 1000
            log = logger.warning if status_code >= 500 else logger.info
            log(
                "%s %s -> %s in %.2fms request_id=%s",
                method,
                path,
                status_code,
                duration_ms,
                request_id,
            )


# Backwards-friendly alias.
RequestIdMiddleware = RequestContextMiddleware


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

RATE_LIMIT_HEADER = "x-ratelimit-limit"
RATE_REMAINING_HEADER = "x-ratelimit-remaining"


class RateLimitMiddleware:
    """A token bucket per caller, in process.

    Keyed on the bearer token when there is one — so two people behind one
    office NAT never share a bucket — and on the client address otherwise.
    Refills at ``per_minute / 60`` tokens a second up to ``burst``. Over the
    limit, the request is answered with 429 and the API's usual error
    envelope, plus ``Retry-After``; nothing downstream runs.

    In process, not in Redis, on purpose: this is the guard against a runaway
    client or a script in a loop, not a billing meter. With N replicas the
    effective ceiling is N times the configured one, which is the right
    direction to be wrong in for a control that exists to stop accidents.
    The per-bot daily budget is what bounds spend.

    Raw ASGI, like ``RequestContextMiddleware`` above, so SSE responses still
    stream: the check happens once at request start and the body is left
    alone.
    """

    def __init__(
        self, app: ASGIApp, *, per_minute: int, burst: int | None = None, max_keys: int = 10_000
    ) -> None:
        self.app = app
        self.per_minute = max(0, int(per_minute))
        self.burst = max(1, int(burst or self.per_minute or 1))
        self.max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, updated_at)

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0

    @staticmethod
    def _key(scope: Scope) -> str:
        headers = Headers(scope=scope)
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            # The token itself is a secret; the key only has to be stable.
            import hashlib

            return "t:" + hashlib.sha256(auth[7:].strip().encode()).hexdigest()[:32]
        client = scope.get("client")
        return "ip:" + (client[0] if client else "unknown")

    def _take(self, key: str, now: float) -> tuple[bool, float, int]:
        """(allowed, seconds_until_next_token, remaining)"""
        tokens, updated = self._buckets.get(key, (float(self.burst), now))
        rate = self.per_minute / 60.0
        tokens = min(float(self.burst), tokens + (now - updated) * rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0, int(tokens - 1.0)
        self._buckets[key] = (tokens, now)
        return False, (1.0 - tokens) / rate if rate > 0 else 60.0, 0

    def _evict(self, now: float) -> None:
        if len(self._buckets) <= self.max_keys:
            return
        # A bucket that has been idle long enough to be full again carries no
        # information; dropping it is free.
        full_after = self.burst / (self.per_minute / 60.0) if self.per_minute else 0
        for key in [k for k, (_, at) in self._buckets.items() if now - at > full_after]:
            self._buckets.pop(key, None)
        while len(self._buckets) > self.max_keys:
            self._buckets.pop(next(iter(self._buckets)))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        key = self._key(scope)
        allowed, wait, remaining = self._take(key, now)
        self._evict(now)
        if allowed:

            async def send_with_headers(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers[RATE_LIMIT_HEADER] = str(self.per_minute)
                    headers[RATE_REMAINING_HEADER] = str(remaining)
                await send(message)

            await self.app(scope, receive, send_with_headers)
            return

        import json
        import math

        retry = max(1, math.ceil(wait))
        body = json.dumps(
            {
                "detail": f"Too many requests; try again in {retry}s",
                "code": "rate_limited",
                "request_id": get_request_id(scope),
            }
        ).encode()
        logger.warning(
            "rate limited key=%s path=%s retry_after=%ss", key[:12], scope.get("path", "-"), retry
        )
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(retry).encode()),
                    (RATE_LIMIT_HEADER.encode(), str(self.per_minute).encode()),
                    (RATE_REMAINING_HEADER.encode(), b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
