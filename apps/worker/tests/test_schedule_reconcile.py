"""Boot reconciliation must not delete schedules it merely cannot see.

`reconcile_schedules` builds `desired` from `GET /routines` and used to delete
every existing `routine-*` schedule that was not in it. That list is not an
inventory: the worker authenticates with `worker_api_token`, the API resolves
that to the service user (`app/auth.py`, documented as owning no bots), and
`GET /routines` filters on `bot_visibility_clause` (`Bot.is_system OR
Bot.owner_user_id == user.id`). So in production the worker sees only
system-bot routines and, on every restart, deleted the schedules the API had
created for every user-owned bot. In development the `X-Nesq-Dev` path resolves
to the dev user, who does own bots — which is why this never showed up locally.

Also pinned here: the schedule policy the worker sends (items the SDK defaults
badly), and the `-scheduled` workflow id the API commits.

All pure-unit against a dict-backed fake client — the pattern the API's ACI and
k8s tests use with fake management clients — so no Temporal server is needed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from worker import client as temporal_client  # noqa: E402
from worker.config import Settings  # noqa: E402


class _FakeHandle:
    def __init__(self, store: dict[str, Any], sid: str) -> None:
        self._store = store
        self._sid = sid

    async def update(self, updater) -> None:
        from temporalio.client import ScheduleUpdate

        result = updater(None)
        assert isinstance(result, ScheduleUpdate)
        self._store[self._sid] = result.schedule

    async def delete(self) -> None:
        if self._sid not in self._store:
            raise RuntimeError(f"schedule not found: {self._sid}")
        del self._store[self._sid]


class _Desc:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _FakeSchedules:
    """Async iterator over the fake store, like `Client.list_schedules()`."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._ids:
            raise StopAsyncIteration
        return _Desc(self._ids.pop(0))


class FakeClient:
    """Enough of `temporalio.client.Client` for schedule reconciliation."""

    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.store: dict[str, Any] = dict(existing or {})
        self.deleted: list[str] = []
        self.created: list[str] = []

    async def create_schedule(self, sid: str, schedule: Any) -> None:
        if sid in self.store:
            raise RuntimeError(f"schedule already exists: {sid}")
        self.store[sid] = schedule
        self.created.append(sid)

    def get_schedule_handle(self, sid: str) -> _FakeHandle:
        handle = _FakeHandle(self.store, sid)
        original_delete = handle.delete

        async def _delete() -> None:
            await original_delete()
            self.deleted.append(sid)

        handle.delete = _delete  # type: ignore[method-assign]
        return handle

    def list_schedules(self) -> _FakeSchedules:
        return _FakeSchedules(sorted(self.store.keys()))


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "temporal_task_queue": "nesq-bot",
        "worker_schedule_workflow": "RoutineWorkflow",
    }
    base.update(overrides)
    return Settings(**base)


def _routine(rid: str, *, cron: str = "0 9 * * *", enabled: bool = True) -> dict[str, Any]:
    return {
        "id": rid,
        "bot_id": f"bot-{rid}",
        "steps": [{"type": "connector", "connector_id": "crm", "action": "ping"}],
        "schedule_cron": cron,
        "enabled": enabled,
        "name": f"routine {rid}",
        "version": 1,
    }


# --------------------------------------------------------------------------- #
# The orphan sweep
# --------------------------------------------------------------------------- #


async def test_a_partial_routine_list_deletes_nothing(caplog):
    """One visible routine out of three schedules is a filtered view, not truth.

    This is the production data-loss case: the service user sees only system-bot
    routines, so a sweep would delete every user-owned bot's schedule.
    """
    client = FakeClient({"routine-a": object(), "routine-b": object(), "routine-c": object()})

    with caplog.at_level("WARNING"):
        result = await temporal_client.reconcile_schedules(
            client,
            [_routine("a")],
            settings=_settings(worker_reconcile_delete_orphans=True),
        )

    assert client.deleted == []
    assert result["deleted"] == []
    assert result["orphans_refused"] == "orphan_fraction_too_high"
    assert "schedule.orphans.refused" in caplog.text
    # b and c survive untouched.
    assert {"routine-a", "routine-b", "routine-c"} <= set(client.store)


async def test_an_empty_routine_list_deletes_nothing(caplog):
    """An API that answers `[]` for an identity that can see nothing must not be
    read as "there are no routines" — that deletion is unrecoverable."""
    client = FakeClient({"routine-a": object(), "routine-b": object()})

    with caplog.at_level("WARNING"):
        result = await temporal_client.reconcile_schedules(
            client, [], settings=_settings(worker_reconcile_delete_orphans=True)
        )

    assert client.deleted == []
    assert result["orphans_refused"] == "empty_routine_list"
    assert set(client.store) == {"routine-a", "routine-b"}


async def test_the_orphan_sweep_is_off_by_default():
    """Default configuration never deletes. The API already deletes a routine's
    schedule on DELETE and on disable, so nothing is lost by refusing here."""
    client = FakeClient({"routine-a": object(), "routine-orphan": object()})

    result = await temporal_client.reconcile_schedules(
        client, [_routine("a")], settings=_settings()
    )

    assert client.deleted == []
    assert result["orphans_refused"] == "disabled"
    assert "routine-orphan" in client.store


async def test_a_single_orphan_is_deleted_when_the_list_is_plausibly_complete():
    """The sweep still works when switched on and the evidence supports it."""
    client = FakeClient(
        {"routine-a": object(), "routine-b": object(), "routine-orphan": object()}
    )

    result = await temporal_client.reconcile_schedules(
        client,
        [_routine("a"), _routine("b")],
        settings=_settings(worker_reconcile_delete_orphans=True),
    )

    assert client.deleted == ["routine-orphan"]
    assert result["deleted"] == ["routine-orphan"]
    assert result["orphans_refused"] is None
    assert set(client.store) == {"routine-a", "routine-b"}


async def test_disabled_and_cronless_routines_are_not_upserted():
    """`desired` is enabled cron routines only — the pre-existing contract."""
    client = FakeClient()

    result = await temporal_client.reconcile_schedules(
        client,
        [_routine("a"), _routine("b", enabled=False), _routine("c", cron="")],
        settings=_settings(),
    )

    assert result["upserted"] == ["routine-a"]
    assert set(client.store) == {"routine-a"}


# --------------------------------------------------------------------------- #
# The schedule the worker sends
# --------------------------------------------------------------------------- #


def _build(**overrides: Any):
    return temporal_client._build_schedule(
        settings=_settings(**overrides),
        routine_id="r1",
        bot_id="bot-1",
        steps=[{"type": "connector", "connector_id": "crm", "action": "ping"}],
        cron="0 2 * * *",
        enabled=True,
    )


def test_the_schedule_never_uses_the_sdk_default_catchup_window():
    """`SchedulePolicy.catchup_window` defaults to 365 days on temporalio 1.9.0
    (measured by inspecting the dataclass field). Left at that, a week-long
    Temporal outage backfills every missed nominal time on recovery: a nightly
    outreach routine fires seven times in a burst, at real cost, and nothing
    downstream de-duplicates it."""
    schedule = _build()

    assert schedule.policy is not None
    assert schedule.policy.catchup_window == timedelta(seconds=600.0)
    assert schedule.policy.catchup_window != timedelta(days=365)


def test_the_schedule_action_bounds_one_run():
    """Under `ScheduleOverlapPolicy.SKIP` a run that never ends silently mutes
    every later fire, so one unanswered approval at 2am stops the routine
    indefinitely with nothing failing and nothing in the UI."""
    schedule = _build()

    assert schedule.action.execution_timeout == timedelta(seconds=90000.0)
    # Long enough that a run legitimately parked on the 24-hour approval
    # deadline reaches its own clean abort first.
    assert schedule.action.execution_timeout > timedelta(hours=24)


def test_both_schedule_bounds_are_configurable():
    schedule = _build(
        worker_schedule_catchup_window_seconds=60.0,
        worker_schedule_execution_timeout_seconds=120.0,
    )

    assert schedule.policy.catchup_window == timedelta(seconds=60)
    assert schedule.action.execution_timeout == timedelta(seconds=120)


def test_the_scheduled_payload_carries_the_configured_approval_deadline():
    """`WORKER_APPROVAL_TIMEOUT_SECONDS` is documented in .env.example, and the
    deadline it names is now enforced inside `RoutineWorkflow` — which cannot
    read settings without breaking replay. So it has to travel in the workflow
    argument, or the setting silently stops doing anything."""
    schedule = _build(worker_approval_timeout_seconds=3600.0)

    (payload,) = schedule.action.args
    assert payload["approval_timeout_seconds"] == 3600.0
    assert payload["routine_id"] == "r1"


def test_the_action_carries_the_workflow_id_the_api_commits():
    """`client.py`'s docstring calls the id format a contract to change in both
    places or not at all. The API writes `routine-{id}-scheduled`
    (`services/temporal_client.py`); the worker used to overwrite it with the
    schedule id itself, so the worker was the side that diverged."""
    schedule = _build()

    assert schedule.action.id == "routine-r1-scheduled"


async def test_an_upsert_of_an_existing_schedule_updates_it_in_place():
    """Restarting the worker must not need the schedule deleted first."""
    client = FakeClient()
    settings = _settings()

    first = await temporal_client.upsert_schedule(
        client,
        routine_id="r1",
        bot_id="bot-1",
        steps=[],
        cron="0 2 * * *",
        settings=settings,
    )
    await temporal_client.upsert_schedule(
        client,
        routine_id="r1",
        bot_id="bot-1",
        steps=[],
        cron="30 3 * * *",
        settings=settings,
    )

    assert first == "routine-r1"
    assert client.created == ["routine-r1"]
    assert client.store["routine-r1"].spec.cron_expressions == ["30 3 * * *"]
