"""Every contract route must be exercised by the suite.

`conftest` records the method and path of every request any test sends. This
module runs last and maps those concrete paths back onto the app's route
templates, so a route added without a test shows up here rather than shipping
untested.

Only meaningful on a full run: when pytest is pointed at a subset the guard
skips, because a partial run legitimately touches a subset of routes.
"""

from __future__ import annotations

import pytest
from starlette.routing import Route

from tests.conftest import REQUESTED
from tests.test_contract_docs import EXCLUDED

pytestmark = pytest.mark.contract


def _contract_routes(application) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in application.routes:
        if not isinstance(route, Route) or route.path in EXCLUDED:
            continue
        for method in route.methods or set():
            if method not in ("HEAD", "OPTIONS"):
                found.add((method, route.path))
    return found


def _exercised(application) -> set[tuple[str, str]]:
    """Resolve each recorded concrete path back to the route template it hit."""
    routes = [r for r in application.routes if isinstance(r, Route)]
    hit: set[tuple[str, str]] = set()
    for method, path in REQUESTED:
        for route in routes:
            if route.path_regex.match(path) and method in (route.methods or set()):
                hit.add((method, route.path))
                break
    return hit


def _full_run(request) -> bool:
    return bool(getattr(request.config, "stash_nesq_full_run", False))


def test_every_contract_route_is_exercised(app, request):
    if not _full_run(request):
        pytest.skip("partial run: the route-coverage guard only applies to a full suite run")

    missing = _contract_routes(app) - _exercised(app)
    assert not missing, "routes with no test coverage:\n" + "\n".join(
        f"  {method:7} {path}" for method, path in sorted(missing, key=lambda p: p[::-1])
    )


def test_the_recorder_saw_a_plausible_amount_of_traffic(request):
    if not _full_run(request):
        pytest.skip("partial run")
    assert len(REQUESTED) > 100, f"the recorder only saw {len(REQUESTED)} distinct requests"
