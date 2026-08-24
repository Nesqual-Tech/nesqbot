"""Rehearsal and reversibility over HTTP.

The two capabilities the competitive analysis names as a competing agent product's most-cited
gaps: a test run that performs no real work, and an approval that can reverse
work already completed. The service lane built both; this module asserts they
are reachable, honest and scoped over the API.

Four claims are load-bearing and each is proved directly here:

1. a dry run over HTTP produces **zero** side effects — no approval, no action
   log entry, no run, no vendor call;
2. a plan whose work changed after a human reviewed it is refused **409
   `plan_drifted`**, with the code intact;
3. an undo is idempotent — the second call is refused rather than performed;
4. every one of these routes is ownership-scoped, and a second user gets 404
   rather than 403.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import ActionLog, Approval, PlanRecord, Run

MISSING = uuid.uuid4()

SEND_STEP = {
    "type": "connector",
    "connector_id": "microsoft_graph",
    "action": "send_mail",
    "input": {"to": "a@b.c", "subject": "Q3 invoice", "body": "attached"},
}
TASK_STEP = {
    "type": "connector",
    "connector_id": "crm",
    "action": "create_task",
    "input": {"account_id": "acc_1", "title": "Follow up"},
}
SAFE_STEP = {
    "type": "connector",
    "connector_id": "crm",
    "action": "search_accounts",
    "input": {"query": "acme"},
}


async def _rows(db, model, bot):
    result = await db.execute(select(model).where(model.bot_id == bot.id))
    return list(result.scalars().all())


@pytest.fixture
async def wired(make_connector_binding, bot_a):
    """`bot_a` with the first-party connectors bound, so nothing mocks for want of a binding."""
    for connector_id in ("microsoft_graph", "crm", "ticketing"):
        await make_connector_binding(bot_a, connector_id, status="connected")
    return bot_a


# ---------------------------------------------------------------------------
# Dry run — the whole promise is that nothing happens
# ---------------------------------------------------------------------------


async def test_a_routine_dry_run_returns_a_plan(authed, make_routine, wired):
    routine = await make_routine(wired, name="Invoice chase", steps=[SEND_STEP, TASK_STEP])
    response = await authed.post(f"/api/routines/{routine.id}/dry-run")
    assert response.status_code == 200

    plan = response.json()
    assert plan["bot_id"] == str(wired.id)
    assert plan["routine_id"] == str(routine.id)
    assert plan["content_hash"]
    assert len(plan["calls"]) == 2
    assert [c["step_index"] for c in plan["calls"]] == [0, 1]
    assert plan["calls"][0]["action"] == "send_mail"
    assert plan["calls"][0]["input"] == SEND_STEP["input"]
    assert plan["calls"][0]["risk"] == "send"
    assert plan["calls"][0]["requires_approval"] is True
    assert plan["verdict"]["steps_total"] == 2
    assert plan["verdict"]["would_gate"] == [0]


async def test_a_dry_run_performs_nothing_at_all(authed, db, make_routine, wired):
    """The claim in one test: rehearsing a gated, mutating routine writes nothing."""
    routine = await make_routine(wired, steps=[SEND_STEP, TASK_STEP])
    response = await authed.post(f"/api/routines/{routine.id}/dry-run")
    assert response.status_code == 200

    assert await _rows(db, Approval, wired) == [], "a dry run parked an approval"
    assert await _rows(db, ActionLog, wired) == [], "a dry run reached the executor"
    assert await _rows(db, Run, wired) == [], "a dry run recorded a run"
    assert await _rows(db, PlanRecord, wired) == [], "a dry run persisted itself"


async def test_a_dry_run_does_not_halt_at_the_first_gate(authed, make_routine, wired):
    """A rehearsal shows the whole plan; only the real run stops at step 0."""
    routine = await make_routine(wired, steps=[SEND_STEP, SEND_STEP, TASK_STEP])
    plan = (await authed.post(f"/api/routines/{routine.id}/dry-run")).json()
    assert plan["verdict"]["would_gate"] == [0, 1]
    assert len(plan["calls"]) == 3


async def test_a_dry_run_reports_reversibility_before_the_work_happens(
    authed, make_routine, wired
):
    """The point of showing it up front: you learn a send cannot be taken back first."""
    routine = await make_routine(wired, steps=[SEND_STEP, TASK_STEP])
    plan = (await authed.post(f"/api/routines/{routine.id}/dry-run")).json()
    by_action = {c["action"]: c for c in plan["calls"]}
    assert by_action["create_task"]["reversible"] is True
    assert by_action["send_mail"]["reversible"] is False
    assert by_action["send_mail"]["undo_note"]
    assert plan["verdict"]["reversible"] == [1]


async def test_a_dry_run_surfaces_a_missing_required_input(authed, make_routine, wired):
    routine = await make_routine(
        wired,
        steps=[{"type": "connector", "connector_id": "crm", "action": "create_task", "input": {}}],
    )
    plan = (await authed.post(f"/api/routines/{routine.id}/dry-run")).json()
    assert plan["verdict"]["ok"] is False
    assert plan["calls"][0]["ok"] is False
    assert any("missing required input" in p for p in plan["calls"][0]["problems"])


async def test_a_dry_run_of_an_unbound_connector_says_so(authed, make_routine, bot_a):
    routine = await make_routine(bot_a, steps=[TASK_STEP])
    plan = (await authed.post(f"/api/routines/{routine.id}/dry-run")).json()
    assert plan["calls"][0]["binding"]["bound"] is False
    assert plan["calls"][0]["ok"] is False


async def test_a_dry_run_is_repeatable_and_stable(authed, make_routine, wired):
    """The hash covers intent, not the clock: two rehearsals of the same work agree."""
    routine = await make_routine(wired, steps=[SEND_STEP])
    first = (await authed.post(f"/api/routines/{routine.id}/dry-run")).json()
    second = (await authed.post(f"/api/routines/{routine.id}/dry-run")).json()
    assert first["content_hash"] == second["content_hash"]


async def test_a_missing_routine_dry_run_is_404(authed):
    response = await authed.post(f"/api/routines/{MISSING}/dry-run")
    assert response.status_code == 404
    assert response.json()["code"] == "routine_not_found"


# ---------------------------------------------------------------------------
# Dry run — a single connector action
# ---------------------------------------------------------------------------


async def test_a_connector_action_dry_run_returns_a_one_call_plan(authed, wired):
    response = await authed.post(
        f"/api/bots/{wired.id}/connectors/microsoft_graph/actions/send_mail/dry-run",
        json={"input": SEND_STEP["input"]},
    )
    assert response.status_code == 200
    plan = response.json()
    assert len(plan["calls"]) == 1
    call = plan["calls"][0]
    assert call["connector_id"] == "microsoft_graph"
    assert call["action"] == "send_mail"
    assert call["risk"] == "send"
    assert call["requires_approval"] is True
    assert call["summary"]


async def test_a_connector_action_dry_run_sends_nothing(authed, db, wired):
    await authed.post(
        f"/api/bots/{wired.id}/connectors/microsoft_graph/actions/send_mail/dry-run",
        json={"input": SEND_STEP["input"]},
    )
    assert await _rows(db, ActionLog, wired) == []
    assert await _rows(db, Approval, wired) == []


async def test_the_dry_run_and_the_execute_route_agree_on_the_gate(authed, wired):
    """Rehearsal is worthless if it disagrees with the path it rehearses."""
    plan = (
        await authed.post(
            f"/api/bots/{wired.id}/connectors/microsoft_graph/actions/send_mail/dry-run",
            json={"input": SEND_STEP["input"]},
        )
    ).json()
    real = await authed.post(
        f"/api/bots/{wired.id}/connectors/microsoft_graph/actions/send_mail",
        json={"input": SEND_STEP["input"]},
    )
    assert plan["calls"][0]["requires_approval"] is True
    assert real.status_code == 201
    assert real.json()["risk"] == plan["calls"][0]["risk"]


async def test_a_dry_run_against_an_unknown_connector_is_404(authed, bot_a):
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/nope/actions/send_mail/dry-run", json={"input": {}}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "connector_not_found"


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


async def _save_routine_plan(client, routine, **extra):
    return await client.post("/api/plans", json={"routine_id": str(routine.id), **extra})


async def test_a_routine_plan_can_be_saved(authed, make_routine, wired):
    routine = await make_routine(wired, name="Nightly", steps=[SAFE_STEP])
    response = await _save_routine_plan(authed, routine)
    assert response.status_code == 200
    record = response.json()
    assert record["routine_id"] == str(routine.id)
    assert record["status"] == "draft"
    assert record["steps_total"] == 1
    assert record["content_hash"]
    assert record["plan"]["calls"][0]["action"] == "search_accounts"


async def test_saving_a_plan_still_performs_nothing(authed, db, make_routine, wired):
    routine = await make_routine(wired, steps=[SEND_STEP])
    await _save_routine_plan(authed, routine)
    assert await _rows(db, ActionLog, wired) == []
    assert await _rows(db, Approval, wired) == []


async def test_an_ad_hoc_plan_needs_no_routine(authed, wired):
    response = await authed.post(
        "/api/plans",
        json={"bot_id": str(wired.id), "steps": [SAFE_STEP], "name": "one-off"},
    )
    assert response.status_code == 200
    record = response.json()
    assert record["routine_id"] is None
    assert record["name"] == "one-off"


async def test_a_plan_needs_a_source(authed):
    response = await authed.post("/api/plans", json={})
    assert response.status_code == 400
    assert response.json()["code"] == "plan_source_required"


async def test_an_expected_hash_that_no_longer_matches_is_refused(authed, make_routine, wired):
    """A client that showed a human one plan cannot save a different one under it."""
    routine = await make_routine(wired, steps=[SAFE_STEP])
    response = await _save_routine_plan(authed, routine, expected_content_hash="0" * 64)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "plan_drifted"
    assert body["expected_hash"] == "0" * 64
    assert body["actual_hash"] != "0" * 64


async def test_an_expected_hash_that_matches_is_accepted(authed, make_routine, wired):
    routine = await make_routine(wired, steps=[SAFE_STEP])
    plan = (await authed.post(f"/api/routines/{routine.id}/dry-run")).json()
    response = await _save_routine_plan(authed, routine, expected_content_hash=plan["content_hash"])
    assert response.status_code == 200
    assert response.json()["content_hash"] == plan["content_hash"]


async def test_saving_a_plan_never_edits_the_routine_it_rehearses(
    authed, db, make_routine, wired
):
    routine = await make_routine(wired, name="Original", steps=[SAFE_STEP])
    await _save_routine_plan(authed, routine, name="Renamed by the plan")
    await db.refresh(routine)
    assert routine.name == "Original"


async def test_plans_are_listed_and_scoped_to_the_bot(authed, make_routine, wired, bot_b):
    routine = await make_routine(wired, steps=[SAFE_STEP])
    first = (await _save_routine_plan(authed, routine)).json()
    second = (
        await authed.post("/api/plans", json={"bot_id": str(wired.id), "steps": [TASK_STEP]})
    ).json()

    listed = await authed.get(f"/api/plans?bot_id={wired.id}&limit=10")
    assert listed.status_code == 200
    # Ordering is `created_at DESC`, but every row in one test transaction shares
    # Postgres' transaction timestamp, so only membership is assertable here.
    assert {p["id"] for p in listed.json()} == {first["id"], second["id"]}
    assert all(p["bot_id"] == str(wired.id) for p in listed.json())


async def test_a_saved_plan_can_be_fetched(authed, make_routine, wired):
    routine = await make_routine(wired, steps=[SAFE_STEP])
    saved = (await _save_routine_plan(authed, routine)).json()
    response = await authed.get(f"/api/plans/{saved['id']}")
    assert response.status_code == 200
    assert response.json()["content_hash"] == saved["content_hash"]


async def test_a_missing_plan_is_404(authed):
    response = await authed.get(f"/api/plans/{MISSING}")
    assert response.status_code == 404
    assert response.json()["code"] == "plan_not_found"


# ---------------------------------------------------------------------------
# Executing a plan — the drift refusal is the security property
# ---------------------------------------------------------------------------


async def test_an_unchanged_plan_executes(authed, db, make_routine, wired):
    routine = await make_routine(wired, steps=[SAFE_STEP])
    saved = (await _save_routine_plan(authed, routine)).json()

    response = await authed.post(f"/api/plans/{saved['id']}/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["run_id"]
    assert body["inline"] is True

    record = await db.get(PlanRecord, uuid.UUID(saved["id"]))
    await db.refresh(record)
    assert record.status == "executed"


async def test_a_mutated_plan_is_refused_409_plan_drifted(authed, db, make_routine, wired):
    """A human approved a search. The routine is then rewritten to send an email.

    This is the attack the content hash exists to stop, and the refusal has to
    be legible: 409, and the `plan_drifted` code intact.
    """
    routine = await make_routine(wired, steps=[SAFE_STEP])
    saved = (await _save_routine_plan(authed, routine)).json()

    swapped = await authed.patch(f"/api/routines/{routine.id}", json={"steps": [SEND_STEP]})
    assert swapped.status_code == 200

    response = await authed.post(f"/api/plans/{saved['id']}/execute")
    assert response.status_code == 409
    assert response.json()["code"] == "plan_drifted"

    assert await _rows(db, ActionLog, wired) == [], "the drifted plan still performed work"
    assert await _rows(db, Approval, wired) == []

    record = await db.get(PlanRecord, uuid.UUID(saved["id"]))
    await db.refresh(record)
    assert record.status == "stale"


async def test_a_plan_cannot_be_executed_twice(authed, make_routine, wired):
    routine = await make_routine(wired, steps=[SAFE_STEP])
    saved = (await _save_routine_plan(authed, routine)).json()
    assert (await authed.post(f"/api/plans/{saved['id']}/execute")).status_code == 200

    again = await authed.post(f"/api/plans/{saved['id']}/execute")
    assert again.status_code == 409
    assert again.json()["code"] == "already_executed"


async def test_executing_a_plan_whose_routine_is_gone_is_404(authed, make_routine, wired):
    routine = await make_routine(wired, steps=[SAFE_STEP])
    saved = (await _save_routine_plan(authed, routine)).json()
    assert (await authed.delete(f"/api/routines/{routine.id}")).status_code == 200

    response = await authed.post(f"/api/plans/{saved['id']}/execute")
    assert response.status_code == 404
    assert response.json()["code"] == "routine_gone"


async def test_executing_an_ad_hoc_plan_needs_no_routine_row(authed, db, wired):
    saved = (
        await authed.post("/api/plans", json={"bot_id": str(wired.id), "steps": [TASK_STEP]})
    ).json()
    response = await authed.post(f"/api/plans/{saved['id']}/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    entries = await _rows(db, ActionLog, wired)
    assert [e.action for e in entries] == ["create_task"]


async def test_executing_a_plan_with_a_gated_step_parks_an_approval(
    authed, db, make_routine, wired
):
    """Approving *the plan* is not approving its risky steps — the gate still runs."""
    routine = await make_routine(wired, steps=[SEND_STEP])
    saved = (await _save_routine_plan(authed, routine, status="approved")).json()
    assert saved["gated_steps"] == 1

    response = await authed.post(f"/api/plans/{saved['id']}/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_approval"

    held = await _rows(db, Approval, wired)
    assert len(held) == 1
    assert held[0].payload["kind"] == "connector_action"


# ---------------------------------------------------------------------------
# The undo log
# ---------------------------------------------------------------------------


async def _do_create_task(client, bot, title="Follow up"):
    return await client.post(
        f"/api/bots/{bot.id}/connectors/crm/actions/create_task",
        json={"input": {"account_id": "acc_1", "title": title}},
    )


async def test_an_executed_action_lands_in_the_action_log(authed, wired):
    assert (await _do_create_task(authed, wired)).status_code == 200
    response = await authed.get(f"/api/action-log?bot_id={wired.id}")
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) == 1
    assert entries[0]["kind"] == "connector"
    assert entries[0]["connector_id"] == "crm"
    assert entries[0]["action"] == "create_task"
    assert entries[0]["reversible"] is True
    assert entries[0]["compensator"]["method"] == "DELETE"
    assert entries[0]["undone"] is False


async def test_an_irreversible_action_says_why(authed, wired):
    await authed.post(
        f"/api/bots/{wired.id}/connectors/microsoft_graph/actions/list_inbox",
        json={"input": {"top": 1}},
    )
    entries = (await authed.get(f"/api/action-log?bot_id={wired.id}")).json()
    assert entries[0]["reversible"] is False
    assert "nothing to undo" in entries[0]["irreversible_reason"]


async def test_reversible_only_filters_the_take_it_back_view(authed, wired):
    await _do_create_task(authed, wired)
    await authed.post(
        f"/api/bots/{wired.id}/connectors/microsoft_graph/actions/list_inbox",
        json={"input": {"top": 1}},
    )
    everything = (await authed.get(f"/api/action-log?bot_id={wired.id}")).json()
    reversible = (
        await authed.get(f"/api/action-log?bot_id={wired.id}&reversible_only=true")
    ).json()
    assert len(everything) == 2
    assert [e["action"] for e in reversible] == ["create_task"]


async def test_the_action_log_can_be_filtered_by_run(
    authed, make_approval, make_thread, make_run, user_a, wired
):
    """An approved action carries the run it belonged to into the undo log."""
    thread = await make_thread(user_a, [wired])
    run = await make_run(thread, wired)
    approval = await make_approval(
        wired,
        run=run,
        risk="mutate",
        payload={
            "kind": "connector_action",
            "connector_id": "crm",
            "action": "create_task",
            "input": {"account_id": "acc_1", "title": "Follow up"},
        },
    )
    decided = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )
    assert decided.json()["execution"]["ok"] is True

    by_run = (await authed.get(f"/api/action-log?run_id={run.id}")).json()
    assert [e["action"] for e in by_run] == ["create_task"]
    assert [e["approval_id"] for e in by_run] == [str(approval.id)]
    assert (await authed.get(f"/api/action-log?run_id={MISSING}")).json() == []


async def test_a_routine_run_stamps_its_run_onto_the_action_log(
    authed, make_routine, user_a, wired
):
    """"What did this run actually do?" has to be answerable.

    `_step_effect` knows the step shape but not the executing run, so routine
    and plan executions used to write action-log rows with a NULL run_id -
    findable only by bot, never by run.
    """
    routine = await make_routine(
        wired,
        steps=[
            {
                "type": "connector",
                "connector_id": "crm",
                "action": "create_task",
                "args": {"account_id": "acc_1", "title": "Follow up"},
            }
        ],
    )
    started = await authed.post(f"/api/routines/{routine.id}/run")
    assert started.status_code == 200
    run_id = started.json()["run_id"]
    assert run_id, "an inline routine run must report the run it created"

    by_run = (await authed.get(f"/api/action-log?run_id={run_id}")).json()
    assert [e["action"] for e in by_run] == ["create_task"]


async def test_an_action_can_be_taken_back(authed, wired):
    await _do_create_task(authed, wired)
    entry = (await authed.get(f"/api/action-log?bot_id={wired.id}")).json()[0]

    response = await authed.post(f"/api/action-log/{entry['id']}/undo")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["action_log_id"] == entry["id"]
    assert body["kind"] == "connector"
    assert body["action"] == "create_task"
    assert body["compensator"] == "delete the task that was created"


async def test_an_undo_is_idempotent_and_refuses_a_second_run(authed, db, wired):
    """The claim the UI makes is "taken back", singular. Running it twice is a bug."""
    await _do_create_task(authed, wired)
    entry = (await authed.get(f"/api/action-log?bot_id={wired.id}")).json()[0]

    assert (await authed.post(f"/api/action-log/{entry['id']}/undo")).status_code == 200

    again = await authed.post(f"/api/action-log/{entry['id']}/undo")
    assert again.status_code == 409
    assert again.json()["code"] == "already_undone"

    row = await db.get(ActionLog, uuid.UUID(entry["id"]))
    await db.refresh(row)
    assert row.undone is True
    assert row.undone_by is not None


async def test_an_undone_entry_leaves_the_take_it_back_view(authed, wired):
    await _do_create_task(authed, wired)
    entry = (await authed.get(f"/api/action-log?bot_id={wired.id}")).json()[0]
    await authed.post(f"/api/action-log/{entry['id']}/undo")

    still_reversible = (
        await authed.get(f"/api/action-log?bot_id={wired.id}&reversible_only=true")
    ).json()
    assert still_reversible == []


async def test_undoing_something_irreversible_is_422(authed, wired):
    await authed.post(
        f"/api/bots/{wired.id}/connectors/microsoft_graph/actions/list_inbox",
        json={"input": {"top": 1}},
    )
    entry = (await authed.get(f"/api/action-log?bot_id={wired.id}")).json()[0]

    response = await authed.post(f"/api/action-log/{entry['id']}/undo")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "not_reversible"
    assert body["reason"]


async def test_undoing_a_missing_entry_is_404(authed):
    response = await authed.post(f"/api/action-log/{MISSING}/undo")
    assert response.status_code == 404
    assert response.json()["code"] == "action_log_not_found"


# ---------------------------------------------------------------------------
# The reversibility matrix
# ---------------------------------------------------------------------------


async def test_the_matrix_is_published(authed):
    response = await authed.get("/api/reversibility")
    assert response.status_code == 200
    rows = response.json()
    assert rows
    by_key = {(r["connector_id"], r["action"]): r for r in rows}
    assert by_key[("crm", "create_task")]["reversible"] is True
    assert by_key[("crm", "create_task")]["compensator"]


async def test_the_matrix_is_honest_about_what_cannot_be_taken_back(authed):
    """"A sent email is sent." A matrix that omitted this would read as a promise."""
    rows = (await authed.get("/api/reversibility")).json()
    by_key = {(r["connector_id"], r["action"]): r for r in rows}

    sent = by_key[("microsoft_graph", "send_mail")]
    assert sent["reversible"] is False
    assert "unsend" in sent["reason"]

    assert by_key[(None, "desktop")]["reversible"] is False
    assert by_key[(None, "mcp")]["reversible"] is False


async def test_the_matrix_matches_what_a_dry_run_promises(authed, make_routine, wired):
    """One source of truth: the plan's `reversible` flag comes from this matrix."""
    rows = {(r["connector_id"], r["action"]): r for r in (await authed.get("/api/reversibility")).json()}
    routine = await make_routine(wired, steps=[SEND_STEP, TASK_STEP])
    plan = (await authed.post(f"/api/routines/{routine.id}/dry-run")).json()
    for call in plan["calls"]:
        assert call["reversible"] == rows[(call["connector_id"], call["action"])]["reversible"]


# ---------------------------------------------------------------------------
# Ownership — a second user gets 404 on every one of these routes
# ---------------------------------------------------------------------------


async def test_a_second_user_cannot_dry_run_another_users_routine(other, make_routine, bot_a):
    routine = await make_routine(bot_a, steps=[SAFE_STEP])
    assert (await other.post(f"/api/routines/{routine.id}/dry-run")).status_code == 404


async def test_a_second_user_cannot_dry_run_against_another_users_bot(other, bot_a):
    response = await other.post(
        f"/api/bots/{bot_a.id}/connectors/crm/actions/create_task/dry-run", json={"input": {}}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


async def test_a_second_user_cannot_save_a_plan_for_another_users_bot(other, bot_a):
    response = await other.post(
        "/api/plans", json={"bot_id": str(bot_a.id), "steps": [SAFE_STEP]}
    )
    assert response.status_code == 404


async def test_a_second_user_cannot_list_another_users_plans(
    authed, other, make_routine, bot_a
):
    routine = await make_routine(bot_a, steps=[SAFE_STEP])
    await _save_routine_plan(authed, routine)

    scoped = await other.get(f"/api/plans?bot_id={bot_a.id}")
    assert scoped.status_code == 404

    unscoped = await other.get("/api/plans")
    assert unscoped.status_code == 200
    assert unscoped.json() == []


async def test_a_second_user_cannot_read_or_execute_another_users_plan(
    authed, other, make_routine, bot_a
):
    routine = await make_routine(bot_a, steps=[SAFE_STEP])
    saved = (await _save_routine_plan(authed, routine)).json()

    assert (await other.get(f"/api/plans/{saved['id']}")).status_code == 404
    executed = await other.post(f"/api/plans/{saved['id']}/execute")
    assert executed.status_code == 404
    assert executed.json()["code"] == "plan_not_found"


async def test_a_second_user_cannot_read_another_users_action_log(authed, other, wired):
    await _do_create_task(authed, wired)

    scoped = await other.get(f"/api/action-log?bot_id={wired.id}")
    assert scoped.status_code == 404
    assert scoped.json()["code"] == "bot_not_found"

    assert (await other.get("/api/action-log")).json() == []


async def test_a_second_user_cannot_undo_another_users_action(authed, other, wired):
    await _do_create_task(authed, wired)
    entry = (await authed.get(f"/api/action-log?bot_id={wired.id}")).json()[0]

    response = await other.post(f"/api/action-log/{entry['id']}/undo")
    assert response.status_code == 404
    assert response.json()["code"] == "action_log_not_found"


async def test_the_reversibility_matrix_is_not_per_user(other):
    """The matrix is product documentation, not tenant data."""
    assert (await other.get("/api/reversibility")).status_code == 200


async def test_every_rehearsal_route_requires_authentication(anon, bot_a):
    for method, path in (
        ("POST", f"/api/routines/{MISSING}/dry-run"),
        ("POST", f"/api/bots/{bot_a.id}/connectors/crm/actions/create_task/dry-run"),
        ("POST", "/api/plans"),
        ("GET", "/api/plans"),
        ("GET", f"/api/plans/{MISSING}"),
        ("POST", f"/api/plans/{MISSING}/execute"),
        ("GET", "/api/action-log"),
        ("POST", f"/api/action-log/{MISSING}/undo"),
        ("GET", "/api/reversibility"),
    ):
        response = await anon.request(method, path, json={} if method == "POST" else None)
        assert response.status_code == 401, f"{method} {path} was not authenticated"
