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
