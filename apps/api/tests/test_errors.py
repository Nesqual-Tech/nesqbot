"""The error envelope.

Handled errors are `{"detail", "code"}`. Validation failures add `errors[]` under
code `validation_error`. Unhandled errors are a 500 carrying `request_id` and
nothing else — never a traceback, never an exception message.
"""

from __future__ import annotations

import uuid

import pytest

from app.errors import STATUS_CODES, AppError, bad_request, conflict, forbidden, not_found

MISSING = uuid.uuid4()


@pytest.fixture
def boom_app():
    """A private app instance carrying a route that raises, so the global app is untouched."""
    from app.main import create_app

    application = create_app()

    @application.get("/api/boom")
    async def _boom() -> dict:  # pragma: no cover - always raises
        raise RuntimeError("secret internals: connection string postgres://nesq:nesq@db/nesqbot")

    @application.get("/api/teapot")
    async def _teapot() -> dict:
        raise AppError(418, "im_a_teapot", "Short and stout", extra={"hint": "use a kettle"})

    return application


@pytest.fixture
async def boom_client(boom_app):
    from tests.conftest import _client_for

    async with _client_for(boom_app, raise_app_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Handled errors
# ---------------------------------------------------------------------------


async def test_a_handled_404_is_detail_plus_code(authed):
    response = await authed.get(f"/api/bots/{MISSING}")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"detail", "code"}
    assert body["code"] == "bot_not_found"
    assert isinstance(body["detail"], str)


async def test_a_handled_403_is_detail_plus_code(authed, system_bot):
    response = await authed.delete(f"/api/bots/{system_bot.id}")
    assert response.status_code == 403
    assert set(response.json()) == {"detail", "code"}


async def test_a_handled_409_is_detail_plus_code(authed, make_approval, bot_a):
    approval = await make_approval(bot_a, status="approved")
    response = await authed.post(f"/api/approvals/{approval.id}/expire")
    assert response.status_code == 409
    assert response.json()["code"] == "approval_not_pending"


async def test_a_401_from_a_starlette_http_exception_still_carries_a_code(anon):
    response = await anon.get("/api/me")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "unauthorized"
    assert body["detail"]


async def test_an_app_error_can_carry_extra_fields(boom_client):
    response = await boom_client.get("/api/teapot")
    assert response.status_code == 418
    assert response.json() == {
        "detail": "Short and stout",
        "code": "im_a_teapot",
        "hint": "use a kettle",
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_a_422_is_validation_error_with_errors(authed, bot_a):
    response = await authed.patch(f"/api/bots/{bot_a.id}/budget", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["detail"] == "Request validation failed"
    assert isinstance(body["errors"], list) and body["errors"]
    first = body["errors"][0]
    assert {"loc", "msg", "type"} <= set(first)


async def test_a_malformed_uuid_in_the_path_is_a_422(authed):
    response = await authed.get("/api/bots/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_a_bad_query_parameter_is_a_422(authed):
    response = await authed.get("/api/usage?days=0")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_invalid_json_body_is_a_422_not_a_500(authed, bot_a):
    response = await authed.patch(
        f"/api/bots/{bot_a.id}/budget",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Unhandled errors
# ---------------------------------------------------------------------------


async def test_an_unhandled_error_is_a_500_with_a_request_id(boom_client):
    response = await boom_client.get("/api/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "internal_error"
    assert body["code"] == "internal_error"
    assert body["request_id"], "the 500 must carry the correlation id"
    assert set(body) == {"detail", "code", "request_id"}


async def test_the_500_request_id_matches_the_response_header(boom_client):
    response = await boom_client.get("/api/boom", headers={"X-Request-Id": "trace-me-123"})
    assert response.status_code == 500
    assert response.json()["request_id"] == "trace-me-123"
    assert response.headers["x-request-id"] == "trace-me-123"


async def test_the_500_leaks_no_traceback_or_exception_text(boom_client):
    response = await boom_client.get("/api/boom")
    payload = response.text
    for leak in ("Traceback", "RuntimeError", "secret internals", "postgres://", "app/main.py"):
        assert leak not in payload, f"the 500 body leaked {leak!r}"


# ---------------------------------------------------------------------------
# Helpers and the status -> code table
# ---------------------------------------------------------------------------


def test_status_code_table_covers_the_codes_the_api_returns():
    for status, code in {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        500: "internal_error",
        503: "service_unavailable",
    }.items():
        assert STATUS_CODES[status] == code


def test_error_helpers_build_the_documented_shape():
    assert not_found().body() == {"detail": "Not found", "code": "not_found"}
    assert forbidden().status_code == 403
    assert conflict().status_code == 409
    assert bad_request().status_code == 400


def test_app_error_defaults_its_detail_from_the_code():
    err = AppError(404, "bot_not_found")
    assert err.detail == "bot not found"
    assert err.status == err.status_code == 404
