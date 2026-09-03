"""RoutineWorkflow tests against the Temporal time-skipping test environment.

Skipped automatically where `temporalio`, `pytest-asyncio`, or the downloadable
test-server binary is unavailable (CI sandboxes, offline builds).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")
pytest_asyncio = pytest.importorskip("pytest_asyncio", reason="pytest-asyncio not installed")

from temporalio import activity  # noqa: E402
from temporalio.client import WorkflowFailureError  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from worker.workflows import RoutineWorkflow, ScheduledRoutineWorkflow  # noqa: E402

DESKTOP_STEPS: list[dict[str, Any]] = [
    {"type": "desktop", "action": "click", "args": {"x": 10, "y": 20}},
    {"type": "connector", "connector_id": "crm", "action": "search_accounts", "input": {"query": "acme"}},
    {"type": "mcp", "mcp_id": "mcp-1", "tool": "lookup", "arguments": {"id": 7}},
]


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


def _mocks(
    step_behaviour,
    calls: dict[str, list[Any]],
    approval_decision: dict[str, Any] | None = None,
    sequence: list[str] | None = None,
):
    """Build mock activities registered under the real activity names.

    `sequence`, when given, records step executions and approval waits in the
    order they actually happened — that ordering is the proof a gated step
    parked *before* the rest of the routine ran, not alongside it.
    """

    def _mark(entry: str) -> None:
        if sequence is not None:
            sequence.append(entry)

    @activity.defn(name="ensure_desktop_activity")
    async def ensure_desktop_activity(payload: Any) -> dict[str, Any]:
        calls["desktop"].append(payload)
        return {"bot_id": payload.get("bot_id"), "state": "running"}

    @activity.defn(name="run_step_activity")
    async def run_step_activity(payload: dict[str, Any]) -> dict[str, Any]:
        calls["steps"].append(payload)
        _mark(f"step:{payload['index']}")
        return step_behaviour(payload)

    @activity.defn(name="wait_for_approval_activity")
    async def wait_for_approval_activity(payload: dict[str, Any]) -> dict[str, Any]:
        calls["approvals"].append(payload)
        _mark(f"approval:{payload['approval_id']}")
        return approval_decision or {
            "approval_id": payload["approval_id"],
            "status": "approved",
            "approved": True,
        }

    @activity.defn(name="record_run_status_activity")
    async def record_run_status_activity(payload: dict[str, Any]) -> dict[str, Any]:
        calls["status"].append(payload)
        return {"ok": True}

    @activity.defn(name="post_message_activity")
    async def post_message_activity(payload: dict[str, Any]) -> dict[str, Any]:
        calls["messages"].append(payload)
        return {"ok": True, "run_id": "run-1"}

    return [
        ensure_desktop_activity,
        run_step_activity,
        wait_for_approval_activity,
        record_run_status_activity,
        post_message_activity,
    ]


def _failure_text(err: BaseException) -> str:
    """Flatten a WorkflowFailureError cause chain into one searchable string."""
    parts: list[str] = []
    current: BaseException | None = err
    for _ in range(10):
        if current is None:
            break
        parts.append(str(current))
        message = getattr(current, "message", None)
        if message:
            parts.append(str(message))
        current = current.__cause__
    return " | ".join(parts)


def _new_calls() -> dict[str, list[Any]]:
    return {"desktop": [], "steps": [], "approvals": [], "status": [], "messages": []}


def _payload(steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "routine_id": "routine-abc",
        "bot_id": "bot-1",
        "run_id": "run-1",
        "steps": steps,
        **extra,
    }


async def _run(env, activities, payload, workflow=RoutineWorkflow):
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[RoutineWorkflow, ScheduledRoutineWorkflow],
        activities=activities,
    ):
        return await env.client.execute_workflow(
            workflow.run,
            payload,
            id=f"wf-{uuid.uuid4()}",
            task_queue=task_queue,
        )


async def test_routine_runs_all_steps_in_order(env):
    calls = _new_calls()

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "index": payload["index"], "type": payload["step"]["type"], "result": {}}

    result = await _run(env, _mocks(behaviour, calls), _payload(DESKTOP_STEPS))

    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["steps_total"] == 3
    assert result["steps_completed"] == 3
    assert [c["index"] for c in calls["steps"]] == [0, 1, 2]
    assert [c["step"]["type"] for c in calls["steps"]] == ["desktop", "connector", "mcp"]
    # A desktop step is present, so the desktop was ensured exactly once first.
    assert len(calls["desktop"]) == 1
    assert calls["status"][-1]["status"] == "completed"


async def test_non_desktop_routine_skips_desktop_boot(env):
    calls = _new_calls()
    steps = [{"type": "connector", "connector_id": "crm", "action": "ping"}]

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "index": payload["index"], "type": "connector", "result": {}}

    result = await _run(env, _mocks(behaviour, calls), _payload(steps))

    assert result["status"] == "completed"
    assert calls["desktop"] == []


async def test_rejected_approval_aborts_routine(env):
    calls = _new_calls()
    steps = [
        {"type": "approval", "risk": "send", "title": "Send outreach"},
        {"type": "connector", "connector_id": "crm", "action": "log_activity"},
    ]

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["step"]["type"] == "approval":
            return {
                "ok": True,
                "index": payload["index"],
                "type": "approval",
                "awaiting_approval": "approval-9",
            }
        return {"ok": True, "index": payload["index"], "type": "connector", "result": {}}

    activities = _mocks(
        behaviour,
        calls,
        approval_decision={"approval_id": "approval-9", "status": "rejected", "approved": False},
    )
    result = await _run(env, activities, _payload(steps))

    assert result["status"] == "aborted"
    assert result["ok"] is False
    assert result["failed_index"] == 0
    assert "rejected" in (result["error"] or "")
    # Step 1 must never have run.
    assert [c["index"] for c in calls["steps"]] == [0]
    assert calls["approvals"][0]["approval_id"] == "approval-9"
    assert calls["status"][-1]["status"] == "aborted"


async def test_approved_approval_continues_routine(env):
    calls = _new_calls()
    steps = [
        {"type": "approval", "risk": "send", "title": "Send outreach"},
        {"type": "connector", "connector_id": "crm", "action": "log_activity"},
    ]

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["step"]["type"] == "approval":
            return {
                "ok": True,
                "index": payload["index"],
                "type": "approval",
                "awaiting_approval": "approval-9",
            }
        return {"ok": True, "index": payload["index"], "type": "connector", "result": {}}

    result = await _run(env, _mocks(behaviour, calls), _payload(steps))

    assert result["status"] == "completed"
    assert [c["index"] for c in calls["steps"]] == [0, 1]
    assert result["steps_completed"] == 2


async def test_failing_step_retries_then_surfaces_error(env):
    calls = _new_calls()
    steps = [{"type": "connector", "connector_id": "crm", "action": "boom"}]

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("connector exploded")

    with pytest.raises(WorkflowFailureError) as excinfo:
        await _run(env, _mocks(behaviour, calls), _payload(steps))

    # STANDARD_RETRY allows 3 attempts before the workflow gives up.
    assert len(calls["steps"]) == 3
    assert "connector exploded" in _failure_text(excinfo.value)
    # The failure was reported back to the API before the workflow failed.
    assert calls["status"], "record_run_status_activity was never called"
    failure = calls["status"][-1]
    assert failure["status"] == "failed"
    assert "connector exploded" in (failure["error"] or "")


async def test_non_retryable_api_error_fails_fast(env):
    calls = _new_calls()
    steps = [{"type": "connector", "connector_id": "crm", "action": "nope"}]

    from temporalio.exceptions import ApplicationError

    from worker.activities import NON_RETRYABLE_ERROR_TYPE

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        raise ApplicationError(
            "HTTP 404: connector missing",
            type=NON_RETRYABLE_ERROR_TYPE,
            non_retryable=True,
        )

    with pytest.raises(WorkflowFailureError):
        await _run(env, _mocks(behaviour, calls), _payload(steps))

    assert len(calls["steps"]) == 1
    assert calls["status"][-1]["status"] == "failed"


async def test_scheduled_wrapper_delegates_to_child(env):
    calls = _new_calls()

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "index": payload["index"], "type": payload["step"]["type"], "result": {}}

    result = await _run(
        env,
        _mocks(behaviour, calls),
        _payload([{"type": "connector", "connector_id": "crm", "action": "ping"}]),
        workflow=ScheduledRoutineWorkflow,
    )

    assert result["status"] == "completed"
    assert result["routine_id"] == "routine-abc"
    assert len(calls["steps"]) == 1


async def test_user_id_propagates_to_steps(env):
    """An attended run must carry the initiating human down to every step, so
    `_approval_request` can stamp `requested_by` on approvals it files."""
    calls = _new_calls()

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "index": payload["index"], "type": "approval", "result": {}}

    steps = [{"type": "approval", "risk": "send", "title": "Send outreach"}]
    await _run(env, _mocks(behaviour, calls), _payload(steps, user_id="user-42"))

    assert calls["steps"][0]["user_id"] == "user-42"


async def test_unattended_run_has_no_user_id(env):
    """A cron schedule has no interactive human; the key is absent, not invented."""
    calls = _new_calls()

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "index": payload["index"], "type": "connector", "result": {}}

    steps = [{"type": "connector", "connector_id": "crm", "action": "ping"}]
    await _run(env, _mocks(behaviour, calls), _payload(steps))

    assert calls["steps"][0]["user_id"] is None


def test_approval_request_stamps_requested_by():
    from worker.activities import StepRequest, _approval_request

    attended = _approval_request(
        StepRequest(
            index=0,
            bot_id="bot-1",
            step={"type": "approval", "risk": "send", "title": "Send"},
            routine_id="r1",
            run_id="run-1",
            thread_id="thread-1",
            user_id="user-42",
        )
    )
    assert attended["payload"]["requested_by"] == "user-42"

    unattended = _approval_request(
        StepRequest(index=0, bot_id="bot-1", step={"type": "approval"}, routine_id="r1")
    )
    assert "requested_by" not in unattended["payload"]


def test_approval_request_keeps_explicit_requester():
    """An explicit requester in the step payload wins over the workflow's user."""
    from worker.activities import StepRequest, _approval_request

    body = _approval_request(
        StepRequest(
            index=0,
            bot_id="bot-1",
            step={"type": "approval", "payload": {"requested_by": "user-explicit"}},
            user_id="user-42",
        )
    )
    assert body["payload"]["requested_by"] == "user-explicit"


# --------------------------------------------------------------------------- #
# Gated MCP tool calls
#
# `POST /bots/{id}/mcp/{mcp_id}/call` classifies the tool *name* and answers
# 201 `PendingApprovalOut` instead of the tool result when the risk needs a
# human. That is the same answer the connector and desktop routes give, so a
# gated MCP step must park the routine exactly like any other gated step —
# never execute unattended, never be quietly reported as done.
# --------------------------------------------------------------------------- #

#: What the API sends back when the gate stops an MCP call.
PENDING_MCP_APPROVAL: dict[str, Any] = {
    "approval_id": "approval-mcp-1",
    "status": "pending_approval",
    "risk": "spend",
    "title": "MCP tool: send_invoice",
    "detail": "billing.send_invoice requires approval (risk=spend)",
}

GATED_MCP_STEPS: list[dict[str, Any]] = [
    {"type": "mcp", "mcp_id": "mcp-1", "tool": "send_invoice", "arguments": {"amount": 4200}},
    {"type": "connector", "connector_id": "crm", "action": "log_activity"},
]


def _gated_mcp_behaviour(executed: list[int]):
    """`run_step_activity` stand-in: the MCP step comes back gated.

    `executed` only ever records steps whose side effect actually ran, so a test
    can assert the invoice was never sent.
    """

    def behaviour(payload: dict[str, Any]) -> dict[str, Any]:
        step = payload["step"]
        if step["type"] == "mcp":
            # Shape produced by the real activity from a 201 PendingApprovalOut.
            return {
                "ok": True,
                "index": payload["index"],
                "type": "mcp",
                "awaiting_approval": PENDING_MCP_APPROVAL["approval_id"],
                "risk": PENDING_MCP_APPROVAL["risk"],
                "result": dict(PENDING_MCP_APPROVAL),
            }
        executed.append(payload["index"])
        return {"ok": True, "index": payload["index"], "type": step["type"], "result": {}}

    return behaviour


async def test_gated_mcp_step_parks_then_resumes_on_approval(env):
    """A gated MCP step parks the routine; a human approval releases it."""
    calls = _new_calls()
    sequence: list[str] = []
    executed: list[int] = []

    activities = _mocks(_gated_mcp_behaviour(executed), calls, sequence=sequence)
    result = await _run(env, activities, _payload(GATED_MCP_STEPS, user_id="user-42"))

    # The gate ran before anything downstream did, and exactly once.
    assert sequence == ["step:0", "approval:approval-mcp-1", "step:1"]
    # One bounded poll attempt, whose budget the workflow states explicitly so
    # an env override cannot make it outlive its own start_to_close.
    assert calls["approvals"] == [
        {
            "approval_id": "approval-mcp-1",
            "poll_budget_seconds": acts.APPROVAL_POLL_BUDGET_SECONDS,
        }
    ]
    # The MCP tool itself never executed — the API held it, the worker waited.
    assert executed == [1]

    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["steps_completed"] == 2
    mcp_result = result["results"][0]
    assert mcp_result["awaiting_approval"] == "approval-mcp-1"
    assert mcp_result["risk"] == "spend"
    assert mcp_result["approval"]["approved"] is True
    assert calls["status"][-1]["status"] == "completed"


async def test_gated_mcp_step_rejection_aborts_routine(env):
    """A rejected MCP approval stops the routine cleanly at that step."""
    calls = _new_calls()
    sequence: list[str] = []
    executed: list[int] = []

    activities = _mocks(
        _gated_mcp_behaviour(executed),
        calls,
        approval_decision={
            "approval_id": "approval-mcp-1",
            "status": "rejected",
            "approved": False,
            "note": "wrong vendor",
        },
        sequence=sequence,
    )
    result = await _run(env, activities, _payload(GATED_MCP_STEPS))

    # Nothing ran after the rejection, and the tool never ran before it either.
    assert sequence == ["step:0", "approval:approval-mcp-1"]
    assert executed == []
    assert [c["index"] for c in calls["steps"]] == [0]

    assert result["status"] == "aborted"
    assert result["ok"] is False
    assert result["failed_index"] == 0
    assert "rejected" in (result["error"] or "")
    assert result["steps_completed"] == 0
    # Aborted, not failed: the run is reported and the workflow does not throw.
    assert calls["status"][-1]["status"] == "aborted"


async def test_gated_mcp_step_approval_timeout_aborts_routine(env):
    """No human decision inside the window is a clean abort, not an execution."""
    calls = _new_calls()
    executed: list[int] = []

    activities = _mocks(
        _gated_mcp_behaviour(executed),
        calls,
        approval_decision={
            "approval_id": "approval-mcp-1",
            "status": "timed_out",
            "approved": False,
        },
    )
    result = await _run(env, activities, _payload(GATED_MCP_STEPS))

    assert result["status"] == "aborted"
    assert "timed_out" in (result["error"] or "")
    assert executed == []


# --------------------------------------------------------------------------- #
# `run_step_activity` against the real HTTP shapes
#
# The workflow tests above trust `run_step_activity` to turn the API's gated
# answer into `awaiting_approval`. These drive the real activity over a mock
# transport so that trust is earned rather than assumed.
# --------------------------------------------------------------------------- #

import json  # noqa: E402

import httpx  # noqa: E402

from worker import activities as acts  # noqa: E402


def _stub_transport(monkeypatch, handler) -> list[httpx.Request]:
    """Point every activity HTTP call at handler; return the captured requests."""
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


def _mcp_step_payload(**step_extra: Any) -> dict[str, Any]:
    return {
        "index": 0,
        "bot_id": "bot-1",
        "routine_id": "routine-abc",
        "run_id": "run-1",
        "step": {
            "type": "mcp",
            "mcp_id": "mcp-1",
            "tool": "send_invoice",
            "arguments": {"amount": 4200},
            **step_extra,
        },
    }


async def test_run_step_mcp_gated_201_becomes_awaiting_approval(monkeypatch):
    """201 PendingApprovalOut from the MCP route parks the step."""
    seen = _stub_transport(
        monkeypatch, lambda request: httpx.Response(201, json=dict(PENDING_MCP_APPROVAL))
    )

    result = await acts.run_step_activity(_mcp_step_payload())

    assert seen[0].url.path == "/api/bots/bot-1/mcp/mcp-1/call"
    assert result["awaiting_approval"] == "approval-mcp-1"
    assert result["risk"] == "spend"
    assert result["type"] == "mcp"
    # There is no tool result to report, because no tool ran.
    assert result["result"]["status"] == "pending_approval"


async def test_run_step_mcp_ungated_call_returns_the_tool_result(monkeypatch):
    """A tool the API does not classify as risky still executes in one trip."""
    _stub_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"ok": True, "result": {"rows": 3}}),
    )

    result = await acts.run_step_activity(
        _mcp_step_payload(tool="lookup_invoice", arguments={"id": 7})
    )

    assert "awaiting_approval" not in result
    assert result["ok"] is True
    assert result["result"]["result"] == {"rows": 3}


async def test_run_step_mcp_forwards_declared_risk_for_escalation(monkeypatch):
    """A step-declared risk travels to the API, which may only escalate on it.

    The worker never classifies. It forwards the declaration so the HTTP lane
    sees exactly what the API's inline executor reads off the same step.
    """
    seen = _stub_transport(
        monkeypatch, lambda request: httpx.Response(201, json=dict(PENDING_MCP_APPROVAL))
    )

    await acts.run_step_activity(_mcp_step_payload(tool="run_report", risk="Delete"))

    body = json.loads(seen[0].content)
    assert body == {"tool": "run_report", "arguments": {"amount": 4200}, "risk": "delete"}


async def test_run_step_mcp_without_declared_risk_sends_no_risk_key(monkeypatch):
    """Absent declaration means absent key — never an invented `observe`."""
    seen = _stub_transport(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}))

    await acts.run_step_activity(_mcp_step_payload())

    assert "risk" not in json.loads(seen[0].content)


async def test_run_step_fails_closed_when_gated_without_an_approval_id(monkeypatch):
    """`pending_approval` with no id must not be reported as an executed step."""
    from temporalio.exceptions import ApplicationError

    _stub_transport(
        monkeypatch,
        lambda request: httpx.Response(201, json={"status": "pending_approval", "risk": "send"}),
    )

    with pytest.raises(ApplicationError) as excinfo:
        await acts.run_step_activity(_mcp_step_payload())

    assert excinfo.value.non_retryable is True
    assert "gated" in str(excinfo.value)


async def test_run_step_connector_sends_input_under_the_input_key(monkeypatch):
    """ExecuteActionIn reads `input`; a flat body is read as an empty input —
    and an empty input is then what a held approval shows its reviewer."""
    seen = _stub_transport(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}))

    await acts.run_step_activity(
        {
            "index": 0,
            "bot_id": "bot-1",
            "thread_id": "thread-1",
            "step": {
                "type": "connector",
                "connector_id": "microsoft_graph",
                "action": "send_mail",
                "input": {"to": "a@b.c"},
                "risk": "send",
            },
        }
    )

    body = json.loads(seen[0].content)
    assert body["input"] == {"to": "a@b.c"}
    assert body["thread_id"] == "thread-1"
    assert body["risk"] == "send"


def test_declared_risk_is_normalised_not_classified():
    """The worker owns no risk table: it normalises the declaration and stops."""
    assert acts._declared_risk({"risk": "  Send "}) == "send"
    assert acts._declared_risk({"risk": ""}) is None
    assert acts._declared_risk({}) is None
    # `send_invoice` is a `spend` to the API's classifier. The worker does not
    # guess that here — classification lives in exactly one place, server-side.
    assert acts._declared_risk({"tool": "send_invoice"}) is None


# --------------------------------------------------------------------------- #
# Gated MCP, end to end with the REAL activities
#
# Everything above either mocks the activities (workflow logic) or calls one
# activity on its own (HTTP handling). This joins the two: RoutineWorkflow with
# the real `run_step_activity` and the real `wait_for_approval_activity`, over a
# stub API that gates the MCP call. Nothing between the 201 and the resumed
# routine is simulated.
# --------------------------------------------------------------------------- #


async def test_gated_mcp_routine_end_to_end_with_real_activities(env, monkeypatch):
    """201 from the MCP route -> park -> approval -> the routine finishes."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append(f"{request.method} {path}")
        if path.endswith("/mcp/mcp-1/call"):
            # The gate: the tool did not run, an approval exists instead.
            return httpx.Response(201, json=dict(PENDING_MCP_APPROVAL))
        if path == "/api/approvals/approval-mcp-1":
            return httpx.Response(
                200,
                json={"id": "approval-mcp-1", "status": "approved", "note": "ok to pay"},
            )
        if path.endswith("/connectors/crm/actions/log_activity"):
            return httpx.Response(200, json={"ok": True, "result": {"logged": True}})
        if path.endswith("/status"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"unrouted {path}"})

    _stub_transport(monkeypatch, handler)

    result = await _run(
        env,
        list(acts.ALL_ACTIVITIES),
        _payload(GATED_MCP_STEPS, user_id="user-42"),
    )

    # The MCP tool was called once, held, polled, and only then did step 1 run.
    assert requests == [
        "POST /api/bots/bot-1/mcp/mcp-1/call",
        "GET /api/approvals/approval-mcp-1",
        "POST /api/bots/bot-1/connectors/crm/actions/log_activity",
        "POST /api/runs/run-1/status",
    ]

    assert result["status"] == "completed"
    assert result["steps_completed"] == 2
    gated = result["results"][0]
    assert gated["awaiting_approval"] == "approval-mcp-1"
    assert gated["risk"] == "spend"
    assert gated["approval"] == {
        "approval_id": "approval-mcp-1",
        "status": "approved",
        "approved": True,
        "note": "ok to pay",
        "execution": None,
    }


async def test_gated_mcp_routine_end_to_end_rejection_aborts(env, monkeypatch):
    """A rejected approval stops the routine before the next step's request."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append(f"{request.method} {path}")
        if path.endswith("/mcp/mcp-1/call"):
            return httpx.Response(201, json=dict(PENDING_MCP_APPROVAL))
        if path == "/api/approvals/approval-mcp-1":
            return httpx.Response(
                200,
                json={"id": "approval-mcp-1", "status": "rejected", "note": "not this vendor"},
            )
        if path.endswith("/status"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"unrouted {path}"})

    _stub_transport(monkeypatch, handler)

    result = await _run(env, list(acts.ALL_ACTIVITIES), _payload(GATED_MCP_STEPS))

    # No connector request was ever made: the routine stopped at the gate.
    assert requests == [
        "POST /api/bots/bot-1/mcp/mcp-1/call",
        "GET /api/approvals/approval-mcp-1",
        "POST /api/runs/run-1/status",
    ]
    assert result["status"] == "aborted"
    assert result["failed_index"] == 0
    assert "rejected" in (result["error"] or "")
    assert result["steps_completed"] == 0
