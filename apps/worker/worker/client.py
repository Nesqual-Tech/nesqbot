"""Temporal client helpers — connection with backoff and schedule management.

The schedule id format (`routine-{routine_id}`), the target workflow, the task
queue and the single-dict workflow argument are a shared contract with the API's
`app/services/temporal_client.py`. Change them in both places or not at all.
"""

from __future__ import annotations

import inspect
import logging
from datetime import timedelta
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
    extra = dict(extra or {})
    # `WORKER_APPROVAL_TIMEOUT_SECONDS` is a documented env var (.env.example),
    # and `RoutineWorkflow` is where the approval deadline is now enforced — but
    # workflow code must not read settings: a value re-read during replay can
    # differ from the one the run started with. Baking it into the schedule's
    # workflow argument is the deterministic way to keep the setting live; it is
    # written once per upsert and replays identically. `RoutineInput` already
    # reads this key, and a run the API starts directly simply falls back to
    # `workflows.APPROVAL_DEADLINE`, which is the same 24 hours as the default.
    extra.setdefault("approval_timeout_seconds", settings.worker_approval_timeout_seconds)
    payload = routine_payload(routine_id, bot_id, steps, **extra)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            settings.worker_schedule_workflow,
            args=[payload],
            # `routine-{id}-scheduled`, matching what the API commits in
            # `services/temporal_client.py`. This module's docstring calls the
            # id format a shared contract to change in both places or not at
            # all; the worker used to pass `sid` (the schedule id itself), so
            # the worker was the side that diverged and is the side fixed here.
            id=f"{sid}-scheduled",
            task_queue=settings.temporal_task_queue,
            execution_timeout=timedelta(seconds=settings.worker_schedule_execution_timeout_seconds),
        ),
        spec=ScheduleSpec(cron_expressions=[cron]),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            # Never the SDK default of 365 days: see
            # `worker_schedule_catchup_window_seconds` for the burst of real
            # sends that default produces after an outage.
            catchup_window=timedelta(seconds=settings.worker_schedule_catchup_window_seconds),
        ),
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
    """Upsert enabled cron routines; sweep orphans only when that is provably safe.

    `routines` is whatever `GET /routines` answered, and that answer is a
    *visibility-filtered* view, not an inventory — see
    `worker_reconcile_delete_orphans` for the production-only data loss that
    treating it as an inventory caused. Upserts are always safe (they only ever
    write the schedule the API asked for); deletes are gated twice.
    """
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
    refused: str | None = None
    if not settings.worker_reconcile_delete_orphans:
        refused = "disabled"
        log.info("%s", kv(event="schedule.orphans.skipped", reason=refused))
    else:
        try:
            existing = await list_schedules(client)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s", kv(event="schedule.list.failed", error=str(exc)[:200]))
            existing = []
        orphans = [sid for sid in existing if sid not in desired]
        # Guard 1: an empty routine list is indistinguishable from "the API
        # answered for an identity that can see nothing", and deleting every
        # schedule on that basis is unrecoverable.
        if not desired:
            refused = "empty_routine_list"
        # Guard 2: a filtered or paginated answer read as authoritative shows up
        # as "most schedules are suddenly orphans". A genuine orphan is rare and
        # arrives one at a time.
        elif existing and len(orphans) > settings.worker_reconcile_orphan_max_fraction * len(existing):
            refused = "orphan_fraction_too_high"
        if refused:
            log.warning(
                "%s",
                kv(
                    event="schedule.orphans.refused",
                    reason=refused,
                    existing=len(existing),
                    desired=len(desired),
                    orphans=len(orphans),
                ),
            )
        else:
            for sid in orphans:
                try:
                    if await delete_schedule(client, sid):
                        deleted.append(sid)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "%s",
                        kv(event="schedule.delete.failed", schedule_id=sid, error=str(exc)[:200]),
                    )

    log.info(
        "%s",
        kv(
            event="schedule.reconciled",
            upserted=len(upserted),
            deleted=len(deleted),
            failed=len(failed),
            orphans_refused=refused,
        ),
    )
    return {"upserted": upserted, "deleted": deleted, "failed": failed, "orphans_refused": refused}
