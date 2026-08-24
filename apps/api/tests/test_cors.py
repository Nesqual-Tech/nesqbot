"""CORS configuration.

`allow_origins=["*"]` together with `allow_credentials=True` is not a valid CORS
configuration — browsers reject it — and it states an intent to trust every
origin with bearer tokens. The API must never be built that way, whatever
`CORS_ORIGINS` happens to hold.
"""

from __future__ import annotations

import pytest
from starlette.middleware.cors import CORSMiddleware

from app.config import Settings
from app.main import DEV_CORS_ORIGINS, create_app, resolve_cors_origins


def _cors_options(application) -> dict:
    for middleware in application.user_middleware:
        if middleware.cls is CORSMiddleware:
            return dict(middleware.kwargs)
    raise AssertionError("the app has no CORS middleware")


def test_the_running_app_never_pairs_a_wildcard_origin_with_credentials(app):
    options = _cors_options(app)
    if options.get("allow_credentials"):
        assert "*" not in options["allow_origins"]
        assert options["allow_origins"], "credentialed CORS needs an explicit origin list"


def test_an_empty_cors_origins_falls_back_to_explicit_dev_origins(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "")
    settings = Settings(cors_origins="", nesq_env="development")
    origins = resolve_cors_origins(settings)
    assert origins == DEV_CORS_ORIGINS
    assert "*" not in origins
    assert all(o.startswith("http://localhost") or o.startswith("http://127.0.0.1") for o in origins)


def test_an_empty_cors_origins_is_fatal_in_production():
    settings = Settings(cors_origins="", nesq_env="production")
    with pytest.raises(RuntimeError, match="CORS_ORIGINS is empty"):
        resolve_cors_origins(settings)


@pytest.mark.parametrize("value", ["*", "http://localhost:1420,*", " * "])
def test_a_wildcard_origin_is_rejected_outright(value):
    settings = Settings(cors_origins=value, nesq_env="development")
    with pytest.raises(RuntimeError, match="may not contain"):
        resolve_cors_origins(settings)


def test_an_explicit_origin_list_is_used_verbatim():
    settings = Settings(cors_origins="https://app.example.com, https://admin.example.com")
    assert resolve_cors_origins(settings) == [
        "https://app.example.com",
        "https://admin.example.com",
    ]


def test_a_freshly_built_app_with_no_cors_origins_is_still_not_a_wildcard(monkeypatch):
    """End to end: build the real app with CORS_ORIGINS unset."""
    from app import main as main_module

    monkeypatch.setenv("CORS_ORIGINS", "")
    monkeypatch.setattr(
        main_module, "get_settings", lambda: Settings(cors_origins="", nesq_env="development")
    )
    application = create_app()
    options = _cors_options(application)
    assert options["allow_credentials"] is True
    assert "*" not in options["allow_origins"]
    assert options["allow_origins"] == DEV_CORS_ORIGINS


async def test_preflight_from_an_allowed_origin_is_answered(app, client):
    origin = _cors_options(app)["allow_origins"][0]
    response = await client.options(
        "/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers.get("access-control-allow-credentials") == "true"


async def test_a_response_never_echoes_a_wildcard_allow_origin(client):
    response = await client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert response.headers.get("access-control-allow-origin") != "*"
