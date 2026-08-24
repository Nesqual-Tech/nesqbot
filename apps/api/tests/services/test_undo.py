"""`app.services.undo` — the compensating-action log, and its honesty.

The tests are split the way the promise is:

* the **matrix** — every first-party action is classified, and nothing is left
  implicitly "unknown";
* the **compensators that work** — a created draft and a created task really are
  deleted, and prior CRM values really are written back;
* the **compensators that do not exist** — a sent email, a sent ticket reply and
  every desktop step record `reversible=False` with a reason a human can read.
  A silent no-op here would turn a known limitation into a false promise, which
  is the failure mode this module exists to avoid.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.models import ActionLog, AuditEvent, BotDesktop, Connector
from app.services import simulation, undo
from app.services.simulation import Effect

ENV_VAR = "NESQ_TEST_UNDO_SECRET"
TOKEN = "undo-token-never-logged-3c91af"

CRM_MANIFEST = {
    "base_url": "https://crm.test",
    "auth": "oauth2",
    "actions": [
        {
            "name": "update_fields",
            "method": "PATCH",
            "path": "/accounts/{account_id}",
            "body": {"fields": "{fields}"},
        },
        {
            "name": "create_task",
            "method": "POST",
            "path": "/accounts/{account_id}/tasks",
            "body": {"title": "{title}"},
        },
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def live_settings(monkeypatch):
    from types import SimpleNamespace

    from app.services import connectors as connectors_module
    from app.services import simulation as simulation_module
    from app.services import undo as undo_module

    state = SimpleNamespace(
        graph_api_base_url="https://graph.test/v1.0",
        request_timeout_seconds=5.0,
        connector_live_calls=True,
    )
    for module in (connectors_module, simulation_module, undo_module):
        monkeypatch.setattr(module, "get_settings", lambda: state)
    return state


@pytest.fixture
def bound_secret(monkeypatch):
    from app.services import secrets as secrets_module

    monkeypatch.setenv(ENV_VAR, TOKEN)
    secrets_module.reset_cache()
    yield f"env://{ENV_VAR}"
    secrets_module.reset_cache()


@pytest.fixture
def vendor(monkeypatch):
    """Install a recording mock transport on every client the app builds."""
    real_client = httpx.AsyncClient
    state = {"requests": [], "responder": lambda request: httpx.Response(200, json={})}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["responder"](request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


@pytest.fixture
async def actor(make_user):
    return await make_user()


@pytest.fixture
async def graph_bot(db, actor, make_bot, make_connector_binding, bound_secret):
    bot = await make_bot(actor)
    await make_connector_binding(
        bot, "microsoft_graph", status="connected", secret_ref=bound_secret
    )
    return bot


@pytest.fixture
async def crm_bot(db, actor, make_bot, make_connector_binding, bound_secret):
    bot = await make_bot(actor)
    await make_connector_binding(bot, "crm", status="connected", secret_ref=bound_secret)
    connector = await db.get(Connector, "crm")
    connector.manifest = CRM_MANIFEST
    await db.commit()
    return bot


async def only_entry(db, bot) -> ActionLog:
    rows = await db.execute(select(ActionLog).where(ActionLog.bot_id == bot.id))
    entries = list(rows.scalars().all())
    assert len(entries) == 1, f"expected exactly one undo-log entry, got {len(entries)}"
    return entries[0]


async def run_effect(db, bot, actor, **kwargs) -> ActionLog:
    outcome = await simulation.perform(
        db, Effect(bot_id=bot.id, actor_user_id=actor.id, **kwargs)
    )
    assert outcome.simulated is False
    return await only_entry(db, bot)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def test_every_first_party_action_is_classified():
    """An omission would read as "unknown", and unknown must never look safe."""
    from app.services.connectors import FIRST_PARTY

    missing = [
        (spec["id"], action["name"])
        for spec in FIRST_PARTY
        for action in spec["actions"]
        if (spec["id"], action["name"]) not in undo.UNDO_SPECS
    ]
    assert missing == []


@pytest.mark.parametrize(
    ("connector_id", "action"),
    [("microsoft_graph", "send_mail"), ("ticketing", "send_reply")],
)
def test_a_sent_thing_is_never_claimed_reversible(connector_id, action):
    spec = undo.UNDO_SPECS[(connector_id, action)]
    assert spec.reversible is False
    assert spec.reason


@pytest.mark.parametrize(
    ("connector_id", "action"),
    [
        ("microsoft_graph", "draft_reply"),
        ("crm", "update_fields"),
        ("crm", "create_task"),
        ("ticketing", "draft_reply"),
    ],
)
def test_the_reversible_four_all_describe_a_concrete_call(connector_id, action):
    spec = undo.UNDO_SPECS[(connector_id, action)]
    assert spec.reversible is True
    assert spec.strategy in ("http", "restore_fields")
    assert spec.description


def test_the_matrix_is_exposed_for_the_ui():
    rows = undo.reversibility_matrix()
    by_key = {(r["connector_id"], r["action"]): r for r in rows}
    assert by_key[("microsoft_graph", "send_mail")]["reversible"] is False
    assert by_key[("microsoft_graph", "send_mail")]["reason"]
    assert by_key[(None, "desktop")]["reversible"] is False
    assert by_key[(None, "mcp")]["reversible"] is False


def test_desktop_and_mcp_are_described_as_irreversible():
    desktop = undo.describe(kind="desktop", connector=None, connector_id=None, action="click")
    assert desktop.reversible is False
    assert "desktop" in desktop.reason
    tool = undo.describe(kind="mcp", connector=None, connector_id=None, action="lookup")
    assert tool.reversible is False
    assert "MCP" in tool.reason


def test_a_result_with_no_handle_is_not_reversible():
    """No draft id came back, so there is nothing to delete. Say so."""
    compensation = undo.describe(
        kind="connector",
        connector=None,
        connector_id="microsoft_graph",
        action="draft_reply",
        result={"state": "draft"},
    )
    assert compensation.reversible is False
    assert "no handle" in compensation.reason


# ---------------------------------------------------------------------------
# Recording — every executed effect lands in the log
# ---------------------------------------------------------------------------


async def test_a_sent_mail_is_logged_as_irreversible(db, graph_bot, actor, live_settings, vendor):
    vendor["responder"] = lambda request: httpx.Response(202, headers={"request-id": "req-1"})
    entry = await run_effect(
        db,
        graph_bot,
        actor,
        kind="connector",
        connector_id="microsoft_graph",
        action="send_mail",
        input_data={"to": "a@b.c", "subject": "Quote", "body": "hi"},
        pre_approved=True,
    )
    assert entry.ok is True
    assert entry.risk == "send"
    assert entry.reversible is False
    assert "sent" in (entry.irreversible_reason or "")
    assert entry.compensator == {}
    assert entry.actor_user_id == actor.id


async def test_a_desktop_step_is_logged_as_irreversible(db, actor, make_bot):
    bot = await make_bot(actor)
    db.add(BotDesktop(bot_id=bot.id, state="running", control_url="http://mock-control/u"))
    await db.commit()
    entry = await run_effect(
        db, bot, actor, kind="desktop", action="click", input_data={"x": 1, "y": 2}
    )
    assert entry.kind == "desktop"
    assert entry.reversible is False
    assert "keystroke" in (entry.irreversible_reason or "")


async def test_an_mcp_call_is_logged_as_irreversible(db, actor, make_bot, make_mcp):
    from app.services.mcp_registry import attach_mcp

    bot = await make_bot(actor)
    # Allowlisted: an empty allowlist now calls nothing (mcp_registry fail-closed).
    server = await make_mcp(transport="stdio", name="Undo MCP", tool_allowlist=["lookup"])
    await attach_mcp(db, bot.id, server.id)
    entry = await run_effect(
        db, bot, actor, kind="mcp", mcp_id=server.id, action="lookup", input_data={"id": 1}
    )
    assert entry.kind == "mcp"
    assert entry.reversible is False
    assert "MCP" in (entry.irreversible_reason or "")


async def test_the_log_entry_never_carries_the_resolved_credential(
    db, graph_bot, actor, live_settings, vendor
):
    vendor["responder"] = lambda request: httpx.Response(200, json={"id": "AAMdraft1"})
    entry = await run_effect(
        db,
        graph_bot,
        actor,
        kind="connector",
        connector_id="microsoft_graph",
        action="draft_reply",
        input_data={"message_id": "m1", "body": "thanks"},
    )
    rendered = json.dumps(
        {
            "input": entry.input_data,
            "result": entry.result_summary,
            "compensator": entry.compensator,
            "reason": entry.irreversible_reason,
        },
        default=str,
    )
    assert TOKEN not in rendered
    assert ENV_VAR not in rendered


# ---------------------------------------------------------------------------
# Compensators that genuinely undo
# ---------------------------------------------------------------------------


async def test_a_created_draft_is_deleted_by_its_compensator(
    db, graph_bot, actor, live_settings, vendor
):
    vendor["responder"] = lambda request: httpx.Response(200, json={"id": "AAMdraft1"})
    entry = await run_effect(
        db,
        graph_bot,
        actor,
        kind="connector",
        connector_id="microsoft_graph",
        action="draft_reply",
        input_data={"message_id": "m1", "body": "thanks"},
    )
    assert entry.reversible is True
    assert entry.target_ref == "AAMdraft1"
    assert entry.compensator["method"] == "DELETE"
    assert entry.compensator["path"] == "/me/messages/AAMdraft1"

    vendor["requests"].clear()
    vendor["responder"] = lambda request: httpx.Response(204)
    result = await undo.undo(db, entry.id, user=actor)

    assert result["ok"] is True
    assert len(vendor["requests"]) == 1
    sent = vendor["requests"][0]
    assert sent.method == "DELETE"
    assert str(sent.url) == "https://graph.test/v1.0/me/messages/AAMdraft1"


async def test_a_created_task_is_deleted_by_its_compensator(
    db, crm_bot, actor, live_settings, vendor
):
    vendor["responder"] = lambda request: httpx.Response(201, json={"task_id": "t_9"})
    entry = await run_effect(
        db,
        crm_bot,
        actor,
        kind="connector",
        connector_id="crm",
        action="create_task",
        input_data={"account_id": "acc_1", "title": "Call back"},
    )
    assert entry.reversible is True
    assert entry.compensator["path"] == "/tasks/t_9"

    vendor["requests"].clear()
    vendor["responder"] = lambda request: httpx.Response(204)
    result = await undo.undo(db, entry.id, user=actor)

    assert result["ok"] is True
    assert vendor["requests"][0].method == "DELETE"
    assert str(vendor["requests"][0].url) == "https://crm.test/tasks/t_9"


async def test_prior_crm_values_are_captured_before_the_write_and_restored(
    db, crm_bot, actor, live_settings, vendor
):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"fields": {"stage": "Qualified", "owner": "amy"}})
        return httpx.Response(200, json={"ok": True})

    vendor["responder"] = responder
    entry = await run_effect(
        db,
        crm_bot,
        actor,
        kind="connector",
        connector_id="crm",
        action="update_fields",
        input_data={"account_id": "acc_1", "fields": {"stage": "Won"}},
    )

    # The read happened first, and only the keys being written were captured.
    assert [r.method for r in vendor["requests"]] == ["GET", "PATCH"]
    assert entry.reversible is True
    assert entry.compensator["input"]["fields"] == {"stage": "Qualified"}

    vendor["requests"].clear()
    result = await undo.undo(db, entry.id, user=actor)
    assert result["ok"] is True
    assert vendor["requests"][0].method == "PATCH"
    assert json.loads(vendor["requests"][0].content.decode())["fields"] == {"stage": "Qualified"}


async def test_a_crm_write_with_no_readable_prior_state_is_not_reversible(db, actor, make_bot,
                                                                          make_connector_binding,
                                                                          bound_secret):
    """The deployment every customer has today: crm has no base URL, so no read.

    Recording `reversible=False` with the reason is the honest answer. Claiming
    a restore we cannot perform would be the dishonest one.
    """
    bot = await make_bot(actor)
    await make_connector_binding(bot, "crm", status="connected", secret_ref=bound_secret)
    entry = await run_effect(
        db,
        bot,
        actor,
        kind="connector",
        connector_id="crm",
        action="update_fields",
        input_data={"account_id": "acc_1", "fields": {"stage": "Won"}},
    )
    assert entry.reversible is False
    assert "no base URL" in (entry.irreversible_reason or "")
    assert entry.compensator == {}


async def test_the_prior_read_failing_is_reported_not_swallowed(
    db, crm_bot, actor, live_settings, vendor
):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, text="no such account")
        return httpx.Response(200, json={"ok": True})

    vendor["responder"] = responder
    entry = await run_effect(
        db,
        crm_bot,
        actor,
        kind="connector",
        connector_id="crm",
        action="update_fields",
        input_data={"account_id": "acc_1", "fields": {"stage": "Won"}},
    )
    assert entry.reversible is False
    assert "prior-state read failed" in (entry.irreversible_reason or "")


# ---------------------------------------------------------------------------
# Undoing is itself an action
# ---------------------------------------------------------------------------


async def test_undo_writes_an_audit_event(db, graph_bot, actor, live_settings, vendor):
    vendor["responder"] = lambda request: httpx.Response(200, json={"id": "AAMdraft2"})
    entry = await run_effect(
        db,
        graph_bot,
        actor,
        kind="connector",
        connector_id="microsoft_graph",
        action="draft_reply",
        input_data={"message_id": "m1", "body": "hi"},
    )
    vendor["responder"] = lambda request: httpx.Response(204)
    await undo.undo(db, entry.id, user=actor)

    rows = await db.execute(
        select(AuditEvent).where(
            AuditEvent.bot_id == graph_bot.id, AuditEvent.event_type == "action_undone"
        )
    )
    events = list(rows.scalars().all())
    assert len(events) == 1
    assert events[0].actor_user_id == actor.id
    assert events[0].detail["action_log_id"] == str(entry.id)


async def test_undo_refuses_to_run_twice(db, graph_bot, actor, live_settings, vendor):
    vendor["responder"] = lambda request: httpx.Response(200, json={"id": "AAMdraft3"})
    entry = await run_effect(
        db,
        graph_bot,
        actor,
        kind="connector",
        connector_id="microsoft_graph",
        action="draft_reply",
        input_data={"message_id": "m1", "body": "hi"},
    )
    vendor["responder"] = lambda request: httpx.Response(204)
    first = await undo.undo(db, entry.id, user=actor)
    vendor["requests"].clear()
    second = await undo.undo(db, entry.id, user=actor)

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["code"] == "already_undone"
    assert vendor["requests"] == [], "the second undo repeated the compensating call"


async def test_undoing_an_irreversible_action_refuses_with_the_reason(
    db, graph_bot, actor, live_settings, vendor
):
    vendor["responder"] = lambda request: httpx.Response(202, headers={"request-id": "r"})
    entry = await run_effect(
        db,
        graph_bot,
        actor,
        kind="connector",
        connector_id="microsoft_graph",
        action="send_mail",
        input_data={"to": "a@b.c", "subject": "s", "body": "b"},
        pre_approved=True,
    )
    result = await undo.undo(db, entry.id, user=actor)
    assert result["ok"] is False
    assert result["code"] == "not_reversible"
    assert "sent" in result["reason"]


async def test_undoing_an_unknown_entry_is_reported_not_raised(db, actor):
    import uuid

    result = await undo.undo(db, uuid.uuid4(), user=actor)
    assert result == {"ok": False, "code": "not_found", "error": "action log entry not found"}


async def test_a_failed_compensator_leaves_the_entry_undoable(
    db, graph_bot, actor, live_settings, vendor
):
    """A failed undo must stay retryable, and must not claim it succeeded."""
    vendor["responder"] = lambda request: httpx.Response(200, json={"id": "AAMdraft4"})
    entry = await run_effect(
        db,
        graph_bot,
        actor,
        kind="connector",
        connector_id="microsoft_graph",
        action="draft_reply",
        input_data={"message_id": "m1", "body": "hi"},
    )
    vendor["responder"] = lambda request: httpx.Response(404, text="gone")
    result = await undo.undo(db, entry.id, user=actor)

    assert result["ok"] is False
    await db.refresh(entry)
    assert entry.undone is False
    assert entry.undone_at is None
    assert entry.undo_result["ok"] is False

    rows = await db.execute(
        select(AuditEvent).where(AuditEvent.event_type == "action_undo_failed")
    )
    assert len(list(rows.scalars().all())) == 1


async def test_a_mocked_action_is_undone_by_a_mocked_compensator(db, actor, make_bot):
    """No base URL means the forward call touched nothing — and so does the undo.

    Saying "mock" out loud matters: the entry is genuinely reversible, but the
    result records that neither call reached a vendor.
    """
    bot = await make_bot(actor)
    entry = await run_effect(
        db,
        bot,
        actor,
        kind="connector",
        connector_id="crm",
        action="create_task",
        input_data={"account_id": "acc_1", "title": "Follow up"},
        pre_approved=True,
    )
    assert entry.reversible is True
    result = await undo.undo(db, entry.id, user=actor)
    assert result["ok"] is True
    assert result["result"]["mock"] is True


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


async def test_the_log_lists_what_can_still_be_taken_back(
    db, graph_bot, actor, live_settings, vendor
):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/createReply"):
            return httpx.Response(200, json={"id": "AAMdraft5"})
        return httpx.Response(202, headers={"request-id": "r"})

    vendor["responder"] = responder
    await simulation.perform(
        db,
        Effect(
            kind="connector",
            bot_id=graph_bot.id,
            connector_id="microsoft_graph",
            action="draft_reply",
            input_data={"message_id": "m1", "body": "hi"},
            actor_user_id=actor.id,
        ),
    )
    await simulation.perform(
        db,
        Effect(
            kind="connector",
            bot_id=graph_bot.id,
            connector_id="microsoft_graph",
            action="send_mail",
            input_data={"to": "a@b.c", "subject": "s", "body": "b"},
            pre_approved=True,
            actor_user_id=actor.id,
        ),
    )

    everything = await undo.list_action_log(db, bot_id=graph_bot.id)
    takeable = await undo.list_action_log(db, bot_id=graph_bot.id, reversible_only=True)
    assert len(everything) == 2
    assert [e.action for e in takeable] == ["draft_reply"]


async def test_an_approved_action_is_attributed_to_its_approval(
    db, graph_bot, actor, make_approval, live_settings, vendor
):
    from app.services.approvals import execute_approved

    vendor["responder"] = lambda request: httpx.Response(202, headers={"request-id": "r"})
    approval = await make_approval(
        graph_bot,
        risk="send",
        payload={
            "kind": "connector_action",
            "connector_id": "microsoft_graph",
            "action": "send_mail",
            "input": {"to": "a@b.c", "subject": "s", "body": "b"},
        },
    )
    outcome = await execute_approved(db, approval, actor)
    assert outcome["ok"] is True

    entry = await only_entry(db, graph_bot)
    assert entry.approval_id == approval.id
    assert entry.actor_user_id == actor.id
    assert entry.reversible is False
