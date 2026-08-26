"""`Orchestrator._tool_result_text` must tell the model when a desktop action
was not real.

`BOT_DESKTOP_MODE=mock` (the local-dev default) fakes a successful result for
every desktop action so a bot can be exercised with no Docker/ACI/k8s desktop
behind it — see `DesktopManager`'s `mode == "mock"` branches in
`app/services/desktop.py`. The screenshot path already disclosed this to the
model (`screen.get("mock")` in `_desktop_observation_message`); the plain
action-result text did not, so a `click`/`type`/`scroll`/... in mock mode was
reported to the model as "ran and reported success" with no caveat, and the
model narrated it as a real action it never took. This is the regression test
for that fix.
"""

from __future__ import annotations

from app.services.orchestrator import Orchestrator


def _orchestrator() -> Orchestrator:
    return Orchestrator()


def test_a_mock_action_result_discloses_it_is_not_real():
    text = _orchestrator()._tool_result_text("click", {"ok": True, "mock": True})
    assert "ran and reported success" in text
    assert "no real desktop" in text
    assert "BOT_DESKTOP_MODE=mock" in text


def test_a_real_action_result_carries_no_mock_disclosure():
    text = _orchestrator()._tool_result_text("click", {"ok": True, "mock": False})
    assert "ran and reported success" in text
    assert "mock" not in text.lower()


def test_a_mock_result_missing_the_key_entirely_is_treated_as_real():
    # `outcome_result.get("mock")` on a dict that never sets the key at all —
    # the docker/aci/k8s backends' results, which have no reason to carry it.
    text = _orchestrator()._tool_result_text("type", {"ok": True})
    assert "mock" not in text.lower()


def test_a_failed_action_reports_failure_before_any_mock_check():
    text = _orchestrator()._tool_result_text("click", {"ok": False, "mock": True, "error": "boom"})
    assert "FAILED" in text
    assert "boom" in text
    # A failure is already the honest answer; it does not also need the mock
    # caveat layered on top of it.
    assert "no real desktop" not in text
