"""Bot Desktop lifecycle â€” mock and docker locally, Azure Container Instances or a
generic self-hosted Kubernetes cluster in production.

Five modes, one `DesktopManager` surface (`start` / `stop` / `suspend` / `resume` /
`screenshot` / `windows` / `computer_action` / `browser_call`):

* ``mock``   â€” no container at all; canned screenshot and window list.
* ``docker`` â€” a container on the API host, bind-mounted per-bot home, published ports.
* ``aci``    â€” one Azure Container Instances *container group* per bot: hypervisor
  isolated, its own kernel and filesystem, a private IP in the delegated subnet, and
  billed per second so an idle roster costs nothing.
* ``k8s``    â€” one Pod per bot against any standard Kubernetes cluster (k3s, kind, EKS,
  GKE, bare metal - whatever the kubeconfig points at), driven directly by the API through
  the `kubernetes` client. The self-hosted alternative to `aci`: no Azure dependency, and
  a real PersistentVolumeClaim (or hostPath, single-node/dev only) keeps a bot's home
  across a stop, which `aci` cannot do at all.
* ``aks``    â€” the older, static-template pod-per-bot path (see
  ``infra/bot-desktop/k8s/desktop-template.yaml``): the API only records "pending" and a
  human applies the template out of band. Superseded by ``k8s`` for anyone who wants the
  API to actually drive the cluster; kept for the manual/CI deployment it was built for.

The per-bot boundary is the product claim (see docs/competitive-analysis.md), so neither
the ACI nor the k8s driver ever shares or reuses a container group/Pod between bots, and
the ACI driver never asks Azure for a public IP.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import shutil
import struct
import time
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import release_transaction
from app.models import Bot, BotDesktop

# The `/browser/*` table: paths, methods and the field whitelist `browser_call`
# forwards. Imported as a module rather than by name so the proxy below reads
# as "the browser table says", and so nothing here can quietly grow a second
# copy of an endpoint list.
from app.services import browser as browser_ops

# The risk vocabulary and the name-based classifier moved to `app.services.risk`
# once MCP tool calls started being gated by the same rule: a classifier living
# in a device-specific module invites a second copy, and a gate that exists on
# one execution path and not another is not a gate. Re-exported here so every
# existing import â€” `routers.desktop`, the risk-gating suite, the desktop
# service tests â€” keeps resolving to the one implementation.
from app.services.risk import (
    ACTION_RISKS,
    DEFAULT_ACTION_RISK,
    RISK_KEYWORDS,
    RISK_ORDER,
    RISK_RANK,
    classify_action_risk,
    max_risk,
    risk_rank,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACI_CONTAINER_NAME",
    "ACI_CONTROL_PORT",
    "ACI_DELETE_WAIT_SECONDS",
    "ACI_GROUP_PREFIX",
    "ACI_POLL_INTERVAL_SECONDS",
    "ACI_STREAM_PORT",
    "ACI_VNC_PASSWORD",
    "ACTION_RISKS",
    "BUTTON_ALIASES",
    "DEFAULT_ACTION_RISK",
    "DEFAULT_BUTTON",
    "DESKTOP_STREAM_CONNECT_TIMEOUT_SECONDS",
    "DESKTOP_STREAM_DEFAULT_ASSET",
    "DESKTOP_STREAM_IDLE_TIMEOUT_SECONDS",
    "DESKTOP_STREAM_TICKET_TTL_SECONDS",
    "DESKTOP_STREAM_WS_UPSTREAM_PATH",
    "K8S_CONTROL_PORT",
    "K8S_STREAM_PORT",
    "MOCK_SCREENSHOT_SIZE",
    "MOCK_WINDOWS",
    "POINT_ARGUMENTS",
    "RISK_KEYWORDS",
    "RISK_ORDER",
    "RISK_RANK",
    "SIDECAR_TOKEN_HEADER",
    "STREAM_PROXY_FORWARDABLE_HEADERS",
    "STREAM_PROXY_RESPONSE_HEADERS",
    "AciStartError",
    "DesktopManager",
    "DesktopStreamTickets",
    "K8sStartError",
    "ScreenGeometry",
    "StreamTicket",
    "StreamTicketError",
    "aci_container_state",
    "aci_events",
    "aci_group_name",
    "aci_group_state",
    "aci_is_running",
    "aci_name_from_id",
    "aci_private_ip",
    "aci_start_failure_reason",
    "classify_action_risk",
    "filter_proxy_request_headers",
    "filter_proxy_response_headers",
    "k8s_pod_phase",
    "k8s_pod_ready",
    "k8s_pvc_name",
    "k8s_resource_name",
    "k8s_start_failure_reason",
    "make_placeholder_png",
    "max_risk",
    "negotiate_stream_subprotocol",
    "normalise_stream_path",
    "normalize_button",
    "parse_window_line",
    "risk_rank",
    "screenshot_image",
    "stream_asset_url",
    "stream_origin",
    "stream_tickets",
    "stream_ws_url",
]

# xdotool takes numeric mouse buttons. Clients send names ("left") or numbers
# ("1") depending on lane, so every action is normalized here in the API rather
# than in the sidecar â€” that way desktop, mobile, worker and routine steps all
# behave the same regardless of which sidecar build is running.
SIDECAR_TOKEN_HEADER = "X-Nesq-Sidecar-Token"

DEFAULT_BUTTON = "1"
BUTTON_ALIASES: dict[str, str] = {
    "left": "1", "l": "1", "1": "1",
    "middle": "2", "m": "2", "2": "2",
    "right": "3", "r": "3", "3": "3",
    "scroll_up": "4", "4": "4",
    "scroll_down": "5", "5": "5",
}

MOCK_SCREENSHOT_SIZE = (320, 200)
MOCK_WINDOWS: list[dict[str, str]] = [
    {"id": "0x02000003", "desktop": "0", "host": "nesq-desktop", "title": "Desktop"},
    {"id": "0x03400005", "desktop": "0", "host": "nesq-desktop", "title": "Chromium â€” Nesq Bot"},
    {"id": "0x03600007", "desktop": "0", "host": "nesq-desktop", "title": "Files"},
]




# ---------------------------------------------------------------------------
# Azure Container Instances
#
# The image contract (infra/bot-desktop/Dockerfile): 6901 is the noVNC stream,
# 7910 is the agent sidecar. The k8s template pins the same two ports and the
# same four environment variables, so an ACI desktop and an AKS desktop present
# an identical surface to the rest of the API.
# ---------------------------------------------------------------------------

ACI_STREAM_PORT = 6901
ACI_CONTROL_PORT = 7910
#: The single container inside each group. ACI wants a DNS-ish label here.
ACI_CONTAINER_NAME = "desktop"
ACI_GROUP_PREFIX = "nesq-desktop"
#: VNC password handed to the image. It is the same constant the docker driver
#: uses, and it is passed as a *secure* value so it is at least absent from
#: `az container show`. That is the honest limit of it: there is no per-bot VNC
#: secret today because nothing on the read side (Tauri, Expo, the noVNC embed)
#: has any way to learn one. The stream is only reachable from the Container
#: Apps VNet, so this is defence in depth rather than the boundary â€” the
#: boundary is the private IP. A per-bot password needs a setting plus a
#: retrieval path on the API, and both are out of this lane.
ACI_VNC_PASSWORD = "nesq"  # noqa: S105 - not a credential to anything but the local X server
#: How often `start`/`resume` re-read the group while waiting for Running.
ACI_POLL_INTERVAL_SECONDS = 3.0
#: How long `stop` waits for ARM to finish the delete before returning anyway.
#: Long enough that the name is usually free for the next start, short enough
#: that a POST /desktop/stop does not hang on Azure.
ACI_DELETE_WAIT_SECONDS = 15.0

#: ARM error codes that mean "ask again shortly", not "this failed".
#:
#: `ContainerGroupTransitioning` is the one that reaches people: ARM refuses a
#: PUT or DELETE while the group is mid-operation, which is exactly the window a
#: person clicking Start right after a Stop lands in. Treated as terminal it put
#: the desktop into `error` and showed the raw Azure sentence to the user, when
#: the correct behaviour was to wait a moment and try again. A retryable
#: condition reported as a failure is a failure we invented.
ACI_RETRYABLE_CODES = frozenset(
    {
        "ContainerGroupTransitioning",
        "TooManyRequests",
        "OperationNotAllowed",
        "ServiceUnavailable",
        "InternalServerError",
    }
)

#: Total time an ACI create/delete may spend being told "not yet".
ACI_TRANSITION_RETRY_SECONDS = 75.0
#: First backoff step; doubles up to `ACI_TRANSITION_RETRY_MAX_SLEEP`.
ACI_TRANSITION_RETRY_BASE_SLEEP = 2.0
ACI_TRANSITION_RETRY_MAX_SLEEP = 12.0

#: Container group names: lowercase alphanumerics and hyphens, 1-63 characters,
#: first and last character alphanumeric.
_ACI_NAME_ILLEGAL = re.compile(r"[^a-z0-9-]+")
_ACI_NAME_RUNS = re.compile(r"-{2,}")

#: Signals in a container's events or detail status that say the *pull* failed
#: rather than the container. Distinguishing the two is the first question an
#: operator asks, so the timeout message answers it up front.
_ACI_PULL_FAILURE_MARKERS = (
    "failed to pull",
    "unauthorized",
    "authentication required",
    "denied",
    "not found",
    "manifest unknown",
    "inspectfailed",
    "errimagepull",
    "imagepullbackoff",
    "registryerror",
)


class AciStartError(RuntimeError):
    """A container group failed to come up.

    Carries the group it left behind so `start` can record it: the desktop is in
    `error`, but the operator still needs a handle to inspect â€” and to stop, since
    a half-started group keeps billing.
    """

    def __init__(self, message: str, container_id: str | None = None) -> None:
        super().__init__(message)
        self.container_id = container_id


def aci_group_name(bot: Any) -> str:
    """Deterministic container group name for one bot.

    Derived from the slug so an operator can find it by eye, suffixed with the
    first 12 hex digits of the bot id so that two slugs which sanitise to the
    same label (``Sales Ops`` and ``sales-ops``) can never land on the same
    group. Reuse across bots is the one thing that would turn per-bot isolation
    back into a competing agent product's shared machine.
    """
    slug = _ACI_NAME_RUNS.sub("-", _ACI_NAME_ILLEGAL.sub("-", str(getattr(bot, "slug", "")).lower()))
    slug = slug.strip("-")[:32].strip("-")
    suffix = _ACI_NAME_ILLEGAL.sub("", str(getattr(bot, "id", "")).lower())[:12]
    parts = [part for part in (ACI_GROUP_PREFIX, slug, suffix) if part]
    return "-".join(parts)[:63].strip("-")


def aci_name_from_id(container_id: Any) -> str:
    """The group name out of whatever `container_id` holds â€” ARM id or bare name."""
    return str(container_id or "").rstrip("/").rsplit("/", 1)[-1]


def aci_group_state(group: Any) -> str:
    """`instanceView.state` of the group: Pending / Running / Stopped / Succeeded."""
    view = getattr(group, "instance_view", None)
    return str(getattr(view, "state", "") or "")


def aci_container_state(group: Any) -> tuple[str, str]:
    """`(state, detailStatus)` of the desktop container, or `("", "")`."""
    containers = list(getattr(group, "containers", None) or [])
    if not containers:
        return ("", "")
    current = getattr(getattr(containers[0], "instance_view", None), "current_state", None)
    return (
        str(getattr(current, "state", "") or ""),
        str(getattr(current, "detail_status", "") or ""),
    )


def aci_events(group: Any) -> list[str]:
    """The container's event log flattened to `"Name: message"` strings."""
    containers = list(getattr(group, "containers", None) or [])
    if not containers:
        return []
    view = getattr(containers[0], "instance_view", None)
    return [
        f"{getattr(event, 'name', '') or ''}: {getattr(event, 'message', '') or ''}".strip(": ")
        for event in (getattr(view, "events", None) or [])
    ]


#: Addresses ARM reports while a VNet-injected group is still being placed.
#: They are *bind* addresses, not destinations, and treating one as a real
#: answer is how the API ended up storing `http://0.0.0.0:6901` as a bot's
#: stream URL: dialling 0.0.0.0 from a container means "this host", so every
#: screenshot and every stream attempt hit the API itself, where nothing
#: listens on 6901, and surfaced as "the bot desktop stream is not reachable".
#: The row was written nine seconds before the container even started.
ACI_PLACEHOLDER_IPS = frozenset({"0.0.0.0", "::", "::0", "0:0:0:0:0:0:0:0"})  # noqa: S104


def aci_error_code(exc: BaseException) -> str:
    """The ARM error code on an exception, or `""`.

    Read structurally where the SDK offers it (`exc.error.code`), and only fall
    back to matching the rendered message when it does not — the message is
    prose and prose gets reworded, so it is the last resort rather than the
    first.
    """
    code = getattr(getattr(exc, "error", None), "code", None)
    if isinstance(code, str) and code:
        return code
    text = str(exc)
    for known in ACI_RETRYABLE_CODES:
        if known in text:
            return known
    return ""


def _aci_retrying(
    call: Callable[[], Any],
    *,
    what: str,
    budget_seconds: float = ACI_TRANSITION_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Any:
    """Run an ARM call, waiting out the conditions that mean "not yet".

    ARM refuses a PUT or DELETE while a container group is mid-operation, with
    `ContainerGroupTransitioning`. That is the ordinary consequence of pressing
    Start shortly after Stop, and it used to reach the person as a raw Azure
    sentence with the desktop parked in `error` — a failure we invented out of a
    condition that resolves itself in seconds.

    Anything not in `ACI_RETRYABLE_CODES` is raised immediately: retrying a real
    error just delays the diagnosis. `sleep`/`now` are injectable so the tests
    can prove the backoff without spending the wall-clock on it.
    """
    deadline = now() + budget_seconds
    delay = ACI_TRANSITION_RETRY_BASE_SLEEP
    attempt = 0
    while True:
        attempt += 1
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            code = aci_error_code(exc)
            remaining = deadline - now()
            if code not in ACI_RETRYABLE_CODES or remaining <= 0:
                if code in ACI_RETRYABLE_CODES:
                    logger.warning(
                        "aci %s still %s after %.0fs and %d attempts", what, code,
                        budget_seconds, attempt,
                    )
                raise
            logger.info(
                "aci %s: %s, retrying in %.0fs (attempt %d)", what, code, delay, attempt
            )
            sleep(min(delay, remaining))
            delay = min(delay * 2, ACI_TRANSITION_RETRY_MAX_SLEEP)


def aci_private_ip(group: Any) -> str:
    """The group's routable private address, or `""` while it has none.

    A placeholder counts as "none" so the start poll keeps waiting rather than
    banking an address that cannot be dialled.
    """
    ip = str(getattr(getattr(group, "ip_address", None), "ip", "") or "").strip()
    return "" if ip in ACI_PLACEHOLDER_IPS else ip


def aci_is_running(group: Any) -> bool:
    """True once either the group or its container reports Running.

    Both are checked because the group-level instance view lags the container's
    by a poll or two, and older API versions omit one of them.
    """
    states = {aci_group_state(group).lower(), aci_container_state(group)[0].lower()}
    return "running" in states


def aci_start_failure_reason(group: Any, image: str, seconds: float) -> str:
    """Name the likely cause of a start that never reached Running.

    "Timed out" is not an answer an operator can act on; the next question is
    always whether the image is still pulling, the pull was refused, or the
    container came up and died. The group's events and instance view can tell
    those apart, so this says which one it looks like.
    """
    waited = f"{seconds:.0f}s"
    if group is None:
        return (
            f"Azure never returned the container group after {waited} â€” the create call was "
            "accepted but nothing was scheduled. Look at subnet address space and the region's "
            "container group quota before looking at the image."
        )

    state, detail = aci_container_state(group)
    events = aci_events(group)
    haystack = " | ".join([detail, *events]).lower()

    if any(marker in haystack for marker in _ACI_PULL_FAILURE_MARKERS):
        evidence = detail or (events[-1] if events else "see the container group events")
        return (
            f"the image pull was refused ({evidence}) â€” "
            f"check that {image} exists at that exact tag and that the user-assigned identity "
            "in aci_registry_identity holds AcrPull on the registry"
        )
    if state.lower() == "terminated":
        return (
            f"the container started and then exited ({detail or 'no detail status'}) â€” this is "
            "the desktop failing, not the pull; `az container logs` has the entrypoint output"
        )
    if aci_is_running(group) and not aci_private_ip(group):
        return (
            "the container is Running but the group still has no private IP â€” check that the "
            "subnet in aci_subnet_id is delegated to Microsoft.ContainerInstance and has free "
            "addresses"
        )
    if "pulling" in haystack:
        return (
            f"still pulling {image} after {waited} â€” the pull started and did not finish, so "
            "either raise aci_start_timeout_seconds for a cold image or check the registry is "
            "reachable from the delegated subnet"
        )
    if not state and not events:
        return (
            f"the group exists but no container event has been recorded after {waited} â€” Azure "
            "has not placed it yet, which points at subnet capacity or quota rather than at the "
            "image"
        )
    return (
        f"group state={aci_group_state(group) or 'unknown'}, container state={state or 'unknown'}"
        f"{f' ({detail})' if detail else ''} after {waited}"
        f"{'; last event: ' + events[-1] if events else '; no container events'}"
    )


# ---------------------------------------------------------------------------
# Generic self-hosted Kubernetes
#
# One Pod per bot against whatever cluster the kubeconfig points at - k3s,
# kind, EKS, GKE, bare metal, anything. This is deliberately not the `aks`
# path above: `aks` is a static template a human `sed`s and `kubectl apply`s
# out of band (infra/bot-desktop/k8s/desktop-template.yaml), meant for a
# managed AKS deployment with its own ServiceAccount/NetworkPolicy/PDB. `k8s`
# is API-driven, like `docker` and `aci`: the API creates and deletes a Pod,
# a Service and (when persistence is configured) a PersistentVolumeClaim at
# runtime through the standard `kubernetes` client, and never touches Azure
# APIs at all.
#
# Ports and the environment variable contract match `aci` exactly (BOT_SLUG,
# DESKTOP_PROFILE, VNC_PW, NESQ_STREAM_PORT, NESQ_SIDECAR_PORT, and
# NESQ_SIDECAR_TOKEN when configured) so the same bot-desktop image serves all
# three backends unmodified.
# ---------------------------------------------------------------------------

K8S_STREAM_PORT = ACI_STREAM_PORT
K8S_CONTROL_PORT = ACI_CONTROL_PORT
K8S_POD_PREFIX = "nesq-desktop"
#: Same constant `docker` and `aci` pass. See `ACI_VNC_PASSWORD` for the honest
#: limit of what this protects - it is defence in depth, not a boundary.
K8S_VNC_PASSWORD = ACI_VNC_PASSWORD
#: How often `start`/`resume` re-read the pod while waiting for Ready.
K8S_POLL_INTERVAL_SECONDS = 3.0
#: How long `stop` waits for the delete to be acknowledged before returning
#: anyway - a stop request must not hang on a slow API server.
K8S_DELETE_WAIT_SECONDS = 15.0

#: Waiting-state reasons that mean "still pulling", not "this failed".
_K8S_PULL_WAITING_REASONS = frozenset({"ContainerCreating", "PodInitializing"})
#: Waiting-state reasons that are a genuine, actionable failure.
_K8S_PULL_FAILURE_REASONS = frozenset(
    {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}
)


class K8sStartError(RuntimeError):
    """A Pod failed to reach Ready.

    Carries the pod name as `container_id` - the same attribute name
    `AciStartError` uses - so `start`'s generic `except Exception` handler
    (`getattr(exc, "container_id", None)`) records it without a mode-specific
    branch: the desktop lands in `error`, but an operator still needs a handle
    to inspect (`kubectl -n <namespace> describe pod <name>`) and to clean up.
    """

    def __init__(self, message: str, container_id: str | None = None) -> None:
        super().__init__(message)
        self.container_id = container_id


def _k8s_short_id(bot: Any) -> str:
    """First 12 hex digits of the bot id, for names that must survive a slug rename."""
    return _ACI_NAME_ILLEGAL.sub("", str(getattr(bot, "id", "")).lower())[:12]


def k8s_resource_name(bot: Any) -> str:
    """Deterministic Pod/Service name for one bot - a valid DNS-1123 label.

    Same sanitisation as `aci_group_name`: k8s object names follow the exact
    same rule ACI container group names do (lowercase alphanumerics and
    hyphens, 1-63 characters, first and last character alphanumeric), so reuse
    rather than reinvent it.
    """
    slug = _ACI_NAME_RUNS.sub("-", _ACI_NAME_ILLEGAL.sub("-", str(getattr(bot, "slug", "")).lower()))
    slug = slug.strip("-")[:32].strip("-")
    suffix = _k8s_short_id(bot)
    parts = [part for part in (K8S_POD_PREFIX, slug, suffix) if part]
    return "-".join(parts)[:63].strip("-")


def k8s_pvc_name(bot: Any) -> str:
    """Deterministic PVC name, keyed on bot id alone so a slug rename does not
    orphan the volume and start a fresh, empty home."""
    return f"{K8S_POD_PREFIX}-home-{_k8s_short_id(bot)}"[:63]


def k8s_pod_phase(pod: Any) -> str:
    """`status.phase`: Pending / Running / Succeeded / Failed / Unknown."""
    return str(getattr(getattr(pod, "status", None), "phase", "") or "")


def k8s_pod_ready(pod: Any) -> bool:
    """True once the pod's `Ready` condition is `True`.

    `phase == "Running"` alone is not enough: a container can be Running and
    still failing its readiness probe (the desktop's `/health` is not up yet),
    and starting to proxy traffic at that point would just 502.
    """
    conditions = list(getattr(getattr(pod, "status", None), "conditions", None) or [])
    return any(
        str(getattr(c, "type", "")) == "Ready" and str(getattr(c, "status", "")) == "True"
        for c in conditions
    )


def k8s_container_waiting_reason(pod: Any) -> str | None:
    """`reason` of the desktop container's `waiting` state, or `None`."""
    statuses = list(getattr(getattr(pod, "status", None), "container_statuses", None) or [])
    if not statuses:
        return None
    waiting = getattr(statuses[0].state, "waiting", None) if hasattr(statuses[0], "state") else None
    return str(getattr(waiting, "reason", "")) if waiting is not None else None


def k8s_start_failure_reason(pod: Any, image: str, seconds: float) -> str:
    """Name the likely cause of a start that never reached Ready.

    Mirrors `aci_start_failure_reason`: an operator's first question is always
    whether the image is still pulling, the pull was refused, or the container
    came up and crashed, and the pod's own status can tell those apart.
    """
    waited = f"{seconds:.0f}s"
    if pod is None:
        return (
            f"the API server never returned the pod after {waited} - the create call was "
            "accepted but nothing was scheduled. Check `kubectl get events` for a scheduling "
            "failure (insufficient CPU/memory, no node matches, PVC not binding) before "
            "looking at the image."
        )

    reason = k8s_container_waiting_reason(pod)
    if reason in _K8S_PULL_FAILURE_REASONS:
        return (
            f"the image pull was refused ({reason}) - check that {image} exists at that exact "
            "tag and, if the registry is private, that k8s_image_pull_secret names a valid "
            "imagePullSecret in the target namespace"
        )
    phase = k8s_pod_phase(pod)
    if phase == "Failed":
        return (
            "the pod reached phase Failed - this is the desktop crashing, not the pull; "
            "`kubectl -n <namespace> logs <pod>` has the entrypoint output"
        )
    if phase == "Running" and not k8s_pod_ready(pod):
        return (
            f"the pod is Running but never passed its readiness probe after {waited} - the "
            "desktop's /health endpoint on the control port is not answering; check "
            "`kubectl -n <namespace> logs <pod>` for the sidecar's own startup errors"
        )
    if reason in _K8S_PULL_WAITING_REASONS or phase == "Pending":
        return (
            f"still {reason or 'Pending'} after {waited} - either raise "
            "k8s_start_timeout_seconds for a cold image pull, or check the node has room to "
            "schedule the pod (`kubectl get events`)"
        )
    return f"phase={phase or 'unknown'}, container waiting reason={reason or 'none'} after {waited}"


def normalize_button(value: Any) -> str:
    """Map a mouse button name or number onto the numeric form xdotool wants.

    Unrecognized input falls back to the primary button rather than being passed
    through to a shell command.
    """
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return BUTTON_ALIASES.get(key, DEFAULT_BUTTON)


def parse_window_line(line: Any) -> dict[str, str]:
    """Turn a `wmctrl -l` line into a dict; pass dicts through untouched.

    `0x03400005  0 nesq-desktop Chromium â€” Nesq Bot`
    """
    if isinstance(line, dict):
        return {str(k): str(v) for k, v in line.items()}
    parts = str(line).split(None, 3)
    keys = ("id", "desktop", "host", "title")
    window = dict(zip(keys, parts, strict=False))
    for key in keys:
        window.setdefault(key, "")
    return window


# ---------------------------------------------------------------------------
# Screen geometry â€” the picture the model saw vs the desktop it is clicking on
# ---------------------------------------------------------------------------
#
# The sidecar can crop and downscale a capture (`?max_width=1024`), which is
# what keeps a vision loop's payloads small: a full-screen PNG is ~1.5 MB of
# base64 and 1105 prompt tokens at 1280x800, and the same frame as JPEG q70 at
# 1024px wide is a small fraction of the bytes and 765 tokens.
#
# It also silently changes the coordinate system. A model handed a 1024x640
# image reports the search box at (405, 359); xdotool, which is looking at a
# 1280x800 root window, would put the pointer 20% up and to the left of it. A
# click that lands somewhere else is a far worse failure than a slow loop, so
# the mapping is explicit, unit-tested, and applied at exactly one place: the
# orchestrator, before the action becomes an `Effect`. Everything downstream â€”
# the approval a human reads, the undo log, the sidecar â€” therefore sees true
# desktop pixels and never the model's scaled view.

#: Argument names that carry a point in the image's coordinate space. `x`/`y`
#: for every pointer primitive, `to_x`/`to_y` for `drag`'s drop target. Anything
#: not named here (`amount`, `steps`, `button`) is left alone.
POINT_ARGUMENTS: tuple[tuple[str, str], ...] = (("x", "y"), ("to_x", "to_y"))


@dataclass(frozen=True)
class ScreenGeometry:
    """How a captured image maps back onto the real desktop.

    Built from a `/screenshot` response. The identity default is deliberate: a
    caller with no screenshot in hand (the opening move of a turn, a routine
    step, a replayed approval) must pass coordinates through untouched.
    """

    #: Size of the image the model was given.
    image_width: int = 0
    image_height: int = 0
    #: Size of the real desktop behind it. Zero when the sidecar did not say,
    #: in which case no clamping is applied.
    screen_width: int = 0
    screen_height: int = 0
    #: Top-left of the crop region within the desktop, in true pixels.
    offset_x: int = 0
    offset_y: int = 0
    #: image pixels per source pixel, per axis. 1.0 is "no rescale".
    scale_x: float = 1.0
    scale_y: float = 1.0

    @property
    def is_identity(self) -> bool:
        return (
            self.scale_x == 1.0
            and self.scale_y == 1.0
            and self.offset_x == 0
            and self.offset_y == 0
        )

    @classmethod
    def from_screenshot(cls, result: Any) -> ScreenGeometry:
        """Read the geometry out of a `/screenshot` payload.

        Derived from the *dimensions* rather than the sidecar's `scale` float:
        `scale` is rounded to four decimals for readability, and the width
        ratio is exact. `scale` is only consulted when the dimensions are
        missing, and a payload with neither yields the identity â€” an unknown
        rescale must never be guessed at.
        """
        if not isinstance(result, dict) or not result.get("ok"):
            return cls()

        image_w = _positive_int(result.get("width"))
        image_h = _positive_int(result.get("height"))
        screen_w = _positive_int(result.get("screen_width"))
        screen_h = _positive_int(result.get("screen_height"))

        region = result.get("region")
        if isinstance(region, dict):
            offset_x = max(_positive_int(region.get("x")), 0)
            offset_y = max(_positive_int(region.get("y")), 0)
            source_w = _positive_int(region.get("w")) or screen_w or image_w
            source_h = _positive_int(region.get("h")) or screen_h or image_h
        else:
            offset_x = offset_y = 0
            source_w = screen_w or image_w
            source_h = screen_h or image_h

        reported = result.get("scale")
        fallback = float(reported) if isinstance(reported, (int, float)) and reported > 0 else 1.0
        scale_x = (image_w / source_w) if image_w and source_w else fallback
        scale_y = (image_h / source_h) if image_h and source_h else fallback

        return cls(
            image_width=image_w,
            image_height=image_h,
            screen_width=screen_w,
            screen_height=screen_h,
            offset_x=offset_x,
            offset_y=offset_y,
            scale_x=scale_x or 1.0,
            scale_y=scale_y or 1.0,
        )

    def to_screen(self, x: Any, y: Any) -> tuple[int, int]:
        """One point, from image space to true desktop pixels.

        Pixel *centres* are mapped, not corners: image pixel `n` covers source
        pixels `[n/s, (n+1)/s)`, and its centre is `(n + 0.5)/s - 0.5`. Naive
        `n/s` biases every coordinate towards the top-left by half a scaled
        pixel, which at a 4x downscale is two real pixels of drift on every
        click. The result is clamped to the desktop when its size is known, so
        rounding at the far edge cannot produce an off-screen point.
        """
        screen_x = self.offset_x + ((int(x) + 0.5) / self.scale_x - 0.5)
        screen_y = self.offset_y + ((int(y) + 0.5) / self.scale_y - 0.5)
        return (
            _clamp(round(screen_x), self.screen_width),
            _clamp(round(screen_y), self.screen_height),
        )

    def to_screen_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """A copy of one action's arguments with its points mapped.

        Returns the input unchanged under the identity, so the common
        no-downscale path costs nothing and reads as a no-op in the audit log.
        """
        if self.is_identity or not arguments:
            return dict(arguments or {})
        mapped = dict(arguments)
        for name_x, name_y in POINT_ARGUMENTS:
            if not _is_number(mapped.get(name_x)) or not _is_number(mapped.get(name_y)):
                continue
            mapped[name_x], mapped[name_y] = self.to_screen(mapped[name_x], mapped[name_y])
        return mapped


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _clamp(value: int, limit: int) -> int:
    if value < 0:
        return 0
    if limit and value > limit - 1:
        return limit - 1
    return value


def screenshot_image(result: Any) -> tuple[str, str]:
    """`(base64, mime)` for a `/screenshot` payload, whatever format it used.

    The sidecar keeps `png_base64` for PNG because docs/API.md pins it, and
    answers with `image_base64` + `mime` for JPEG. One reader for both, so a
    caller that switches format does not silently start sending no image at
    all â€” which is what "the screenshot key moved" looks like from inside a
    vision loop.
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return "", ""
    payload = result.get("image_base64") or result.get("png_base64") or ""
    mime = str(result.get("mime") or "")
    if not mime:
        mime = "image/png" if result.get("png_base64") else "image/jpeg"
    return str(payload), mime


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def make_placeholder_png(width: int, height: int) -> bytes:
    """Build a small RGB PNG in-process â€” no Pillow dependency in the API."""
    # Banded diagonal gradient â€” visibly "a screen", and it compresses to a
    # couple of KB instead of the ~100 KB a smooth gradient would cost.
    bands = 16
    rows = bytearray()
    for y in range(height):
        green = ((y * bands) // height) * (256 // bands)
        row = bytearray()
        for x in range(width):
            red = ((x * bands) // width) * (256 // bands)
            row += bytes((red, green, 120))
        rows.append(0)  # filter type 0 (None)
        rows += row
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


class DesktopManager:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def get(self, db: AsyncSession, bot_id: uuid.UUID) -> BotDesktop:
        row = await db.get(BotDesktop, bot_id)
        if row:
            return row
        row = BotDesktop(bot_id=bot_id, state="absent")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    async def start(self, db: AsyncSession, bot: Bot) -> BotDesktop:
        desktop = await self.get(db, bot.id)
        if desktop.state == "running":
            return desktop

        desktop.state = "starting"
        desktop.last_error = None
        await db.commit()

        try:
            if self.settings.bot_desktop_mode == "mock":
                desktop.state = "running"
                desktop.container_id = f"mock-{bot.slug}"
                desktop.stream_url = f"{self.settings.bot_desktop_stream_base}/?bot={bot.slug}"
                desktop.control_url = f"http://mock-control/{bot.slug}"
            elif self.settings.bot_desktop_mode == "docker":
                info = await asyncio.to_thread(self._docker_start, bot)
                desktop.state = "running"
                desktop.container_id = info["container_id"]
                desktop.stream_url = info["stream_url"]
                desktop.control_url = info["control_url"]
            elif self.settings.bot_desktop_mode == "aci":
                info = await self._aci_start(bot)
                desktop.state = "running"
                desktop.container_id = info["container_id"]
                desktop.stream_url = info["stream_url"]
                desktop.control_url = info["control_url"]
            elif self.settings.bot_desktop_mode == "k8s":
                info = await self._k8s_start(bot)
                desktop.state = "running"
                desktop.container_id = info["container_id"]
                desktop.stream_url = info["stream_url"]
                desktop.control_url = info["control_url"]
            else:
                # aks mode â€” worker creates pod; API records pending then worker updates
                desktop.state = "starting"
                desktop.stream_url = None
                desktop.control_url = None
                desktop.container_id = f"aks-pending-{bot.id}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("desktop start failed")
            desktop.state = "error"
            desktop.last_error = str(exc)
            # A failed ACI start still leaves a container group behind, on purpose:
            # deleting it would delete the evidence. Record the handle so `stop`
            # can clean it up â€” it bills until something does.
            leftover = getattr(exc, "container_id", None)
            if leftover:
                desktop.container_id = str(leftover)

        await db.commit()
        await db.refresh(desktop)
        return desktop

    def _docker_client(self):
        """Docker client honouring BOT_DESKTOP_DOCKER_HOST, else the environment."""
        import docker

        host = (self.settings.bot_desktop_docker_host or "").strip()
        if host:
            return docker.DockerClient(base_url=host)
        return docker.from_env()

    def _sidecar_headers(self) -> dict[str, str]:
        """Auth header for the sidecar, omitted entirely when no token is set."""
        token = (self.settings.nesq_sidecar_token or "").strip()
        return {SIDECAR_TOKEN_HEADER: token} if token else {}

    def _docker_start(self, bot: Bot) -> dict[str, str]:
        client = self._docker_client()
        home = Path(self.settings.bot_desktop_home_root) / str(bot.id)
        home.mkdir(parents=True, exist_ok=True)

        name = f"nesq-desktop-{bot.slug}"
        # Remove stale container with same name
        try:
            old = client.containers.get(name)
            old.remove(force=True)
        except Exception:  # noqa: BLE001
            pass

        # Host port allocation: hash slug into 6901-6999
        port = 6901 + (sum(ord(c) for c in bot.slug) % 90)
        control_port = 7901 + (sum(ord(c) for c in bot.slug) % 90)

        container = client.containers.run(
            self.settings.bot_desktop_image,
            name=name,
            detach=True,
            environment={
                "BOT_SLUG": bot.slug,
                "VNC_PW": "nesq",
                "DESKTOP_PROFILE": bot.desktop_profile,
            },
            volumes={str(home.resolve()): {"bind": "/home/nesq", "mode": "rw"}},
            ports={"6901/tcp": port, "7910/tcp": control_port},
            shm_size="512m",
            network=self.settings.bot_desktop_network if self._network_exists(client) else None,
            labels={"nesqbot.bot_id": str(bot.id), "nesqbot.role": "bot-desktop"},
        )
        return {
            "container_id": container.id,
            "stream_url": f"http://localhost:{port}",
            "control_url": f"http://localhost:{control_port}",
        }

    def _network_exists(self, client) -> bool:
        try:
            client.networks.get(self.settings.bot_desktop_network)
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- Azure Container Instances ------------------------------------------
    #
    # The SDK is imported inside the methods, exactly like `docker` above, so a
    # mock or docker deployment never loads azure-mgmt-containerinstance.

    def _aci_client(self):
        """Management client for the configured subscription, via managed identity.

        Uses `ManagedIdentityCredential` with an explicit `client_id`, never bare
        `DefaultAzureCredential`. `DefaultAzureCredential` reads the ambient
        `AZURE_CLIENT_ID`, which on this deployment is the **Entra API app
        registration** - the audience the API accepts on a user token - and not a
        managed identity at all. Handed that, the IMDS probe fails with
        `invalid_scope` and the whole chain then falls through Environment,
        SharedTokenCache, AzureCli, PowerShell and azd before surfacing a wall of
        text that names every credential except the real problem. That is what
        "Desktop error: DefaultAzureCredential failed to retrieve a token" was.

        Falls back to `DefaultAzureCredential` only when no managed-identity client
        id is configured, which is the local `az login` path.
        """
        from azure.mgmt.containerinstance import ContainerInstanceManagementClient

        client_id = (self.settings.azure_managed_identity_client_id or "").strip()
        if client_id:
            from azure.identity import ManagedIdentityCredential

            credential = ManagedIdentityCredential(client_id=client_id)
            logger.info("aci management client using managed identity %s", client_id)
        else:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
            logger.info("aci management client using DefaultAzureCredential (no managed identity configured)")

        return ContainerInstanceManagementClient(
            credential, str(self.settings.aci_subscription_id).strip()
        )

    def _aci_require_config(self) -> None:
        """Refuse to start rather than start something unsafe or unreachable."""
        settings = self.settings
        missing = [
            name
            for name, value in (
                ("aci_subscription_id", settings.aci_subscription_id),
                ("aci_resource_group", settings.aci_resource_group),
                ("aci_region", settings.aci_region),
                ("bot_desktop_image", settings.bot_desktop_image),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise AciStartError(f"aci mode is not configured: {', '.join(missing)} empty")

        if not str(settings.aci_subnet_id or "").strip():
            # A container group with no subnet gets a public IP. A Bot Desktop is a
            # real browser driven by an LLM over hostile content; it is never allowed
            # to be reachable from the internet, so an unset subnet is a hard stop and
            # not a fallback.
            raise AciStartError(
                "aci_subnet_id is empty. A container group without a delegated subnet is "
                "given a public IP, and a Bot Desktop must only be reachable from the "
                "Container Apps VNet â€” refusing to start."
            )

        if str(settings.aci_registry_server or "").strip() and not str(
            settings.aci_registry_identity or ""
        ).strip():
            raise AciStartError(
                f"aci_registry_server is set ({settings.aci_registry_server}) but "
                "aci_registry_identity is empty. The desktop image is pulled with a "
                "user-assigned identity holding AcrPull; there is no admin "
                "username/password path in this driver."
            )

    def _sidecar_secure_env(self) -> list:
        """The sidecar shared secret as a *secure* ACI environment variable.

        `secure_value` is write-only in ARM: it is absent from `az container show`,
        from the group's ARM history, and from every read of the group we make while
        polling. That is the only reason the token is allowed into a container group
        definition at all.
        """
        from azure.mgmt.containerinstance import models as aci

        token = (self.settings.nesq_sidecar_token or "").strip()
        if not token:
            logger.warning(
                "NESQ_SIDECAR_TOKEN is empty â€” the desktop control plane on %s will accept "
                "any caller that can reach the private IP",
                ACI_CONTROL_PORT,
            )
            return []
        return [aci.EnvironmentVariable(name="NESQ_SIDECAR_TOKEN", secure_value=token)]

    def _aci_registry_credentials(self) -> list | None:
        """ACR pull via the user-assigned identity â€” never a username/password."""
        from azure.mgmt.containerinstance import models as aci

        server = str(self.settings.aci_registry_server or "").strip()
        identity = str(self.settings.aci_registry_identity or "").strip()
        if not server:
            return None
        entry = aci.ImageRegistryCredential(server=server, identity=identity)
        return [entry]

    def _aci_identity(self):
        """Attach the user-assigned identity the group pulls (and authenticates) with."""
        from azure.mgmt.containerinstance import models as aci

        identity = str(self.settings.aci_registry_identity or "").strip()
        if not identity:
            return None
        # The key is the identity's full ARM resource id; the value is an empty
        # object that ARM fills in with principalId/clientId.
        return aci.ContainerGroupIdentity(
            type="UserAssigned", user_assigned_identities={identity: {}}
        )

    def _aci_container_group(self, bot: Bot, name: str):
        """The container group definition for one bot.

        One container, two ports, a private address in the delegated subnet, the
        pull identity, and the same four environment variables the image documents.
        No volume is mounted: see `stop` for what that costs.
        """
        from azure.mgmt.containerinstance import models as aci

        settings = self.settings
        ports = (ACI_STREAM_PORT, ACI_CONTROL_PORT)
        image = str(settings.bot_desktop_image)
        registry = str(settings.aci_registry_server or "").strip()
        if registry and not image.startswith(f"{registry}/"):
            logger.warning(
                "bot_desktop_image %s is not under aci_registry_server %s â€” the pull "
                "identity will not be used for it",
                image,
                registry,
            )

        container = aci.Container(
            name=ACI_CONTAINER_NAME,
            image=image,
            resources=aci.ResourceRequirements(
                requests=aci.ResourceRequests(
                    cpu=float(settings.aci_cpu),
                    memory_in_gb=float(settings.aci_memory_gb),
                )
            ),
            ports=[aci.ContainerPort(port=port, protocol="TCP") for port in ports],
            environment_variables=[
                aci.EnvironmentVariable(name="BOT_SLUG", value=bot.slug),
                aci.EnvironmentVariable(
                    name="DESKTOP_PROFILE", value=bot.desktop_profile or "icewm"
                ),
                aci.EnvironmentVariable(name="VNC_PW", secure_value=ACI_VNC_PASSWORD),
                aci.EnvironmentVariable(name="NESQ_STREAM_PORT", value=str(ACI_STREAM_PORT)),
                aci.EnvironmentVariable(name="NESQ_SIDECAR_PORT", value=str(ACI_CONTROL_PORT)),
                *self._sidecar_secure_env(),
            ],
        )
        group = aci.ContainerGroup(
            location=str(settings.aci_region),
            os_type="Linux",
            # Matches the AKS Deployment this replaces: the entrypoint supervises the
            # X session itself, so only a wedged container should ever be recycled.
            restart_policy="Always",
            # VNet-injected groups must be Standard.
            sku="Standard",
            containers=[container],
            # type="Private" plus a subnet is what keeps this off the internet. There
            # is deliberately no dns_name_label: that is a public-IP-only field.
            ip_address=aci.IpAddress(
                type="Private",
                ports=[aci.Port(port=port, protocol="TCP") for port in ports],
            ),
            subnet_ids=[aci.ContainerGroupSubnetId(id=str(settings.aci_subnet_id).strip())],
            identity=self._aci_identity(),
            image_registry_credentials=self._aci_registry_credentials(),
            tags={
                "nesqbot.bot_id": str(bot.id),
                "nesqbot.bot_slug": str(bot.slug),
                "nesqbot.role": "bot-desktop",
                "nesqbot.group": name,
            },
        )
        return group

    async def _aci_start(self, bot: Bot) -> dict[str, str]:
        """Create this bot's container group and wait for it to serve."""
        self._aci_require_config()
        name = aci_group_name(bot)
        client = await asyncio.to_thread(self._aci_client)
        definition = self._aci_container_group(bot, name)
        await asyncio.to_thread(self._aci_create, client, name, definition)
        return await self._aci_wait_for_running(client, name)

    def _aci_create(self, client, name: str, definition) -> None:
        # `begin_create_or_update` issues the PUT before it returns. We deliberately
        # do not block on the ARM poller: the timeout that matters is ours, and so is
        # the diagnosis when it expires.
        _aci_retrying(
            lambda: client.container_groups.begin_create_or_update(
                str(self.settings.aci_resource_group), name, definition
            ),
            what=f"create {name}",
        )

    async def _aci_wait_for_running(self, client, name: str) -> dict[str, str]:
        """Poll the group until it serves on a private IP, or give up with a reason.

        Cold-pulling the desktop image is 30-90s, so this is the normal path and not
        an error path. `aci_start_timeout_seconds` bounds it.
        """
        budget = max(float(self.settings.aci_start_timeout_seconds), 0.0)
        resource_group = str(self.settings.aci_resource_group)
        started = time.monotonic()
        deadline = started + budget
        group = None
        while True:
            try:
                group = await asyncio.to_thread(client.container_groups.get, resource_group, name)
            except Exception as exc:  # noqa: BLE001 - a 404 right after the PUT is normal
                logger.debug("aci group %s not readable yet: %s", name, exc)
                group = None

            if group is not None:
                ip = aci_private_ip(group)
                if ip and aci_is_running(group):
                    return {
                        "container_id": str(getattr(group, "id", "") or name),
                        "stream_url": f"http://{ip}:{ACI_STREAM_PORT}",
                        "control_url": f"http://{ip}:{ACI_CONTROL_PORT}",
                    }

            if time.monotonic() >= deadline:
                waited = time.monotonic() - started
                raise AciStartError(
                    f"container group {name} did not serve within {budget:.0f}s: "
                    f"{aci_start_failure_reason(group, str(self.settings.bot_desktop_image), waited)}."
                    f" It was left in place for diagnosis (az container show -g {resource_group}"
                    f" -n {name}) and bills until it is stopped.",
                    container_id=str(getattr(group, "id", "") or name),
                )
            await asyncio.sleep(ACI_POLL_INTERVAL_SECONDS)

    def _aci_delete(self, container_id: str) -> None:
        """Delete the group. This is what `stop` means on ACI â€” and it is a wipe."""
        name = aci_name_from_id(container_id)
        try:
            client = self._aci_client()
            poller = _aci_retrying(
                lambda: client.container_groups.begin_delete(
                    str(self.settings.aci_resource_group), name
                ),
                what=f"delete {name}",
            )
        except Exception:  # noqa: BLE001 - teardown never fails a request
            logger.warning("aci group delete failed for %s", name)
            return
        wait = getattr(poller, "wait", None)
        if wait is None:
            return
        try:
            # Bounded: a name still in Deleting would collide with this bot's next
            # start, but a stop request must not hang on ARM either.
            wait(timeout=ACI_DELETE_WAIT_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("aci group %s is still deleting after %ss", name, ACI_DELETE_WAIT_SECONDS)

    def _aci_stop_group(self, container_id: str) -> None:
        """ACI stop: the definition survives, the compute (and the billing) does not."""
        client = self._aci_client()
        client.container_groups.stop(
            str(self.settings.aci_resource_group), aci_name_from_id(container_id)
        )

    async def _aci_resume(self, container_id: str) -> dict[str, str]:
        """ACI start: a cold boot of the stored definition, on a possibly new IP."""
        name = aci_name_from_id(container_id)
        client = await asyncio.to_thread(self._aci_client)
        await asyncio.to_thread(
            client.container_groups.begin_start, str(self.settings.aci_resource_group), name
        )
        return await self._aci_wait_for_running(client, name)

    # -- Generic self-hosted Kubernetes --------------------------------------
    #
    # `kubernetes` is imported inside the methods, exactly like `docker` and
    # the Azure SDK above, so a mock/docker/aci deployment never loads it.

    def _k8s_client(self):
        """A `CoreV1Api` for the configured cluster.

        `k8s_kubeconfig_path` set -> that file (and `k8s_context`, if also
        set). Otherwise: try in-cluster config first (the API itself running
        as a pod in the cluster it manages desktops in - the common
        self-hosted layout), and fall back to the default kubeconfig
        (`~/.kube/config`) for the local `k3s`/`kind` dev path.
        """
        from kubernetes import client, config

        path = (self.settings.k8s_kubeconfig_path or "").strip()
        if path:
            config.load_kube_config(config_file=path, context=self.settings.k8s_context or None)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config(context=self.settings.k8s_context or None)
        return client.CoreV1Api()

    def _k8s_require_config(self) -> None:
        """Refuse to start rather than start something unreachable."""
        settings = self.settings
        if not str(settings.bot_desktop_image or "").strip():
            raise K8sStartError("k8s mode is not configured: bot_desktop_image is empty")
        if not str(settings.k8s_namespace or "").strip():
            raise K8sStartError("k8s mode is not configured: k8s_namespace is empty")
        if settings.k8s_service_type == "NodePort" and not str(settings.k8s_public_host or "").strip():
            # A ClusterIP-only URL is unusable from outside the cluster. Rather than
            # hand back a Service address nothing can reach, refuse up front - the
            # same "fail loud, not silently unreachable" call `aci_subnet_id` makes.
            raise K8sStartError(
                "k8s_service_type is NodePort but k8s_public_host is empty - refusing to start "
                "a desktop whose stream/control URLs would be unreachable"
            )

    def _k8s_home_volume(self, bot: Bot):
        """The `home` volume source: a PVC when `k8s_storage_class` is set, else
        a hostPath directory (single-node/dev only - see config.py)."""
        from kubernetes import client as k8s

        if str(self.settings.k8s_storage_class or "").strip():
            return k8s.V1Volume(
                name="home",
                persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                    claim_name=k8s_pvc_name(bot)
                ),
            )
        home = f"{self.settings.k8s_host_path_root.rstrip('/')}/{bot.id}"
        return k8s.V1Volume(
            name="home", host_path=k8s.V1HostPathVolumeSource(path=home, type="DirectoryOrCreate")
        )

    def _k8s_ensure_pvc(self, core, bot: Bot) -> None:
        """Get-or-create the bot's home PVC. Never recreated on every start -
        that would defeat the point of it surviving a stop."""
        from kubernetes.client.rest import ApiException

        name = k8s_pvc_name(bot)
        namespace = str(self.settings.k8s_namespace)
        try:
            core.read_namespaced_persistent_volume_claim(name, namespace)
            return
        except ApiException as exc:
            if exc.status != 404:
                raise

        from kubernetes import client as k8s

        pvc = k8s.V1PersistentVolumeClaim(
            metadata=k8s.V1ObjectMeta(
                name=name,
                labels={
                    "app.kubernetes.io/name": "nesq-bot-desktop",
                    "app.kubernetes.io/part-of": "nesqbot",
                    "nesqbot.bot_id": str(bot.id),
                },
            ),
            spec=k8s.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                storage_class_name=str(self.settings.k8s_storage_class),
                resources=k8s.V1ResourceRequirements(
                    requests={"storage": f"{self.settings.k8s_pvc_size_gi}Gi"}
                ),
            ),
        )
        try:
            core.create_namespaced_persistent_volume_claim(namespace, pvc)
        except ApiException as exc:
            if exc.status != 409:  # already created by a racing request
                raise

    def _k8s_pod_manifest(self, bot: Bot, name: str):
        """The Pod definition for one bot.

        Security posture matches `infra/bot-desktop/k8s/desktop-template.yaml`:
        non-root, no privilege escalation, every capability dropped, seccomp
        RuntimeDefault. A Bot Desktop runs a real browser driven by an LLM over
        content that must be assumed hostile, so this is not optional hardening.
        """
        from kubernetes import client as k8s

        settings = self.settings
        env = [
            k8s.V1EnvVar(name="BOT_SLUG", value=bot.slug),
            k8s.V1EnvVar(name="DESKTOP_PROFILE", value=bot.desktop_profile or "icewm"),
            k8s.V1EnvVar(name="VNC_PW", value=K8S_VNC_PASSWORD),
            k8s.V1EnvVar(name="NESQ_STREAM_PORT", value=str(K8S_STREAM_PORT)),
            k8s.V1EnvVar(name="NESQ_SIDECAR_PORT", value=str(K8S_CONTROL_PORT)),
        ]
        token = (settings.nesq_sidecar_token or "").strip()
        if token:
            # No write-only field exists for a Pod env var the way ACI's
            # `secure_value` is - this is readable by anyone with pod-read RBAC
            # on the namespace. Restrict who can `get pods -o yaml` there.
            env.append(k8s.V1EnvVar(name="NESQ_SIDECAR_TOKEN", value=token))
        else:
            logger.warning(
                "NESQ_SIDECAR_TOKEN is empty - the desktop control plane on %s will accept "
                "any caller that can reach the pod network",
                K8S_CONTROL_PORT,
            )

        image_pull_secrets = None
        secret_name = (settings.k8s_image_pull_secret or "").strip()
        if secret_name:
            image_pull_secrets = [k8s.V1LocalObjectReference(name=secret_name)]

        container = k8s.V1Container(
            name=ACI_CONTAINER_NAME,
            image=str(settings.bot_desktop_image),
            image_pull_policy="IfNotPresent",
            env=env,
            ports=[
                k8s.V1ContainerPort(name="stream", container_port=K8S_STREAM_PORT, protocol="TCP"),
                k8s.V1ContainerPort(name="control", container_port=K8S_CONTROL_PORT, protocol="TCP"),
            ],
            resources=k8s.V1ResourceRequirements(
                requests={"cpu": settings.k8s_cpu_request, "memory": settings.k8s_memory_request},
                limits={"cpu": settings.k8s_cpu_limit, "memory": settings.k8s_memory_limit},
            ),
            security_context=k8s.V1SecurityContext(
                allow_privilege_escalation=False,
                privileged=False,
                read_only_root_filesystem=False,  # X sockets, dbus and apt-free tmp writes
                capabilities=k8s.V1Capabilities(drop=["ALL"]),
            ),
            startup_probe=k8s.V1Probe(
                http_get=k8s.V1HTTPGetAction(path="/health", port="control"),
                period_seconds=5,
                failure_threshold=30,
                timeout_seconds=3,
            ),
            readiness_probe=k8s.V1Probe(
                http_get=k8s.V1HTTPGetAction(path="/health", port="control"),
                period_seconds=10,
                timeout_seconds=3,
                failure_threshold=3,
            ),
            liveness_probe=k8s.V1Probe(
                http_get=k8s.V1HTTPGetAction(path="/health", port="control"),
                period_seconds=20,
                timeout_seconds=5,
                failure_threshold=6,
            ),
            volume_mounts=[
                k8s.V1VolumeMount(name="home", mount_path="/home/nesq"),
                k8s.V1VolumeMount(name="tmp", mount_path="/tmp"),  # noqa: S108 - in-container path
                k8s.V1VolumeMount(name="dshm", mount_path="/dev/shm"),  # noqa: S108 - in-container path
            ],
        )
        pod = k8s.V1Pod(
            metadata=k8s.V1ObjectMeta(
                name=name,
                labels={
                    "app.kubernetes.io/name": "nesq-bot-desktop",
                    "app.kubernetes.io/part-of": "nesqbot",
                    "nesqbot.bot_id": str(bot.id),
                    "nesqbot.bot_slug": str(bot.slug)[:63],
                },
            ),
            spec=k8s.V1PodSpec(
                automount_service_account_token=False,
                restart_policy="Always",
                security_context=k8s.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=1000,
                    run_as_group=1000,
                    fs_group=1000,
                    seccomp_profile=k8s.V1SeccompProfile(type="RuntimeDefault"),
                ),
                image_pull_secrets=image_pull_secrets,
                containers=[container],
                volumes=[
                    self._k8s_home_volume(bot),
                    k8s.V1Volume(
                        name="tmp",
                        empty_dir=k8s.V1EmptyDirVolumeSource(medium="Memory", size_limit="256Mi"),
                    ),
                    # Chromium crashes with the default 64Mi /dev/shm.
                    k8s.V1Volume(
                        name="dshm",
                        empty_dir=k8s.V1EmptyDirVolumeSource(medium="Memory", size_limit="512Mi"),
                    ),
                ],
            ),
        )
        return pod

    def _k8s_service_manifest(self, bot: Bot, name: str):
        from kubernetes import client as k8s

        settings = self.settings
        ports = [
            k8s.V1ServicePort(name="stream", port=K8S_STREAM_PORT, target_port="stream", protocol="TCP"),
            k8s.V1ServicePort(name="control", port=K8S_CONTROL_PORT, target_port="control", protocol="TCP"),
        ]
        return k8s.V1Service(
            metadata=k8s.V1ObjectMeta(
                name=name,
                labels={"app.kubernetes.io/name": "nesq-bot-desktop", "nesqbot.bot_id": str(bot.id)},
            ),
            spec=k8s.V1ServiceSpec(
                type=str(settings.k8s_service_type),
                selector={"app.kubernetes.io/name": "nesq-bot-desktop", "nesqbot.bot_id": str(bot.id)},
                ports=ports,
            ),
        )

    def _k8s_urls(self, name: str, service) -> dict[str, str]:
        """Stream/control URLs for a created Service.

        ClusterIP -> cluster-local DNS, stable for the Service's lifetime and
        only reachable from inside the cluster (or the self-hoster's own
        ingress - out of scope here). NodePort -> `k8s_public_host` plus
        whichever node port the API server assigned (or the one requested).
        """
        namespace = str(self.settings.k8s_namespace)
        if str(self.settings.k8s_service_type) == "NodePort":
            host = str(self.settings.k8s_public_host).strip()
            node_ports = {p.name: p.node_port for p in (service.spec.ports or [])}
            return {
                "stream_url": f"http://{host}:{node_ports.get('stream')}",
                "control_url": f"http://{host}:{node_ports.get('control')}",
            }
        dns = f"{name}.{namespace}.svc.cluster.local"
        return {
            "stream_url": f"http://{dns}:{K8S_STREAM_PORT}",
            "control_url": f"http://{dns}:{K8S_CONTROL_PORT}",
        }

    def _k8s_delete_pod_and_service(self, core, name: str) -> None:
        """Idempotent teardown of the Pod and Service. Never the PVC - see `stop`.

        Catches broadly and never raises, like `_docker_stop` and `_aci_delete`:
        teardown never fails a request, and `start` also calls this to clear a
        stale Pod/Service before creating fresh ones, where a delete failure
        must not block a legitimate start.
        """
        from kubernetes.client.rest import ApiException

        namespace = str(self.settings.k8s_namespace)
        for delete, kind in (
            (core.delete_namespaced_service, "service"),
            (core.delete_namespaced_pod, "pod"),
        ):
            try:
                delete(name, namespace)
            except ApiException as exc:
                if exc.status != 404:
                    logger.warning("k8s %s delete failed for %s: %s", kind, name, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("k8s %s delete failed for %s: %s", kind, name, exc)

    def _k8s_delete_pvc(self, core, bot: Bot) -> None:
        """Never raises - `stop` calls this unguarded, same contract as above."""
        from kubernetes.client.rest import ApiException

        try:
            core.delete_namespaced_persistent_volume_claim(
                k8s_pvc_name(bot), str(self.settings.k8s_namespace)
            )
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("k8s pvc delete failed for bot %s: %s", bot.id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("k8s pvc delete failed for bot %s: %s", bot.id, exc)

    async def _k8s_start(self, bot: Bot) -> dict[str, str]:
        """Create this bot's Pod (+ PVC, + Service) and wait for it to be ready.

        Idempotent the same way `_docker_start` is: a stale Pod/Service with
        this bot's name is removed first rather than left to collide, since a
        `start` after an ungraceful `stop` (API restart mid-teardown, say)
        must not fail just because the old objects are still terminating.
        """
        self._k8s_require_config()
        name = k8s_resource_name(bot)
        core = await asyncio.to_thread(self._k8s_client)
        await asyncio.to_thread(self._k8s_delete_pod_and_service, core, name)
        if str(self.settings.k8s_storage_class or "").strip():
            await asyncio.to_thread(self._k8s_ensure_pvc, core, bot)
        pod = self._k8s_pod_manifest(bot, name)
        service = self._k8s_service_manifest(bot, name)
        namespace = str(self.settings.k8s_namespace)
        await asyncio.to_thread(core.create_namespaced_pod, namespace, pod)
        created_service = await asyncio.to_thread(core.create_namespaced_service, namespace, service)
        return await self._k8s_wait_for_running(core, name, created_service)

    async def _k8s_wait_for_running(self, core, name: str, service) -> dict[str, str]:
        budget = max(float(self.settings.k8s_start_timeout_seconds), 0.0)
        namespace = str(self.settings.k8s_namespace)
        started = time.monotonic()
        deadline = started + budget
        pod = None
        while True:
            try:
                pod = await asyncio.to_thread(core.read_namespaced_pod, name, namespace)
            except Exception as exc:  # noqa: BLE001 - a 404 right after create is normal
                logger.debug("k8s pod %s not readable yet: %s", name, exc)
                pod = None

            if pod is not None and k8s_pod_ready(pod):
                urls = self._k8s_urls(name, service)
                return {"container_id": name, **urls}

            if time.monotonic() >= deadline:
                waited = time.monotonic() - started
                raise K8sStartError(
                    f"pod {name} did not become ready within {budget:.0f}s: "
                    f"{k8s_start_failure_reason(pod, str(self.settings.bot_desktop_image), waited)}."
                    f" It was left in place for diagnosis (kubectl -n {namespace} describe pod {name}).",
                    container_id=name,
                )
            await asyncio.sleep(K8S_POLL_INTERVAL_SECONDS)

    async def _k8s_resume(self, db: AsyncSession, bot_id: uuid.UUID, name: str) -> dict[str, str]:
        """k8s resume: recreate the Pod + Service, reattaching the same home
        volume by name. Unlike `docker` pause/unpause this is a cold start -
        the running processes and the X session are gone - but unlike `aci`
        the bot's home directory (PVC or hostPath) survives, so it is a cold
        start onto the same filesystem, not a blank one.
        """
        bot = await db.get(Bot, bot_id)
        if bot is None:
            raise K8sStartError(f"bot {bot_id} no longer exists")
        # Releasing at the call site is not enough: this `db.get` re-opens a
        # transaction of its own, and `_k8s_start` is the part that waits out
        # `k8s_start_timeout_seconds`. See `resume`.
        await release_transaction(db)
        return await self._k8s_start(bot)

    def _home_dir(self, bot_id: uuid.UUID) -> Path | None:
        """Resolved home for a bot, or None when it escapes the configured root."""
        try:
            root = Path(self.settings.bot_desktop_home_root).resolve()
            home = (root / str(bot_id)).resolve()
        except OSError:
            return None
        if home == root or root not in home.parents:
            logger.error("refusing to touch %s â€” outside desktop home root %s", home, root)
            return None
        return home

    def _wipe_home(self, bot_id: uuid.UUID) -> bool:
        home = self._home_dir(bot_id)
        if home is None or not home.exists():
            return False
        shutil.rmtree(home, ignore_errors=True)
        return not home.exists()

    def _k8s_wipe_hostpath_home(self, bot_id: uuid.UUID) -> bool:
        """Wipe a hostPath home directory. Only correct when the API and the
        cluster node share a filesystem - the single-node dev layout hostPath
        targets. On a real multi-node cluster this deletes nothing on the node
        the pod actually ran on; that gap is why `k8s_storage_class` exists."""
        try:
            root = Path(self.settings.k8s_host_path_root).resolve()
            home = (root / str(bot_id)).resolve()
        except OSError:
            return False
        if home == root or root not in home.parents:
            logger.error("refusing to touch %s â€” outside k8s_host_path_root %s", home, root)
            return False
        if not home.exists():
            return False
        shutil.rmtree(home, ignore_errors=True)
        return not home.exists()

    async def stop(self, db: AsyncSession, bot_id: uuid.UUID, wipe: bool = False) -> BotDesktop:
        """Tear the desktop down.

        On ``aci`` this deletes the container group, and a container group has no
        persistent volume attached by this driver: **every stop is a wipe**, whatever
        `wipe` says. The docker driver keeps `/home/nesq` on the host unless asked to
        wipe it; ACI cannot, because the only ACI volume type that would survive is an
        Azure Files share, and mounting one needs a storage account *key* in config â€”
        which is precisely the kind of credential this lane refuses to hold. Persisting
        a bot home across stops on ACI needs a file share plus an identity-based way to
        mount it; until then, treat `stop` on ACI as destructive.
        """
        desktop = await self.get(db, bot_id)
        desktop.state = "stopping"
        await db.commit()
        if self.settings.bot_desktop_mode == "docker" and desktop.container_id:
            await asyncio.to_thread(self._docker_stop, desktop.container_id)
        elif self.settings.bot_desktop_mode == "aci" and desktop.container_id:
            await asyncio.to_thread(self._aci_delete, desktop.container_id)
        elif self.settings.bot_desktop_mode == "k8s" and desktop.container_id:
            bot = await db.get(Bot, bot_id)
            core = await asyncio.to_thread(self._k8s_client)
            await asyncio.to_thread(self._k8s_delete_pod_and_service, core, desktop.container_id)
            # Unlike `aci`, a k8s desktop's home survives stop by default - the PVC
            # (or hostPath directory) is untouched here. `wipe` below is what deletes it.
            if wipe and bot is not None and str(self.settings.k8s_storage_class or "").strip():
                await asyncio.to_thread(self._k8s_delete_pvc, core, bot)
        desktop.state = "absent"
        desktop.container_id = None
        desktop.stream_url = None
        desktop.control_url = None
        if wipe:
            if self.settings.bot_desktop_mode == "aci":
                logger.info(
                    "aci desktop for bot %s deleted; its filesystem went with the group, so "
                    "there is no separate home to wipe",
                    bot_id,
                )
            elif self.settings.bot_desktop_mode == "k8s":
                if str(self.settings.k8s_storage_class or "").strip():
                    logger.info("k8s desktop for bot %s: PVC deleted with the pod", bot_id)
                else:
                    wiped = await asyncio.to_thread(self._k8s_wipe_hostpath_home, bot_id)
                    if not wiped:
                        logger.warning("hostPath home wipe for bot %s did not complete", bot_id)
            else:
                wiped = await asyncio.to_thread(self._wipe_home, bot_id)
                if not wiped:
                    logger.warning("home wipe for bot %s did not complete", bot_id)
        await db.commit()
        await db.refresh(desktop)
        return desktop

    def _docker_stop(self, container_id: str) -> None:
        client = self._docker_client()
        try:
            c = client.containers.get(container_id)
            c.stop(timeout=10)
            c.remove(force=True)
        except Exception:  # noqa: BLE001
            logger.warning("container stop failed for %s", container_id)

    async def _fail(self, db: AsyncSession, desktop: BotDesktop, exc: Exception) -> BotDesktop:
        """Record a driver failure on the row. Nothing here raises at a request."""
        desktop.state = "error"
        desktop.last_error = str(exc)
        await db.commit()
        await db.refresh(desktop)
        return desktop

    async def suspend(self, db: AsyncSession, bot_id: uuid.UUID) -> BotDesktop:
        """Park the desktop.

        The two backends do *not* mean the same thing by this, and the difference is
        not cosmetic:

        * docker â€” `pause` freezes the processes (SIGSTOP). Memory, the X session and
          every open window survive; resume is instant and the container is still
          billed as running because it never stopped occupying the host.
        * aci â€” `stop` deallocates the container group. Billing stops, which is the
          whole point, but the running processes, the in-memory state and the
          container filesystem are all destroyed. `resume` is a cold boot of the same
          definition, not a thaw, and Azure may hand it a different private IP.

        So a caller that suspends mid-task and expects to find the same browser tabs
        after resume is right on docker and wrong on ACI. Nothing in this repo relies
        on that today (suspend/resume only move the state field and the sidecar is
        stateless), but any future "park the bot mid-routine" feature has to treat an
        ACI suspend as losing the desktop's state, or it will silently corrupt work.
        """
        desktop = await self.get(db, bot_id)
        # Same reason as `resume` below. `get` only commits on the branch that
        # creates a missing row, so the ordinary path arrives here with the
        # SELECT's transaction still open, and an ACI group stop or a k8s Pod
        # delete is an Azure/API-server round trip of unbounded length.
        await release_transaction(db)
        if self.settings.bot_desktop_mode == "docker" and desktop.container_id:
            await asyncio.to_thread(self._docker_pause, desktop.container_id)
        elif self.settings.bot_desktop_mode == "aci" and desktop.container_id:
            try:
                await asyncio.to_thread(self._aci_stop_group, desktop.container_id)
            except Exception as exc:  # noqa: BLE001
                # Unlike a docker pause, a failed ACI stop means the group is still
                # running and still billing. Saying "suspended" would be a lie about
                # money as well as about state.
                logger.warning("aci suspend failed for %s: %s", bot_id, exc)
                return await self._fail(db, desktop, exc)
            # The addresses die with the compute, and Azure reuses private IPs â€” a
            # stale URL could later point at a different bot's desktop.
            desktop.stream_url = None
            desktop.control_url = None
        elif self.settings.bot_desktop_mode == "k8s" and desktop.container_id:
            try:
                core = await asyncio.to_thread(self._k8s_client)
                await asyncio.to_thread(
                    self._k8s_delete_pod_and_service, core, desktop.container_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("k8s suspend failed for %s: %s", bot_id, exc)
                return await self._fail(db, desktop, exc)
            # The Pod and Service are gone - the running processes and the X
            # session with them - but the PVC/hostPath home is untouched, so
            # `resume` comes back onto the same filesystem, just not the same
            # in-memory state. Same caveat as `aci`, minus the data loss.
            desktop.stream_url = None
            desktop.control_url = None
        desktop.state = "suspended"
        await db.commit()
        await db.refresh(desktop)
        return desktop

    async def resume(self, db: AsyncSession, bot_id: uuid.UUID) -> BotDesktop:
        desktop = await self.get(db, bot_id)
        if desktop.state != "suspended":
            return desktop

        # `start` and `stop` both commit immediately before their slow call; this
        # path did not, and that omission costs more than state. `get` above
        # issues a SELECT and only commits on the branch that creates a missing
        # row, so the transaction is open when `_aci_resume`/`_k8s_resume` waits
        # on a cold start — budgeted at `aci_start_timeout_seconds = 180` and
        # `k8s_start_timeout_seconds = 180`, three times the
        # `idle_in_transaction_session_timeout = 60000` that
        # `db.release_transaction` records for nesqbot-pg.
        #
        # The failure is not a lost read. The commit at the end of this method
        # raises on the terminated backend, so the row keeps `state="suspended"`
        # and the *old* container_id/stream_url while the group is actually
        # running: the viewer points at a dead address, nothing records the new
        # handle, and `stop`'s docstring is explicit that an ACI group bills
        # until something deletes it.
        await release_transaction(db)

        if self.settings.bot_desktop_mode == "docker" and desktop.container_id:
            try:
                await asyncio.to_thread(self._docker_unpause, desktop.container_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("resume failed for %s: %s", bot_id, exc)
                return await self._fail(db, desktop, exc)
        elif self.settings.bot_desktop_mode == "aci" and desktop.container_id:
            try:
                info = await self._aci_resume(desktop.container_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("aci resume failed for %s: %s", bot_id, exc)
                return await self._fail(db, desktop, exc)
            # A restarted group can come back on a different private IP, so the
            # stream and control URLs are re-derived rather than reused.
            desktop.container_id = info["container_id"]
            desktop.stream_url = info["stream_url"]
            desktop.control_url = info["control_url"]
        elif self.settings.bot_desktop_mode == "k8s" and desktop.container_id:
            try:
                info = await self._k8s_resume(db, bot_id, desktop.container_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("k8s resume failed for %s: %s", bot_id, exc)
                return await self._fail(db, desktop, exc)
            desktop.container_id = info["container_id"]
            desktop.stream_url = info["stream_url"]
            desktop.control_url = info["control_url"]
        desktop.state = "running"
        desktop.last_error = None
        await db.commit()
        await db.refresh(desktop)
        return desktop

    def _docker_pause(self, container_id: str) -> None:
        client = self._docker_client()
        try:
            client.containers.get(container_id).pause()
        except Exception:  # noqa: BLE001
            pass

    def _docker_unpause(self, container_id: str) -> None:
        client = self._docker_client()
        client.containers.get(container_id).unpause()

    async def screenshot(
        self,
        db: AsyncSession,
        bot_id: uuid.UUID,
        *,
        fmt: str | None = None,
        quality: int | None = None,
        max_width: int | None = None,
        grayscale: bool | None = None,
    ) -> dict:
        """Proxy the sidecar `/screenshot`; mock mode returns a generated PNG.

        Every option defaults to None, which sends no query parameter and gets
        the sidecar's own default: a full-size PNG under `png_base64`. That is
        the shape `docs/API.md` pins for `GET /bots/{id}/desktop/screenshot`
        and the shape a human wants when they look at their bot's screen, so
        the HTTP route is unaffected by anything below.

        The agent loop passes the compact options instead (JPEG, q70, 1024px)
        because it pays for the same frame on every step. Passing `max_width`
        changes the coordinate space of the returned image â€” read
        `ScreenGeometry` before you use it.
        """
        desktop = await self.get(db, bot_id)
        if self.settings.bot_desktop_mode == "mock":
            return self._mock_screenshot(max_width)
        if desktop.state != "running" or not desktop.control_url:
            return {"ok": False, "error": "desktop not running"}
        params: dict[str, Any] = {}
        if fmt:
            params["format"] = fmt
        if quality is not None:
            params["quality"] = int(quality)
        if max_width is not None:
            params["max_width"] = int(max_width)
        if grayscale is not None:
            params["grayscale"] = "true" if grayscale else "false"
        return await self._sidecar_get(desktop.control_url, "/screenshot", params=params or None)

    def _mock_screenshot(self, max_width: int | None) -> dict:
        """A placeholder frame that models the sidecar, downscale included.

        `max_width` is honoured rather than ignored: the coordinate mapping is
        the risky half of the downscale, and a mock that always answered at
        full size would leave it untested everywhere except production. The
        image is *generated* at the reduced size rather than resampled â€” there
        is no Pillow in the API â€” which is indistinguishable from the sidecar's
        behaviour as far as every caller is concerned.

        Always PNG. Encoding a JPEG would need an image library the API does
        not ship, and claiming a `mime` the bytes do not match would be the one
        kind of lie this codebase does not tell.
        """
        screen_width, screen_height = MOCK_SCREENSHOT_SIZE
        width, height = screen_width, screen_height
        scale = 1.0
        if max_width and max_width < screen_width:
            scale = max_width / screen_width
            width = int(max_width)
            height = max(1, round(screen_height * scale))
        png = make_placeholder_png(width, height)
        payload = base64.b64encode(png).decode("ascii")
        return {
            "ok": True,
            "mock": True,
            "width": width,
            "height": height,
            "screen_width": screen_width,
            "screen_height": screen_height,
            "region": None,
            "scale": round(scale, 4),
            "mime": "image/png",
            "bytes": len(png),
            "image_base64": payload,
            "png_base64": payload,
        }

    async def windows(self, db: AsyncSession, bot_id: uuid.UUID) -> dict:
        """Proxy the sidecar `/windows`; mock mode returns a canned window list."""
        desktop = await self.get(db, bot_id)
        if self.settings.bot_desktop_mode == "mock":
            return {"ok": True, "mock": True, "windows": [dict(w) for w in MOCK_WINDOWS]}
        if desktop.state != "running" or not desktop.control_url:
            return {"ok": False, "error": "desktop not running"}
        result = await self._sidecar_get(desktop.control_url, "/windows")
        if result.get("ok"):
            result["windows"] = [parse_window_line(w) for w in result.get("windows") or []]
        return result

    async def _sidecar_get(
        self, control_url: str, path: str, params: dict[str, Any] | None = None
    ) -> dict:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.settings.sidecar_timeout_seconds) as client:
                r = await client.get(
                    f"{control_url.rstrip('/')}{path}",
                    headers=self._sidecar_headers(),
                    params=params or None,
                )
                r.raise_for_status()
                return r.json()
        except Exception as exc:  # noqa: BLE001 - sidecar hiccups are not 500s
            logger.warning("sidecar %s failed: %s", path, exc)
            return {"ok": False, "error": str(exc)}

    async def computer_action(
        self,
        db: AsyncSession,
        bot_id: uuid.UUID,
        action: str,
        payload: dict,
    ) -> dict:
        desktop = await self.get(db, bot_id)
        if desktop.state != "running" or not desktop.control_url:
            return {"ok": False, "error": "desktop not running"}

        # Copy so the caller's dict is untouched, and replace `button` in place
        # rather than sending both the raw and the normalized value downstream.
        payload = dict(payload or {})
        if "button" in payload:
            payload["button"] = normalize_button(payload["button"])

        if self.settings.bot_desktop_mode == "mock":
            return {"ok": True, "action": action, "payload": payload, "mock": True}

        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.settings.sidecar_timeout_seconds) as client:
                r = await client.post(
                    f"{desktop.control_url.rstrip('/')}/action",
                    json={"action": action, **payload},
                    headers=self._sidecar_headers(),
                )
                r.raise_for_status()
                return r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("desktop action %s failed: %s", action, exc)
            return {"ok": False, "action": action, "error": str(exc)}

    async def browser_call(
        self,
        db: AsyncSession,
        bot_id: uuid.UUID,
        action: str,
        payload: dict | None = None,
    ) -> dict:
        """Proxy one `/browser/*` endpoint, keeping the sidecar's error contract.

        This is deliberately *not* `computer_action` with a different path.
        `computer_action` calls `raise_for_status()` and collapses everything
        that goes wrong into `{"ok": false, "error": "<repr of an exception>"}`,
        which is adequate for a pixel click — there is nothing useful to say
        about one beyond "it did not happen". The browser lane's whole value is
        in the codes:

        * ``409 obscured`` means *"I refused rather than clicking the wrong
          thing, and here is what is on top"* — the model should dismiss the
          cookie banner, not retry;
        * ``409 stale_ref`` means the page moved and the fix is one snapshot;
        * ``503 browser_unavailable`` means fall back to pixels and carry on;
        * ``504 cdp_timeout`` with ``pending_dialog`` means a blocking
          ``alert()`` froze the renderer.

        Flattened to "the click failed", all four become the same retry. So the
        JSON body is returned *as the sidecar wrote it*, with `status` added,
        and `browser.result_text` is what turns it into a sentence.

        Unreachable-sidecar and mock deployments both answer
        `browser_unavailable`, because that is what is true: there is no
        Chromium to drive, and the caller's correct response — use the pixel
        API — is identical in both cases.
        """
        op = browser_ops.op_for(action)
        if op is None:
            return {
                "ok": False,
                "action": action,
                "error": "unknown_browser_action",
                "status": 400,
                "detail": f"no /browser endpoint is named '{action}'",
            }

        desktop = await self.get(db, bot_id)
        if self.settings.bot_desktop_mode == "mock":
            return {
                "ok": False,
                "action": action,
                "mock": True,
                "error": browser_ops.BROWSER_UNAVAILABLE,
                "status": 503,
                "detail": "this deployment runs no real browser (BOT_DESKTOP_MODE=mock)",
            }
        if desktop.state != "running" or not desktop.control_url:
            return {
                "ok": False,
                "action": action,
                "error": browser_ops.BROWSER_UNAVAILABLE,
                "status": 503,
                "detail": "desktop not running",
            }

        body = browser_ops.request_body(op, payload)
        url = f"{desktop.control_url.rstrip('/')}{op.path}"

        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.settings.sidecar_timeout_seconds) as client:
                if op.method == "GET":
                    response = await client.get(url, headers=self._sidecar_headers())
                else:
                    response = await client.post(
                        url, json=body, headers=self._sidecar_headers()
                    )
        except Exception as exc:  # noqa: BLE001 - an unreachable sidecar is not a 500
            logger.warning("browser %s failed: %s", action, exc)
            return {
                "ok": False,
                "action": action,
                "error": browser_ops.BROWSER_UNAVAILABLE,
                "status": 503,
                "detail": str(exc),
            }

        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            # A proxy, a gateway or a crashed worker answered instead of the
            # sidecar. Say so with the real status rather than inventing an
            # error code the contract does not contain.
            return {
                "ok": False,
                "action": action,
                "error": "cdp_error",
                "status": response.status_code,
                "detail": f"the sidecar answered {response.status_code} with a non-JSON body",
            }
        # `ok` and `error` are trusted from the body whenever the body speaks
        # the contract — inferring them over the top would be a second opinion
        # that could disagree with the one the sidecar documents. What
        # `browser.envelope` adds is the case the old code assumed away: a
        # response that does not speak the contract at all, which is what a
        # desktop running a pre-DOM image returns for every one of these paths.
        return browser_ops.envelope(action, response.status_code, parsed)


# ---------------------------------------------------------------------------
# Bot Desktop stream proxy
#
# A Bot Desktop's noVNC endpoint lives on a *private* address in the delegated
# subnet (see `_aci_container_group`: `type="Private"`, no `dns_name_label`).
# That is the isolation claim, so the stream is deliberately unreachable from a
# user's laptop. The API, however, sits inside the same VNet and already has
# public ingress - so the only honest way to show a desktop to its owner is to
# proxy it *through* the API, under the same authorization as every other bot
# route.
#
# Two legs are needed and both are load-bearing:
#
#   * the HTTP leg - noVNC's `vnc.html`, its JS/CSS/images;
#   * the WebSocket leg - the actual RFC 6455 transport websockify speaks.
#
# An HTTP-only proxy produces a page that loads and never paints.
#
# Browsers cannot set an `Authorization` header on either an `<iframe src>` or a
# `new WebSocket(...)` handshake, so neither leg can carry the session JWT the
# way the rest of the API does. The answer here is a **ticket**: a short-lived,
# HMAC-signed capability minted by an authenticated `POST`, bound to one
# (user, bot) pair, carried in the URL *path* so relative asset references
# inherit it, and burned the moment the WebSocket redeems it.
#
# Why the path and not the query: `vnc.html` pulls dozens of relative assets, and
# a query parameter is dropped by relative resolution while a path prefix is not.
# A cookie would work in a browser but not reliably inside a Tauri webview,
# where the API is a third-party origin. Why a ticket and not the JWT: a
# 14-day session token in a URL is a 14-day session token in a log. A ticket is
# 60 seconds long, single-redeem for the control connection, and worthless for
# anything but this one bot's pixels.
# ---------------------------------------------------------------------------

#: How long a freshly minted ticket stays usable. Long enough for a slow app to
#: put the iframe on screen and for noVNC to fetch its assets, short enough that
#: a ticket recovered from a log or a screen recording is already dead.
DESKTOP_STREAM_TICKET_TTL_SECONDS = 60

#: Tear the relay down after this long with no traffic in either direction. A
#: live VNC session is never silent for minutes; a wedged one is.
DESKTOP_STREAM_IDLE_TIMEOUT_SECONDS = 300.0

#: Budget for the upstream WebSocket handshake.
DESKTOP_STREAM_CONNECT_TIMEOUT_SECONDS = 10.0

#: Served when the proxied path is empty - noVNC's page, not a directory listing.
DESKTOP_STREAM_DEFAULT_ASSET = "vnc.html"

#: The path websockify upgrades on. `infra/bot-desktop/entrypoint.sh` runs
#: `websockify --web=<novnc root> <port> localhost:5900`, which upgrades any
#: request carrying `Upgrade: websocket`; noVNC's own default is `websockify`.
DESKTOP_STREAM_WS_UPSTREAM_PATH = "websockify"

#: Redis key prefix for the single-redeem claim. Only the nonce is stored - the
#: ticket itself never lands anywhere durable.
DESKTOP_STREAM_CLAIM_PREFIX = "nesq:desktop:stream-ticket:"

#: Ticket wire format version, so the shape can change without a silent
#: mis-parse being read as a valid ticket.
DESKTOP_STREAM_TICKET_VERSION = "v1"

#: How long to stop trying redis after a failed connect. Long enough that a
#: down redis does not add its connect timeout to every ticket redeemed.
_REDIS_RETRY_SECONDS = 30.0

#: Request headers worth forwarding upstream. Everything else - `authorization`,
#: `cookie`, `host`, the hop-by-hop set - is dropped: websockify has no use for
#: them and forwarding credentials into a container an LLM drives is exactly the
#: kind of accident this proxy exists to prevent. `accept-encoding` is dropped so
#: the upstream answers identity and the raw byte stream can be relayed with its
#: `content-length` intact, and the conditional headers go with it: the proxied
#: responses are `no-store`, so a revalidation round trip has nothing to hit.
STREAM_PROXY_FORWARDABLE_HEADERS = frozenset({"accept", "accept-language", "range"})

#: Response headers worth returning. Anything that would let the upstream set
#: cookies, redirect the parent frame, or relax the app's own CSP is dropped.
STREAM_PROXY_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
    }
)


class StreamTicketError(RuntimeError):
    """A ticket was missing, malformed, expired, already used, or not for this bot.

    The `reason` is for operators. Callers deliberately render one
    indistinguishable rejection to the client: telling an attacker *which* check
    failed lets them walk a forged ticket towards acceptance, exactly as with the
    Entra rejections in `app.auth`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class StreamTicket:
    """A minted capability to view one bot's desktop, for one user, briefly."""

    token: str
    bot_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime

    @property
    def expires_in(self) -> int:
        """Whole seconds left, floored at zero."""
        remaining = (self.expires_at - datetime.now(timezone.utc)).total_seconds()
        return max(int(remaining), 0)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class DesktopStreamTickets:
    """Mint and redeem the short-lived capability that fronts the stream proxy.

    Signed rather than stored, on purpose: the API runs with ``minReplicas: 2``
    in the full deployment (infra/azure/main.bicep), so a ticket minted on one
    replica is routinely presented to another. A server-side table would make
    that a coin flip; an HMAC over the claims is verifiable anywhere the
    ``JWT_SECRET`` is.

    Single-use is the one thing a signature cannot express, so it is a *claim*:
    Redis ``SET NX`` when Redis is reachable (which is the deployed
    configuration), an in-process set otherwise. The fallback is honest about its
    limit - with Redis down and two replicas, a replayed redeem could land on the
    replica that has not seen the nonce. That window is bounded by the
    60-second TTL and by the ticket being useless for any bot but the one it
    names, which is why the fallback is acceptable and a silent "trust me" would
    not be.
    """

    def __init__(self) -> None:
        # nonce -> monotonic deadline. Pruned on every read and write, so it can
        # never grow past the tickets minted in one TTL window.
        self._claimed: dict[str, float] = {}
        # Redis client, and the loop it belongs to. A client is bound to the loop
        # that created it, so it is re-dialled rather than reused when the loop
        # changes - otherwise a cached client would raise "bound to a different
        # event loop" at exactly the wrong moment.
        self._redis_client: Any = None
        self._redis_loop: Any = None
        #: monotonic deadline before which no further connect is attempted.
        self._redis_retry_after = 0.0

    # -- minting ----------------------------------------------------------

    def mint(
        self,
        *,
        bot_id: uuid.UUID,
        user_id: uuid.UUID,
        ttl_seconds: int = DESKTOP_STREAM_TICKET_TTL_SECONDS,
    ) -> StreamTicket:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(int(ttl_seconds), 1))
        nonce = secrets.token_urlsafe(16)
        payload = ":".join(
            (
                DESKTOP_STREAM_TICKET_VERSION,
                str(bot_id),
                str(user_id),
                str(int(expires_at.timestamp())),
                nonce,
            )
        )
        encoded = _b64(payload.encode("utf-8"))
        return StreamTicket(
            token=f"{encoded}.{self._sign(encoded)}",
            bot_id=bot_id,
            user_id=user_id,
            expires_at=expires_at,
        )

    # -- verification -----------------------------------------------------

    def verify(self, ticket: str, *, bot_id: uuid.UUID) -> StreamTicket:
        """Check signature, expiry and bot binding. Does **not** consume the ticket.

        This is the gate on the static assets, and it deliberately ignores
        whether the control connection has already been redeemed. noVNC pulls
        dozens of files for one session and keeps pulling a few of them *after*
        it connects; spending the ticket on the first GET would make the page
        unloadable, and spending it on the WebSocket would leave the page
        half-painted. What single-use protects is the control connection - the
        assets are the stock noVNC distribution, and the 60-second expiry is
        what bounds them.
        """
        encoded, _, signature = str(ticket or "").partition(".")
        if not encoded or not signature:
            raise StreamTicketError("malformed ticket")
        if not hmac.compare_digest(signature, self._sign(encoded)):
            raise StreamTicketError("bad signature")

        try:
            fields = _unb64(encoded).decode("utf-8").split(":")
        except (ValueError, UnicodeDecodeError) as exc:
            raise StreamTicketError(f"undecodable payload: {exc}") from exc
        if len(fields) != 5:
            raise StreamTicketError("unexpected payload shape")

        version, raw_bot, raw_user, raw_exp, nonce = fields
        if version != DESKTOP_STREAM_TICKET_VERSION:
            raise StreamTicketError(f"unknown ticket version {version!r}")
        try:
            ticket_bot = uuid.UUID(raw_bot)
            ticket_user = uuid.UUID(raw_user)
            expires_at = datetime.fromtimestamp(int(raw_exp), tz=timezone.utc)
        except (TypeError, ValueError) as exc:
            raise StreamTicketError(f"unparseable claims: {exc}") from exc

        if ticket_bot != bot_id:
            # A signed ticket for someone else's desktop is the whole attack; a
            # good signature must never be enough on its own.
            raise StreamTicketError("ticket is for a different bot")
        if expires_at <= datetime.now(timezone.utc):
            raise StreamTicketError("ticket expired")
        if not nonce:
            raise StreamTicketError("ticket carries no nonce")

        return StreamTicket(
            token=str(ticket), bot_id=ticket_bot, user_id=ticket_user, expires_at=expires_at
        )

    async def redeem(self, ticket: str, *, bot_id: uuid.UUID) -> StreamTicket:
        """Verify **and** burn the ticket. Only the control connection calls this.

        A second WebSocket presenting the same ticket is refused even while its
        assets are still loading, which is the property that matters: a ticket
        recovered from a log or an over-the-shoulder read cannot open a second
        remote-control session onto the desktop.
        """
        claims = self.verify(ticket, bot_id=bot_id)
        if not await self._claim(self.nonce_of(ticket), claims.expires_in + 1):
            raise StreamTicketError("ticket already redeemed")
        return claims

    def nonce_of(self, ticket: str) -> str:
        """The nonce inside a ticket, for claim bookkeeping. Assumes verified input."""
        encoded = str(ticket or "").partition(".")[0]
        return _unb64(encoded).decode("utf-8").split(":")[-1]

    # -- internals --------------------------------------------------------

    def _sign(self, encoded_payload: str) -> str:
        """Base64url HMAC-SHA256 over the encoded claims.

        The signing key is read inline rather than bound to a local: a digest is
        a one-way function of the key, so what leaves here provably carries none
        of it, and there is no need for a name holding the raw secret to exist at
        all. `tests/services/test_vendors.py` audits this module for exactly that
        shape of data flow.
        """
        digest = hmac.new(
            str(get_settings().jwt_secret or "").encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return _b64(digest)

    def _prune(self) -> None:
        now = time.monotonic()
        for nonce in [n for n, deadline in self._claimed.items() if deadline <= now]:
            self._claimed.pop(nonce, None)

    def _remember(self, nonce: str, ttl_seconds: int) -> None:
        self._prune()
        self._claimed[nonce] = time.monotonic() + max(ttl_seconds, 1)

    async def _claim(self, nonce: str, ttl_seconds: int) -> bool:
        """Take the single redemption, returning False if someone already had it."""
        client = await self._redis()
        if client is not None:
            try:
                taken = await client.set(
                    f"{DESKTOP_STREAM_CLAIM_PREFIX}{nonce}",
                    "1",
                    nx=True,
                    ex=max(ttl_seconds, 1),
                )
            except Exception as exc:  # noqa: BLE001 - a redis hiccup must not lock viewers out
                logger.warning("stream ticket claim via redis failed (%s); using local state", exc)
            else:
                # Mirror locally as well, so this replica still refuses a replay
                # if redis disappears between the claim and the replay.
                if taken:
                    self._remember(nonce, ttl_seconds)
                return bool(taken)

        self._prune()
        if nonce in self._claimed:
            return False
        self._remember(nonce, ttl_seconds)
        return True

    async def _redis(self):
        """A redis client for the replay claims, or None when there is not one.

        Its own client rather than the event bus's: `app.services.events` caches
        a module-level `asyncio.Lock`, which binds to whichever loop first
        touched it and then raises "bound to a different event loop" for
        everyone afterwards. Borrowing that machinery for an unrelated purpose
        made the ticket store able to break the event bus, which is not a trade
        a viewer-side nicety gets to make.

        Redis being down is never a viewer-facing failure - it only downgrades
        the single-use claim to this process, which `_claim` handles. So a failed
        connect is remembered for `_REDIS_RETRY_SECONDS` rather than retried on
        every ticket.
        """
        loop = asyncio.get_running_loop()
        if self._redis_client is not None:
            if self._redis_loop is loop:
                return self._redis_client
            # The loop that owned it is gone; the client goes with it.
            self._redis_client = None
            self._redis_loop = None
        if time.monotonic() < self._redis_retry_after:
            return None

        settings = get_settings()
        timeout = float(settings.redis_connect_timeout_seconds)
        client = None
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
            )
            await asyncio.wait_for(client.ping(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - any failure means "no redis today"
            logger.info(
                "redis unavailable for stream ticket replay claims (%s); "
                "single-use is enforced per API replica until it is back",
                exc,
            )
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()
            self._redis_retry_after = time.monotonic() + _REDIS_RETRY_SECONDS
            return None

        self._redis_client = client
        self._redis_loop = loop
        return client


#: Process-wide ticket authority. Stateless apart from the replay claims.
stream_tickets = DesktopStreamTickets()


# --- upstream URL plumbing -------------------------------------------------


def stream_origin(desktop: Any) -> str:
    """`scheme://host:port` of a desktop's noVNC endpoint, or `""` when it has none.

    `stream_url` is written by whichever driver started the desktop: ACI stores
    `http://10.60.4.x:6901`, docker `http://localhost:<hashed port>`, mock a URL
    with a query string on it. Only the origin is ever wanted here - the path is
    the proxy's business.
    """
    raw = str(getattr(desktop, "stream_url", "") or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "//" in raw else f"//{raw}", scheme="http")
    if not parsed.hostname:
        return ""
    scheme = parsed.scheme or "http"
    if scheme not in ("http", "https"):
        return ""
    return f"{scheme}://{parsed.netloc}"


def normalise_stream_path(path: str) -> str:
    """The upstream path for a proxied asset, or raise `ValueError`.

    Rejects anything that could aim the proxy somewhere other than the desktop it
    belongs to: parent traversal, a scheme, a protocol-relative `//host`, a
    backslash, or an embedded NUL/CR/LF. This proxy is a hole through the network
    boundary that exists precisely to stop arbitrary reach into the VNet, so the
    path is validated rather than merely quoted.
    """
    candidate = str(path or "").strip()
    if not candidate or candidate in (".", "/"):
        return DESKTOP_STREAM_DEFAULT_ASSET
    if any(ch in candidate for ch in ("\\", "\x00", "\r", "\n")):
        raise ValueError("illegal character in stream path")
    if "://" in candidate or candidate.startswith("//"):
        raise ValueError("stream path may not carry a scheme or host")
    candidate = candidate.lstrip("/")
    if any(segment == ".." for segment in candidate.split("/")):
        raise ValueError("stream path may not traverse upwards")
    return candidate or DESKTOP_STREAM_DEFAULT_ASSET


def stream_asset_url(origin: str, path: str, query: str = "") -> str:
    """Absolute upstream URL for one proxied HTTP asset."""
    url = f"{origin.rstrip('/')}/{normalise_stream_path(path)}"
    return f"{url}?{query}" if query else url


def stream_ws_url(origin: str, path: str = DESKTOP_STREAM_WS_UPSTREAM_PATH) -> str:
    """`ws(s)://...` upstream URL for the VNC transport."""
    scheme = "wss" if origin.startswith("https://") else "ws"
    host = origin.split("://", 1)[-1].rstrip("/")
    return f"{scheme}://{host}/{normalise_stream_path(path)}"


def filter_proxy_request_headers(headers: Any) -> dict[str, str]:
    """The subset of a client's headers that may be forwarded upstream."""
    items = headers.items() if hasattr(headers, "items") else headers
    forwarded = {
        str(name).lower(): str(value)
        for name, value in items
        if str(name).lower() in STREAM_PROXY_FORWARDABLE_HEADERS
    }
    # Pinned rather than merely dropped: httpx adds its own `accept-encoding`,
    # and asking for identity is what keeps the relayed `content-length` honest.
    forwarded["accept-encoding"] = "identity"
    return forwarded


def filter_proxy_response_headers(headers: Any) -> dict[str, str]:
    """The subset of the upstream's headers that may be returned to the client."""
    items = headers.items() if hasattr(headers, "items") else headers
    filtered = {
        str(name).lower(): str(value)
        for name, value in items
        if str(name).lower() in STREAM_PROXY_RESPONSE_HEADERS
    }
    # The ticket in the URL is short-lived; a cached copy of a proxied asset
    # keyed on it is dead weight, and a shared cache holding it is worse.
    filtered["cache-control"] = "no-store"
    return filtered


def negotiate_stream_subprotocol(offered: Any) -> str | None:
    """Pick the subprotocol to speak with both sides, or None.

    noVNC offers `binary` (and historically `base64`); websockify accepts
    either. Echoing back exactly one of what the client offered is required by
    RFC 6455 - inventing one closes the socket on the client side.
    """
    wanted = [str(item) for item in (offered or [])]
    for preferred in ("binary", "base64"):
        if preferred in wanted:
            return preferred
    return wanted[0] if wanted else None
