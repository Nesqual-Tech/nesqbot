"""There has to be a way out of a run that will not finish.

Both shapes here were found in the live database, not imagined:

* **5 runs stuck in `running`**, one for over a day. The API container restarts
  on every deploy; the in-process agent loop dies with it and nothing reconciles
  the row, so the UI shows work in progress that will never progress.
* **3 runs parked in `awaiting_approval` with zero pending approvals.** They are
  waiting on something that no longer exists, and `resume` cannot help — those
  runs also carry no `agent_state`, so it answers 409 `run_not_resumable`.

The owner's report was "sometimes it gets blocked and we can't do anything".
Until this route existed, the only way out was editing the database.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def test_a_run_stuck_running_can_be_cancelled(authed, db, bot_a, make_run, make_thread, user_a):
    """The orphaned-by-restart case."""
    run = await make_run(await make_thread(user_a, [bot_a]), bot_a, status="running")

    response = await authed.post(f"/api/runs/{run.id}/cancel", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["cancelled"] is True
    assert body["status"] == "cancelled"

    await db.refresh(run)
    assert run.status == "cancelled"
    assert run.finished_at is not None, "a cancelled run must stop being open-ended"


async def test_a_run_parked_on_an_approval_that_no_longer_exists_can_be_cancelled(authed, db, bot_a, make_run, make_thread, user_a):
    """The deadlock: parked on an approval nobody can decide any more.

    `resume` is no help here — these runs have no `agent_state`, so it refuses
    with `run_not_resumable`, which is correct and also a dead end.
    """
    run = await make_run(await make_thread(user_a, [bot_a]), bot_a, status="awaiting_approval")

    resumed = await authed.post(f"/api/runs/{run.id}/resume", json={})
    assert resumed.status_code == 409, "precondition: resume cannot rescue this run"

    cancelled = await authed.post(
        f"/api/runs/{run.id}/cancel", json={"reason": "approval vanished"}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True

    await db.refresh(run)
    assert run.status == "cancelled"
    assert "approval vanished" in (run.error or "")


async def test_cancelling_a_parked_takeover_releases_it(authed, db, bot_a, make_run, make_thread, user_a):
    """A takeover the person cannot complete must not trap the thread."""
    run = await make_run(await make_thread(user_a, [bot_a]), bot_a, status="awaiting_human")

    response = await authed.post(f"/api/runs/{run.id}/cancel", json={})

    assert response.json()["cancelled"] is True
    await db.refresh(run)
    assert run.status == "cancelled"


async def test_a_second_press_is_not_an_error(authed, bot_a, make_run, make_thread, user_a):
    """Same idempotency contract as resume: a double-click is a double-click."""
    run = await make_run(await make_thread(user_a, [bot_a]), bot_a, status="running")

    first = await authed.post(f"/api/runs/{run.id}/cancel", json={})
    second = await authed.post(f"/api/runs/{run.id}/cancel", json={})

    assert first.json()["cancelled"] is True
    assert second.status_code == 200
    assert second.json()["cancelled"] is False
    assert second.json()["status"] == "cancelled"


async def test_a_finished_run_is_left_alone(authed, db, bot_a, make_run, make_thread, user_a):
    """Cancel must not rewrite history."""
    run = await make_run(await make_thread(user_a, [bot_a]), bot_a, status="completed")

    response = await authed.post(f"/api/runs/{run.id}/cancel", json={})

    assert response.json()["cancelled"] is False
    await db.refresh(run)
    assert run.status == "completed", "cancel overwrote a finished run"


async def test_another_users_run_is_404_never_403(
    authed, other, db, bot_a, make_run, make_thread, user_a
):
    """Same scoping rule as everywhere else: not-yours is indistinguishable from
    does-not-exist, so a run's existence stays private."""
    run = await make_run(await make_thread(user_a, [bot_a]), bot_a, status="running")

    response = await other.post(f"/api/runs/{run.id}/cancel", json={})

    assert response.status_code == 404
    await db.refresh(run)
    assert run.status == "running", "another user cancelled a run they cannot see"


async def test_the_cancellation_is_recorded_in_the_audit(authed, db, bot_a, make_run, make_thread, user_a):
    """Abandoning work is a decision, and decisions are evidence."""
    from sqlalchemy import select

    from app.models import AuditEvent

    run = await make_run(await make_thread(user_a, [bot_a]), bot_a, status="running")
    await authed.post(f"/api/runs/{run.id}/cancel", json={"reason": "stuck on a captcha"})

    rows = await db.execute(select(AuditEvent).where(AuditEvent.event_type == "run_cancelled"))
    events = [e for e in rows.scalars().all() if e.detail.get("run_id") == str(run.id)]
    assert len(events) == 1
    assert events[0].detail.get("reason") == "stuck on a captcha"
    assert events[0].actor_user_id is not None, "the audit must name who abandoned it"
