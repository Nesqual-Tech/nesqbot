"""Health probes — the two unauthenticated routes."""

from __future__ import annotations

from app.routers.deps import API_VERSION
from app.routers.health import BUILD


async def test_health_is_shallow_and_unauthenticated(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "nesqbot-api",
        "version": API_VERSION,
        "build": BUILD,
    }


async def test_health_reports_the_deployed_build_separately_from_the_contract_version(client):
    """Two questions, two fields.

    `version` is the hand-maintained API contract version; `build` is the image
    tag stamped at docker build time. Reporting only the former meant the app
    footer read "API 0.2.0" while image v0.3.0 was live, and a successful deploy
    looked like a failed one.
    """
    body = (await client.get("/api/health")).json()
    assert "build" in body
    assert body["build"], "build must never be empty - 'unknown' is the floor"
    assert body["version"] == API_VERSION
    # They are independent on purpose; asserting they differ would be wrong too,
    # but the build must not silently inherit the contract version.
    assert body["build"] == BUILD


async def test_health_stamps_a_request_id(client):
    response = await client.get("/api/health")
    assert response.headers.get("x-request-id")
    assert response.headers.get("x-response-time-ms")


async def test_health_echoes_an_inbound_request_id(client):
    response = await client.get("/api/health", headers={"X-Request-Id": "caller-supplied-id"})
    assert response.headers["x-request-id"] == "caller-supplied-id"


async def test_health_deep_reports_db_ok_and_degraded_dependencies(client):
    response = await client.get("/api/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["version"] == API_VERSION
    assert body["checks"]["db"] == "ok"
    # No Redis and no Temporal in this configuration: advisory, never fatal.
    assert body["checks"]["redis"] != "ok"
    assert body["checks"]["temporal"] != "ok"


async def test_health_deep_503s_when_the_database_is_down(app, tolerant_client):
    """db is the only required dependency; a dead db must fail readiness."""
    from app.db import get_db

    class DeadSession:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("connection refused")

    async def _dead_db():
        yield DeadSession()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _dead_db
    try:
        response = await tolerant_client.get("/api/health/deep")
    finally:
        if previous is not None:
            app.dependency_overrides[get_db] = previous
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["checks"]["db"].startswith("error")
