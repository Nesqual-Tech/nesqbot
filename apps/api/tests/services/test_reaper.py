"""Orphaned runs are reclaimed; parked runs and live runs are not.

The failure this fixes was found in the live database: five runs sitting in
`running`, the oldest more than a day, because the API container restarts on
every deploy and nothing reconciles the row the dead asyncio task left behind.

The dangerous mistake here would be reaping too eagerly, so most of these tests
are about what the reaper must *not* touch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.models import AuditEvent, Run
from app.services.reaper import STALE_AFTER, reap_orphaned_runs

pytestmark = pytest.mark.anyio


async def _age(db, run, delta: timedelta):
    """Backdate the row's heartbeat without tripping `onupdate`."""
    await db.execute(
        update(Run)
        .where(Run.id == run.id)
        .values(updated_at=datetime.now(timezone.utc) - delta)
    )
    await db.commit()
    await db.refresh(run)


async def test_a_long_dead_running_run_is_reclaimed(db, user_a, bot_a, make_thread, make_run):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a, status="running")
    await _age(db, run, STALE_AFTER * 2)

    claimed = await reap_orphaned_runs(db)

    assert str(run.id) in claimed
    await db.refresh(run)
    assert run.status == "interrupted"
    assert run.finished_at is not None
    assert "deploy or a restart" in (run.error or ""), "the reason must be readable"


async def test_a_run_that_is_still_beating_is_left_alone(
    db, user_a, bot_a, make_thread, make_run
):
    """The one that matters with several replicas: do not kill live work."""
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a, status="running")
    await _age(db, run, timedelta(minutes=2))

    assert await reap_orphaned_runs(db) == []
    await db.refresh(run)
    assert run.status == "running"


@pytest.mark.parametrize("parked", ["awaiting_human", "awaiting_approval"])
async def test_a_run_parked_on_a_person_is_never_reaped(
    db, user_a, bot_a, make_thread, make_run, parked
):
    """A person is allowed to take a week. Reaping this turns lunch into a lost task."""
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a, status=parked)
    await _age(db, run, timedelta(days=7))

    assert await reap_orphaned_runs(db) == []
    await db.refresh(run)
    assert run.status == parked


@pytest.mark.parametrize("finished", ["completed", "failed", "cancelled"])
async def test_a_finished_run_is_not_rewritten(
    db, user_a, bot_a, make_thread, make_run, finished
):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a, status=finished)
    await _age(db, run, timedelta(days=3))

    assert await reap_orphaned_runs(db) == []
    await db.refresh(run)
    assert run.status == finished


async def test_running_it_twice_claims_each_run_once(
    db, user_a, bot_a, make_thread, make_run
):
    """Two replicas booting together must not both write an audit event."""
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a, status="running")
    await _age(db, run, STALE_AFTER * 2)

    first = await reap_orphaned_runs(db)
    second = await reap_orphaned_runs(db)

    assert first == [str(run.id)]
    assert second == []

    rows = await db.execute(
        select(AuditEvent).where(AuditEvent.event_type == "run_interrupted")
    )
    events = [e for e in rows.scalars().all() if e.detail.get("run_id") == str(run.id)]
    assert len(events) == 1, "the same run was reaped twice"


async def test_it_says_which_state_the_run_was_in(db, user_a, bot_a, make_thread, make_run):
    """"It was queued" and "it was mid-step" are different diagnoses."""
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a, status="queued")
    await _age(db, run, STALE_AFTER * 2)

    await reap_orphaned_runs(db)

    rows = await db.execute(
        select(AuditEvent).where(AuditEvent.event_type == "run_interrupted")
    )
    event = next(e for e in rows.scalars().all() if e.detail.get("run_id") == str(run.id))
    assert event.detail["from_status"] == "queued"
    assert event.actor_user_id is None, "nobody decided this; it must not name a person"
