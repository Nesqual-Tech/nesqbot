"""Boot must be bounded, and the health marker must not go green early.

Three worker-owned failures in the old eight-line boot sequence:

* `reconcile()` awaited one `upsert_schedule` per routine with no deadline and
  ran *before* `Worker(...)` was constructed, so a Temporal server that accepts
  connections and then stalls on `create_schedule` hung boot forever, never
  wrote the health marker, and let the container HEALTHCHECK restart the worker
  into a loop — the same class of bug as commits 557cab9 (schema bootstrap
  lock) and f80af74 (bots/runs lock);
* `_touch_health` and `_health_loop` started before the worker existed, so the
  probe went green during boot and stayed green if the worker shut down while
  the process lingered: a wedged worker that looks healthy.

Pure unit — no Temporal server, no sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from worker import client as temporal_client  # noqa: E402
from worker import main  # noqa: E402
from worker.config import Settings  # noqa: E402


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "worker_health_file": str(tmp_path / "worker.health"),
        "worker_health_interval_seconds": 0.01,
        "worker_reconcile_timeout_seconds": 0.05,
    }
    base.update(overrides)
    return Settings(**base)


class _FakeWorker:
    """Stands in for `temporalio.worker.Worker` (async context manager + flag)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.entered = False
        self._running = False
        _FakeWorker.constructed.append(self)

    constructed: list["_FakeWorker"] = []

    @property
    def is_running(self) -> bool:
        return self._running

    async def __aenter__(self) -> "_FakeWorker":
        self.entered = True
        self._running = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._running = False


# --------------------------------------------------------------------------- #
# Bounded reconcile
# --------------------------------------------------------------------------- #


async def test_reconcile_gives_up_on_a_hanging_temporal_instead_of_hanging_boot(
    tmp_path, monkeypatch, caplog
):
    """A `create_schedule` that never returns costs the budget, not the boot."""

    async def _routines(settings: Settings) -> list[dict[str, Any]]:
        return [{"id": "r1", "bot_id": "b1", "schedule_cron": "0 9 * * *", "enabled": True}]

    started = asyncio.Event()

    async def _hangs(*args: Any, **kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()  # never resolves, like a stalled RPC

    monkeypatch.setattr(main, "fetch_routines", _routines)
    monkeypatch.setattr(temporal_client, "reconcile_schedules", _hangs)

    with caplog.at_level("WARNING"):
        await asyncio.wait_for(
            main.reconcile(object(), _settings(tmp_path)), timeout=5.0
        )

    assert started.is_set(), "the sync really started before the budget expired"
    assert "schedule.reconcile.timeout" in caplog.text


async def test_an_unreachable_api_skips_reconcile_without_raising(tmp_path, monkeypatch, caplog):
    """`fetch_routines` returning None is a normal boot condition (API still up-
    coming in compose), not a failure that may stop the worker serving."""

    async def _none(settings: Settings) -> None:
        return None

    monkeypatch.setattr(main, "fetch_routines", _none)

    with caplog.at_level("WARNING"):
        await main.reconcile(object(), _settings(tmp_path))

    assert "schedule.reconcile.skipped" in caplog.text


# --------------------------------------------------------------------------- #
# The health marker
# --------------------------------------------------------------------------- #


async def test_the_health_loop_refuses_to_touch_a_stopped_worker(tmp_path, caplog):
    """A worker that has shut down while the process lingers must let the marker
    go stale — that staleness is the only thing the HEALTHCHECK can see."""
    settings = _settings(tmp_path)
    marker = Path(settings.worker_health_file)
    worker = _FakeWorker()  # constructed, never entered -> is_running False

    task = asyncio.create_task(main._health_loop(settings, worker))
    with caplog.at_level("WARNING"):
        await asyncio.sleep(0.05)
        assert not marker.exists()
        assert "health.touch.refused" in caplog.text

        # Once it is genuinely serving, the marker is refreshed again.
        async with worker:
            await asyncio.sleep(0.05)
            assert marker.exists()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_the_marker_appears_only_once_the_worker_is_serving(tmp_path, monkeypatch):
    """The window this closes: the marker used to be written before
    `Worker(...)` was even constructed, so the probe passed while the worker was
    not polling anything."""
    settings = _settings(tmp_path)
    marker = Path(settings.worker_health_file)
    _FakeWorker.constructed.clear()

    async def _connect(_settings: Settings) -> object:
        return object()

    reconciled = asyncio.Event()

    async def _reconcile(client: Any, _settings: Settings) -> None:
        reconciled.set()
        # Marker must not exist yet: reconcile runs before the worker serves.
        assert not marker.exists()

    monkeypatch.setattr(main, "_install_signal_handlers", lambda loop, stop: None)
    monkeypatch.setattr(temporal_client, "connect", _connect)
    monkeypatch.setattr(main, "reconcile", _reconcile)
    monkeypatch.setattr(main, "Worker", _FakeWorker)

    task = asyncio.create_task(main.run_worker(settings))
    seen_marker = False
    for _ in range(500):
        if marker.exists():
            seen_marker = True
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert reconciled.is_set()
    # Worker construction is still reached, and the marker followed it.
    assert _FakeWorker.constructed and _FakeWorker.constructed[-1].entered
    assert seen_marker
