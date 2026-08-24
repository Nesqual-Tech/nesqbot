"""The Temporal worker authenticates with a shared service token, not a user JWT.

Regression cover for a gap that shipped to production: the worker had always
sent `Authorization: Bearer <WORKER_API_TOKEN>`, the bicep had always set the
value on both apps, and the API contained **no reference to it at all**. Every
worker call 401'd (`routines.fetch.failed http=401` ->
`schedule.reconcile.skipped`), so no cron routine could ever fire. It stayed
invisible because Temporal itself was down for an unrelated reason, so the
worker never got far enough to make the call.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import SERVICE_USER_EMAIL
from app.config import get_settings

WORKER_TOKEN = "test-worker-token-do-not-reuse"  # noqa: S105 - a fixture value


@pytest.fixture
async def worker_client(app, monkeypatch):
    """A client authenticating exactly the way the worker does."""
    settings = get_settings()
    monkeypatch.setattr(settings, "worker_api_token", WORKER_TOKEN, raising=False)
    monkeypatch.setattr(settings, "nesq_env", "production", raising=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    ) as client:
        yield client


async def test_the_worker_token_authenticates(worker_client):
    response = await worker_client.get("/api/me")
    assert response.status_code == 200, response.text
    assert response.json()["email"] == SERVICE_USER_EMAIL


async def test_the_worker_can_list_routines(worker_client):
    """The call that was failing: `reconcile_schedules` fetches routines on boot."""
    response = await worker_client.get("/api/routines")
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)


async def test_a_wrong_service_token_is_rejected(app, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "worker_api_token", WORKER_TOKEN, raising=False)
    monkeypatch.setattr(settings, "nesq_env", "production", raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer not-the-worker-token"},
    ) as client:
        assert (await client.get("/api/me")).status_code == 401


async def test_an_empty_configured_token_never_authenticates_anything(app, monkeypatch):
    """An unset secret must not degrade into "accept any bearer".

    This is the failure mode that matters: `secrets.compare_digest("", "")` is
    True, so a naive implementation would hand a service identity to anyone
    sending an empty bearer the moment the setting was left blank.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "worker_api_token", "", raising=False)
    monkeypatch.setattr(settings, "nesq_env", "production", raising=False)
    for header in ("Bearer ", "Bearer  ", "Bearer x"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": header},
        ) as client:
            assert (await client.get("/api/me")).status_code == 401, header


async def test_the_service_user_owns_nothing(worker_client):
    """It is an actor for the audit trail, not a tenant.

    Bot and thread listings are owner-scoped, so the worker seeing only system
    bots and no threads is what keeps a service credential from becoming a way
    to read someone's conversations.
    """
    threads = await worker_client.get("/api/threads")
    assert threads.status_code == 200
    assert threads.json() == []

    bots = await worker_client.get("/api/bots")
    assert bots.status_code == 200
    assert all(bot["is_system"] for bot in bots.json())
