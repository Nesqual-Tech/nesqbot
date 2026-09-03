"""Connector catalog, bot bindings, and the MCP registry."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import BotConnector, BotMcp
from app.services.connectors import FIRST_PARTY

MISSING = uuid.uuid4()


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


async def test_connector_catalog_lists_the_first_party_connectors(authed):
    response = await authed.get("/api/integrations/connectors")
    assert response.status_code == 200
    by_id = {c["id"]: c for c in response.json()}
    for spec in FIRST_PARTY:
        assert spec["id"] in by_id
        assert by_id[spec["id"]]["first_party"] is True
        assert by_id[spec["id"]]["actions"]


async def test_register_a_custom_connector(authed):
    response = await authed.post(
        "/api/integrations/connectors",
        json={
            "id": "acme_widgets",
            "name": "Acme Widgets",
            "auth": "api_key",
            "actions": [{"name": "list_widgets", "risk": "observe"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "acme_widgets"
    assert body["first_party"] is False


async def test_registering_a_duplicate_connector_id_is_400(authed):
    response = await authed.post(
        "/api/integrations/connectors", json={"id": "microsoft_graph", "name": "Impostor"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "connector_exists"


async def test_delete_a_custom_connector(authed):
    await authed.post("/api/integrations/connectors", json={"id": "temp_conn", "name": "Temp"})
    response = await authed.delete("/api/integrations/connectors/temp_conn")
    assert response.status_code == 200
    assert response.json()["detail"] == "deleted"

    listed = {c["id"] for c in (await authed.get("/api/integrations/connectors")).json()}
    assert "temp_conn" not in listed


async def test_first_party_connectors_cannot_be_deleted(authed):
    response = await authed.delete("/api/integrations/connectors/microsoft_graph")
    assert response.status_code == 403
    assert response.json()["code"] == "connector_first_party"


async def test_deleting_an_unknown_connector_is_404(authed):
    response = await authed.delete("/api/integrations/connectors/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "connector_not_found"


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


async def test_list_bindings_is_empty_for_a_fresh_bot(authed, bot_a):
    response = await authed.get(f"/api/bots/{bot_a.id}/connectors")
    assert response.status_code == 200
    assert response.json() == []



def _returning(value: str):
    """A `SecretClient.get_secret` that answers for any name.

    Fine here because this test is about the binding row, not about the guard;
    `tests/services/test_connector_secret_storage.py` uses a vault fake with a
    real inventory precisely so the guard is exercised against absent names.
    """
    from types import SimpleNamespace

    async def _get_secret(_name: str):
        return SimpleNamespace(value=value)

    return _get_secret


async def test_bind_a_connector(authed, db, bot_a, monkeypatch):
    """A `kv://` ref binds when the vault confirms the secret is really there.

    The stub is the point rather than scaffolding. Binding used to accept any
    string that *looked* like a ref, which meant a credential pasted into the
    field was stored verbatim and then echoed by `GET /bots/{id}/connectors` to
    every user who could see the bot. `secrets.check_ref` now asks the vault
    whether the name exists, so a test binding a real-looking ref has to supply
    a vault that has it — and a deployment with no credential at all can no
    longer save a `kv://` ref, which could never have resolved there anyway.
    """
    from types import SimpleNamespace

    from app.services import secrets

    monkeypatch.setattr(
        secrets,
        "_get_client",
        lambda _url: SimpleNamespace(
            get_secret=_returning("the-real-crm-key"),
        ),
    )
    secrets.reset_cache()

    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm",
        json={"secret_ref": "kv://vault/crm-key", "status": "connected"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "connected"}

    listed = (await authed.get(f"/api/bots/{bot_a.id}/connectors")).json()
    assert len(listed) == 1
    assert listed[0]["connector_id"] == "crm"
    assert listed[0]["name"] == "CRM"
    assert listed[0]["risk_default"] == "observe"
    assert listed[0]["first_party"] is True
    assert listed[0]["actions"]


async def test_binding_twice_updates_rather_than_duplicating(authed, db, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm", json={"status": "connected"})
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm", json={"status": "disconnected"})
    rows = await db.execute(select(BotConnector).where(BotConnector.bot_id == bot_a.id))
    links = rows.scalars().all()
    assert len(links) == 1
    assert links[0].status == "disconnected"


async def test_unbind_a_connector(authed, bot_a, make_connector_binding):
    await make_connector_binding(bot_a, "crm")
    response = await authed.delete(f"/api/bots/{bot_a.id}/connectors/crm")
    assert response.status_code == 200
    assert response.json()["detail"] == "unbound"
    assert (await authed.get(f"/api/bots/{bot_a.id}/connectors")).json() == []


async def test_unbinding_something_that_was_never_bound_is_404(authed, bot_a):
    response = await authed.delete(f"/api/bots/{bot_a.id}/connectors/crm")
    assert response.status_code == 404
    assert response.json()["code"] == "binding_not_found"


# ---------------------------------------------------------------------------
# Executing actions
# ---------------------------------------------------------------------------


async def test_executing_an_unknown_action_reports_the_failure(authed, bot_a):
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/actions/teleport", json={"input": {}}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "unknown action" in body["error"]


async def test_executing_with_missing_required_input_reports_which_keys(authed, bot_a):
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/actions/search_accounts", json={"input": {}}
    )
    body = response.json()
    assert body["ok"] is False
    assert body["missing"] == ["query"]


async def test_executing_an_observe_action_returns_a_mock_result(authed, bot_a):
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/actions/search_accounts",
        json={"input": {"query": "acme"}},
    )
    body = response.json()
    assert body["ok"] is True
    assert body["result"][0]["name"].startswith("Acme")


# ---------------------------------------------------------------------------
# MCP registry
# ---------------------------------------------------------------------------


async def test_register_an_mcp_server(authed):
    response = await authed.post(
        "/api/integrations/mcp",
        json={"name": "Files MCP", "transport": "stdio", "command": "mcp-files", "tool_allowlist": ["read"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Files MCP"
    assert body["enabled"] is True
    assert body["tool_allowlist"] == ["read"]


async def test_list_mcp_servers(authed, make_mcp, user_a):
    server = await make_mcp(user_a, name="Mine")
    ids = {m["id"] for m in (await authed.get("/api/integrations/mcp")).json()}
    assert str(server.id) in ids


async def test_shared_mcp_servers_have_no_owner(authed, other, make_mcp):
    shared = await make_mcp(None, name="Shared MCP")
    for client in (authed, other):
        ids = {m["id"] for m in (await client.get("/api/integrations/mcp")).json()}
        assert str(shared.id) in ids


async def test_patch_an_mcp_server(authed, make_mcp, user_a):
    server = await make_mcp(user_a)
    response = await authed.patch(
        f"/api/integrations/mcp/{server.id}",
        json={"enabled": False, "tool_allowlist": ["only_this"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["tool_allowlist"] == ["only_this"]


async def test_delete_an_mcp_server(authed, make_mcp, user_a):
    server = await make_mcp(user_a)
    response = await authed.delete(f"/api/integrations/mcp/{server.id}")
    assert response.status_code == 200
    assert (await authed.patch(f"/api/integrations/mcp/{server.id}", json={})).status_code == 404


async def test_missing_mcp_server_is_404(authed):
    response = await authed.get(f"/api/integrations/mcp/{MISSING}/tools")
    assert response.status_code == 404
    assert response.json()["code"] == "mcp_not_found"


async def test_tools_list_falls_back_to_a_mock_for_a_stdio_server(authed, make_mcp, user_a):
    server = await make_mcp(user_a, transport="stdio", tool_allowlist=["alpha", "beta"])
    response = await authed.get(f"/api/integrations/mcp/{server.id}/tools")
    assert response.status_code == 200
    body = response.json()
    assert body["mock"] is True
    assert [t["name"] for t in body["tools"]] == ["alpha", "beta"]


async def test_tools_list_mocks_an_unreachable_http_server(authed, make_mcp, user_a):
    server = await make_mcp(
        user_a, transport="http", endpoint="http://127.0.0.1:63999", command=None
    )
    response = await authed.get(f"/api/integrations/mcp/{server.id}/tools")
    assert response.status_code == 200
    body = response.json()
    assert body["mock"] is True
    assert body["error"]
    assert [t["name"] for t in body["tools"]] == ["echo", "search"]


async def test_attach_and_detach_an_mcp_server(authed, db, make_mcp, user_a, bot_a):
    server = await make_mcp(user_a)
    attached = await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    assert attached.status_code == 200
    assert attached.json() == {"ok": True}

    rows = await db.execute(select(BotMcp).where(BotMcp.bot_id == bot_a.id))
    assert len(rows.scalars().all()) == 1

    detached = await authed.delete(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    assert detached.status_code == 200
    assert detached.json()["detail"] == "detached"


async def test_attaching_twice_is_idempotent(authed, db, make_mcp, user_a, bot_a):
    server = await make_mcp(user_a)
    await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    rows = await db.execute(select(BotMcp).where(BotMcp.bot_id == bot_a.id))
    assert len(rows.scalars().all()) == 1


async def test_detaching_something_never_attached_is_404(authed, make_mcp, user_a, bot_a):
    server = await make_mcp(user_a)
    response = await authed.delete(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    assert response.status_code == 404
    assert response.json()["code"] == "mcp_not_attached"


async def test_call_an_mcp_tool(authed, make_mcp, user_a, bot_a):
    server = await make_mcp(user_a, name="Callable", tool_allowlist=["echo"])
    await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "echo", "arguments": {"x": 1}}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["tool"] == "echo"
    assert body["arguments"] == {"x": 1}


async def test_calling_an_unattached_mcp_server_fails_cleanly(authed, make_mcp, user_a, bot_a):
    server = await make_mcp(user_a)
    response = await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "echo"})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "mcp not attached to bot"}


async def test_a_tool_outside_the_allowlist_is_refused(authed, make_mcp, user_a, bot_a):
    server = await make_mcp(user_a, tool_allowlist=["allowed"])
    await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "forbidden"}
    )
    assert response.json() == {"ok": False, "error": "tool not allowlisted"}


async def test_a_disabled_mcp_server_refuses_calls(authed, make_mcp, user_a, bot_a):
    server = await make_mcp(user_a, enabled=False)
    await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    response = await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json={"tool": "echo"})
    assert response.json() == {"ok": False, "error": "mcp unavailable"}


@pytest.mark.parametrize("body", [{}, {"arguments": {"x": 1}}])
async def test_mcp_call_requires_a_tool_name(authed, make_mcp, user_a, bot_a, body):
    server = await make_mcp(user_a)
    await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}")
    response = await authed.post(f"/api/bots/{bot_a.id}/mcp/{server.id}/call", json=body)
    assert response.status_code == 422
