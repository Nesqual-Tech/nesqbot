"""Authentication, current user, and push-device registration."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, status
from jose import JWTError, jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ALGORITHM,
    ROLE_ADMIN,
    create_access_token,
    decode_session_token,
    get_current_user,
    get_or_create_dev_user,
    require_admin,
    revoke_token,
    upsert_entra_user,
    verify_entra_access_token,
)
from app.config import get_settings
from app.db import get_db
from app.errors import AppError
from app.models import AuditEvent, User
from app.routers.deps import require_model
from app.schemas import DeviceRegisterIn, EntraLoginIn, OkOut, TokenOut, UpdateUserRoleIn, UserOut

logger = logging.getLogger("nesqbot.auth")

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/auth/dev-login", response_model=TokenOut)
async def dev_login(db: AsyncSession = Depends(get_db)) -> TokenOut:
    if get_settings().nesq_env == "production":
        raise AppError(403, "dev_login_disabled", "Dev login is disabled in production")
    user = await get_or_create_dev_user(db)
    token = create_access_token(str(user.id), user.email)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.post("/auth/entra", response_model=TokenOut)
async def entra_login(
    body: EntraLoginIn | None = None,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> TokenOut:
    """Exchange an Entra **access token** for a Nesq Bot session token.

    Why this endpoint still exists now that the clients hold a bearer token the
    API could accept directly: validating a remote token means an RS256 verify
    against cached JWKS on *every* request, and it leaves the session's lifetime
    entirely in Entra's hands. Exchanging once for a local HS256 JWT keeps the
    per-request check to a signature and a `users` row lookup, and makes a
    deleted user's session stop working immediately (see docs/security.md).

    The token is read from `Authorization: Bearer …`, which is where a bearer
    credential belongs. The documented `{id_token}` body is still accepted as the
    legacy spelling — the *name* is legacy, the validation is not: whatever
    arrives must pass as an access token audienced to this API and carrying
    `access_as_user`, so an actual ID token is rejected on its merits.
    """
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[len("bearer ") :].strip()
    if not presented and body is not None:
        presented = (body.id_token or "").strip()

    claims = await verify_entra_access_token(presented)
    user = await upsert_entra_user(db, claims)
    token = create_access_token(str(user.id), user.email)
    logger.info("entra login oid=%s user=%s", claims.get("oid"), user.id)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.post("/auth/logout", response_model=OkOut)
async def logout(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    """End this session token now, rather than waiting out its 14-day life.

    `get_current_user` already proved the token is valid and unrevoked; this
    decodes it a second time only to reach the claims (`jti`/`exp`) it does not
    return. A token with no `jti` (minted before this endpoint existed) or the
    worker's shared service token both decode-fail here and are reported as
    nothing to revoke rather than an error - logging out was never going to do
    anything to them anyway.
    """
    token = (authorization or "").removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
    except JWTError:
        return OkOut(ok=True, detail="nothing_to_revoke")
    revoked = await revoke_token(db, payload)
    return OkOut(ok=True, detail="revoked" if revoked else "nothing_to_revoke")


@router.post("/auth/refresh", response_model=TokenOut)
async def refresh(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TokenOut:
    """Trade a valid session token for a fresh one.

    The presented token is revoked in the same transaction the new one is
    minted in, so there is never a moment with two live tokens for one
    session — a client that refreshes on a schedule keeps exactly one, and a
    token copied out of a device dies fourteen days after it was minted no
    matter how often the real client refreshes.

    Only a token this API minted can be refreshed. The dev bypass and the
    worker's service token both authenticate `get_current_user` but carry no
    session to renew; they get 400 `not_refreshable` rather than a token that
    would silently change what identity the caller is using.
    """
    token = (authorization or "").removeprefix("Bearer ").strip()
    payload = decode_session_token(token) if token else None
    if payload is None or str(payload.get("sub") or "") != str(user.id):
        raise AppError(400, "not_refreshable", "Only a session token can be refreshed")
    fresh = create_access_token(str(user.id), user.email)
    await revoke_token(db, payload)
    return TokenOut(access_token=fresh, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


# ---------------------------------------------------------------------------
# Users (admin)
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[User]:
    """Everyone who has ever signed in, oldest first. Admin only."""
    result = await db.execute(select(User).order_by(User.created_at, User.email))
    return list(result.scalars().all())


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user_role(
    user_id: uuid.UUID,
    body: UpdateUserRoleIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    """Grant or remove the admin role. Admin only.

    An admin may not demote themself while they are the only admin: with
    enforcement switched on by the existence of an admin, that would lock
    every admin-only route to nobody. Anyone else may be demoted, including
    another admin — the last-admin rule is about the *count*, not the caller.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise AppError(404, "user_not_found", "User not found")
    if target.role == body.role:
        return target
    if target.role == ROLE_ADMIN and body.role != ROLE_ADMIN:
        remaining = await db.execute(
            select(func.count()).select_from(User).where(User.role == ROLE_ADMIN, User.id != target.id)
        )
        if int(remaining.scalar_one() or 0) == 0:
            raise AppError(409, "last_admin", "At least one admin must remain")
    previous = target.role
    target.role = body.role
    db.add(
        AuditEvent(
            actor_user_id=admin.id,
            event_type="user_role_changed",
            detail={"user_id": str(target.id), "from": previous, "to": body.role},
        )
    )
    await db.commit()
    await db.refresh(target)
    return target


@router.post("/me/devices", status_code=status.HTTP_201_CREATED)
async def register_device(
    body: DeviceRegisterIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Upsert a push token for the calling user, keyed on (user_id, token)."""
    UserDevice = require_model("UserDevice", "devices_unavailable")
    result = await db.execute(
        select(UserDevice).where(UserDevice.user_id == user.id, UserDevice.token == body.token)
    )
    device = result.scalar_one_or_none()
    if device is None:
        device = UserDevice(user_id=user.id, token=body.token, platform=body.platform)
        db.add(device)
    else:
        device.platform = body.platform
    await db.commit()
    await db.refresh(device)
    return {"ok": True, "device_id": str(device.id)}


@router.delete("/me/devices/{token:path}", response_model=OkOut)
async def unregister_device(
    token: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    UserDevice = require_model("UserDevice", "devices_unavailable")
    result = await db.execute(
        select(UserDevice).where(UserDevice.user_id == user.id, UserDevice.token == token)
    )
    device = result.scalar_one_or_none()
    if device is None:
        # Idempotent: unregistering an unknown token is not an error.
        return OkOut(ok=True, detail="not_registered")
    await db.delete(device)
    await db.commit()
    return OkOut(ok=True, detail="deleted")
