"""Manifest-driven HTTP — the driver for connectors we did not write.

A connector registered through `POST /integrations/connectors` describes its
own calls in its manifest, so a custom connector can reach a real API without
any code landing in this repo:

    {
      "base_url": "https://invoices.internal/api",
      "auth": "api_key",
      "api_key_header": "X-Api-Key",
      "actions": [
        {
          "name": "list_unpaid",
          "method": "GET",
          "path": "/invoices",
          "query": {"older_than": "{older_than_days}"}
        },
        {
          "name": "draft_reminder",
          "method": "POST",
          "path": "/invoices/{invoice_id}/reminders",
          "body": {"note": "{note}", "silent": false}
        }
      ]
    }

Every one of those keys is optional. A manifest without `base_url` — which is
every manifest written before this driver existed — registers exactly as it
did and keeps mocking.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.services.vendors.base import (
    DEFAULT_API_KEY_HEADER,
    VendorCallError,
    VendorOutcome,
    call_vendor,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

VALID_AUTH = ("oauth2", "api_key", "none")


class ManifestError(Exception):
    """The manifest asked for something the input cannot satisfy."""


def base_url(manifest: dict[str, Any]) -> str:
    return str((manifest or {}).get("base_url") or "").strip().rstrip("/")


def action_http(manifest: dict[str, Any], action: str) -> dict[str, Any] | None:
    """The HTTP description of one action, or None when it has none."""
    for entry in (manifest or {}).get("actions") or []:
        if entry.get("name") != action:
            continue
        if not entry.get("path"):
            return None
        return entry
    return None


def _substitute_text(template: str, data: dict[str, Any], *, url_quote: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in data:
            raise ManifestError(f"template references input '{key}', which was not supplied")
        value = data[key]
        text = "" if value is None else str(value)
        return quote(text, safe="") if url_quote else text

    return _PLACEHOLDER.sub(replace, template)


def _substitute(node: Any, data: dict[str, Any]) -> Any:
    """Fill a body/query template.

    A string that is exactly one placeholder keeps the input's own type, so
    `{"fields": "{fields}"}` sends the object rather than its `repr`.
    """
    if isinstance(node, str):
        whole = _PLACEHOLDER.fullmatch(node)
        if whole:
            key = whole.group(1)
            if key not in data:
                raise ManifestError(f"template references input '{key}', which was not supplied")
            return data[key]
        return _substitute_text(node, data, url_quote=False)
    if isinstance(node, dict):
        return {key: _substitute(value, data) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(value, data) for value in node]
    return node


def _query(node: Any, data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(node, dict) or not node:
        return None
    filled = {key: _substitute(value, data) for key, value in node.items()}
    return {key: value for key, value in filled.items() if value is not None}


class GenericHttpDriver:
    """Performs whatever the manifest describes."""

    name = "generic_http"

    def configured(self, manifest: dict[str, Any], settings: Any) -> bool:
        return bool(base_url(manifest))

    def supports(self, action: str, manifest: dict[str, Any]) -> bool:
        return action_http(manifest, action) is not None

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
        spec = action_http(manifest, action)
        if spec is None:
            return VendorOutcome.failure(f"{connector_id} manifest describes no call for '{action}'")

        data = input_data or {}
        label = f"{connector_id} {action}"
        auth = str((manifest or {}).get("auth") or "none")
        if auth not in VALID_AUTH:
            auth = "none"

        try:
            path = _substitute_text(str(spec.get("path")), data, url_quote=True)
            json_body = _substitute(spec.get("body"), data) if spec.get("body") is not None else None
            params = _query(spec.get("query"), data)
        except ManifestError as exc:
            return VendorOutcome.failure(f"{label}: {exc}")

        if not path.startswith("/"):
            path = f"/{path}"

        try:
            response = await call_vendor(
                method=str(spec.get("method") or "GET"),
                url=f"{base_url(manifest)}{path}",
                auth=auth,
                credential=credential,
                label=label,
                timeout_seconds=float(getattr(settings, "request_timeout_seconds", 60.0)),
                api_key_header=str((manifest or {}).get("api_key_header") or DEFAULT_API_KEY_HEADER),
                json_body=json_body,
                params=params,
                transport=transport,
            )
        except VendorCallError as exc:
            return VendorOutcome.failure(str(exc), status=exc.status)

        # We do not know this vendor's vocabulary, so the decoded body is the
        # normalised result. Anything that is not JSON is wrapped so callers
        # always receive a container.
        if isinstance(response.data, (dict, list)):
            return VendorOutcome.success(response.data)
        return VendorOutcome.success({"status": response.status, "body": response.data})


driver = GenericHttpDriver()
