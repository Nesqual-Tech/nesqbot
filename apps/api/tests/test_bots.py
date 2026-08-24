"""Bot CRUD and the per-bot budget route."""

from __future__ import annotations

import uuid

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
