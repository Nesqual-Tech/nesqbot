"""The undo log — every executed effect, and its inverse where one exists.

a competing agent product's documentation is explicit that "an approval controls the proposed
action, it does not reverse work already completed". This module is the answer,
and its whole value rests on one rule:

    **A compensator is recorded only when it genuinely undoes the work.**

A compensator that silently no-ops is worse than no compensator at all, because
the UI shows `reversible` to a human as a promise. So every action lands in one
of two states and never in between: reversible with a concrete compensating
call, or `reversible=False` with `irreversible_reason` saying plainly why not.
A sent email is sent.

The matrix lives in `UNDO_SPECS` for the first-party connectors and in the
per-action `undo` block of a custom connector's manifest for everything else.
`describe()` is pure and side-effect-free, which is what lets a dry-run plan
tell a reviewer up front which steps could be taken back.

Credential discipline is the same as `services.vendors`: a resolved secret
enters `call_vendor` and leaves by no other route. The AST audit in
`tests/services/test_vendors.py` covers this module.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ActionLog, AuditEvent, Connector, User
from app.services.connectors import action_risk, action_spec, execute_connector_action
from app.services.secrets import resolve_connector_secrets
from app.services.vendors import VendorCallError, call_vendor

if TYPE_CHECKING:  # pragma: no cover - annotations only, and simulation imports us
    from app.services.simulation import Assessment, Effect

logger = logging.getLogger(__name__)

#: Result keys worth remembering as the handle on what was created.
TARGET_KEYS = ("draft_id", "task_id", "reply_id", "message_id", "ticket_id", "id")

#: How many characters of a vendor result we keep in `result_summary`.
SUMMARY_CHARS = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UndoSpec:
    """How (or whether) one connector action can be taken back."""

    reversible: bool
    reason: str = ""
    description: str = ""
    #: "http" — a direct call against the connector's base URL.
    #: "restore_fields" — write previously-captured values back through the
    #: connector's own update action.
    strategy: str = ""
    method: str | None = None
    path: str | None = None
    #: Result keys the path template needs; absent keys make it not reversible.
    result_keys: tuple[str, ...] = ()
    #: A read that must succeed *before* the write for "restore_fields" to work.
    read_method: str | None = None
    read_path: str | None = None
    read_field: str | None = None
    #: Input key naming the map of fields being written (restore_fields only).
    write_field: str = "fields"


READ_ONLY = UndoSpec(
    reversible=False,
    reason="a read-only action changed nothing, so there is nothing to undo",
)

DESKTOP_SPEC = UndoSpec(
    reversible=False,
    reason=(
        "a desktop step is a keystroke or a click on a live machine — nothing records "
        "what it overwrote, so there is no inverse to replay"
    ),
)

MCP_SPEC = UndoSpec(
    reversible=False,
    reason=(
        "an MCP tool's effect is defined entirely by the server that ran it, and MCP "
        "exposes no way to describe or perform an inverse"
    ),
)

UNKNOWN_SPEC = UndoSpec(
    reversible=False,
    reason="this connector action declares no compensating call, so it cannot be undone",
)

#: The first-party reversibility matrix. Every action of every first-party
#: connector appears here — an omission would read as "unknown", and "unknown"
#: must never be mistaken for "safe".
UNDO_SPECS: dict[tuple[str, str], UndoSpec] = {
    ("microsoft_graph", "list_inbox"): READ_ONLY,
    ("microsoft_graph", "draft_reply"): UndoSpec(
        reversible=True,
        strategy="http",
        method="DELETE",
        path="/me/messages/{draft_id}",
        result_keys=("draft_id",),
        description="delete the draft reply that was created",
    ),
    ("microsoft_graph", "send_mail"): UndoSpec(
        reversible=False,
        reason=(
            "a sent email is sent — Microsoft Graph has no unsend, and the message is "
            "already in the recipient's mailbox"
        ),
    ),
    ("crm", "search_accounts"): READ_ONLY,
    ("crm", "update_fields"): UndoSpec(
        reversible=True,
        strategy="restore_fields",
        read_method="GET",
        read_path="/accounts/{account_id}",
        read_field="fields",
        write_field="fields",
        description="write the prior field values back",
    ),
    ("crm", "create_task"): UndoSpec(
        reversible=True,
        strategy="http",
        method="DELETE",
        path="/tasks/{task_id}",
        result_keys=("task_id",),
        description="delete the task that was created",
    ),
    ("ticketing", "list_open"): READ_ONLY,
    ("ticketing", "draft_reply"): UndoSpec(
        reversible=True,
        strategy="http",
        method="DELETE",
        path="/drafts/{draft_id}",
        result_keys=("draft_id",),
        description="delete the ticket draft that was created",
    ),
    ("ticketing", "send_reply"): UndoSpec(
        reversible=False,
        reason="a reply that reached the customer cannot be withdrawn",
    ),
}


def _manifest_spec(connector: Connector | None, action: str) -> UndoSpec | None:
    """An `undo` block on a custom connector's own action description."""
    spec = action_spec(connector, action) if connector else None
    block = (spec or {}).get("undo")
    if not isinstance(block, dict):
        return None
    if block.get("reversible") is False:
        return UndoSpec(
            reversible=False,
            reason=str(block.get("reason") or "the connector declares this action irreversible"),
        )
    method = block.get("method")
    path = block.get("path")
    if not method or not path:
        return None
    keys = block.get("result_keys") or _template_keys(str(path))
    return UndoSpec(
        reversible=True,
        strategy="http",
        method=str(method),
        path=str(path),
        result_keys=tuple(str(k) for k in keys),
        description=str(block.get("description") or f"{method} {path}"),
    )


def _template_keys(path: str) -> tuple[str, ...]:
    parts: list[str] = []
    rest = path
    while "{" in rest and "}" in rest:
        _, _, rest = rest.partition("{")
        key, _, rest = rest.partition("}")
        if key:
            parts.append(key)
    return tuple(parts)


def spec_for(connector: Connector | None, connector_id: str, action: str) -> UndoSpec:
    """The undo rule for one connector action. Never returns None."""
    declared = UNDO_SPECS.get((connector_id, action))
    if declared is not None:
        return declared
    from_manifest = _manifest_spec(connector, action)
    if from_manifest is not None:
        return from_manifest
    if connector is not None and action_risk(connector, action) == "observe":
        return READ_ONLY
    return UNKNOWN_SPEC


# ---------------------------------------------------------------------------
# Describing a compensator — pure, so a dry-run plan can show it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Compensation:
    """What could take one action back, or why nothing can."""

    reversible: bool
    reason: str = ""
    compensator: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reversible": self.reversible,
            "reason": self.reason,
            "compensator": self.compensator,
        }


def describe(
    *,
    kind: str,
    connector: Connector | None,
    connector_id: str | None,
    action: str,
    input_data: dict[str, Any] | None = None,
    result: Any = None,
    prior: PriorState | None = None,
) -> Compensation:
    """The compensator for one effect, or an honest reason there is none.

    Pure: it inspects the matrix, the manifest, the input and the result, and
    touches nothing. `result` is `None` when the effect has not run yet, which
    is how a dry-run plan gets a *provisional* answer — the shape of the undo
    is known from the matrix even before the handle it needs exists.
    """
    if kind == "desktop":
        return Compensation(False, DESKTOP_SPEC.reason)
    if kind == "mcp":
        return Compensation(False, MCP_SPEC.reason)
    if kind != "connector" or not connector_id:
        return Compensation(False, "only connector actions have compensators")

    spec = spec_for(connector, connector_id, action)
    if not spec.reversible:
        return Compensation(False, spec.reason)

    if spec.strategy == "restore_fields":
        return _describe_restore(spec, connector_id, action, input_data or {}, prior)
    return _describe_http(spec, connector_id, action, result)


def _describe_http(
    spec: UndoSpec,
    connector_id: str,
    action: str,
    result: Any,
) -> Compensation:
    data = result if isinstance(result, dict) else {}
    if result is None:
        return Compensation(
            True,
            "",
            {
                "kind": "http",
                "connector_id": connector_id,
                "action": action,
                "method": spec.method,
                "path": spec.path,
                "description": spec.description,
                "provisional": True,
            },
        )
    values: dict[str, str] = {}
    for key in spec.result_keys:
        value = data.get(key)
        if value is None or not str(value).strip():
            return Compensation(
                False,
                (
                    f"the {connector_id} {action} result carries no '{key}', so there is no "
                    "handle on what was created and nothing to delete"
                ),
            )
        values[key] = str(value)
    path = str(spec.path or "")
    for key, value in values.items():
        path = path.replace("{" + key + "}", value)
    return Compensation(
        True,
        "",
        {
            "kind": "http",
            "connector_id": connector_id,
            "action": action,
            "method": spec.method,
            "path": path,
            "description": spec.description,
        },
    )


def _describe_restore(
    spec: UndoSpec,
    connector_id: str,
    action: str,
    input_data: dict[str, Any],
    prior: PriorState | None,
) -> Compensation:
    if prior is None or not prior.captured:
        why = (prior.reason if prior else "") or "the prior values were not read before the write"
        return Compensation(
            False,
            f"{connector_id} {action} cannot be reversed: {why}",
        )
    restored = dict(input_data)
    restored[spec.write_field] = prior.data
    return Compensation(
        True,
        "",
        {
            "kind": "connector_action",
            "connector_id": connector_id,
            "action": action,
            "input": restored,
            "description": spec.description,
        },
    )


def reversibility_matrix() -> list[dict[str, Any]]:
    """The full first-party matrix, for docs, the UI and tests."""
    rows = [
        {
            "connector_id": connector_id,
            "action": action,
            "reversible": spec.reversible,
            "compensator": spec.description or None,
            "reason": spec.reason or None,
        }
        for (connector_id, action), spec in sorted(UNDO_SPECS.items())
    ]
    rows.append(
        {
            "connector_id": None,
            "action": "desktop",
            "reversible": False,
            "compensator": None,
            "reason": DESKTOP_SPEC.reason,
        }
    )
    rows.append(
        {
            "connector_id": None,
            "action": "mcp",
            "reversible": False,
            "compensator": None,
            "reason": MCP_SPEC.reason,
        }
    )
    return rows


# ---------------------------------------------------------------------------
# Capturing prior state — before the write, or not at all
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorState:
    """What a field-restoring action looked like before it ran."""

    captured: bool
    data: dict[str, Any] | None = None
    reason: str = ""


def _connector_base_url(connector: Connector | None, connector_id: str, settings: Any) -> str:
    manifest = (connector.manifest if connector else None) or {}
    if connector_id == "microsoft_graph":
        configured = manifest.get("base_url") or getattr(settings, "graph_api_base_url", "")
    else:
        configured = manifest.get("base_url") or ""
    return str(configured or "").strip().rstrip("/")


async def capture_prior_state(db: AsyncSession, effect: Effect) -> PriorState:
    """Read the values a write is about to overwrite. Called *before* it runs.

    Returns `captured=False` with a reason whenever the read cannot be made —
    which is the normal case for a connector that mocks, because nothing real
    is being overwritten there either. The caller records that reason verbatim
    rather than claiming a reversibility it does not have.
    """
    if effect.kind != "connector" or not effect.connector_id:
        return PriorState(False, None, "only connector writes capture prior state")

    connector = await db.get(Connector, str(effect.connector_id))
    spec = spec_for(connector, str(effect.connector_id), effect.action)
    if spec.strategy != "restore_fields":
        return PriorState(False, None, "this action does not restore prior values")

    settings = get_settings()
    base = _connector_base_url(connector, str(effect.connector_id), settings)
    if not base:
        return PriorState(
            False,
            None,
            (
                f"the {effect.connector_id} connector has no base URL in this deployment, so its "
                "prior values cannot be read — the write only reaches the mock"
            ),
        )
    if not getattr(settings, "connector_live_calls", True):
        return PriorState(False, None, "live connector calls are switched off, so nothing was read")

    wanted = effect.input_data.get(spec.write_field)
    if not isinstance(wanted, dict) or not wanted:
        return PriorState(False, None, "the write names no fields, so there is nothing to restore")

    path = str(spec.read_path or "")
    for key in _template_keys(path):
        value = effect.input_data.get(key)
        if value is None:
            return PriorState(False, None, f"the input carries no '{key}' to read the prior state by")
        path = path.replace("{" + key + "}", str(value))

    values = await resolve_connector_secrets(db, effect.bot_id, str(effect.connector_id))
    try:
        response = await call_vendor(
            method=str(spec.read_method or "GET"),
            url=f"{base}{path}",
            auth=str((connector.auth if connector else "none") or "none"),
            credential=values.get("secret"),
            label=f"{effect.connector_id} prior-state read",
            timeout_seconds=float(getattr(settings, "request_timeout_seconds", 60.0)),
            api_key_header=str(
                ((connector.manifest if connector else None) or {}).get("api_key_header")
                or "X-API-Key"
            ),
        )
    except VendorCallError as exc:
        return PriorState(False, None, f"the prior-state read failed ({exc})")

    body = response.data if isinstance(response.data, dict) else {}
    holder = body.get(spec.read_field) if spec.read_field else body
    if not isinstance(holder, dict):
        holder = body
    snapshot = {key: holder.get(key) for key in wanted}
    if not snapshot:
        return PriorState(False, None, "the prior-state read returned none of the fields being written")
    return PriorState(True, snapshot, "")


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _target_ref(result: Any) -> str | None:
    data = result if isinstance(result, dict) else {}
    inner = data.get("result") if isinstance(data.get("result"), dict) else data
    for key in TARGET_KEYS:
        value = (inner or {}).get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _summarise(outcome: dict[str, Any]) -> dict[str, Any]:
    """A small, JSON-safe record of what came back. Never the whole payload."""
    summary: dict[str, Any] = {"ok": bool(outcome.get("ok"))}
    for key in ("mock", "error", "status", "authenticated", "needs_approval"):
        if key in outcome:
            summary[key] = outcome[key]
    result = outcome.get("result")
    if isinstance(result, dict):
        summary["result"] = {k: v for k, v in result.items() if len(str(v)) <= SUMMARY_CHARS}
    elif isinstance(result, list):
        summary["result"] = {"items": len(result)}
    elif result is not None:
        summary["result"] = {"value": str(result)[:SUMMARY_CHARS]}
    return summary


async def record_effect(
    db: AsyncSession,
    effect: Effect,
    assessment: Assessment,
    outcome: dict[str, Any],
    *,
    prior: PriorState | None = None,
) -> ActionLog | None:
    """Write the undo-log entry for one effect that just executed.

    Called from the one chokepoint in `services.simulation.perform`, so a step
    type cannot be added without landing here. Never raises: a failure to write
    the log must not fail the work that already happened, but it is logged
    loudly because a missing entry is a missing undo.
    """
    if effect.kind == "approval":
        return None

    connector = None
    if effect.kind == "connector" and effect.connector_id:
        connector = await db.get(Connector, str(effect.connector_id))

    result = outcome.get("result") if isinstance(outcome, dict) else None
    if not outcome.get("ok"):
        compensation = Compensation(
            False,
            "the action did not complete, so there is nothing to take back",
        )
    else:
        compensation = describe(
            kind=effect.kind,
            connector=connector,
            connector_id=effect.connector_id,
            action=effect.action,
            input_data=effect.input_data,
            result=result,
            prior=prior,
        )

    entry = ActionLog(
        bot_id=effect.bot_id,
        run_id=effect.run_id,
        approval_id=effect.approval_id,
        standing_approval_id=effect.standing_approval_id,
        actor_user_id=effect.actor_user_id,
        kind=effect.kind,
        connector_id=effect.connector_id,
        mcp_id=effect.mcp_id,
        action=effect.action,
        risk=assessment.risk,
        target_ref=_target_ref(outcome),
        input_data=effect.input_data or {},
        result_summary=_summarise(outcome if isinstance(outcome, dict) else {}),
        ok=bool(outcome.get("ok")),
        reversible=compensation.reversible,
        irreversible_reason=compensation.reason or None,
        compensator=compensation.compensator or {},
    )
    try:
        db.add(entry)
        await db.flush()
    except Exception as exc:  # noqa: BLE001 - the work already happened; do not undo it here
        logger.error(
            "could not write the undo-log entry for %s %s: %s", effect.kind, effect.action, exc
        )
        return None
    return entry


async def list_action_log(
    db: AsyncSession,
    *,
    bot_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    reversible_only: bool = False,
    limit: int = 50,
) -> list[ActionLog]:
    """Recent entries, newest first — what the "take it back" view reads."""
    query = select(ActionLog).order_by(ActionLog.created_at.desc()).limit(max(1, min(limit, 500)))
    if bot_id is not None:
        query = query.where(ActionLog.bot_id == bot_id)
    if run_id is not None:
        query = query.where(ActionLog.run_id == run_id)
    if reversible_only:
        query = query.where(ActionLog.reversible.is_(True), ActionLog.undone.is_(False))
    rows = await db.execute(query)
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Undoing
# ---------------------------------------------------------------------------


async def undo(db: AsyncSession, action_log_id: uuid.UUID, *, user: User | None = None) -> dict:
    """Run the compensator for one logged action. Never raises.

    Undoing is itself an action: it is audited, it is idempotent, and it refuses
    to run twice. The refusal is not a read-then-write check — the entry is
    *claimed* with a conditional UPDATE before the compensator runs, so two
    concurrent callers cannot both perform it. A compensator that fails releases
    the claim so the undo can be retried.
    """
    entry = await db.get(ActionLog, action_log_id)
    if entry is None:
        return {"ok": False, "code": "not_found", "error": "action log entry not found"}
    if entry.undone:
        return {
            "ok": False,
            "code": "already_undone",
            "error": "this action has already been undone",
            "undone_at": entry.undone_at.isoformat() if entry.undone_at else None,
        }
    if not entry.reversible or not entry.compensator:
        return {
            "ok": False,
            "code": "not_reversible",
            "error": "this action cannot be undone",
            "reason": entry.irreversible_reason,
        }

    actor = getattr(user, "id", None)
    claimed = await db.execute(
        update(ActionLog)
        .where(ActionLog.id == entry.id, ActionLog.undone.is_(False))
        .values(undone=True, undone_at=_now(), undone_by=actor)
        .returning(ActionLog.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.first() is None:
        return {
            "ok": False,
            "code": "already_undone",
            "error": "this action has already been undone",
        }
    db.expire(entry)
    await db.refresh(entry)

    try:
        result = await _run_compensator(db, entry)
    except Exception as exc:  # noqa: BLE001 - an undo must never 500 the API
        logger.exception("compensator for action log %s failed", entry.id)
        result = {"ok": False, "error": str(exc)}

    succeeded = bool(result.get("ok"))
    entry.undo_result = result
    if not succeeded:
        # Release the claim: a failed undo must stay retryable.
        entry.undone = False
        entry.undone_at = None
        entry.undone_by = None

    db.add(
        AuditEvent(
            actor_user_id=actor,
            bot_id=entry.bot_id,
            event_type="action_undone" if succeeded else "action_undo_failed",
            detail={
                "action_log_id": str(entry.id),
                "kind": entry.kind,
                "connector_id": entry.connector_id,
                "action": entry.action,
                "target_ref": entry.target_ref,
                "ok": succeeded,
                "error": result.get("error"),
            },
        )
    )
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record the undo of %s: %s", entry.id, exc)
        await db.rollback()

    return {
        "ok": succeeded,
        "action_log_id": str(entry.id),
        "kind": entry.kind,
        "action": entry.action,
        "compensator": entry.compensator.get("description"),
        "result": result,
        "error": None if succeeded else result.get("error"),
    }


async def _run_compensator(db: AsyncSession, entry: ActionLog) -> dict:
    plan = entry.compensator or {}
    kind = plan.get("kind")
    if kind == "connector_action":
        # force=True: undoing an approved action does not need a second approval,
        # and the compensator is narrower than the action it reverses.
        result = await execute_connector_action(
            db,
            bot_id=entry.bot_id,
            connector_id=str(plan.get("connector_id")),
            action=str(plan.get("action")),
            input_data=dict(plan.get("input") or {}),
            force=True,
        )
        return {"ok": bool(result.get("ok")), "error": result.get("error"), "result": result}
    if kind == "http":
        return await _run_http_compensator(db, entry, plan)
    return {"ok": False, "error": f"unknown compensator kind '{kind}'"}


async def _run_http_compensator(db: AsyncSession, entry: ActionLog, plan: dict) -> dict:
    connector_id = str(plan.get("connector_id") or "")
    connector = await db.get(Connector, connector_id) if connector_id else None
    settings = get_settings()
    base = _connector_base_url(connector, connector_id, settings)

    if not base or not getattr(settings, "connector_live_calls", True):
        # The forward call mocked for exactly the same reason, so nothing real
        # was created. Saying so is honest; pretending we deleted it is not.
        return {
            "ok": True,
            "mock": True,
            "detail": (
                f"{connector_id} has no live endpoint in this deployment — the action it "
                "reverses did not reach a vendor either"
            ),
        }

    values = await resolve_connector_secrets(db, entry.bot_id, connector_id)
    try:
        response = await call_vendor(
            method=str(plan.get("method") or "DELETE"),
            url=f"{base}{plan.get('path')}",
            auth=str((connector.auth if connector else "none") or "none"),
            credential=values.get("secret"),
            label=f"undo {connector_id} {entry.action}",
            timeout_seconds=float(getattr(settings, "request_timeout_seconds", 60.0)),
            api_key_header=str(
                ((connector.manifest if connector else None) or {}).get("api_key_header")
                or "X-API-Key"
            ),
        )
    except VendorCallError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status}
    return {"ok": True, "status": response.status, "detail": plan.get("description")}
