"""Temporal client — schedules and ad-hoc runs, degrading to no-ops when down."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.config import get_settings
from app.models import Routine

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "RoutineWorkflow"


def schedule_id_for(routine_id: uuid.UUID | str) -> str:
    return f"routine-{routine_id}"


def routine_argument(routine: Routine, *, user_id: str | None = None) -> dict[str, Any]:
    """Single dict argument for RoutineWorkflow.

    The worker's `RoutineInput.from_mapping` ignores keys it does not know, so
    name/version ride along for logging.

    `user_id` is the human who triggered *this* run. It wins over the routine's
    owner, because running a colleague's routine should file approvals against
    the person who pressed the button, not the person who wrote it. Absent a
    trigger (a cron-fired schedule), the owner is the right answer.
    """
    argument: dict[str, Any] = {
        "routine_id": str(routine.id),
        "bot_id": str(routine.bot_id),
        "steps": list(routine.steps or []),
        "name": routine.name,
        "version": routine.version,
    }

    # Requester scoping: the worker reads this off the start payload and stamps
    # `requested_by` on any approval the routine raises. Without it a routine on
    # a shared system bot has no knowable human.
    requester = (user_id or "").strip() or None
    owner_id = getattr(routine, "owner_user_id", None)
    if requester is None and owner_id:
        requester = str(owner_id)

    if requester:
        argument["user_id"] = requester
    else:
        logger.info(
            "routine %s ran with no triggering user and no owner_user_id — "
            "its approvals will be unattributed",
            routine.id,
        )
    return argument


async def get_client() -> Any | None:
    """Connect to Temporal, or return None when it is unreachable.

    Import and connection both happen here so the module is safe to import on a
    box with no Temporal (and no temporalio wheel warm in the cache).
    """
    settings = get_settings()
    try:
        import asyncio

        from temporalio.client import Client

        return await asyncio.wait_for(
            Client.connect(settings.temporal_host, namespace=settings.temporal_namespace),
            timeout=settings.temporal_connect_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - includes RuntimeError/OSError/TimeoutError/RPCError
        logger.info("temporal unavailable at %s (%s)", settings.temporal_host, exc)
        return None


async def sync_routine_schedule(routine: Routine) -> str | None:
    """Create or replace the cron schedule for a routine. Returns the schedule id.

    A disabled routine keeps its schedule — paused, not deleted — so re-enabling
    it does not lose the cron. Only dropping the cron removes the schedule.
    """
    if not routine.schedule_cron:
        await delete_routine_schedule(routine.id)
        return None

    client = await get_client()
    if client is None:
        return None

    settings = get_settings()
    sid = schedule_id_for(routine.id)
    try:
        from temporalio.client import (
            Schedule,
            ScheduleActionStartWorkflow,
            ScheduleOverlapPolicy,
            SchedulePolicy,
            ScheduleSpec,
            ScheduleState,
        )

        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                WORKFLOW_NAME,
                routine_argument(routine),
                id=f"routine-{routine.id}-scheduled",
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(cron_expressions=[routine.schedule_cron]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            state=ScheduleState(paused=not routine.enabled),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not build schedule for routine %s: %s", routine.id, exc)
        return None

    try:
        await client.create_schedule(sid, schedule)
        return sid
    except Exception as exc:  # noqa: BLE001 - most often "already exists"
        logger.info("schedule %s exists or create failed (%s) — recreating", sid, exc)

    try:
        handle = client.get_schedule_handle(sid)
        await handle.delete()
    except Exception:  # noqa: BLE001
        pass
    try:
        await client.create_schedule(sid, schedule)
        return sid
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule sync failed for routine %s: %s", routine.id, exc)
        return None


async def delete_routine_schedule(routine_id: uuid.UUID | str) -> None:
    """Remove a routine's schedule. Silent when Temporal or the schedule is gone."""
    client = await get_client()
    if client is None:
        return
    try:
        handle = client.get_schedule_handle(schedule_id_for(routine_id))
        await handle.delete()
    except Exception as exc:  # noqa: BLE001
        logger.info("schedule delete for %s skipped (%s)", routine_id, exc)


async def start_routine_now(routine: Routine, user_id: str | None = None) -> dict:
    """Kick off RoutineWorkflow immediately.

    `user_id` is the human who triggered this run; it takes precedence over the
    routine's owner for approval attribution. Omit it for unattended starts.

    Returns `{workflow_id, run_id}` on success and an empty dict when Temporal
    is unreachable — callers treat the empty dict as "run it inline instead".
    """
    client = await get_client()
    if client is None:
        return {}

    settings = get_settings()
    workflow_id = f"routine-{routine.id}-{uuid.uuid4().hex[:8]}"
    try:
        handle = await client.start_workflow(
            WORKFLOW_NAME,
            routine_argument(routine, user_id=user_id),
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("routine start failed for %s: %s", routine.id, exc)
        return {}

    run_id = getattr(handle, "first_execution_run_id", None) or getattr(handle, "result_run_id", None)
    return {"workflow_id": handle.id, "run_id": run_id}
