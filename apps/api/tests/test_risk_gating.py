"""Risk gating: `send` / `spend` / `delete` never execute without a human.

Four paths reach an executor, and all four must gate identically:

1. a direct connector action,
2. a Bot Desktop action,
3. a routine step (inline execution, the default when Temporal is down),
4. an MCP tool call.

A caller may *declare* a risk, but only to escalate — a taught routine must
never be able to relabel `send_invoice` as `observe` and walk past the gate.

Path 4 was the hole. MCP tool calls were risk-gated on no execution path at
all: `POST /bots/{id}/mcp/{id}/call` reached `call_mcp_tool` directly, so a tool
named `send_invoice` ran unattended and left no undo-log entry either. It now
goes through `services.simulation.perform` like everything else, and is
classified by the same `classify_action_risk` the desktop lane uses.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import Approval, AuditEvent, Message
from app.services.connectors import requires_approval
from app.services.desktop import classify_action_risk, max_risk


async def _pending(db, bot):
    rows = await db.execute(
        select(Approval).where(Approval.bot_id == bot.id, Approval.status == "pending")
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Path 1 — a direct connector action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("connector_id", "action", "payload"),
    [
        ("microsoft_graph", "send_mail", {"to": "a@b.c", "subject": "s", "body": "b"}),
        ("ticketing", "send_reply", {"ticket_id": "t-1", "body": "b"}),
    ],
)
async def test_a_send_connector_action_is_held_not_executed(
    authed, db, bot_a, make_connector_binding, connector_id, action, payload
):
    await make_connector_binding(bot_a, connector_id, status="connected")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/{connector_id}/actions/{action}",
        json={"input": payload},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_approval"
    assert body["approval_id"]
    assert body["risk"] == "send"
    assert "result" not in body, "a held action must not report a result"

    held = await _pending(db, bot_a)
    assert len(held) == 1
    assert held[0].payload["kind"] == "connector_action"
    assert held[0].payload["action"] == action
    assert held[0].payload["input"] == payload


async def test_an_observe_connector_action_executes_immediately(authed, bot_a):
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/list_inbox",
        json={"input": {"top": 2}},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_a_mutate_connector_action_is_not_gated(authed, db, bot_a):
    """`mutate` is below the gate — only send/spend/delete need a human."""
    assert requires_approval("mutate") is False
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/actions/update_fields",
        json={"input": {"account_id": "acc_1", "fields": {"stage": "Won"}}},
    )
    assert response.status_code == 200
    assert await _pending(db, bot_a) == []


async def test_the_held_approval_carries_the_requester(authed, db, bot_a, user_a):
    await authed.post(
        f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/send_mail",
        json={"input": {"to": "a@b.c", "subject": "s", "body": "b"}},
    )
    held = await _pending(db, bot_a)
    assert held[0].payload["requested_by"] == str(user_a.id)


async def test_a_held_action_is_audited(authed, db, bot_a):
    await authed.post(
        f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/send_mail",
        json={"input": {"to": "a@b.c", "subject": "s", "body": "b"}},
    )
    rows = await db.execute(select(AuditEvent).where(AuditEvent.bot_id == bot_a.id))
    assert "action_held_for_approval" in {e.event_type for e in rows.scalars().all()}


async def test_an_unknown_connector_is_404_not_a_silent_execution(authed, bot_a):
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/does_not_exist/actions/send_mail", json={"input": {}}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "connector_not_found"


# ---------------------------------------------------------------------------
# Path 2 — a Bot Desktop action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected_risk"),
    [
        ("send_email", "send"),
        ("delete_downloads", "delete"),
        ("purchase_licence", "spend"),
        ("post_screenshot", "send"),
    ],
)
async def test_a_risky_desktop_action_is_held(authed, db, bot_a, action, expected_risk):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/desktop/action", json={"action": action, "x": 1, "y": 2}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_approval"
    assert body["risk"] == expected_risk

    held = await _pending(db, bot_a)
    assert len(held) == 1
    assert held[0].payload["kind"] == "desktop_steps"
    assert held[0].payload["steps"][0]["action"] == action


async def test_a_safe_desktop_action_runs(authed, db, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/desktop/action", json={"action": "click", "x": 3, "y": 4}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert await _pending(db, bot_a) == []


async def test_a_declared_risk_escalates_a_desktop_action_over_http(authed, db, bot_a):
    """The worker sends `risk` on desktop bodies; the API must honour it.

    `click` classifies as `observe`, so only the declared value can gate this.
    Without the field the declared risk was honoured on the inline path and
    silently dropped over HTTP - the per-executor divergence the single
    classifier exists to prevent, in a second place.
    """
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/desktop/action",
        json={"action": "click", "x": 1, "y": 2, "risk": "delete"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_approval"
    assert body["risk"] == "delete"

    held = await _pending(db, bot_a)
    assert len(held) == 1
    assert held[0].payload["steps"][0]["action"] == "click"
    assert "risk" not in held[0].payload["steps"][0], "the declared risk is not an action arg"


async def test_a_declared_risk_cannot_lower_a_desktop_action_over_http(authed, db, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/desktop/action",
        json={"action": "send_invoice", "risk": "observe"},
    )
    assert response.status_code == 201
    assert response.json()["risk"] == "spend"
    assert len(await _pending(db, bot_a)) == 1


async def test_a_structurally_safe_action_still_escalates_on_a_keyword():
    """`post_screenshot` matches ACTION_RISKS-adjacent naming but must gate."""
    assert classify_action_risk("post_screenshot") == "send"
    assert classify_action_risk("screenshot") == "observe"


# ---------------------------------------------------------------------------
# Path 3 — a routine step, executed inline because Temporal is down
# ---------------------------------------------------------------------------


def _requires_inline_runner() -> None:
    from app.routers.deps import optional_service

    service = optional_service("routines")
    if service is None or getattr(service, "run_inline", None) is None:
        pytest.skip("app.services.routines.run_inline is not available in this build")


async def test_routine_run_falls_back_to_inline_when_temporal_is_down(
    authed, make_routine, bot_a
):
    routine = await make_routine(bot_a, steps=[])
    response = await authed.post(f"/api/routines/{routine.id}/run")
    assert response.status_code == 200
    body = response.json()
    assert body["inline"] is True
    assert body["workflow_id"]


async def test_a_routine_desktop_step_cannot_declare_its_risk_down(
    authed, db, make_routine, bot_a
):
    """`{"action": "delete_file", "risk": "observe"}` must still be gated."""
    _requires_inline_runner()
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    routine = await make_routine(
        bot_a, steps=[{"type": "desktop", "action": "delete_file", "risk": "observe"}]
    )
    response = await authed.post(f"/api/routines/{routine.id}/run")
    assert response.status_code == 200

    held = await _pending(db, bot_a)
    assert len(held) == 1, "a declared-down risk walked past the approval gate"
    assert held[0].risk == "delete"
    assert held[0].payload["kind"] == "desktop_steps"


async def test_a_routine_desktop_step_can_declare_its_risk_up(authed, db, make_routine, bot_a):
    """`{"action": "click", "risk": "send"}` is honoured: escalation is allowed."""
    _requires_inline_runner()
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    routine = await make_routine(
        bot_a, steps=[{"type": "desktop", "action": "click", "risk": "send"}]
    )
    await authed.post(f"/api/routines/{routine.id}/run")

    held = await _pending(db, bot_a)
    assert len(held) == 1
    assert held[0].risk == "send"


async def test_a_routine_connector_step_cannot_declare_its_risk_down(
    authed, db, make_routine, bot_a
):
    """The connector manifest is authoritative; a declared risk can only raise it."""
    _requires_inline_runner()
    routine = await make_routine(
        bot_a,
        steps=[
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "send_mail",
                "risk": "observe",
                "input": {"to": "a@b.c", "subject": "s", "body": "b"},
            }
        ],
    )
    await authed.post(f"/api/routines/{routine.id}/run")

    held = await _pending(db, bot_a)
    assert len(held) == 1, "a routine step relabelled send_mail as observe and sent it"
    assert held[0].risk == "send"
    assert held[0].payload["kind"] == "connector_action"


async def test_a_routine_connector_step_can_declare_its_risk_up(
    authed, db, make_routine, bot_a
):
    """A read-only action declared `delete` is gated, even though the manifest says observe."""
    _requires_inline_runner()
    routine = await make_routine(
        bot_a,
        steps=[
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "list_inbox",
                "risk": "delete",
                "input": {"top": 1},
            }
        ],
    )
    await authed.post(f"/api/routines/{routine.id}/run")

    held = await _pending(db, bot_a)
    assert len(held) == 1
    assert held[0].risk == "delete"


async def test_a_safe_routine_step_runs_without_an_approval(authed, db, make_routine, bot_a):
    _requires_inline_runner()
    routine = await make_routine(
        bot_a,
        steps=[
            {
                "type": "connector",
                "connector_id": "crm",
                "action": "search_accounts",
                "input": {"query": "acme"},
            }
        ],
    )
    await authed.post(f"/api/routines/{routine.id}/run")
    assert await _pending(db, bot_a) == []


async def test_an_approval_raised_by_a_routine_is_scoped_to_the_triggering_human(
    authed, other, db, make_routine, bot_a, system_bot, user_a
):
    """A manual trigger has a human behind it; nobody else may decide its approvals."""
    _requires_inline_runner()
    routine = await make_routine(
        system_bot,
        steps=[
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "send_mail",
                "input": {"to": "a@b.c", "subject": "s", "body": "b"},
            }
        ],
    )
    await authed.post(f"/api/routines/{routine.id}/run")

    held = await _pending(db, system_bot)
    assert len(held) == 1
    assert held[0].payload.get("requested_by") == str(user_a.id)
    assert (await other.get(f"/api/approvals/{held[0].id}")).status_code == 404


async def test_routine_run_reports_awaiting_approval_when_a_step_is_held(
    authed, make_routine, bot_a
):
    """The UI must not be told `completed` for a routine parked on an approval."""
    _requires_inline_runner()
    routine = await make_routine(
        bot_a,
        steps=[
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "send_mail",
                "input": {"to": "a@b.c", "subject": "s", "body": "b"},
            }
        ],
    )
    response = await authed.post(f"/api/routines/{routine.id}/run")
    assert response.json()["status"] == "awaiting_approval"


def test_temporal_started_runs_can_be_attributed_to_the_triggering_human():
    """The Temporal path must scope approvals the same way the inline path does.

    Without a `user_id` on the workflow start, an approval raised against a shared
    system bot carries no `requested_by` and falls back to bot visibility, i.e.
    it becomes decidable by anyone who can see that system bot.
    """
    import inspect

    from app.services import temporal_client

    params = inspect.signature(temporal_client.start_routine_now).parameters
    accepts_user = "user_id" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    assert accepts_user, "start_routine_now must carry the triggering user into the workflow"


def test_the_temporal_workflow_argument_carries_the_requester(make_bot):
    """`routine_argument` is what the worker receives; the requester must be in it."""
    import uuid as _uuid

    from app.models import Routine
    from app.services import temporal_client

    triggering_user = _uuid.uuid4()
    routine = Routine(
        id=_uuid.uuid4(),
        bot_id=_uuid.uuid4(),
        name="r",
        steps=[],
        version=1,
        owner_user_id=_uuid.uuid4(),
    )
    argument = temporal_client.routine_argument(routine, user_id=str(triggering_user))
    assert argument["user_id"] == str(triggering_user), "the trigger must win over the owner"

    unattended = temporal_client.routine_argument(routine)
    assert unattended.get("user_id") == str(routine.owner_user_id), "fall back to the owner"


async def test_an_inline_run_falls_back_to_the_routine_owner_as_requester(
    authed, db, bot_a, system_bot
):
    """Even a routine created through the API carries an owner to scope approvals."""
    _requires_inline_runner()
    created = await authed.post(
        "/api/routines",
        json={
            "bot_id": str(system_bot.id),
            "name": "owned routine",
            "steps": [
                {
                    "type": "connector",
                    "connector_id": "microsoft_graph",
                    "action": "send_mail",
                    "input": {"to": "a@b.c", "subject": "s", "body": "b"},
                }
            ],
        },
    )
    assert created.status_code == 200
    routine_id = created.json()["id"]
    await authed.post(f"/api/routines/{routine_id}/run")

    held = await _pending(db, system_bot)
    assert len(held) == 1
    assert held[0].payload.get("requested_by"), "an approval with no requester is unscoped"


# ---------------------------------------------------------------------------
# The risk vocabulary itself
# ---------------------------------------------------------------------------


def test_the_gate_covers_exactly_send_spend_delete():
    assert [r for r in ("observe", "draft", "mutate", "send", "spend", "delete") if requires_approval(r)] == [
        "send",
        "spend",
        "delete",
    ]


def test_max_risk_picks_the_more_dangerous_label():
    assert max_risk("observe", "send") == "send"
    assert max_risk("send", "observe") == "send"
    assert max_risk("delete", "send", "spend") == "delete"
    assert max_risk("observe", "draft") == "draft"


def test_max_risk_treats_an_unknown_label_as_mutate():
    assert max_risk("observe", "banana") == "banana"  # unknown ranks as mutate > observe
    assert max_risk("delete", "banana") == "delete"


# ---------------------------------------------------------------------------
# The gate is not bypassable through the chat turn either
# ---------------------------------------------------------------------------


async def test_the_orchestrator_holds_a_send_instead_of_sending(
    authed, db, make_thread, user_a, bot_a, make_connector_binding
):
    await make_connector_binding(bot_a, "microsoft_graph", status="connected")
    thread = await make_thread(user_a, [bot_a])
    response = await authed.post(
        f"/api/threads/{thread.id}/messages",
        json={"content": "send the email to buyer@example.com please"},
    )
    assert response.status_code == 200
    assert response.json()["approval_id"], "an outbound send must raise an approval"

    held = await _pending(db, bot_a)
    assert len(held) == 1
    assert held[0].risk == "send"

    rows = await db.execute(select(Message).where(Message.thread_id == thread.id))
    assistant = [m for m in rows.scalars().all() if m.role == "assistant"]
    assert any("until you approve" in m.content for m in assistant)


# ---------------------------------------------------------------------------
# Path 4 — an MCP tool call
# ---------------------------------------------------------------------------


async def _attached_mcp(client, make_mcp, user, bot, **kwargs):
    server = await make_mcp(user, **kwargs)
    await client.post(f"/api/bots/{bot.id}/mcp/{server.id}")
    return server


@pytest.mark.parametrize(
    ("tool", "expected_risk"),
    [
        ("send_invoice", "spend"),
        ("purchase_seats", "spend"),
        ("send_message", "send"),
        ("delete_record", "delete"),
    ],
)
async def test_a_risky_mcp_tool_is_held_not_called(
    authed, db, bot_a, make_mcp, user_a, tool, expected_risk
):
    """The gap the competitive analysis calls excessive agency: MCP ran ungated."""
    server = await _attached_mcp(authed, make_mcp, user_a, bot_a, name="Billing MCP")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call",
        json={"tool": tool, "arguments": {"amount": 100}},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_approval"
    assert body["approval_id"]
    assert body["risk"] == expected_risk
    assert "result" not in body, "a held MCP call must not report a result"

    held = await _pending(db, bot_a)
    assert len(held) == 1
    assert held[0].payload["kind"] == "mcp_tool"
    assert held[0].payload["tool"] == tool
    assert held[0].payload["arguments"] == {"amount": 100}
    assert held[0].payload["mcp_id"] == str(server.id)


async def test_a_gated_mcp_call_writes_no_action_log_entry(
    authed, db, bot_a, make_mcp, user_a
):
    from app.models import ActionLog

    server = await _attached_mcp(authed, make_mcp, user_a, bot_a)
    await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "send_invoice"}
    )
    rows = await db.execute(select(ActionLog).where(ActionLog.bot_id == bot_a.id))
    assert list(rows.scalars().all()) == [], "a held call reached the executor"


async def test_a_safe_mcp_tool_runs_and_lands_in_the_undo_log(
    authed, db, bot_a, make_mcp, user_a
):
    from app.models import ActionLog

    # The allowlist must name the tool. It used to be optional: an empty list
    # short-circuited the guard and permitted everything, which is the defect
    # `test_mcp_registry_gating.py` now pins shut.
    server = await _attached_mcp(authed, make_mcp, user_a, bot_a, tool_allowlist=["echo"])
    response = await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call",
        json={"tool": "echo", "arguments": {"x": 1}},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert await _pending(db, bot_a) == []

    rows = await db.execute(select(ActionLog).where(ActionLog.bot_id == bot_a.id))
    entries = list(rows.scalars().all())
    assert len(entries) == 1, "the MCP call left no undo-log entry"
    assert entries[0].kind == "mcp"
    assert entries[0].action == "echo"
    assert entries[0].mcp_id == server.id
    assert entries[0].actor_user_id == user_a.id
    # Honest, not optimistic: MCP defines no inverse, and the row says so.
    assert entries[0].reversible is False
    assert entries[0].irreversible_reason


async def test_an_mcp_call_cannot_declare_its_risk_down(authed, db, bot_a, make_mcp, user_a):
    """`{"tool": "send_invoice", "risk": "observe"}` must still be gated."""
    server = await _attached_mcp(authed, make_mcp, user_a, bot_a)
    response = await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call",
        json={"tool": "send_invoice", "risk": "observe"},
    )
    assert response.status_code == 201
    assert response.json()["risk"] in ("send", "spend")
    assert len(await _pending(db, bot_a)) == 1


async def test_an_mcp_call_can_declare_its_risk_up(authed, db, bot_a, make_mcp, user_a):
    """A structurally harmless tool declared `delete` is gated. Escalation is allowed."""
    server = await _attached_mcp(authed, make_mcp, user_a, bot_a)
    response = await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call",
        json={"tool": "echo", "risk": "delete"},
    )
    assert response.status_code == 201
    assert response.json()["risk"] == "delete"
    held = await _pending(db, bot_a)
    assert len(held) == 1
    assert held[0].risk == "delete"


async def test_the_held_mcp_call_carries_the_requester(authed, db, bot_a, make_mcp, user_a):
    server = await _attached_mcp(authed, make_mcp, user_a, bot_a)
    await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "send_invoice"}
    )
    held = await _pending(db, bot_a)
    assert held[0].payload["requested_by"] == str(user_a.id)


async def test_a_held_mcp_call_is_audited(authed, db, bot_a, make_mcp, user_a):
    server = await _attached_mcp(authed, make_mcp, user_a, bot_a)
    await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "send_invoice"}
    )
    rows = await db.execute(select(AuditEvent).where(AuditEvent.bot_id == bot_a.id))
    assert "mcp_call_held" in {e.event_type for e in rows.scalars().all()}


async def test_an_approved_mcp_call_then_executes(authed, db, bot_a, make_mcp, user_a):
    """The whole point of the gate: held, then performed once a human says yes."""
    from app.models import ActionLog

    # Allowlisted explicitly: an empty allowlist now calls nothing, which is what
    # docs/connectors.md always promised and the code did not do.
    server = await _attached_mcp(
        authed, make_mcp, user_a, bot_a, name="Billing MCP", tool_allowlist=["send_invoice"]
    )
    held = await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call",
        json={"tool": "send_invoice", "arguments": {"amount": 100}},
    )
    approval_id = held.json()["approval_id"]

    decided = await authed.post(
        f"/api/approvals/{approval_id}/decide", json={"decision": "approved"}
    )
    assert decided.status_code == 200
    execution = decided.json()["execution"]
    assert execution["ok"] is True
    assert execution["result"]["tool"] == "send_invoice"
    assert execution["result"]["arguments"] == {"amount": 100}

    rows = await db.execute(select(ActionLog).where(ActionLog.bot_id == bot_a.id))
    entries = list(rows.scalars().all())
    assert len(entries) == 1
    assert entries[0].action == "send_invoice"
    assert entries[0].approval_id == uuid.UUID(approval_id)
    assert entries[0].risk in ("send", "spend")


async def test_a_rejected_mcp_call_never_runs(authed, db, bot_a, make_mcp, user_a):
    from app.models import ActionLog

    server = await _attached_mcp(authed, make_mcp, user_a, bot_a)
    held = await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "send_invoice"}
    )
    await authed.post(
        f"/api/approvals/{held.json()['approval_id']}/decide", json={"decision": "rejected"}
    )
    rows = await db.execute(select(ActionLog).where(ActionLog.bot_id == bot_a.id))
    assert list(rows.scalars().all()) == []


async def test_the_mcp_gate_shares_the_desktop_classifier():
    """One classifier, four paths. A second one would drift on the first new keyword."""
    for tool in ("send_invoice", "delete_record", "echo", "purchase_seats"):
        assert classify_action_risk(tool) == max_risk(classify_action_risk(tool), "observe")
    assert requires_approval(classify_action_risk("send_invoice")) is True
    assert requires_approval(classify_action_risk("echo")) is False


async def test_a_second_user_cannot_call_another_users_mcp_server(
    other, bot_a, make_mcp, user_a
):
    server = await make_mcp(user_a)
    response = await other.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "echo"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"
