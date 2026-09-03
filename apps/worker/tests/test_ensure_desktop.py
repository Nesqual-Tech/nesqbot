"""`bot_desktop_mode=aks` has no reconciler, so waiting for it is a lie.

The API's aks branch (`app/services/desktop.py`) sets `state="starting"` and
`container_id = f"aks-pending-{bot.id}"` on the assumption that the worker
creates the pod. It does not: `apps/worker` contains no aks/pod/kubernetes code
at all, which is what `docs/STATUS.md` says too. `ensure_desktop_activity` could
not tell, so it polled for `worker_desktop_ready_timeout_seconds` (300s) and
then raised a *retryable* error that STANDARD_RETRY multiplied by 3 — fifteen
minutes of held activity slot per routine, reported as "not running after 300s
(state=starting)", a message that sends an operator hunting a slow container
image instead of a deployment mode with nothing behind it.

These tests pin the two halves: the placeholder fails immediately with the
answer in it, and a genuinely slow docker/k8s start is untouched.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

import httpx  # noqa: E402
from temporalio.exceptions import ApplicationError  # noqa: E402

from worker import activities as acts  # noqa: E402


def _transport(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    async def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _factory(settings, *, timeout=None):  # matches acts._client's signature
        return httpx.AsyncClient(
            base_url="http://api.test/api",
            transport=httpx.MockTransport(_handle),
        )

    monkeypatch.setattr(acts, "_client", _factory)
    return seen


async def test_an_aks_placeholder_desktop_fails_immediately_with_the_cause(monkeypatch):
    """Two seconds and a useful message, not fifteen minutes and a wrong one."""
    seen = _transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"state": "starting", "container_id": "aks-pending-bot-1"}
        ),
    )

    with pytest.raises(ApplicationError) as excinfo:
        await acts.ensure_desktop_activity({"bot_id": "bot-1"})

    assert excinfo.value.non_retryable is True
    message = str(excinfo.value)
    # Names the mode that is broken and the shipped one that works.
    assert "aks" in message
    assert "k8s" in message
    # The 300-second poll was skipped entirely: only the start POST happened.
    assert len(seen) == 1
    assert seen[0].url.path == "/api/bots/bot-1/desktop/start"


async def test_a_slow_but_real_desktop_start_still_polls_to_running(monkeypatch):
    """Control case: an ordinary container_id keeps the existing behaviour, so
    a genuinely slow docker or k8s start is not turned into a hard failure."""
    answers: list[dict[str, Any]] = [
        {"state": "starting", "container_id": "nesq-desktop-bot-1"},
        {"state": "starting", "container_id": "nesq-desktop-bot-1"},
        {"state": "running", "container_id": "nesq-desktop-bot-1", "stream_url": "ws://x"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers.pop(0) if len(answers) > 1 else answers[0])

    seen = _transport(monkeypatch, handler)

    state = await acts.ensure_desktop_activity(
        {"bot_id": "bot-1", "timeout_seconds": 5.0, "poll_interval_seconds": 0.01}
    )

    assert state["state"] == "running"
    assert len(seen) > 1, "it really polled rather than trusting the start answer"


async def test_a_desktop_that_regresses_to_the_placeholder_also_fails(monkeypatch):
    """The sentinel is checked on every probe, not only on the start answer."""
    answers: list[dict[str, Any]] = [
        {"state": "starting", "container_id": "nesq-desktop-bot-1"},
        {"state": "starting", "container_id": "aks-pending-bot-1"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers.pop(0) if len(answers) > 1 else answers[0])

    _transport(monkeypatch, handler)

    with pytest.raises(ApplicationError) as excinfo:
        await acts.ensure_desktop_activity(
            {"bot_id": "bot-1", "timeout_seconds": 5.0, "poll_interval_seconds": 0.01}
        )

    assert excinfo.value.non_retryable is True
