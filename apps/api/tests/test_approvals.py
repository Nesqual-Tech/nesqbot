"""The approval execution loop.

`POST /approvals/{id}/decide` with `approved` must actually run the held action
through `services.approvals.execute_approved`, stamp the outcome on
`Approval.execution`, and return it in the `execution` envelope. Rejecting must
run nothing. Deciding twice must 409.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import AuditEvent, Message

MISSING = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def test_create_approval_directly(authed, bot_a):
    response = await authed.post(
        "/api/approvals",
        json={
            "bot_id": str(bot_a.id),
            "risk": "send",
            "title": "Routine wants to send",
            "summary": "Step 3 of the nightly routine",
            "payload": {"kind": "message_only", "draft": "hello"},
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["risk"] == "send"
    assert body["payload"]["kind"] == "message_only"


async def test_create_approval_without_a_run_id(authed, bot_a):
    """Routine steps park approvals outside any chat run."""
    response = await authed.post(
        "/api/approvals", json={"bot_id": str(bot_a.id), "title": "no run"}
    )
    assert response.status_code == 201
    assert response.json()["run_id"] is None


async def test_get_approval(authed, make_approval, bot_a):
    approval = await make_approval(bot_a)
    response = await authed.get(f"/api/approvals/{approval.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(approval.id)


async def test_get_missing_approval_is_404(authed):
    response = await authed.get(f"/api/approvals/{MISSING}")
    assert response.status_code == 404
    assert response.json()["code"] == "approval_not_found"


async def test_list_approvals_defaults_to_pending(authed, make_approval, bot_a):
    pending = await make_approval(bot_a, title="still open")
    decided = await make_approval(bot_a, title="already done", status="approved")
    ids = {a["id"] for a in (await authed.get("/api/approvals")).json()}
    assert str(pending.id) in ids
    assert str(decided.id) not in ids


async def test_list_approvals_status_all(authed, make_approval, bot_a):
    decided = await make_approval(bot_a, status="approved")
    ids = {a["id"] for a in (await authed.get("/api/approvals?status=all")).json()}
    assert str(decided.id) in ids


async def test_list_approvals_filtered_by_bot(authed, make_approval, make_bot, user_a, bot_a):
    other_bot = await make_bot(user_a, name="second")
    mine = await make_approval(bot_a)
    theirs = await make_approval(other_bot)
    ids = {a["id"] for a in (await authed.get(f"/api/approvals?bot_id={bot_a.id}")).json()}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids


# ---------------------------------------------------------------------------
# Executing on approve — one test per documented `kind`
# ---------------------------------------------------------------------------


async def test_approving_a_connector_action_executes_it(authed, db, make_approval, bot_a):
    approval = await make_approval(
        bot_a,
        risk="send",
        payload={
            "kind": "connector_action",
            "connector_id": "microsoft_graph",
            "action": "send_mail",
            "input": {"to": "buyer@example.com", "subject": "Quote", "body": "Attached."},
        },
    )
    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved", "note": "go"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_at"]
    assert body["execution"]["ok"] is True
    assert body["execution"]["kind"] == "connector_action"
    assert body["execution"]["result"]["result"]["sent"] is True

    await db.refresh(approval)
    assert approval.execution["ok"] is True, "the outcome must be stamped on the row"


async def test_approving_an_mcp_tool_executes_it(authed, make_approval, make_mcp, bot_a, user_a):
    server = await make_mcp(
        user_a, name="Local MCP", transport="stdio", tool_allowlist=["lookup"]
    )
    await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}")

    approval = await make_approval(
        bot_a,
        risk="send",
        payload={"kind": "mcp_tool", "mcp_id": str(server.id), "tool": "lookup", "arguments": {"id": 1}},
    )
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert response.status_code == 200
    execution = response.json()["execution"]
    assert execution["ok"] is True
    assert execution["kind"] == "mcp_tool"
    assert execution["result"]["tool"] == "lookup"


async def test_approving_desktop_steps_executes_them(authed, make_approval, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")

    approval = await make_approval(
        bot_a,
        risk="delete",
        payload={"kind": "desktop_steps", "steps": [{"action": "click", "x": 5, "y": 6}]},
    )
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert response.status_code == 200
    execution = response.json()["execution"]
    assert execution["ok"] is True
    assert execution["kind"] == "desktop_steps"
    assert execution["results"][0]["action"] == "click"


async def test_approving_a_message_only_approval_posts_the_draft(
    authed, db, make_approval, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    approval = await make_approval(
        bot_a,
        risk="send",
        payload={"kind": "message_only", "draft": "Here is the reply.", "thread_id": str(thread.id)},
    )
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert response.status_code == 200
    execution = response.json()["execution"]
    assert execution["ok"] is True

    rows = await db.execute(select(Message).where(Message.thread_id == thread.id))
    posted = [m for m in rows.scalars().all() if m.content == "Here is the reply."]
    assert len(posted) == 1
    assert posted[0].meta["approval_id"] == str(approval.id)


async def test_an_unknown_kind_reports_a_failed_execution_not_a_500(authed, make_approval, bot_a):
    approval = await make_approval(bot_a, payload={"kind": "teleport"})
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert response.status_code == 200
    execution = response.json()["execution"]
    assert execution["ok"] is False
    assert "teleport" in execution["error"]


async def test_a_failing_side_effect_still_records_the_decision(authed, db, make_approval, bot_a):
    """The decision stands even when the held action cannot run."""
    approval = await make_approval(
        bot_a,
        payload={"kind": "connector_action", "connector_id": "microsoft_graph"},  # no action
    )
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["execution"]["ok"] is False


async def test_approving_writes_audit_events(authed, db, make_approval, bot_a):
    approval = await make_approval(
        bot_a,
        payload={
            "kind": "connector_action",
            "connector_id": "crm",
            "action": "search_accounts",
            "input": {"query": "acme"},
        },
    )
    await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    rows = await db.execute(select(AuditEvent).where(AuditEvent.bot_id == bot_a.id))
    kinds = {e.event_type for e in rows.scalars().all()}
    assert {"approval_executed", "approval_decision"} <= kinds


# ---------------------------------------------------------------------------
# Rejecting
# ---------------------------------------------------------------------------


async def test_rejecting_does_not_execute(authed, db, make_approval, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    approval = await make_approval(
        bot_a,
        payload={"kind": "message_only", "draft": "must never appear", "thread_id": str(thread.id)},
    )
    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "rejected", "note": "no"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert not body["execution"], "rejecting must leave the execution envelope empty"
    assert body["note"] == "no"

    rows = await db.execute(select(Message).where(Message.thread_id == thread.id))
    assert [m for m in rows.scalars().all() if m.content == "must never appear"] == []


async def test_decision_must_be_approved_or_rejected(authed, make_approval, bot_a):
    approval = await make_approval(bot_a)
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "maybe"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Idempotency of the gate
# ---------------------------------------------------------------------------


async def test_deciding_an_already_decided_approval_is_409(authed, make_approval, bot_a):
    approval = await make_approval(bot_a, payload={"kind": "message_only", "draft": "x"})
    first = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert first.status_code == 200

    second = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert second.status_code == 409
    assert second.json()["code"] == "approval_not_pending"


async def test_deciding_a_rejected_approval_is_409(authed, make_approval, bot_a):
    approval = await make_approval(bot_a, status="rejected")
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert response.status_code == 409


async def test_approving_twice_only_executes_once(
    authed, db, make_approval, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    approval = await make_approval(
        bot_a, payload={"kind": "message_only", "draft": "once only", "thread_id": str(thread.id)}
    )
    await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})

    rows = await db.execute(select(Message).where(Message.thread_id == thread.id))
    assert len([m for m in rows.scalars().all() if m.content == "once only"]) == 1


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


async def test_expire_marks_an_approval_expired(authed, make_approval, bot_a):
    approval = await make_approval(bot_a)
    response = await authed.post(f"/api/approvals/{approval.id}/expire")
    assert response.status_code == 200
    assert response.json()["status"] == "expired"
    assert response.json()["decided_at"]


async def test_expiring_a_decided_approval_is_409(authed, make_approval, bot_a):
    approval = await make_approval(bot_a, status="approved")
    response = await authed.post(f"/api/approvals/{approval.id}/expire")
    assert response.status_code == 409
    assert response.json()["code"] == "approval_not_pending"


async def test_an_expired_approval_can_no_longer_be_decided(authed, make_approval, bot_a):
    approval = await make_approval(bot_a)
    await authed.post(f"/api/approvals/{approval.id}/expire")
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})
    assert response.status_code == 409


async def test_deleting_a_thread_expires_its_pending_approvals(
    authed, db, make_approval, make_thread, make_run, user_a, bot_a
):
    """A pending approval is never silently dropped from the queue.

    Deleting the originating thread used to cascade the approval away. It now
    survives as an expired audit record: gone from the pending queue, still
    visible under `status=all`, and no longer decidable.
    """
    thread = await make_thread(user_a, [bot_a])
    run = await make_run(thread, bot_a)
    approval = await make_approval(bot_a, run=run, risk="send", requested_by=user_a)

    assert (await authed.delete(f"/api/threads/{thread.id}")).status_code == 200

    pending = {a["id"] for a in (await authed.get("/api/approvals")).json()}
    assert str(approval.id) not in pending

    listed = {a["id"]: a for a in (await authed.get("/api/approvals?status=all")).json()}
    assert str(approval.id) in listed
    assert listed[str(approval.id)]["status"] == "expired"

    rows = await db.execute(select(AuditEvent).where(AuditEvent.bot_id == bot_a.id))
    assert "approval_expired" in {e.event_type for e in rows.scalars().all()}
