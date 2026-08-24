"""The vendor driver protocol and the one HTTP call path every driver shares.

Credential discipline (enforced by the AST audit in
`tests/services/test_vendors.py`): the resolved credential enters
`call_vendor`, is written into a request header dict that stays local to that
frame, and leaves by no other route. It is never returned, never logged, never
formatted into an error message. Anything derived from a vendor response —
including the text of a 4xx body and the string form of a transport error —
goes through `redact()` before it becomes part of an error envelope, because a
chatty vendor may echo the header we sent it and an `httpx` exception repr can
carry the request that produced it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

#: Matches `model_router.RETRY_ATTEMPTS` — one shared idea of "transient".
RETRY_ATTEMPTS = 3

#: What a redacted credential looks like in an error string.
REDACTED = "[redacted]"

#: Header used when a manifest asks for `api_key` auth without naming one.
DEFAULT_API_KEY_HEADER = "X-API-Key"

#: How much of a vendor error body is worth quoting back to the caller.
BODY_EXCERPT_CHARS = 200

#: Tests install an `httpx.MockTransport` here so the real paths can be
#: exercised without a network. Production leaves it `None`.
_default_transport: httpx.AsyncBaseTransport | None = None


def set_default_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Install (or clear) the transport every driver builds its client with."""
    global _default_transport
    _default_transport = transport


def get_default_transport() -> httpx.AsyncBaseTransport | None:
    return _default_transport


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VendorOutcome:
    """What a driver hands back to `connectors._invoke_vendor`.

    `result` is the *normalised* payload — the same shape the mock returns for
    that connector and action, so nothing downstream has to know which path
    ran. `live` says which one did.
    """

    ok: bool
    result: Any = None
    error: str | None = None
    status: int | None = None
    live: bool = True

    @classmethod
    def success(cls, result: Any, *, live: bool = True) -> VendorOutcome:
        return cls(ok=True, result=result, live=live)

    @classmethod
    def failure(cls, error: str, *, status: int | None = None) -> VendorOutcome:
        return cls(ok=False, error=error, status=status)


@dataclass(frozen=True)
class VendorResponse:
    """A successful vendor reply, already decoded and stripped of our request."""

    status: int
    data: Any
    headers: dict[str, str]


class VendorCallError(Exception):
    """A vendor call that failed. The message is safe to return and to log."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class VendorDriver(Protocol):
    """Given a resolved credential, an action and validated input, make the call."""

    name: str

    def configured(self, manifest: dict[str, Any], settings: Any) -> bool:
        """True when this connector has somewhere real to call."""
        ...

    def supports(self, action: str, manifest: dict[str, Any]) -> bool:
        """True when this driver knows how to perform `action`."""
        ...

    async def invoke(
        self,
        *,
        connector_id: str,
        action: str,
        input_data: dict[str, Any],
        credential: str,
        manifest: dict[str, Any],
        settings: Any,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> VendorOutcome:
        """Perform the call and normalise the reply."""
        ...


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact(text: str, credential: str | None) -> str:
    """Remove the credential from text that came back from a vendor.

    Vendors echo headers, proxies quote requests, and `httpx` error reprs can
    carry the request that produced them. This is the last gate before such a
    string becomes an error envelope or a log line.
    """
    cleaned = text or ""
    if not credential or len(credential) < 4:
        return cleaned
    cleaned = cleaned.replace(credential, REDACTED)
    # `Bearer <token>` survives a partial match if the vendor re-cased it.
    lowered = credential.lower()
    if lowered != credential and lowered in cleaned:
        cleaned = cleaned.replace(lowered, REDACTED)
    return cleaned


def _excerpt(text: str) -> str:
    return " ".join((text or "").split())[:BODY_EXCERPT_CHARS]


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class _RetryableStatus(Exception):
    """Internal: a 5xx worth another attempt, carrying the response it came from."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


def is_retryable(exc: BaseException) -> bool:
    """Transient failures only: connection-level errors and 5xx."""
    if isinstance(exc, _RetryableStatus):
        return True
    return isinstance(exc, (httpx.NetworkError, httpx.ConnectTimeout, httpx.PoolTimeout))


def _retrying() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(is_retryable),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


async def call_vendor(
    *,
    method: str,
    url: str,
    auth: str,
    credential: str | None,
    label: str,
    timeout_seconds: float,
    api_key_header: str = DEFAULT_API_KEY_HEADER,
    json_body: Any | None = None,
    params: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> VendorResponse:
    """Perform one authenticated vendor call.

    Raises `VendorCallError` — with a message that has been through
    `redact()` — on a transport failure or any status >= 400. Retries
    connection errors and 5xx `RETRY_ATTEMPTS` times.
    """
    # Built here and used here. This dict is the only place the credential
    # exists in this module, and it does not outlive the request.
    request_headers: dict[str, str] = {"Accept": "application/json"}
    if credential:
        if auth == "oauth2":
            request_headers["Authorization"] = f"Bearer {credential}"
        elif auth == "api_key":
            request_headers[api_key_header or DEFAULT_API_KEY_HEADER] = credential

    client_transport = transport if transport is not None else _default_transport

    async def _once() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout_seconds, transport=client_transport) as client:
            response = await client.request(
                method.upper(),
                url,
                headers=request_headers,
                json=json_body,
                params=params,
            )
        if 500 <= response.status_code < 600:
            raise _RetryableStatus(response)
        return response

    response: httpx.Response | None = None
    try:
        async for attempt in _retrying():
            with attempt:
                response = await _once()
    except _RetryableStatus as exc:
        response = exc.response
    except httpx.HTTPError as exc:
        # `from None`: the httpx exception holds the request — and therefore the
        # header we just built — so it must not ride along as __cause__ into
        # whatever logs this.
        message = redact(f"{type(exc).__name__}: {exc}", credential)
        raise VendorCallError(f"{label} could not reach the vendor ({_excerpt(message)})") from None

    assert response is not None  # `reraise=True` guarantees one of the two above
    if response.status_code >= 400:
        detail = redact(_excerpt(response.text), credential)
        suffix = f": {detail}" if detail else ""
        raise VendorCallError(
            f"{label} failed with HTTP {response.status_code}{suffix}",
            status=response.status_code,
        )

    return VendorResponse(
        status=response.status_code,
        data=_decode(response),
        headers={k.lower(): v for k, v in response.headers.items()},
    )
