"""Per-bot memories and the shared knowledge base."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.errors import AppError
from app.models import AuditEvent, KbArticle, Memory, User
from app.routers.deps import get_visible_bot, optional_service
from app.schemas import (
    KbArticleIn,
    KbArticleOut,
    KbArticleUpdateIn,
    MemoryIn,
    MemoryOut,
    OkOut,
)

logger = logging.getLogger("nesqbot.knowledge")

router = APIRouter(tags=["knowledge"])


def _rag():
    return optional_service("rag")


async def _embed_memory(db: AsyncSession, memory: Memory) -> None:
    rag = _rag()
    fn = getattr(rag, "upsert_memory_embedding", None) if rag else None
    if fn is None:
        return
    try:
        await fn(db, memory)
    except Exception:  # noqa: BLE001 - embeddings are best effort (no Azure keys locally)
        logger.warning("memory embedding failed for %s", memory.id, exc_info=True)


async def _embed_article(db: AsyncSession, article: KbArticle) -> None:
    rag = _rag()
    fn = getattr(rag, "upsert_kb_embedding", None) if rag else None
    if fn is None:
        return
    try:
        await fn(db, article)
    except Exception:  # noqa: BLE001
        logger.warning("kb embedding failed for %s", article.id, exc_info=True)


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


@router.get("/bots/{bot_id}/memories", response_model=list[MemoryOut])
async def list_memories(
    bot_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Memory]:
    await get_visible_bot(db, bot_id, user)
    result = await db.execute(
        select(Memory)
        .where(
            Memory.bot_id == bot_id,
            or_(Memory.user_id == user.id, Memory.user_id.is_(None)),
        )
        .order_by(Memory.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.post("/bots/{bot_id}/memories", response_model=MemoryOut)
async def create_memory(
    bot_id: uuid.UUID,
    body: MemoryIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Memory:
    await get_visible_bot(db, bot_id, user)
    memory = Memory(bot_id=bot_id, user_id=user.id, kind=body.kind, content=body.content)
    db.add(memory)
    await db.commit()
    await db.refresh(memory)
    await _embed_memory(db, memory)
    return memory


@router.delete("/memories/{memory_id}", response_model=OkOut)
async def delete_memory(
    memory_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    memory = await db.get(Memory, memory_id)
    if memory is None:
        raise AppError(404, "memory_not_found", "Memory not found")
    if memory.user_id not in (None, user.id):
        raise AppError(404, "memory_not_found", "Memory not found")
    if memory.bot_id is not None:
        await get_visible_bot(db, memory.bot_id, user)
    await db.delete(memory)
    await db.commit()
    return OkOut(ok=True, detail="deleted")


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------


@router.get("/kb", response_model=list[KbArticleOut])
async def search_kb(
    q: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[KbArticleOut]:
    """Vector search when embeddings are available, keyword LIKE otherwise."""
    if q:
        rag = _rag()
        search = getattr(rag, "search_kb", None) if rag else None
        if search is not None:
            try:
                hits = await search(db, q, limit)
            except Exception:  # noqa: BLE001 - fall back to keyword search
                logger.warning("vector kb search failed, falling back", exc_info=True)
                hits = None
            if hits:
                out: list[KbArticleOut] = []
                for hit in hits:
                    article, score = hit if isinstance(hit, (tuple, list)) else (hit, None)
                    model = KbArticleOut.model_validate(article)
                    model.score = float(score) if score is not None else None
                    out.append(model)
                return out

        pattern = f"%{q}%"
        stmt = (
            select(KbArticle)
            .where(or_(KbArticle.title.ilike(pattern), KbArticle.body.ilike(pattern)))
            .order_by(KbArticle.created_at.desc())
            .limit(limit)
        )
    else:
        stmt = select(KbArticle).order_by(KbArticle.created_at.desc()).limit(limit)

    result = await db.execute(stmt)
    return [KbArticleOut.model_validate(a) for a in result.scalars().all()]


@router.post("/kb", response_model=KbArticleOut)
async def create_kb_article(
    body: KbArticleIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> KbArticleOut:
    article = KbArticle(title=body.title, body=body.body)
    db.add(article)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="kb_article_created",
            detail={"title": body.title},
        )
    )
    await db.commit()
    await db.refresh(article)
    await _embed_article(db, article)
    return KbArticleOut.model_validate(article)


@router.patch("/kb/{article_id}", response_model=KbArticleOut)
async def update_kb_article(
    article_id: uuid.UUID,
    body: KbArticleUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> KbArticleOut:
    article = await db.get(KbArticle, article_id)
    if article is None:
        raise AppError(404, "kb_article_not_found", "KB article not found")
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if value is not None:
            setattr(article, field, value)
    await db.commit()
    await db.refresh(article)
    await _embed_article(db, article)
    return KbArticleOut.model_validate(article)


@router.delete("/kb/{article_id}", response_model=OkOut)
async def delete_kb_article(
    article_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    article = await db.get(KbArticle, article_id)
    if article is None:
        raise AppError(404, "kb_article_not_found", "KB article not found")
    await db.delete(article)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="kb_article_deleted",
            detail={"article_id": str(article_id)},
        )
    )
    await db.commit()
    return OkOut(ok=True, detail="deleted")
