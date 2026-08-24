"""Auth — dev bypass, local JWTs, and Entra ID **access token** validation.

The clients run auth code + PKCE and obtain an access token audienced to the API
(`api://<api app id>/access_as_user`). They present it once to `POST /auth/entra`,
which validates it here and mints a local session JWT. ID tokens are not accepted:
an ID token is audienced to the *client* and says who signed in — it is not
authorization to call this API. See docs/entra-setup.md.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import Depends, Header, HTTPException
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import User

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
DEV_USER_EMAIL = "dev@nesqualtech.com"
JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"

#: The single body every Entra rejection returns. Which check failed is an
#: operator detail, not a hint to hand back to whoever presented the token.
UNAUTHORIZED_DETAIL = "Invalid or expired token"

# tenant -> (fetched_at_monotonic, jwks document)
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_lock = asyncio.Lock()


def create_access_token(user_id: str, email: str) -> str:
    settings = get_settings()
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=14),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


async def get_or_create_dev_user(db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.email == DEV_USER_EMAIL))
    user = result.scalar_one_or_none()
    if user:
        return user
    user = User(email=DEV_USER_EMAIL, display_name="Nesqual Dev")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


#: Identity the Temporal worker acts as. A real row, so anything it does carries
#: an actor in the audit trail rather than appearing to come from nobody - and
#: so approval scoping keeps working: the worker resolves as `created_by` on
#: approvals it files, which grants read (it must poll for a decision) but never
#: the right to decide. It must never own bots or threads.
SERVICE_USER_EMAIL = "worker@nesqbot.service"


async def get_or_create_service_user(db: AsyncSession) -> User:
    """The worker's identity. Created on first authenticated call, like the dev user."""
    result = await db.execute(select(User).where(User.email == SERVICE_USER_EMAIL))
    user = result.scalar_one_or_none()
    if user:
        return user
    user = User(email=SERVICE_USER_EMAIL, display_name="Nesq Bot Worker")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# --------------------------------------------------------------------- Entra


def expected_issuer(tenant_id: str) -> str:
    """The one issuer this API trusts.

    The registration sets ``requestedAccessTokenVersion: 2``, so every token the
    API can legitimately receive carries the v2 issuer. The legacy
    ``https://sts.windows.net/{tenant}/`` form is deliberately *not* accepted:
    widening the issuer set to cover a token shape the resource server cannot be
    issued only creates a second thing to get wrong.
    """
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


def accepted_audiences(api_app_id: str) -> set[str]:
    """The `aud` values that mean "this API".

    v2 access tokens carry the bare app-id GUID. The App ID URI form is accepted
    too so that `AZURE_CLIENT_ID` written either way resolves to the same single
    resource identity — this is a spelling tolerance for one app, not a widening
    of who may call.
    """
    bare = api_app_id.removeprefix("api://").strip("/")
    return {bare, f"api://{bare}"}


def token_scopes(claims: dict[str, Any]) -> set[str]:
    """Delegated scopes from `scp`.

    Entra emits `scp` as a space-delimited string; a list is tolerated because
    some tooling normalises it that way. `roles` (application permissions) is
    deliberately ignored — this API is only ever called on behalf of a user.
    """
    raw = claims.get("scp")
    if isinstance(raw, str):
        return {part for part in raw.split() if part}
    if isinstance(raw, (list, tuple)):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set()


async def _fetch_jwks(tenant_id: str, *, force: bool = False) -> dict[str, Any]:
    """Tenant JWKS, cached for `entra_jwks_cache_seconds`."""
    settings = get_settings()
    now = time.monotonic()
    cached = _jwks_cache.get(tenant_id)
    if cached and not force and (now - cached[0]) < settings.entra_jwks_cache_seconds:
        return cached[1]

    async with _jwks_lock:
        cached = _jwks_cache.get(tenant_id)
        if cached and not force and (time.monotonic() - cached[0]) < settings.entra_jwks_cache_seconds:
            return cached[1]
        url = JWKS_URL_TEMPLATE.format(tenant=tenant_id)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                document = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("JWKS fetch failed for tenant %s: %s", tenant_id, exc)
            if cached:
                return cached[1]  # stale keys beat no keys
            raise HTTPException(status_code=503, detail="Identity keys unavailable") from exc
        _jwks_cache[tenant_id] = (time.monotonic(), document)
        return document


def _select_key(jwks: dict[str, Any], kid: str | None) -> dict[str, Any] | None:
    for key in jwks.get("keys", []):
        if not kid or key.get("kid") == kid:
            return key
    return None


def reset_jwks_cache() -> None:
    _jwks_cache.clear()


def _rejected(reason: str) -> HTTPException:
    """A 401 that tells the caller nothing about *which* check failed.

    Every rejection returns the same body. The specific reason is logged at debug
    level for operators; handing it to the caller would let an attacker binary-search
    a forged token towards acceptance one check at a time.
    """
    logger.debug("entra token rejected: %s", reason)
    return HTTPException(status_code=401, detail=UNAUTHORIZED_DETAIL)


async def verify_entra_access_token(access_token: str) -> dict:
    """Validate a v2 Entra **access token** for this API and return its claims.

    Every one of these must hold, or the token is rejected with an
    indistinguishable 401:

    * RS256 (or whatever `ENTRA_ALLOWED_ALGORITHMS` permits) signature over the
      tenant JWKS, with a forced refetch on an unknown `kid` so key rollover is
      not an outage;
    * `aud` is this API's app id — not the client's;
    * `iss` is exactly the v2 issuer for the configured tenant;
    * `exp`/`nbf` hold, allowing `ENTRA_CLOCK_SKEW_SECONDS` of drift;
    * `scp` contains `AZURE_API_SCOPE` (`access_as_user`);
    * there is an `oid` (or, failing that, a `sub`) to key the local user on.
    """
    settings = get_settings()
    tenant_id = (settings.azure_tenant_id or "").strip()
    api_app_id = (settings.azure_client_id or "").strip()
    required_scope = (settings.azure_api_scope or "").strip()
    if not tenant_id or not api_app_id:
        raise HTTPException(status_code=503, detail="Entra sign-in is not configured")
    if not required_scope:
        # Refusing to run without a required scope is the fail-closed choice: an
        # empty setting must not degrade into "any scope will do".
        raise HTTPException(status_code=503, detail="Entra sign-in is not configured")
    if not access_token or not access_token.strip():
        raise _rejected("empty token")

    try:
        header = jwt.get_unverified_header(access_token)
    except JWTError as exc:
        raise _rejected(f"malformed header: {exc}") from exc

    if header.get("alg") not in settings.entra_algorithm_list:
        # `none` and HS256-with-the-public-key are the classic confusions; refuse
        # before a key is even selected.
        raise _rejected(f"algorithm {header.get('alg')!r} is not allowed")

    kid = header.get("kid")
    jwks = await _fetch_jwks(tenant_id)
    key = _select_key(jwks, kid)
    if key is None:
        # Signing keys roll; refetch once before giving up.
        jwks = await _fetch_jwks(tenant_id, force=True)
        key = _select_key(jwks, kid)
    if key is None:
        raise _rejected(f"no JWKS key for kid {kid!r}")

    try:
        claims = jwt.decode(
            access_token,
            key,
            algorithms=settings.entra_algorithm_list,
            options={
                # `aud` and `iss` are checked below against explicit sets so the
                # comparison is visible here rather than buried in the library.
                "verify_aud": False,
                "verify_iss": False,
                "verify_at_hash": False,
                "require_exp": True,
                "leeway": settings.entra_clock_skew_seconds,
            },
        )
    except JWTError as exc:
        raise _rejected(f"signature/expiry: {exc}") from exc

    audience = claims.get("aud")
    audiences = {audience} if isinstance(audience, str) else set(audience or [])
    if not audiences & accepted_audiences(api_app_id):
        raise _rejected(f"audience {audience!r} is not this API")

    if str(claims.get("iss") or "") != expected_issuer(tenant_id):
        raise _rejected(f"issuer {claims.get('iss')!r} is not the tenant v2 issuer")

    tid = str(claims.get("tid") or "").strip()
    if tid and tid != tenant_id:
        raise _rejected(f"tenant {tid!r} is not {tenant_id!r}")

    if required_scope not in token_scopes(claims):
        # An ID token lands here even if it somehow passed `aud`: it carries no
        # `scp` at all, because it is not authorization to call anything.
        raise _rejected(f"scp does not contain {required_scope!r}")

    if not claims.get("oid") and not claims.get("sub"):
        raise _rejected("no oid/sub claim")

    return dict(claims)


async def _email_is_free(db: AsyncSession, email: str, *, exclude_id: Any) -> bool:
    stmt = select(User.id).where(User.email == email)
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return (await db.execute(stmt)).first() is None


async def upsert_entra_user(db: AsyncSession, claims: dict) -> User:
    """Find or create the local user behind a validated Entra token.

    Keyed on `oid`, the immutable per-tenant object id. `preferred_username`,
    `email` and `upn` all change when someone marries, changes team or gets a
    vanity address; keying on any of them would silently hand one person's data
    to the next holder of their address.
    """
    oid = str(claims.get("oid") or claims.get("sub") or "").strip()
    email = str(
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
        or ""
    ).strip().lower()
    display_name = str(claims.get("name") or email or oid or "Nesq User").strip()

    if not oid:
        raise _rejected("no oid claim at upsert")
    if not email:
        email = f"{oid}@entra.local"

    result = await db.execute(select(User).where(User.entra_oid == oid))
    user = result.scalar_one_or_none()

    if user is None:
        # First sign-in: adopt a pre-existing local account that used this
        # address. Only ever an account not already bound to an Entra identity —
        # re-pointing a row whose `entra_oid` is someone else's would turn a
        # recycled email address into an account takeover.
        by_email = await db.execute(
            select(User).where(User.email == email, User.entra_oid.is_(None))
        )
        user = by_email.scalar_one_or_none()

    # `users.email` is unique. Someone else already holding the address means the
    # directory recycled it, or two identities share a mail nickname; either way
    # the address is not ours to take, and a unique-violation 500 is a worse
    # answer than a synthetic one. `oid` is what identifies the account anyway.
    if user is None:
        if not await _email_is_free(db, email, exclude_id=None):
            email = f"{oid}@entra.local"
        user = User(email=email, display_name=display_name, entra_oid=oid)
        db.add(user)
    else:
        user.entra_oid = oid
        if await _email_is_free(db, email, exclude_id=user.id):
            user.email = email
        user.display_name = display_name or user.display_name

    await db.commit()
    await db.refresh(user)
    return user


# --------------------------------------------------------------- dependency


async def get_current_user(
    authorization: str | None = Header(default=None),
    x_nesq_dev: str | None = Header(default=None, alias="X-Nesq-Dev"),
    db: AsyncSession = Depends(get_db),
) -> User:
    settings = get_settings()
    # The dev bypass exists only in development — never reachable elsewhere.
    if settings.is_development and (not authorization or x_nesq_dev == "1"):
        return await get_or_create_dev_user(db)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.removeprefix("Bearer ").strip()

    # The Temporal worker authenticates with a shared service token, not a user
    # JWT. Without this branch every worker -> API call 401s, which is exactly
    # what happened: `routines.fetch.failed http=401` -> `schedule.reconcile
    # .skipped`, so no cron routine could ever fire. The worker had been sending
    # this header all along and the API had no idea what it was.
    service_token = (settings.worker_api_token or "").strip()
    if service_token and secrets.compare_digest(token, service_token):
        return await get_or_create_service_user(db)

    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
        user_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user
