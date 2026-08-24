"""`app.services.simulation` — the rehearsal, and the promise that it is one.

The load-bearing tests here are the two that justify the feature existing:

* **No side effects.** A dry run makes zero outbound HTTP requests and writes
  zero rows — proven against a routine whose *real* run does both.
* **One traversal.** The simulated and the real path visit the same steps, in
  the same order, with the same resolved inputs. If that ever stops being true
  the plan a human approved has stopped describing what will happen, which is
  worse than having no plan at all.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select, text

from app.models import BotDesktop
from app.services import simulation
from app.services.simulation import Effect, SimulationContext

ENV_VAR = "NESQ_TEST_SIMULATION_SECRET"
TOKEN = "sim-token-never-in-a-plan-71b3ee"

GRAPH_INBOX = {
    "value": [
        {
            "id": "AAMk1",
            "subject": "Invoice #4421",
            "bodyPreview": "Please find attached...",
            "from": {"emailAddress": {"address": "vendor@example.com"}},
        }
    ]
}

#: Every table a routine run can touch. A dry run must move none of them.
WATCHED_TABLES = (
    "runs",
    "approvals",
    "audit_events",
    "action_log",
    "plan_records",
    "messages",
    "bot_desktops",
    "cost_ledger",
    "memories",
)


async def row_counts(db) -> dict[str, int]:
    counts = {}
    for table in WATCHED_TABLES:
        result = await db.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
        counts[table] = int(result.scalar() or 0)
    return counts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def live_settings(monkeypatch):
    """Point every module that reads settings at a fake, live-enabled Graph."""
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
def outbound(monkeypatch):
    """Record every outbound HTTP request any client in the app would make.

    Patches `httpx.AsyncClient` itself rather than only the vendor transport,
    because the desktop sidecar and the MCP registry build their own clients.
    A dry run that reached any of them would show up here.
    """
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "graph.test" in request.url.host:
            return httpx.Response(200, json=GRAPH_INBOX)
        return httpx.Response(200, json={"ok": True})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


@pytest.fixture
async def wired_bot(db, make_user, make_bot, make_connector_binding, bound_secret):
    """A bot with a live Graph binding and a running (mock) desktop."""
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=bound_secret)
    db.add(
        BotDesktop(
            bot_id=bot.id,
            state="running",
            container_id="mock-sim",
            control_url="http://mock-control/sim",
        )
    )
    await db.commit()
    return user, bot


@pytest.fixture
async def mcp_server(db, make_mcp, make_user):
    from app.services.mcp_registry import attach_mcp

    # Allowlisted: an empty allowlist now calls nothing (mcp_registry fail-closed).
    server = await make_mcp(
        transport="stdio", name="Sim MCP", tool_allowlist=["safe_tool", "send_invoice", "lookup"]
    )
    return server, attach_mcp


# ---------------------------------------------------------------------------
# The context itself
# ---------------------------------------------------------------------------


def test_no_simulation_is_active_by_default():
    assert simulation.active_simulation() is None
    assert simulation.simulating() is False


def test_the_context_is_scoped_to_its_block():
    import uuid

    with SimulationContext(bot_id=uuid.uuid4()) as context:
        assert simulation.active_simulation() is context
        assert simulation.simulating() is True
    assert simulation.active_simulation() is None


def test_contexts_nest_without_leaking():
    import uuid

    outer_id, inner_id = uuid.uuid4(), uuid.uuid4()
    with SimulationContext(bot_id=outer_id) as outer:
        with SimulationContext(bot_id=inner_id) as inner:
            assert simulation.active_simulation() is inner
        assert simulation.active_simulation() is outer


async def test_execute_refuses_to_run_inside_a_simulation(db, make_user, make_bot):
    """The guard behind the guarantee: no bypass, only a loud failure."""
    user = await make_user()
    bot = await make_bot(user)
    effect = Effect(
        kind="connector",
        bot_id=bot.id,
        connector_id="microsoft_graph",
        action="list_inbox",
        input_data={"top": 1},
    )
    assessment = await simulation.assess(db, effect)
    with SimulationContext(bot_id=bot.id):
        with pytest.raises(RuntimeError, match="refusing to execute"):
            await simulation._execute(db, effect, assessment)


# ---------------------------------------------------------------------------
# No side effects — the claim the whole feature rests on
# ---------------------------------------------------------------------------


async def test_a_dry_run_makes_no_outbound_request_where_a_real_run_does(
    db, make_routine, wired_bot, live_settings, outbound
):
    from app.services.routines import run_inline

    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "list_inbox",
                "input": {"top": 1},
            }
        ],
    )

    plan = await simulation.dry_run_routine(db, routine)
    assert outbound == [], "a dry run reached the network"
    assert len(plan.calls) == 1

    await run_inline(db, routine)
    assert len(outbound) == 1, "the same routine run for real must call the vendor"
    assert outbound[0].url.host == "graph.test"


async def test_a_dry_run_writes_no_rows(db, make_routine, wired_bot, live_settings, outbound):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "connector_id": "microsoft_graph", "action": "list_inbox"},
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "send_mail",
                "input": {"to": "a@b.c", "subject": "s", "body": "b"},
            },
            {"type": "desktop", "action": "click", "args": {"x": 1, "y": 2}},
            {"type": "approval", "title": "Check this", "risk": "send"},
        ],
    )

    before = await row_counts(db)
    plan = await simulation.dry_run_routine(db, routine)
    after = await row_counts(db)

    assert before == after, "the simulated path wrote rows"
    assert len(plan.calls) == 4


async def test_only_the_plan_itself_is_persisted(db, make_routine, wired_bot, live_settings):
    _, bot = wired_bot
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "crm", "action": "search_accounts",
                     "input": {"query": "acme"}}]
    )
    plan = await simulation.dry_run_routine(db, routine)

    before = await row_counts(db)
    record = await simulation.save_plan(db, plan)
    after = await row_counts(db)

    assert after["plan_records"] == before["plan_records"] + 1
    assert {k: v for k, v in after.items() if k != "plan_records"} == {
        k: v for k, v in before.items() if k != "plan_records"
    }
    assert record.content_hash == plan.content_hash


async def test_a_gated_step_is_planned_not_held(db, make_routine, wired_bot, live_settings):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "send_mail",
                "input": {"to": "a@b.c", "subject": "s", "body": "b"},
            }
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)

    assert plan.calls[0].requires_approval is True
    assert plan.calls[0].risk == "send"
    assert plan.verdict["would_gate"] == [0]
    rows = await db.execute(
        text("SELECT count(*) FROM approvals WHERE bot_id = CAST(:b AS uuid)"),
        {"b": str(bot.id)},
    )
    assert int(rows.scalar() or 0) == 0


# ---------------------------------------------------------------------------
# One traversal
# ---------------------------------------------------------------------------


def _visited(effect: Effect) -> tuple:
    return (
        effect.step_index,
        effect.kind,
        effect.connector_id,
        str(effect.mcp_id) if effect.mcp_id else None,
        effect.action,
        tuple(sorted((str(k), str(v)) for k, v in (effect.input_data or {}).items())),
    )


async def test_the_simulated_and_real_paths_visit_the_same_steps(
    db, make_routine, wired_bot, mcp_server, live_settings, outbound, monkeypatch
):
    """The equivalence claim, asserted directly at the chokepoint."""
    from app.services.routines import run_inline

    _, bot = wired_bot
    server, attach = mcp_server
    await attach(db, bot.id, server.id)

    routine = await make_routine(
        bot,
        steps=[
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "list_inbox",
                "input": {"top": 3},
            },
            {
                "type": "connector",
                "connector_id": "crm",
                "action": "search_accounts",
                "input": {"query": "acme"},
            },
            {"type": "desktop", "action": "click", "args": {"x": 10, "y": 20}},
            {
                "type": "mcp",
                "mcp_id": str(server.id),
                "tool": "lookup",
                "arguments": {"id": 7},
            },
        ],
    )

    visited: list[tuple] = []
    original = simulation.perform

    async def spy(session, effect):
        visited.append(_visited(effect))
        return await original(session, effect)

    monkeypatch.setattr(simulation, "perform", spy)

    await simulation.dry_run_routine(db, routine)
    simulated = list(visited)
    visited.clear()

    outcome = await run_inline(db, routine)
    real = list(visited)

    assert outcome["status"] == "completed", outcome
    assert simulated == real, "the rehearsal and the real run disagree about what happens"
    assert [v[1] for v in simulated] == ["connector", "connector", "desktop", "mcp"]


async def test_the_plan_mirrors_the_traversal(db, make_routine, wired_bot, live_settings, outbound):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "connector_id": "crm", "action": "search_accounts",
             "input": {"query": "a"}},
            {"type": "connector", "connector_id": "crm", "action": "create_task",
             "input": {"account_id": "acc_1", "title": "Call back"}},
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert [c.step_index for c in plan.calls] == [0, 1]
    assert [c.action for c in plan.calls] == ["search_accounts", "create_task"]
    assert plan.calls[1].input_data == {"account_id": "acc_1", "title": "Call back"}


async def test_the_rehearsal_does_not_stop_at_the_first_problem(
    db, make_routine, wired_bot, live_settings
):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "connector_id": "microsoft_graph", "action": "draft_reply"},
            {"type": "connector", "connector_id": "crm", "action": "search_accounts",
             "input": {"query": "a"}},
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert len(plan.calls) == 2, "the plan stopped at the first failing step"
    assert plan.calls[0].problems
    assert plan.calls[1].ok is True


# ---------------------------------------------------------------------------
# Preflight is real
# ---------------------------------------------------------------------------


async def test_missing_required_input_is_caught_before_anything_runs(
    db, make_routine, wired_bot, live_settings
):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "draft_reply",
                "input": {"message_id": "m1"},
            }
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert plan.calls[0].problems == ("missing required input: body",)
    assert plan.verdict["ok"] is False
    assert plan.verdict["would_fail"] == [{"step_index": 0, "problems": ["missing required input: body"]}]


async def test_an_unknown_action_is_a_preflight_failure(db, make_routine, wired_bot, live_settings):
    _, bot = wired_bot
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "crm", "action": "teleport"}]
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert "has no action 'teleport'" in plan.calls[0].problems[0]


async def test_an_unregistered_connector_is_a_preflight_failure(
    db, make_routine, make_user, make_bot
):
    user = await make_user()
    bot = await make_bot(user)
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "nope", "action": "x"}]
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert "is not registered" in plan.calls[0].problems[0]


async def test_an_unbound_connector_fails_preflight_for_a_risky_action(
    db, make_routine, make_user, make_bot
):
    """Observe and draft mock without a binding; anything riskier does not."""
    user = await make_user()
    bot = await make_bot(user)
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "connector_id": "crm", "action": "update_fields",
             "input": {"account_id": "a", "fields": {"stage": "won"}}},
            {"type": "connector", "connector_id": "crm", "action": "search_accounts",
             "input": {"query": "a"}},
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert "is not connected for this bot" in plan.calls[0].problems[0]
    assert plan.calls[1].problems == ()
    assert any("mock data" in note for note in plan.calls[1].notes)


async def test_a_desktop_step_fails_preflight_when_the_desktop_is_not_running(
    db, make_routine, make_user, make_bot
):
    user = await make_user()
    bot = await make_bot(user)
    routine = await make_routine(bot, steps=[{"type": "desktop", "action": "click"}])
    plan = await simulation.dry_run_routine(db, routine)
    assert "desktop" in plan.calls[0].problems[0]
    assert plan.calls[0].ok is False


async def test_an_unattached_mcp_server_fails_preflight(db, make_routine, wired_bot, make_mcp):
    _, bot = wired_bot
    server = await make_mcp(transport="stdio", name="Detached")
    routine = await make_routine(
        bot, steps=[{"type": "mcp", "mcp_id": str(server.id), "tool": "lookup"}]
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert "is not attached to this bot" in plan.calls[0].problems[0]


async def test_a_tool_off_the_allowlist_fails_preflight(db, make_routine, wired_bot, make_mcp):
    from app.services.mcp_registry import attach_mcp

    _, bot = wired_bot
    server = await make_mcp(transport="stdio", name="Narrow", tool_allowlist=["safe_tool"])
    await attach_mcp(db, bot.id, server.id)
    routine = await make_routine(
        bot, steps=[{"type": "mcp", "mcp_id": str(server.id), "tool": "dangerous"}]
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert "allowlist" in plan.calls[0].problems[0]


async def test_a_malformed_step_still_appears_in_the_plan(db, make_routine, wired_bot):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "action": "send_mail"},
            {"type": "connector", "connector_id": "crm", "action": "search_accounts",
             "input": {"query": "a"}},
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert len(plan.calls) == 2, "a step the dispatcher rejected vanished from the plan"
    assert "needs connector_id and action" in plan.calls[0].problems[0]


async def test_the_driver_seam_is_reported(db, make_routine, wired_bot, live_settings):
    _, bot = wired_bot
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "microsoft_graph", "action": "list_inbox"}]
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert any("call the vendor for real" in note for note in plan.calls[0].notes)


async def test_an_unsupported_action_reports_the_mock_path(
    db, make_routine, make_user, make_bot, make_connector_binding, bound_secret, live_settings
):
    """crm has a driver but no base URL, so it mocks — and the plan says so."""
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "crm", status="connected", secret_ref=bound_secret)
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "crm", "action": "search_accounts",
                     "input": {"query": "a"}}]
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert any("mock" in note for note in plan.calls[0].notes)


# ---------------------------------------------------------------------------
# The credential is resolve-checked, never fetched
# ---------------------------------------------------------------------------


async def test_a_set_environment_reference_resolves(db, wired_bot):
    _, bot = wired_bot
    check = await simulation.check_binding(db, bot.id, "microsoft_graph")
    assert check.bound is True
    assert check.has_reference is True
    assert check.resolves is True


async def test_an_unset_environment_reference_does_not_resolve(
    db, make_user, make_bot, make_connector_binding
):
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(
        bot, "crm", status="connected", secret_ref="env://NESQ_DEFINITELY_UNSET_VAR"
    )
    check = await simulation.check_binding(db, bot.id, "crm")
    assert check.resolves is False


async def test_a_key_vault_reference_is_reported_as_unverified(
    db, make_user, make_bot, make_connector_binding
):
    """Tri-state on purpose: a dry run must not fetch the value to find out."""
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(
        bot, "crm", status="connected", secret_ref="kv://a-vault/a-secret"
    )
    check = await simulation.check_binding(db, bot.id, "crm")
    assert check.resolves is None
    assert "does not fetch" in check.note


async def test_an_unbound_connector_reports_no_binding(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    check = await simulation.check_binding(db, bot.id, "crm")
    assert check.bound is False
    assert check.resolves is None


async def test_no_resolved_credential_appears_anywhere_in_a_plan(
    db, make_routine, wired_bot, live_settings
):
    """The runtime half of the leak audit, for the plan."""
    import json

    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "connector_id": "microsoft_graph", "action": "list_inbox"},
            {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "send_mail",
                "input": {"to": "a@b.c", "subject": "s", "body": "b"},
            },
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    rendered = json.dumps(plan.as_dict(), default=str)
    assert TOKEN not in rendered
    assert ENV_VAR not in rendered

    record = await simulation.save_plan(db, plan)
    assert TOKEN not in json.dumps(record.plan, default=str)


# ---------------------------------------------------------------------------
# dry_run_action
# ---------------------------------------------------------------------------


async def test_dry_run_action_plans_one_call(db, wired_bot, live_settings, outbound):
    _, bot = wired_bot
    plan = await simulation.dry_run_action(
        db,
        bot_id=bot.id,
        connector_id="microsoft_graph",
        action="send_mail",
        input_data={"to": "a@b.c", "subject": "Quote", "body": "hi"},
    )
    assert outbound == []
    assert len(plan.calls) == 1
    call = plan.calls[0]
    assert call.connector_id == "microsoft_graph"
    assert call.action == "send_mail"
    assert call.risk == "send"
    assert call.requires_approval is True
    assert call.reversible is False
    assert "sent" in call.undo_note
    assert plan.routine_id is None


async def test_dry_run_action_catches_a_missing_input(db, wired_bot, live_settings):
    _, bot = wired_bot
    plan = await simulation.dry_run_action(
        db, bot_id=bot.id, connector_id="microsoft_graph", action="send_mail", input_data={"to": "a@b.c"}
    )
    assert plan.calls[0].problems == ("missing required input: subject, body",)


async def test_a_plan_summary_line_is_human_readable(db, wired_bot, live_settings):
    _, bot = wired_bot
    plan = await simulation.dry_run_action(
        db,
        bot_id=bot.id,
        connector_id="microsoft_graph",
        action="send_mail",
        input_data={"to": "a@b.c", "subject": "s", "body": "b"},
    )
    summary = plan.calls[0].summary
    assert "microsoft_graph.send_mail" in summary
    assert "risk=send" in summary
    assert "needs approval" in summary


# ---------------------------------------------------------------------------
# The plan record and the drift check
# ---------------------------------------------------------------------------


async def test_the_content_hash_is_stable_across_two_dry_runs(
    db, make_routine, wired_bot, live_settings
):
    _, bot = wired_bot
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "crm", "action": "search_accounts",
                     "input": {"query": "a"}}]
    )
    first = await simulation.dry_run_routine(db, routine)
    second = await simulation.dry_run_routine(db, routine)
    assert first.content_hash == second.content_hash


async def test_the_content_hash_moves_when_the_input_changes(db, make_routine, wired_bot):
    _, bot = wired_bot
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "crm", "action": "search_accounts",
                     "input": {"query": "a"}}]
    )
    before = (await simulation.dry_run_routine(db, routine)).content_hash
    routine.steps = [
        {"type": "connector", "connector_id": "crm", "action": "search_accounts",
         "input": {"query": "b"}}
    ]
    await db.commit()
    after = (await simulation.dry_run_routine(db, routine)).content_hash
    assert before != after


async def test_a_saved_plan_executes_exactly_it(db, make_routine, wired_bot, live_settings, outbound):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "connector_id": "microsoft_graph", "action": "list_inbox",
             "input": {"top": 1}}
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    record = await simulation.save_plan(db, plan)
    assert outbound == []

    result = await simulation.execute_plan(db, record, user=None)
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert len(outbound) == 1
    await db.refresh(record)
    assert record.status == "executed"
    assert record.executed_run_id is not None


async def test_an_executed_plan_refuses_to_run_twice(db, make_routine, wired_bot, live_settings, outbound):
    _, bot = wired_bot
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "crm", "action": "search_accounts",
                     "input": {"query": "a"}}]
    )
    record = await simulation.save_plan(db, await simulation.dry_run_routine(db, routine))
    await simulation.execute_plan(db, record)
    again = await simulation.execute_plan(db, record)
    assert again["ok"] is False
    assert again["code"] == "already_executed"


async def test_a_drifted_plan_refuses_to_execute(db, make_routine, wired_bot, live_settings, outbound):
    """Approving a plan must not authorise whatever the routine says later."""
    _, bot = wired_bot
    routine = await make_routine(
        bot, steps=[{"type": "connector", "connector_id": "crm", "action": "search_accounts",
                     "input": {"query": "a"}}]
    )
    record = await simulation.save_plan(db, await simulation.dry_run_routine(db, routine))

    routine.steps = [
        {"type": "connector", "connector_id": "microsoft_graph", "action": "send_mail",
         "input": {"to": "a@b.c", "subject": "s", "body": "b"}}
    ]
    await db.commit()

    result = await simulation.execute_plan(db, record)
    assert result["ok"] is False
    assert result["code"] == "plan_drifted"
    await db.refresh(record)
    assert record.status == "stale"
    assert outbound == []


async def test_an_ad_hoc_plan_executes_without_a_routine_row(db, wired_bot, live_settings, outbound):
    _, bot = wired_bot
    plan = await simulation.dry_run_action(
        db, bot_id=bot.id, connector_id="microsoft_graph", action="list_inbox", input_data={"top": 2}
    )
    record = await simulation.save_plan(db, plan)
    assert record.routine_id is None

    result = await simulation.execute_plan(db, record)
    assert result["ok"] is True
    assert len(outbound) == 1


async def test_plans_are_listable_per_bot(db, wired_bot, live_settings):
    _, bot = wired_bot
    plan = await simulation.dry_run_action(
        db, bot_id=bot.id, connector_id="crm", action="search_accounts", input_data={"query": "a"}
    )
    await simulation.save_plan(db, plan)
    rows = await simulation.list_plans(db, bot_id=bot.id)
    assert len(rows) == 1
    assert rows[0].bot_id == bot.id


async def test_the_verdict_counts_what_a_reviewer_needs(db, make_routine, wired_bot, live_settings):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "connector_id": "crm", "action": "search_accounts",
             "input": {"query": "a"}},
            {"type": "connector", "connector_id": "microsoft_graph", "action": "send_mail",
             "input": {"to": "a@b.c", "subject": "s", "body": "b"}},
            {"type": "connector", "connector_id": "microsoft_graph", "action": "draft_reply"},
        ],
    )
    verdict = (await simulation.dry_run_routine(db, routine)).verdict
    assert verdict["steps_total"] == 3
    assert verdict["would_gate"] == [1]
    assert [f["step_index"] for f in verdict["would_fail"]] == [2]
    assert verdict["would_execute"] == 1
    assert verdict["ok"] is False


async def test_the_plan_previews_reversibility(db, make_routine, wired_bot, live_settings):
    _, bot = wired_bot
    routine = await make_routine(
        bot,
        steps=[
            {"type": "connector", "connector_id": "crm", "action": "create_task",
             "input": {"account_id": "a1", "title": "Follow up"}},
            {"type": "connector", "connector_id": "microsoft_graph", "action": "send_mail",
             "input": {"to": "a@b.c", "subject": "s", "body": "b"}},
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    assert plan.calls[0].reversible is True
    assert plan.calls[1].reversible is False
    assert plan.verdict["reversible"] == [0]


# ---------------------------------------------------------------------------
# assess() on its own
# ---------------------------------------------------------------------------


async def test_a_declared_risk_can_only_raise(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    lowered = await simulation.assess(
        db,
        Effect(
            kind="connector",
            bot_id=bot.id,
            connector_id="microsoft_graph",
            action="send_mail",
            input_data={"to": "a", "subject": "b", "body": "c"},
            declared_risk="observe",
        ),
    )
    assert lowered.risk == "send"
    assert lowered.requires_approval is True

    raised = await simulation.assess(
        db,
        Effect(
            kind="connector",
            bot_id=bot.id,
            connector_id="microsoft_graph",
            action="list_inbox",
            declared_risk="delete",
        ),
    )
    assert raised.risk == "delete"
    assert raised.requires_approval is True


async def test_pre_approved_effects_do_not_gate_again(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    assessment = await simulation.assess(
        db,
        Effect(
            kind="connector",
            bot_id=bot.id,
            connector_id="microsoft_graph",
            action="send_mail",
            input_data={"to": "a", "subject": "b", "body": "c"},
            pre_approved=True,
        ),
    )
    assert assessment.risk == "send"
    assert assessment.requires_approval is False


async def test_an_unknown_effect_kind_is_reported_not_raised_(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    assessment = await simulation.assess(db, Effect(kind="astral", bot_id=bot.id, action="x"))
    assert assessment.problems == ("unknown effect kind 'astral'",)


async def test_an_unknown_effect_kind_is_reported_not_raised(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    assessment = await simulation.assess(db, Effect(kind="telepathy", bot_id=bot.id, action="x"))
    assert assessment.problems == ("unknown effect kind 'telepathy'",)


# ---------------------------------------------------------------------------
# The MCP gate
#
# A gate that exists on one execution path and not another is not a gate. MCP
# tool names are classified by the same `classify_action_risk` the desktop uses,
# a declared risk may only raise the result, and a gated call is held rather
# than run. The whole cycle is exercised here: classified -> held -> approved ->
# executed, on the inline routine path.
# ---------------------------------------------------------------------------


@pytest.fixture
async def http_mcp(db, make_mcp):
    """An MCP server whose tool calls are real HTTP, so 'did not run' is provable."""
    return await make_mcp(
        transport="http",
        endpoint="http://mcp.test",
        name="Remote MCP",
        tool_allowlist=["safe_tool", "send_invoice", "lookup"],
    )


async def test_the_mcp_classifier_is_the_shared_one(db, wired_bot, http_mcp):
    """No second classifier: the tool name goes through `services.risk`."""
    from app.services import risk as risk_module
    from app.services.mcp_registry import attach_mcp

    _, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    for tool in ("send_invoice", "delete_record", "lookup", "screenshot"):
        assessment = await simulation.assess(
            db, Effect(kind="mcp", bot_id=bot.id, mcp_id=http_mcp.id, action=tool)
        )
        assert assessment.risk == risk_module.classify_action_risk(tool)


async def test_a_risky_mcp_tool_name_is_gated(db, wired_bot, http_mcp):
    from app.services.mcp_registry import attach_mcp

    _, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    assessment = await simulation.assess(
        db, Effect(kind="mcp", bot_id=bot.id, mcp_id=http_mcp.id, action="send_invoice")
    )
    assert assessment.risk == "spend"
    assert assessment.requires_approval is True
    assert any("held for a human" in note for note in assessment.notes)


async def test_an_ordinary_mcp_tool_name_is_not_gated(db, wired_bot, http_mcp):
    from app.services.mcp_registry import attach_mcp

    _, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    assessment = await simulation.assess(
        db, Effect(kind="mcp", bot_id=bot.id, mcp_id=http_mcp.id, action="lookup")
    )
    assert assessment.risk == "mutate"
    assert assessment.requires_approval is False


async def test_a_declared_risk_cannot_lower_an_mcp_classification(db, wired_bot, http_mcp):
    """`{"tool": "send_invoice", "risk": "observe"}` must still be gated."""
    from app.services.mcp_registry import attach_mcp

    _, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    assessment = await simulation.assess(
        db,
        Effect(
            kind="mcp",
            bot_id=bot.id,
            mcp_id=http_mcp.id,
            action="send_invoice",
            declared_risk="observe",
        ),
    )
    assert assessment.risk == "spend"
    assert assessment.requires_approval is True


async def test_a_declared_risk_can_raise_an_mcp_classification(db, wired_bot, http_mcp):
    from app.services.mcp_registry import attach_mcp

    _, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    assessment = await simulation.assess(
        db,
        Effect(
            kind="mcp", bot_id=bot.id, mcp_id=http_mcp.id, action="lookup", declared_risk="delete"
        ),
    )
    assert assessment.risk == "delete"
    assert assessment.requires_approval is True


async def test_an_approved_mcp_effect_does_not_gate_again(db, wired_bot, http_mcp):
    from app.services.mcp_registry import attach_mcp

    _, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    assessment = await simulation.assess(
        db,
        Effect(
            kind="mcp",
            bot_id=bot.id,
            mcp_id=http_mcp.id,
            action="send_invoice",
            pre_approved=True,
        ),
    )
    assert assessment.risk == "spend"
    assert assessment.requires_approval is False


async def test_a_gated_mcp_step_is_held_and_not_called(db, make_routine, wired_bot, http_mcp, outbound):
    """The end-to-end claim: held instead of executed, on the inline path."""
    from app.models import Approval
    from app.services.mcp_registry import attach_mcp
    from app.services.routines import run_inline

    user, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    routine = await make_routine(
        bot,
        steps=[
            {
                "type": "mcp",
                "mcp_id": str(http_mcp.id),
                "tool": "send_invoice",
                "arguments": {"account_id": "acc_1"},
            }
        ],
    )

    outcome = await run_inline(db, routine, user=user)
    assert outcome["status"] == "awaiting_approval"
    assert outbound == [], "a gated MCP tool was called anyway"

    rows = await db.execute(select(Approval).where(Approval.bot_id == bot.id))
    approvals = list(rows.scalars().all())
    assert len(approvals) == 1
    held = approvals[0]
    assert held.risk == "spend"
    assert held.payload["kind"] == "mcp_tool"
    assert held.payload["mcp_id"] == str(http_mcp.id)
    assert held.payload["tool"] == "send_invoice"
    assert held.payload["arguments"] == {"account_id": "acc_1"}
    assert held.payload["requested_by"] == str(user.id)


async def test_an_mcp_routine_step_carries_its_declared_risk_inline(
    db, make_routine, wired_bot, http_mcp, outbound
):
    """A step's declared risk must escalate on the inline path too.

    The desktop and connector branches of `_step_effect` pass `declared_risk`;
    the mcp branch did not, so escalate-only held over HTTP (the worker forwards
    `risk` on the wire) but not inline - the same per-executor divergence the
    single-classifier rule exists to prevent. `lookup` classifies as `observe`,
    so only the declared value can gate this step.
    """
    from app.models import Approval
    from app.services.mcp_registry import attach_mcp
    from app.services.routines import run_inline

    user, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    routine = await make_routine(
        bot,
        steps=[
            {
                "type": "mcp",
                "mcp_id": str(http_mcp.id),
                "tool": "lookup",
                "arguments": {"q": "acme"},
                "risk": "delete",
            }
        ],
    )

    outcome = await run_inline(db, routine, user=user)
    assert outcome["status"] == "awaiting_approval"
    assert outbound == [], "a step declaring `delete` was executed without a hold"

    rows = await db.execute(select(Approval).where(Approval.bot_id == bot.id))
    held = list(rows.scalars().all())
    assert len(held) == 1
    assert held[0].risk == "delete"


async def test_an_mcp_routine_step_cannot_declare_its_risk_down_inline(
    db, make_routine, wired_bot, http_mcp, outbound
):
    """The other direction: forwarding the declared risk must not let it lower."""
    from app.services.mcp_registry import attach_mcp
    from app.services.routines import run_inline

    user, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    routine = await make_routine(
        bot,
        steps=[
            {
                "type": "mcp",
                "mcp_id": str(http_mcp.id),
                "tool": "send_invoice",
                "arguments": {"account_id": "acc_1"},
                "risk": "observe",
            }
        ],
    )

    outcome = await run_inline(db, routine, user=user)
    assert outcome["status"] == "awaiting_approval"
    assert outbound == [], "a declared `observe` lowered a `spend` classification"


async def test_an_approved_mcp_call_then_executes(db, make_routine, wired_bot, http_mcp, outbound):
    from app.models import Approval
    from app.services.approvals import execute_approved
    from app.services.mcp_registry import attach_mcp
    from app.services.routines import run_inline

    user, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    routine = await make_routine(
        bot,
        steps=[
            {"type": "mcp", "mcp_id": str(http_mcp.id), "tool": "send_invoice",
             "arguments": {"account_id": "acc_1"}}
        ],
    )
    await run_inline(db, routine, user=user)
    rows = await db.execute(select(Approval).where(Approval.bot_id == bot.id))
    held = rows.scalars().one()

    result = await execute_approved(db, held, user)

    assert result["ok"] is True, result
    assert len(outbound) == 1, "the approved MCP tool did not run"
    assert outbound[0].method == "POST"
    assert str(outbound[0].url) == "http://mcp.test/tools/call"


async def test_a_gated_mcp_step_is_visible_in_a_plan(db, make_routine, wired_bot, http_mcp):
    from app.services.mcp_registry import attach_mcp

    _, bot = wired_bot
    await attach_mcp(db, bot.id, http_mcp.id)
    routine = await make_routine(
        bot,
        steps=[
            {"type": "mcp", "mcp_id": str(http_mcp.id), "tool": "delete_record",
             "arguments": {"id": 4}}
        ],
    )
    plan = await simulation.dry_run_routine(db, routine)
    call = plan.calls[0]
    assert call.kind == "mcp"
    assert call.risk == "delete"
    assert call.requires_approval is True
    assert plan.verdict["would_gate"] == [0]
    assert any("held for a human" in note for note in call.notes)
    assert not any("unattended" in note for note in call.notes)
