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


async def test_logout_revokes_the_presented_token(authed):
    logout = await authed.post("/api/auth/logout")
    assert logout.status_code == 200
    assert logout.json() == {"ok": True, "detail": "revoked"}

    # Same client, same header - the token itself is now dead.
    response = await authed.get("/api/me")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_logout_does_not_touch_other_sessions(app, user_a):
    """Logging out one token must not revoke a second token for the same user."""
    from tests.conftest import _client_for, auth_headers

    async with _client_for(app, auth_headers(user_a)) as first, _client_for(app, auth_headers(user_a)) as second:
        await first.post("/api/auth/logout")
        assert (await first.get("/api/me")).status_code == 401
        assert (await second.get("/api/me")).status_code == 200


async def test_logging_out_an_already_revoked_token_is_unauthorized(authed):
    """A revoked token cannot even reach the handler a second time -
    `get_current_user` rejects it before the route runs, same as any other
    dead token would."""
    first = await authed.post("/api/auth/logout")
    assert first.json()["detail"] == "revoked"
    second = await authed.post("/api/auth/logout")
    assert second.status_code == 401


async def test_revoking_the_same_token_twice_at_the_database_is_a_no_op(db, user_a):
    """The `ON CONFLICT DO NOTHING` path: two writes racing for the same `jti`
    (concurrent requests both past the `get_current_user` check before either
    commit) must not 500 on the second."""
    from jose import jwt

    from app.auth import create_access_token, revoke_token
    from app.config import get_settings

    token = create_access_token(str(user_a.id), user_a.email)
    payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])

    assert await revoke_token(db, payload) is True
    assert await revoke_token(db, payload) is True  # same jti, no unique-violation


async def test_logout_with_no_bearer_token_is_not_an_error(client):
    """The dev bypass reaches this route with nothing to decode."""
    response = await client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json()["detail"] == "nothing_to_revoke"


async def test_dev_login_tokens_can_be_revoked(client, app):
    from tests.conftest import _client_for

    token = (await client.post("/api/auth/dev-login")).json()["access_token"]
    async with _client_for(app, {"Authorization": f"Bearer {token}"}) as bearer_client:
        await bearer_client.post("/api/auth/logout")
        response = await bearer_client.get("/api/me")
    assert response.status_code == 401


# ------------------------------------------------------------- refresh


async def test_refresh_mints_a_new_token_and_revokes_the_old_one(client, app):
    from tests.conftest import _client_for

    first = (await client.post("/api/auth/dev-login")).json()["access_token"]
    async with _client_for(app, {"Authorization": f"Bearer {first}"}) as bearer:
        refreshed = await bearer.post("/api/auth/refresh")
        assert refreshed.status_code == 200
        second = refreshed.json()["access_token"]
        assert second and second != first
        assert refreshed.json()["user"]["email"] == "dev@nesqualtech.com"
        # The old token died in the same call.
        assert (await bearer.get("/api/me")).status_code == 401
    async with _client_for(app, {"Authorization": f"Bearer {second}"}) as bearer:
        assert (await bearer.get("/api/me")).status_code == 200


async def test_refresh_needs_a_session_token(client):
    """The dev bypass authenticates but has nothing to renew."""
    response = await client.post("/api/auth/refresh")
    assert response.status_code == 400
    assert response.json()["code"] == "not_refreshable"


async def test_refresh_with_garbage_is_401(anon):
    assert (await anon.post("/api/auth/refresh")).status_code == 401


def test_session_tokens_carry_iat_and_a_fourteen_day_exp():
    from datetime import datetime, timezone

    from jose import jwt

    from app.auth import ALGORITHM, create_access_token

    claims = jwt.decode(create_access_token("u", "u@x"), get_settings().jwt_secret, algorithms=[ALGORITHM])
    assert claims["iat"] <= datetime.now(timezone.utc).timestamp()
    assert 13.9 * 86400 < claims["exp"] - claims["iat"] <= 14 * 86400
