"""Error envelope for the Nesq Bot API.

Every handled error is rendered as ``{"detail": "...", "code": "snake_case_code"}``.
Unhandled errors become a 500 ``{"detail": "internal_error", "code": "internal_error",
"request_id": "..."}`` and are logged with the id issued by the X-Request-Id middleware.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("nesqbot.errors")

#: Fallback ``code`` per status when an HTTPException carries no explicit code.
STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    501: "not_implemented",
    502: "upstream_error",
    503: "service_unavailable",
    504: "upstream_timeout",
}


class AppError(Exception):
    """Application level error carrying an HTTP status and a stable machine code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.detail = detail or code.replace("_", " ")
        self.extra = extra or {}
        self.headers = headers
        super().__init__(self.detail)

    @property
    def status(self) -> int:
        """Alias so call sites can read ``err.status`` as in the contract."""
        return self.status_code

    def body(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"detail": self.detail, "code": self.code}
        payload.update(self.extra)
        return payload


def not_found(code: str = "not_found", detail: str = "Not found") -> AppError:
    return AppError(status.HTTP_404_NOT_FOUND, code, detail)


def forbidden(code: str = "forbidden", detail: str = "Forbidden") -> AppError:
    return AppError(status.HTTP_403_FORBIDDEN, code, detail)


def conflict(code: str = "conflict", detail: str = "Conflict") -> AppError:
    return AppError(status.HTTP_409_CONFLICT, code, detail)


def bad_request(code: str = "bad_request", detail: str = "Bad request") -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, code, detail)


def service_unavailable(code: str = "service_unavailable", detail: str = "Service unavailable") -> AppError:
    return AppError(status.HTTP_503_SERVICE_UNAVAILABLE, code, detail)


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _json(
    status_code: int,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload), headers=headers)


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    logger.info(
        "app_error status=%s code=%s request_id=%s detail=%s",
        exc.status_code,
        exc.code,
        request_id_of(request),
        exc.detail,
    )
    return _json(exc.status_code, exc.body(), exc.headers)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    detail: Any = exc.detail
    code = STATUS_CODES.get(exc.status_code, "http_error")
    extra: dict[str, Any] = {}

    if isinstance(detail, dict):
        extra = dict(detail)
        code = str(extra.pop("code", code))
        detail = extra.pop("detail", code.replace("_", " "))
    elif detail is None:
        detail = code.replace("_", " ")

    body: dict[str, Any] = {"detail": detail, "code": code}
    body.update(extra)
    return _json(exc.status_code, body, getattr(exc, "headers", None))


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return _json(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        {
            "detail": "Request validation failed",
            "code": "validation_error",
            "errors": jsonable_encoder(errors),
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = request_id_of(request)
    logger.exception(
        "unhandled_error request_id=%s method=%s path=%s",
        rid,
        request.method,
        request.url.path,
    )
    # Never leak the traceback to the caller — only the correlation id.
    # This handler runs inside ServerErrorMiddleware, which sits outside the
    # request-id middleware, so the header has to be stamped here as well.
    return _json(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        {"detail": "internal_error", "code": "internal_error", "request_id": rid},
        {"X-Request-Id": rid} if rid else None,
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
