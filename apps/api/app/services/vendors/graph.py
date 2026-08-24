"""Microsoft Graph — the one first-party connector with a real public API.

Three actions, mapped onto the exact shapes `connectors._mock_result` returns
for them, because that mock shape is the normalised contract: the orchestrator,
the approval card and the clients read the same keys whichever path ran.

    list_inbox   GET  /me/messages?$top=
    draft_reply  POST /me/messages/{id}/createReply
    send_mail    POST /me/sendMail

Auth is an OAuth2 bearer token taken from the resolved secret. Graph stays on
the mock path until `GRAPH_API_BASE_URL` is set — an unset base URL is how a
deployment says "not wired to a tenant yet".
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.services.vendors.base import (
    VendorCallError,
    VendorOutcome,
    call_vendor,
)

logger = logging.getLogger(__name__)

ACTIONS = ("list_inbox", "draft_reply", "send_mail")

#: Graph caps `$top` at 1000; the inbox listing is a preview, not a sync.
MAX_TOP = 50
DEFAULT_TOP = 10

_MESSAGE_FIELDS = "id,subject,bodyPreview,from,receivedDateTime"


def _base_url(manifest: dict[str, Any], settings: Any) -> str:
    """Manifest override first, then `GRAPH_API_BASE_URL`. Empty means mock."""
    configured = (manifest or {}).get("base_url") or getattr(settings, "graph_api_base_url", "")
    return str(configured or "").strip().rstrip("/")


def _top(input_data: dict[str, Any]) -> int:
    raw = (input_data or {}).get("top")
    if raw is None:
        return DEFAULT_TOP
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TOP
    return max(1, min(value, MAX_TOP))


def _normalise_message(item: dict[str, Any]) -> dict[str, Any]:
    """Graph message resource -> the `list_inbox` mock's shape."""
    sender = ((item.get("from") or {}).get("emailAddress")) or {}
    return {
        "id": item.get("id"),
        "from": sender.get("address") or sender.get("name") or "",
        "subject": item.get("subject") or "",
        "snippet": item.get("bodyPreview") or "",
    }


class GraphDriver:
    """Microsoft Graph mail actions."""

    name = "microsoft_graph"

    def configured(self, manifest: dict[str, Any], settings: Any) -> bool:
        return bool(_base_url(manifest, settings))

    def supports(self, action: str, manifest: dict[str, Any]) -> bool:
        return action in ACTIONS

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
        base = _base_url(manifest, settings)
        data = input_data or {}
        label = f"microsoft_graph {action}"
        timeout_seconds = float(getattr(settings, "request_timeout_seconds", 60.0))

        try:
            if action == "list_inbox":
                response = await call_vendor(
                    method="GET",
                    url=f"{base}/me/messages",
                    auth="oauth2",
                    credential=credential,
                    label=label,
                    timeout_seconds=timeout_seconds,
                    params={"$top": _top(data), "$select": _MESSAGE_FIELDS},
                    transport=transport,
                )
                items = (response.data or {}).get("value") or []
                return VendorOutcome.success([_normalise_message(m) for m in items])

            if action == "draft_reply":
                message_id = str(data.get("message_id") or "")
                body = data.get("body") or ""
                response = await call_vendor(
                    method="POST",
                    url=f"{base}/me/messages/{quote(message_id, safe='')}/createReply",
                    auth="oauth2",
                    credential=credential,
                    label=label,
                    timeout_seconds=timeout_seconds,
                    json_body={"comment": body},
                    transport=transport,
                )
                draft = response.data if isinstance(response.data, dict) else {}
                return VendorOutcome.success(
                    {
                        "draft_id": draft.get("id") or f"draft_{message_id or 'unknown'}",
                        "message_id": message_id,
                        "body": body,
                        "state": "draft",
                        "sent": False,
                    }
                )

            if action == "send_mail":
                to = data.get("to")
                subject = data.get("subject")
                response = await call_vendor(
                    method="POST",
                    url=f"{base}/me/sendMail",
                    auth="oauth2",
                    credential=credential,
                    label=label,
                    timeout_seconds=timeout_seconds,
                    json_body={
                        "message": {
                            "subject": subject,
                            "body": {"contentType": "Text", "content": data.get("body") or ""},
                            "toRecipients": [
                                {"emailAddress": {"address": address}}
                                for address in _recipients(to)
                            ],
                        },
                        "saveToSentItems": True,
                    },
                    transport=transport,
                )
                # `sendMail` answers 202 with no body; Graph's own request id is
                # the only handle it gives us.
                return VendorOutcome.success(
                    {
                        "message_id": response.headers.get("request-id") or f"sent_{uuid4().hex[:8]}",
                        "to": to,
                        "subject": subject,
                        "state": "sent",
                        "sent": True,
                    }
                )
        except VendorCallError as exc:
            return VendorOutcome.failure(str(exc), status=exc.status)

        return VendorOutcome.failure(f"microsoft_graph has no live implementation for '{action}'")


def _recipients(to: Any) -> list[str]:
    """`to` is documented as a string; accept a list without complaining."""
    if isinstance(to, (list, tuple)):
        return [str(address) for address in to if str(address).strip()]
    text = str(to or "")
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


driver = GraphDriver()
