"""Secret resolution — Key Vault refs, with an env fallback for local dev.

Resolved values never enter a log line, an API response, or an audit event.
Callers get the plaintext in-process only; everything that leaves this module
by another route carries the *reference*, never the value.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import BotConnector

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300
ENV_SCHEME = "env://"
KV_SCHEME = "kv://"

# ref -> (fetched_at_monotonic, value). Values are secrets: never log this map.
_cache: dict[str, tuple[float, str]] = {}
_cache_lock: asyncio.Lock | None = None
_clients: dict[str, Any] = {}
_credential: Any | None = None


def _lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def reset_cache() -> None:
    """Drop cached secrets and clients (used on rotation and by tests)."""
    _cache.clear()
    _clients.clear()
    global _credential
    _credential = None


def _vault_url(vault_name: str) -> str:
    return f"https://{vault_name}.vault.azure.net"


def parse_ref(ref: str) -> tuple[str, str, str] | None:
    """Split a secret ref into `(kind, location, name)`.

    Accepted forms:
      * `kv://{vault-name}/{secret-name}`  -> ("kv", vault_url, secret_name)
      * `env://VAR_NAME`                   -> ("env", "", "VAR_NAME")
      * `secret-name`                      -> ("kv", AZURE_KEY_VAULT_URL, name)
    """
    value = (ref or "").strip()
    if not value:
        return None

    if value.lower().startswith(ENV_SCHEME):
        name = value[len(ENV_SCHEME) :].strip()
        return ("env", "", name) if name else None

    if value.lower().startswith(KV_SCHEME):
        remainder = value[len(KV_SCHEME) :].strip("/")
        if "/" not in remainder:
            return None
        vault, _, name = remainder.partition("/")
        vault, name = vault.strip(), name.strip()
        if not vault or not name:
            return None
        # Accept a bare vault name or a full hostname.
        location = vault if vault.startswith("http") else _vault_url(vault.split(".")[0])
        return ("kv", location, name)

    # Bare name against the configured default vault.
    default_vault = (get_settings().azure_key_vault_url or "").strip().rstrip("/")
    if not default_vault:
        return None
    if "/" in value or urlparse(value).scheme:
        return None
    return ("kv", default_vault, value)


def _get_credential() -> Any | None:
    """Lazily build DefaultAzureCredential; None when azure-identity is absent."""
    global _credential
    if _credential is not None:
        return _credential
    try:
        from azure.identity.aio import DefaultAzureCredential

        _credential = DefaultAzureCredential()
    except Exception as exc:  # noqa: BLE001 - no azure libs / no managed identity
        logger.info("azure credential unavailable (%s) — secret refs will not resolve", exc)
        return None
    return _credential


def _get_client(vault_url: str) -> Any | None:
    client = _clients.get(vault_url)
    if client is not None:
        return client
    credential = _get_credential()
    if credential is None:
        return None
    try:
        from azure.keyvault.secrets.aio import SecretClient

        client = SecretClient(vault_url=vault_url, credential=credential)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not build Key Vault client for %s: %s", vault_url, exc)
        return None
    _clients[vault_url] = client
    return client


async def resolve_secret(ref: str) -> str | None:
    """Resolve a secret reference to its value, or None when unresolvable.

    Never raises and never logs the value — only the reference.
    """
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    kind, location, name = parsed
    cache_key = f"{kind}:{location}:{name}"

    cached = _cache.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    async with _lock():
        cached = _cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

        value: str | None = None
        if kind == "env":
            value = os.getenv(name) or None
        else:
            client = _get_client(location)
            if client is None:
                return None
            try:
                secret = await client.get_secret(name)
                value = secret.value
            except Exception as exc:  # noqa: BLE001 - missing secret / no access
                logger.warning("secret ref %r did not resolve: %s", _redact_ref(ref), exc)
                return None

        if value is None:
            logger.info("secret ref %r resolved to nothing", _redact_ref(ref))
            return None
        _cache[cache_key] = (time.monotonic(), value)
        return value


def _redact_ref(ref: str) -> str:
    """Refs are not secrets, but keep log lines short and predictable."""
    return (ref or "")[:120]


async def resolve_connector_secrets(
    db: AsyncSession,
    bot_id,
    connector_id: str,
) -> dict[str, str]:
    """Resolve the secrets bound to one bot/connector pair.

    Returns `{}` when nothing is bound or nothing resolves, so callers can
    degrade to the mock path. The returned dict is for in-process use only —
    it must never be attached to a response, audit event, or log line.
    """
    result = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id,
            BotConnector.connector_id == connector_id,
        )
    )
    link = result.scalar_one_or_none()
    if link is None or not link.secret_ref:
        return {}

    value = await resolve_secret(link.secret_ref)
    if value is None:
        return {}
    return {"secret": value}
