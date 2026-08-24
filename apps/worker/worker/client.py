"""Temporal client helpers — connection with backoff and schedule management.

The schedule id format (`routine-{routine_id}`), the target workflow, the task
queue and the single-dict workflow argument are a shared contract with the API's
`app/services/temporal_client.py`. Change them in both places or not at all.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Iterable

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    stop_never,
    wait_exponential,
)

from worker.config import Settings, get_settings, kv

log = logging.getLogger("worker.client")

SCHEDULE_PREFIX = "routine-"


def schedule_id(routine_id: str) -> str:
    return f"{SCHEDULE_PREFIX}{routine_id}"


def routine_payload(
    routine_id: str,
    bot_id: str,
    steps: list[dict[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    """The single dict argument every RoutineWorkflow start uses."""
    payload: dict[str, Any] = {
        "routine_id": str(routine_id),
        "bot_id": str(bot_id),
        "steps": list(steps or []),
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


async def connect(settings: Settings | None = None) -> Client:
    """Connect to Temporal, retrying with exponential backoff.

    Temporal is frequently not up yet when the worker container starts, so this
    never fails fast: with `worker_connect_max_attempts=0` (the default) it
    retries forever instead of crash-looping the container.
    """
    settings = settings or get_settings()

    def _log_retry(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        log.warning(
            "%s",
            kv(
                event="temporal.connect.retry",
                attempt=state.attempt_number,
                host=settings.temporal_host,
                error=f"{type(exc).__name__}: {exc}" if exc else None,
            ),
        )

    stop = (
        stop_after_attempt(settings.worker_connect_max_attempts)
        if settings.worker_connect_max_attempts > 0
        else stop_never
    )
    retrying = AsyncRetrying(
        stop=stop,
        wait=wait_exponential(
            multiplier=settings.worker_connect_initial_backoff_seconds,
            max=settings.worker_connect_max_backoff_seconds,
        ),
        retry=retry_if_exception_type(Exception),
        before_sleep=_log_retry,
        reraise=True,
    )

    async for attempt in retrying:
        with attempt:
            client = await Client.connect(
                settings.temporal_host,
                namespace=settings.temporal_namespace,
                identity=settings.worker_identity or None,
            )
            log.info(
                "%s",
                kv(
                    event="temporal.connected",
                    host=settings.temporal_host,
                    namespace=settings.temporal_namespace,
                ),
            )
            return client
    raise RuntimeError("unreachable: AsyncRetrying exhausted without raising")


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #


def _build_schedule(
    *,
    settings: Settings,
    routine_id: str,
    bot_id: str,
    steps: list[dict[str, Any]],
    cron: str,
    enabled: bool,
    extra: dict[str, Any] | None = None,
) -> Schedule:
    sid = schedule_id(routine_id)
    payload = routine_payload(routine_id, bot_id, steps, **(extra or {}))
    return Schedule(
        action=ScheduleActionStartWorkflow(
            settings.worker_schedule_workflow,
            args=[payload],
            id=sid,
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(cron_expressions=[cron]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
        state=ScheduleState(
            note=f"nesq routine {routine_id}",
            paused=not enabled,
        ),
    )


async def upsert_schedule(
    client: Client,
    *,
    routine_id: str,
    bot_id: str,
    steps: list[dict[str, Any]],
    cron: str,
    enabled: bool = True,
    settings: Settings | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Create or update the Temporal Schedule for a routine. Returns its id."""
    settings = settings or get_settings()
    sid = schedule_id(routine_id)
    schedule = _build_schedule(
        settings=settings,
        routine_id=routine_id,
        bot_id=bot_id,
        steps=steps,
        cron=cron,
        enabled=enabled,
        extra=extra,
    )
    try:
        await client.create_schedule(sid, schedule)
        log.info("%s", kv(event="schedule.created", schedule_id=sid, cron=cron, enabled=enabled))
        return sid
    except ScheduleAlreadyRunningError:
        pass
    except Exception as exc:  # noqa: BLE001 - server may report ALREADY_EXISTS as RPCError
        if "already" not in str(exc).lower():
            raise

    handle = client.get_schedule_handle(sid)

    def _update(_inp: ScheduleUpdateInput) -> ScheduleUpdate:
        return ScheduleUpdate(schedule=schedule)

    await handle.update(_update)
    log.info("%s", kv(event="schedule.updated", schedule_id=sid, cron=cron, enabled=enabled))
    return sid


async def delete_schedule(client: Client, routine_id: str) -> bool:
    """Delete a routine's schedule. Missing schedules are not an error."""
    sid = schedule_id(routine_id) if not str(routine_id).startswith(SCHEDULE_PREFIX) else str(routine_id)
    try:
        await client.get_schedule_handle(sid).delete()
    except Exception as exc:  # noqa: BLE001 - NotFound comes back as RPCError
        text = str(exc).lower()
        if "not found" in text or "notfound" in text:
            log.info("%s", kv(event="schedule.delete.missing", schedule_id=sid))
            return False
        raise
    log.info("%s", kv(event="schedule.deleted", schedule_id=sid))
    return True


async def list_schedules(client: Client, *, prefix: str = SCHEDULE_PREFIX) -> list[str]:
    """List schedule ids the worker owns (`routine-*` by default)."""
    iterator: Any = client.list_schedules()
    if inspect.isawaitable(iterator):
        iterator = await iterator
    ids: list[str] = []
    async for desc in iterator:
        sid = getattr(desc, "id", None)
        if sid and (not prefix or str(sid).startswith(prefix)):
            ids.append(str(sid))
    return ids


async def reconcile_schedules(
    client: Client,
    routines: Iterable[dict[str, Any]],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Make Temporal match the API: upsert enabled cron routines, drop orphans."""
    settings = settings or get_settings()
    desired: dict[str, dict[str, Any]] = {}
    for routine in routines:
        cron = routine.get("schedule_cron")
        if not cron or not routine.get("enabled", True):
            continue
        rid = str(routine.get("id") or routine.get("routine_id") or "")
        if not rid:
            continue
        desired[schedule_id(rid)] = routine

    upserted: list[str] = []
    failed: list[str] = []
    for sid, routine in desired.items():
        rid = str(routine.get("id") or routine.get("routine_id"))
        try:
            await upsert_schedule(
                client,
                routine_id=rid,
                bot_id=str(routine.get("bot_id", "")),
                steps=list(routine.get("steps") or []),
                cron=str(routine["schedule_cron"]),
                enabled=bool(routine.get("enabled", True)),
                settings=settings,
                extra={
                    "name": routine.get("name"),
                    "version": routine.get("version"),
                    # Carried only if the API exposes a routine owner; a cron
                    # routine with no owner is a genuinely unattended run.
                    "user_id": routine.get("owner_user_id") or routine.get("user_id"),
                },
            )
            upserted.append(sid)
        except Exception as exc:  # noqa: BLE001 - one bad routine must not stop boot
            failed.append(sid)
            log.warning("%s", kv(event="schedule.upsert.failed", schedule_id=sid, error=str(exc)[:200]))

    deleted: list[str] = []
    try:
        existing = await list_schedules(client)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s", kv(event="schedule.list.failed", error=str(exc)[:200]))
        existing = []
    for sid in existing:
        if sid in desired:
            continue
        try:
            if await delete_schedule(client, sid):
                deleted.append(sid)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s", kv(event="schedule.delete.failed", schedule_id=sid, error=str(exc)[:200]))

    log.info(
        "%s",
        kv(
            event="schedule.reconciled",
            upserted=len(upserted),
            deleted=len(deleted),
            failed=len(failed),
        ),
    )
    return {"upserted": upserted, "deleted": deleted, "failed": failed}
