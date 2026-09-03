"""Async engine / session factory plus the pgvector type codec hook."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
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


async def release_transaction(session: AsyncSession) -> None:
    """Close any open transaction before the caller waits on something slow.

    Production incident, 2026-09-02 09:31 UTC:

        find work items      2.8s
        create work item    57.0s
        create work item      46ms
        sqlalchemy.exc.InterfaceError: (asyncpg.InterfaceError) connection is closed
        [SQL: INSERT INTO cost_ledger …]

    `nesqbot-pg` runs with `idle_in_transaction_session_timeout = 60000`.
    SQLAlchemy opens a transaction on the first statement and holds it until
    commit, so a turn's opening reads leave one open; a model call at the
    `reason` tier then thought for 57 seconds; Postgres terminated a backend
    that had been idle *in a transaction* for over a minute; and the next
    statement found a dead socket and took the whole turn with it.

    An idle connection is not the problem — an idle transaction is, because
    that is what has a timer on it. So the rule is: before any await that can
    outlast a minute on something that is not the database, stop being in a
    transaction. There are two such awaits in this codebase, a model call and a
    Bot Desktop step, and they live in different modules; this is the one
    implementation both of them call, because an invariant with two copies is
    an invariant with two behaviours.

    `pool_pre_ping` above is the other half and was already in place: it
    validates a connection on *checkout*, which does nothing for a connection
    that dies while a session is holding it.

    Never raises. It runs before the slow call, and a commit that cannot land
    is not a reason to abandon work that has not been attempted yet — the next
    statement will raise on its own, with the session in a state SQLAlchemy can
    recover by discarding the connection.
    """
    if not session.in_transaction():
        return
    try:
        await session.commit()
    except SQLAlchemyError:
        logger.warning("could not close the transaction before a slow call", exc_info=True)
        try:
            await session.rollback()
        except SQLAlchemyError:  # pragma: no cover - rollback on a dead socket
            logger.debug("rollback after a failed pre-call commit also failed")
