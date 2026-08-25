"""FastAPI application factory for the Nesq Bot control plane."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import prune_expired_revocations
from app.config import get_settings
from app.db import SessionLocal, engine
from app.errors import register_error_handlers
from app.middleware import RequestContextMiddleware
from app.routers import API_VERSION, OPENAPI_TAGS, router
from app.services.reaper import reap_orphaned_runs
from app.services.schema import ensure_schema
from app.services.seed import seed_system

logger = logging.getLogger("nesqbot.startup")

#: Used only in development when CORS_ORIGINS is empty. Never a wildcard:
#: pairing "*" with allow_credentials=True is rejected by browsers and signals
#: intent to trust every origin with cookies/bearer tokens.
DEV_CORS_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:8081",
    "http://127.0.0.1:8081",
    "http://localhost:19006",
    "http://127.0.0.1:19006",
]


def resolve_cors_origins(settings) -> list[str]:
    """Explicit origin list, or a hard failure in production.

    An empty CORS_ORIGINS used to fall back to ["*"] alongside
    allow_credentials=True. That combination is invalid per the CORS spec, so
    browsers reject it anyway - and it advertises the wrong intent. Development
    now falls back to the known local dev servers; production refuses to boot.
    """
    origins = settings.cors_origin_list
    if origins:
        if "*" in origins:
            raise RuntimeError(
                "CORS_ORIGINS may not contain \"*\": the API sends credentials, and "
                "browsers reject a wildcard origin on credentialed requests. "
                "List the exact origins instead."
            )
        return origins
    if settings.nesq_env == "production":
        raise RuntimeError(
            "CORS_ORIGINS is empty. Set it to the exact browser origins allowed to "
            "call this API (comma separated); the API will not serve a wildcard "
            "origin with credentials enabled."
        )
    logger.warning(
        "CORS_ORIGINS is empty; falling back to the local dev origins %s", DEV_CORS_ORIGINS
    )
    return list(DEV_CORS_ORIGINS)


DESCRIPTION = """
Control plane for Nesq Bot: bots, threads, approvals, connectors, MCP servers,
Bot Desktop, routines, memory/KB, usage and evals.

All routes are mounted under `/api`. Authenticate with `Authorization: Bearer <jwt>`,
or `X-Nesq-Dev: 1` while `NESQ_ENV=development`.

Handled errors return `{"detail": "...", "code": "snake_case_code"}`; unhandled ones
return a 500 carrying the `X-Request-Id` correlation id.
""".strip()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    try:
        await ensure_schema(engine)
        async with SessionLocal() as db:
            await seed_system(db)
            # Reclaim runs whose process went away — most often this very deploy.
            # Age-based and idempotent, so it is safe with several replicas; see
            # services/reaper.py for why it never reaps a parked run.
            reaped = await reap_orphaned_runs(db)
            pruned = await prune_expired_revocations(db)
        logger.info(
            "schema ensured, system bots seeded, %d orphaned run(s) reclaimed, "
            "%d expired revocation(s) pruned",
            len(reaped),
            pruned,
        )
    except Exception:  # noqa: BLE001 - logged, and fatal in production
        logger.exception("startup failed: schema migration or seeding did not complete")
        if settings.nesq_env == "production":
            # Fail loudly so the container never passes its health check with a
            # half-initialised database.
            raise
        logger.warning(
            "continuing without a verified schema because NESQ_ENV=%s", settings.nesq_env
        )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Nesq Bot API",
        description=DESCRIPTION,
        version=API_VERSION,
        openapi_tags=OPENAPI_TAGS,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        # FastAPI defaults this to an unprefixed /docs/oauth2-redirect, which
        # breaks a reverse proxy that only forwards /api.
        swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
        lifespan=lifespan,
    )

    # CORS first so the request-id middleware ends up outermost and can stamp
    # every response, including preflights and error envelopes.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolve_cors_origins(settings),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-Response-Time-Ms"],
    )
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)
    app.include_router(router, prefix="/api")
    return app


app = create_app()
