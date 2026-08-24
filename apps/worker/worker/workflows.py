"""Temporal workflows — deterministic orchestration only, no I/O.

Every side effect goes through an activity in `worker.activities`. Time comes
from `workflow.now()`, logging from `workflow.logger`; there is no `datetime.now`,
no `random`, and no direct HTTP anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from worker.activities import (
        NON_RETRYABLE_ERROR_TYPE,
        ensure_desktop_activity,
        post_message_activity,
        record_run_status_activity,
        run_step_activity,
        wait_for_approval_activity,
    )

# --------------------------------------------------------------------------- #
# Shared policies / timeouts
# --------------------------------------------------------------------------- #

STANDARD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
    non_retryable_error_types=[NON_RETRYABLE_ERROR_TYPE],
)
"""Max 3 attempts; 4xx from the API is never retried."""

BOOKKEEPING_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=5,
    non_retryable_error_types=[NON_RETRYABLE_ERROR_TYPE],
)

DESKTOP_START_TO_CLOSE = timedelta(minutes=10)
DESKTOP_HEARTBEAT = timedelta(seconds=30)
MESSAGE_START_TO_CLOSE = timedelta(minutes=5)
STEP_START_TO_CLOSE = timedelta(minutes=15)
APPROVAL_START_TO_CLOSE = timedelta(hours=24)
APPROVAL_HEARTBEAT = timedelta(minutes=2)
STATUS_START_TO_CLOSE = timedelta(seconds=30)


# --------------------------------------------------------------------------- #
# Workflow arguments
# --------------------------------------------------------------------------- #


@dataclass
class AgentTurnInput:
    thread_id: str
    bot_id: str
    message: str = ""
    run_id: str | None = None
    mention_bot_ids: list[str] = field(default_factory=list)
    ensure_desktop: bool = True

    @classmethod
    def from_mapping(cls, data: Any) -> AgentTurnInput:
        if isinstance(data, AgentTurnInput):
            return data
        data = dict(data or {})
        return cls(
            thread_id=str(data.get("thread_id", "")),
            bot_id=str(data.get("bot_id", "")),
            message=str(data.get("message") or data.get("content") or ""),
            run_id=_opt_str(data.get("run_id")),
            mention_bot_ids=[str(m) for m in (data.get("mention_bot_ids") or [])],
            ensure_desktop=bool(data.get("ensure_desktop", True)),
        )


@dataclass
class RoutineInput:
    """Shape produced by the API's `services/temporal_client.py` and by schedules:
    `{"routine_id": …, "bot_id": …, "steps": [...]}` (extra keys tolerated)."""

    routine_id: str
    bot_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    run_id: str | None = None
    thread_id: str | None = None
    user_id: str | None = None
    """Initiating human, when there is one. None for unattended cron runs."""
    name: str = ""
    version: int = 1
    ensure_desktop: bool | None = None
    approval_timeout_seconds: float | None = None

    @classmethod
    def from_mapping(cls, data: Any) -> RoutineInput:
        if isinstance(data, RoutineInput):
            return data
        data = dict(data or {})
        raw_steps = data.get("steps") or []
        steps = [dict(s) for s in raw_steps if isinstance(s, dict)]
        ensure = data.get("ensure_desktop")
        return cls(
            routine_id=str(data.get("routine_id", "")),
            bot_id=str(data.get("bot_id", "")),
            steps=steps,
            run_id=_opt_str(data.get("run_id")),
            thread_id=_opt_str(data.get("thread_id")),
            user_id=_opt_str(
                data.get("user_id")
                or data.get("requested_by")
                or data.get("owner_user_id")
            ),
            name=str(data.get("name") or ""),
            version=int(data.get("version") or 1),
            ensure_desktop=None if ensure is None else bool(ensure),
            approval_timeout_seconds=(
                float(data["approval_timeout_seconds"])
                if data.get("approval_timeout_seconds") is not None
                else None
            ),
        )

    def needs_desktop(self) -> bool:
        if self.ensure_desktop is not None:
            return self.ensure_desktop
        return any(str(s.get("type", "desktop")).lower() == "desktop" for s in self.steps)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _root_message(err: BaseException) -> str:
    """Unwrap ActivityError/ChildWorkflowError chains down to the real message."""
    seen = 0
    current: BaseException | None = err
    last = err
    while current is not None and seen < 10:
        last = current
        if isinstance(current, ApplicationError):
            return f"{current.type or 'error'}: {current.message}"
        current = current.__cause__
        seen += 1
    return f"{type(last).__name__}: {last}"


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #


@workflow.defn
class AgentTurnWorkflow:
    """One durable chat turn: desktop up, message posted, status recorded."""

    @workflow.run
    async def run(self, data: dict[str, Any]) -> dict[str, Any]:
        params = AgentTurnInput.from_mapping(data)
        started_at = workflow.now().isoformat()
        workflow.logger.info(
            "agent_turn.start thread_id=%s bot_id=%s", params.thread_id, params.bot_id
        )

        try:
            if params.ensure_desktop and params.bot_id:
                await workflow.execute_activity(
                    ensure_desktop_activity,
                    {"bot_id": params.bot_id},
                    start_to_close_timeout=DESKTOP_START_TO_CLOSE,
                    heartbeat_timeout=DESKTOP_HEARTBEAT,
                    retry_policy=STANDARD_RETRY,
                )

            result = await workflow.execute_activity(
                post_message_activity,
                {
                    "thread_id": params.thread_id,
                    "bot_id": params.bot_id,
                    "message": params.message,
                    "mention_bot_ids": params.mention_bot_ids,
                },
                start_to_close_timeout=MESSAGE_START_TO_CLOSE,
                retry_policy=STANDARD_RETRY,
            )
        except (ActivityError, ApplicationError) as err:
            message = _root_message(err)
            workflow.logger.error("agent_turn.failed thread_id=%s error=%s", params.thread_id, message)
            await self._record(params, status="failed", error=message, started_at=started_at)
            raise ApplicationError(
                f"agent turn failed: {message}", type="agent_turn_failed"
            ) from err

        run_id = _opt_str(result.get("run_id")) or params.run_id
        status = "awaiting_approval" if result.get("approval_id") else "completed"
        await self._record(
            params,
            status=status,
            error=None,
            started_at=started_at,
            run_id=run_id,
            detail={"approval_id": result.get("approval_id"), "tier": result.get("tier")},
        )
        workflow.logger.info("agent_turn.done thread_id=%s status=%s", params.thread_id, status)
        return {"ok": True, "status": status, "started_at": started_at, **result}

    async def _record(
        self,
        params: AgentTurnInput,
        *,
        status: str,
        error: str | None,
        started_at: str,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await workflow.execute_activity(
            record_run_status_activity,
            {
                "run_id": run_id or params.run_id,
                "thread_id": params.thread_id,
                "bot_id": params.bot_id,
                "status": status,
                "error": error,
                "workflow_id": workflow.info().workflow_id,
                "detail": {
                    "started_at": started_at,
                    "finished_at": workflow.now().isoformat(),
                    **(detail or {}),
                },
            },
            start_to_close_timeout=STATUS_START_TO_CLOSE,
            retry_policy=BOOKKEEPING_RETRY,
        )


@workflow.defn
class RoutineWorkflow:
    """Run a taught routine step by step.

    Each step is its own activity, so a worker restart resumes at the next
    unfinished step instead of replaying side effects. When a step comes back
    `awaiting_approval` the workflow blocks on the approval poller and aborts
    cleanly on rejection, timeout, or expiry.
    """

    @workflow.run
    async def run(self, data: dict[str, Any]) -> dict[str, Any]:
        params = RoutineInput.from_mapping(data)
        started_at = workflow.now().isoformat()
        info = workflow.info()
        workflow.logger.info(
            "routine.start routine_id=%s bot_id=%s steps=%d",
            params.routine_id,
            params.bot_id,
            len(params.steps),
        )

        results: list[dict[str, Any]] = []
        status = "completed"
        error: str | None = None
        failed_index: int | None = None

        try:
            if params.needs_desktop() and params.bot_id:
                await workflow.execute_activity(
                    ensure_desktop_activity,
                    {"bot_id": params.bot_id},
                    start_to_close_timeout=DESKTOP_START_TO_CLOSE,
                    heartbeat_timeout=DESKTOP_HEARTBEAT,
                    retry_policy=STANDARD_RETRY,
                )

            for index, step in enumerate(params.steps):
                step_result = await workflow.execute_activity(
                    run_step_activity,
                    {
                        "index": index,
                        "bot_id": params.bot_id,
                        "routine_id": params.routine_id,
                        "run_id": params.run_id,
                        "thread_id": params.thread_id,
                        "user_id": params.user_id,
                        "step": step,
                    },
                    start_to_close_timeout=STEP_START_TO_CLOSE,
                    retry_policy=STANDARD_RETRY,
                )
                results.append(step_result)

                approval_id = step_result.get("awaiting_approval")
                if not approval_id:
                    continue

                workflow.logger.info(
                    "routine.awaiting_approval routine_id=%s index=%d approval_id=%s",
                    params.routine_id,
                    index,
                    approval_id,
                )
                decision = await workflow.execute_activity(
                    wait_for_approval_activity,
                    {
                        "approval_id": approval_id,
                        "timeout_seconds": params.approval_timeout_seconds,
                    },
                    start_to_close_timeout=APPROVAL_START_TO_CLOSE,
                    heartbeat_timeout=APPROVAL_HEARTBEAT,
                    retry_policy=STANDARD_RETRY,
                )
                step_result["approval"] = decision
                if not decision.get("approved"):
                    status = "aborted"
                    error = (
                        f"approval {approval_id} {decision.get('status', 'not approved')} "
                        f"at step {index}"
                    )
                    failed_index = index
                    workflow.logger.warning("routine.aborted routine_id=%s %s", params.routine_id, error)
                    break

        except (ActivityError, ApplicationError) as err:
            status = "failed"
            error = _root_message(err)
            failed_index = len(results)
            workflow.logger.error(
                "routine.failed routine_id=%s index=%s error=%s",
                params.routine_id,
                failed_index,
                error,
            )
            summary = self._summary(params, results, status, error, failed_index, started_at, info.workflow_id)
            await self._record(params, summary)
            raise ApplicationError(
                f"routine {params.routine_id} failed at step {failed_index}: {error}",
                type="routine_failed",
            ) from err

        summary = self._summary(params, results, status, error, failed_index, started_at, info.workflow_id)
        await self._record(params, summary)
        workflow.logger.info(
            "routine.done routine_id=%s status=%s completed=%d/%d",
            params.routine_id,
            status,
            summary["steps_completed"],
            summary["steps_total"],
        )
        return summary

    def _summary(
        self,
        params: RoutineInput,
        results: list[dict[str, Any]],
        status: str,
        error: str | None,
        failed_index: int | None,
        started_at: str,
        workflow_id: str,
    ) -> dict[str, Any]:
        completed = sum(1 for r in results if r.get("ok") and not r.get("awaiting_approval")) + sum(
            1
            for r in results
            if r.get("awaiting_approval") and (r.get("approval") or {}).get("approved")
        )
        return {
            "ok": status == "completed",
            "status": status,
            "routine_id": params.routine_id,
            "bot_id": params.bot_id,
            "run_id": params.run_id,
            "workflow_id": workflow_id,
            "version": params.version,
            "steps_total": len(params.steps),
            "steps_attempted": len(results),
            "steps_completed": completed,
            "failed_index": failed_index,
            "error": error,
            "started_at": started_at,
            "finished_at": workflow.now().isoformat(),
            "results": results,
        }

    async def _record(self, params: RoutineInput, summary: dict[str, Any]) -> None:
        await workflow.execute_activity(
            record_run_status_activity,
            {
                "run_id": params.run_id,
                "routine_id": params.routine_id,
                "thread_id": params.thread_id,
                "bot_id": params.bot_id,
                "status": summary["status"],
                "error": summary["error"],
                "workflow_id": summary["workflow_id"],
                "detail": {
                    k: summary[k]
                    for k in (
                        "steps_total",
                        "steps_attempted",
                        "steps_completed",
                        "failed_index",
                        "started_at",
                        "finished_at",
                    )
                },
            },
            start_to_close_timeout=STATUS_START_TO_CLOSE,
            retry_policy=BOOKKEEPING_RETRY,
        )


@workflow.defn
class ScheduledRoutineWorkflow:
    """Thin wrapper a Temporal Schedule can target.

    Starts `RoutineWorkflow` as a child so the schedule's own history stays tiny
    and a routine's real execution keeps its own retention/visibility.
    """

    @workflow.run
    async def run(self, data: dict[str, Any]) -> dict[str, Any]:
        params = RoutineInput.from_mapping(data)
        info = workflow.info()
        child_id = f"{info.workflow_id}-exec"
        workflow.logger.info(
            "scheduled_routine.start routine_id=%s child_id=%s", params.routine_id, child_id
        )
        try:
            return await workflow.execute_child_workflow(
                RoutineWorkflow.run,
                data,
                id=child_id,
                task_queue=info.task_queue,
            )
        except ChildWorkflowError as err:
            message = _root_message(err)
            workflow.logger.error(
                "scheduled_routine.failed routine_id=%s error=%s", params.routine_id, message
            )
            raise ApplicationError(
                f"scheduled routine {params.routine_id} failed: {message}",
                type="routine_failed",
            ) from err


ALL_WORKFLOWS = [AgentTurnWorkflow, RoutineWorkflow, ScheduledRoutineWorkflow]
