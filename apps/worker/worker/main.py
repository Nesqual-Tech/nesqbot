"""Temporal worker entrypoint.

    python -m worker.main                # run the worker
    python -m worker.main --healthcheck  # container HEALTHCHECK probe

Boot sequence: connect to Temporal (retry forever, Temporal is often not up yet
in compose) -> reconcile routine schedules against the API -> serve the task
queue until SIGTERM/SIGINT, then drain gracefully.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from temporalio.client import Client
from temporalio.worker import Worker

from worker import client as temporal_client
from worker.activities import ALL_ACTIVITIES
from worker.config import Settings, configure_logging, get_settings, kv
from worker.workflows import ALL_WORKFLOWS

log = logging.getLogger("worker.main")


# --------------------------------------------------------------------------- #
# Liveness marker (read by the Docker HEALTHCHECK)
# --------------------------------------------------------------------------- #


def _touch_health(settings: Settings) -> None:
    try:
        path = Path(settings.worker_health_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())), encoding="utf-8")
    except OSError as exc:
        log.debug("%s", kv(event="health.touch.failed", error=str(exc)[:200]))


async def _health_loop(settings: Settings) -> None:
    while True:
        _touch_health(settings)
        await asyncio.sleep(settings.worker_health_interval_seconds)


def healthcheck(settings: Settings | None = None) -> int:
    """Exit 0 when the worker refreshed its liveness marker recently."""
    settings = settings or get_settings()
    path = Path(settings.worker_health_file)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        print(f"unhealthy: no marker at {path}", file=sys.stderr)
        return 1
    if age > settings.worker_health_max_age_seconds:
        print(f"unhealthy: marker is {age:.0f}s old", file=sys.stderr)
        return 1
    print(f"healthy: marker {age:.0f}s old")
    return 0


# --------------------------------------------------------------------------- #
# Boot-time schedule reconciliation
# --------------------------------------------------------------------------- #


async def fetch_routines(settings: Settings) -> list[dict[str, Any]] | None:
    """GET /routines. Returns None when the API is unreachable (not an error)."""
    try:
        async with httpx.AsyncClient(
            base_url=settings.api_base,
            headers=settings.api_headers(),
            timeout=httpx.Timeout(30.0, connect=settings.worker_http_connect_timeout_seconds),
        ) as http:
            resp = await http.get("/routines")
    except Exception as exc:  # noqa: BLE001 - API may still be booting
        log.warning("%s", kv(event="routines.fetch.unreachable", error=f"{type(exc).__name__}: {exc}"))
        return None
    if not resp.is_success:
        log.warning("%s", kv(event="routines.fetch.failed", http=resp.status_code))
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        log.warning("%s", kv(event="routines.fetch.badjson"))
        return None
    if not isinstance(body, list):
        log.warning("%s", kv(event="routines.fetch.badshape", got=type(body).__name__))
        return None
    return [r for r in body if isinstance(r, dict)]


async def reconcile(client: Client, settings: Settings) -> None:
    """Best-effort: a schedule sync failure must never stop the worker booting."""
    if not settings.worker_reconcile_schedules:
        log.info("%s", kv(event="schedule.reconcile.disabled"))
        return
    routines = await fetch_routines(settings)
    if routines is None:
        log.warning("%s", kv(event="schedule.reconcile.skipped", reason="api_unreachable"))
        return
    try:
        await temporal_client.reconcile_schedules(client, routines, settings=settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s", kv(event="schedule.reconcile.failed", error=f"{type(exc).__name__}: {exc}"))


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def _banner(settings: Settings) -> None:
    log.info("%s", kv(event="worker.boot", banner="Nesq Bot Temporal worker"))
    log.info(
        "%s",
        kv(
            event="worker.config",
            env=settings.nesq_env,
            temporal=settings.temporal_host,
            namespace=settings.temporal_namespace,
            task_queue=settings.temporal_task_queue,
            api=settings.api_internal_url,
            schedule_workflow=settings.worker_schedule_workflow,
            max_activities=settings.worker_max_concurrent_activities,
        ),
    )
    log.info(
        "%s",
        kv(
            event="worker.registry",
            workflows=",".join(w.__name__ for w in ALL_WORKFLOWS),
            activities=",".join(a.__name__ for a in ALL_ACTIVITIES),
        ),
    )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def _request_stop(signame: str) -> None:
        log.info("%s", kv(event="worker.signal", signal=signame))
        stop.set()

    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop, signame)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows / non-main thread: fall back to the default handler.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda *_: _request_stop(signame))


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    _banner(settings)

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    # Race the (potentially endless) connect against shutdown so `docker stop`
    # during a Temporal outage exits immediately instead of waiting for SIGKILL.
    connect_task = asyncio.create_task(temporal_client.connect(settings), name="connect")
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    await asyncio.wait({connect_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if not connect_task.done():
        connect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await connect_task
        log.info("%s", kv(event="worker.stopped", reason="shutdown_before_connect"))
        return
    stop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stop_task
    client = connect_task.result()

    await reconcile(client, settings)

    _touch_health(settings)
    health_task = asyncio.create_task(_health_loop(settings), name="health")

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=ALL_WORKFLOWS,
        activities=ALL_ACTIVITIES,
        max_concurrent_activities=settings.worker_max_concurrent_activities,
        max_concurrent_workflow_tasks=settings.worker_max_concurrent_workflow_tasks,
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
    )
    log.info(
        "%s",
        kv(
            event="worker.ready",
            task_queue=settings.temporal_task_queue,
            host=settings.temporal_host,
        ),
    )
    try:
        async with worker:
            await stop.wait()
        log.info("%s", kv(event="worker.drained"))
    finally:
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
        with contextlib.suppress(OSError):
            Path(settings.worker_health_file).unlink(missing_ok=True)
        log.info("%s", kv(event="worker.stopped"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    configure_logging(os.getenv("LOG_LEVEL", settings.log_level))

    if "--healthcheck" in argv:
        return healthcheck(settings)

    try:
        asyncio.run(run_worker(settings))
    except KeyboardInterrupt:
        log.info("%s", kv(event="worker.interrupted"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
