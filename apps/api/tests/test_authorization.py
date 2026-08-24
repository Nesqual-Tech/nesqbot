"""Cross-tenant authorization.

The rule (deps.py): anything the caller may not see answers **404**, never 403,
so the API never leaks the existence of another tenant's row. System bots are
shared and stay visible to everyone; everything hanging off a *custom* bot
inherits that bot's owner. Approvals *and runs* are additionally scoped to the
human behind them rather than to the bot, which is what keeps the
human-in-the-loop gate - and one user's run history - honest on the five shared
system bots.
"""

from __future__ import annotations

import uuid

import pytest

from app.routers.deps import REQUESTED_BY_KEY


def _codes_are_404(response) -> None:
    assert response.status_code == 404, (
        f"{response.request.method} {response.request.url.path} leaked "
        f"{response.status_code}; cross-tenant access must be 404"
    )
    body = response.json()
    assert body["code"].endswith("not_found"), body


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------


async def test_second_user_cannot_read_another_users_bot(other, bot_a):
    _codes_are_404(await other.get(f"/api/bots/{bot_a.id}"))


async def test_second_user_cannot_patch_another_users_bot(other, bot_a):
    _codes_are_404(await other.patch(f"/api/bots/{bot_a.id}", json={"name": "pwned"}))


async def test_second_user_cannot_delete_another_users_bot(other, bot_a):
    _codes_are_404(await other.delete(f"/api/bots/{bot_a.id}"))


async def test_second_user_cannot_change_another_users_budget(other, bot_a):
    _codes_are_404(await other.patch(f"/api/bots/{bot_a.id}/budget", json={"daily_budget_usd": 0}))


async def test_system_bots_stay_visible_to_everyone(authed, other, system_bot):
    for client in (authed, other):
        response = await client.get(f"/api/bots/{system_bot.id}")
        assert response.status_code == 200
        assert response.json()["is_system"] is True


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


async def test_second_user_cannot_read_another_users_thread_messages(
    other, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    _codes_are_404(await other.get(f"/api/threads/{thread.id}/messages"))


async def test_second_user_cannot_post_into_another_users_thread(other, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    _codes_are_404(await other.post(f"/api/threads/{thread.id}/messages", json={"content": "hi"}))


async def test_second_user_cannot_stream_another_users_thread(other, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    _codes_are_404(
        await other.post(f"/api/threads/{thread.id}/messages/stream", json={"content": "hi"})
    )


async def test_second_user_cannot_subscribe_to_another_users_thread_events(
    other, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    _codes_are_404(await other.get(f"/api/threads/{thread.id}/events"))


async def test_second_user_cannot_delete_another_users_thread(other, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    _codes_are_404(await other.delete(f"/api/threads/{thread.id}"))


# ---------------------------------------------------------------------------
# Runs / audit
# ---------------------------------------------------------------------------


async def test_second_user_cannot_read_another_users_run(other, make_thread, make_run, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    _codes_are_404(await other.get(f"/api/runs/{run.id}"))


async def test_second_user_cannot_post_a_status_callback_on_another_users_run(
    other, make_thread, make_run, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    _codes_are_404(await other.post(f"/api/runs/{run.id}/status", json={"status": "failed"}))


async def test_run_list_is_owner_scoped(authed, other, make_thread, make_run, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    assert str(run.id) in {r["id"] for r in (await authed.get("/api/runs")).json()}
    assert str(run.id) not in {r["id"] for r in (await other.get("/api/runs")).json()}


# ---------------------------------------------------------------------------
# Runs on the shared system bots
#
# The five system bots belong to everyone, so a run may not inherit their
# visibility: that would publish one user's run - status, `detail`, and the
# free-text `error` the orchestrator and the worker write - to the whole tenant.
# `deps.resolve_run_owner` scopes a run to the human behind it instead (ledger
# `requested_by`, thread owner, routine owner, custom-bot owner), and a run with
# no such human belongs to nobody rather than to everybody.
# ---------------------------------------------------------------------------

#: A believable free-text failure, to make the point that this is not metadata.
LEAKY_ERROR = "SMTP 550: mailbox unavailable for finance@acme.example"


async def _detached_run(db, bot, *, routine=None, ledger=None, status="failed", error=LEAKY_ERROR):
    """A run with no thread - what a routine run is, and what a chat run becomes
    once its thread is deleted (`runs.thread_id` is ON DELETE SET NULL)."""
    from app.models import Run

    run = Run(
        thread_id=None,
        routine_id=getattr(routine, "id", routine),
        bot_id=bot.id,
        status=status,
        error=error,
        context_ledger=dict(ledger or {}),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _owned_routine(db, bot, owner):
    from app.models import Routine

    routine = Routine(
        bot_id=bot.id,
        owner_user_id=getattr(owner, "id", owner),
        name="Nightly sweep",
        steps=[{"type": "message", "text": "hi"}],
    )
    db.add(routine)
    await db.commit()
    await db.refresh(routine)
    return routine


async def test_second_user_cannot_read_a_system_bot_run_in_another_users_thread(
    other, make_thread, make_run, user_a, system_bot
):
    """The pre-existing hole: bot visibility is not a scoping rule."""
    thread = await make_thread(user_a, [system_bot])
    run = await make_run(thread, system_bot)
    _codes_are_404(await other.get(f"/api/runs/{run.id}"))


async def test_second_user_cannot_post_a_status_callback_on_a_system_bot_run(
    other, make_thread, make_run, user_a, system_bot
):
    thread = await make_thread(user_a, [system_bot])
    run = await make_run(thread, system_bot)
    _codes_are_404(await other.post(f"/api/runs/{run.id}/status", json={"status": "failed"}))


async def test_system_bot_run_list_is_owner_scoped(
    authed, other, make_thread, make_run, user_a, system_bot
):
    thread = await make_thread(user_a, [system_bot])
    run = await make_run(thread, system_bot)
    assert str(run.id) in {r["id"] for r in (await authed.get("/api/runs")).json()}
    assert str(run.id) not in {r["id"] for r in (await other.get("/api/runs")).json()}


async def test_deleting_a_thread_does_not_publish_its_system_bot_run(
    authed, other, db, make_thread, make_run, user_a, system_bot
):
    """`thread_id` goes NULL on delete; that must not promote the run to public.

    An orphaned chat run is indistinguishable from an unattended routine run, so
    any fallback that exposes the second exposes the first.
    """
    thread = await make_thread(user_a, [system_bot])
    run = await make_run(thread, system_bot, status="failed")
    run.error = LEAKY_ERROR
    await db.commit()
    run_id = run.id

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200
    db.expunge_all()  # the SET NULL happened in the database, not in the session

    _codes_are_404(await other.get(f"/api/runs/{run_id}"))
    _codes_are_404(await other.post(f"/api/runs/{run_id}/status", json={"status": "completed"}))
    listed = {r["id"] for r in (await other.get("/api/runs")).json()}
    assert str(run_id) not in listed, "a thread delete must not publish the run it orphaned"


async def test_a_system_bot_routine_run_is_scoped_to_the_routine_owner(
    authed, other, db, user_a, system_bot
):
    routine = await _owned_routine(db, system_bot, user_a)
    run = await _detached_run(db, system_bot, routine=routine)

    assert (await authed.get(f"/api/runs/{run.id}")).status_code == 200
    assert str(run.id) in {r["id"] for r in (await authed.get("/api/runs")).json()}

    _codes_are_404(await other.get(f"/api/runs/{run.id}"))
    assert str(run.id) not in {r["id"] for r in (await other.get("/api/runs")).json()}


async def test_a_run_stamped_with_a_requester_stays_scoped_to_them(
    authed, other, db, user_a, system_bot
):
    """`context_ledger.requested_by` is what `services.routines.run_inline` writes."""
    run = await _detached_run(db, system_bot, ledger={"requested_by": str(user_a.id)})

    assert (await authed.get(f"/api/runs/{run.id}")).status_code == 200
    _codes_are_404(await other.get(f"/api/runs/{run.id}"))
    assert str(run.id) not in {r["id"] for r in (await other.get("/api/runs")).json()}


async def test_an_unattributable_system_bot_run_belongs_to_nobody(authed, other, db, system_bot):
    """No thread, no routine owner, shared bot: there is no human to scope it to.

    Unattended routine history stays reachable through
    `GET /routines/{routine_id}/runs`, which is gated on routine visibility.
    """
    run = await _detached_run(db, system_bot)
    for client in (authed, other):
        _codes_are_404(await client.get(f"/api/runs/{run.id}"))
        assert str(run.id) not in {r["id"] for r in (await client.get("/api/runs")).json()}


async def test_audit_is_scoped_to_the_actor_and_their_bots(authed, other, bot_a):
    await authed.patch(f"/api/bots/{bot_a.id}", json={"name": "Audited"})
    mine = await authed.get("/api/audit")
    theirs = await other.get("/api/audit")
    assert any(e["event_type"] == "bot_updated" for e in mine.json())
    assert all(e.get("bot_id") != str(bot_a.id) for e in theirs.json())


# ---------------------------------------------------------------------------
# Approvals — including the system-bot case, which is the important one
# ---------------------------------------------------------------------------


async def test_second_user_cannot_read_an_approval_on_another_users_custom_bot(
    other, make_approval, bot_a, user_a
):
    approval = await make_approval(bot_a, requested_by=user_a)
    _codes_are_404(await other.get(f"/api/approvals/{approval.id}"))


async def test_second_user_cannot_read_a_system_bot_approval_requested_by_someone_else(
    other, make_approval, system_bot, user_a
):
    """System bots are shared, so the requester tag is the only thing scoping this."""
    approval = await make_approval(system_bot, requested_by=user_a)
    _codes_are_404(await other.get(f"/api/approvals/{approval.id}"))


async def test_second_user_cannot_decide_a_system_bot_approval_requested_by_someone_else(
    other, db, make_approval, system_bot, user_a
):
    """The human-in-the-loop gate: B must not be able to approve A's held send."""
    approval = await make_approval(
        system_bot,
        risk="send",
        requested_by=user_a,
        payload={
            "kind": "connector_action",
            "connector_id": "microsoft_graph",
            "action": "send_mail",
            "input": {"to": "victim@example.com", "subject": "s", "body": "b"},
        },
    )
    response = await other.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )
    _codes_are_404(response)

    await db.refresh(approval)
    assert approval.status == "pending", "a rejected decision must not mutate the approval"
    assert not approval.execution, "the held action must not have run"


async def test_second_user_cannot_expire_a_system_bot_approval_requested_by_someone_else(
    other, make_approval, system_bot, user_a
):
    approval = await make_approval(system_bot, requested_by=user_a)
    _codes_are_404(await other.post(f"/api/approvals/{approval.id}/expire"))


async def test_second_user_cannot_see_a_run_scoped_approval_on_a_system_bot(
    other, make_thread, make_run, make_approval, system_bot, user_a
):
    """Approvals raised inside a chat run inherit the thread owner, not the bot."""
    thread = await make_thread(user_a, [system_bot])
    run = await make_run(thread, system_bot)
    approval = await make_approval(system_bot, run=run)
    _codes_are_404(await other.get(f"/api/approvals/{approval.id}"))


async def test_approval_list_hides_another_users_system_bot_approval(
    authed, other, make_approval, system_bot, user_a
):
    approval = await make_approval(system_bot, requested_by=user_a)
    mine = {a["id"] for a in (await authed.get("/api/approvals")).json()}
    theirs = {a["id"] for a in (await other.get("/api/approvals")).json()}
    assert str(approval.id) in mine
    assert str(approval.id) not in theirs


async def test_approval_list_hides_run_scoped_approvals_from_other_users(
    authed, other, make_thread, make_run, make_approval, system_bot, user_a
):
    thread = await make_thread(user_a, [system_bot])
    run = await make_run(thread, system_bot)
    approval = await make_approval(system_bot, run=run)
    assert str(approval.id) in {a["id"] for a in (await authed.get("/api/approvals")).json()}
    assert str(approval.id) not in {a["id"] for a in (await other.get("/api/approvals")).json()}


async def test_creating_an_approval_on_another_users_bot_is_404(other, bot_a):
    response = await other.post(
        "/api/approvals",
        json={"bot_id": str(bot_a.id), "risk": "send", "title": "sneak", "summary": ""},
    )
    _codes_are_404(response)


async def test_the_requester_can_still_decide_their_own_system_bot_approval(
    authed, make_approval, system_bot, user_a
):
    """The scoping must not lock the legitimate requester out."""
    approval = await make_approval(
        system_bot,
        requested_by=user_a,
        payload={"kind": "message_only", "draft": "ok", "thread_id": None},
    )
    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "rejected"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


# ---------------------------------------------------------------------------
# Connector bindings
# ---------------------------------------------------------------------------


async def test_second_user_cannot_list_another_users_bindings(other, bot_a):
    _codes_are_404(await other.get(f"/api/bots/{bot_a.id}/connectors"))


async def test_second_user_cannot_bind_a_connector_to_another_users_bot(other, bot_a):
    _codes_are_404(
        await other.post(f"/api/bots/{bot_a.id}/connectors/microsoft_graph", json={"status": "connected"})
    )


async def test_second_user_cannot_unbind_another_users_connector(
    other, bot_a, make_connector_binding
):
    await make_connector_binding(bot_a)
    _codes_are_404(await other.delete(f"/api/bots/{bot_a.id}/connectors/microsoft_graph"))


async def test_second_user_cannot_execute_an_action_on_another_users_bot(other, bot_a):
    _codes_are_404(
        await other.post(
            f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/list_inbox",
            json={"input": {}},
        )
    )


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


async def test_second_user_cannot_read_another_users_mcp_server(other, make_mcp, user_a):
    server = await make_mcp(user_a)
    _codes_are_404(await other.patch(f"/api/integrations/mcp/{server.id}", json={"enabled": False}))
    _codes_are_404(await other.delete(f"/api/integrations/mcp/{server.id}"))
    _codes_are_404(await other.get(f"/api/integrations/mcp/{server.id}/tools"))


async def test_mcp_list_is_owner_scoped(authed, other, make_mcp, user_a):
    server = await make_mcp(user_a)
    assert str(server.id) in {m["id"] for m in (await authed.get("/api/integrations/mcp")).json()}
    assert str(server.id) not in {m["id"] for m in (await other.get("/api/integrations/mcp")).json()}


async def test_second_user_cannot_attach_or_call_mcp_on_another_users_bot(
    other, make_mcp, user_a, bot_a
):
    server = await make_mcp(user_a)
    _codes_are_404(await other.post(f"/api/bots/{bot_a.id}/mcp/{server.id}"))
    _codes_are_404(await other.delete(f"/api/bots/{bot_a.id}/mcp/{server.id}"))
    _codes_are_404(
        await other.post(f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "echo"})
    )


# ---------------------------------------------------------------------------
# Bot Desktop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("post", "/start"),
        ("post", "/stop"),
        ("post", "/suspend"),
        ("post", "/resume"),
        ("get", "/screenshot"),
        ("get", "/windows"),
    ],
)
async def test_second_user_cannot_touch_another_users_desktop(other, bot_a, method, suffix):
    call = getattr(other, method)
    _codes_are_404(await call(f"/api/bots/{bot_a.id}/desktop{suffix}"))


async def test_second_user_cannot_run_a_desktop_action_on_another_users_bot(other, bot_a):
    _codes_are_404(
        await other.post(f"/api/bots/{bot_a.id}/desktop/action", json={"action": "click", "x": 1, "y": 2})
    )


# ---------------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------------


async def test_second_user_cannot_read_another_users_routine(other, make_routine, bot_a):
    routine = await make_routine(bot_a)
    _codes_are_404(await other.get(f"/api/routines/{routine.id}"))


async def test_second_user_cannot_mutate_another_users_routine(other, make_routine, bot_a):
    routine = await make_routine(bot_a)
    _codes_are_404(await other.patch(f"/api/routines/{routine.id}", json={"name": "pwned"}))
    _codes_are_404(await other.delete(f"/api/routines/{routine.id}"))


async def test_second_user_cannot_run_another_users_routine(other, make_routine, bot_a):
    routine = await make_routine(bot_a)
    _codes_are_404(await other.post(f"/api/routines/{routine.id}/run"))
    _codes_are_404(await other.get(f"/api/routines/{routine.id}/runs"))


async def test_routine_list_is_bot_scoped(authed, other, make_routine, bot_a):
    routine = await make_routine(bot_a)
    assert str(routine.id) in {r["id"] for r in (await authed.get("/api/routines")).json()}
    assert str(routine.id) not in {r["id"] for r in (await other.get("/api/routines")).json()}


async def test_creating_a_routine_on_another_users_bot_is_404(other, bot_a):
    _codes_are_404(
        await other.post(
            "/api/routines", json={"bot_id": str(bot_a.id), "name": "sneak", "steps": []}
        )
    )
    _codes_are_404(
        await other.post(
            "/api/routines/teach",
            json={"bot_id": str(bot_a.id), "name": "sneak", "recorded_steps": []},
        )
    )


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


async def test_second_user_cannot_list_or_write_memories_on_another_users_bot(other, bot_a):
    _codes_are_404(await other.get(f"/api/bots/{bot_a.id}/memories"))
    _codes_are_404(await other.post(f"/api/bots/{bot_a.id}/memories", json={"content": "x"}))


async def test_second_user_cannot_delete_another_users_memory(
    other, make_memory, bot_a, user_a
):
    memory = await make_memory(bot_a, user_a)
    _codes_are_404(await other.delete(f"/api/memories/{memory.id}"))


async def test_memories_on_a_system_bot_are_still_per_user(
    authed, other, make_memory, system_bot, user_a, user_b
):
    mine = await make_memory(system_bot, user_a, content="A private note")
    theirs = await make_memory(system_bot, user_b, content="B private note")

    a_list = {m["id"] for m in (await authed.get(f"/api/bots/{system_bot.id}/memories")).json()}
    b_list = {m["id"] for m in (await other.get(f"/api/bots/{system_bot.id}/memories")).json()}
    assert str(mine.id) in a_list and str(theirs.id) not in a_list
    assert str(theirs.id) in b_list and str(mine.id) not in b_list


async def test_second_user_cannot_delete_a_system_bot_memory_owned_by_someone_else(
    other, make_memory, system_bot, user_a
):
    memory = await make_memory(system_bot, user_a)
    _codes_are_404(await other.delete(f"/api/memories/{memory.id}"))


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


async def test_usage_only_reports_visible_bots(authed, other, bot_a):
    mine = {u["bot_id"] for u in (await authed.get("/api/usage")).json()}
    theirs = {u["bot_id"] for u in (await other.get("/api/usage")).json()}
    assert str(bot_a.id) in mine
    assert str(bot_a.id) not in theirs


# ---------------------------------------------------------------------------
# Unknown ids answer the same way as forbidden ones
# ---------------------------------------------------------------------------


async def test_unknown_and_forbidden_ids_are_indistinguishable(other, bot_a):
    """A 404 for someone else's bot must look exactly like a 404 for a bogus id."""
    unknown = uuid.uuid4()
    forbidden = await other.get(f"/api/bots/{bot_a.id}")
    missing = await other.get(f"/api/bots/{unknown}")
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json() == missing.json()


async def test_requested_by_key_is_the_documented_payload_field():
    assert REQUESTED_BY_KEY == "requested_by"
