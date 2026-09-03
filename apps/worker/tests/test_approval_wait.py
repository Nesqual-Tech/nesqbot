"""An unanswered approval must time out cleanly instead of wedging the worker.

Two failures live here, both from the same three lines of the old code:

* the activity's own deadline (`worker_approval_timeout_seconds`, 86400s) was
  the same 86400 seconds as Temporal's `start_to_close` for that activity, so
  which timer fired first was a coin flip — and Temporal winning produced a
  *retryable* timeout under STANDARD_RETRY's 3 attempts, i.e. a run alive for
  up to 72 hours instead of a clean abort at 24;
* the wait was a blocking poll inside an activity, so every parked approval held
  one of `worker_max_concurrent_activities` (20) slots for the whole span. Twenty
  unanswered approvals left a worker that could execute nothing while
  `_health_loop` kept the container HEALTHCHECK green.

The unit tests run everywhere; the workflow tests skip where the time-skipping
test server binary is unavailable.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")
pytest_asyncio = pytest.importorskip("pytest_asyncio", reason="pytest-asyncio not installed")

import httpx  # noqa: E402
from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from worker import activities as acts  # noqa: E402
from worker import workflows as wf  # noqa: E402

APPROVAL_ID = "approval-mcp-1"

GATED_STEPS: list[dict[str, Any]] = [
    {"type": "mcp", "mcp_id": "mcp-1", "tool": "send_invoice", "arguments": {"amount": 4200}},
    {"type": "connector", "connector_id": "crm", "action": "log_activity"},
]


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


# --------------------------------------------------------------------------- #
# The activity: one bounded attempt, never a 24-hour block
# --------------------------------------------------------------------------- #


async def test_an_undecided_approval_returns_pending_within_its_budget(monkeypatch):
    """Nobody decided: the activity hands control back instead of parking.

    Before this, the same call blocked for `worker_approval_timeout_seconds`
    (86400s) holding an activity slot.
    """
    seen = _transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"id": APPROVAL_ID, "status": "pending"}),
    )

    result = await acts.wait_for_approval_activity(
        {
            "approval_id": APPROVAL_ID,
            "poll_budget_seconds": 0.05,
            "poll_interval_seconds": 0.01,
        }
    )

    assert result == {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "approved": False,
        "note": "no decision within 0s",
    }
    # `pending` is what the workflow loop treats as "ask again", so it must be a
    # status the shared set recognises.
    assert result["status"] in acts.PENDING_APPROVAL_STATUSES
    assert seen, "the activity polled at least once before giving the slot back"


async def test_a_decision_on_the_first_poll_is_returned_immediately(monkeypatch):
    """The fast path is unchanged: one GET, one answer, no extra latency."""
    seen = _transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"id": APPROVAL_ID, "status": "approved", "note": "ok to pay"}
        ),
    )

    result = await acts.wait_for_approval_activity(
        {"approval_id": APPROVAL_ID, "poll_budget_seconds": 5.0, "poll_interval_seconds": 0.01}
    )

    assert len(seen) == 1
    assert result["approved"] is True
    assert result["status"] == "approved"


async def test_a_decision_mid_budget_is_returned_without_waiting_it_out(monkeypatch):
    """A human deciding during an attempt is picked up by that same attempt."""
    answers = ["pending", "pending", "rejected"]

    def handler(request: httpx.Request) -> httpx.Response:
        status = answers.pop(0) if answers else "rejected"
        return httpx.Response(200, json={"id": APPROVAL_ID, "status": status})

    _transport(monkeypatch, handler)

    result = await acts.wait_for_approval_activity(
        {"approval_id": APPROVAL_ID, "poll_budget_seconds": 5.0, "poll_interval_seconds": 0.01}
    )

    assert result["status"] == "rejected"
    assert result["approved"] is False


async def test_a_caller_whose_whole_deadline_fits_one_attempt_still_gets_timed_out(monkeypatch):
    """Compatibility: a direct caller passing a short `timeout_seconds` owns the
    whole wait, so exhausting it is a timeout, not a `pending` to poll again."""
    _transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"id": APPROVAL_ID, "status": "pending"}),
    )

    result = await acts.wait_for_approval_activity(
        {
            "approval_id": APPROVAL_ID,
            "timeout_seconds": 0.05,
            "poll_budget_seconds": 5.0,
            "poll_interval_seconds": 0.01,
        }
    )

    assert result["status"] == "timed_out"
    assert result["approved"] is False


def test_the_activity_deadline_is_strictly_inside_its_start_to_close():
    """The arithmetic that was wrong: both were 86400 seconds, so the winner was
    a coin flip and Temporal winning meant a retryable timeout."""
    assert wf.APPROVAL_START_TO_CLOSE.total_seconds() > acts.APPROVAL_POLL_BUDGET_SECONDS
    # And an attempt is minutes, not a day: it must not hold a slot overnight.
    assert acts.APPROVAL_POLL_BUDGET_SECONDS <= 120.0
    # The heartbeat window has to cover several polls at the configured interval.
    assert wf.APPROVAL_HEARTBEAT.total_seconds() > 10.0


# --------------------------------------------------------------------------- #
# The workflow: the 24-hour wait is a durable timer, and it really expires
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


def _mocks(approval_answers: list[dict[str, Any]], calls: dict[str, list[Any]]):
    """Activity stand-ins where the approval poller answers from a script."""

    @activity.defn(name="run_step_activity")
    async def run_step_activity(payload: dict[str, Any]) -> dict[str, Any]:
        calls["steps"].append(payload)
        if payload["step"]["type"] == "mcp":
            return {
                "ok": True,
                "index": payload["index"],
                "type": "mcp",
                "awaiting_approval": APPROVAL_ID,
                "risk": "spend",
            }
        return {"ok": True, "index": payload["index"], "type": "connector", "result": {}}

    @activity.defn(name="wait_for_approval_activity")
    async def wait_for_approval_activity(payload: dict[str, Any]) -> dict[str, Any]:
        calls["approvals"].append(payload)
        if approval_answers:
            return approval_answers.pop(0)
        # Never decided: exactly what the real activity returns when its own
        # poll budget runs out.
        return {"approval_id": APPROVAL_ID, "status": "pending", "approved": False}

    @activity.defn(name="record_run_status_activity")
    async def record_run_status_activity(payload: dict[str, Any]) -> dict[str, Any]:
        calls["status"].append(payload)
        return {"ok": True}

    return [run_step_activity, wait_for_approval_activity, record_run_status_activity]


async def _run(env, activities, payload):
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[wf.RoutineWorkflow],
        activities=activities,
    ):
        return await env.client.execute_workflow(
            wf.RoutineWorkflow.run,
            payload,
            id=f"wf-{uuid.uuid4()}",
            task_queue=task_queue,
        )


def _payload(**extra: Any) -> dict[str, Any]:
    return {
        "routine_id": "routine-abc",
        "bot_id": "bot-1",
        "run_id": "run-1",
        "steps": [dict(s) for s in GATED_STEPS],
        **extra,
    }


async def test_an_approval_nobody_answers_aborts_the_routine_at_its_deadline(env):
    """The clean `timed_out` abort is now the guaranteed outcome, not a race.

    A one-hour deadline keeps the test small; the mechanism is the same at 24
    hours. Time-skipping makes the durable timers instant, which is the proof
    they are workflow timers and not activity `asyncio.sleep`s.
    """
    calls: dict[str, list[Any]] = {"steps": [], "approvals": [], "status": []}

    result = await _run(env, _mocks([], calls), _payload(approval_timeout_seconds=3600))

    assert result["status"] == "aborted"
    assert result["ok"] is False
    assert result["failed_index"] == 0
    assert "timed_out" in (result["error"] or "")
    # The step after the gate never ran, and the abort was reported.
    assert [c["index"] for c in calls["steps"]] == [0]
    assert calls["status"][-1]["status"] == "aborted"
    # Polled repeatedly, in short attempts, rather than blocking once for an hour.
    assert len(calls["approvals"]) > 3
    assert all(c["poll_budget_seconds"] == acts.APPROVAL_POLL_BUDGET_SECONDS for c in calls["approvals"])


async def test_an_approval_decided_on_a_later_poll_resumes_the_routine(env):
    """Two `pending` attempts must not be read as a rejection or a timeout."""
    calls: dict[str, list[Any]] = {"steps": [], "approvals": [], "status": []}
    answers = [
        {"approval_id": APPROVAL_ID, "status": "pending", "approved": False},
        {"approval_id": APPROVAL_ID, "status": "pending", "approved": False},
        {"approval_id": APPROVAL_ID, "status": "approved", "approved": True, "note": "ok"},
    ]

    result = await _run(env, _mocks(answers, calls), _payload())

    assert result["status"] == "completed"
    assert len(calls["approvals"]) == 3
    assert [c["index"] for c in calls["steps"]] == [0, 1]
    assert result["results"][0]["approval"]["approved"] is True
