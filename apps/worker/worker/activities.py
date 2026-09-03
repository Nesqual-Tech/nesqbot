"""Temporal activities — every side effect the worker performs lives here.

All activities are idempotent, heartbeat while they poll, and classify API
failures so the workflow retry policies can do the right thing:

* 4xx (except 408/429) -> `ApplicationError(type="api_client_error", non_retryable=True)`
* 5xx / transport errors -> retryable

Nothing in this module may be imported from workflow code without going through
`workflow.unsafe.imports_passed_through()`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from worker.config import Settings, get_settings, kv

NON_RETRYABLE_ERROR_TYPE = "api_client_error"
RETRYABLE_ERROR_TYPE = "api_server_error"
UNKNOWN_OUTCOME_ERROR_TYPE = "api_unknown_outcome"
"""A step request that was delivered but whose answer never arrived.

Non-retryable on purpose — see `_unknown_outcome`. A routine that stops and
asks a human beats one that sends the invoice three times.
"""

# Statuses that mean "keep polling". Read by `RoutineWorkflow` too, which owns
# the long wait now, so the two lanes agree on what "not decided yet" looks like.
PENDING_APPROVAL_STATUSES = frozenset({"pending", "queued", ""})


@dataclass(frozen=True)
class StepRequest:
    """Normalised view of one routine step (mirrors the API's routine step JSON)."""

    index: int
    bot_id: str
    step: dict[str, Any]
    routine_id: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    user_id: str | None = None
    """The initiating human, when one exists. None for unattended cron runs."""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> StepRequest:
        return cls(
            index=int(data.get("index", 0)),
            bot_id=str(data["bot_id"]),
            step=dict(data.get("step") or {}),
            routine_id=_opt_str(data.get("routine_id")),
            run_id=_opt_str(data.get("run_id")),
            thread_id=_opt_str(data.get("thread_id")),
            user_id=_opt_str(data.get("user_id")),
        )


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #


def _idempotency_key(suffix: str = "") -> str:
    """Stable across retries of the same activity task, unique per attempt-set.

    Temporal keeps `activity_id` constant while an activity is retried, so this
    key lets the API de-duplicate a replayed side effect.
    """
    try:
        info = activity.info()
        base = f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}"
    except RuntimeError:  # outside an activity context (tests, direct calls)
        base = f"local:{time.time_ns()}"
    return f"{base}:{suffix}" if suffix else base


def _client(settings: Settings, *, timeout: float | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.api_base,
        headers=settings.api_headers(),
        timeout=httpx.Timeout(
            timeout if timeout is not None else settings.worker_http_timeout_seconds,
            connect=settings.worker_http_connect_timeout_seconds,
        ),
    )


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - body may be empty/html
        return resp.text[:400]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("code") or body)[:400]
    return str(body)[:400]


def _check(resp: httpx.Response, *, op: str) -> None:
    if resp.is_success:
        return
    code = resp.status_code
    message = f"{op} failed: HTTP {code}: {_detail(resp)}"
    if 400 <= code < 500 and code not in (408, 429):
        raise ApplicationError(message, type=NON_RETRYABLE_ERROR_TYPE, non_retryable=True)
    raise ApplicationError(message, type=RETRYABLE_ERROR_TYPE)


# --------------------------------------------------------------------------- #
# Delivered-vs-never-delivered, for steps that are not replayable
#
# `_idempotency_key` above claims the API can "de-duplicate a replayed side
# effect". Measured against this tree, exactly one endpoint reads that header:
# `POST /threads/{id}/messages` (`apps/api/app/routers/threads.py:270`, which
# names the worker as the reason it exists). Grepping `Idempotency-Key` across
# `apps/api/app` finds nothing else — not `POST /bots/{id}/desktop/action`, not
# `POST /bots/{id}/connectors/{c}/actions/{a}`, not `/mcp/{id}/call`, not
# `/approvals`. So for every *routine step* the key is decorative, and a retry
# of a delivered step is a second real side effect.
#
# httpx transport failures used to propagate raw out of `run_step_activity`,
# which Temporal classifies as retryable, and STANDARD_RETRY then grants three
# attempts. An `httpx.ReadTimeout` on a `send_mail` connector step means the
# request WAS delivered and the response was lost — so the old behaviour sent
# it twice more. The distinction below is the whole fix: connect-phase failures
# never reached the app and stay retryable; anything after the connection was
# established has an unknown outcome and stops the routine instead.
# --------------------------------------------------------------------------- #

NEVER_DELIVERED_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)
"""Failures that prove no request handler ran: no connection was ever made.

An API that is down, restarting, or not up yet in compose fails here, and those
must keep retrying or a routine cannot survive an ordinary API deploy.
"""

UNKNOWN_OUTCOME_STATUS_CODES = frozenset({408, 504})
"""Statuses where a timer between us and the handler fired, not the handler.

408 is the server abandoning the request, 504 a gateway abandoning its upstream.
Either way the app may have executed the side effect and lost the answer.
`_check` maps both to retryable, which is correct for a read and wrong for a
send, so steps route them here instead.
"""


def _unknown_outcome(detail: str, *, op: str, index: int, key: str) -> ApplicationError:
    """Stop the routine and say why, rather than repeating a possible send."""
    return ApplicationError(
        f"{op} at step {index} was delivered but its outcome is unknown ({detail}); "
        "deliberately not retried because the API honours Idempotency-Key only on "
        "POST /threads/{id}/messages, so a replay would re-run the side effect. "
        f"Check whether the action took effect (idempotency key {key}) before re-running.",
        type=UNKNOWN_OUTCOME_ERROR_TYPE,
        non_retryable=True,
    )


async def _post_step(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any] | None,
    *,
    key: str,
    op: str,
    index: int,
) -> httpx.Response:
    """POST one routine step, translating transport failures by delivery state."""
    try:
        return await client.post(path, json=body, headers={"Idempotency-Key": key})
    except NEVER_DELIVERED_TRANSPORT_ERRORS as exc:
        raise ApplicationError(
            f"{op} at step {index} never reached the API ({type(exc).__name__}: {exc}); retrying",
            type=RETRYABLE_ERROR_TYPE,
        ) from exc
    except httpx.TransportError as exc:
        # ReadTimeout / ReadError / RemoteProtocolError / Write* all happen after
        # the connection is up, i.e. after the request may have been read and
        # executed. Unknown, not failed.
        raise _unknown_outcome(f"{type(exc).__name__}: {exc}", op=op, index=index, key=key) from exc


def _check_step(resp: httpx.Response, *, op: str, index: int, key: str) -> None:
    """`_check` for a step response, with 408/504 reclassified as unknown."""
    if resp.status_code in UNKNOWN_OUTCOME_STATUS_CODES:
        raise _unknown_outcome(f"HTTP {resp.status_code}: {_detail(resp)}", op=op, index=index, key=key)
    _check(resp, op=op)


def _payload(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": resp.is_success, "raw": resp.text[:2000]}
    if isinstance(body, dict):
        return body
    return {"ok": resp.is_success, "result": body}


def _log(event: str, **fields: object) -> None:
    activity.logger.info("%s", kv(event=event, **fields))


def _warn(event: str, **fields: object) -> None:
    activity.logger.warning("%s", kv(event=event, **fields))


def _heartbeat(*details: Any) -> None:
    try:
        activity.heartbeat(*details)
    except RuntimeError:  # not inside an activity (unit tests)
        pass


#: Status the API answers with when it gated a call instead of running it
#: (`PendingApprovalOut`, HTTP 201). The body is identical on every gated path:
#: connector action, MCP tool call, Bot Desktop action.
PENDING_APPROVAL_STATUS = "pending_approval"


def _is_gated(body: dict[str, Any]) -> bool:
    """True when the API parked the call for a human instead of executing it."""
    return str(body.get("status") or "").strip().lower() == PENDING_APPROVAL_STATUS


def _approval_from(body: dict[str, Any]) -> str | None:
    """Detect the API's risk-gate response: 201 `PendingApprovalOut`.

    `{approval_id, status:"pending_approval", risk, title, detail}` — the same
    body whichever executor gated the call, which is exactly why this runs once
    for every step type instead of once per branch. An MCP tool call parks the
    routine on the approval poller for the same reason, and by the same code,
    that a connector or desktop step does.
    """
    approval_id = body.get("approval_id") or body.get("awaiting_approval")
    if approval_id:
        return str(approval_id)
    if _is_gated(body) and body.get("id"):
        return str(body["id"])
    return None


def _declared_risk(step: dict[str, Any]) -> str | None:
    """The risk a taught step declares for itself, normalised.

    Forwarded to the API so the HTTP lane sees the same declaration the API's
    inline routine executor reads straight off the step. It is **escalate-only**
    server-side: it can raise the classification the API derives from the action
    or tool name, never lower it.

    The worker deliberately classifies nothing itself. There is exactly one
    implementation — `app/services/desktop.py::classify_action_risk` — and the
    worker cannot import API code, so a second copy here is precisely the
    divergence this contract exists to prevent.
    """
    risk = step.get("risk")
    if risk is None:
        return None
    text = str(risk).strip().lower()
    return text or None


def _add_declared_risk(body: dict[str, Any], step: dict[str, Any]) -> None:
    """Attach the step's declared risk to an outgoing request body, if any."""
    risk = _declared_risk(step)
    if risk:
        body["risk"] = risk


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #


@activity.defn
async def post_message_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """POST a turn into a thread. Idempotency key survives activity retries."""
    settings = get_settings()
    thread_id = str(payload["thread_id"])
    body: dict[str, Any] = {"content": payload.get("message") or payload.get("content") or ""}
    mentions = payload.get("mention_bot_ids") or []
    if mentions:
        body["mention_bot_ids"] = [str(m) for m in mentions]
    key = _idempotency_key(f"message:{thread_id}")

    _log("post_message.start", thread_id=thread_id, chars=len(body["content"]))
    async with _client(settings, timeout=settings.worker_message_timeout_seconds) as client:
        resp = await client.post(
            f"/threads/{thread_id}/messages",
            json=body,
            headers={"Idempotency-Key": key},
        )
    _check(resp, op="post_message")
    result = _payload(resp)
    _log(
        "post_message.done",
        thread_id=thread_id,
        bot_id=result.get("bot_id"),
        run_id=result.get("run_id"),
        approval_id=result.get("approval_id"),
    )
    return result


#: What `bot_desktop_mode=aks` writes as the container handle. The API's
#: `services/desktop.py` aks branch sets `state="starting"` and
#: `container_id = f"aks-pending-{bot.id}"` on the assumption that "the worker
#: creates the pod". No such code exists here — grep the module for `pod` or
#: `kubernetes` — so that state never advances. Recognising the sentinel is the
#: honest answer: fail in two seconds naming the cause, instead of polling for
#: `worker_desktop_ready_timeout_seconds` (300s) and then retrying that three
#: times under STANDARD_RETRY, which is fifteen minutes of held activity slot
#: reported as "not running after 300s (state=starting)" — a message that sends
#: an operator hunting a slow container image.
AKS_PENDING_PREFIX = "aks-pending-"


def _refuse_unreconciled_aks(bot_id: str, state: dict[str, Any]) -> None:
    """Raise non-retryably when the desktop is an aks placeholder nobody drives."""
    container_id = str(state.get("container_id") or "")
    if not container_id.startswith(AKS_PENDING_PREFIX):
        return
    raise ApplicationError(
        f"desktop for bot {bot_id} is a bot_desktop_mode=aks placeholder "
        f"(container_id={container_id}) and no reconciler exists: the worker has no "
        "aks/pod/kubernetes code, so this desktop can never leave state=starting. "
        "Waiting is pointless. Set bot_desktop_mode=k8s, which drives any cluster "
        "from the API and is already shipped, or attach the desktop manually.",
        type=NON_RETRYABLE_ERROR_TYPE,
        non_retryable=True,
    )


@activity.defn
async def ensure_desktop_activity(payload: dict[str, Any] | str) -> dict[str, Any]:
    """Start the bot desktop and poll `GET /bots/{id}/desktop` until it runs.

    Idempotent: starting an already-running desktop is a no-op on the API side.
    Heartbeats on every poll so a stalled desktop is detected by Temporal.
    """
    settings = get_settings()
    if isinstance(payload, str):
        payload = {"bot_id": payload}
    bot_id = str(payload["bot_id"])
    timeout_s = float(payload.get("timeout_seconds") or settings.worker_desktop_ready_timeout_seconds)
    interval = float(payload.get("poll_interval_seconds") or settings.worker_desktop_poll_interval_seconds)
    deadline = time.monotonic() + timeout_s
    key = _idempotency_key(f"desktop:{bot_id}")

    _log("ensure_desktop.start", bot_id=bot_id, timeout_s=timeout_s)
    async with _client(settings) as client:
        resp = await client.post(
            f"/bots/{bot_id}/desktop/start",
            headers={"Idempotency-Key": key},
        )
        _check(resp, op="desktop_start")
        state = _payload(resp)

        polls = 0
        while True:
            # Checked on the start answer and on every probe: a desktop that
            # regresses to the placeholder is the same dead end as one that
            # started there.
            _refuse_unreconciled_aks(bot_id, state)
            current = str(state.get("state") or "unknown")
            if current == "running":
                _log("ensure_desktop.ready", bot_id=bot_id, polls=polls)
                return state
            if current == "error":
                raise ApplicationError(
                    f"desktop for bot {bot_id} entered error state: {state.get('last_error')}",
                    type=RETRYABLE_ERROR_TYPE,
                )
            if time.monotonic() >= deadline:
                raise ApplicationError(
                    f"desktop for bot {bot_id} not running after {timeout_s:.0f}s (state={current})",
                    type=RETRYABLE_ERROR_TYPE,
                )
            polls += 1
            _heartbeat({"bot_id": bot_id, "state": current, "polls": polls})
            await asyncio.sleep(interval)
            probe = await client.get(f"/bots/{bot_id}/desktop")
            _check(probe, op="desktop_get")
            state = _payload(probe)


@activity.defn
async def run_step_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute exactly ONE routine step and return its result.

    Returned shape:
        {"ok": bool, "index": int, "type": str, "result": {...}}
        {"awaiting_approval": "<approval_id>", "index": int, "type": str, ...}

    Risk gating is the API's job on every step type — desktop, connector and
    MCP alike. This activity only recognises the gated answer (201
    `PendingApprovalOut`) and hands the approval id back so `RoutineWorkflow`
    parks on `wait_for_approval_activity`. It never decides for itself whether
    something is safe to run, and it fails closed if the API says "gated"
    without naming an approval.
    """
    settings = get_settings()
    req = StepRequest.from_mapping(payload)
    step_type = str(req.step.get("type") or "desktop").lower()
    key = _idempotency_key(f"step:{req.routine_id or '-'}:{req.index}")
    op = f"run_step[{step_type}]"
    _log("run_step.start", index=req.index, type=step_type, bot_id=req.bot_id, routine_id=req.routine_id)

    async with _client(settings, timeout=settings.worker_step_timeout_seconds) as client:
        if step_type == "desktop":
            body = {"action": req.step.get("action", "click"), **(req.step.get("args") or {})}
            _add_declared_risk(body, req.step)
            resp = await _post_step(
                client,
                f"/bots/{req.bot_id}/desktop/action",
                body,
                key=key,
                op=op,
                index=req.index,
            )
        elif step_type == "connector":
            connector_id = req.step.get("connector_id") or req.step.get("connector")
            action = req.step.get("action")
            if not connector_id or not action:
                raise ApplicationError(
                    f"connector step {req.index} needs connector_id and action",
                    type=NON_RETRYABLE_ERROR_TYPE,
                    non_retryable=True,
                )
            # `ExecuteActionIn` carries the arguments under `input`. A flat body
            # is read as an *empty* input — and an empty input is then what the
            # held approval records for a human to review.
            body = {"input": dict(req.step.get("input") or req.step.get("args") or {})}
            if req.step.get("title"):
                body["title"] = str(req.step["title"])
            if req.thread_id:
                body["thread_id"] = req.thread_id
            _add_declared_risk(body, req.step)
            resp = await _post_step(
                client,
                f"/bots/{req.bot_id}/connectors/{connector_id}/actions/{action}",
                body,
                key=key,
                op=op,
                index=req.index,
            )
        elif step_type == "mcp":
            mcp_id = req.step.get("mcp_id") or req.step.get("mcp")
            tool = req.step.get("tool") or req.step.get("action")
            if not mcp_id or not tool:
                raise ApplicationError(
                    f"mcp step {req.index} needs mcp_id and tool",
                    type=NON_RETRYABLE_ERROR_TYPE,
                    non_retryable=True,
                )
            # The API classifies MCP tool risk from the tool *name* and gates on
            # it, exactly as it does for a desktop action, so `send_invoice` over
            # MCP is held for the same reason it is held over a connector. A
            # gated call answers 201 PendingApprovalOut instead of the tool
            # result; that is picked up below, in the one place that picks it up
            # for every step type.
            body = {
                "tool": tool,
                "arguments": dict(req.step.get("arguments") or req.step.get("args") or {}),
            }
            _add_declared_risk(body, req.step)
            resp = await _post_step(
                client,
                f"/bots/{req.bot_id}/mcp/{mcp_id}/call",
                body,
                key=key,
                op=op,
                index=req.index,
            )
        elif step_type == "approval":
            resp = await _post_step(
                client,
                "/approvals",
                _approval_request(req),
                key=key,
                op=op,
                index=req.index,
            )
            if resp.status_code in (404, 405):
                raise ApplicationError(
                    "approval steps need POST /api/approvals on the API "
                    f"(got HTTP {resp.status_code})",
                    type=NON_RETRYABLE_ERROR_TYPE,
                    non_retryable=True,
                )
        else:
            raise ApplicationError(
                f"unknown routine step type {step_type!r} at index {req.index}",
                type=NON_RETRYABLE_ERROR_TYPE,
                non_retryable=True,
            )

    _check_step(resp, op=op, index=req.index, key=key)
    body_out = _payload(resp)
    approval_id = _approval_from(body_out)
    if approval_id:
        _log(
            "run_step.awaiting_approval",
            index=req.index,
            type=step_type,
            approval_id=approval_id,
            risk=body_out.get("risk"),
        )
        return {
            "ok": True,
            "index": req.index,
            "type": step_type,
            "awaiting_approval": approval_id,
            "risk": body_out.get("risk"),
            "result": body_out,
        }
    if _is_gated(body_out):
        # Gated but unidentifiable. Fail closed: reporting success here would
        # mark a step complete that never ran, against an approval nobody can
        # poll, and the routine would sail past the gate.
        raise ApplicationError(
            f"step {req.index} ({step_type}) was gated but the API returned no "
            "approval id; refusing to treat it as executed",
            type=NON_RETRYABLE_ERROR_TYPE,
            non_retryable=True,
        )

    ok = bool(body_out.get("ok", True))
    _log("run_step.done", index=req.index, type=step_type, ok=ok)
    return {"ok": ok, "index": req.index, "type": step_type, "result": body_out}


def _approval_request(req: StepRequest) -> dict[str, Any]:
    """Build the held-payload shape documented in docs/API.md."""
    step = req.step
    payload = dict(step.get("payload") or {})
    payload.setdefault("kind", step.get("kind", "message_only"))
    for field in ("connector_id", "action", "input", "draft"):
        if step.get(field) is not None:
            payload.setdefault(field, step[field])
    if req.thread_id:
        payload.setdefault("thread_id", req.thread_id)
    if req.user_id:
        # Requester scoping: the API resolves the approval owner from this key
        # first. Without it a routine approval on a shared system bot has no
        # knowable human and falls back to bot visibility.
        payload.setdefault("requested_by", req.user_id)
    else:
        _warn(
            "run_step.approval.no_requester",
            index=req.index,
            routine_id=req.routine_id,
            bot_id=req.bot_id,
        )
    body: dict[str, Any] = {
        "bot_id": req.bot_id,
        "risk": step.get("risk", "send"),
        "title": step.get("title") or f"Approve routine step {req.index}",
        "summary": step.get("summary") or step.get("description") or "",
        "payload": payload,
    }
    if req.run_id:
        body["run_id"] = req.run_id
    return body


APPROVAL_POLL_BUDGET_SECONDS = 60.0
"""How long ONE `wait_for_approval_activity` attempt may poll.

The wait for a human used to live entirely inside this activity, with its own
deadline (`worker_approval_timeout_seconds`, 86400s) equal to the workflow's
`start_to_close` for the same activity (`APPROVAL_START_TO_CLOSE`, 24h), which
made "which timer wins" a coin flip — and when Temporal won, the timeout was
retryable, so an approval nobody answered became a run alive for up to 72
hours instead of a clean abort at 24. Worse, every parked approval held one of
`worker_max_concurrent_activities` (20) slots for that whole span, so twenty
unanswered approvals left a worker that could execute nothing while
`_health_loop` kept the container HEALTHCHECK green.

Now one attempt is bounded to a minute, "not decided yet" is a plain `pending`
return, and `RoutineWorkflow` holds the 24-hour deadline on a durable timer
that costs no worker slot. `RoutineWorkflow` passes this same number down
explicitly so an env override can never make an attempt outlive the
`start_to_close` derived from it.
"""


@activity.defn
async def wait_for_approval_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Poll `GET /approvals/{id}` for one bounded budget, then hand back control.

    Returns `{"approval_id", "status", "approved": bool, "note"}`. Neither
    outcome throws, so the workflow can abort cleanly:

    * decided/expired -> that status, `approved` set accordingly;
    * budget spent with no decision -> `status="pending"`, for the workflow to
      sleep on and ask again;
    * `timeout_seconds` given and smaller than the budget and fully spent ->
      `status="timed_out"`, preserving the old contract for a caller whose whole
      deadline fits inside a single attempt (the inline/direct-call path).
    """
    settings = get_settings()
    approval_id = str(payload["approval_id"])
    interval = float(payload.get("poll_interval_seconds") or settings.worker_approval_poll_interval_seconds)
    timeout_s = float(
        payload.get("poll_budget_seconds") or settings.worker_approval_poll_budget_seconds
    )
    exhausted_status = "pending"
    overall = payload.get("timeout_seconds")
    if overall:
        overall = float(overall)
        if overall < timeout_s:
            timeout_s = overall
            exhausted_status = "timed_out"
    deadline = time.monotonic() + timeout_s

    _log("wait_approval.start", approval_id=approval_id, timeout_s=timeout_s)
    polls = 0
    async with _client(settings) as client:
        while True:
            resp = await client.get(f"/approvals/{approval_id}")
            if resp.status_code == 404:
                raise ApplicationError(
                    f"approval {approval_id} not found",
                    type=NON_RETRYABLE_ERROR_TYPE,
                    non_retryable=True,
                )
            _check(resp, op="get_approval")
            body = _payload(resp)
            status = str(body.get("status") or "").lower()
            if status not in PENDING_APPROVAL_STATUSES:
                approved = status == "approved"
                _log("wait_approval.decided", approval_id=approval_id, status=status, polls=polls)
                return {
                    "approval_id": approval_id,
                    "status": status,
                    "approved": approved,
                    "note": body.get("note"),
                    "execution": body.get("execution"),
                }
            if time.monotonic() >= deadline:
                _log(
                    "wait_approval.budget_spent",
                    approval_id=approval_id,
                    polls=polls,
                    status=exhausted_status,
                )
                return {
                    "approval_id": approval_id,
                    "status": exhausted_status,
                    "approved": False,
                    "note": f"no decision within {timeout_s:.0f}s",
                }
            polls += 1
            _heartbeat({"approval_id": approval_id, "polls": polls, "status": status})
            await asyncio.sleep(interval)


@activity.defn
async def record_run_status_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Push run status/error back to the API so failures are visible in the UI.

    Best-effort by design: this activity never raises, because a bookkeeping
    failure must not take down the run it is reporting on. Transport errors are
    logged and reported as `{"ok": false}` so Temporal can retry cheaply.
    """
    settings = get_settings()
    run_id = _opt_str(payload.get("run_id"))
    body = {
        "status": payload.get("status", "unknown"),
        "error": payload.get("error"),
        "detail": payload.get("detail") or {},
        "routine_id": _opt_str(payload.get("routine_id")),
        "thread_id": _opt_str(payload.get("thread_id")),
        "bot_id": _opt_str(payload.get("bot_id")),
        "workflow_id": _opt_str(payload.get("workflow_id")),
    }
    if not run_id:
        _log("record_run_status.skipped", reason="no_run_id", status=body["status"])
        return {"ok": False, "skipped": True}

    key = _idempotency_key(f"run_status:{run_id}:{body['status']}")
    try:
        async with _client(settings, timeout=30.0) as client:
            resp = await client.post(
                f"/runs/{run_id}/status",
                json=body,
                headers={"Idempotency-Key": key},
            )
    except Exception as exc:  # noqa: BLE001 - never fail the caller
        _warn("record_run_status.unreachable", run_id=run_id, error=type(exc).__name__)
        return {"ok": False, "error": str(exc)[:200]}

    if resp.status_code in (404, 405):
        # API lane has not shipped the write endpoint yet — degrade to a log line.
        _warn("record_run_status.unsupported", run_id=run_id, status=body["status"], http=resp.status_code)
        return {"ok": False, "unsupported": True}
    if not resp.is_success:
        _warn("record_run_status.rejected", run_id=run_id, http=resp.status_code, detail=_detail(resp))
        return {"ok": False, "http": resp.status_code}

    _log("record_run_status.done", run_id=run_id, status=body["status"])
    return {"ok": True}


ALL_ACTIVITIES = [
    post_message_activity,
    ensure_desktop_activity,
    run_step_activity,
    wait_for_approval_activity,
    record_run_status_activity,
]
