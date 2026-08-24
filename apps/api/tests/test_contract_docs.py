"""Contract coverage: `docs/API.md` and the FastAPI app must agree.

`docs/API.md` is the binding contract. This module parses its route tables and
checks the mapping in both directions:

* every documented route exists in the app;
* every route the app serves is documented.

Path parameter *names* are normalised away (`{id}` in the docs vs `{approval_id}`
in the code is the same route), as are Starlette path converters
(`{token:path}`). The OpenAPI surface (`/docs`, `/redoc`, `/openapi.json`) is
excluded by design.
"""

from __future__ import annotations

import re

import pytest
from starlette.routing import Route

from tests.conftest import DOCS_API_MD

pytestmark = pytest.mark.contract

#: Documented paths are relative to the `/api` mount.
API_PREFIX = "/api"

#: Interactive docs are not part of the contract. `/docs/oauth2-redirect` is
#: FastAPI's Swagger OAuth2 helper: it comes from `swagger_ui_oauth2_redirect_url`,
#: which is not derived from `docs_url`, so it lands unprefixed. See
#: `test_swagger_oauth2_redirect_is_mounted_under_the_api_prefix` below.
EXCLUDED = {
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/api/docs/oauth2-redirect",
    "/docs/oauth2-redirect",
}

#: Routes this lane serves that `docs/API.md` has not been written for yet.
#:
#: `docs/API.md` is owned by a different lane, so a route can land here one
#: commit before its documentation does. This list is the seam, and it is
#: deliberately an explicit enumeration rather than a prefix or a wildcard: a
#: route must be named here by hand, and the guard below fails the moment a
#: named route *is* documented, so the list cannot rot into a permanent
#: exemption. It must be empty in a released build.
PENDING_DOCS: set[tuple[str, str]] = set()

_PARAM_RE = re.compile(r"\{[^}]*\}")
_ROW_RE = re.compile(r"^\|\s*([A-Z]+)\s*\|\s*`([^`]+)`\s*\|")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def normalise(path: str) -> str:
    """Collapse parameter names and converters so two spellings compare equal."""
    path = path.split("?", 1)[0].rstrip("/") or "/"
    return _PARAM_RE.sub("{}", path)


def documented_routes() -> set[tuple[str, str]]:
    """`(METHOD, normalised path)` for every row in the docs' route tables."""
    found: set[tuple[str, str]] = set()
    for line in DOCS_API_MD.read_text(encoding="utf-8").splitlines():
        match = _ROW_RE.match(line.strip())
        if not match:
            continue
        method, raw_path = match.group(1), match.group(2)
        if method not in _METHODS or not raw_path.startswith("/"):
            continue
        found.add((method, normalise(API_PREFIX + raw_path)))
    return found


def app_routes(application) -> set[tuple[str, str]]:
    """`(METHOD, normalised path)` for every HTTP route the app actually serves."""
    found: set[tuple[str, str]] = set()
    for route in application.routes:
        if not isinstance(route, Route):
            continue
        if route.path in EXCLUDED or normalise(route.path) in EXCLUDED:
            continue
        for method in route.methods or set():
            if method in ("HEAD", "OPTIONS"):
                continue
            found.add((method, normalise(route.path)))
    return found


def _render(pairs) -> str:
    return "\n".join(f"  {method:7} {path}" for method, path in sorted(pairs, key=lambda p: p[::-1]))


# ---------------------------------------------------------------------------
# The parser itself, so a silent mis-parse cannot make the checks vacuous
# ---------------------------------------------------------------------------


def test_the_docs_file_is_where_the_suite_expects_it():
    assert DOCS_API_MD.exists(), f"docs/API.md not found at {DOCS_API_MD}"


def test_the_docs_parser_finds_a_plausible_number_of_routes():
    routes = documented_routes()
    assert len(routes) > 60, f"only parsed {len(routes)} routes out of docs/API.md"


def test_the_parser_normalises_parameter_names_and_converters():
    assert normalise("/api/approvals/{id}/decide") == normalise("/api/approvals/{approval_id}/decide")
    assert normalise("/api/me/devices/{token}") == normalise("/api/me/devices/{token:path}")
    assert normalise("/api/routines?bot_id=") == "/api/routines"
    assert normalise("/api/bots/{bot_id}/desktop/stop?wipe=") == "/api/bots/{}/desktop/stop"


def test_a_few_landmark_routes_parse_out_of_the_docs():
    routes = documented_routes()
    for landmark in [
        ("GET", "/api/health"),
        ("POST", "/api/threads/{}/messages/stream"),
        ("GET", "/api/threads/{}/events"),
        ("POST", "/api/approvals/{}/decide"),
        ("POST", "/api/bots/{}/connectors/{}/actions/{}"),
        ("POST", "/api/bots/{}/desktop/action"),
        ("POST", "/api/evals/suite"),
    ]:
        assert landmark in routes, f"{landmark} did not parse out of docs/API.md"


# ---------------------------------------------------------------------------
# Both directions
# ---------------------------------------------------------------------------


def test_every_documented_route_exists_in_the_app(app):
    missing = documented_routes() - app_routes(app)
    assert not missing, (
        "docs/API.md documents routes the app does not serve:\n" + _render(missing)
    )


def test_every_app_route_is_documented(app):
    undocumented = app_routes(app) - documented_routes() - PENDING_DOCS
    assert not undocumented, (
        "the app serves routes docs/API.md does not document:\n" + _render(undocumented)
    )


def test_the_two_sets_are_identical(app):
    assert app_routes(app) - PENDING_DOCS == documented_routes()


def test_the_pending_docs_list_is_only_ever_routes_the_app_actually_serves(app):
    """A stale exemption is worse than none — it silently excuses a real gap."""
    stale = PENDING_DOCS - app_routes(app)
    assert not stale, "PENDING_DOCS names routes the app does not serve:\n" + _render(stale)


def test_a_documented_route_is_removed_from_the_pending_docs_list():
    """The seam closes itself: documenting a route fails until it is delisted."""
    already_documented = PENDING_DOCS & documented_routes()
    assert not already_documented, (
        "docs/API.md now documents these routes; delete them from PENDING_DOCS:\n"
        + _render(already_documented)
    )


# ---------------------------------------------------------------------------
# Shape checks on the served surface
# ---------------------------------------------------------------------------


def test_every_route_is_mounted_under_the_api_prefix(app):
    for _method, path in app_routes(app):
        assert path.startswith(API_PREFIX), f"{path} is not under {API_PREFIX}"


def test_swagger_oauth2_redirect_is_mounted_under_the_api_prefix(app):
    """docs/API.md opens with "All routes under /api".

    FastAPI defaults its Swagger OAuth2 helper to an unprefixed
    /docs/oauth2-redirect rather than deriving it from docs_url, which breaks a
    reverse proxy forwarding only /api. main.py passes
    swagger_ui_oauth2_redirect_url explicitly to keep the contract true.
    """
    paths = {route.path for route in app.routes if isinstance(route, Route)}
    assert "/docs/oauth2-redirect" not in paths
    assert "/api/docs/oauth2-redirect" in paths


def test_the_openapi_document_is_served_under_the_api_prefix(app):
    assert app.openapi_url == "/api/openapi.json"
    assert app.docs_url == "/api/docs"
    assert app.redoc_url == "/api/redoc"


def test_the_openapi_document_builds(app):
    schema = app.openapi()
    assert schema["info"]["title"] == "Nesq Bot API"
    assert schema["paths"], "the OpenAPI document has no paths"


def test_every_openapi_tag_is_used(app):
    declared = {tag["name"] for tag in app.openapi_tags or []}
    used = set()
    for route in app.routes:
        if isinstance(route, Route):
            used.update(getattr(route, "tags", []) or [])
    assert declared == used, f"declared but unused: {declared - used}; used but undeclared: {used - declared}"


def test_no_route_is_registered_twice(app):
    seen: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, Route) or route.path in EXCLUDED:
            continue
        for method in route.methods or set():
            if method in ("HEAD", "OPTIONS"):
                continue
            seen.append((method, route.path))
    duplicates = {pair for pair in seen if seen.count(pair) > 1}
    assert not duplicates, f"duplicate route registrations: {sorted(duplicates)}"


async def test_the_openapi_document_is_reachable(client):
    response = await client.get("/api/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["version"]
