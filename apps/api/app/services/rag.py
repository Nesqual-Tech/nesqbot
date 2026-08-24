"""Retrieval — pgvector cosine search with a keyword fallback for keyless dev."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CostLedger, KbArticle, Memory
from app.services.model_router import ModelRouter, estimate_cost_usd

logger = logging.getLogger(__name__)

#: Embeddings deliberately have no Azure client of their own: they borrow
#: `ModelRouter.client()`, so api-key / managed-identity / mock selection is
#: decided in exactly one place. If `client()` returns None, `embed()` returns
#: None and every caller degrades to keyword scoring.
_router = ModelRouter()
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> set[str]:
    return set(_WORD_RE.findall((value or "").lower()))


def to_vector_literal(vector: list[float]) -> str:
    """pgvector text form: `[0.1,0.2,…]`."""
    return "[" + ",".join(f"{float(v):.7f}" for v in vector) + "]"


def vector_param(name: str) -> str:
    """SQL for a bind parameter holding a `to_vector_literal` string.

    The double cast is load-bearing. `CAST(:q AS vector)` makes asyncpg infer the
    parameter's type as `vector`, and `app.db` registers pgvector's asyncpg codec,
    whose encoder iterates the value calling `float()` on each element. Handed the
    text form it iterates the *characters* and dies with

        invalid input for query argument $1: '[0.01,…]'
        (could not convert string to float: '[0.01,…]')

    Casting through `text` first pins the inferred type to `text` and lets
    Postgres' own `vector` input function parse it, which works whether or not
    the codec is registered. Sending a `list[float]` instead would work only with
    the codec, so this form is the portable one.
    """
    return f"CAST(CAST(:{name} AS text) AS vector)"


@asynccontextmanager
async def _contained(db: AsyncSession) -> AsyncIterator[None]:
    """Run a read-only probe inside a SAVEPOINT so a failure stays local.

    Retrieval is best-effort: every vector query here has a keyword fallback. But
    a failed statement leaves Postgres' transaction aborted, and the previous
    recovery — `await db.rollback()` — threw away the *caller's* transaction with
    it and expired every ORM object in the session. The next attribute access on
    a passed-in object then raised `MissingGreenlet` inside whatever was calling
    us, turning "retrieval degraded" into a 500 several frames away.

    A savepoint rolls back only what happened in here, so clean objects the
    caller still holds stay usable.
    """
    async with db.begin_nested():
        yield


def keyword_score(query: str, document: str) -> float:
    """Overlap ratio in 0..1 — the local-dev stand-in for cosine similarity."""
    q = _tokens(query)
    if not q:
        return 0.0
    d = _tokens(document)
    if not d:
        return 0.0
    return len(q & d) / len(q)


async def embed(
    texts: list[str],
    *,
    db: AsyncSession | None = None,
    bot_id: uuid.UUID | None = None,
) -> list[list[float]] | None:
    """Embed texts through the Azure embeddings deployment.

    Returns None when Azure is unconfigured or the call fails, which is the
    signal for callers to fall back to keyword scoring.
    """
    clean = [t for t in (texts or []) if t and t.strip()]
    if not clean:
        return None

    client = _router.client()
    if client is None:
        return None

    settings = get_settings()
    try:
        resp = await client.embeddings.create(
            model=settings.azure_deployment_embed,
            input=clean,
            timeout=settings.request_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - retrieval degrades, never fails a turn
        logger.warning("embedding call failed (%s) — falling back to keyword search", exc)
        return None

    vectors = [list(item.embedding) for item in resp.data]
    await _record_embed_cost(db, bot_id, resp, clean)
    return vectors


async def _record_embed_cost(
    db: AsyncSession | None,
    bot_id: uuid.UUID | None,
    resp: Any,
    texts: list[str],
) -> None:
    if db is None or bot_id is None:
        return
    usage = getattr(resp, "usage", None)
    tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    if not tokens:
        tokens = max(sum(len(t) for t in texts) // 4, 1)
    try:
        db.add(
            CostLedger(
                bot_id=bot_id,
                tier="embed",
                input_tokens=tokens,
                output_tokens=0,
                cost_usd=estimate_cost_usd("embed", tokens, 0),
            )
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record embedding cost: %s", exc)
        await db.rollback()


async def _write_embedding(db: AsyncSession, table: str, row_id: uuid.UUID, vector: list[float]) -> None:
    await db.execute(
        text(f"UPDATE {table} SET embedding = {vector_param('embedding')} WHERE id = :row_id"),  # noqa: S608
        {"embedding": to_vector_literal(vector), "row_id": str(row_id)},
    )
    await db.commit()


async def upsert_memory_embedding(db: AsyncSession, memory: Memory) -> None:
    vectors = await embed([memory.content], db=db, bot_id=memory.bot_id)
    if not vectors:
        return
    try:
        await _write_embedding(db, "memories", memory.id, vectors[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory embedding write failed: %s", exc)
        await db.rollback()


async def upsert_kb_embedding(db: AsyncSession, article: KbArticle) -> None:
    vectors = await embed([f"{article.title}\n\n{article.body}"], db=db)
    if not vectors:
        return
    try:
        await _write_embedding(db, "kb_articles", article.id, vectors[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb embedding write failed: %s", exc)
        await db.rollback()


async def search_kb(db: AsyncSession, query: str, limit: int = 5) -> list[tuple[KbArticle, float]]:
    """Vector search when embeddings exist, keyword overlap otherwise.

    Scores are similarities in 0..1 (higher is better).
    """
    vectors = await embed([query])
    if vectors:
        try:
            async with _contained(db):
                rows = await db.execute(
                    # noqa: S608 - `vector_param` returns a fixed cast expression
                    # naming a bind parameter; the vector itself is still bound.
                    text(
                        f"SELECT id, 1 - (embedding <=> {vector_param('q')}) AS score "  # noqa: S608
                        "FROM kb_articles WHERE embedding IS NOT NULL "
                        f"ORDER BY embedding <=> {vector_param('q')} LIMIT :limit"
                    ),
                    {"q": to_vector_literal(vectors[0]), "limit": limit},
                )
                scored = [(uuid.UUID(str(r[0])), float(r[1])) for r in rows.all()]
                if scored:
                    found = await db.execute(
                        select(KbArticle).where(KbArticle.id.in_([i for i, _ in scored]))
                    )
                    by_id = {a.id: a for a in found.scalars().all()}
                    return [(by_id[i], s) for i, s in scored if i in by_id]
        except Exception as exc:  # noqa: BLE001 - no pgvector / no index: degrade
            logger.warning("kb vector search failed (%s) — keyword fallback", exc)

    settings = get_settings()
    result = await db.execute(
        select(KbArticle).order_by(KbArticle.created_at.desc()).limit(settings.rag_max_candidates)
    )
    articles = list(result.scalars().all())
    ranked = sorted(
        ((a, keyword_score(query, f"{a.title} {a.body}")) for a in articles),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return [pair for pair in ranked[:limit] if pair[1] > 0] or ranked[:limit]


async def search_memories(
    db: AsyncSession,
    bot_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    query: str,
    limit: int = 8,
) -> list[Memory]:
    """Most relevant memories for this bot/user pair."""
    vectors = await embed([query])
    if vectors:
        try:
            async with _contained(db):
                rows = await db.execute(
                    # noqa: S608 - as above; only the cast expression is interpolated.
                    text(
                        "SELECT id FROM memories "  # noqa: S608
                        "WHERE embedding IS NOT NULL "
                        "  AND (CAST(:bot_id AS uuid) IS NULL OR bot_id = CAST(:bot_id AS uuid)) "
                        "  AND (CAST(:user_id AS uuid) IS NULL OR user_id = CAST(:user_id AS uuid)) "
                        f"ORDER BY embedding <=> {vector_param('q')} LIMIT :limit"
                    ),
                    {
                        "q": to_vector_literal(vectors[0]),
                        "bot_id": str(bot_id) if bot_id else None,
                        "user_id": str(user_id) if user_id else None,
                        "limit": limit,
                    },
                )
                ids = [uuid.UUID(str(r[0])) for r in rows.all()]
                if ids:
                    found = await db.execute(select(Memory).where(Memory.id.in_(ids)))
                    by_id = {m.id: m for m in found.scalars().all()}
                    return [by_id[i] for i in ids if i in by_id]
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory vector search failed (%s) — keyword fallback", exc)

    settings = get_settings()
    stmt = select(Memory)
    if bot_id is not None:
        stmt = stmt.where(Memory.bot_id == bot_id)
    if user_id is not None:
        stmt = stmt.where(Memory.user_id == user_id)
    result = await db.execute(
        stmt.order_by(Memory.created_at.desc()).limit(settings.rag_max_candidates)
    )
    memories = list(result.scalars().all())
    ranked = sorted(memories, key=lambda m: keyword_score(query, m.content), reverse=True)
    return ranked[:limit]
