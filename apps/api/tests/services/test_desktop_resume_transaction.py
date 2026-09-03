"""`resume` and `suspend` must not hold a transaction across a cold start.

The same shape as the 2026-09-02 incident in `app.db.release_transaction`, in
the driver rather than the model path, and this one loses money as well as
state.

`DesktopManager.get` issues a SELECT and only commits on the branch that creates
a missing row, so the ordinary path reaches `_aci_resume`/`_k8s_resume` with a
transaction open. Those wait on a container cold start budgeted at
`aci_start_timeout_seconds = 180` and `k8s_start_timeout_seconds = 180` — three
times the `idle_in_transaction_session_timeout = 60000` that
`release_transaction` records for nesqbot-pg. The commit at the end of
`resume` then fails on the terminated backend, so the row keeps
`state="suspended"` and the *old* container_id/stream_url while the group is
actually running: the viewer points at a dead address and nothing records the
new handle, against a group that `stop`'s own docstring says "bills until
something does".

That `start` and `stop` both commit immediately before their slow call is the
evidence this was an omission rather than a design.

The fake `CoreV1Api` is `test_desktop_k8s.py`'s, reused rather than copied — it
already proves this lifecycle with no live cluster, and a second fake would be a
second set of behaviours to keep in step.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import BotDesktop, CostLedger
from app.services.desktop import k8s_resource_name

# The fake below subclasses `test_desktop_k8s.FakeCoreV1Api`, whose module
# imports the `kubernetes` client at the top level. It is a real dependency
# (requirements.txt pins 31.0.0), so this only matters to an environment that
# installed the runtime without it - skip rather than fail the collection of a
# file that is about transactions, not about Kubernetes.
pytest.importorskip("kubernetes")

from tests.services.test_desktop_k8s import (  # noqa: E402 - after the importorskip above
    NAMESPACE,
    FakeCoreV1Api,
    k8s_manager,
)


class TransactionWatchingCoreV1Api(FakeCoreV1Api):
    """Records `db.in_transaction()` at the moment the cluster is called.

    That is the state Postgres is looking at while the call is outstanding,
    which is the only moment the assertion is about.
    """

    def __init__(self, db, **kwargs) -> None:
        super().__init__(**kwargs)
        self._db = db
        self.in_transaction_at_call: list[tuple[str, bool]] = []

    def _note(self, op: str) -> None:
        self.in_transaction_at_call.append((op, self._db.in_transaction()))

    def create_namespaced_pod(self, namespace, pod):
        self._note("create_pod")
        return super().create_namespaced_pod(namespace, pod)

    def read_namespaced_pod(self, name, namespace):
        self._note("read_pod")
        return super().read_namespaced_pod(name, namespace)

    def delete_namespaced_pod(self, name, namespace):
        self._note("delete_pod")
        return super().delete_namespaced_pod(name, namespace)

    def create_namespaced_service(self, namespace, service):
        self._note("create_service")
        return super().create_namespaced_service(namespace, service)

    def delete_namespaced_service(self, name, namespace):
        self._note("delete_service")
        return super().delete_namespaced_service(name, namespace)


def _held_open(core: TransactionWatchingCoreV1Api) -> list[str]:
    return [op for op, in_transaction in core.in_transaction_at_call if in_transaction]


async def test_resume_closes_the_transaction_before_waiting_on_the_cold_start(
    db, bot_a, monkeypatch
):
    bot_id = bot_a.id
    core = TransactionWatchingCoreV1Api(db, pvc_exists=True)
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")
    await manager.start(db, bot_a)
    await manager.suspend(db, bot_id)

    # Force `DesktopManager.get`'s SELECT to actually hit the database rather
    # than the identity map, so the transaction this test is about is the one
    # production opens, not one the test arranged.
    db.expunge_all()
    core.in_transaction_at_call.clear()
    core.created_pods.clear()

    desktop = await manager.resume(db, bot_id)

    assert core.in_transaction_at_call, "the cluster was never called"
    assert _held_open(core) == [], (
        "a k8s resume waited on the cold start with a transaction open; at "
        "k8s_start_timeout_seconds=180 against a 60s "
        "idle_in_transaction_session_timeout the connection dies under it"
    )

    # And the row landed: the point of releasing early is that the write at the
    # end of `resume` still has a live connection to land on.
    assert desktop.state == "running"
    assert desktop.container_id == k8s_resource_name(bot_a)
    assert desktop.stream_url is not None
    db.expunge_all()
    stored = await db.get(BotDesktop, bot_id)
    assert stored is not None
    assert stored.state == "running", "the new state was never committed"
    assert stored.stream_url == desktop.stream_url


async def test_suspend_closes_the_transaction_before_deleting_the_pod(db, bot_a, monkeypatch):
    """Same gap, same fix. A Pod delete is an API-server round trip with no
    bound this code controls, held open by `get`'s own SELECT."""
    bot_id = bot_a.id
    core = TransactionWatchingCoreV1Api(db, pvc_exists=True)
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")
    await manager.start(db, bot_a)

    db.expunge_all()
    core.in_transaction_at_call.clear()
    # `start` deletes any stale Pod/Service before it creates fresh ones — see
    # `_k8s_start` — so the recorder already holds one delete that has nothing
    # to do with what this test is asserting. Cleared here for the same reason
    # `in_transaction_at_call` is: the arrangement must not be mistaken for the
    # behaviour.
    core.deleted_pods.clear()

    desktop = await manager.suspend(db, bot_id)

    assert ("delete_pod", False) in core.in_transaction_at_call
    assert _held_open(core) == [], "a k8s suspend deleted the Pod with a transaction open"
    assert desktop.state == "suspended"
    assert core.deleted_pods == [(NAMESPACE, k8s_resource_name(bot_a))]


async def test_the_release_survives_a_transaction_the_caller_opened_first(
    db, bot_a, monkeypatch
):
    """The production shape: the request's auth dependency has already read.

    `get_current_user` shares the request session and SELECTs the user before
    any handler runs, so `DesktopManager` is never the thing that opened the
    transaction it has to close.
    """
    bot_id = bot_a.id
    core = TransactionWatchingCoreV1Api(db, pvc_exists=True)
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")
    await manager.start(db, bot_a)
    await manager.suspend(db, bot_id)
    core.in_transaction_at_call.clear()

    await db.execute(select(func.count()).select_from(CostLedger))
    assert db.in_transaction(), "the test is not exercising an open transaction"

    await manager.resume(db, bot_id)

    assert _held_open(core) == []
