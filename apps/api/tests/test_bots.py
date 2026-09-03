"""Bot CRUD and the per-bot budget route."""

from __future__ import annotations

import uuid

from app.config import get_settings

MISSING = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


async def test_list_bots_returns_system_bots_and_own_customs(authed, bot_a, bot_b):
    response = await authed.get("/api/bots")
    assert response.status_code == 200
    ids = {b["id"] for b in response.json()}
    assert str(bot_a.id) in ids
    assert str(bot_b.id) not in ids, "another user's custom bot must not be listed"
    assert any(b["is_system"] for b in response.json())


async def test_create_custom_bot(authed):
    response = await authed.post(
        "/api/bots",
        json={"name": "Invoice Wrangler", "role": "Ops", "system_prompt": "Wrangle invoices."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Invoice Wrangler"
    assert body["slug"] == "invoice_wrangler"
    assert body["is_system"] is False


async def test_creating_a_bot_with_an_unknown_connector_is_a_404_with_a_code(authed, db):
    """`bot_connectors.connector_id` is a foreign key onto the catalog.

    An id the catalog does not have — a connector removed since the client
    cached the list — failed at the commit and came back through `app.errors`'
    catch-all as `{"detail": "internal_error", "code": "internal_error"}`. This
    also proves the bot row is not left behind by the rejected request, which is
    why the check runs before the INSERT rather than around it.
    """
    from sqlalchemy import func, select

    from app.models import Bot

    response = await authed.post(
        "/api/bots",
        json={
            "name": "Ghost Connector Bot",
            "role": "Ops",
            "system_prompt": "x",
            "connector_ids": ["not_a_real_connector"],
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "connector_not_found"

    rows = await db.execute(
        select(func.count()).select_from(Bot).where(Bot.name == "Ghost Connector Bot")
    )
    assert int(rows.scalar_one()) == 0, "the rejected request left a bot row behind"


async def test_creating_a_bot_with_an_unknown_mcp_id_is_a_404_with_a_code(authed):
    response = await authed.post(
        "/api/bots",
        json={
            "name": "Ghost MCP Bot",
            "role": "Ops",
            "system_prompt": "x",
            "mcp_ids": [str(MISSING)],
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "mcp_not_found"


async def test_creating_a_bot_cannot_attach_another_users_mcp_server(authed, make_mcp, user_b):
    """Same visibility rule `routers/integrations.py::_get_mcp` applies on read.

    Attaching someone else's private MCP server would let this bot call its
    tools — which for a platform server is deletes, money movement and messages
    to real people.
    """
    theirs = await make_mcp(user_b, name="B's private MCP")
    response = await authed.post(
        "/api/bots",
        json={
            "name": "Borrowed MCP Bot",
            "role": "Ops",
            "system_prompt": "x",
            "mcp_ids": [str(theirs.id)],
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "mcp_not_found"


async def test_list_available_providers_shape(authed):
    """No provider is configured in the test environment — every key present,
    every value False."""
    response = await authed.get("/api/bots/providers")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"azure", "openai", "anthropic", "google"}
    assert all(v is False for v in body.values())


async def test_reseed_is_a_no_op_when_nothing_new_is_configured(authed):
    """The database is already seeded (see conftest) - reseeding again must
    not duplicate anything or report new bots that were not new."""
    response = await authed.post("/api/bots/system/reseed")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "0 new" in body["detail"]


async def test_reseed_picks_up_a_new_yaml_bot_without_a_restart(authed, monkeypatch):
    import app.services.seed as seed_module

    extra = {
        "slug": "finance_test",
        "name": "Finance",
        "role": "Bookkeeping",
        "system_prompt": "Reconcile invoices.",
    }
    monkeypatch.setattr(seed_module, "DEFAULT_BOTS", [*seed_module.DEFAULT_BOTS, extra])

    response = await authed.post("/api/bots/system/reseed")
    assert response.status_code == 200
    assert "1 new" in response.json()["detail"]

    listed = await authed.get("/api/bots")
    slugs = {b["slug"] for b in listed.json()}
    assert "finance_test" in slugs


async def test_create_custom_bot_deduplicates_slugs(authed):
    first = await authed.post(
        "/api/bots", json={"name": "Twin", "role": "r", "system_prompt": "p"}
    )
    second = await authed.post(
        "/api/bots", json={"name": "Twin", "role": "r", "system_prompt": "p"}
    )
    assert first.json()["slug"] != second.json()["slug"]


async def test_create_custom_bot_with_connector_bindings(authed):
    response = await authed.post(
        "/api/bots",
        json={
            "name": "Bound Bot",
            "role": "Ops",
            "system_prompt": "p",
            "connector_ids": ["microsoft_graph"],
        },
    )
    assert response.status_code == 200
    bot_id = response.json()["id"]
    bindings = await authed.get(f"/api/bots/{bot_id}/connectors")
    assert [b["connector_id"] for b in bindings.json()] == ["microsoft_graph"]


async def test_get_bot(authed, bot_a):
    response = await authed.get(f"/api/bots/{bot_a.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(bot_a.id)


async def test_get_missing_bot_is_404_with_a_code(authed):
    response = await authed.get(f"/api/bots/{MISSING}")
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


async def test_patch_bot_fields(authed, bot_a):
    response = await authed.patch(
        f"/api/bots/{bot_a.id}",
        json={"name": "Renamed", "role": "Analyst", "daily_budget_usd": 12.5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["role"] == "Analyst"
    assert body["daily_budget_usd"] == 12.5


async def test_create_bot_with_a_model_override(authed):
    response = await authed.post(
        "/api/bots",
        json={
            "name": "Claude Bot",
            "role": "Test",
            "system_prompt": "Be helpful.",
            "model_provider": "anthropic",
            "model_name": "claude-opus-4",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model_provider"] == "anthropic"
    assert body["model_name"] == "claude-opus-4"


async def test_create_bot_with_no_override_leaves_both_fields_null(authed):
    response = await authed.post(
        "/api/bots", json={"name": "Plain Bot", "role": "Test", "system_prompt": "Be helpful."}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model_provider"] is None
    assert body["model_name"] is None


async def test_create_bot_rejects_provider_without_model(authed):
    response = await authed.post(
        "/api/bots",
        json={
            "name": "Broken Bot",
            "role": "Test",
            "system_prompt": "Be helpful.",
            "model_provider": "openai",
        },
    )
    assert response.status_code == 422


async def test_create_bot_rejects_model_without_provider(authed):
    response = await authed.post(
        "/api/bots",
        json={
            "name": "Broken Bot",
            "role": "Test",
            "system_prompt": "Be helpful.",
            "model_name": "gpt-5.1",
        },
    )
    assert response.status_code == 422


async def test_create_bot_rejects_an_unknown_provider(authed):
    response = await authed.post(
        "/api/bots",
        json={
            "name": "Broken Bot",
            "role": "Test",
            "system_prompt": "Be helpful.",
            "model_provider": "bedrock",
            "model_name": "claude",
        },
    )
    assert response.status_code == 422


async def test_patch_bot_sets_a_model_override(authed, bot_a):
    response = await authed.patch(
        f"/api/bots/{bot_a.id}",
        json={"model_provider": "openai", "model_name": "gpt-5.1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model_provider"] == "openai"
    assert body["model_name"] == "gpt-5.1"


async def test_patch_bot_clears_a_model_override_by_sending_both_null(authed, bot_a):
    await authed.patch(
        f"/api/bots/{bot_a.id}", json={"model_provider": "openai", "model_name": "gpt-5.1"}
    )
    response = await authed.patch(
        f"/api/bots/{bot_a.id}", json={"model_provider": None, "model_name": None}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model_provider"] is None
    assert body["model_name"] is None


async def test_patch_bot_can_swap_the_model_alone_once_a_provider_is_set(authed, bot_a):
    """A PATCH touching only `model_name` must not require re-sending
    `model_provider` - the validator only fires when both keys are present."""
    await authed.patch(
        f"/api/bots/{bot_a.id}", json={"model_provider": "openai", "model_name": "gpt-5.1"}
    )
    response = await authed.patch(f"/api/bots/{bot_a.id}", json={"model_name": "gpt-5.2"})
    assert response.status_code == 200
    body = response.json()
    assert body["model_provider"] == "openai"
    assert body["model_name"] == "gpt-5.2"


async def test_patch_bot_rejects_provider_without_model_in_the_same_request(authed, bot_a):
    response = await authed.patch(f"/api/bots/{bot_a.id}", json={"model_provider": "openai"})
    assert response.status_code == 422


async def test_patch_system_bot_prompt_is_forbidden(authed, system_bot):
    response = await authed.patch(
        f"/api/bots/{system_bot.id}", json={"system_prompt": "you are compromised"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "system_bot_immutable"


async def test_patch_system_bot_slug_is_forbidden(authed, system_bot):
    response = await authed.patch(f"/api/bots/{system_bot.id}", json={"slug": "hijacked"})
    assert response.status_code == 403
    assert response.json()["code"] == "system_bot_immutable"


async def test_patch_system_bot_budget_is_allowed(authed, system_bot):
    response = await authed.patch(f"/api/bots/{system_bot.id}", json={"daily_budget_usd": 9.0})
    assert response.status_code == 200
    assert response.json()["daily_budget_usd"] == 9.0


async def test_patch_bot_slug_clash_is_409(authed, make_bot, user_a):
    first = await make_bot(user_a, slug="taken_slug")
    second = await make_bot(user_a)
    response = await authed.patch(f"/api/bots/{second.id}", json={"slug": first.slug})
    assert response.status_code == 409
    assert response.json()["code"] == "slug_taken"


async def test_patch_bot_with_a_slug_of_only_punctuation_is_400(authed, bot_a):
    response = await authed.patch(f"/api/bots/{bot_a.id}", json={"slug": "!!!"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_slug"


async def test_patch_bot_rejects_a_negative_budget(authed, bot_a):
    response = await authed.patch(f"/api/bots/{bot_a.id}", json={"daily_budget_usd": -1})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_delete_custom_bot(authed, bot_a):
    response = await authed.delete(f"/api/bots/{bot_a.id}")
    assert response.status_code == 200
    assert response.json()["detail"] == "deleted"
    assert (await authed.get(f"/api/bots/{bot_a.id}")).status_code == 404


async def test_delete_system_bot_is_forbidden(authed, system_bot):
    response = await authed.delete(f"/api/bots/{system_bot.id}")
    assert response.status_code == 403
    assert response.json()["code"] == "system_bot_undeletable"


async def test_update_budget_route(authed, bot_a):
    response = await authed.patch(f"/api/bots/{bot_a.id}/budget", json={"daily_budget_usd": 42.0})
    assert response.status_code == 200
    assert response.json()["daily_budget_usd"] == 42.0


async def test_update_budget_rejects_a_negative_cap(authed, bot_a):
    response = await authed.patch(f"/api/bots/{bot_a.id}/budget", json={"daily_budget_usd": -5})
    assert response.status_code == 422


async def test_list_provider_credentials_starts_empty(authed):
    response = await authed.get("/api/bots/providers/credentials")
    assert response.status_code == 200
    rows = response.json()["credentials"]
    assert {r["provider"] for r in rows} == {"azure", "openai", "anthropic", "google"}
    assert all(r["configured"] is False for r in rows)
    assert all(r["key_hint"] is None for r in rows)


async def test_set_provider_credential_then_it_shows_up_as_configured(authed):
    saved = await authed.post(
        "/api/bots/providers/openai/credential",
        json={"api_key": "sk-test-abcdef1234", "base_url": "https://openrouter.ai/api/v1"},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["provider"] == "openai"
    assert body["configured"] is True
    assert body["key_hint"] == "…1234"
    assert body["base_url"] == "https://openrouter.ai/api/v1"

    listed = await authed.get("/api/bots/providers/credentials")
    row = next(r for r in listed.json()["credentials"] if r["provider"] == "openai")
    assert row["configured"] is True
    assert row["key_hint"] == "…1234"


async def test_set_provider_credential_never_returns_the_raw_key(authed):
    saved = await authed.post(
        "/api/bots/providers/anthropic/credential",
        json={"api_key": "sk-ant-do-not-leak-this"},
    )
    assert "sk-ant-do-not-leak-this" not in saved.text


async def test_set_provider_credential_rejects_an_unknown_provider(authed):
    response = await authed.post(
        "/api/bots/providers/wat/credential",
        json={"api_key": "sk-whatever"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_provider"


async def test_set_provider_credential_rejects_a_blank_key(authed):
    response = await authed.post(
        "/api/bots/providers/google/credential",
        json={"api_key": "   "},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "empty_api_key"


async def test_delete_provider_credential_clears_it(authed):
    await authed.post("/api/bots/providers/google/credential", json={"api_key": "sk-to-remove"})
    delete = await authed.delete("/api/bots/providers/google/credential")
    assert delete.status_code == 200
    assert delete.json()["ok"] is True

    listed = await authed.get("/api/bots/providers/credentials")
    row = next(r for r in listed.json()["credentials"] if r["provider"] == "google")
    assert row["configured"] is False


async def test_a_saved_credential_makes_the_provider_available(authed):
    """The whole point: `GET /bots/providers` reflects an app-typed key the
    same way it reflects an env-configured one, because `model_router.py`
    treats a stored override as a real fallback, not just a UI artifact."""
    before = await authed.get("/api/bots/providers")
    assert before.json()["anthropic"] is False

    await authed.post("/api/bots/providers/anthropic/credential", json={"api_key": "sk-ant-live"})

    after = await authed.get("/api/bots/providers")
    assert after.json()["anthropic"] is True


async def test_provider_credential_endpoints_require_auth_in_production(app, monkeypatch):
    """Same guarantee `test_the_dev_bypass_is_unreachable_outside_development`
    holds `GET /me` to: these endpoints read and write real credentials, so a
    production deployment with no session must refuse them, not fall back to
    the dev bypass."""
    import app.auth as auth_module
    from tests.conftest import _client_for

    production = get_settings().model_copy(update={"nesq_env": "production"})
    monkeypatch.setattr(auth_module, "get_settings", lambda: production)

    async with _client_for(app) as bare:
        assert (await bare.get("/api/bots/providers/credentials")).status_code == 401
        assert (
            await bare.post("/api/bots/providers/openai/credential", json={"api_key": "x"})
        ).status_code == 401
        assert (await bare.delete("/api/bots/providers/openai/credential")).status_code == 401


async def test_list_provider_models_returns_the_live_list(authed, monkeypatch):
    from app.routers.deps import model_router

    async def fake_list_models(provider):
        assert provider == "anthropic"
        return ["claude-opus-4-5", "claude-sonnet-4-5"]

    monkeypatch.setattr(model_router, "list_models", fake_list_models)

    response = await authed.get("/api/bots/providers/anthropic/models")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "anthropic"
    assert body["models"] == ["claude-opus-4-5", "claude-sonnet-4-5"]


async def test_list_provider_models_rejects_an_unknown_provider(authed):
    response = await authed.get("/api/bots/providers/wat/models")
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_provider"


async def test_list_provider_models_surfaces_a_provider_failure_as_502(authed, monkeypatch):
    from app.routers.deps import model_router

    async def fake_list_models(provider):
        raise RuntimeError("openai has no live credential to list models with")

    monkeypatch.setattr(model_router, "list_models", fake_list_models)

    response = await authed.get("/api/bots/providers/openai/models")
    assert response.status_code == 502
    assert response.json()["code"] == "provider_unreachable"
    assert "no live credential" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Persona: the fields that existed and could not be read
# ---------------------------------------------------------------------------
#
# Reported as "the bots have personas, with emails and so on but on the desktop
# app, i can't see that", which was literally true. `system_prompt` was
# write-only across the whole API - `CreateCustomBotIn` and `UpdateBotIn` take
# one, `BotOut` never returned one - so a client could show a name and a
# one-line role and nothing else. Editing a prompt in the Builder meant typing
# over something you could not see.


async def test_a_bots_persona_includes_the_prompt_that_was_write_only(authed, bot_a):
    response = await authed.get(f"/api/bots/{bot_a.id}/persona")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(bot_a.id)
    assert body["system_prompt"], "the persona endpoint exists to return this"
    # And it still carries everything `BotOut` did, so one request draws the card.
    for field in ("slug", "name", "role", "desktop_profile", "daily_budget_usd"):
        assert field in body, field
    assert "spent_usd_today" in body


async def test_the_list_endpoint_stays_lean(authed, bot_a):
    """The sidebar draws on every launch and has no use for system prompts."""
    listed = (await authed.get("/api/bots")).json()
    assert listed, "no bots came back"
    assert all("system_prompt" not in bot for bot in listed)


async def test_a_persona_names_the_channels_the_outside_world_reaches_it_on(
    authed, db, bot_a, user_a
):
    """The "emails" half. An inbound source routes a mailbox to a roster."""
    from app.models import InboundSource

    db.add(
        InboundSource(
            slug="ops-mailbox",
            name="Ops mailbox",
            kind="poll",
            channel="email",
            owner_user_id=user_a.id,
            bot_ids=[str(bot_a.id)],
            config={"address": "ops@nesqualtech.test"},
        )
    )
    await db.commit()

    body = (await authed.get(f"/api/bots/{bot_a.id}/persona")).json()

    assert [i["address"] for i in body["inboxes"]] == ["ops@nesqualtech.test"]
    assert body["inboxes"][0]["channel"] == "email"


async def test_an_older_single_bot_inbound_source_is_still_found(authed, db, bot_a, user_a):
    """`bot_id` predates `bot_ids`, and deployments configured then are live."""
    from app.models import InboundSource

    db.add(
        InboundSource(
            slug="legacy-hook",
            name="Legacy hook",
            kind="webhook",
            channel="email",
            owner_user_id=user_a.id,
            bot_id=bot_a.id,
            config={"email": "legacy@nesqualtech.test"},
        )
    )
    await db.commit()

    body = (await authed.get(f"/api/bots/{bot_a.id}/persona")).json()
    assert [i["address"] for i in body["inboxes"]] == ["legacy@nesqualtech.test"]


async def test_a_connector_is_reported_as_a_reference_never_a_secret(
    authed, db, bot_a, make_connector_binding
):
    """`secret_ref` is the pointer; the value is resolved in-process only."""
    await make_connector_binding(bot_a, "crm", secret_ref="kv://vault/crm-key")

    body = (await authed.get(f"/api/bots/{bot_a.id}/persona")).json()

    bound = {c["connector_id"]: c for c in body["connectors"]}
    assert "crm" in bound
    assert bound["crm"]["secret_ref"] == "kv://vault/crm-key"
    assert "the-real-crm-key" not in response_text(body)


def response_text(body) -> str:
    import json

    return json.dumps(body)


async def test_a_config_value_that_is_not_an_address_is_not_rendered_as_one(
    authed, db, bot_a, user_a
):
    """`config` is provider-shaped and can hold anything somebody needed.

    Only a plain string from a known key that actually looks like an address is
    shown - rendering an arbitrary config value into a persona card is how a
    secret ends up on screen.
    """
    from app.models import InboundSource

    db.add(
        InboundSource(
            slug="odd-config",
            name="Odd",
            kind="webhook",
            channel="webhook",
            owner_user_id=user_a.id,
            bot_ids=[str(bot_a.id)],
            config={"address": {"nested": "not-a-string"}, "token": "sk-live-secret"},
        )
    )
    await db.commit()

    body = (await authed.get(f"/api/bots/{bot_a.id}/persona")).json()

    assert body["inboxes"][0]["address"] is None
    assert "sk-live-secret" not in response_text(body)


async def test_another_users_bot_has_no_readable_persona(authed, other, make_bot, user_b):
    """The persona carries a system prompt, so visibility matters more here."""
    theirs = await make_bot(user_b, name="Theirs", slug="theirs-persona")
    response = await authed.get(f"/api/bots/{theirs.id}/persona")
    assert response.status_code == 404
