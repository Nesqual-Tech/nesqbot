"""First-party connectors + custom connector registry."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import BotConnector, Connector
from app.services.secrets import resolve_connector_secrets
from app.services.vendors import VendorOutcome, select_driver

logger = logging.getLogger(__name__)

FIRST_PARTY: list[dict[str, Any]] = [
    {
        "id": "microsoft_graph",
        "name": "Microsoft Graph (Mail & Calendar)",
        "version": "1.0.0",
        "auth": "oauth2",
        "scopes": ["Mail.Read", "Mail.Send", "Calendars.Read", "User.Read"],
        "risk_default": "observe",
        "first_party": True,
        "actions": [
            {"name": "list_inbox", "description": "List recent inbox messages", "risk": "observe", "input_schema": {"type": "object", "properties": {"top": {"type": "integer"}}}},
            {"name": "draft_reply", "description": "Create a draft reply", "risk": "draft", "input_schema": {"type": "object", "properties": {"message_id": {"type": "string"}, "body": {"type": "string"}}, "required": ["message_id", "body"]}},
            {"name": "send_mail", "description": "Send an email", "risk": "send", "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
        ],
    },
    {
        "id": "crm",
        "name": "CRM",
        "version": "1.0.0",
        "auth": "oauth2",
        "scopes": ["crm.read", "crm.write"],
        "risk_default": "observe",
        "first_party": True,
        "actions": [
            {"name": "search_accounts", "description": "Search accounts", "risk": "observe", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
            {"name": "update_fields", "description": "Update CRM fields", "risk": "mutate", "input_schema": {"type": "object", "properties": {"account_id": {"type": "string"}, "fields": {"type": "object"}}, "required": ["account_id", "fields"]}},
            {"name": "create_task", "description": "Create follow-up task", "risk": "mutate", "input_schema": {"type": "object", "properties": {"account_id": {"type": "string"}, "title": {"type": "string"}}, "required": ["account_id", "title"]}},
        ],
    },
    {
        "id": "ticketing",
        "name": "Support Ticketing",
        "version": "1.0.0",
        "auth": "api_key",
        "scopes": [],
        "risk_default": "observe",
        "first_party": True,
        "actions": [
            {"name": "list_open", "description": "List open tickets", "risk": "observe", "input_schema": {"type": "object", "properties": {}}},
            {"name": "draft_reply", "description": "Draft ticket reply", "risk": "draft", "input_schema": {"type": "object", "properties": {"ticket_id": {"type": "string"}, "body": {"type": "string"}}, "required": ["ticket_id", "body"]}},
            {"name": "send_reply", "description": "Send ticket reply", "risk": "send", "input_schema": {"type": "object", "properties": {"ticket_id": {"type": "string"}, "body": {"type": "string"}}, "required": ["ticket_id", "body"]}},
        ],
    },
]


async def seed_connectors(db: AsyncSession) -> None:
    for spec in FIRST_PARTY:
        existing = await db.get(Connector, spec["id"])
        if existing:
            continue
        db.add(
            Connector(
                id=spec["id"],
                name=spec["name"],
                version=spec["version"],
                auth=spec["auth"],
                scopes=spec["scopes"],
                actions=spec["actions"],
                risk_default=spec["risk_default"],
                first_party=spec["first_party"],
                manifest=spec,
            )
        )
    await db.commit()


async def list_connectors(db: AsyncSession) -> list[Connector]:
    result = await db.execute(select(Connector).order_by(Connector.name))
    return list(result.scalars().all())


def action_risk(connector: Connector, action: str) -> str:
    for a in connector.actions or []:
        if a.get("name") == action:
            return a.get("risk", connector.risk_default)
    return connector.risk_default


def requires_approval(risk: str) -> bool:
    return risk in ("send", "spend", "delete")


def action_spec(connector: Connector, action: str) -> dict[str, Any] | None:
    for a in connector.actions or []:
        if a.get("name") == action:
            return a
    return None


def validate_action_input(
    connector: Connector,
    action: str,
    input_data: dict[str, Any],
) -> list[str]:
    """Return the names of `input_schema.required` keys that are missing or blank."""
    spec = action_spec(connector, action)
    if not spec:
        return []
    schema = spec.get("input_schema") or {}
    required = schema.get("required") or []
    data = input_data or {}
    missing: list[str] = []
    for key in required:
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing


async def execute_connector_action(
    db: AsyncSession,
    *,
    bot_id,
    connector_id: str,
    action: str,
    input_data: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    """Run a connector action.

    `force=True` bypasses the risk gate — used only after a human approved the
    held action through `services.approvals.execute_approved`.
    """
    connector = await db.get(Connector, connector_id)
    if not connector:
        return {"ok": False, "error": "connector not found"}

    if action_spec(connector, action) is None:
        return {"ok": False, "error": f"unknown action '{action}' for {connector_id}"}

    missing = validate_action_input(connector, action, input_data)
    if missing:
        return {
            "ok": False,
            "error": "missing required input",
            "missing": missing,
            "connector_id": connector_id,
            "action": action,
        }

    risk = action_risk(connector, action)
    if requires_approval(risk) and not force:
        return {
            "ok": False,
            "needs_approval": True,
            "risk": risk,
            "connector_id": connector_id,
            "action": action,
            "input": input_data,
        }

    result = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id,
            BotConnector.connector_id == connector_id,
        )
    )
    link = result.scalar_one_or_none()
    if not link or link.status != "connected":
        # Dev mode: allow observe/draft mock without connection
        if risk in ("observe", "draft") or force:
            return {
                "ok": True,
                "mock": True,
                "connector": connector_id,
                "action": action,
                "input": input_data,
                "result": _mock_result(connector_id, action, input_data),
            }
        return {"ok": False, "error": "connector not connected", "needs_auth": True}

    # Real path: resolve the bound credential, then call the vendor API.
    # `credentials` stays local to this frame — it is deliberately never merged
    # into the returned dict, which is echoed into audit events and responses.
    credentials = await resolve_connector_secrets(db, bot_id, connector_id)
    if not credentials:
        # Bound but unresolvable (no Key Vault access, missing secret): fall back
        # to the mock so local and half-configured environments keep working.
        logger.info(
            "no resolvable secret for %s/%s — using mock result",
            connector_id,
            action,
        )

    outcome = await _invoke_vendor(
        connector_id,
        action,
        input_data,
        credentials=credentials,
        manifest=connector.manifest or {},
    )
    if not outcome.ok:
        failure: dict[str, Any] = {
            "ok": False,
            "error": outcome.error,
            "connector": connector_id,
            "action": action,
        }
        if outcome.status is not None:
            failure["status"] = outcome.status
        return failure

    envelope: dict[str, Any] = {
        "ok": True,
        "connector": connector_id,
        "action": action,
        "input": input_data,
        "authenticated": bool(credentials),
        "result": outcome.result,
    }
    if not outcome.live:
        envelope["mock"] = True
    return envelope


async def _invoke_vendor(
    connector_id: str,
    action: str,
    input_data: dict[str, Any],
    *,
    credentials: dict[str, str],
    manifest: dict[str, Any] | None = None,
    settings: Any | None = None,
) -> VendorOutcome:
    """Call the vendor API for a connected connector, or mock.

    The live path runs only when all three hold:

      1. a credential resolved for this bot/connector binding;
      2. live calls are not switched off (`CONNECTOR_LIVE_CALLS`);
      3. the connector's driver is configured — a base URL from settings for
         Microsoft Graph, a `base_url` in the manifest for everything else —
         and knows how to perform this action.

    Anything else returns `_mock_result`, whose shapes the drivers normalise
    onto. Which path ran is logged at debug level, because that is the first
    question anyone debugging a connector asks.

    Must never place a credential value in its return value.
    """
    settings = settings or get_settings()
    manifest = manifest or {}
    driver = select_driver(connector_id)
    live_enabled = bool(getattr(settings, "connector_live_calls", True))
    configured = driver.configured(manifest, settings)
    supported = driver.supports(action, manifest)
    live = bool(credentials) and live_enabled and configured and supported

    logger.debug(
        "connector %s/%s -> %s path (driver=%s, credential=%s, live_calls=%s, "
        "configured=%s, supported=%s)",
        connector_id,
        action,
        "live" if live else "mock",
        driver.name,
        bool(credentials),
        live_enabled,
        configured,
        supported,
    )

    if not live:
        return VendorOutcome.success(_mock_result(connector_id, action, input_data), live=False)

    # Assigned, then returned: the credential goes *into* the driver call and
    # only the outcome comes back out, which is what the leak audit checks.
    outcome = await driver.invoke(
        connector_id=connector_id,
        action=action,
        input_data=input_data,
        credential=credentials["secret"],
        manifest=manifest,
        settings=settings,
    )
    return outcome


def _mock_result(connector_id: str, action: str, input_data: dict) -> Any:
    data = input_data or {}

    if connector_id == "microsoft_graph":
        if action == "list_inbox":
            return [
                {"id": "m1", "from": "vendor@example.com", "subject": "Invoice #4421", "snippet": "Please find attached..."},
                {"id": "m2", "from": "lead@acme.com", "subject": "Pricing question", "snippet": "Can you send a quote?"},
            ]
        if action == "draft_reply":
            return {
                "draft_id": f"draft_{data.get('message_id', 'unknown')}",
                "message_id": data.get("message_id"),
                "body": data.get("body", ""),
                "state": "draft",
                "sent": False,
            }
        if action == "send_mail":
            return {
                "message_id": f"sent_{abs(hash((data.get('to'), data.get('subject')))) % 10**8:08d}",
                "to": data.get("to"),
                "subject": data.get("subject"),
                "state": "sent",
                "sent": True,
            }

    if connector_id == "crm":
        if action == "search_accounts":
            q = data.get("query", "")
            return [{"id": "acc_1", "name": f"Acme ({q})", "owner": "you", "stage": "Qualified"}]
        if action == "update_fields":
            fields = data.get("fields") or {}
            return {
                "account_id": data.get("account_id"),
                "updated_fields": sorted(fields.keys()),
                "state": "updated",
            }
        if action == "create_task":
            return {
                "task_id": f"task_{abs(hash(data.get('title'))) % 10**6:06d}",
                "account_id": data.get("account_id"),
                "title": data.get("title"),
                "state": "open",
            }

    if connector_id == "ticketing":
        if action == "list_open":
            return [{"id": "t-100", "subject": "Login timeout", "priority": "high"}]
        if action == "draft_reply":
            return {
                "draft_id": f"tdraft_{data.get('ticket_id', 'unknown')}",
                "ticket_id": data.get("ticket_id"),
                "body": data.get("body", ""),
                "state": "draft",
                "sent": False,
            }
        if action == "send_reply":
            return {
                "ticket_id": data.get("ticket_id"),
                "reply_id": f"reply_{abs(hash(data.get('body'))) % 10**6:06d}",
                "state": "sent",
                "sent": True,
            }

    return {"status": "ok", "echo": data}
