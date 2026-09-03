"""Rehearsal — an execution context that records intent instead of performing it.

a competing agent product's documentation says of its test runs: "a test run performs real work:
it can navigate websites, change files and call connected tools." This module is
the opposite promise, and it keeps it structurally rather than by convention.

**The chokepoint.** `perform()` is the single function through which every
outbound effect in the service layer passes — connector actions, MCP tool calls,
desktop actions, and the approval steps that hold them. It is the one place that
classifies risk, runs preflight, and then either records the intent or performs
it. `_execute()` refuses outright to run while a `SimulationContext` is active,
so a new step type cannot quietly acquire a side effect: it either goes through
`perform` and is simulated, or it trips the guard. This follows the precedent
`routines._hold` set for approval stamping — one function, impossible to forget.

**Preflight is real.** A dry run that only echoes the steps back is worthless.
`assess()` validates the resolved input against each action's `input_schema`,
checks the binding, checks the desktop is up, checks the MCP server is
registered, enabled, attached and allowlisted, and checks the driver can
actually perform the action. It never makes an outbound call, and it never
fetches a secret value — a credential is *resolve-checked*, not resolved.

**One traversal.** `dry_run_routine` walks the same `routines._run_step` the
real run walks, with the effects behind this context. There is no second
implementation of step dispatch to drift out of sync.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import release_transaction
from app.models import (
    Bot,
    BotConnector,
    BotDesktop,
    BotMcp,
    Connector,
    McpServer,
    PlanRecord,
    Routine,
    StandingApproval,
    User,
)
from app.services import browser as browser_ops
from app.services import standing_approvals
from app.services import undo as undo_service
from app.services.connectors import (
    action_risk,
    action_spec,
    execute_connector_action,
    requires_approval,
    validate_action_input,
)
from app.services.desktop import DesktopManager
from app.services.mcp_registry import call_mcp_tool
from app.services.risk import classify_action_risk, classify_label_risk, max_risk, risk_rank
from app.services.secrets import parse_ref
from app.services.vendors import select_driver

logger = logging.getLogger(__name__)

#: Every effect kind that passes through the chokepoint.
KINDS = ("connector", "mcp", "desktop", "approval")

#: Connector risks that fall back to a mock rather than failing when the bot
#: has no live binding. Mirrors `connectors.execute_connector_action`.
MOCKABLE_RISKS = ("observe", "draft")

#: Desktop effects that are served by a `DesktopManager` method rather than by
#: a sidecar `/action` POST. They are still effects: classified, gated and
#: undo-logged by `perform` exactly like a click. Routing them here is what lets
#: an agent loop *look at its own screen* and *bring its own machine up* without
#: any caller holding a `DesktopManager` of its own — a caller that could take
#: its own screenshot could just as easily take its own click, and then there
#: would be two gates.
DESKTOP_SCREENSHOT = "screenshot"
DESKTOP_WINDOWS = "windows"
DESKTOP_START = "start_desktop"
DESKTOP_STOP = "stop_desktop"

#: Read the screen or the window list, or read a page through the DOM. Change
#: nothing. The browser reads are merged in from `services.browser` rather than
#: listed again — one table, and a rehearsal describes a `browser_snapshot` as
#: a read for the same reason it describes a `screenshot` as one.
DESKTOP_OBSERVATIONS = (
    DESKTOP_SCREENSHOT,
    DESKTOP_WINDOWS,
    *sorted(browser_ops.BROWSER_OBSERVATIONS),
)
#: Bring the machine up, or take it down. Their precondition is a *state* of the
#: machine rather than a running one, which is why `_assess_desktop` cannot treat
#: "not running" as a problem for them. A bot owns its own computer: it starts it
#: when it needs one and stops it when it is finished, and both of those are
#: effects through the same chokepoint as a click.
DESKTOP_LIFECYCLE = (DESKTOP_START, DESKTOP_STOP)

_desktop = DesktopManager()

_active: contextvars.ContextVar[SimulationContext | None] = contextvars.ContextVar(
    "nesq_simulation", default=None
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# What a step wants to do
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Effect:
    """One outbound effect, with its inputs already fully resolved.

    Built by the caller that knows the step shape (`routines._step_effect`,
    `approvals`), consumed only by `perform`. Frozen because the plan records
    it verbatim: what the reviewer approves must be what executes.
    """

    kind: str
    bot_id: uuid.UUID
    action: str
    input_data: dict[str, Any] = field(default_factory=dict)
    step_index: int = 0
    connector_id: str | None = None
    mcp_id: uuid.UUID | None = None
    declared_risk: str | None = None
    #: A human already cleared this exact action; skip the gate, not the log.
    pre_approved: bool = False
    run_id: uuid.UUID | None = None
    approval_id: uuid.UUID | None = None
    #: The standing permission that let this through the gate, when one did.
    #: Set by `perform` and never by a caller — it is the gate's own record of
    #: why it did not stop, and a caller able to set it could grant itself one.
    #: It also routes execution: a browser action carrying it takes the same
    #: identity re-derivation an approved action takes, so an unattended send
    #: proves what it is about to touch rather than trusting a live ref.
    standing_approval_id: uuid.UUID | None = None
    actor_user_id: uuid.UUID | None = None
    label: str = ""

    @property
    def target(self) -> str:
        if self.kind == "connector":
            return f"{self.connector_id}.{self.action}"
        if self.kind == "mcp":
            return f"mcp:{self.mcp_id}.{self.action}"
        if self.kind == "approval":
            return "approval"
        return f"desktop.{self.action}"


@dataclass(frozen=True)
class Assessment:
    """The risk class, the gate decision and everything preflight found."""

    risk: str
    requires_approval: bool
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    binding: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class EffectResult:
    """What `perform` hands back to the step dispatcher."""

    ok: bool
    risk: str
    result: dict[str, Any]
    #: True when the caller must park this action for a human. Always False
    #: while simulating, so a caller that forgets to check `simulated` still
    #: cannot create an approval row during a dry run.
    gated: bool = False
    simulated: bool = False


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedCall:
    """One effect as it *would* have happened."""

    step_index: int
    kind: str
    action: str
    input_data: dict[str, Any]
    risk: str
    requires_approval: bool
    summary: str
    connector_id: str | None = None
    mcp_id: str | None = None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    binding: dict[str, Any] | None = None
    reversible: bool = False
    undo_note: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    def intent(self) -> dict[str, Any]:
        """The subset the content hash covers: what would be done, to what.

        Deliberately excludes preflight findings and binding state — those
        describe the environment, and a plan is approved for its intent. Change
        the target or the input and the hash moves; connect a credential and it
        does not.
        """
        return {
            "step_index": self.step_index,
            "kind": self.kind,
            "connector_id": self.connector_id,
            "mcp_id": self.mcp_id,
            "action": self.action,
            "input": self.input_data,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.intent(),
            "summary": self.summary,
            "ok": self.ok,
            "problems": list(self.problems),
            "notes": list(self.notes),
            "binding": self.binding,
            "reversible": self.reversible,
            "undo_note": self.undo_note,
        }

    def simulated_result(self) -> dict[str, Any]:
        """The envelope the traversal sees in place of a real result."""
        return {
            "ok": self.ok,
            "simulated": True,
            "would_execute": self.intent(),
            "problems": list(self.problems),
            "notes": list(self.notes),
            "error": self.problems[0] if self.problems else None,
        }


@dataclass(frozen=True)
class Plan:
    """Every planned call, plus the verdict a reviewer reads first."""

    bot_id: uuid.UUID
    calls: tuple[PlannedCall, ...]
    steps: tuple[dict[str, Any], ...] = ()
    routine_id: uuid.UUID | None = None
    name: str = ""
    created_at: datetime = field(default_factory=_now)

    @property
    def content_hash(self) -> str:
        """SHA-256 over the intent of every call, in order."""
        canonical = json.dumps(
            [call.intent() for call in self.calls],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def gated(self) -> list[int]:
        return [c.step_index for c in self.calls if c.requires_approval]

    @property
    def failing(self) -> list[dict[str, Any]]:
        return [
            {"step_index": c.step_index, "problems": list(c.problems)}
            for c in self.calls
            if c.problems
        ]

    @property
    def verdict(self) -> dict[str, Any]:
        failing = self.failing
        return {
            "steps_total": len(self.calls),
            "would_gate": self.gated,
            "would_fail": failing,
            "would_execute": len([c for c in self.calls if c.ok and not c.requires_approval]),
            "reversible": [c.step_index for c in self.calls if c.reversible],
            "ok": not failing,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "bot_id": str(self.bot_id),
            "routine_id": str(self.routine_id) if self.routine_id else None,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "content_hash": self.content_hash,
            "verdict": self.verdict,
            "calls": [call.as_dict() for call in self.calls],
        }


# ---------------------------------------------------------------------------
# The context
# ---------------------------------------------------------------------------


class SimulationContext:
    """While this is active, every effect records its intent instead of running.

    Enter it with `with SimulationContext(bot_id=...) as sim:`. It is a
    `ContextVar`, so it follows the task rather than the argument list — which
    is what makes it impossible for a call site to forget it, and what makes the
    guard in `_execute` meaningful.
    """

    def __init__(
        self,
        *,
        bot_id: uuid.UUID,
        routine_id: uuid.UUID | None = None,
        name: str = "",
    ) -> None:
        self.bot_id = bot_id
        self.routine_id = routine_id
        self.name = name
        self.calls: list[PlannedCall] = []
        self._token: contextvars.Token | None = None

    def __enter__(self) -> SimulationContext:
        self._token = _active.set(self)
        return self

    def __exit__(self, *exc_info: object) -> bool:
        if self._token is not None:
            _active.reset(self._token)
            self._token = None
        return False

    def record(self, planned: PlannedCall) -> PlannedCall:
        self.calls.append(planned)
        return planned

    def plan(self, *, steps: list[dict[str, Any]] | None = None) -> Plan:
        return Plan(
            bot_id=self.bot_id,
            calls=tuple(self.calls),
            steps=tuple(steps or []),
            routine_id=self.routine_id,
            name=self.name,
        )


def active_simulation() -> SimulationContext | None:
    return _active.get()


def simulating() -> bool:
    return _active.get() is not None


def record_problem(
    *,
    step_index: int,
    kind: str,
    message: str,
    action: str = "",
) -> PlannedCall | None:
    """Record a step the traversal could not even turn into an effect.

    A malformed step is a preflight finding like any other — it must appear in
    the plan rather than vanishing, or a reviewer would approve a plan with a
    hole in it.
    """
    context = active_simulation()
    if context is None:
        return None
    return context.record(
        PlannedCall(
            step_index=step_index,
            kind=kind,
            action=action,
            input_data={},
            risk="mutate",
            requires_approval=False,
            summary=f"step {step_index}: {kind} — BLOCKED: {message}",
            problems=(message,),
        )
    )


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingCheck:
    """Whether a bot/connector binding exists and whether it would resolve.

    `resolves` is deliberately tri-state. A dry run must not fetch a secret
    value, so a Key Vault reference can only be reported as *unknown* — saying
    "yes" would be a guess and saying "no" would be a lie. An `env://` reference
    is checked by key presence alone, which reads no value.
    """

    bound: bool
    has_reference: bool = False
    resolves: bool | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bound": self.bound,
            "has_reference": self.has_reference,
            "resolves": self.resolves,
            "note": self.note,
        }


async def check_binding(db: AsyncSession, bot_id: uuid.UUID, connector_id: str) -> BindingCheck:
    """Resolve-check a bot's connector binding without reading any value."""
    rows = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id,
            BotConnector.connector_id == connector_id,
        )
    )
    link = rows.scalar_one_or_none()
    if link is None:
        return BindingCheck(False, note="this bot has no binding for the connector")
    if link.status != "connected":
        return BindingCheck(False, note=f"the binding is '{link.status}', not 'connected'")

    reference = (link.secret_ref or "").strip()
    if not reference:
        return BindingCheck(True, False, False, "the binding names no credential reference")

    parsed = parse_ref(reference)
    if parsed is None:
        return BindingCheck(True, True, False, "the credential reference is not a valid reference")
    scheme, _, name = parsed
    if scheme == "env":
        present = name in os.environ
        return BindingCheck(
            True,
            True,
            present,
            "the environment reference is set" if present else "the environment reference is unset",
        )
    return BindingCheck(
        True,
        True,
        None,
        "a Key Vault reference — a dry run does not fetch it, so resolution is unverified",
    )


async def assess(db: AsyncSession, effect: Effect) -> Assessment:
    """Classify the risk and run every preflight check. Makes no outbound call."""
    if effect.kind == "approval":
        return Assessment(risk=str(effect.declared_risk or "send"), requires_approval=True)
    if effect.kind == "desktop":
        return await _assess_desktop(db, effect)
    if effect.kind == "connector":
        return await _assess_connector(db, effect)
    if effect.kind == "mcp":
        return await _assess_mcp(db, effect)
    return Assessment(
        risk="mutate",
        requires_approval=False,
        problems=(f"unknown effect kind '{effect.kind}'",),
    )


async def _assess_desktop(db: AsyncSession, effect: Effect) -> Assessment:
    # A step may declare a risk, but only to raise it — the same escalate-only
    # rule the real path applies.
    #
    # A DOM action gets a *third* input the pixel lane has never had: the
    # accessible name of the element it is about to touch, carried on the
    # effect as `ref_label` by whoever read the snapshot. A pixel `click` is
    # named for the motion and the server has no idea what is under the cursor,
    # so the only defence was asking the model to declare it; a
    # `browser_click` on `button "Delete account"` classifies as `delete` here
    # whether the model said anything or not. Escalate-only like the other two,
    # so the worst a mis-read name can do is put a step in front of a human.
    label = browser_ops.label_in(effect.input_data)
    declared = str(effect.declared_risk or "observe")

    # Typing is not transmitting, and the payload proves which one this is.
    #
    # A model declared `send` on typing a message into LinkedIn's composer, so
    # the person was asked twice for one act: once to write the words and again
    # to press Send. Writing into a field transmits nothing. `browser_type` can
    # submit — it takes a `submit` flag that presses Enter — and when that flag
    # is set this does not apply and the declaration stands.
    #
    # This is the one place a declared risk is capped rather than obeyed, and
    # the reason is narrow: escalate-only exists so a model can protect the user
    # with something the server cannot see, and here the server sees *more* than
    # the model — it is holding the arguments. A structural fact about the
    # payload beats a guess about it. The act that does transmit is the Send
    # control, which is gated on its own and is not affected by this.
    if (
        effect.action == "browser_type"
        and not (effect.input_data or {}).get("submit")
        and risk_rank(declared) > risk_rank("mutate")
    ):
        declared = "mutate"

    risk = max_risk(
        classify_action_risk(effect.action),
        declared,
        classify_label_risk(label)
        if label and effect.action in browser_ops.BROWSER_TARGETED
        else "observe",
    )
    gated = requires_approval(risk) and not effect.pre_approved
    problems: list[str] = []
    notes: list[str] = []
    # `db.get`, not `DesktopManager.get`: the latter creates the row it cannot
    # find, and a dry run writes nothing.
    desktop = await db.get(BotDesktop, effect.bot_id)
    state = desktop.state if desktop is not None else "absent"

    if effect.action == DESKTOP_START:
        # "The desktop is not running" is this action's *reason to exist*, so
        # reporting it as a problem would make every honest cold start read as
        # a blocked step in a rehearsal.
        if state == "running":
            notes.append("the bot desktop is already running — starting it would be a no-op")
        else:
            notes.append(
                f"would start the bot desktop from '{state}' — a cold start takes 30-90s"
            )
        return Assessment(
            risk=risk, requires_approval=gated, problems=(), notes=tuple(notes)
        )

    if effect.action == DESKTOP_STOP:
        # Same reasoning inverted: stopping a desktop that is already down is a
        # no-op, not a blocked step. On the ACI driver a stop is destructive —
        # the container group and its filesystem go together — so the note says
        # so rather than letting a reviewer assume the machine can be resumed.
        if state == "running":
            notes.append(
                "would stop the bot desktop; on the ACI driver the container group and "
                "its filesystem are deleted together, so the stop is destructive"
            )
        else:
            notes.append(f"the bot desktop is '{state}' — stopping it would be a no-op")
        return Assessment(
            risk=risk, requires_approval=gated, problems=(), notes=tuple(notes)
        )

    # A URL the browser will refuse is a problem a rehearsal can show without
    # making any call at all. The sidecar enforces the same allowlist and
    # answers `400 url_not_allowed`, so this is not the boundary — it is the
    # difference between a dry run that says "this step cannot work" and one
    # that says nothing until it is run for real.
    url_problem = browser_ops.url_problem(effect.action, effect.input_data)
    if url_problem:
        problems.append(url_problem)

    if desktop is None:
        problems.append("this bot has no desktop record — the desktop has never been started")
    elif desktop.state != "running":
        problems.append(f"the bot desktop is '{desktop.state}', not running")
    elif not desktop.control_url:
        problems.append("the bot desktop is running but exposes no control URL")
    elif effect.action in DESKTOP_OBSERVATIONS:
        notes.append(f"would read the desktop's '{effect.action}' without changing anything")
    elif browser_ops.is_browser_action(effect.action):
        target = f" on {label}" if label else ""
        notes.append(
            f"would drive the desktop's browser over CDP: '{effect.action}'{target}. "
            "Chromium answering is not checked here — a wedged browser is a `503` at "
            "call time and the caller falls back to the pixel API"
        )
    else:
        notes.append(f"would send '{effect.action}' to the desktop sidecar")
    return Assessment(
        risk=risk,
        requires_approval=gated,
        problems=tuple(problems),
        notes=tuple(notes),
    )


async def _assess_connector(db: AsyncSession, effect: Effect) -> Assessment:
    connector_id = str(effect.connector_id or "")
    problems: list[str] = []
    notes: list[str] = []

    connector = await db.get(Connector, connector_id) if connector_id else None
    if connector is None:
        return Assessment(
            risk=max_risk("observe", str(effect.declared_risk or "observe")),
            requires_approval=False,
            problems=(f"connector '{connector_id}' is not registered",),
        )

    # The manifest is authoritative; a declared risk can only raise it.
    risk = max_risk(action_risk(connector, effect.action), str(effect.declared_risk or "observe"))
    gated = requires_approval(risk) and not effect.pre_approved

    if action_spec(connector, effect.action) is None:
        problems.append(f"connector '{connector_id}' has no action '{effect.action}'")
    else:
        missing = validate_action_input(connector, effect.action, effect.input_data)
        if missing:
            problems.append(f"missing required input: {', '.join(missing)}")

    binding = await check_binding(db, effect.bot_id, connector_id)
    if not binding.bound:
        if risk in MOCKABLE_RISKS or effect.pre_approved:
            notes.append(f"{binding.note} — the action would return mock data")
        else:
            problems.append(f"connector '{connector_id}' is not connected for this bot")
    else:
        notes.append(_live_path_note(connector, connector_id, effect.action, binding))

    return Assessment(
        risk=risk,
        requires_approval=gated,
        problems=tuple(problems),
        notes=tuple(notes),
        binding=binding.as_dict(),
    )


def _live_path_note(
    connector: Connector,
    connector_id: str,
    action: str,
    binding: BindingCheck,
) -> str:
    """Which of the two paths a bound connector would take, and why."""
    settings = get_settings()
    manifest = connector.manifest or {}
    driver = select_driver(connector_id)
    if not getattr(settings, "connector_live_calls", True):
        return "CONNECTOR_LIVE_CALLS is off — the action would return mock data"
    if not driver.configured(manifest, settings):
        return f"driver '{driver.name}' has no base URL configured — the action would mock"
    if not driver.supports(action, manifest):
        return f"driver '{driver.name}' describes no call for '{action}' — the action would mock"
    if binding.resolves is False:
        return f"{binding.note} — the action would mock"
    if binding.resolves is None:
        return f"{binding.note}; if it resolves, this would call the vendor for real"
    return f"driver '{driver.name}' would call the vendor for real"


async def _assess_mcp(db: AsyncSession, effect: Effect) -> Assessment:
    problems: list[str] = []
    notes: list[str] = []
    risk = max_risk(classify_action_risk(effect.action), str(effect.declared_risk or "observe"))

    mcp = await db.get(McpServer, effect.mcp_id) if effect.mcp_id else None
    if mcp is None:
        problems.append(f"MCP server '{effect.mcp_id}' is not registered")
    elif not mcp.enabled:
        problems.append(f"MCP server '{mcp.name}' is disabled")
    else:
        rows = await db.execute(
            select(BotMcp).where(BotMcp.bot_id == effect.bot_id, BotMcp.mcp_id == effect.mcp_id)
        )
        if rows.scalar_one_or_none() is None:
            problems.append(f"MCP server '{mcp.name}' is not attached to this bot")
        allow = mcp.tool_allowlist or []
        if allow and effect.action not in allow:
            problems.append(f"tool '{effect.action}' is not on '{mcp.name}''s allowlist")
        if mcp.transport in ("sse", "http") and mcp.endpoint:
            notes.append(
                f"would POST to {mcp.endpoint.rstrip('/')}/tools/call — a dry run does not "
                "probe reachability, so a live failure is still possible"
            )
        else:
            notes.append(f"transport '{mcp.transport}' has no endpoint — the call would mock")

    gated = requires_approval(risk) and not effect.pre_approved
    if gated:
        notes.append(
            f"the tool name classifies as '{risk}', so this call is held for a human before it "
            "runs — an MCP server's tools are third-party code and are gated by the same rule as "
            "a desktop step"
        )
    elif effect.pre_approved and requires_approval(risk):
        notes.append(f"classified '{risk}'; a human has already approved this call")
    return Assessment(
        risk=risk,
        requires_approval=gated,
        problems=tuple(problems),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# The chokepoint
# ---------------------------------------------------------------------------


async def perform(db: AsyncSession, effect: Effect) -> EffectResult:
    """Classify, preflight, then either record the intent or perform it.

    Every outbound effect in the service layer goes through here. There is no
    second path: `_execute` raises if it is ever reached while a simulation is
    active.
    """
    assessment = await assess(db, effect)

    # The one place a standing permission is read. It is applied *at the gate* —
    # as a recorded reason the gate did not stop — and never as a second path
    # around it: the effect still goes through `_execute` below, is still
    # classified `send`, still lands in the undo log, and now carries the id of
    # the permission that let it past.
    #
    # It is here rather than inside `assess` because `assess` is documented as
    # the pure classification, and a lookup that decides whether a human is
    # asked is a decision rather than a classification. It is *before* the
    # rehearsal branch on purpose: a dry run that reports "this would be held
    # for a person" about a step a standing permission would wave through is
    # wrong in the dangerous direction, because the reviewer concludes there is
    # a human in the loop. The lookup is a single indexed read and makes no
    # outbound call, so a rehearsal can afford the truth.
    standing: StandingApproval | None = None
    if assessment.requires_approval:
        standing = await standing_approvals.covering(db, effect=effect, risk=assessment.risk)
        if standing is not None:
            described = f'{standing.ref_role} "{standing.ref_name}"'
            assessment = replace(
                assessment,
                requires_approval=False,
                notes=(*assessment.notes, standing_approvals.gate_note(standing, described)),
            )
            effect = replace(effect, standing_approval_id=standing.id)

    context = active_simulation()
    if context is not None:
        planned = context.record(await _plan_call(db, effect, assessment))
        return EffectResult(
            ok=planned.ok,
            risk=assessment.risk,
            result=planned.simulated_result(),
            gated=False,
            simulated=True,
        )

    if assessment.requires_approval:
        return EffectResult(ok=False, risk=assessment.risk, result={}, gated=True)

    # Preflight findings are not used to short-circuit the real path. The
    # executors already refuse the same cases with their own error envelopes,
    # and duplicating that here would give the real run and the rehearsal two
    # different vocabularies for the same failure.
    prior = await undo_service.capture_prior_state(db, effect)

    # Everything above this line is reads — `assess` is the pure
    # classification, the standing lookup is one indexed select, and
    # `capture_prior_state` writes nothing — and everything below it is the
    # write. In between sits the only await in this function that talks to
    # something other than the database, and it is slow: a Bot Desktop cold
    # start is 30-90 seconds and a browser step can wait up to `timeout_ms`.
    #
    # `nesqbot-pg` terminates a backend that stays idle in a transaction
    # for 60 seconds, so holding the opening reads' transaction across that
    # call is how a desktop step loses its connection and takes the run with
    # it. See `db.release_transaction` for the incident this comes from; it
    # happened on the model-call lane first, but this is the lane every desktop
    # step goes through, several times a run.
    await release_transaction(db)

    outcome = await _execute(db, effect, assessment)
    await undo_service.record_effect(db, effect, assessment, outcome, prior=prior)
    if standing is not None:
        await standing_approvals.record_use(db, standing, effect=effect, outcome=outcome)
    return EffectResult(
        ok=bool(outcome.get("ok")),
        risk=assessment.risk,
        result=outcome,
        gated=bool(outcome.get("needs_approval")),
    )


async def _execute(db: AsyncSession, effect: Effect, assessment: Assessment) -> dict[str, Any]:
    """Perform the effect for real. The one place that is allowed to.

    The guard is not defensive programming — it is the guarantee. A future step
    type that reaches an API by some other route will trip it in the dry-run
    tests rather than quietly sending an email during a rehearsal.
    """
    if simulating():  # pragma: no cover - the guard must never fire in a passing suite
        raise RuntimeError(
            f"refusing to execute {effect.target} inside a simulation — an effect reached "
            "_execute without going through perform()"
        )

    if effect.kind == "connector":
        return await execute_connector_action(
            db,
            bot_id=effect.bot_id,
            connector_id=str(effect.connector_id),
            action=effect.action,
            input_data=effect.input_data,
            force=effect.pre_approved,
        )
    if effect.kind == "mcp":
        return await call_mcp_tool(
            db,
            bot_id=effect.bot_id,
            mcp_id=effect.mcp_id,
            tool=effect.action,
            arguments=effect.input_data,
        )
    if effect.kind == "desktop":
        return await _perform_desktop(db, effect, assessment)
    return {"ok": False, "error": f"unknown effect kind '{effect.kind}'"}


#: Screenshot capture options a caller may put on a `screenshot` effect, and
#: the `DesktopManager.screenshot` keyword each maps to. Anything else on the
#: effect's `input_data` is ignored rather than forwarded: the sidecar rejects
#: unknown query parameters, and a caller should not be able to reach into the
#: capture API by guessing names.
_SCREENSHOT_OPTIONS: dict[str, str] = {
    "format": "fmt",
    "fmt": "fmt",
    "quality": "quality",
    "max_width": "max_width",
    "grayscale": "grayscale",
}


def _screenshot_options(effect: Effect) -> dict[str, Any]:
    """Capture options off a `screenshot` effect, keyword-ready.

    Empty for a bare `screenshot`, which is what `GET
    /bots/{id}/desktop/screenshot` and any model-issued call produce, so the
    documented full-size PNG stays the default. The agent loop fills these in
    from settings because it pays for the same frame on every step.
    """
    options: dict[str, Any] = {}
    for key, keyword in _SCREENSHOT_OPTIONS.items():
        value = (effect.input_data or {}).get(key)
        if value is not None:
            options[keyword] = value
    return options


async def _perform_desktop(
    db: AsyncSession, effect: Effect, assessment: Assessment | None = None
) -> dict[str, Any]:
    """Dispatch one desktop effect to the manager capability that serves it.

    Four families, one chokepoint. Observation reads the screen or the window
    list; lifecycle brings the machine up; a `browser_*` action drives the same
    machine's Chromium over CDP; everything else is a sidecar `/action`. All
    four arrive here only through `perform`, so all four are classified, gated
    and undo-logged identically — the alternative is a caller that owns a
    `DesktopManager`, and a caller that can screenshot for itself can click for
    itself.

    The browser branch is the newest and the one most tempting to shortcut: a
    DOM click reaches a different sidecar path than a pixel click, and it would
    have been half the code to give the agent loop its own client. It gets the
    same risk gate, the same approval flow and the same undo log precisely
    because a DOM click on Send is a send.
    """
    if browser_ops.is_browser_action(effect.action):
        # A standing permission takes the *approved* path, not the ordinary one.
        #
        # The ordinary path trusts the ref the model just read off its own
        # snapshot and lets the sidecar refuse a stale one. That is right when a
        # person is watching the run and wrong when nobody is: what a standing
        # permission authorises is `button "Message"` on one page, so before it
        # is spent the element has to be *proved* to be that — one match, same
        # page, in a snapshot that is not truncated — and `resolve_approved`
        # already does exactly that and refuses rather than guessing. The
        # unattended send therefore gets a stricter proof than the attended one.
        if effect.pre_approved or effect.standing_approval_id is not None:
            return await _perform_approved_browser(db, effect)
        return await _perform_browser(db, effect, assessment)
    if effect.action == DESKTOP_SCREENSHOT:
        return await _desktop.screenshot(db, effect.bot_id, **_screenshot_options(effect))
    if effect.action == DESKTOP_WINDOWS:
        return await _desktop.windows(db, effect.bot_id)
    if effect.action == DESKTOP_START:
        bot = await db.get(Bot, effect.bot_id)
        if bot is None:
            return {"ok": False, "action": effect.action, "error": "no such bot"}
        desktop = await _desktop.start(db, bot)
        running = desktop.state == "running"
        return {
            "ok": running,
            "action": effect.action,
            "state": desktop.state,
            "error": None
            if running
            else (desktop.last_error or f"the desktop is '{desktop.state}', not running"),
        }
    if effect.action == DESKTOP_STOP:
        # `wipe` is deliberately not reachable from here. An agent may take its
        # own machine down; erasing the home directory it has been signing into
        # is a different decision, and it belongs to a human on
        # `POST /bots/{bot_id}/desktop/stop?wipe=true`.
        desktop = await _desktop.stop(db, effect.bot_id)
        stopped = desktop.state == "absent"
        return {
            "ok": stopped,
            "action": effect.action,
            "state": desktop.state,
            "error": None if stopped else (desktop.last_error or "the desktop did not stop"),
        }
    return await _desktop.computer_action(db, effect.bot_id, effect.action, effect.input_data)


#: The risk at which an ordinary DOM action stops being allowed to re-derive its
#: own reference. Nothing at or above it can reach `_execute` unapproved today —
#: `connectors.requires_approval` gates exactly send/spend/delete, so such a step
#: is either held or arrives `pre_approved` and takes the other path entirely.
#: The check below is therefore redundant, and it is written down anyway: the
#: rule is "auto-retry must never let an action skip an approval it would
#: otherwise have needed", and a rule enforced only by a coincidence in another
#: module is a rule that a future change to that module silently deletes.
RETRY_RISK_CEILING = "send"


def _may_recover_ref(effect: Effect, assessment: Assessment | None) -> bool:
    """Is this call allowed one re-derived reference?

    The instinct to allow a read and refuse a click does not survive contact
    with what the two codes mean. `stale_ref` and `unknown_ref` are raised by
    the sidecar's `resolve()` *before* it dispatches anything, so the action did
    not happen: a retry is the first attempt, not a second one, and there is no
    double-click or double-send to be had. What makes the retry safe is not the
    verb, it is the identity — the retried call acts on an element with the same
    role, the same accessible name Chrome computed, on the same page, proved
    unique in an untruncated snapshot. Refusing to retry a click does not make
    the click safer; it just makes the model take a step to do the identical
    thing with weaker evidence than this has.

    What is actually load-bearing:

    * **the risk gate has already spoken.** `_assess_desktop` classified this
      step from `ref_label` — the very same string the recovery re-resolves by —
      so a re-derived element classifies identically by construction, and
      `send`/`spend`/`delete` was held before `_execute` was ever reached. A
      recovery cannot turn a step a human would have seen into one they do not.
      `RETRY_RISK_CEILING` states that rather than relying on it.
    * **no identity, no recovery.** Handled in `browser.ref_identity`.
    * **exactly once.** One re-resolution, one retry, then whatever comes back
      is the answer. A loop here would spend the run's budget on a page that has
      simply changed.
    """
    if effect.pre_approved:  # pragma: no cover - the approved path never gets here
        return False
    risk = str((assessment.risk if assessment else "") or "observe")
    return risk_rank(risk) < risk_rank(RETRY_RISK_CEILING)


async def _perform_browser(
    db: AsyncSession, effect: Effect, assessment: Assessment | None
) -> dict[str, Any]:
    """One ordinary DOM action, with one re-derived reference if it needs it.

    The happy path is unchanged and costs nothing: the call goes out, it works,
    it comes back. Only a `409 stale_ref` / `409 unknown_ref` — the sidecar
    saying *your reference does not name a live element* — buys a snapshot and a
    second attempt, and the second attempt is aimed by
    `browser.resolve_recovered`, which can only ever point at the element the
    payload described or refuse and say why.

    Why this is worth a round trip. A real run: 33 desktop actions, 32 ran, and
    the one failure was a ref minted by the previous snapshot. Every DOM action
    can invalidate every ref, and expecting a model to track that by hand across
    a long task is a tool-design mistake, not a model failure — each miss costs a
    step, a model call and real money. Two sidecar calls are cheaper than one
    model call by a wide margin.

    On the snapshot not going back through `perform`: the same reasoning as
    `_perform_approved_browser`. It is not an effect, it is how this effect is
    carried out; the undo log still records exactly one action, and routing a
    lookup through the chokepoint would put a `browser_snapshot` in the audit
    trail as though the bot had chosen to take one.
    """
    outcome = await _desktop.browser_call(db, effect.bot_id, effect.action, effect.input_data)
    if outcome.get("ok"):
        return outcome
    if str(outcome.get("error") or "") not in browser_ops.RECOVERABLE_REF_ERRORS:
        return outcome
    if not _may_recover_ref(effect, assessment):
        return outcome

    target = browser_ops.ref_identity(effect.action, effect.input_data)
    if target is None:
        # Nothing was recorded about what that ref was, so there is nothing to
        # find again. The sidecar's own refusal is the honest answer and it
        # already tells the model to snapshot.
        return outcome

    snapshot = await _desktop.browser_call(
        db, effect.bot_id, "browser_snapshot", browser_ops.identity_snapshot_request(target)
    )
    resolved = browser_ops.resolve_recovered(target, snapshot, outcome)
    failure = resolved.get("failure")
    if failure is not None:
        return failure

    payload = resolved["payload"]
    retried = await _desktop.browser_call(db, effect.bot_id, effect.action, payload)
    if not retried.get("ok"):
        # The recovery found the element and the action still did not work —
        # `obscured` under a banner, `not_actionable` off screen. That is the
        # sidecar's own honest refusal about a *live* element and it is far more
        # useful than the stale_ref that started this, so it is what goes back.
        return retried
    return {
        **retried,
        browser_ops.RECOVERED_KEY: True,
        "recovered_from": str(outcome.get("error") or ""),
        "recovered_label": target.described,
        "requested_ref": str((effect.input_data or {}).get("ref") or ""),
    }


async def _perform_approved_browser(db: AsyncSession, effect: Effect) -> dict[str, Any]:
    """Run a DOM action a human approved, against the page as it is *now*.

    The approval a person read said `browser_click on button "Delete account"`.
    It did not say `click node e9 of snapshot s3`, and by the time they press
    Approve that snapshot is evicted and that node id belongs to something else
    — so replaying the payload verbatim earns a `409 stale_ref` almost every
    time. The refusal is correct and it is also useless: it makes approving mean
    "re-run the whole task".

    So this re-derives the reference from the identity the approval carries:
    fresh snapshot, find the element whose role and accessible name are the ones
    in the sentence the human read, act on *that*. It can only ever do the thing
    that was described or nothing — `browser.resolve_approved` has no positional
    fallback, no nearest match and no `force`, and every way it can decline
    comes back as a code with a sentence a person can act on.

    Two failure shapes are worth naming because they are honest outcomes rather
    than bugs: the tab having moved to a different page (`approved_page_changed`
    — a same-named button elsewhere is a different button), and an element that
    is now out of the viewport, which the sidecar refuses as `not_actionable`.
    The second earns exactly one `browser_scroll` of the *resolved* ref and one
    retry: scrolling commits nothing, and "the page scrolled since you approved"
    is not a reason to make a person do the task again.

    On the snapshot and the scroll not going back through `perform`: they are
    not effects, they are how *this* effect is carried out. This runs inside
    `_execute`, which is the one place allowed to reach the sidecar, and the
    thing the undo log records is still exactly one action — the click a human
    approved. Routing a lookup back through the chokepoint would put a
    `browser_snapshot` in the audit trail as though the bot had decided to take
    one.
    """
    target = browser_ops.approved_target(effect.action, effect.input_data)
    if target is None:
        # Nothing recorded to re-resolve by, so what was approved is literally
        # the payload — pinned `snapshot_id` and all. Run it unchanged and let
        # the sidecar's own checks be the answer.
        return await _desktop.browser_call(db, effect.bot_id, effect.action, effect.input_data)

    snapshot = await _desktop.browser_call(
        db, effect.bot_id, "browser_snapshot", browser_ops.identity_snapshot_request(target)
    )
    resolved = browser_ops.resolve_approved(target, snapshot)
    failure = resolved.get("failure")
    if failure is not None:
        return failure

    payload = resolved["payload"]
    outcome = await _desktop.browser_call(db, effect.bot_id, effect.action, payload)
    if outcome.get("ok") or str(outcome.get("error") or "") != "not_actionable":
        return outcome

    scrolled = await _desktop.browser_call(
        db,
        effect.bot_id,
        "browser_scroll",
        {"ref": payload["ref"]},
    )
    if not scrolled.get("ok"):
        return outcome
    return await _desktop.browser_call(db, effect.bot_id, effect.action, payload)


async def _plan_call(db: AsyncSession, effect: Effect, assessment: Assessment) -> PlannedCall:
    """Turn an assessed effect into the record a reviewer reads."""
    connector = None
    if effect.kind == "connector" and effect.connector_id:
        connector = await db.get(Connector, str(effect.connector_id))
    compensation = undo_service.describe(
        kind=effect.kind,
        connector=connector,
        connector_id=effect.connector_id,
        action=effect.action,
        input_data=effect.input_data,
        result=None,
    )
    return PlannedCall(
        step_index=effect.step_index,
        kind=effect.kind,
        action=effect.action,
        input_data=dict(effect.input_data or {}),
        risk=assessment.risk,
        requires_approval=assessment.requires_approval,
        summary=_summary_line(effect, assessment),
        connector_id=effect.connector_id,
        mcp_id=str(effect.mcp_id) if effect.mcp_id else None,
        problems=assessment.problems,
        notes=assessment.notes,
        binding=assessment.binding,
        reversible=compensation.reversible,
        undo_note=(compensation.compensator or {}).get("description") or compensation.reason,
    )


def _summary_line(effect: Effect, assessment: Assessment) -> str:
    """One line a human can read without expanding anything."""
    head = f"step {effect.step_index}: {effect.target}"
    if effect.kind == "approval":
        head = f"step {effect.step_index}: hold for approval"
    bits = [f"risk={assessment.risk}"]
    if assessment.requires_approval:
        bits.append("needs approval")
    if effect.input_data:
        keys = ", ".join(sorted(str(k) for k in effect.input_data))
        bits.append(f"input({keys})")
    if assessment.problems:
        bits.append(f"BLOCKED: {assessment.problems[0]}")
    return f"{head} — {'; '.join(bits)}"


# ---------------------------------------------------------------------------
# Dry runs
# ---------------------------------------------------------------------------


async def dry_run_routine(
    db: AsyncSession,
    routine: Routine,
    *,
    user: User | None = None,
) -> Plan:
    """Rehearse a routine. Performs nothing and writes nothing.

    Walks `routines._run_step` — the same dispatcher the real run walks — with
    the effects behind a `SimulationContext`. The traversal does not stop at the
    first gate or the first failure, because the point of a rehearsal is to see
    the whole plan, not the first problem.
    """
    # Imported here: `routines` imports this module for the chokepoint, and the
    # rehearsal API belongs with the mechanism rather than with the runner.
    from app.services import routines as routines_service

    requester = getattr(user, "id", None) or routine.owner_user_id
    steps = [s if isinstance(s, dict) else {} for s in (routine.steps or [])]
    with SimulationContext(
        bot_id=routine.bot_id,
        routine_id=routine.id,
        name=routine.name or "",
    ) as context:
        await routines_service.walk_steps(
            db,
            routine,
            None,
            requester=requester,
            halt_on_failure=False,
        )
        return context.plan(steps=steps)


async def dry_run_action(
    db: AsyncSession,
    *,
    bot_id: uuid.UUID,
    connector_id: str,
    action: str,
    input_data: dict[str, Any] | None = None,
    user: User | None = None,
) -> Plan:
    """Rehearse a single connector action, through the routine traversal.

    A one-step transient routine rather than a bespoke code path, so a single
    action and a routine step are rehearsed by exactly the same code.
    """
    from app.services import routines as routines_service

    routine = routines_service.transient_routine(
        bot_id=bot_id,
        name=f"{connector_id}.{action}",
        steps=[
            {
                "type": "connector",
                "connector_id": connector_id,
                "action": action,
                "input": dict(input_data or {}),
            }
        ],
        owner_user_id=getattr(user, "id", None),
    )
    return await dry_run_routine(db, routine, user=user)


# ---------------------------------------------------------------------------
# Persisting a plan, and executing exactly it
# ---------------------------------------------------------------------------


async def save_plan(
    db: AsyncSession,
    plan: Plan,
    *,
    user: User | None = None,
    status: str = "draft",
) -> PlanRecord:
    """Persist a plan so a human can approve *the plan* and then execute it."""
    verdict = plan.verdict
    record = PlanRecord(
        bot_id=plan.bot_id,
        routine_id=plan.routine_id,
        created_by=getattr(user, "id", None),
        name=plan.name,
        status=status,
        content_hash=plan.content_hash,
        steps=list(plan.steps),
        plan=plan.as_dict(),
        steps_total=verdict["steps_total"],
        gated_steps=len(verdict["would_gate"]),
        failing_steps=len(verdict["would_fail"]),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get_plan(db: AsyncSession, plan_id: uuid.UUID) -> PlanRecord | None:
    return await db.get(PlanRecord, plan_id)


async def list_plans(
    db: AsyncSession,
    *,
    bot_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[PlanRecord]:
    query = select(PlanRecord).order_by(PlanRecord.created_at.desc()).limit(max(1, min(limit, 500)))
    if bot_id is not None:
        query = query.where(PlanRecord.bot_id == bot_id)
    rows = await db.execute(query)
    return list(rows.scalars().all())


async def execute_plan(
    db: AsyncSession,
    record: PlanRecord,
    *,
    user: User | None = None,
) -> dict[str, Any]:
    """Execute a saved plan, but only if it still describes the same work.

    The plan is re-derived from the steps it was produced from and the hash
    recomputed. Anything that changes what would be done — an edited routine, a
    different input, a re-classified risk — moves the hash, and this refuses
    rather than executing something a human never saw. Never raises.
    """
    from app.services import routines as routines_service

    if record.status == "executed":
        return {
            "ok": False,
            "code": "already_executed",
            "error": "this plan has already been executed",
            "run_id": str(record.executed_run_id) if record.executed_run_id else None,
        }

    routine = None
    if record.routine_id is not None:
        routine = await db.get(Routine, record.routine_id)
        if routine is None:
            return {"ok": False, "code": "routine_gone", "error": "the routine no longer exists"}
        if list(routine.steps or []) != list(record.steps or []):
            await _mark_stale(db, record)
            return {
                "ok": False,
                "code": "plan_drifted",
                "error": "the routine's steps have changed since this plan was produced",
            }
    else:
        routine = routines_service.transient_routine(
            bot_id=record.bot_id,
            name=record.name,
            steps=list(record.steps or []),
            owner_user_id=record.created_by,
        )

    fresh = await dry_run_routine(db, routine, user=user)
    if fresh.content_hash != record.content_hash:
        await _mark_stale(db, record)
        return {
            "ok": False,
            "code": "plan_drifted",
            "error": "this plan no longer matches what would happen",
            "expected_hash": record.content_hash,
            "actual_hash": fresh.content_hash,
        }

    outcome = await routines_service.run_inline(db, routine, user=user)
    record.status = "executed"
    record.executed_at = _now()
    run_id = outcome.get("run_id")
    record.executed_run_id = uuid.UUID(run_id) if run_id else None
    await db.commit()
    return {"ok": outcome.get("status") != "failed", "plan_id": str(record.id), **outcome}


async def _mark_stale(db: AsyncSession, record: PlanRecord) -> None:
    record.status = "stale"
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - the refusal matters more than the bookkeeping
        logger.warning("could not mark plan %s stale: %s", record.id, exc)
        await db.rollback()
