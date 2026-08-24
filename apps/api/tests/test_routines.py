"""Routine CRUD, teaching, schedule sync, and run history."""

from __future__ import annotations

import uuid

MISSING = uuid.uuid4()

STEPS = [
    {"type": "connector", "connector_id": "crm", "action": "search_accounts", "input": {"query": "a"}},
]


async def test_create_a_routine(authed, bot_a):
    response = await authed.post(
        "/api/routines",
        json={
            "bot_id": str(bot_a.id),
            "name": "Morning sweep",
            "description": "Check the pipeline",
            "steps": STEPS,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Morning sweep"
    assert body["version"] == 1
    assert body["enabled"] is True
    assert body["steps"] == STEPS


async def test_create_a_routine_with_a_cron_when_temporal_is_down(authed, bot_a):
    """Schedule sync is best effort — an unreachable Temporal must not fail the write."""
    response = await authed.post(
        "/api/routines",
        json={
            "bot_id": str(bot_a.id),
            "name": "Cron routine",
            "steps": STEPS,
            "schedule_cron": "0 9 * * *",
        },
    )
    assert response.status_code == 200
    assert response.json()["schedule_cron"] == "0 9 * * *"


async def test_teach_a_routine_from_recorded_steps(authed, bot_a):
    response = await authed.post(
        "/api/routines/teach",
        json={
            "bot_id": str(bot_a.id),
            "name": "Taught flow",
            "recorded_steps": [
                {"type": "desktop", "action": "click", "x": 10, "y": 20},
                {"action": "type", "text": "hello"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["description"] == "Taught by demonstration"
    assert body["steps"] == [
        {"type": "desktop", "action": "click", "args": {"x": 10, "y": 20}},
        {"type": "desktop", "action": "type", "args": {"text": "hello"}},
    ]


async def test_list_routines(authed, make_routine, bot_a):
    routine = await make_routine(bot_a)
    ids = {r["id"] for r in (await authed.get("/api/routines")).json()}
    assert str(routine.id) in ids


async def test_list_routines_filtered_by_bot(authed, make_routine, make_bot, user_a, bot_a):
    other_bot = await make_bot(user_a, name="Second")
    mine = await make_routine(bot_a)
    theirs = await make_routine(other_bot)
    ids = {r["id"] for r in (await authed.get(f"/api/routines?bot_id={bot_a.id}")).json()}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids


async def test_get_a_routine(authed, make_routine, bot_a):
    routine = await make_routine(bot_a)
    response = await authed.get(f"/api/routines/{routine.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(routine.id)


async def test_get_a_missing_routine_is_404(authed):
    response = await authed.get(f"/api/routines/{MISSING}")
    assert response.status_code == 404
    assert response.json()["code"] == "routine_not_found"


async def test_patch_a_routine_name_does_not_bump_the_version(authed, make_routine, bot_a):
    routine = await make_routine(bot_a, steps=STEPS)
    response = await authed.patch(f"/api/routines/{routine.id}", json={"name": "Renamed"})
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert response.json()["version"] == 1


async def test_changing_the_steps_bumps_the_version(authed, make_routine, bot_a):
    routine = await make_routine(bot_a, steps=STEPS)
    new_steps = [*STEPS, {"type": "desktop", "action": "click"}]
    response = await authed.patch(f"/api/routines/{routine.id}", json={"steps": new_steps})
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["steps"] == new_steps


async def test_writing_identical_steps_does_not_bump_the_version(authed, make_routine, bot_a):
    routine = await make_routine(bot_a, steps=STEPS)
    response = await authed.patch(f"/api/routines/{routine.id}", json={"steps": STEPS})
    assert response.json()["version"] == 1


async def test_disable_and_re_enable_a_routine(authed, make_routine, bot_a):
    routine = await make_routine(bot_a, schedule_cron="0 8 * * *")
    disabled = await authed.patch(f"/api/routines/{routine.id}", json={"enabled": False})
    assert disabled.json()["enabled"] is False
    enabled = await authed.patch(f"/api/routines/{routine.id}", json={"enabled": True})
    assert enabled.json()["enabled"] is True


async def test_clearing_the_cron_is_persisted(authed, make_routine, bot_a):
    routine = await make_routine(bot_a, schedule_cron="0 8 * * *")
    response = await authed.patch(f"/api/routines/{routine.id}", json={"schedule_cron": None})
    assert response.status_code == 200
    assert response.json()["schedule_cron"] is None


async def test_delete_a_routine(authed, make_routine, bot_a):
    routine = await make_routine(bot_a)
    response = await authed.delete(f"/api/routines/{routine.id}")
    assert response.status_code == 200
    assert response.json()["detail"] == "deleted"
    assert (await authed.get(f"/api/routines/{routine.id}")).status_code == 404


async def test_delete_a_missing_routine_is_404(authed):
    assert (await authed.delete(f"/api/routines/{MISSING}")).status_code == 404


async def test_run_a_routine_inline_and_see_it_in_the_run_history(authed, make_routine, bot_a):
    routine = await make_routine(bot_a, steps=STEPS)
    started = await authed.post(f"/api/routines/{routine.id}/run")
    assert started.status_code == 200
    assert started.json()["inline"] is True

    runs = await authed.get(f"/api/routines/{routine.id}/runs")
    assert runs.status_code == 200
    rows = runs.json()
    assert rows, "an inline run must be visible in the routine's run history"
    assert rows[0]["bot_id"] == str(bot_a.id)
    assert rows[0]["thread_id"] is None, "a routine run has no chat thread"
    assert rows[0]["status"] in ("completed", "failed", "awaiting_approval")


async def test_routine_runs_is_empty_before_any_run(authed, make_routine, bot_a):
    routine = await make_routine(bot_a)
    response = await authed.get(f"/api/routines/{routine.id}/runs")
    assert response.status_code == 200
    assert response.json() == []


async def test_routine_runs_honours_the_limit(authed, make_routine, bot_a):
    routine = await make_routine(bot_a, steps=[])
    for _ in range(3):
        await authed.post(f"/api/routines/{routine.id}/run")
    response = await authed.get(f"/api/routines/{routine.id}/runs?limit=2")
    assert len(response.json()) == 2


async def test_creating_a_routine_requires_steps(authed, bot_a):
    response = await authed.post("/api/routines", json={"bot_id": str(bot_a.id), "name": "x"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_creating_a_routine_on_an_unknown_bot_is_404(authed):
    response = await authed.post(
        "/api/routines", json={"bot_id": str(MISSING), "name": "x", "steps": []}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"
