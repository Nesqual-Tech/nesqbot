"""Liveness and readiness probes.

Both endpoints are intentionally unauthenticated: they back the container health
check and the compose/AKS probes, which have no bearer token.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.routers.deps import API_VERSION, optional_service
from app.schemas import HealthDeepOut, HealthOut

logger = logging.getLogger("nesqbot.health")

router = APIRouter(tags=["health"])

_PROBE_TIMEOUT_SECONDS = 2.0

#: The image tag actually running, stamped at build time (`--build-arg NESQ_BUILD`).
#: `API_VERSION` is the *contract* version and is bumped by hand, so it says
#: nothing about what is deployed - a user reading "API 0.2.0" in the app footer
#: reasonably concluded a v0.3.0 deploy had failed, when it had not. Two different
#: questions deserve two different fields.
BUILD = os.getenv("NESQ_BUILD", "").strip() or "unknown"


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    """Shallow probe - never touches a backing service."""
    return HealthOut(ok=True, service="nesqbot-api", version=API_VERSION, build=BUILD)


async def _check_db(db: AsyncSession) -> str:
    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return f"error: {type(exc).__name__}"
    return "ok"


async def _check_redis() -> str:
    settings = get_settings()
    if not settings.redis_url:
        return "unconfigured"
    try:
        from redis.asyncio import from_url  # imported lazily; redis is optional at runtime
    except ImportError:
        return "unavailable"
    client = None
    try:
        client = from_url(settings.redis_url)
        await asyncio.wait_for(client.ping(), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
    return "ok"


async def _check_temporal() -> str:
    temporal = optional_service("temporal_client")
    if temporal is None:
        return "unavailable"
    get_client = getattr(temporal, "get_client", None)
    if get_client is None:
        return "unavailable"
    try:
        client = await asyncio.wait_for(get_client(), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"
    return "ok" if client is not None else "unreachable"


@router.get("/health/deep", response_model=HealthDeepOut)
async def health_deep(response: Response, db: AsyncSession = Depends(get_db)) -> HealthDeepOut:
    """Readiness probe - db is required, redis and temporal are advisory."""
    db_status, redis_status, temporal_status = await asyncio.gather(
        _check_db(db),
        _check_redis(),
        _check_temporal(),
    )
    ok = db_status == "ok"
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("health/deep failing: db=%s", db_status)
    return HealthDeepOut(
        ok=ok,
        version=API_VERSION,
        checks={"db": db_status, "redis": redis_status, "temporal": temporal_status},
    )
