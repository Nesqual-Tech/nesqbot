"""Roles: who may touch the shared catalog, the shared bots, and other people's roles.

The rule under test (`app.auth.require_admin`): admin-only routes are open to
everyone until an admin exists — a fresh install keeps working — and refuse
members the moment one does. `RBAC_ENFORCE=1` forces refusal even before.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.auth import ROLE_ADMIN, ROLE_MEMBER
from app.config import get_settings
from tests.conftest import _client_for, auth_headers

CONNECTOR = {
    "id": "acme_rbac",
    "name": "Acme RBAC",
    "auth": "api_key",
    "actions": [{"name": "list", "risk": "observe"}],
}


@pytest_asyncio.fixture
async def admin_user(make_user, db):
    user = await make_user(display_name="Admin")
    user.role = ROLE_ADMIN
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin(app, admin_user):
    async with _client_for(app, auth_headers(admin_user)) as c:
        yield c


# ------------------------------------------------------------- bootstrap


async def test_before_any_admin_exists_a_member_may_register_a_connector(authed):
    response = await authed.post("/api/integrations/connectors", json=CONNECTOR)
    assert response.status_code == 200


async def test_rbac_enforce_locks_the_catalog_even_with_no_admin(authed, monkeypatch):
    import app.auth as auth_module

    strict = get_settings().model_copy(update={"rbac_enforce": True})
    monkeypatch.setattr(auth_module, "get_settings", lambda: strict)
    response = await authed.post("/api/integrations/connectors", json=CONNECTOR)
    assert response.status_code == 403
    assert response.json()["code"] == "role_required"


# ------------------------------------------------------------- enforced


async def test_once_an_admin_exists_a_member_is_refused(authed, admin_user):
    response = await authed.post("/api/integrations/connectors", json=CONNECTOR)
    assert response.status_code == 403
    assert response.json()["code"] == "role_required"


async def test_the_admin_is_allowed(admin):
    response = await admin.post("/api/integrations/connectors", json=CONNECTOR)
    assert response.status_code == 200
    gone = await admin.delete("/api/integrations/connectors/acme_rbac")
    assert gone.status_code == 200


async def test_a_member_may_not_delete_from_the_catalog(authed, admin):
    assert (await admin.post("/api/integrations/connectors", json=CONNECTOR)).status_code == 200
    response = await authed.delete("/api/integrations/connectors/acme_rbac")
    assert response.status_code == 403


async def test_a_member_may_still_edit_their_own_bot(authed, bot_a, admin_user):
    response = await authed.patch(f"/api/bots/{bot_a.id}", json={"name": "Renamed"})
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


async def test_a_member_may_not_edit_a_system_bot(authed, system_bot, admin_user):
    response = await authed.patch(f"/api/bots/{system_bot.id}", json={"name": "Hijacked"})
    assert response.status_code == 403
    assert response.json()["code"] == "role_required"


async def test_a_member_may_not_change_a_system_bot_budget(authed, system_bot, admin_user):
    response = await authed.patch(f"/api/bots/{system_bot.id}/budget", json={"daily_budget_usd": 999})
    assert response.status_code == 403


async def test_the_admin_may_change_a_system_bot_budget(admin, system_bot):
    response = await admin.patch(f"/api/bots/{system_bot.id}/budget", json={"daily_budget_usd": 12.5})
    assert response.status_code == 200
    assert float(response.json()["daily_budget_usd"]) == 12.5


async def test_reseed_is_admin_only(authed, admin, admin_user):
    assert (await authed.post("/api/bots/system/reseed")).status_code == 403
    assert (await admin.post("/api/bots/system/reseed")).status_code == 200


async def test_the_dev_bypass_user_counts_as_an_admin(client, admin_user):
    """`X-Nesq-Dev` in development is all-access; it must not need ADMIN_EMAILS."""
    response = await client.post("/api/integrations/connectors", json=CONNECTOR)
    assert response.status_code == 200


# ---------------------------------------------------------------- users


async def test_me_reports_the_role(authed, admin):
    assert (await authed.get("/api/me")).json()["role"] == ROLE_MEMBER
    assert (await admin.get("/api/me")).json()["role"] == ROLE_ADMIN


async def test_listing_users_is_admin_only(authed, admin, user_a, admin_user):
    assert (await authed.get("/api/users")).status_code == 403
    response = await admin.get("/api/users")
    assert response.status_code == 200
    emails = {u["email"] for u in response.json()}
    assert {user_a.email, admin_user.email} <= emails


async def test_an_admin_can_promote_and_demote(admin, user_a, db):
    up = await admin.patch(f"/api/users/{user_a.id}", json={"role": "admin"})
    assert up.status_code == 200
    assert up.json()["role"] == "admin"
    down = await admin.patch(f"/api/users/{user_a.id}", json={"role": "member"})
    assert down.status_code == 200
    assert down.json()["role"] == "member"


async def test_the_last_admin_cannot_demote_themself(admin, admin_user):
    response = await admin.patch(f"/api/users/{admin_user.id}", json={"role": "member"})
    assert response.status_code == 409
    assert response.json()["code"] == "last_admin"


async def test_a_member_cannot_change_roles(authed, user_a, admin_user):
    response = await authed.patch(f"/api/users/{user_a.id}", json={"role": "admin"})
    assert response.status_code == 403


async def test_role_values_are_validated(admin, user_a):
    response = await admin.patch(f"/api/users/{user_a.id}", json={"role": "root"})
    assert response.status_code == 422


async def test_unknown_user_is_404(admin):
    response = await admin.patch(
        "/api/users/00000000-0000-0000-0000-0000000000ff", json={"role": "admin"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------- admin_emails


async def test_admin_emails_grants_the_role_at_dev_login(client, monkeypatch, db):
    import app.auth as auth_module

    granted = get_settings().model_copy(update={"admin_emails": "DEV@nesqualtech.com"})
    monkeypatch.setattr(auth_module, "get_settings", lambda: granted)
    response = await client.post("/api/auth/dev-login")
    assert response.status_code == 200
    assert response.json()["user"]["role"] == ROLE_ADMIN


@pytest.mark.parametrize("raw, expected", [("", []), ("a@x.com, B@Y.com ,", ["a@x.com", "b@y.com"])])
def test_admin_email_list_parsing(raw, expected):
    assert get_settings().model_copy(update={"admin_emails": raw}).admin_email_list == expected
