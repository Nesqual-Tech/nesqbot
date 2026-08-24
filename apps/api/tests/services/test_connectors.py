"""`app.services.connectors` — risk lookup, the gate, and input validation."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Connector
from app.services.connectors import (
    FIRST_PARTY,
    action_risk,
    action_spec,
    execute_connector_action,
    list_connectors,
    requires_approval,
    validate_action_input,
)


@pytest.fixture
async def graph(db):
    return await db.get(Connector, "microsoft_graph")


# ---------------------------------------------------------------------------
# requires_approval — the gate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("risk", ["send", "spend", "delete"])
def test_risky_classes_require_approval(risk):
    assert requires_approval(risk) is True


@pytest.mark.parametrize("risk", ["observe", "draft", "mutate", "", "unknown", "SEND", "Send"])
def test_everything_else_does_not(risk):
    assert requires_approval(risk) is False


# ---------------------------------------------------------------------------
# action_risk / action_spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [("list_inbox", "observe"), ("draft_reply", "draft"), ("send_mail", "send")],
)
async def test_action_risk_reads_the_manifest(graph, action, expected):
    assert action_risk(graph, action) == expected


async def test_an_unknown_action_falls_back_to_the_connector_default(graph):
    assert action_risk(graph, "no_such_action") == graph.risk_default


async def test_action_spec_returns_none_for_an_unknown_action(graph):
    assert action_spec(graph, "list_inbox") is not None
    assert action_spec(graph, "no_such_action") is None


async def test_every_first_party_send_action_is_gated(db):
    for connector in await list_connectors(db):
        for action in connector.actions or []:
            risk = action.get("risk", connector.risk_default)
            if action["name"].startswith("send"):
                assert requires_approval(risk), f"{connector.id}.{action['name']} is not gated"


def test_the_first_party_catalog_declares_a_risk_for_every_action():
    for spec in FIRST_PARTY:
        for action in spec["actions"]:
            assert "risk" in action, f"{spec['id']}.{action['name']} has no risk"
            assert action["risk"] in ("observe", "draft", "mutate", "send", "spend", "delete")


# ---------------------------------------------------------------------------
# validate_action_input
# ---------------------------------------------------------------------------


async def test_validate_reports_every_missing_required_key(graph):
    assert validate_action_input(graph, "send_mail", {}) == ["to", "subject", "body"]


async def test_validate_treats_blank_strings_as_missing(graph):
    assert validate_action_input(
        graph, "send_mail", {"to": "  ", "subject": "", "body": "ok"}
    ) == ["to", "subject"]


async def test_validate_accepts_a_complete_payload(graph):
    assert validate_action_input(graph, "send_mail", {"to": "a@b.c", "subject": "s", "body": "b"}) == []


async def test_validate_ignores_extra_keys(graph):
    assert validate_action_input(
        graph, "send_mail", {"to": "a@b.c", "subject": "s", "body": "b", "cc": "x"}
    ) == []


async def test_validate_on_an_action_with_no_schema_is_permissive(graph):
    assert validate_action_input(graph, "list_inbox", {}) == []


async def test_validate_on_an_unknown_action_returns_nothing_to_report(graph):
    assert validate_action_input(graph, "no_such_action", {}) == []


async def test_validate_handles_a_none_payload(graph):
    assert validate_action_input(graph, "send_mail", None) == ["to", "subject", "body"]


async def test_a_non_string_required_value_counts_as_present(db):
    connector = await db.get(Connector, "crm")
    assert validate_action_input(connector, "update_fields", {"account_id": 7, "fields": {}}) == []


# ---------------------------------------------------------------------------
# execute_connector_action
# ---------------------------------------------------------------------------


async def test_executing_a_risky_action_returns_needs_approval_not_a_result(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    result = await execute_connector_action(
        db,
        bot_id=bot.id,
        connector_id="microsoft_graph",
        action="send_mail",
        input_data={"to": "a@b.c", "subject": "s", "body": "b"},
    )
    assert result["ok"] is False
    assert result["needs_approval"] is True
    assert result["risk"] == "send"
    assert "result" not in result


async def test_force_bypasses_the_gate(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    result = await execute_connector_action(
        db,
        bot_id=bot.id,
        connector_id="microsoft_graph",
        action="send_mail",
        input_data={"to": "a@b.c", "subject": "s", "body": "b"},
        force=True,
    )
    assert result["ok"] is True
    assert result["result"]["sent"] is True


async def test_validation_runs_before_the_gate(db, make_user, make_bot):
    """A malformed risky call is rejected outright rather than parked."""
    user = await make_user()
    bot = await make_bot(user)
    result = await execute_connector_action(
        db, bot_id=bot.id, connector_id="microsoft_graph", action="send_mail", input_data={}
    )
    assert result["ok"] is False
    assert result["missing"] == ["to", "subject", "body"]
    assert "needs_approval" not in result


async def test_an_unknown_connector_is_reported_not_raised(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    result = await execute_connector_action(
        db, bot_id=bot.id, connector_id="nope", action="x", input_data={}
    )
    assert result == {"ok": False, "error": "connector not found"}


async def test_an_unconnected_binding_still_mocks_observe_actions(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    result = await execute_connector_action(
        db, bot_id=bot.id, connector_id="ticketing", action="list_open", input_data={}
    )
    assert result["ok"] is True
    assert result["mock"] is True


async def test_the_catalog_is_seeded_once(db):
    rows = await db.execute(select(Connector).where(Connector.first_party.is_(True)))
    ids = [c.id for c in rows.scalars().all()]
    assert sorted(ids) == sorted(spec["id"] for spec in FIRST_PARTY)
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# The vendor seam: which path ran, and why
#
# `_invoke_vendor` goes live only when a credential resolved AND the connector
# is configured for real calls. Everything else keeps the mock, which is the
# shape the drivers normalise onto. No test here touches a network: the drivers
# are pointed at an `httpx.MockTransport`.
# ---------------------------------------------------------------------------

ENV_VAR = "NESQ_TEST_VENDOR_SECRET"
TOKEN = "vendor-token-8fa02c"

GRAPH_PAYLOAD = {
    "value": [
        {
            "id": "AAMk1",
            "subject": "Invoice #4421",
            "bodyPreview": "Please find attached...",
            "from": {"emailAddress": {"address": "vendor@example.com"}},
        }
    ]
}


@pytest.fixture
def vendor_settings(monkeypatch):
    """Point the API at a fake Graph and let live calls through."""
    from types import SimpleNamespace

    from app.services import connectors as connectors_module

    state = SimpleNamespace(
        graph_api_base_url="https://graph.test/v1.0",
        request_timeout_seconds=5.0,
        connector_live_calls=True,
    )
    monkeypatch.setattr(connectors_module, "get_settings", lambda: state)
    return state


@pytest.fixture
def bound_secret(monkeypatch):
    from app.services import secrets as secrets_module

    monkeypatch.setenv(ENV_VAR, TOKEN)
    secrets_module.reset_cache()
    yield f"env://{ENV_VAR}"
    secrets_module.reset_cache()


@pytest.fixture
def vendor_transport():
    """Install a mock transport for the duration of one test."""
    import httpx

    from app.services.vendors import base as vendor_base

    def install(responder):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            result = responder(request)
            if isinstance(result, Exception):
                raise result
            return result

        vendor_base.set_default_transport(httpx.MockTransport(handler))
        return requests

    yield install
    vendor_base.set_default_transport(None)


async def _graph_action(db, bot, action, input_data, *, force=False):
    return await execute_connector_action(
        db,
        bot_id=bot.id,
        connector_id="microsoft_graph",
        action=action,
        input_data=input_data,
        force=force,
    )


async def test_a_bound_connector_calls_the_vendor(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport
):
    import httpx

    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=bound_secret)
    requests = vendor_transport(lambda request: httpx.Response(200, json=GRAPH_PAYLOAD))

    result = await _graph_action(db, bot, "list_inbox", {"top": 1})

    assert result["ok"] is True
    assert result["authenticated"] is True
    assert "mock" not in result
    assert result["result"] == [
        {
            "id": "AAMk1",
            "from": "vendor@example.com",
            "subject": "Invoice #4421",
            "snippet": "Please find attached...",
        }
    ]
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


async def test_the_kill_switch_forces_the_mock_even_when_everything_else_is_ready(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport
):
    import httpx

    vendor_settings.connector_live_calls = False
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=bound_secret)
    requests = vendor_transport(lambda request: httpx.Response(200, json=GRAPH_PAYLOAD))

    result = await _graph_action(db, bot, "list_inbox", {"top": 1})

    assert result["ok"] is True
    assert result["mock"] is True
    assert result["authenticated"] is True
    assert requests == [], "the kill switch let a call out"
    assert result["result"][0]["id"] == "m1", "this is the mock inbox"


async def test_an_unconfigured_connector_mocks_even_with_a_credential(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport
):
    """No `GRAPH_API_BASE_URL` means this deployment is not wired to a tenant."""
    import httpx

    vendor_settings.graph_api_base_url = ""
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=bound_secret)
    requests = vendor_transport(lambda request: httpx.Response(200, json=GRAPH_PAYLOAD))

    result = await _graph_action(db, bot, "list_inbox", {"top": 1})

    assert result["mock"] is True
    assert requests == []


async def test_a_binding_with_no_resolvable_secret_mocks(
    db, make_user, make_bot, make_connector_binding, vendor_settings, vendor_transport
):
    import httpx

    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(
        bot, "microsoft_graph", status="connected", secret_ref="env://NESQ_NOT_SET_51423"
    )
    requests = vendor_transport(lambda request: httpx.Response(200, json=GRAPH_PAYLOAD))

    result = await _graph_action(db, bot, "list_inbox", {"top": 1})

    assert result["ok"] is True
    assert result["mock"] is True
    assert result["authenticated"] is False
    assert requests == []


async def test_the_placeholder_connectors_stay_on_the_mock(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport
):
    """`crm` ships without a `base_url`, so there is nothing real to call."""
    import httpx

    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "crm", status="connected", secret_ref=bound_secret)
    requests = vendor_transport(lambda request: httpx.Response(200, json={"accounts": []}))

    result = await execute_connector_action(
        db, bot_id=bot.id, connector_id="crm", action="search_accounts", input_data={"query": "acme"}
    )

    assert result["mock"] is True
    assert requests == []


async def test_a_custom_connector_with_a_manifest_base_url_makes_a_real_call(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport
):
    import httpx

    manifest = {
        "id": "invoice_portal",
        "auth": "api_key",
        "api_key_header": "X-Api-Key",
        "base_url": "https://invoices.internal/api",
        "actions": [
            {
                "name": "list_unpaid",
                "risk": "observe",
                "method": "GET",
                "path": "/invoices/{state}",
                "input_schema": {"type": "object", "properties": {"state": {"type": "string"}}},
            }
        ],
    }
    db.add(
        Connector(
            id="invoice_portal",
            name="Invoice Portal",
            version="1.0.0",
            auth="api_key",
            scopes=[],
            actions=manifest["actions"],
            risk_default="observe",
            first_party=False,
            manifest=manifest,
        )
    )
    await db.commit()

    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "invoice_portal", status="connected", secret_ref=bound_secret)
    requests = vendor_transport(lambda request: httpx.Response(200, json=[{"id": "inv_1"}]))

    result = await execute_connector_action(
        db,
        bot_id=bot.id,
        connector_id="invoice_portal",
        action="list_unpaid",
        input_data={"state": "unpaid"},
    )

    assert result["ok"] is True
    assert "mock" not in result
    assert result["result"] == [{"id": "inv_1"}]
    assert requests[0].url.path == "/api/invoices/unpaid"
    assert requests[0].headers["X-Api-Key"] == TOKEN


async def test_a_vendor_failure_becomes_the_services_error_envelope(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport
):
    import httpx

    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=bound_secret)
    vendor_transport(lambda request: httpx.Response(401, text="InvalidAuthenticationToken"))

    result = await _graph_action(db, bot, "list_inbox", {"top": 1})

    assert result["ok"] is False
    assert result["status"] == 401
    assert "HTTP 401" in result["error"]
    assert result["connector"] == "microsoft_graph"
    assert TOKEN not in str(result)


async def test_a_vendor_that_cannot_be_reached_does_not_raise_into_the_caller(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport,
    monkeypatch,
):
    import httpx
    from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_none

    from app.services.vendors import base as vendor_base

    monkeypatch.setattr(
        vendor_base,
        "_retrying",
        lambda: AsyncRetrying(
            stop=stop_after_attempt(vendor_base.RETRY_ATTEMPTS),
            wait=wait_none(),
            retry=retry_if_exception(vendor_base.is_retryable),
            reraise=True,
        ),
    )
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=bound_secret)
    requests = vendor_transport(
        lambda request: httpx.ConnectError("connection refused", request=request)
    )

    result = await _graph_action(db, bot, "list_inbox", {"top": 1})

    assert result["ok"] is False
    assert "could not reach the vendor" in result["error"]
    assert len(requests) == vendor_base.RETRY_ATTEMPTS, "connection errors are retried"


async def test_the_chosen_path_is_logged_at_debug_level(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport,
    caplog,
):
    import logging

    import httpx

    caplog.set_level(logging.DEBUG, logger="app.services.connectors")
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=bound_secret)
    vendor_transport(lambda request: httpx.Response(200, json=GRAPH_PAYLOAD))

    await _graph_action(db, bot, "list_inbox", {"top": 1})

    assert "microsoft_graph/list_inbox -> live path" in caplog.text
    assert "driver=microsoft_graph" in caplog.text
    assert TOKEN not in caplog.text


async def test_an_approved_send_goes_out_through_the_vendor(
    db, make_user, make_bot, make_connector_binding, vendor_settings, bound_secret, vendor_transport
):
    """`force=True` still runs the real call, not a second mock."""
    import httpx

    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=bound_secret)
    requests = vendor_transport(lambda request: httpx.Response(202, headers={"request-id": "req-9"}))

    result = await _graph_action(
        db, bot, "send_mail", {"to": "a@b.c", "subject": "s", "body": "b"}, force=True
    )

    assert result["ok"] is True
    assert result["result"] == {
        "message_id": "req-9",
        "to": "a@b.c",
        "subject": "s",
        "state": "sent",
        "sent": True,
    }
    assert requests[0].url.path == "/v1.0/me/sendMail"
