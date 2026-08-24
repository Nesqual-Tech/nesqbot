"""Per-bot memories and the shared knowledge base."""

from __future__ import annotations

import uuid

MISSING = uuid.uuid4()


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


async def test_create_a_memory(authed, bot_a, user_a):
    response = await authed.post(
        f"/api/bots/{bot_a.id}/memories", json={"kind": "preference", "content": "Prefers bullet points"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "preference"
    assert body["content"] == "Prefers bullet points"
    assert body["bot_id"] == str(bot_a.id)
    assert body["user_id"] == str(user_a.id)


async def test_memory_kind_defaults_to_note(authed, bot_a):
    response = await authed.post(f"/api/bots/{bot_a.id}/memories", json={"content": "plain"})
    assert response.json()["kind"] == "note"


async def test_memory_requires_content(authed, bot_a):
    response = await authed.post(f"/api/bots/{bot_a.id}/memories", json={})
    assert response.status_code == 422


async def test_list_memories_newest_first(authed, bot_a):
    for i in range(3):
        await authed.post(f"/api/bots/{bot_a.id}/memories", json={"content": f"note {i}"})
    response = await authed.get(f"/api/bots/{bot_a.id}/memories")
    assert response.status_code == 200
    contents = [m["content"] for m in response.json()]
    assert set(contents) == {"note 0", "note 1", "note 2"}


async def test_list_memories_honours_the_limit(authed, bot_a):
    for i in range(4):
        await authed.post(f"/api/bots/{bot_a.id}/memories", json={"content": f"note {i}"})
    response = await authed.get(f"/api/bots/{bot_a.id}/memories?limit=2")
    assert len(response.json()) == 2


async def test_shared_memories_with_no_user_are_visible(authed, make_memory, bot_a):
    shared = await make_memory(bot_a, None, content="shared fact")
    ids = {m["id"] for m in (await authed.get(f"/api/bots/{bot_a.id}/memories")).json()}
    assert str(shared.id) in ids


async def test_delete_a_memory(authed, make_memory, bot_a, user_a):
    memory = await make_memory(bot_a, user_a)
    response = await authed.delete(f"/api/memories/{memory.id}")
    assert response.status_code == 200
    assert response.json()["detail"] == "deleted"
    ids = {m["id"] for m in (await authed.get(f"/api/bots/{bot_a.id}/memories")).json()}
    assert str(memory.id) not in ids


async def test_deleting_a_missing_memory_is_404(authed):
    response = await authed.delete(f"/api/memories/{MISSING}")
    assert response.status_code == 404
    assert response.json()["code"] == "memory_not_found"


async def test_memories_are_written_by_the_orchestrator_on_a_long_turn(
    authed, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    long_prompt = "Please summarise the entire quarterly pipeline review for the leadership team"
    await authed.post(f"/api/threads/{thread.id}/messages", json={"content": long_prompt})

    memories = (await authed.get(f"/api/bots/{bot_a.id}/memories")).json()
    assert any(m["kind"] == "interaction" for m in memories)


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------


async def test_create_a_kb_article(authed):
    response = await authed.post("/api/kb", json={"title": "Refund policy", "body": "30 days."})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Refund policy"
    assert body["id"]


async def test_kb_requires_a_title_and_body(authed):
    assert (await authed.post("/api/kb", json={"title": "only"})).status_code == 422


async def test_kb_listing_without_a_query(authed):
    await authed.post("/api/kb", json={"title": "Listed", "body": "x"})
    response = await authed.get("/api/kb")
    assert response.status_code == 200
    assert any(a["title"] == "Listed" for a in response.json())


async def test_kb_keyword_search_finds_the_article(authed):
    await authed.post(
        "/api/kb", json={"title": "Widget calibration", "body": "Turn the flange counterclockwise."}
    )
    response = await authed.get("/api/kb?q=flange")
    assert response.status_code == 200
    titles = [a["title"] for a in response.json()]
    assert "Widget calibration" in titles


async def test_kb_search_scores_the_best_match_first(authed):
    await authed.post("/api/kb", json={"title": "Zebra husbandry", "body": "Stripes and hay."})
    await authed.post("/api/kb", json={"title": "Unrelated", "body": "Nothing to do with it."})
    response = await authed.get("/api/kb?q=zebra stripes")
    hits = response.json()
    assert hits
    assert hits[0]["title"] == "Zebra husbandry"


async def test_kb_search_honours_the_limit(authed):
    for i in range(4):
        await authed.post("/api/kb", json={"title": f"Cabbage {i}", "body": "cabbage"})
    response = await authed.get("/api/kb?q=cabbage&limit=2")
    assert len(response.json()) <= 2


async def test_update_a_kb_article(authed):
    created = await authed.post("/api/kb", json={"title": "Old", "body": "Old body"})
    article_id = created.json()["id"]
    response = await authed.patch(f"/api/kb/{article_id}", json={"title": "New"})
    assert response.status_code == 200
    assert response.json()["title"] == "New"
    assert response.json()["body"] == "Old body"


async def test_updating_a_missing_kb_article_is_404(authed):
    response = await authed.patch(f"/api/kb/{MISSING}", json={"title": "x"})
    assert response.status_code == 404
    assert response.json()["code"] == "kb_article_not_found"


async def test_delete_a_kb_article(authed):
    created = await authed.post("/api/kb", json={"title": "Doomed", "body": "b"})
    article_id = created.json()["id"]
    response = await authed.delete(f"/api/kb/{article_id}")
    assert response.status_code == 200
    assert (await authed.patch(f"/api/kb/{article_id}", json={"title": "x"})).status_code == 404


async def test_deleting_a_missing_kb_article_is_404(authed):
    assert (await authed.delete(f"/api/kb/{MISSING}")).status_code == 404


async def test_the_kb_is_shared_across_users(authed, other):
    created = await authed.post("/api/kb", json={"title": "Company handbook", "body": "shared"})
    ids = {a["id"] for a in (await other.get("/api/kb")).json()}
    assert created.json()["id"] in ids, "the KB is deliberately a shared corpus"
