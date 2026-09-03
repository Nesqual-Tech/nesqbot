"""Threads, messages, and the non-streaming turn."""

from __future__ import annotations

import contextlib
import uuid

import pytest_asyncio
from sqlalchemy import event, func, select

from app.models import Approval, AuditEvent, Message, Run, ThreadBot

MISSING = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


@contextlib.contextmanager
def counted_statements(db_connection):
    """Every statement the engine sends while the block runs.

    The only assertion that catches a regression back to a per-row query: the
    payload is identical either way, so asserting on the payload proves nothing
    about how many round trips produced it.
    """
    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    engine = db_connection.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before)


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


async def test_creating_a_thread_with_an_unknown_bot_id_is_a_404_with_a_code(authed):
    """`thread_bots.bot_id` is a foreign key, and an unknown id used to reach it.

    The IntegrityError came back through `app.errors`' catch-all as
    `{"detail": "internal_error", "code": "internal_error"}` — an opaque 500 on
    an ordinary user action (a bot deleted in another window, a stale client
    cache), which is exactly what the error envelope exists to prevent.
    """
    response = await authed.post("/api/threads", json={"bot_ids": [str(MISSING)]})
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


async def test_creating_a_thread_with_another_users_bot_writes_no_membership(
    authed, bot_b, db
):
    """Membership is trusted at read time, so it has to be checked at write time.

    The orchestrator's roster query joins `thread_bots` with no visibility
    predicate. A bot id belonging to someone else's custom bot, accepted here,
    becomes a participant in the thread and answers with its own system prompt
    and its own connector set.
    """
    response = await authed.post("/api/threads", json={"bot_ids": [str(bot_b.id)]})
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"

    rows = await db.execute(
        select(func.count()).select_from(ThreadBot).where(ThreadBot.bot_id == bot_b.id)
    )
    assert int(rows.scalar_one()) == 0, "the membership row was written anyway"


async def test_creating_a_thread_naming_the_same_bot_twice_keeps_one_membership(
    authed, bot_a, db
):
    """`thread_bots` has a composite primary key: `[x, x]` was a second 500."""
    response = await authed.post(
        "/api/threads", json={"bot_ids": [str(bot_a.id), str(bot_a.id)]}
    )
    assert response.status_code == 200
    assert response.json()["bot_ids"] == [str(bot_a.id)]

    thread_id = uuid.UUID(response.json()["id"])
    rows = await db.execute(
        select(func.count()).select_from(ThreadBot).where(ThreadBot.thread_id == thread_id)
    )
    assert int(rows.scalar_one()) == 1


async def test_listing_threads_does_not_cost_a_query_per_thread(
    authed, db_connection, make_thread, user_a, bot_a
):
    """`GET /threads` was 1+N: the sidebar's roster came from a SELECT per row.

    Counted rather than timed, and compared against itself with more rows rather
    than against a fixed number, so the assertion says the one thing that
    matters — the cost does not grow with the list — without pinning the exact
    query plan of the auth dependency or the ORM.
    """
    await make_thread(user_a, [bot_a], title="one")
    with counted_statements(db_connection) as one_thread:
        assert (await authed.get("/api/threads")).status_code == 200

    for index in range(5):
        await make_thread(user_a, [bot_a], title=f"thread {index}")
    with counted_statements(db_connection) as six_threads:
        listed = await authed.get("/api/threads")
    assert len(listed.json()) == 6

    assert len(six_threads) == len(one_thread), (
        "listing six threads cost more statements than listing one — the roster "
        f"lookup is back inside the loop: {len(one_thread)} -> {len(six_threads)}"
    )


async def test_listing_threads_reports_every_bot_on_the_thread(
    authed, make_thread, user_a, bot_a, make_bot
):
    """The grouped roster query has to return the same membership as the loop did."""
    second = await make_bot(user_a, name="Second")
    thread = await make_thread(user_a, [bot_a, second])

    listed = (await authed.get("/api/threads")).json()
    row = next(t for t in listed if t["id"] == str(thread.id))
    assert set(row["bot_ids"]) == {str(bot_a.id), str(second.id)}
    assert row["bot_ids"] == sorted(row["bot_ids"]), (
        "the roster order is pinned by the query's ORDER BY, not left to the plan"
    )


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



@pytest_asyncio.fixture
async def mate(make_bot, user_a):
    """A second bot belonging to the *caller*.

    Not `bot_b`, which belongs to user B: seating that one is correctly a 404,
    and a roster test written against it proves the visibility check rather
    than the feature.
    """
    return await make_bot(user_a, name="A's second bot", slug="a-second-bot")

# ---------------------------------------------------------------------------
# The roster: what makes delegation possible at all
# ---------------------------------------------------------------------------
#
# `orchestrator._delegate_targets` is "everyone else in this room", so a
# one-bot thread means `delegate_to_bot` is never even advertised and a chief
# of staff asked to hand work over holds no tool that can. The desktop app
# created every thread with exactly one bot
# (`ChatPane.tsx`: `bot_ids: [activeBot.id]`), so in the shipped product no
# thread ever had a second participant - which is the whole of every "it never
# delegated" report. A roster could only be set at creation until these two
# routes existed.


async def test_more_bots_can_be_seated_on_an_existing_thread(authed, db, bot_a, mate):
    created = (
        await authed.post("/api/threads", json={"bot_ids": [str(bot_a.id)], "title": "Kickoff"})
    ).json()
    assert created["bot_ids"] == [str(bot_a.id)]

    response = await authed.post(
        f"/api/threads/{created['id']}/bots", json={"bot_ids": [str(mate.id)]}
    )

    assert response.status_code == 200, response.text
    assert set(response.json()["bot_ids"]) == {str(bot_a.id), str(mate.id)}


async def test_seating_a_bot_twice_is_not_an_error(authed, bot_a, mate):
    """`thread_bots` has a composite primary key, so a second insert is a 500.

    The obvious client behaviour is to send the whole intended roster rather
    than the difference, so idempotence here is the difference between a
    working "add these three" button and an intermittent server error.
    """
    created = (await authed.post("/api/threads", json={"bot_ids": [str(bot_a.id)]})).json()
    first = await authed.post(
        f"/api/threads/{created['id']}/bots", json={"bot_ids": [str(mate.id)]}
    )
    second = await authed.post(
        f"/api/threads/{created['id']}/bots",
        json={"bot_ids": [str(bot_a.id), str(mate.id)]},
    )

    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert set(second.json()["bot_ids"]) == {str(bot_a.id), str(mate.id)}


async def test_a_bot_the_caller_cannot_see_is_refused(authed, bot_a, user_b, make_bot):
    """The write side of the visibility boundary `create_thread` also enforces.

    Membership is trusted at read time by a query with no visibility predicate
    in it, so accepting a stranger's bot here would seat a participant that
    answers with its own prompt and its own connectors.
    """
    theirs = await make_bot(user_b, name="Not Yours", slug="not-yours-roster")
    created = (await authed.post("/api/threads", json={"bot_ids": [str(bot_a.id)]})).json()

    response = await authed.post(
        f"/api/threads/{created['id']}/bots", json={"bot_ids": [str(theirs.id)]}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


async def test_an_empty_roster_request_is_refused(authed, bot_a):
    created = (await authed.post("/api/threads", json={"bot_ids": [str(bot_a.id)]})).json()
    response = await authed.post(f"/api/threads/{created['id']}/bots", json={"bot_ids": []})
    assert response.status_code == 400
    assert response.json()["code"] == "bot_ids_required"


async def test_a_bot_can_be_unseated(authed, bot_a, mate):
    created = (
        await authed.post("/api/threads", json={"bot_ids": [str(bot_a.id), str(mate.id)]})
    ).json()

    response = await authed.delete(f"/api/threads/{created['id']}/bots/{mate.id}")

    assert response.status_code == 200, response.text
    assert response.json()["bot_ids"] == [str(bot_a.id)]


async def test_the_last_bot_cannot_be_unseated(authed, bot_a):
    """`_turn` raises "thread has no bots", so an empty roster is a dead page."""
    created = (await authed.post("/api/threads", json={"bot_ids": [str(bot_a.id)]})).json()

    response = await authed.delete(f"/api/threads/{created['id']}/bots/{bot_a.id}")

    assert response.status_code == 409
    assert response.json()["code"] == "last_bot_in_thread"


async def test_unseating_a_bot_that_was_never_there_is_a_404(authed, bot_a, mate):
    created = (await authed.post("/api/threads", json={"bot_ids": [str(bot_a.id)]})).json()
    response = await authed.delete(f"/api/threads/{created['id']}/bots/{mate.id}")
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_in_thread"


async def test_another_users_thread_is_not_reachable(authed, db, user_b, make_bot, bot_a):
    """Thread ownership is checked first, as everywhere else."""
    from app.models import Thread, ThreadBot

    theirs = Thread(title="Theirs", owner_user_id=user_b.id)
    db.add(theirs)
    await db.commit()
    await db.refresh(theirs)
    their_bot = await make_bot(user_b, name="Theirs", slug="theirs-roster")
    db.add(ThreadBot(thread_id=theirs.id, bot_id=their_bot.id))
    await db.commit()

    response = await authed.post(
        f"/api/threads/{theirs.id}/bots", json={"bot_ids": [str(bot_a.id)]}
    )

    assert response.status_code == 404
