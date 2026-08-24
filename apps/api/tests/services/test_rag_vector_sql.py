"""`app.services.rag` against a real pgvector database — the live-embedding path.

The rest of the rag suite runs keyless, so `embed()` returns None and the vector
branches are never entered. That gap let two bugs reach production the moment
managed-identity auth made embeddings real:

1. ``CAST(:q AS vector)`` makes asyncpg type the bind parameter as ``vector``.
   ``app.db`` registers pgvector's asyncpg codec, whose encoder calls ``float()``
   over the value — handed the text form it iterates *characters* and raises
   ``could not convert string to float: '[0.016,…]'``.
2. The recovery from (1) was ``await db.rollback()``, which discarded the
   caller's transaction and expired every ORM object in the session. The
   orchestrator's next ``thread.id`` then raised ``MissingGreenlet`` — a 500
   several frames away from a retrieval helper that was only meant to degrade.

These tests stub `embed()` so the vector path runs without Azure.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.models import KbArticle
from app.services import rag

DIM = 1536


def _vector(seed: float) -> list[float]:
    return [seed] + [0.0] * (DIM - 1)


@pytest.fixture
def stub_embed(monkeypatch):
    """Make `embed()` return a fixed vector, as a live Foundry account would."""

    def use(vector: list[float]):
        async def fake_embed(texts, *, db=None, bot_id=None):
            return [vector for _ in (texts or [])] or None

        monkeypatch.setattr(rag, "embed", fake_embed)

    return use


async def _pgvector_available(db) -> bool:
    try:
        await db.execute(text("SELECT '[1,2,3]'::vector"))
    except Exception:  # noqa: BLE001 - extension not installed in this database
        await db.rollback()
        return False
    return True


# ---------------------------------------------------------------------------
# The SQL form
# ---------------------------------------------------------------------------


def test_vector_param_casts_through_text():
    """Pinning the inferred parameter type to `text` is what makes this work."""
    assert rag.vector_param("q") == "CAST(CAST(:q AS text) AS vector)"


def test_no_bare_vector_cast_survives_in_the_module():
    """`CAST(:x AS vector)` is the shape asyncpg mis-types. Docstrings may say so."""
    import ast
    import inspect
    import re

    tree = ast.parse(inspect.getsource(rag))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            docstrings.add(id(body[0].value))

    code_strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]
    bare = re.compile(r"CAST\(:\w+ AS vector\)")
    offenders = [s for s in code_strings if bare.search(s)]
    assert not offenders, f"bare vector casts still in the SQL: {offenders}"
    assert any("AS text) AS vector)" in s for s in code_strings)


# ---------------------------------------------------------------------------
# Round trips against the real database
# ---------------------------------------------------------------------------


async def test_writing_and_searching_an_embedding_round_trips(db, stub_embed):
    """The exact statements that failed in production, against real Postgres."""
    if not await _pgvector_available(db):
        pytest.skip("pgvector extension is not installed in the test database")

    target = KbArticle(title="Flange calibration", body="Turn the flange counterclockwise.")
    other = KbArticle(title="Coffee machine", body="Descale monthly.")
    db.add_all([target, other])
    await db.commit()

    # `_write_embedding` uses the same cast; a bad one fails here first.
    await rag._write_embedding(db, "kb_articles", target.id, _vector(1.0))
    await rag._write_embedding(db, "kb_articles", other.id, _vector(-1.0))

    stub_embed(_vector(1.0))
    hits = await rag.search_kb(db, "anything at all", limit=5)
    assert hits, "the vector search returned nothing"
    top_article, top_score = hits[0]
    assert top_article.id == target.id
    # A cosine similarity, not the keyword overlap the fallback would produce.
    assert top_score == pytest.approx(1.0, abs=1e-6)


async def test_memory_vector_search_round_trips(db, make_user, make_bot, make_memory, stub_embed):
    if not await _pgvector_available(db):
        pytest.skip("pgvector extension is not installed in the test database")

    user = await make_user()
    bot = await make_bot(user)
    wanted = await make_memory(bot, user, content="the flange is calibrated")
    unwanted = await make_memory(bot, user, content="the coffee machine is descaled")

    await rag._write_embedding(db, "memories", wanted.id, _vector(1.0))
    await rag._write_embedding(db, "memories", unwanted.id, _vector(-1.0))

    stub_embed(_vector(1.0))
    found = await rag.search_memories(db, bot.id, user.id, "anything at all", limit=5)
    assert [m.id for m in found][:1] == [wanted.id]


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


async def test_a_failing_vector_search_still_returns_the_keyword_fallback(db, stub_embed):
    db.add(KbArticle(title="Flange calibration", body="Turn the flange counterclockwise."))
    await db.commit()

    stub_embed([1.0, 2.0])  # wrong dimensionality — Postgres rejects the comparison
    hits = await rag.search_kb(db, "flange calibration", limit=5)
    assert hits, "a failed vector search must fall back, not return nothing"
    assert hits[0][0].title == "Flange calibration"


async def test_a_failing_vector_search_leaves_the_callers_objects_usable(db, stub_embed):
    """The MissingGreenlet regression: degrading must not nuke the session.

    A bare `db.rollback()` here expires every loaded object, so the caller's next
    plain attribute access becomes a lazy load — which, on an async session,
    raises `MissingGreenlet` from wherever that caller happens to be.
    """
    article = KbArticle(title="Flange calibration", body="Turn the flange counterclockwise.")
    db.add(article)
    await db.commit()
    article_id = article.id

    stub_embed([1.0, 2.0])  # forces the vector query to fail
    await rag.search_kb(db, "flange", limit=5)

    # No await, no refresh: exactly what the orchestrator does with `thread.id`.
    assert article.id == article_id
    assert article.title == "Flange calibration"


async def test_a_failing_memory_search_leaves_the_callers_objects_usable(
    db, make_user, make_bot, make_memory, stub_embed
):
    user = await make_user()
    bot = await make_bot(user)
    memory = await make_memory(bot, user, content="the flange is calibrated")
    bot_id = bot.id

    stub_embed([1.0, 2.0])
    await rag.search_memories(db, bot.id, user.id, "flange", limit=5)

    assert bot.id == bot_id
    assert bot.name is not None
    assert memory.content == "the flange is calibrated"


async def test_the_session_can_still_commit_after_a_failed_vector_search(db, stub_embed):
    """The savepoint rolls back; the outer transaction survives to do more work."""
    stub_embed([1.0, 2.0])
    await rag.search_kb(db, "flange", limit=5)

    db.add(KbArticle(title="Written afterwards", body="the transaction survived"))
    await db.commit()
    found = await db.get(KbArticle, uuid.UUID(str(await _latest_kb_id(db))))
    assert found is not None and found.title == "Written afterwards"


async def _latest_kb_id(db):
    row = await db.execute(text("SELECT id FROM kb_articles ORDER BY created_at DESC LIMIT 1"))
    return row.scalar_one()
