"""Usage, evals, run history, and the audit log."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models import AuditEvent, CostLedger

MISSING = uuid.uuid4()


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


async def test_usage_lists_every_visible_bot(authed, bot_a, system_bot):
    response = await authed.get("/api/usage")
    assert response.status_code == 200
    ids = {u["bot_id"] for u in response.json()}
    assert str(bot_a.id) in ids
    assert str(system_bot.id) in ids


async def test_usage_reports_zero_for_an_unused_bot(authed, bot_a):
    row = next(u for u in (await authed.get("/api/usage")).json() if u["bot_id"] == str(bot_a.id))
    assert row["spent_usd_today"] == 0.0
    assert row["budget_usd"] == 5.0
    assert row["entries"] == []


async def test_usage_entries_carry_the_ledger_shape(authed, db, bot_a):
    db.add(
        CostLedger(
            bot_id=bot_a.id, tier="nano", input_tokens=100, output_tokens=50, cost_usd=Decimal("0.0001")
        )
    )
    await db.commit()
    row = next(u for u in (await authed.get("/api/usage")).json() if u["bot_id"] == str(bot_a.id))
    entry = row["entries"][0]
    assert entry["tier"] == "nano"
    assert entry["input_tokens"] == 100
    assert entry["output_tokens"] == 50
    assert entry["cost_usd"] == 0.0001
    assert entry["created_at"]


async def test_usage_days_window_is_validated(authed):
    assert (await authed.get("/api/usage?days=0")).status_code == 422
    assert (await authed.get("/api/usage?days=91")).status_code == 422
    assert (await authed.get("/api/usage?days=90")).status_code == 200


# ---------------------------------------------------------------------------
# Evals
# ---------------------------------------------------------------------------


async def test_run_a_single_eval_case(authed):
    response = await authed.post(
        "/api/evals/run", json={"name": "smoke", "prompt": "say hello", "expect_contains": []}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "smoke"
    assert body["passed"] is True
    assert body["missing"] == []
    assert body["tier"] == "mini"
    assert body["output"]


async def test_an_eval_case_reports_what_was_missing(authed):
    response = await authed.post(
        "/api/evals/run",
        json={"name": "strict", "prompt": "hello", "expect_contains": ["a phrase never emitted"]},
    )
    body = response.json()
    assert body["passed"] is False
    assert body["missing"] == ["a phrase never emitted"]


async def test_expect_contains_is_case_insensitive(authed):
    response = await authed.post(
        "/api/evals/run", json={"name": "case", "prompt": "hi", "expect_contains": ["ACKNOWLEDGED"]}
    )
    assert response.json()["passed"] is True


async def test_run_an_eval_suite(authed):
    response = await authed.post(
        "/api/evals/suite",
        json={
            "cases": [
                {"name": "one", "prompt": "hello", "expect_contains": []},
                {"name": "two", "prompt": "hello", "expect_contains": ["never in the output"]},
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["passed"] == 1
    assert [r["name"] for r in body["results"]] == ["one", "two"]
    assert body["cost_usd"] >= 0


async def test_an_empty_eval_suite_is_valid(authed):
    response = await authed.post("/api/evals/suite", json={"cases": []})
    assert response.status_code == 200
    assert response.json() == {"passed": 0, "total": 0, "results": [], "cost_usd": 0.0}


async def test_eval_run_requires_a_prompt(authed):
    assert (await authed.post("/api/evals/run", json={"name": "x"})).status_code == 422


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def test_list_runs(authed, make_thread, make_run, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    response = await authed.get("/api/runs")
    assert response.status_code == 200
    assert str(run.id) in {r["id"] for r in response.json()}


async def test_list_runs_filtered_by_thread_bot_and_status(
    authed, make_thread, make_run, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    running = await make_run(thread, bot_a, status="running")
    done = await make_run(thread, bot_a, status="completed")

    by_thread = await authed.get(f"/api/runs?thread_id={thread.id}")
    assert {r["id"] for r in by_thread.json()} >= {str(running.id), str(done.id)}

    by_bot = await authed.get(f"/api/runs?bot_id={bot_a.id}")
    assert {r["id"] for r in by_bot.json()} >= {str(running.id)}

    by_status = await authed.get("/api/runs?status=completed")
    ids = {r["id"] for r in by_status.json()}
    assert str(done.id) in ids
    assert str(running.id) not in ids


async def _detached_run(db, bot, *, routine=None, ledger=None, status="completed"):
    """A run with no thread: a routine run, or a chat run whose thread is gone."""
    from app.models import Run

    run = Run(
        thread_id=None,
        routine_id=getattr(routine, "id", routine),
        bot_id=bot.id,
        status=status,
        context_ledger=dict(ledger or {}),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def test_list_runs_includes_a_routine_run_the_caller_owns(authed, db, user_a, system_bot):
    """`Routine.owner_user_id` is the second way a run gets a human."""
    from app.models import Routine

    routine = Routine(
        bot_id=system_bot.id,
        owner_user_id=user_a.id,
        name="Nightly sweep",
        steps=[{"type": "message", "text": "hi"}],
    )
    db.add(routine)
    await db.commit()
    await db.refresh(routine)

    run = await _detached_run(db, system_bot, routine=routine)
    assert (await authed.get(f"/api/runs/{run.id}")).status_code == 200
    assert str(run.id) in {r["id"] for r in (await authed.get("/api/runs")).json()}


async def test_list_runs_keeps_a_threadless_run_on_the_callers_own_bot(authed, db, bot_a):
    """A custom bot has exactly one owner, so its runs stay that owner's."""
    run = await _detached_run(db, bot_a)
    assert (await authed.get(f"/api/runs/{run.id}")).status_code == 200
    assert str(run.id) in {r["id"] for r in (await authed.get("/api/runs")).json()}


async def test_list_runs_excludes_unattributable_runs(authed, db, system_bot):
    """No thread, no routine owner, shared bot: nobody's run, so nobody's list.

    Unattended routine history has a scoped home in `/routines/{id}/runs`;
    `/runs` is the caller's own history and must not become a tenant-wide feed.
    """
    run = await _detached_run(db, system_bot)
    assert str(run.id) not in {r["id"] for r in (await authed.get("/api/runs")).json()}
    assert (await authed.get(f"/api/runs/{run.id}")).status_code == 404


async def test_get_a_run(authed, make_thread, make_run, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    response = await authed.get(f"/api/runs/{run.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(run.id)


async def test_get_a_missing_run_is_404(authed):
    response = await authed.get(f"/api/runs/{MISSING}")
    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


async def test_run_status_callback_marks_a_failure_visible(
    authed, db, make_thread, make_run, make_routine, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    routine = await make_routine(bot_a)

    response = await authed.post(
        f"/api/runs/{run.id}/status",
        json={
            "status": "failed",
            "error": "step 2 timed out",
            "detail": {"failed_step": 2},
            "routine_id": str(routine.id),
            "workflow_id": "routine-abc-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "step 2 timed out"
    assert body["detail"] == {"failed_step": 2}
    assert body["finished_at"], "a terminal status must stamp finished_at"
    assert body["temporal_workflow_id"] == "routine-abc-1"
    assert body["context_ledger"]["routine_id"] == str(routine.id)


async def test_run_status_callback_writes_an_audit_event(
    authed, db, make_thread, make_run, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    await authed.post(f"/api/runs/{run.id}/status", json={"status": "completed"})

    rows = await db.execute(select(AuditEvent).where(AuditEvent.event_type == "run_status"))
    events = rows.scalars().all()
    assert any(e.detail["run_id"] == str(run.id) for e in events)


async def test_a_non_terminal_status_does_not_stamp_finished_at(
    authed, make_thread, make_run, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a, status="queued")
    response = await authed.post(f"/api/runs/{run.id}/status", json={"status": "running"})
    assert response.json()["finished_at"] is None


async def test_run_status_requires_a_status(authed, make_thread, make_run, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    assert (await authed.post(f"/api/runs/{run.id}/status", json={})).status_code == 422


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_audit_is_newest_first(authed, bot_a):
    await authed.patch(f"/api/bots/{bot_a.id}", json={"name": "One"})
    await authed.patch(f"/api/bots/{bot_a.id}", json={"name": "Two"})
    events = (await authed.get("/api/audit")).json()
    stamps = [e["created_at"] for e in events]
    assert stamps == sorted(stamps, reverse=True)


async def test_audit_filtered_by_bot_and_event_type(authed, bot_a):
    await authed.patch(f"/api/bots/{bot_a.id}/budget", json={"daily_budget_usd": 3.0})
    response = await authed.get(f"/api/audit?bot_id={bot_a.id}&event_type=budget_updated")
    assert response.status_code == 200
    events = response.json()
    assert events
    assert all(e["event_type"] == "budget_updated" for e in events)
    assert all(e["bot_id"] == str(bot_a.id) for e in events)


async def test_audit_before_filter(authed, bot_a):
    await authed.patch(f"/api/bots/{bot_a.id}", json={"name": "Recent"})
    now = datetime.now(timezone.utc)

    older = await authed.get("/api/audit", params={"before": (now - timedelta(days=1)).isoformat()})
    assert older.status_code == 200
    assert older.json() == []

    newer = await authed.get("/api/audit", params={"before": (now + timedelta(days=1)).isoformat()})
    assert newer.status_code == 200
    assert any(e["event_type"] == "bot_updated" for e in newer.json())


async def test_audit_honours_the_limit(authed, bot_a):
    for i in range(4):
        await authed.patch(f"/api/bots/{bot_a.id}", json={"name": f"Name {i}"})
    response = await authed.get("/api/audit?limit=2")
    assert len(response.json()) == 2


async def test_audit_limit_is_validated(authed):
    assert (await authed.get("/api/audit?limit=0")).status_code == 422
    assert (await authed.get("/api/audit?limit=501")).status_code == 422
