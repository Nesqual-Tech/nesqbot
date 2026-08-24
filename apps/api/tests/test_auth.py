"""Dev login, Entra sign-in, /me, and push-device registration."""

from __future__ import annotations

import pytest

from app.config import get_settings


async def test_dev_login_returns_a_token_and_user(client):
    response = await client.post("/api/auth/dev-login")
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["user"]["email"] == "dev@nesqualtech.com"


async def test_dev_login_is_forbidden_in_production(client, monkeypatch):
    import app.routers.auth as auth_router

    production = get_settings().model_copy(update={"nesq_env": "production"})
    monkeypatch.setattr(auth_router, "get_settings", lambda: production)

    response = await client.post("/api/auth/dev-login")
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "dev_login_disabled"
    assert "detail" in body


async def test_dev_login_token_authenticates_subsequent_requests(client, app):
    from tests.conftest import _client_for

    token = (await client.post("/api/auth/dev-login")).json()["access_token"]
    async with _client_for(app, {"Authorization": f"Bearer {token}"}) as bearer_client:
        me = await bearer_client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["email"] == "dev@nesqualtech.com"


async def test_entra_login_is_unavailable_without_tenant_configuration(client):
    """No AZURE_TENANT_ID / AZURE_CLIENT_ID locally: 503, never a 500."""
    response = await client.post("/api/auth/entra", json={"id_token": "not.a.real.token"})
    assert response.status_code == 503
    body = response.json()
    assert set(body) >= {"detail", "code"}


async def test_entra_login_requires_an_id_token(client):
    response = await client.post("/api/auth/entra", json={})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_the_dev_header_overrides_a_bearer_token_in_development(authed):
    """`X-Nesq-Dev: 1` is the documented development bypass."""
    response = await authed.get("/api/me", headers={"X-Nesq-Dev": "1"})
    assert response.status_code == 200
    assert response.json()["email"] == "dev@nesqualtech.com"


async def test_no_credentials_at_all_lands_on_the_dev_user_in_development(client):
    response = await client.get("/api/me")
    assert response.status_code == 200
    assert response.json()["email"] == "dev@nesqualtech.com"


async def test_the_dev_bypass_is_unreachable_outside_development(app, monkeypatch):
    """Production must never hand out a session to an unauthenticated caller."""
    import app.auth as auth_module
    from tests.conftest import _client_for

    production = get_settings().model_copy(update={"nesq_env": "production"})
    monkeypatch.setattr(auth_module, "get_settings", lambda: production)

    async with _client_for(app) as bare:
        assert (await bare.get("/api/me")).status_code == 401
    async with _client_for(app, {"X-Nesq-Dev": "1"}) as spoofed:
        assert (await spoofed.get("/api/me")).status_code == 401


async def test_a_token_for_a_deleted_user_is_rejected(app, db, make_user):
    from tests.conftest import _client_for, auth_headers

    ghost = await make_user()
    headers = auth_headers(ghost)
    await db.delete(ghost)
    await db.commit()

    async with _client_for(app, headers) as client:
        response = await client.get("/api/me")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_a_token_signed_with_the_wrong_secret_is_rejected(app, user_a):
    from jose import jwt

    from tests.conftest import _client_for

    forged = jwt.encode({"sub": str(user_a.id), "email": user_a.email}, "not-the-secret", algorithm="HS256")
    async with _client_for(app, {"Authorization": f"Bearer {forged}"}) as client:
        assert (await client.get("/api/me")).status_code == 401


async def test_me_returns_the_bearer_user(authed, user_a):
    response = await authed.get("/api/me")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user_a.id)
    assert body["email"] == user_a.email


async def test_me_rejects_a_bogus_bearer_token(anon):
    response = await anon.get("/api/me")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_register_device_is_an_upsert(authed):
    first = await authed.post("/api/me/devices", json={"token": "ExponentPushToken[abc]", "platform": "ios"})
    assert first.status_code == 201
    assert first.json()["ok"] is True
    device_id = first.json()["device_id"]

    second = await authed.post("/api/me/devices", json={"token": "ExponentPushToken[abc]", "platform": "android"})
    assert second.status_code == 201
    assert second.json()["device_id"] == device_id, "re-registering the same token must not create a row"


@pytest.mark.parametrize("platform", ["ios", "android", "web"])
async def test_register_device_accepts_documented_platforms(authed, platform):
    response = await authed.post(
        "/api/me/devices", json={"token": f"tok-{platform}", "platform": platform}
    )
    assert response.status_code == 201


async def test_register_device_rejects_an_unknown_platform(authed):
    response = await authed.post("/api/me/devices", json={"token": "tok", "platform": "blackberry"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"]


async def test_unregister_device(authed):
    await authed.post("/api/me/devices", json={"token": "to-remove", "platform": "web"})
    response = await authed.delete("/api/me/devices/to-remove")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "detail": "deleted"}


async def test_unregister_unknown_device_is_idempotent(authed):
    response = await authed.delete("/api/me/devices/never-registered")
    assert response.status_code == 200
    assert response.json()["detail"] == "not_registered"


async def test_devices_are_scoped_to_their_owner(authed, other):
    await authed.post("/api/me/devices", json={"token": "a-device", "platform": "ios"})
    # User B deleting user A's token must not touch it.
    response = await other.delete("/api/me/devices/a-device")
    assert response.status_code == 200
    assert response.json()["detail"] == "not_registered"
