"""`app.services.rag` — the keyword fallback that keeps retrieval working
without Azure keys."""

from __future__ import annotations

import pytest

from app.services import rag

# ---------------------------------------------------------------------------
# embed() signals "no vectors" by returning None
# ---------------------------------------------------------------------------


async def test_embed_returns_none_when_azure_is_unconfigured():
    """None is the documented signal for callers to fall back to keywords."""
    assert rag._router.client() is None
    assert await rag.embed(["anything"]) is None


@pytest.mark.parametrize("texts", [[], [""], ["   ", ""], None])
async def test_embed_returns_none_for_empty_input(texts):
    assert await rag.embed(texts) is None


# ---------------------------------------------------------------------------
# keyword_score
# ---------------------------------------------------------------------------


def test_keyword_score_is_the_query_overlap_ratio():
    assert rag.keyword_score("alpha beta", "alpha beta gamma") == 1.0
    assert rag.keyword_score("alpha beta", "alpha only") == 0.5
    assert rag.keyword_score("alpha beta", "nothing here") == 0.0


def test_keyword_score_is_case_and_punctuation_insensitive():
    assert rag.keyword_score("Flange!", "the flange, calibrated") == 1.0


@pytest.mark.parametrize(("query", "document"), [("", "anything"), ("query", ""), ("!!!", "x")])
def test_keyword_score_handles_empty_sides(query, document):
    assert rag.keyword_score(query, document) == 0.0


def test_keyword_score_is_bounded():
    for query, document in [("a b c", "a b c a b c"), ("a", "a a a a")]:
        assert 0.0 <= rag.keyword_score(query, document) <= 1.0


def test_to_vector_literal_is_pgvector_text_form():
    assert rag.to_vector_literal([0.1, -2.0]) == "[0.1000000,-2.0000000]"


# ---------------------------------------------------------------------------
# search_kb falls back to keyword ranking
# ---------------------------------------------------------------------------


async def test_search_kb_ranks_by_keyword_overlap_without_embeddings(db):
    from app.models import KbArticle

    best = KbArticle(title="Flange calibration", body="Turn the flange counterclockwise.")
    worst = KbArticle(title="Coffee machine", body="Descale monthly.")
    db.add_all([best, worst])
    await db.commit()

    hits = await rag.search_kb(db, "flange calibration", limit=5)
    assert hits, "the keyword fallback returned nothing"
    top_article, top_score = hits[0]
    assert top_article.title == "Flange calibration"
    assert 0.0 < top_score <= 1.0
    assert all(isinstance(score, float) for _, score in hits)


async def test_search_kb_honours_the_limit(db):
    from app.models import KbArticle

    db.add_all([KbArticle(title=f"Widget {i}", body="widget") for i in range(6)])
    await db.commit()
    assert len(await rag.search_kb(db, "widget", limit=3)) == 3


async def test_search_kb_with_no_match_still_returns_something_to_show(db):
    from app.models import KbArticle

    db.add(KbArticle(title="Only article", body="unrelated"))
    await db.commit()
    hits = await rag.search_kb(db, "zzzzz-no-such-token", limit=3)
    assert isinstance(hits, list)


# ---------------------------------------------------------------------------
# search_memories falls back the same way
# ---------------------------------------------------------------------------


async def test_search_memories_ranks_by_keyword_overlap(db, make_user, make_bot, make_memory):
    user = await make_user()
    bot = await make_bot(user)
    await make_memory(bot, user, content="Prefers concise bullet points in briefs")
    await make_memory(bot, user, content="Drinks tea, never coffee")

    hits = await rag.search_memories(db, bot.id, user.id, "bullet points", limit=5)
    assert hits
    assert "bullet points" in hits[0].content


async def test_search_memories_is_scoped_to_the_bot_and_user(
    db, make_user, make_bot, make_memory
):
    owner = await make_user()
    stranger = await make_user()
    bot = await make_bot(owner)
    mine = await make_memory(bot, owner, content="shared token here")
    await make_memory(bot, stranger, content="shared token here too")

    hits = await rag.search_memories(db, bot.id, owner.id, "shared token", limit=10)
    assert [m.id for m in hits] == [mine.id]


async def test_search_memories_with_no_rows_returns_empty(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    assert await rag.search_memories(db, bot.id, user.id, "anything") == []


# ---------------------------------------------------------------------------
# Embedding writes degrade quietly when there is nothing to write
# ---------------------------------------------------------------------------


async def test_upsert_memory_embedding_is_a_no_op_without_azure(db, make_user, make_bot, make_memory):
    user = await make_user()
    bot = await make_bot(user)
    memory = await make_memory(bot, user)
    await rag.upsert_memory_embedding(db, memory)  # must not raise
    assert await db.get(type(memory), memory.id) is not None


async def test_upsert_kb_embedding_is_a_no_op_without_azure(db):
    from app.models import KbArticle

    article = KbArticle(title="t", body="b")
    db.add(article)
    await db.commit()
    await db.refresh(article)
    await rag.upsert_kb_embedding(db, article)  # must not raise
    assert await db.get(KbArticle, article.id) is not None
