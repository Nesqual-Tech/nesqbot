"""`app.services.desktop` - the generic self-hosted Kubernetes backend (BOT_DESKTOP_MODE=k8s).

Mirrors `test_desktop_service.py`'s ACI section: a fake `CoreV1Api` records every
call and replays scripted Pod states, so the lifecycle is proven without a live
cluster - exactly how the ACI tests prove the lifecycle without live Azure.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from app.services import desktop as desktop_module
from app.services.desktop import (
    ACI_CONTAINER_NAME,
    K8S_CONTROL_PORT,
    K8S_STREAM_PORT,
    DesktopManager,
    K8sStartError,
    k8s_pod_phase,
    k8s_pod_ready,
    k8s_pvc_name,
    k8s_resource_name,
    k8s_start_failure_reason,
)

NAMESPACE = "nesqbot-test"
IMAGE = "nesqbot/bot-desktop:local"
PUBLIC_HOST = "node1.example.internal"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def fake_pod(*, phase: str = "Running", ready: bool = True, waiting_reason: str | None = None):
    condition = SimpleNamespace(type="Ready", status="True" if ready else "False")
    if waiting_reason:
        state = SimpleNamespace(waiting=SimpleNamespace(reason=waiting_reason))
    else:
        state = SimpleNamespace(waiting=None)
    container_status = SimpleNamespace(state=state)
    status = SimpleNamespace(phase=phase, conditions=[condition], container_statuses=[container_status])
    return SimpleNamespace(status=status)


def fake_service(*, service_type: str = "ClusterIP", stream_node_port=None, control_node_port=None):
    ports = [
        SimpleNamespace(name="stream", port=K8S_STREAM_PORT, node_port=stream_node_port),
        SimpleNamespace(name="control", port=K8S_CONTROL_PORT, node_port=control_node_port),
    ]
    return SimpleNamespace(spec=SimpleNamespace(type=service_type, ports=ports))


class FakeCoreV1Api:
    """Records every call and replays a scripted sequence of pod states."""

    def __init__(self, pod_states=None, *, fail_on: str | None = None, service=None, pvc_exists=False):
        self.created_pods: list[tuple[str, object]] = []
        self.deleted_pods: list[tuple[str, str]] = []
        self.created_services: list[tuple[str, object]] = []
        self.deleted_services: list[tuple[str, str]] = []
        self.created_pvcs: list[tuple[str, object]] = []
        self.deleted_pvcs: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str]] = []
        self._pod_states = list(pod_states) if pod_states is not None else [fake_pod()]
        self._fail_on = fail_on
        self._service = service or fake_service()
        self._pvc_exists = pvc_exists

    def _boom(self, op: str) -> None:
        if self._fail_on == op:
            raise RuntimeError(f"k8s said no to {op}")

    def create_namespaced_pod(self, namespace, pod):
        self._boom("create_pod")
        self.created_pods.append((namespace, pod))

    def read_namespaced_pod(self, name, namespace):
        self.gets.append((namespace, name))
        self._boom("read_pod")
        if not self._pod_states:
            raise RuntimeError("the fake ran out of scripted pod states")
        pod = self._pod_states[0] if len(self._pod_states) == 1 else self._pod_states.pop(0)
        return pod

    def delete_namespaced_pod(self, name, namespace):
        self._boom("delete_pod")
        self.deleted_pods.append((namespace, name))

    def create_namespaced_service(self, namespace, service):
        self._boom("create_service")
        self.created_services.append((namespace, service))
        return self._service

    def delete_namespaced_service(self, name, namespace):
        self._boom("delete_service")
        self.deleted_services.append((namespace, name))

    def create_namespaced_persistent_volume_claim(self, namespace, pvc):
        self._boom("create_pvc")
        self.created_pvcs.append((namespace, pvc))
        self._pvc_exists = True

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        self._boom("read_pvc")
        if not self._pvc_exists:
            raise ApiException(status=404)
        return SimpleNamespace()

    def delete_namespaced_persistent_volume_claim(self, name, namespace):
        self._boom("delete_pvc")
        self.deleted_pvcs.append((namespace, name))


def k8s_manager(monkeypatch, core: FakeCoreV1Api, **overrides):
    """A `DesktopManager` in k8s mode wired to a fake `CoreV1Api`."""
    manager = DesktopManager()
    values = {
        "bot_desktop_mode": "k8s",
        "bot_desktop_image": IMAGE,
        "k8s_namespace": NAMESPACE,
        "k8s_start_timeout_seconds": 30,
        "nesq_sidecar_token": "test-sidecar-token",
    }
    values.update(overrides)
    manager.settings = manager.settings.model_copy(update=values)
    monkeypatch.setattr(manager, "_k8s_client", lambda: core)
    monkeypatch.setattr(desktop_module, "K8S_POLL_INTERVAL_SECONDS", 0)
    return manager


def a_bot(slug: str = "sales_ops", bot_id: str = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"):
    return SimpleNamespace(id=uuid.UUID(bot_id), slug=slug, desktop_profile="xfce")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def test_resource_name_is_a_valid_dns_1123_label():
    name = k8s_resource_name(a_bot(slug="Sales Ops!!"))
    assert name.replace("-", "").isalnum()
    assert name == name.lower()
    assert not name.startswith("-") and not name.endswith("-")
    assert len(name) <= 63


def test_resource_name_disambiguates_slugs_that_sanitise_the_same():
    a = k8s_resource_name(a_bot(slug="Sales Ops", bot_id="0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"))
    b = k8s_resource_name(a_bot(slug="sales-ops", bot_id="11111111-2222-3333-4444-555555555555"))
    assert a != b


def test_pvc_name_is_stable_across_a_slug_rename():
    original = k8s_pvc_name(a_bot(slug="sales_ops"))
    renamed = k8s_pvc_name(a_bot(slug="sales-team-renamed"))
    assert original == renamed, "the PVC must survive a slug rename, not orphan the volume"


# ---------------------------------------------------------------------------
# Pod status helpers
# ---------------------------------------------------------------------------


def test_pod_ready_requires_the_ready_condition_true():
    assert k8s_pod_ready(fake_pod(phase="Running", ready=True)) is True
    assert k8s_pod_ready(fake_pod(phase="Running", ready=False)) is False


def test_pod_phase_reads_status_phase():
    assert k8s_pod_phase(fake_pod(phase="Pending")) == "Pending"
    assert k8s_pod_phase(SimpleNamespace()) == ""


def test_start_failure_reason_names_an_image_pull_backoff():
    pod = fake_pod(phase="Pending", ready=False, waiting_reason="ImagePullBackOff")
    reason = k8s_start_failure_reason(pod, IMAGE, 42.0)
    assert "image pull was refused" in reason
    assert "ImagePullBackOff" in reason


def test_start_failure_reason_names_a_never_scheduled_pod():
    reason = k8s_start_failure_reason(None, IMAGE, 10.0)
    assert "never returned the pod" in reason


def test_start_failure_reason_names_a_running_but_not_ready_pod():
    pod = fake_pod(phase="Running", ready=False)
    reason = k8s_start_failure_reason(pod, IMAGE, 5.0)
    assert "never passed its readiness probe" in reason


# ---------------------------------------------------------------------------
# Config validation - fail loud rather than start something unreachable
# ---------------------------------------------------------------------------


async def test_start_refuses_nodeport_without_a_public_host(db, bot_a, monkeypatch):
    manager = k8s_manager(
        monkeypatch, FakeCoreV1Api(), k8s_service_type="NodePort", k8s_public_host=""
    )
    desktop = await manager.start(db, bot_a)
    assert desktop.state == "error"
    assert "k8s_public_host is empty" in desktop.last_error


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


async def test_start_records_running_with_the_clusterip_urls(db, bot_a, monkeypatch):
    manager = k8s_manager(monkeypatch, FakeCoreV1Api())
    desktop = await manager.start(db, bot_a)

    name = k8s_resource_name(bot_a)
    assert desktop.state == "running"
    assert desktop.container_id == name
    assert desktop.stream_url == f"http://{name}.{NAMESPACE}.svc.cluster.local:{K8S_STREAM_PORT}"
    assert desktop.control_url == f"http://{name}.{NAMESPACE}.svc.cluster.local:{K8S_CONTROL_PORT}"
    assert desktop.last_error is None


async def test_start_with_nodeport_uses_the_public_host_and_assigned_ports(db, bot_a, monkeypatch):
    core = FakeCoreV1Api(service=fake_service(service_type="NodePort", stream_node_port=31000, control_node_port=31001))
    manager = k8s_manager(monkeypatch, core, k8s_service_type="NodePort", k8s_public_host=PUBLIC_HOST)

    desktop = await manager.start(db, bot_a)

    assert desktop.stream_url == f"http://{PUBLIC_HOST}:31000"
    assert desktop.control_url == f"http://{PUBLIC_HOST}:31001"


async def test_start_deletes_a_stale_pod_and_service_first(db, bot_a, monkeypatch):
    """Idempotent like `_docker_start`: a leftover object from an ungraceful
    stop must not block the next start."""
    core = FakeCoreV1Api()
    manager = k8s_manager(monkeypatch, core)

    await manager.start(db, bot_a)

    name = k8s_resource_name(bot_a)
    assert core.deleted_pods == [(NAMESPACE, name)]
    assert core.deleted_services == [(NAMESPACE, name)]
    # The delete happened before the create.
    assert core.created_pods


async def test_start_creates_the_pvc_only_when_a_storage_class_is_configured(db, bot_a, monkeypatch):
    core = FakeCoreV1Api()
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")

    await manager.start(db, bot_a)

    assert core.created_pvcs
    _, pvc = core.created_pvcs[0]
    assert pvc.metadata.name == k8s_pvc_name(bot_a)
    assert pvc.spec.storage_class_name == "local-path"


async def test_start_does_not_touch_the_pvc_api_without_a_storage_class(db, bot_a, monkeypatch):
    core = FakeCoreV1Api()
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="")

    await manager.start(db, bot_a)

    assert core.created_pvcs == []


async def test_start_does_not_recreate_an_existing_pvc(db, bot_a, monkeypatch):
    core = FakeCoreV1Api(pvc_exists=True)
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")

    await manager.start(db, bot_a)

    assert core.created_pvcs == [], "an existing PVC must survive a start, not be replaced"


def test_pod_manifest_uses_a_hostpath_home_without_a_storage_class():
    manager = DesktopManager()
    manager.settings = manager.settings.model_copy(
        update={"k8s_storage_class": "", "k8s_host_path_root": "/var/lib/nesqbot/bot-homes"}
    )
    bot = a_bot()
    pod = manager._k8s_pod_manifest(bot, k8s_resource_name(bot))
    home_volume = next(v for v in pod.spec.volumes if v.name == "home")
    assert home_volume.host_path is not None
    assert home_volume.host_path.path == f"/var/lib/nesqbot/bot-homes/{bot.id}"
    assert home_volume.persistent_volume_claim is None


def test_pod_manifest_uses_a_pvc_home_with_a_storage_class():
    manager = DesktopManager()
    manager.settings = manager.settings.model_copy(update={"k8s_storage_class": "local-path"})
    bot = a_bot()
    pod = manager._k8s_pod_manifest(bot, k8s_resource_name(bot))
    home_volume = next(v for v in pod.spec.volumes if v.name == "home")
    assert home_volume.persistent_volume_claim is not None
    assert home_volume.persistent_volume_claim.claim_name == k8s_pvc_name(bot)
    assert home_volume.host_path is None


def test_pod_manifest_matches_the_aci_environment_contract():
    """Same four values `aci` sets, so one image serves every backend unmodified."""
    manager = DesktopManager()
    manager.settings = manager.settings.model_copy(update={"nesq_sidecar_token": "shh"})
    bot = a_bot()
    pod = manager._k8s_pod_manifest(bot, k8s_resource_name(bot))
    container = pod.spec.containers[0]
    env = {e.name: e.value for e in container.env}
    assert env["BOT_SLUG"] == bot.slug
    assert env["DESKTOP_PROFILE"] == "xfce"
    assert env["NESQ_STREAM_PORT"] == str(K8S_STREAM_PORT)
    assert env["NESQ_SIDECAR_PORT"] == str(K8S_CONTROL_PORT)
    assert env["NESQ_SIDECAR_TOKEN"] == "shh"
    assert container.name == ACI_CONTAINER_NAME


def test_pod_manifest_omits_the_sidecar_token_variable_when_unset():
    manager = DesktopManager()
    manager.settings = manager.settings.model_copy(update={"nesq_sidecar_token": ""})
    bot = a_bot()
    pod = manager._k8s_pod_manifest(bot, k8s_resource_name(bot))
    env_names = {e.name for e in pod.spec.containers[0].env}
    assert "NESQ_SIDECAR_TOKEN" not in env_names


def test_pod_manifest_is_hardened():
    manager = DesktopManager()
    bot = a_bot()
    pod = manager._k8s_pod_manifest(bot, k8s_resource_name(bot))
    pod_sc = pod.spec.security_context
    assert pod_sc.run_as_non_root is True
    assert pod_sc.seccomp_profile.type == "RuntimeDefault"
    container_sc = pod.spec.containers[0].security_context
    assert container_sc.allow_privilege_escalation is False
    assert container_sc.privileged is False
    assert container_sc.capabilities.drop == ["ALL"]
    volume_names = {v.name for v in pod.spec.volumes}
    assert {"home", "tmp", "dshm"} <= volume_names


async def test_a_pod_that_never_becomes_ready_lands_as_error_and_is_left_for_diagnosis(
    db, bot_a, monkeypatch
):
    core = FakeCoreV1Api([fake_pod(phase="Pending", ready=False)])
    manager = k8s_manager(monkeypatch, core, k8s_start_timeout_seconds=0)

    desktop = await manager.start(db, bot_a)

    assert desktop.state == "error"
    assert "did not become ready within" in desktop.last_error
    assert desktop.container_id == k8s_resource_name(bot_a), "left in place for diagnosis"


async def test_a_k8s_error_on_create_lands_as_error_state(db, bot_a, monkeypatch):
    manager = k8s_manager(monkeypatch, FakeCoreV1Api(fail_on="create_pod"))
    desktop = await manager.start(db, bot_a)
    assert desktop.state == "error"
    assert "k8s said no to create_pod" in desktop.last_error


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


async def test_stop_deletes_the_pod_and_service(db, bot_a, monkeypatch):
    core = FakeCoreV1Api()
    manager = k8s_manager(monkeypatch, core)
    await manager.start(db, bot_a)
    core.deleted_pods.clear()
    core.deleted_services.clear()

    desktop = await manager.stop(db, bot_a.id)

    name = k8s_resource_name(bot_a)
    assert core.deleted_pods == [(NAMESPACE, name)]
    assert core.deleted_services == [(NAMESPACE, name)]
    assert desktop.state == "absent"
    assert desktop.container_id is None


async def test_stop_keeps_the_pvc_by_default(db, bot_a, monkeypatch):
    core = FakeCoreV1Api()
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")
    await manager.start(db, bot_a)

    await manager.stop(db, bot_a.id)

    assert core.deleted_pvcs == [], "a plain stop must not destroy the bot's home"


async def test_stop_with_wipe_deletes_the_pvc_too(db, bot_a, monkeypatch, caplog):
    core = FakeCoreV1Api()
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")
    await manager.start(db, bot_a)

    with caplog.at_level(logging.INFO, logger="app.services.desktop"):
        desktop = await manager.stop(db, bot_a.id, wipe=True)

    assert core.deleted_pvcs == [(NAMESPACE, k8s_pvc_name(bot_a))]
    assert desktop.state == "absent"
    assert "PVC deleted with the pod" in caplog.text


async def test_stop_with_wipe_and_no_storage_class_wipes_the_hostpath_dir(
    db, bot_a, monkeypatch, tmp_path
):
    core = FakeCoreV1Api()
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="", k8s_host_path_root=str(tmp_path))
    home = tmp_path / str(bot_a.id)
    home.mkdir()
    (home / "profile").write_text("data")
    await manager.start(db, bot_a)

    desktop = await manager.stop(db, bot_a.id, wipe=True)

    assert desktop.state == "absent"
    assert not home.exists()


async def test_a_delete_failure_still_leaves_the_desktop_absent(db, bot_a, monkeypatch):
    """Teardown never fails a request; the operator gets a log, not a 500."""
    manager = k8s_manager(monkeypatch, FakeCoreV1Api(fail_on="delete_pod"))
    await manager.start(db, bot_a)
    desktop = await manager.stop(db, bot_a.id)
    assert desktop.state == "absent"


# ---------------------------------------------------------------------------
# suspend / resume
# ---------------------------------------------------------------------------


async def test_suspend_deletes_the_pod_but_keeps_the_pvc(db, bot_a, monkeypatch):
    core = FakeCoreV1Api()
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")
    await manager.start(db, bot_a)
    core.deleted_pods.clear()
    core.deleted_services.clear()

    desktop = await manager.suspend(db, bot_a.id)

    name = k8s_resource_name(bot_a)
    assert core.deleted_pods == [(NAMESPACE, name)]
    assert core.deleted_pvcs == [], "suspend must not touch the home volume"
    assert desktop.state == "suspended"
    assert desktop.stream_url is None
    assert desktop.control_url is None
    assert desktop.container_id == name, "resume needs this to recreate the pod"


async def test_resume_recreates_the_pod_and_reattaches_the_same_pvc(db, bot_a, monkeypatch):
    core = FakeCoreV1Api(pvc_exists=True)
    manager = k8s_manager(monkeypatch, core, k8s_storage_class="local-path")
    await manager.start(db, bot_a)
    await manager.suspend(db, bot_a.id)
    core.created_pods.clear()

    desktop = await manager.resume(db, bot_a.id)

    assert desktop.state == "running"
    assert core.created_pvcs == [], "the existing PVC is reattached, not recreated"
    _, pod = core.created_pods[0]
    home_volume = next(v for v in pod.spec.volumes if v.name == "home")
    assert home_volume.persistent_volume_claim.claim_name == k8s_pvc_name(bot_a)
    assert desktop.stream_url is not None


async def test_resume_is_a_noop_when_not_suspended(db, bot_a, monkeypatch):
    manager = k8s_manager(monkeypatch, FakeCoreV1Api())
    desktop = await manager.resume(db, bot_a.id)
    assert desktop.state == "absent"


async def test_k8s_resume_raises_cleanly_when_the_bot_row_is_gone(db, monkeypatch):
    """`_k8s_resume` fetches the `Bot` row fresh; a deleted bot must error, not crash.

    Exercised directly against `_k8s_resume` rather than through the public
    `resume()` -> `get()` path: `bot_desktops.bot_id` has a foreign key onto
    `bots`, so a desktop row can never actually reference a bot that does not
    exist - `_k8s_resume`'s own `db.get(Bot, bot_id)` guard is what has to be
    proven here, not the schema's referential integrity.
    """
    manager = k8s_manager(monkeypatch, FakeCoreV1Api())
    missing_id = uuid.uuid4()

    try:
        await manager._k8s_resume(db, missing_id, "nesq-desktop-ghost-000000000000")
        raised = None
    except K8sStartError as exc:
        raised = exc

    assert raised is not None
    assert "no longer exists" in str(raised)
