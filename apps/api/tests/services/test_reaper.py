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


# ---------------------------------------------------------------------------
# Telling the person, not just the database
# ---------------------------------------------------------------------------
#
# Measured on the live database while the owner reported "I messaged and nothing
# happened at all":
#
#     MESSAGES  2026-09-02T18:14:07  user  'Maya, you are the COO of Nesqual…'
#     RUNS      2026-09-02T18:14:07  running     <- no reply, ever
#               2026-09-02T15:43:24  running     <- stuck ~3h
#
# Both true statements about the product: an assistant message is only written
# when a turn *finishes*, so a turn killed by a deploy or stalled mid-step
# leaves the transcript holding the person's own message and nothing else. The
# run row said `running`, the reaper would eventually say `interrupted`, and the
# screen said nothing either way.


async def test_a_reclaimed_run_leaves_a_sentence_in_its_thread(db, make_user, make_bot, make_thread):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.models import Message, Run
    from app.services.reaper import reap_orphaned_runs

    user = await make_user(email="stalled@nesqualtech.test")
    bot = await make_bot(user, name="Chief of Staff")
    thread = await make_thread(user, [bot])
    stale = datetime.now(timezone.utc) - timedelta(hours=3)

    run = Run(thread_id=thread.id, bot_id=bot.id, status="running")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    # Backdate the heartbeat: only age decides, which is what makes this safe
    # with several replicas.
    run.updated_at = stale
    await db.commit()

    claimed = await reap_orphaned_runs(db)

    assert str(run.id) in claimed
    posted = (
        (await db.execute(select(Message).where(Message.thread_id == thread.id))).scalars().all()
    )
    assert len(posted) == 1, "the thread was left with no explanation"
    note = posted[0]
    assert note.role == "assistant"
    assert note.bot_id == bot.id
    assert "stopped mid-task" in note.content
    assert "nothing more is coming" in note.content
    assert "Send the message again" in note.content
    assert note.meta["interrupted_run_id"] == str(run.id)


async def test_a_run_with_no_thread_is_still_reclaimed(db, make_user, make_bot):
    """A routine or an inbound handler has no transcript to apologise in."""
    from datetime import datetime, timedelta, timezone

    from app.models import Run
    from app.services.reaper import reap_orphaned_runs

    user = await make_user(email="routine@nesqualtech.test")
    bot = await make_bot(user, name="Ops")
    run = Run(thread_id=None, bot_id=bot.id, status="running")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    run.updated_at = datetime.now(timezone.utc) - timedelta(hours=3)
    await db.commit()

    assert str(run.id) in await reap_orphaned_runs(db)


async def test_a_live_run_is_neither_reaped_nor_apologised_for(db, make_user, make_bot, make_thread):
    """The asymmetry the reaper's own docstring argues for, asserted.

    Reaping late costs a stale row; reaping early kills work somebody is
    waiting on - and now it would also post a note claiming a live turn is
    dead, which is worse than the stale row by some distance.
    """
    from sqlalchemy import select

    from app.models import Message, Run
    from app.services.reaper import reap_orphaned_runs

    user = await make_user(email="live@nesqualtech.test")
    bot = await make_bot(user, name="Sales")
    thread = await make_thread(user, [bot])
    run = Run(thread_id=thread.id, bot_id=bot.id, status="running")
    db.add(run)
    await db.commit()

    assert await reap_orphaned_runs(db) == []
    assert (
        (await db.execute(select(Message).where(Message.thread_id == thread.id))).scalars().all()
        == []
    )
