"""Threads, messages, and the non-streaming turn."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import Approval, AuditEvent, Message, Run

MISSING = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


async def test_create_thread_requires_bot_ids(authed):
    response = await authed.post("/api/threads", json={"bot_ids": []})
    assert response.status_code == 400
    assert response.json()["code"] == "bot_ids_required"


async def test_create_thread(authed, bot_a):
    response = await authed.post(
        "/api/threads", json={"bot_ids": [str(bot_a.id)], "title": "Q3 planning"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Q3 planning"
    assert body["bot_ids"] == [str(bot_a.id)]


async def test_create_thread_defaults_the_title(authed, bot_a):
    response = await authed.post("/api/threads", json={"bot_ids": [str(bot_a.id)]})
    assert response.json()["title"] == "New thread"


async def test_create_thread_with_an_initial_message_runs_a_turn(authed, bot_a, db):
    response = await authed.post(
        "/api/threads",
        json={"bot_ids": [str(bot_a.id)], "initial_message": "hello there"},
    )
    assert response.status_code == 200
    thread_id = response.json()["id"]
    rows = await db.execute(select(Message).where(Message.thread_id == uuid.UUID(thread_id)))
    roles = [m.role for m in rows.scalars().all()]
    assert "user" in roles and "assistant" in roles


async def test_list_threads_is_owner_scoped(authed, make_thread, user_a, user_b, bot_a):
    mine = await make_thread(user_a, [bot_a], title="mine")
    theirs = await make_thread(user_b, [bot_a], title="theirs")
    response = await authed.get("/api/threads")
    ids = {t["id"] for t in response.json()}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids


async def test_send_message_returns_the_turn_result(authed, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    response = await authed.post(
        f"/api/threads/{thread.id}/messages", json={"content": "What is on my plate today?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["bot_id"] == str(bot_a.id)
    assert body["message"]
    assert body["run_id"]
    assert body["tier"] == "mini"


async def test_send_message_persists_both_turns(authed, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "ping"})
    listed = await authed.get(f"/api/threads/{thread.id}/messages")
    assert listed.status_code == 200
    messages = listed.json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "ping"


async def test_send_message_honours_an_idempotency_key(authed, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    headers = {"Idempotency-Key": "wf-1:run-1:act-1"}
    first = await authed.post(
        f"/api/threads/{thread.id}/messages", json={"content": "only once"}, headers=headers
    )
    second = await authed.post(
        f"/api/threads/{thread.id}/messages", json={"content": "only once"}, headers=headers
    )
    assert first.json() == second.json()

    listed = await authed.get(f"/api/threads/{thread.id}/messages")
    assert len(listed.json()) == 2, "the replay must not run a second turn"


async def test_send_message_to_a_thread_with_no_bots_is_an_error_event_not_a_500(
    authed, make_thread, user_a
):
    thread = await make_thread(user_a, [])
    response = await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "hi"})
    assert response.status_code == 200
    assert "thread has no bots" in response.json()["error"]


async def test_send_message_requires_content(authed, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    response = await authed.post(f"/api/threads/{thread.id}/messages", json={})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_mentions_pin_the_responding_bot(authed, make_thread, user_a, make_bot):
    first = await make_bot(user_a, name="First", slug="mention_first")
    second = await make_bot(user_a, name="Second", slug="mention_second")
    thread = await make_thread(user_a, [first, second])
    response = await authed.post(
        f"/api/threads/{thread.id}/messages",
        json={"content": "specific ask", "mention_bot_ids": [str(second.id)]},
    )
    assert response.json()["bot_id"] == str(second.id)


async def test_list_messages_on_a_missing_thread_is_404(authed):
    response = await authed.get(f"/api/threads/{MISSING}/messages")
    assert response.status_code == 404
    assert response.json()["code"] == "thread_not_found"


async def test_delete_thread_cascades_the_conversation(authed, make_thread, user_a, bot_a, db):
    """Deleting a conversation deletes the conversation: messages go with it."""
    thread = await make_thread(user_a, [bot_a])
    await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "leave a trace"})

    response = await authed.delete(f"/api/threads/{thread.id}")
    assert response.status_code == 200
    assert response.json()["detail"] == "deleted"

    remaining = await db.execute(select(Message).where(Message.thread_id == thread.id))
    assert remaining.scalars().all() == []
    assert (await authed.get(f"/api/threads/{thread.id}/messages")).status_code == 404


async def test_delete_thread_keeps_its_runs_as_audit_records(
    authed, make_thread, user_a, bot_a, db
):
    """`runs.thread_id` is ON DELETE SET NULL - the run itself is history."""
    thread = await make_thread(user_a, [bot_a])
    turn = await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "hi"})
    run_id = uuid.UUID(turn.json()["run_id"])

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    run = await db.get(Run, run_id)
    assert run is not None, "a run must outlive the thread it ran in"
    assert run.thread_id is None
    assert run.bot_id == bot_a.id


async def test_delete_thread_expires_pending_approvals_instead_of_dropping_them(
    authed, make_thread, make_run, make_approval, user_a, bot_a, db
):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    approval = await make_approval(bot_a, run=run, risk="send", requested_by=user_a)

    response = await authed.delete(f"/api/threads/{thread.id}")
    assert response.status_code == 200
    assert "1 pending approval" in response.json()["detail"]
    db.expunge_all()

    survivor = await db.get(Approval, approval.id)
    assert survivor is not None
    assert survivor.status == "expired"
    assert survivor.decided_at is not None
    assert survivor.decided_by is None, "expiry is not a human decision"
    assert "deleted" in (survivor.note or "")

    summary = await db.execute(
        select(AuditEvent).where(AuditEvent.event_type == "thread_deleted")
    )
    events = [e for e in summary.scalars().all() if e.detail.get("thread_id") == str(thread.id)]
    assert len(events) == 1
    assert events[0].detail["expired_approvals"] == 1


async def test_delete_missing_thread_is_404(authed):
    response = await authed.delete(f"/api/threads/{MISSING}")
    assert response.status_code == 404


async def test_delete_thread_keeps_the_owner_able_to_see_their_orphaned_run(
    authed, make_thread, user_a, bot_a, db
):
    """The owner must not lose their own run history by deleting the thread.

    Once `thread_id` is NULL, `resolve_run_owner` has nothing left to resolve on
    a shared system bot, so the delete path stamps the owner into
    `context_ledger` on the way out - the same trick the approvals use.
    """
    thread = await make_thread(user_a, [bot_a])
    turn = await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "hi"})
    run_id = uuid.UUID(turn.json()["run_id"])

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    run = await db.get(Run, run_id)
    assert run.thread_id is None
    assert run.context_ledger.get("requested_by") == str(user_a.id)

    assert (await authed.get(f"/api/runs/{run_id}")).status_code == 200
    listed = {row["id"] for row in (await authed.get("/api/runs")).json()}
    assert str(run_id) in listed, "the owner should still see their own orphaned run"
