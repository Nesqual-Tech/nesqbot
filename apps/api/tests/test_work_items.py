"""Work items: the entity, its ownership transfer, and the ledger behind it.

Three things this module is really checking, in order of how much they matter:

1. **A handover is always recorded.** Every path that changes
   ``work_items.owner_bot_id`` writes a ``work_item_transfers`` row, and no path
   that does not write one can change it. That ledger is the product claim (see
   ``docs/architecture.md``), so a hole in it is not a cosmetic bug.
2. **Owner scoping refuses another user, with 404 and not 403.** Same rule as
   the rest of the API — a 403 confirms the id exists.
3. **The schema migration is genuinely re-runnable.** ``sql/init.sql`` executes
   on every boot; the last test in this file runs it again against a database
   that already has these tables, with data in them.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.models import AuditEvent, Bot, WorkItem, WorkItemKey, WorkItemTransfer
from app.schemas import WorkItemStatus
from app.services import work_items as service


def _is_404(response) -> None:
    assert response.status_code == 404, (
        f"{response.request.method} {response.request.url.path} answered "
        f"{response.status_code}; cross-tenant access must be 404, never 403"
    )
    assert response.json()["code"] == "work_item_not_found", response.json()


@pytest_asyncio.fixture
async def second_bot(make_bot, user_a):
    """A second bot user A can see, to hand things to."""
    return await make_bot(user_a, name="A's second bot")


async def _create(authed, bot, **overrides) -> dict:
    body = {"owner_bot_id": str(bot.id), "title": "Sarah at Acme", "reason": "outbound sweep"}
    body.update(overrides)
    response = await authed.post("/api/work-items", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_the_status_literal_and_the_service_tuple_cannot_drift():
    """``get_args`` returns ``()`` for a non-Literal, so this is not vacuous."""
    from typing import get_args

    assert service.WORK_ITEM_STATUSES == tuple(get_args(WorkItemStatus))
    assert service.WORK_ITEM_STATUSES == ("open", "working", "waiting", "closed")
    assert service.TERMINAL_STATUSES <= set(service.WORK_ITEM_STATUSES)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_creating_a_work_item_returns_it_owned_by_the_named_bot(authed, bot_a, user_a):
    item = await _create(authed, bot_a, type="lead", summary="Replied to the March sequence")
    assert item["owner_bot_id"] == str(bot_a.id)
    assert item["owner_user_id"] == str(user_a.id)
    assert item["type"] == "lead"
    assert item["status"] == "open"
    assert item["closed_at"] is None
    # Never handed over, so there is nothing for `transferred_at` to record.
    assert item["transferred_at"] is None
    assert item["last_event_at"] is None


async def test_creation_writes_the_opening_ledger_row_with_no_predecessor(authed, bot_a, db):
    """The first holder is in the ledger, which is what makes it complete."""
    item = await _create(authed, bot_a, reason="picked up from the inbound form")
    rows = (
        await db.execute(
            select(WorkItemTransfer).where(WorkItemTransfer.work_item_id == uuid.UUID(item["id"]))
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.from_bot_id is None
    assert row.to_bot_id == bot_a.id
    assert row.reason == "picked up from the inbound form"
    assert row.source == service.SOURCE_CREATE


async def test_creation_normalises_and_deduplicates_the_external_keys(authed, bot_a):
    item = await _create(
        authed,
        bot_a,
        keys=[
            {"channel": "Email", "value": "  Sarah@Acme.test "},
            {"channel": "email", "value": "sarah@acme.test"},
            {"channel": "linkedin", "value": "https://LinkedIn.test/in/Sarah"},
        ],
    )
    assert item["keys"] == [
        {"channel": "email", "value": "sarah@acme.test"},
        {"channel": "linkedin", "value": "https://linkedin.test/in/sarah"},
    ]


async def test_creating_against_an_invisible_bot_is_404(authed, bot_b):
    response = await authed.post(
        "/api/work-items",
        json={"owner_bot_id": str(bot_b.id), "title": "Nope", "reason": "x"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


async def test_creating_against_another_users_thread_is_404(authed, bot_a, make_thread, user_b):
    thread = await make_thread(user_b, [])
    response = await authed.post(
        "/api/work-items",
        json={
            "owner_bot_id": str(bot_a.id),
            "title": "Pinned to someone else's conversation",
            "reason": "x",
            "thread_id": str(thread.id),
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "thread_not_found"


async def test_creation_writes_an_audit_event_without_the_free_form_detail(authed, bot_a, db):
    item = await _create(authed, bot_a, detail={"api_key": "sk-should-never-be-audited"})
    events = (
        await db.execute(select(AuditEvent).where(AuditEvent.event_type == "work_item_created"))
    ).scalars().all()
    match = [e for e in events if e.detail.get("work_item_id") == item["id"]]
    assert len(match) == 1
    assert "sk-should-never-be-audited" not in str(match[0].detail)


# ---------------------------------------------------------------------------
# Read / list
# ---------------------------------------------------------------------------


async def test_the_list_is_scoped_to_the_calling_user(authed, other, bot_a):
    item = await _create(authed, bot_a)
    mine = await authed.get("/api/work-items")
    assert mine.status_code == 200
    assert item["id"] in [row["id"] for row in mine.json()]

    theirs = await other.get("/api/work-items")
    assert theirs.status_code == 200
    assert item["id"] not in [row["id"] for row in theirs.json()]


async def test_the_list_filters_by_type_status_and_owning_bot(authed, bot_a, second_bot):
    lead = await _create(authed, bot_a, type="lead", title="A lead")
    ticket = await _create(authed, second_bot, type="ticket", title="A ticket", status="working")

    by_type = await authed.get("/api/work-items", params={"type": "ticket"})
    assert [row["id"] for row in by_type.json()] == [ticket["id"]]

    by_status = await authed.get("/api/work-items", params={"status": "open"})
    assert lead["id"] in [row["id"] for row in by_status.json()]
    assert ticket["id"] not in [row["id"] for row in by_status.json()]

    by_bot = await authed.get("/api/work-items", params={"owner_bot_id": str(second_bot.id)})
    assert [row["id"] for row in by_bot.json()] == [ticket["id"]]


async def test_a_single_work_item_reads_back_with_its_keys(authed, bot_a):
    item = await _create(authed, bot_a, keys=[{"channel": "email", "value": "sarah@acme.test"}])
    response = await authed.get(f"/api/work-items/{item['id']}")
    assert response.status_code == 200
    assert response.json()["keys"] == [{"channel": "email", "value": "sarah@acme.test"}]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_closing_stamps_closed_at_and_reopening_clears_it(authed, bot_a):
    item = await _create(authed, bot_a)
    closed = await authed.patch(
        f"/api/work-items/{item['id']}", json={"status": "closed", "resolution": "won"}
    )
    assert closed.status_code == 200
    assert closed.json()["closed_at"] is not None
    assert closed.json()["resolution"] == "won"

    reopened = await authed.patch(f"/api/work-items/{item['id']}", json={"status": "working"})
    assert reopened.json()["closed_at"] is None


async def test_patch_refuses_an_attempt_to_move_ownership(authed, bot_a, second_bot, db):
    """The one hole that would make the ledger a lie — and it is *refused*, not ignored.

    Pydantic's default is to drop unknown keys, which would have made this a 200
    with the old owner still in the body: the caller reads a successful handover
    and there is no ledger row, because nothing happened. `UpdateWorkItemIn`
    forbids extras and names this field specifically for that reason.
    """
    item = await _create(authed, bot_a)
    response = await authed.patch(
        f"/api/work-items/{item['id']}",
        json={"owner_bot_id": str(second_bot.id), "title": "Still A's bot's"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    assert "/work-items/{work_item_id}/transfer" in str(response.json()["errors"])

    # Nothing moved, and — the part that matters — the title in the same body
    # did not land either. A partial application would be its own silent lie.
    unchanged = (await authed.get(f"/api/work-items/{item['id']}")).json()
    assert unchanged["owner_bot_id"] == str(bot_a.id)
    assert unchanged["title"] == "Sarah at Acme"
    rows = (
        await db.execute(
            select(WorkItemTransfer).where(WorkItemTransfer.work_item_id == uuid.UUID(item["id"]))
        )
    ).scalars().all()
    assert len(rows) == 1, "a PATCH must not be able to add a ledger row"


async def test_patch_refuses_an_explicit_null_owner_too(authed, bot_a):
    """Keyed on presence, not truthiness: `null` is still a claim about ownership."""
    item = await _create(authed, bot_a)
    response = await authed.patch(f"/api/work-items/{item['id']}", json={"owner_bot_id": None})
    assert response.status_code == 422
    assert "/work-items/{work_item_id}/transfer" in str(response.json()["errors"])


async def test_patch_refuses_any_unknown_field(authed, bot_a):
    """Proves the refusal is a model rule and not a special case for one key."""
    item = await _create(authed, bot_a)
    response = await authed.patch(f"/api/work-items/{item['id']}", json={"owner_user_id": str(uuid.uuid4())})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_patching_keys_replaces_the_whole_set(authed, bot_a):
    item = await _create(authed, bot_a, keys=[{"channel": "email", "value": "old@acme.test"}])
    response = await authed.patch(
        f"/api/work-items/{item['id']}",
        json={"keys": [{"channel": "email", "value": "new@acme.test"}]},
    )
    assert response.json()["keys"] == [{"channel": "email", "value": "new@acme.test"}]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_deleting_a_work_item_leaves_the_transfer_ledger_intact(authed, bot_a, db):
    """Otherwise DELETE is the way to erase the audit trail."""
    item = await _create(authed, bot_a)
    item_id = uuid.UUID(item["id"])
    response = await authed.delete(f"/api/work-items/{item['id']}")
    assert response.status_code == 200

    assert await db.get(WorkItem, item_id) is None
    keys = (
        await db.execute(select(WorkItemKey).where(WorkItemKey.work_item_id == item_id))
    ).scalars().all()
    assert keys == []
    ledger = (
        await db.execute(select(WorkItemTransfer).where(WorkItemTransfer.work_item_id == item_id))
    ).scalars().all()
    assert len(ledger) == 1


# ---------------------------------------------------------------------------
# Transfer — the point of the entity
# ---------------------------------------------------------------------------


async def test_a_transfer_moves_the_bot_and_records_who_to_whom_when_and_why(
    authed, bot_a, second_bot, user_a, db
):
    item = await _create(authed, bot_a)
    response = await authed.post(
        f"/api/work-items/{item['id']}/transfer",
        json={
            "to_bot_id": str(second_bot.id),
            "reason": "She answered; Sales closes from here",
            "actor_bot_id": str(bot_a.id),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transferred"] is True
    assert body["work_item"]["owner_bot_id"] == str(second_bot.id)
    assert body["work_item"]["transferred_at"] is not None

    transfer = body["transfer"]
    assert transfer["from_bot_id"] == str(bot_a.id)
    assert transfer["to_bot_id"] == str(second_bot.id)
    assert transfer["actor_user_id"] == str(user_a.id)
    assert transfer["actor_bot_id"] == str(bot_a.id)
    assert transfer["reason"] == "She answered; Sales closes from here"
    assert transfer["source"] == "api"

    ledger = await authed.get(f"/api/work-items/{item['id']}/transfers")
    assert [row["to_bot_id"] for row in ledger.json()] == [str(second_bot.id), str(bot_a.id)]

    events = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "work_item_transferred")
        )
    ).scalars().all()
    assert [e for e in events if e.detail.get("work_item_id") == item["id"]]


async def test_transferring_to_the_current_owner_is_a_no_op_and_writes_no_second_row(
    authed, bot_a, db
):
    """A model calling the tool twice must not mint a handover that did not happen."""
    item = await _create(authed, bot_a)
    response = await authed.post(
        f"/api/work-items/{item['id']}/transfer",
        json={"to_bot_id": str(bot_a.id), "reason": "retry"},
    )
    assert response.status_code == 200
    assert response.json()["transferred"] is False
    assert response.json()["transfer"] is None
    rows = (
        await db.execute(
            select(WorkItemTransfer).where(WorkItemTransfer.work_item_id == uuid.UUID(item["id"]))
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_a_repeated_transfer_to_a_new_bot_is_idempotent_after_the_first(
    authed, bot_a, second_bot, db
):
    item = await _create(authed, bot_a)
    body = {"to_bot_id": str(second_bot.id), "reason": "hand to sales"}
    first = await authed.post(f"/api/work-items/{item['id']}/transfer", json=body)
    second = await authed.post(f"/api/work-items/{item['id']}/transfer", json=body)
    assert first.json()["transferred"] is True
    assert second.json()["transferred"] is False
    rows = (
        await db.execute(
            select(WorkItemTransfer).where(WorkItemTransfer.work_item_id == uuid.UUID(item["id"]))
        )
    ).scalars().all()
    assert len(rows) == 2, "opening row plus exactly one handover"


async def test_a_transfer_needs_a_reason(authed, bot_a, second_bot, db):
    """The reason is the artefact a reviewer reads; an empty one is refused."""
    item = await _create(authed, bot_a)
    response = await authed.post(
        f"/api/work-items/{item['id']}/transfer",
        json={"to_bot_id": str(second_bot.id), "reason": ""},
    )
    assert response.status_code == 422
    fresh = await db.get(WorkItem, uuid.UUID(item["id"]))
    await db.refresh(fresh)
    assert fresh.owner_bot_id == bot_a.id


async def test_transferring_a_closed_work_item_is_409(authed, bot_a, second_bot):
    item = await _create(authed, bot_a, status="closed")
    response = await authed.post(
        f"/api/work-items/{item['id']}/transfer",
        json={"to_bot_id": str(second_bot.id), "reason": "too late"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "work_item_closed"


async def test_transferring_to_a_bot_the_caller_cannot_see_is_404(authed, bot_a, bot_b):
    item = await _create(authed, bot_a)
    response = await authed.post(
        f"/api/work-items/{item['id']}/transfer",
        json={"to_bot_id": str(bot_b.id), "reason": "leak probe"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


async def test_naming_an_invisible_initiating_bot_is_404(authed, bot_a, second_bot, bot_b):
    """The ledger must not become a way to confirm which bot ids exist."""
    item = await _create(authed, bot_a)
    response = await authed.post(
        f"/api/work-items/{item['id']}/transfer",
        json={
            "to_bot_id": str(second_bot.id),
            "reason": "probe",
            "actor_bot_id": str(bot_b.id),
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


# ---------------------------------------------------------------------------
# Negative controls: another user is refused, with 404 and never 403
# ---------------------------------------------------------------------------


async def test_a_second_user_cannot_read_another_users_work_item(other, authed, bot_a):
    item = await _create(authed, bot_a)
    _is_404(await other.get(f"/api/work-items/{item['id']}"))


async def test_a_second_user_cannot_patch_another_users_work_item(other, authed, bot_a):
    item = await _create(authed, bot_a)
    _is_404(await other.patch(f"/api/work-items/{item['id']}", json={"title": "pwned"}))


async def test_a_second_user_cannot_delete_another_users_work_item(other, authed, bot_a, db):
    item = await _create(authed, bot_a)
    _is_404(await other.delete(f"/api/work-items/{item['id']}"))
    assert await db.get(WorkItem, uuid.UUID(item["id"])) is not None


async def test_a_second_user_cannot_transfer_another_users_work_item(
    other, authed, bot_a, bot_b, db
):
    """The one that would actually hurt: stealing a lead into your own bot."""
    item = await _create(authed, bot_a)
    _is_404(
        await other.post(
            f"/api/work-items/{item['id']}/transfer",
            json={"to_bot_id": str(bot_b.id), "reason": "mine now"},
        )
    )
    fresh = await db.get(WorkItem, uuid.UUID(item["id"]))
    await db.refresh(fresh)
    assert fresh.owner_bot_id == bot_a.id
    rows = (
        await db.execute(
            select(WorkItemTransfer).where(WorkItemTransfer.work_item_id == fresh.id)
        )
    ).scalars().all()
    assert len(rows) == 1, "a refused transfer must leave no trace in the ledger"


async def test_a_second_user_cannot_read_another_users_transfer_ledger(other, authed, bot_a):
    item = await _create(authed, bot_a)
    _is_404(await other.get(f"/api/work-items/{item['id']}/transfers"))


async def test_a_missing_work_item_is_the_same_404_as_someone_elses(other, authed, bot_a):
    """Indistinguishable answers, which is the whole point of 404 over 403."""
    item = await _create(authed, bot_a)
    theirs = await other.get(f"/api/work-items/{item['id']}")
    absent = await other.get(f"/api/work-items/{uuid.uuid4()}")
    assert theirs.status_code == absent.status_code == 404
    assert theirs.json() == absent.json()


# ---------------------------------------------------------------------------
# The seam left for the inbound-events lane
# ---------------------------------------------------------------------------


async def test_an_inbound_identity_resolves_to_its_work_item(authed, bot_a, db, user_a):
    await _create(authed, bot_a, title="Other lead", keys=[{"channel": "email", "value": "x@y.test"}])
    item = await _create(
        authed, bot_a, title="Sarah", keys=[{"channel": "email", "value": "sarah@acme.test"}]
    )
    # A webhook shouting the address back in a different case still resolves.
    hits = await service.resolve_by_key(db, "EMAIL", " Sarah@Acme.test ")
    assert [str(hit.id) for hit in hits] == [item["id"]]

    scoped = await service.resolve_by_key(db, "email", "sarah@acme.test", owner_user_id=user_a.id)
    assert [str(hit.id) for hit in scoped] == [item["id"]]
    assert await service.resolve_by_key(db, "email", "sarah@acme.test", owner_user_id=uuid.uuid4()) == []


async def test_a_closed_work_item_is_out_of_the_way_unless_asked_for(authed, bot_a, db):
    item = await _create(authed, bot_a, keys=[{"channel": "email", "value": "sarah@acme.test"}])
    await authed.patch(f"/api/work-items/{item['id']}", json={"status": "closed"})
    assert await service.resolve_by_key(db, "email", "sarah@acme.test") == []
    reopened = await service.resolve_by_key(
        db, "email", "sarah@acme.test", include_closed=True
    )
    assert [str(hit.id) for hit in reopened] == [item["id"]]


async def test_two_work_items_on_one_address_come_back_ordered_not_refused(
    authed, bot_a, db, user_a
):
    """A unique index here would throw a real customer reply away at the webhook."""
    stale = await _create(
        authed, bot_a, title="March", keys=[{"channel": "email", "value": "sarah@acme.test"}]
    )
    fresh = await _create(
        authed, bot_a, title="August", keys=[{"channel": "email", "value": "sarah@acme.test"}]
    )
    stale_row = await db.get(WorkItem, uuid.UUID(stale["id"]))
    await service.mark_inbound_event(db, stale_row)
    await db.commit()

    hits = await service.resolve_by_key(db, "email", "sarah@acme.test", owner_user_id=user_a.id)
    ids = [str(hit.id) for hit in hits]
    assert set(ids) == {stale["id"], fresh["id"]}
    # Most recent contact from the outside wins the tie-break, not creation order.
    assert ids[0] == stale["id"]


async def test_marking_an_inbound_event_moves_only_last_event_at(authed, bot_a, db):
    item = await _create(authed, bot_a)
    row = await db.get(WorkItem, uuid.UUID(item["id"]))
    before_status = row.status
    await service.mark_inbound_event(db, row)
    await db.commit()
    await db.refresh(row)
    assert row.last_event_at is not None
    assert row.status == before_status, "deciding what a reply means is the inbound lane's call"


# ---------------------------------------------------------------------------
# The FK decisions, exercised
# ---------------------------------------------------------------------------


async def test_deleting_the_owning_bot_orphans_the_item_rather_than_the_pipeline(
    authed, make_bot, user_a, db
):
    """``ON DELETE SET NULL``: losing the bot must not lose the customer's leads."""
    doomed = await make_bot(user_a, name="Soon to be deleted")
    item = await _create(authed, doomed)
    response = await authed.delete(f"/api/bots/{doomed.id}")
    assert response.status_code == 200, response.text

    row = await db.get(WorkItem, uuid.UUID(item["id"]))
    await db.refresh(row)
    assert row is not None
    assert row.owner_bot_id is None
    assert await db.get(Bot, doomed.id) is None
    # And the ledger still names the bot that used to hold it.
    ledger = (
        await db.execute(select(WorkItemTransfer).where(WorkItemTransfer.work_item_id == row.id))
    ).scalars().all()
    assert [t.to_bot_id for t in ledger] == [doomed.id]


async def test_an_orphaned_work_item_can_be_re_homed(authed, make_bot, user_a, second_bot, db):
    doomed = await make_bot(user_a, name="Soon to be deleted")
    item = await _create(authed, doomed)
    await authed.delete(f"/api/bots/{doomed.id}")
    response = await authed.post(
        f"/api/work-items/{item['id']}/transfer",
        json={"to_bot_id": str(second_bot.id), "reason": "its bot was deleted"},
    )
    assert response.status_code == 200
    assert response.json()["transferred"] is True
    assert response.json()["transfer"]["from_bot_id"] is None


# ---------------------------------------------------------------------------
# Negative control: the migration really is re-runnable
# ---------------------------------------------------------------------------


async def _snapshot(engine) -> dict[str, list[tuple]]:
    """Columns and indexes of the three tables, as Postgres reports them."""
    tables = ("work_items", "work_item_keys", "work_item_transfers")
    async with engine.connect() as conn:
        columns = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = ANY(:tables) "
                    "ORDER BY table_name, column_name"
                ),
                {"tables": list(tables)},
            )
        ).all()
        indexes = (
            await conn.execute(
                text(
                    "SELECT tablename, indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND tablename = ANY(:tables) "
                    "ORDER BY tablename, indexdef"
                ),
                {"tables": list(tables)},
            )
        ).all()
    return {"columns": [tuple(r) for r in columns], "indexes": [tuple(r) for r in indexes]}


@pytest.mark.contract
async def test_init_sql_is_idempotent_against_a_database_that_already_has_these_tables(
    database_url, bootstrap, caplog
):
    """``init.sql`` runs on every boot, so "re-runnable" is a hard requirement.

    Deliberately not a re-read of the file: this executes the real script, twice,
    against the live database the rest of the suite is using — which conftest
    already bootstrapped once — with rows in the new tables. Three assertions,
    and the first is the one that would catch a non-idempotent statement:

    * ``ensure_schema`` reports **zero** failed statements. It logs and swallows
      per-statement failures by design, so a second run that quietly errored
      would otherwise look identical to one that did not;
    * the column and index definitions are byte-identical before and after;
    * the canary rows are still there.

    Takes no ``db`` fixture on purpose. ``ensure_schema`` opens its own
    connections and issues ``ALTER TABLE``, which wants an exclusive lock; an
    open test transaction touching the same tables would be a deadlock waiting
    for a slow afternoon.
    """
    import logging

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.services.schema import ensure_schema

    engine = create_async_engine(database_url, poolclass=NullPool)
    user_id, bot_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO users (id, email, display_name) VALUES (:i, :e, 'canary')"),
                {"i": user_id, "e": f"idempotency-{user_id.hex[:8]}@example.test"},
            )
            await conn.execute(
                text(
                    "INSERT INTO bots (id, slug, name, role, system_prompt, owner_user_id) "
                    "VALUES (:i, :s, 'Canary', 'Canary', 'p', :u)"
                ),
                {"i": bot_id, "s": f"canary_{bot_id.hex[:8]}", "u": user_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO work_items (id, title, owner_bot_id, owner_user_id) "
                    "VALUES (:i, 'canary', :b, :u)"
                ),
                {"i": item_id, "b": bot_id, "u": user_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO work_item_keys (work_item_id, channel, value, owner_user_id) "
                    "VALUES (:i, 'email', 'canary@example.test', :u)"
                ),
                {"i": item_id, "u": user_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO work_item_transfers "
                    "(work_item_id, owner_user_id, to_bot_id, reason, source) "
                    "VALUES (:i, :u, :b, 'canary', 'create')"
                ),
                {"i": item_id, "u": user_id, "b": bot_id},
            )

        before = await _snapshot(engine)

        with caplog.at_level(logging.INFO, logger="app.services.schema"):
            await ensure_schema(engine)
            await ensure_schema(engine)

        summaries = [
            record.args
            for record in caplog.records
            if record.msg.startswith("schema bootstrap ran")
        ]
        assert len(summaries) == 2, f"expected two bootstrap runs, saw {summaries}"
        for statements, failures in summaries:
            assert statements > 0
            assert failures == 0, f"{failures} statement(s) failed on a re-run of init.sql"

        assert await _snapshot(engine) == before

        async with engine.connect() as conn:
            counts = (
                await conn.execute(
                    text(
                        "SELECT (SELECT count(*) FROM work_items WHERE id = :i), "
                        "       (SELECT count(*) FROM work_item_keys WHERE work_item_id = :i), "
                        "       (SELECT count(*) FROM work_item_transfers WHERE work_item_id = :i)"
                    ),
                    {"i": item_id},
                )
            ).one()
        assert tuple(counts) == (1, 1, 1), "a re-run of init.sql must not touch data"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM work_item_transfers WHERE work_item_id = :i"), {"i": item_id}
            )
            await conn.execute(text("DELETE FROM work_items WHERE id = :i"), {"i": item_id})
            await conn.execute(text("DELETE FROM bots WHERE id = :i"), {"i": bot_id})
            await conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        await engine.dispose()
