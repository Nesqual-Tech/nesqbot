"""Model-provider API keys typed into the app, not set in the environment.

`model_router.py` resolves every provider's key from `Settings` first — an
operator's `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` /
`AZURE_OPENAI_*`. This module is the fallback for the self-hoster who never
touches a `.env` file: a credential saved from the app, additive only, never
overriding a value the operator already set. See `get_override`.

Storage: encrypted in Postgres (`provider_credentials`), not Key Vault — this
has to work identically on Azure and on a laptop running `docker compose up`,
and only Azure deployments have a vault. The Fernet key is derived from
`JWT_SECRET`, which every deployment already has and rotates deliberately;
rotating it invalidates every row here, which is the accepted trade for not
inventing a second secret nobody self-hosting this will ever set.

Resolved values never enter a log line, an API response, or an audit event —
same rule `secrets.py` follows for Key Vault-backed ones. Callers of
`get_override` get the plaintext in-process only.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
import uuid
from typing import Literal, TypedDict

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ProviderCredential

logger = logging.getLogger(__name__)

#: Duplicated from `model_router.Provider` rather than imported — that module
#: imports this one (for `get_override`), so importing it back would be a
#: cycle. Four literal strings kept in sync by hand beats restructuring either
#: module around it.
Provider = Literal["azure", "openai", "anthropic", "google"]
KNOWN_PROVIDERS: tuple[Provider, ...] = ("azure", "openai", "anthropic", "google")

#: How long an in-memory replica trusts its own snapshot of the table before
#: reloading. A write updates the writing replica's copy immediately (see
#: `set_credential`/`delete_credential`); this is only what makes *other*
#: replicas converge without a restart. Same value and same reasoning as
#: `secrets.py`'s `CACHE_TTL_SECONDS`.
RELOAD_INTERVAL_SECONDS = 300


class _Override(TypedDict):
    api_key: str
    base_url: str | None


_overrides: dict[str, _Override] = {}
_loaded_at: float | None = None
_fernet_instance: Fernet | None = None


def _fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    secret = get_settings().jwt_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    _fernet_instance = Fernet(key)
    return _fernet_instance


def reset_cache() -> None:
    """Drop the in-memory overrides and derived Fernet key (tests, rotation)."""
    global _loaded_at, _fernet_instance
    _overrides.clear()
    _loaded_at = None
    _fernet_instance = None


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        # A JWT_SECRET rotation is the expected cause: every row encrypted
        # under the old key stops decrypting. Treat it as "no credential"
        # rather than raising — the env-var fallback in model_router.py is
        # exactly the safety net this is supposed to fall back to.
        logger.warning("a stored provider credential did not decrypt (%s) — treating as absent", exc)
        return None


async def _reload(db: AsyncSession) -> None:
    result = await db.execute(select(ProviderCredential))
    fresh: dict[str, _Override] = {}
    for row in result.scalars().all():
        value = decrypt(row.api_key_encrypted)
        if value is None:
            continue
        fresh[row.provider] = {"api_key": value, "base_url": row.base_url}
    _overrides.clear()
    _overrides.update(fresh)
    global _loaded_at
    _loaded_at = time.monotonic()


async def load_overrides_from_db(db: AsyncSession) -> None:
    """Populate the in-memory table once, at boot — see `main.py`."""
    await _reload(db)


def get_override(provider: str) -> _Override | None:
    """The in-memory snapshot for `provider`, or None.

    Synchronous and DB-free on purpose: `model_router.py`'s per-request
    config resolution (`_openai_config_for` and friends) is itself
    synchronous, and threading a DB session through it to check one more
    fallback would touch every call site that builds a `ModelRouter` client.
    Staleness is bounded by `RELOAD_INTERVAL_SECONDS` on other replicas and by
    nothing at all on the replica that made the write.
    """
    return _overrides.get(provider)


async def maybe_reload(db: AsyncSession) -> None:
    """Refresh the in-memory table if `RELOAD_INTERVAL_SECONDS` has passed.

    Call this from a request path that is about to read `get_override` and
    can afford one extra query sometimes — `GET /bots/providers` does. Not
    required for correctness (writes update the local copy synchronously);
    it only shortens how long a *different* replica can lag behind one that
    just got a new key.
    """
    if _loaded_at is not None and (time.monotonic() - _loaded_at) < RELOAD_INTERVAL_SECONDS:
        return
    await _reload(db)


async def list_credentials(db: AsyncSession) -> list[ProviderCredential]:
    result = await db.execute(select(ProviderCredential).order_by(ProviderCredential.provider))
    return list(result.scalars().all())


async def set_credential(
    db: AsyncSession,
    *,
    provider: str,
    api_key: str,
    base_url: str | None,
    user_id: uuid.UUID | None,
) -> ProviderCredential:
    encrypted = encrypt(api_key)
    stmt = insert(ProviderCredential).values(
        provider=provider,
        api_key_encrypted=encrypted,
        base_url=base_url,
        updated_by_user_id=user_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProviderCredential.provider],
        set_={
            "api_key_encrypted": encrypted,
            "base_url": base_url,
            "updated_by_user_id": user_id,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)
    await db.commit()
    _overrides[provider] = {"api_key": api_key, "base_url": base_url}
    result = await db.execute(select(ProviderCredential).where(ProviderCredential.provider == provider))
    return result.scalar_one()


async def delete_credential(db: AsyncSession, *, provider: str) -> None:
    await db.execute(delete(ProviderCredential).where(ProviderCredential.provider == provider))
    await db.commit()
    _overrides.pop(provider, None)
