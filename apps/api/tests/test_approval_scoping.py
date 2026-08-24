"""Approval ownership resolution — the scoping keys from docs/API.md.

> Approvals are scoped by requester, not by bot — inheriting visibility from a
> shared system bot would leave every send/spend/delete approval decidable by
> any authenticated user.

Owner precedence is `requested_by` -> the thread owner behind `run_id` -> the
custom bot's `owner_user_id`. `created_by` grants **read only**, so a worker can
poll for the outcome but can never decide. An approval with no knowable human
is the genuine unattended case and falls back to bot visibility.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import Approval, AuditEvent, Run
from app.routers.deps import (
    CREATED_BY_KEY,
    REQUESTED_BY_KEY,
    approval_owner,
    create_gated_approval,
    resolve_approval_owner,
)
from tests.conftest import _client_for, auth_headers


def test_the_scoping_keys_are_the_documented_names():
    assert REQUESTED_BY_KEY == "requested_by"
    assert CREATED_BY_KEY == "created_by"


# ---------------------------------------------------------------------------
# resolve_approval_owner — precedence
# ---------------------------------------------------------------------------


async def test_requested_by_wins_over_everything(db, make_user, make_bot, make_thread, make_run):
    requester = await make_user()
    thread_owner = await make_user()
    bot_owner = await make_user()
    bot = await make_bot(bot_owner)
    thread = await make_thread(thread_owner, [bot])
    run = await make_run(thread, bot)

    owner = await resolve_approval_owner(
        db, run_id=run.id, payload={REQUESTED_BY_KEY: str(requester.id)}, bot=bot
    )
    assert owner == requester.id


async def test_the_thread_owner_behind_run_id_is_next(db, make_user, make_bot, make_thread, make_run):
    thread_owner = await make_user()
    bot_owner = await make_user()
    bot = await make_bot(bot_owner)
    thread = await make_thread(thread_owner, [bot])
    run = await make_run(thread, bot)

    assert await resolve_approval_owner(db, run_id=run.id, payload={}, bot=bot) == thread_owner.id


async def test_a_custom_bots_owner_is_the_last_resort(db, make_user, make_bot):
    bot_owner = await make_user()
    bot = await make_bot(bot_owner)
    assert await resolve_approval_owner(db, run_id=None, payload={}, bot=bot) == bot_owner.id


async def test_a_system_bot_with_no_other_signal_has_no_knowable_owner(db, system_bot):
    assert await resolve_approval_owner(db, run_id=None, payload={}, bot=system_bot) is None


async def test_an_unparseable_requested_by_falls_through_to_the_next_level(
    db, make_user, make_bot, make_thread, make_run
):
    thread_owner = await make_user()
    bot = await make_bot(await make_user())
    thread = await make_thread(thread_owner, [bot])
    run = await make_run(thread, bot)

    owner = await resolve_approval_owner(
        db, run_id=run.id, payload={REQUESTED_BY_KEY: "not-a-uuid"}, bot=bot
    )
    assert owner == thread_owner.id


async def test_approval_owner_resolves_from_a_persisted_row(db, make_user, make_bot, make_approval):
    requester = await make_user()
    bot = await make_bot(await make_user())
    approval = await make_approval(bot, requested_by=requester)
    assert await approval_owner(db, approval) == requester.id


# ---------------------------------------------------------------------------
# Stamping at creation — scoping must survive thread deletion
# ---------------------------------------------------------------------------


async def test_create_gated_approval_stamps_the_resolved_owner(
    db, make_user, make_bot, make_thread, make_run
):
    thread_owner = await make_user()
    bot = await make_bot(thread_owner)
    thread = await make_thread(thread_owner, [bot])
    run = await make_run(thread, bot)

    approval = await create_gated_approval(
        db,
        bot_id=bot.id,
        run_id=run.id,
        risk="send",
        title="t",
        summary="s",
        payload={"kind": "message_only"},
    )
    assert approval.payload[REQUESTED_BY_KEY] == str(thread_owner.id)


async def test_scoping_survives_when_the_originating_thread_is_gone(
    authed, other, db, user_a, system_bot, make_thread
):
    """The stamped `requested_by` is what keeps an approval scoped once the
    thread it came from no longer exists to resolve an owner from."""
    thread = await make_thread(user_a, [system_bot])
    approval = await create_gated_approval(
        db,
        bot_id=system_bot.id,
        risk="send",
        title="held",
        summary="s",
        payload={"kind": "message_only", "draft": "x", REQUESTED_BY_KEY: str(user_a.id)},
    )
    approval_id = approval.id

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    assert await db.get(Approval, approval_id) is not None
    assert (await authed.get(f"/api/approvals/{approval_id}")).status_code == 200
    assert (await other.get(f"/api/approvals/{approval_id}")).status_code == 404


async def test_a_run_linked_approval_outlives_its_thread(
    authed, db, user_a, bot_a, make_thread, make_run
):
    """Deleting a thread must not take its pending approvals with it.

    An approval is the record of what a bot was authorised to do; it outlives
    the conversation that produced it. The run survives too, with its thread
    link nulled, and the approval is expired with a note rather than silently
    disappearing from the queue.
    """
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    approval = await create_gated_approval(
        db,
        bot_id=bot_a.id,
        run_id=run.id,
        risk="send",
        title="held",
        summary="s",
        payload={"kind": "message_only", "draft": "x"},
    )
    approval_id, run_id = approval.id, run.id

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    surviving_run = await db.get(Run, run_id)
    assert surviving_run is not None, "runs are audit records and must survive"
    assert surviving_run.thread_id is None

    surviving = await db.get(Approval, approval_id)
    assert surviving is not None, "the approval must survive its thread"
    assert surviving.status == "expired"
    assert surviving.run_id == run_id, "the run is still there, so the link holds"
    assert surviving.note and str(thread.id) in surviving.note

    body = (await authed.get(f"/api/approvals/{approval_id}")).json()
    assert body["status"] == "expired"


async def test_expiring_on_thread_delete_writes_an_audit_trail(
    authed, db, user_a, bot_a, make_thread, make_run
):
    """A pending approval is never dropped without a record of why."""
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    approval = await create_gated_approval(
        db,
        bot_id=bot_a.id,
        run_id=run.id,
        risk="send",
        title="held",
        summary="s",
        payload={"kind": "message_only", "draft": "x"},
    )
    approval_id = approval.id

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    rows = await db.execute(select(AuditEvent))
    events = list(rows.scalars().all())

    expiries = [
        e
        for e in events
        if e.event_type == "approval_expired"
        and e.detail.get("approval_id") == str(approval_id)
    ]
    assert len(expiries) == 1
    assert expiries[0].detail["reason"] == "thread_deleted"
    assert expiries[0].detail["thread_id"] == str(thread.id)
    assert expiries[0].actor_user_id == user_a.id

    summaries = [
        e
        for e in events
        if e.event_type == "thread_deleted" and e.detail.get("thread_id") == str(thread.id)
    ]
    assert len(summaries) == 1
    assert summaries[0].detail["expired_approval_ids"] == [str(approval_id)]


async def test_a_decided_approval_keeps_its_status_when_the_thread_goes(
    authed, db, user_a, bot_a, make_thread, make_run, make_approval
):
    """Only `pending` is affected - a decision already taken is history."""
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    approved = await make_approval(bot_a, run=run, status="approved", requested_by=user_a)
    rejected = await make_approval(bot_a, run=run, status="rejected", requested_by=user_a)

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    assert (await db.get(Approval, approved.id)).status == "approved"
    assert (await db.get(Approval, rejected.id)).status == "rejected"


async def test_deleting_a_thread_leaves_unrelated_approvals_alone(
    authed, db, user_a, bot_a, make_thread, make_run, make_approval
):
    thread = await make_thread(user_a, [bot_a])
    other_thread = await make_thread(user_a, [bot_a])
    other_run = await make_run(other_thread, bot_a)
    untouched = await make_approval(bot_a, run=other_run, requested_by=user_a)
    runless = await make_approval(bot_a, requested_by=user_a)

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    assert (await db.get(Approval, untouched.id)).status == "pending"
    assert (await db.get(Approval, runless.id)).status == "pending"


# ---------------------------------------------------------------------------
# Item 5: scoping must survive the thread that resolved it
# ---------------------------------------------------------------------------


async def test_the_thread_owner_branch_degrades_to_the_stamped_requested_by(
    db, user_a, user_b, bot_b, authed, make_thread, make_run
):
    """`resolve_approval_owner` resolves through the thread behind `run_id`.

    Once that thread is gone the branch cannot fire, so the answer has to come
    from the stamped `requested_by` - not from a fallback that would hand the
    approval to somebody else. The bot here belongs to B while the thread
    belongs to A, so the two candidate answers are distinguishable.
    """
    thread = await make_thread(user_a, [bot_b])
    run = await make_run(thread, bot_b)
    approval = await create_gated_approval(
        db,
        bot_id=bot_b.id,
        run_id=run.id,
        risk="send",
        title="held",
        summary="s",
        payload={"kind": "message_only", "draft": "x"},
    )
    assert approval.payload[REQUESTED_BY_KEY] == str(user_a.id)
    assert await approval_owner(db, approval) == user_a.id

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    # The branch really is dead: the run is there, its thread is not.
    run_row = await db.get(Run, run.id)
    assert run_row is not None and run_row.thread_id is None
    assert await resolve_approval_owner(db, run_id=run.id, payload={}, bot=bot_b) == user_b.id

    survivor = await db.get(Approval, approval.id)
    assert survivor.payload[REQUESTED_BY_KEY] == str(user_a.id)
    assert await approval_owner(db, survivor) == user_a.id, (
        "the stamped requester, not the bot owner, still owns this approval"
    )


async def test_a_system_bot_approval_is_not_left_unownable_by_a_thread_delete(
    authed, other, db, user_a, system_bot, make_thread, make_run, make_approval
):
    """The dangerous case: a system bot is shared, so an approval with no
    knowable owner falls back to bot visibility and becomes readable by anyone.

    An approval written without `requested_by` (not everything goes through
    `create_gated_approval`) is owned only via its thread. Deleting the thread
    must not turn it into a public row - the owner is stamped on the way out.
    """
    thread = await make_thread(user_a, [system_bot])
    run = await make_run(thread, system_bot)
    approval = await make_approval(system_bot, run=run, payload={"kind": "message_only"})
    assert approval.payload.get(REQUESTED_BY_KEY) is None
    assert await approval_owner(db, approval) == user_a.id
    assert (await other.get(f"/api/approvals/{approval.id}")).status_code == 404

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()

    survivor = await db.get(Approval, approval.id)
    assert survivor is not None
    assert survivor.payload[REQUESTED_BY_KEY] == str(user_a.id)
    assert await approval_owner(db, survivor) == user_a.id

    assert (await authed.get(f"/api/approvals/{approval.id}")).status_code == 200
    assert (await other.get(f"/api/approvals/{approval.id}")).status_code == 404, (
        "a deleted thread must never widen who can see an approval"
    )
    assert str(approval.id) not in {
        a["id"] for a in (await other.get("/api/approvals?status=all")).json()
    }


async def test_an_expired_approval_from_a_deleted_thread_cannot_be_decided(
    authed, db, user_a, bot_a, make_thread, make_run, make_approval
):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    approval = await make_approval(bot_a, run=run, requested_by=user_a)

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200

    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "approval_not_pending"


async def test_an_agent_filing_on_someone_elses_behalf_is_recorded_as_created_by(
    db, make_user, make_bot, make_thread, make_run
):
    human = await make_user()
    worker = await make_user(display_name="Worker service identity")
    bot = await make_bot(human)
    thread = await make_thread(human, [bot])
    run = await make_run(thread, bot)

    approval = await create_gated_approval(
        db,
        bot_id=bot.id,
        run_id=run.id,
        risk="send",
        title="t",
        summary="s",
        payload={"kind": "message_only"},
        actor=worker,
    )
    assert approval.payload[REQUESTED_BY_KEY] == str(human.id)
    assert approval.payload[CREATED_BY_KEY] == str(worker.id)


async def test_the_actor_is_not_recorded_when_it_is_the_owner(db, make_user, make_bot):
    human = await make_user()
    bot = await make_bot(human)
    approval = await create_gated_approval(
        db,
        bot_id=bot.id,
        risk="send",
        title="t",
        summary="s",
        payload={"kind": "message_only"},
        actor=human,
    )
    assert CREATED_BY_KEY not in approval.payload


# ---------------------------------------------------------------------------
# created_by is read-only
# ---------------------------------------------------------------------------


@pytest.fixture
async def worker_client(app, make_user):
    """A second identity that filed an approval but does not own it."""
    worker = await make_user(display_name="Worker")
    async with _client_for(app, auth_headers(worker)) as client:
        client.worker = worker  # type: ignore[attr-defined]
        yield client


async def test_created_by_may_read_the_approval(worker_client, make_approval, system_bot, user_a):
    approval = await make_approval(
        system_bot,
        requested_by=user_a,
        payload={"kind": "message_only", "created_by": str(worker_client.worker.id)},
    )
    response = await worker_client.get(f"/api/approvals/{approval.id}")
    assert response.status_code == 200, "the filing agent must be able to poll for the outcome"
    assert response.json()["id"] == str(approval.id)


async def test_created_by_sees_the_approval_in_the_list(worker_client, make_approval, system_bot, user_a):
    approval = await make_approval(
        system_bot,
        requested_by=user_a,
        payload={"kind": "message_only", "created_by": str(worker_client.worker.id)},
    )
    ids = {a["id"] for a in (await worker_client.get("/api/approvals")).json()}
    assert str(approval.id) in ids


async def test_created_by_may_not_decide(worker_client, db, make_approval, system_bot, user_a):
    approval = await make_approval(
        system_bot,
        risk="send",
        requested_by=user_a,
        payload={
            "kind": "connector_action",
            "connector_id": "microsoft_graph",
            "action": "send_mail",
            "input": {"to": "a@b.c", "subject": "s", "body": "b"},
            "created_by": str(worker_client.worker.id),
        },
    )
    response = await worker_client.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )
    assert response.status_code == 404, "read access must never imply decide access"

    await db.refresh(approval)
    assert approval.status == "pending"
    assert not approval.execution


async def test_created_by_may_not_expire(worker_client, db, make_approval, system_bot, user_a):
    approval = await make_approval(
        system_bot,
        requested_by=user_a,
        payload={"kind": "message_only", "created_by": str(worker_client.worker.id)},
    )
    response = await worker_client.post(f"/api/approvals/{approval.id}/expire")
    assert response.status_code == 404

    await db.refresh(approval)
    assert approval.status == "pending"


async def test_a_third_party_can_neither_read_nor_decide(other, make_approval, system_bot, user_a, db):
    worker_id = uuid.uuid4()
    approval = await make_approval(
        system_bot,
        requested_by=user_a,
        payload={"kind": "message_only", "created_by": str(worker_id)},
    )
    assert (await other.get(f"/api/approvals/{approval.id}")).status_code == 404
    assert (
        await other.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    ).status_code == 404


# ---------------------------------------------------------------------------
# The unattended case
# ---------------------------------------------------------------------------


async def test_an_unowned_system_bot_approval_falls_back_to_bot_visibility(
    authed, other, make_approval, system_bot
):
    """A cron-triggered routine step has no knowable human; document that this
    is the one case that stays open to anyone who can see the bot."""
    approval = await make_approval(system_bot, payload={"kind": "message_only"})
    assert approval.payload.get(REQUESTED_BY_KEY) is None

    for client in (authed, other):
        assert (await client.get(f"/api/approvals/{approval.id}")).status_code == 200


async def test_an_unowned_custom_bot_approval_is_still_scoped_to_the_bot_owner(
    authed, other, make_approval, bot_a
):
    """A custom bot always has an owner, so there is no unattended hole there."""
    approval = await make_approval(bot_a, payload={"kind": "message_only"})
    assert (await authed.get(f"/api/approvals/{approval.id}")).status_code == 200
    assert (await other.get(f"/api/approvals/{approval.id}")).status_code == 404


# ---------------------------------------------------------------------------
# The list query mirrors the single-object check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_filter", ["pending", "all"])
async def test_the_list_query_and_the_single_lookup_agree(
    authed, other, db, make_approval, make_thread, make_run, system_bot, bot_a, user_a, user_b, status_filter
):
    thread = await make_thread(user_a, [system_bot])
    run = await make_run(thread, system_bot)
    candidates = [
        await make_approval(system_bot, requested_by=user_a),
        await make_approval(system_bot, requested_by=user_b),
        await make_approval(system_bot, run=run),
        await make_approval(system_bot, payload={"kind": "message_only"}),
        await make_approval(bot_a),
    ]

    for client in (authed, other):
        listed = {
            a["id"] for a in (await client.get(f"/api/approvals?status={status_filter}")).json()
        }
        for approval in candidates:
            readable = (await client.get(f"/api/approvals/{approval.id}")).status_code == 200
            assert (str(approval.id) in listed) is readable, (
                f"approval {approval.id} is {'listed' if str(approval.id) in listed else 'hidden'} "
                f"but individually {'readable' if readable else 'not readable'}"
            )


async def test_no_approval_leaks_into_a_stranger_list(other, db, make_approval, system_bot, user_a):
    await make_approval(system_bot, requested_by=user_a, title="A's secret send")
    listed = (await other.get("/api/approvals?status=all")).json()
    assert all("A's secret send" != a["title"] for a in listed)

    total = await db.execute(select(Approval))
    assert len(total.scalars().all()) >= 1, "the row exists; it is simply not visible to B"
