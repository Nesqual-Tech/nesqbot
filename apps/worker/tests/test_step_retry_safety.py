"""A routine step whose answer is lost must not be sent again.

The worker stamps `Idempotency-Key` on every step POST and its own docstring
says that key "lets the API de-duplicate a replayed side effect". Measured
against this tree, exactly one endpoint reads it — `POST /threads/{id}/messages`
(`apps/api/app/routers/threads.py:270`) — so for connector, desktop, MCP and
approval steps a retry is a second real side effect. These tests pin the one
distinction that makes that safe: a request that never reached the API keeps
retrying, a request that was delivered and left unanswered stops the routine.

The failure they exist to catch is "the routine sent the invoice three times":
an `httpx.ReadTimeout` on a `send_mail` step used to propagate raw out of the
activity, which Temporal treats as retryable, and STANDARD_RETRY then granted
three attempts.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")
pytest_asyncio = pytest.importorskip("pytest_asyncio", reason="pytest-asyncio not installed")

import httpx  # noqa: E402
from temporalio import activity  # noqa: E402
from temporalio.client import WorkflowFailureError  # noqa: E402
from temporalio.exceptions import ApplicationError  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from worker import activities as acts  # noqa: E402
from worker.workflows import RoutineWorkflow  # noqa: E402

CONNECTOR_STEP: dict[str, Any] = {
    "type": "connector",
    "connector_id": "microsoft_graph",
    "action": "send_mail",
    "input": {"to": "a@b.c", "subject": "invoice"},
    "risk": "send",
}


def _step_payload() -> dict[str, Any]:
    return {
        "index": 0,
        "bot_id": "bot-1",
        "routine_id": "routine-abc",
        "run_id": "run-1",
        "step": dict(CONNECTOR_STEP),
    }


def _transport(monkeypatch, handler) -> list[httpx.Request]:
    """Route every activity HTTP call at `handler`; return captured requests."""
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


def _raises(exc: BaseException):
    def _handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return _handler


# --------------------------------------------------------------------------- #
# Activity-level classification (no Temporal server needed)
# --------------------------------------------------------------------------- #


async def test_read_timeout_on_a_send_step_is_not_retried(monkeypatch):
    """The request was delivered and the answer lost: unknown, so stop."""
    _transport(monkeypatch, _raises(httpx.ReadTimeout("timed out")))

    with pytest.raises(ApplicationError) as excinfo:
        await acts.run_step_activity(_step_payload())

    assert excinfo.value.non_retryable is True
    assert excinfo.value.type == acts.UNKNOWN_OUTCOME_ERROR_TYPE
    message = str(excinfo.value)
    assert "outcome is unknown" in message
    assert "not retried" in message
    # The operator is told what to check before re-running.
    assert "idempotency key" in message


async def test_remote_protocol_error_on_a_step_is_not_retried(monkeypatch):
    """A connection that dies mid-response is the same unknown outcome."""
    _transport(monkeypatch, _raises(httpx.RemoteProtocolError("peer closed")))

    with pytest.raises(ApplicationError) as excinfo:
        await acts.run_step_activity(_step_payload())

    assert excinfo.value.non_retryable is True


async def test_connect_error_on_a_step_stays_retryable(monkeypatch):
    """No connection means no handler ran, so an API that is down gets retried.

    Without this the fix would trade duplicate sends for routines that die on
    every ordinary API restart.
    """
    _transport(monkeypatch, _raises(httpx.ConnectError("connection refused")))

    with pytest.raises(ApplicationError) as excinfo:
        await acts.run_step_activity(_step_payload())

    assert not excinfo.value.non_retryable
    assert excinfo.value.type == acts.RETRYABLE_ERROR_TYPE
    assert "never reached the API" in str(excinfo.value)


async def test_connect_timeout_and_pool_timeout_stay_retryable(monkeypatch):
    """Both are connect-phase: the request never left the worker."""
    for exc in (httpx.ConnectTimeout("slow"), httpx.PoolTimeout("no slot")):
        _transport(monkeypatch, _raises(exc))
        with pytest.raises(ApplicationError) as excinfo:
            await acts.run_step_activity(_step_payload())
        assert not excinfo.value.non_retryable, exc


async def test_http_408_on_a_step_is_not_retried(monkeypatch):
    """408 is a timer between us and the handler, not the handler declining.

    `_check` maps 408 to retryable, which is right for a read and wrong for a
    send, so steps reclassify it.
    """
    _transport(monkeypatch, lambda request: httpx.Response(408, json={"detail": "request timeout"}))

    with pytest.raises(ApplicationError) as excinfo:
        await acts.run_step_activity(_step_payload())

    assert excinfo.value.non_retryable is True
    assert excinfo.value.type == acts.UNKNOWN_OUTCOME_ERROR_TYPE


async def test_http_504_on_a_step_is_not_retried(monkeypatch):
    """A gateway that gave up on the upstream may have let the send through."""
    _transport(monkeypatch, lambda request: httpx.Response(504, text="gateway timeout"))

    with pytest.raises(ApplicationError) as excinfo:
        await acts.run_step_activity(_step_payload())

    assert excinfo.value.non_retryable is True


async def test_http_503_on_a_step_stays_retryable(monkeypatch):
    """503 is the API refusing before it ran anything: retry is safe and wanted."""
    _transport(monkeypatch, lambda request: httpx.Response(503, json={"detail": "unavailable"}))

    with pytest.raises(ApplicationError) as excinfo:
        await acts.run_step_activity(_step_payload())

    assert not excinfo.value.non_retryable
    assert excinfo.value.type == acts.RETRYABLE_ERROR_TYPE


async def test_post_message_transport_failure_is_still_retryable(monkeypatch):
    """Scope check: the thread-message POST is the one endpoint that replays.

    `POST /threads/{id}/messages` honours `Idempotency-Key`, so narrowing must
    not spill onto it — a lost answer there costs nothing to ask again.
    """
    _transport(monkeypatch, _raises(httpx.ReadTimeout("timed out")))

    with pytest.raises(httpx.ReadTimeout):
        await acts.post_message_activity({"thread_id": "thread-1", "message": "hi"})


# --------------------------------------------------------------------------- #
# Workflow level: the whole point is the attempt count
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def env():
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - binary download/exec not possible here
        pytest.skip(f"time-skipping test server unavailable: {exc}")
    try:
        yield environment
    finally:
        await environment.shutdown()


async def test_a_delivered_step_with_no_answer_runs_exactly_once(env):
    """The mirror of `test_failing_step_retries_then_surfaces_error`.

    That test asserts a genuinely failed step is attempted 3 times. This one
    asserts a step whose *outcome is unknown* is attempted once — before the
    fix it was also 3, i.e. three sends.
    """
    attempts: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    @activity.defn(name="run_step_activity")
    async def run_step_activity(payload: dict[str, Any]) -> dict[str, Any]:
        attempts.append(payload)
        # Exactly what the real activity now raises on a ReadTimeout.
        raise acts._unknown_outcome(
            "ReadTimeout: timed out", op="run_step[connector]", index=payload["index"], key="k"
        )

    @activity.defn(name="record_run_status_activity")
    async def record_run_status_activity(payload: dict[str, Any]) -> dict[str, Any]:
        statuses.append(payload)
        return {"ok": True}

    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[RoutineWorkflow],
        activities=[run_step_activity, record_run_status_activity],
    ):
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                RoutineWorkflow.run,
                {
                    "routine_id": "routine-abc",
                    "bot_id": "bot-1",
                    "run_id": "run-1",
                    "steps": [dict(CONNECTOR_STEP)],
                },
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    assert len(attempts) == 1, "an unknown-outcome step must not be replayed"
    # And the human is told, rather than the routine dying silently.
    assert statuses[-1]["status"] == "failed"
    assert "outcome is unknown" in (statuses[-1]["error"] or "")
