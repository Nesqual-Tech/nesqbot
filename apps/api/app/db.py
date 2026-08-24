"""Async engine / session factory plus the pgvector type codec hook."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _register_pgvector(dbapi_connection, _record) -> None:
    """Teach asyncpg the `vector` type when pgvector is installed (best effort)."""
    try:
        from pgvector.asyncpg import register_vector
    except Exception:  # noqa: BLE001 - pgvector/numpy optional at runtime
        return
    try:
        run_async = getattr(dbapi_connection, "run_async", None)
        if run_async is not None:
            run_async(register_vector)
    except Exception:  # noqa: BLE001 - never block a connection over a codec
        logger.debug("pgvector codec registration skipped", exc_info=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
