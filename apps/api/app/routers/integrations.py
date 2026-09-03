"""Connector catalog + bindings, and the MCP server registry."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.errors import AppError
from app.models import AuditEvent, BotConnector, BotMcp, Connector, McpServer, User
from app.routers.deps import (
    REQUESTED_BY_KEY,
    create_gated_approval,
    get_visible_bot,
)
from app.schemas import (
    BindConnectorIn,
    BotConnectorOut,
    ConnectorOut,
    ConnectorSecretIn,
    ConnectorSecretOut,
    ExecuteActionIn,
    McpCallIn,
    McpOut,
    McpToolsOut,
    OkOut,
    PendingApprovalOut,
    RegisterConnectorIn,
    RegisterMcpIn,
    UpdateMcpIn,
)
from app.services import mcp_registry, provider_credentials, secrets, simulation
from app.services.connectors import (
    action_risk,
    list_connectors,
    requires_approval,
)
from app.services.simulation import Effect

logger = logging.getLogger("nesqbot.integrations")

router = APIRouter(tags=["integrations"])


# ---------------------------------------------------------------------------
# Connector catalog
# ---------------------------------------------------------------------------


@router.get("/integrations/connectors", response_model=list[ConnectorOut])
async def integrations_connectors(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Connector]:
    return await list_connectors(db)


@router.post("/integrations/connectors", response_model=ConnectorOut)
async def register_custom_connector(
    body: RegisterConnectorIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Connector:
    existing = await db.get(Connector, body.id)
    if existing:
        raise AppError(400, "connector_exists", "connector id already exists")
    row = Connector(
        id=body.id,
        name=body.name,
        version=body.version,
        auth=body.auth,
        scopes=body.scopes,
        actions=body.actions,
        risk_default=body.risk_default,
        first_party=False,
        manifest=body.model_dump(),
    )
    db.add(row)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="connector_registered",
            detail={"connector_id": body.id},
        )
    )
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/integrations/connectors/{connector_id}", response_model=OkOut)
async def delete_connector(
    connector_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    """Remove a custom connector. First-party connectors are permanent."""
    connector = await db.get(Connector, connector_id)
    if connector is None:
        raise AppError(404, "connector_not_found", "Connector not found")
    if connector.first_party:
        raise AppError(
            403,
            "connector_first_party",
            "First-party connectors cannot be deleted",
        )
    await db.delete(connector)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="connector_deleted",
            detail={"connector_id": connector_id},
        )
    )
    await db.commit()
    return OkOut(ok=True, detail="deleted")


# ---------------------------------------------------------------------------
# Connector bindings
# ---------------------------------------------------------------------------


@router.get("/bots/{bot_id}/connectors", response_model=list[BotConnectorOut])
async def list_bot_connectors(
    bot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[BotConnectorOut]:
    await get_visible_bot(db, bot_id, user)
    rows = await db.execute(
        select(BotConnector, Connector)
        .join(Connector, Connector.id == BotConnector.connector_id, isouter=True)
        .where(BotConnector.bot_id == bot_id)
        .order_by(BotConnector.connector_id)
    )
    out: list[BotConnectorOut] = []
    for link, connector in rows.all():
        out.append(
            BotConnectorOut(
                bot_id=link.bot_id,
                connector_id=link.connector_id,
                name=connector.name if connector else link.connector_id,
                status=link.status,
                secret_ref=link.secret_ref,
                secret_backend=secrets.describe_backend(link.secret_ref),
                risk_default=connector.risk_default if connector else "observe",
                first_party=bool(connector.first_party) if connector else False,
                actions=list(connector.actions or []) if connector else [],
            )
        )
    return out


def _app_secret_key(bot_id: uuid.UUID, connector_id: str) -> str:
    """The `provider_credentials` row key behind an `app://` reference.

    One definition, used by the write (`secrets.store_connector_secret` builds
    the same string) and by every path that has to throw the value away again.
    """
    return f"connector/{bot_id}/{connector_id}"


async def _discard_stored_secret(
    db: AsyncSession, bot_id: uuid.UUID, connector_id: str, ref: str | None
) -> None:
    """Drop an app-encrypted value whose binding no longer points at it.

    Only the `app://` case: a `kv://` secret is the vault's to keep (versions
    are its whole rotation story, and this identity generally cannot delete
    there anyway). Without this, unbinding a connector left the credential
    encrypted in the database with nothing referencing it — invisible, and
    still decryptable by anyone holding `JWT_SECRET`.
    """
    if secrets.describe_backend(ref) != "app_encrypted":
        return
    await provider_credentials.delete_app_secret(db, key=_app_secret_key(bot_id, connector_id))
    secrets.forget(ref or "")


async def _validated_secret_ref(
    value: str | None, db: AsyncSession | None = None
) -> str | None:
    """Refuse a credential pasted into the field that holds references.

    `inbound.py` already learned this for the identically-shaped column on
    inbound sources: a value typed here is stored in plaintext and then echoed
    back by `GET /bots/{bot_id}/connectors` to every user who can see the bot,
    forever. This route is the more likely trap of the two, because until now
    the *only* input the app offered was labelled "Secret ref" — somebody who
    wants to hand over a key has nowhere else to put it. Now they do, and the
    error says where.

    One deliberate behaviour change: a bare name (`my-secret`) parses only when
    `AZURE_KEY_VAULT_URL` is set, so on a deployment with no vault it is now a
    422 instead of a stored ref. That ref could never have resolved there —
    `parse_ref` returns None for it and `resolve_secret` gives up — so what is
    lost is the ability to save something that never worked.
    """
    ref = (value or "").strip()
    if not ref:
        return None

    # Two checks, not one, because "this is not a reference" and "this names
    # nothing" are different mistakes with the same cause.
    #
    # The shape check alone was not enough, and the way it failed is worth
    # keeping written down: `parse_ref` accepts a bare name against the default
    # vault, so on any deployment with `AZURE_KEY_VAULT_URL` set — which is
    # every real one — `sk-live-51H8xQ2eZvKYlo0hunter2` parsed happily as "a
    # secret called sk-live-51H8xQ2eZvKYlo0hunter2" and was stored in
    # `bot_connectors.secret_ref`, which `GET /bots/{id}/connectors` hands to
    # every user who can see the bot. The guard only ever fired on laptops with
    # no vault. Two tests said so and were right to fail.
    #
    # There is no shape rule that fixes it: a pasted key and a real secret name
    # are both `^[0-9a-zA-Z-]{1,127}$`, and prefix or entropy rules eventually
    # refuse a name somebody chose on purpose. So the vault is asked whether the
    # name exists — see `secrets.check_ref`.
    verdict = await secrets.check_ref(ref, db)
    if verdict.ok:
        return ref

    where = (
        "To hand the credential to Nesq Bot instead, POST it to "
        "/bots/{bot_id}/connectors/{connector_id}/secret and it will be stored "
        "server-side, leaving only a reference here."
    )
    if verdict.reason == "unparsable":
        raise AppError(
            422,
            "invalid_secret_ref",
            "secret_ref must be a reference to a secret (env://NAME, kv://vault/name, "
            "or a bare name when AZURE_KEY_VAULT_URL is configured) — never the secret "
            f"itself. {where}",
        )
    if verdict.reason == "missing":
        raise AppError(
            422,
            "invalid_secret_ref",
            "secret_ref looks like a reference but nothing by that name exists to "
            "resolve — check the spelling, or, if what you pasted is the credential "
            f"itself, do not store it here. {where}",
        )
    # Unverifiable: fail closed. Storing an unchecked value is how a credential
    # ends up published to the tenant, and an admin can retry a bind.
    raise AppError(
        422,
        "invalid_secret_ref",
        "secret_ref could not be checked: the vault did not answer, or this "
        "deployment has no credential to read it with. Nothing was saved rather "
        f"than saving something unverified. {where}",
    )


@router.post("/bots/{bot_id}/connectors/{connector_id}")
async def bind_connector(
    bot_id: uuid.UUID,
    connector_id: str,
    body: BindConnectorIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await get_visible_bot(db, bot_id, user)
    secret_ref = await _validated_secret_ref(body.secret_ref, db)
    row = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id, BotConnector.connector_id == connector_id
        )
    )
    link = row.scalar_one_or_none()
    previous_ref = link.secret_ref if link else None
    if not link:
        link = BotConnector(bot_id=bot_id, connector_id=connector_id)
        db.add(link)
    link.secret_ref = secret_ref
    link.status = body.status
    await db.commit()
    if previous_ref and previous_ref != secret_ref:
        secrets.forget(previous_ref)
        await _discard_stored_secret(db, bot_id, connector_id, previous_ref)
    return {"ok": True, "status": link.status}


@router.post("/bots/{bot_id}/connectors/{connector_id}/secret", response_model=ConnectorSecretOut)
async def set_connector_secret(
    bot_id: uuid.UUID,
    connector_id: str,
    body: ConnectorSecretIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConnectorSecretOut:
    """Accept the credential itself and store it server-side.

    The thing this product could not do until now: every connector credential
    had to be created in Key Vault by hand and referenced here. Key Vault is
    still the preferred destination — `store_connector_secret` tries it first —
    but the write needs the "Key Vault Secrets Officer" role and the deployed
    identity holds the read-only "Key Vault Secrets User", so the realistic
    outcome today is the encrypted-in-Postgres fallback. The response says
    which one happened, and `GET /bots/{bot_id}/connectors` keeps saying it
    afterwards (`secret_backend`).

    POST rather than PUT for the reason written out at
    `routers/bots.py:set_provider_credential`: the Container Apps ingress
    `corsPolicy.allowedMethods` in `infra/azure/main.bicep` answers a PUT
    preflight before FastAPI ever sees it.

    The value goes no further than `store_connector_secret`. It is not
    returned, not logged, and not put in the audit row — the audit row records
    that a credential was set and where it went, which is the fact worth
    keeping.
    """
    await get_visible_bot(db, bot_id, user)
    connector = await db.get(Connector, connector_id)
    if connector is None:
        raise AppError(404, "connector_not_found", "Connector not found")

    value = body.value.strip()
    if not value:
        raise AppError(400, "empty_secret", "value cannot be blank")

    row = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id, BotConnector.connector_id == connector_id
        )
    )
    link = row.scalar_one_or_none()
    previous_ref = link.secret_ref if link else None

    stored = await secrets.store_connector_secret(
        db,
        bot_id=bot_id,
        connector_id=connector_id,
        value=value,
        user_id=user.id,
    )

    if not link:
        link = BotConnector(bot_id=bot_id, connector_id=connector_id)
        db.add(link)
    link.secret_ref = stored.secret_ref
    link.status = body.status
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="connector_secret_set",
            detail={
                "connector_id": connector_id,
                "backend": stored.backend,
                "secret_ref": stored.secret_ref,
            },
        )
    )
    await db.commit()

    # A replaced credential keeps its reference when the backend does not
    # change, so `store_connector_secret` has already invalidated that one.
    # This is the other case: the backend moved (app-encrypted -> Key Vault
    # after the role assignment lands, say), leaving a stale cache entry and an
    # orphaned encrypted row behind the old reference.
    if previous_ref and previous_ref != stored.secret_ref:
        secrets.forget(previous_ref)
        await _discard_stored_secret(db, bot_id, connector_id, previous_ref)

    return ConnectorSecretOut(
        connector_id=connector_id,
        backend=stored.backend,
        secret_ref=stored.secret_ref,
        status=link.status,
        detail=stored.detail,
    )


@router.delete("/bots/{bot_id}/connectors/{connector_id}", response_model=OkOut)
async def unbind_connector(
    bot_id: uuid.UUID,
    connector_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    await get_visible_bot(db, bot_id, user)
    row = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id, BotConnector.connector_id == connector_id
        )
    )
    link = row.scalar_one_or_none()
    if link is None:
        raise AppError(404, "binding_not_found", "Connector is not bound to this bot")
    previous_ref = link.secret_ref
    await db.delete(link)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="connector_unbound",
            detail={"connector_id": connector_id},
        )
    )
    await db.commit()
    if previous_ref:
        secrets.forget(previous_ref)
        await _discard_stored_secret(db, bot_id, connector_id, previous_ref)
    return OkOut(ok=True, detail="unbound")


@router.post("/bots/{bot_id}/connectors/{connector_id}/actions/{action}")
async def execute_action(
    bot_id: uuid.UUID,
    connector_id: str,
    action: str,
    body: ExecuteActionIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Execute a connector action, or park it for approval when it is risky."""
    await get_visible_bot(db, bot_id, user)
    connector = await db.get(Connector, connector_id)
    if connector is None:
        raise AppError(404, "connector_not_found", "Connector not found")

    risk = action_risk(connector, action)
    if requires_approval(risk):
        approval = await create_gated_approval(
            db,
            bot_id=bot_id,
            risk=risk,
            title=body.title or f"{connector.name}: {action}",
            summary=f"{connector_id}.{action} requires approval (risk={risk})",
            payload={
                "kind": "connector_action",
                "connector_id": connector_id,
                "action": action,
                "input": body.input,
                "thread_id": str(body.thread_id) if body.thread_id else None,
                REQUESTED_BY_KEY: str(user.id),
            },
            actor=user,
        )
        db.add(
            AuditEvent(
                actor_user_id=user.id,
                bot_id=bot_id,
                event_type="action_held_for_approval",
                detail={"connector_id": connector_id, "action": action, "risk": risk},
            )
        )
        await db.commit()
        response.status_code = status.HTTP_201_CREATED
        return PendingApprovalOut(
            approval_id=approval.id,
            status="pending_approval",
            risk=risk,
            title=approval.title,
        ).model_dump(mode="json")

    # Through the chokepoint, not around it: `simulation.perform` is what writes
    # the undo-log entry, and it is what refuses to run at all inside a dry run.
    outcome = await simulation.perform(
        db,
        Effect(
            kind="connector",
            bot_id=bot_id,
            connector_id=connector_id,
            action=action,
            input_data=body.input,
            actor_user_id=user.id,
        ),
    )
    result = outcome.result
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="connector_action",
            detail={"connector_id": connector_id, "action": action, "ok": result.get("ok")},
        )
    )
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# MCP registry
# ---------------------------------------------------------------------------


async def _get_mcp(db: AsyncSession, mcp_id: uuid.UUID, user: User) -> McpServer:
    mcp = await db.get(McpServer, mcp_id)
    if mcp is None:
        raise AppError(404, "mcp_not_found", "MCP server not found")
    if mcp.owner_user_id not in (None, user.id):
        raise AppError(404, "mcp_not_found", "MCP server not found")
    return mcp


@router.get("/integrations/mcp", response_model=list[McpOut])
async def integrations_mcp(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[McpServer]:
    servers = await mcp_registry.list_mcp(db)
    return [m for m in servers if m.owner_user_id in (None, user.id)]


@router.post("/integrations/mcp", response_model=McpOut)
async def register_mcp(
    body: RegisterMcpIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> McpServer:
    return await mcp_registry.register_mcp(
        db,
        name=body.name,
        transport=body.transport,
        endpoint=body.endpoint,
        command=body.command,
        tool_allowlist=body.tool_allowlist,
        owner_user_id=user.id,
    )


@router.patch("/integrations/mcp/{mcp_id}", response_model=McpOut)
async def update_mcp(
    mcp_id: uuid.UUID,
    body: UpdateMcpIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> McpServer:
    mcp = await _get_mcp(db, mcp_id, user)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if value is not None:
            setattr(mcp, field, value)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="mcp_updated",
            detail={"mcp_id": str(mcp_id), "fields": sorted(changes.keys())},
        )
    )
    await db.commit()
    await db.refresh(mcp)
    return mcp


@router.delete("/integrations/mcp/{mcp_id}", response_model=OkOut)
async def delete_mcp(
    mcp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    mcp = await _get_mcp(db, mcp_id, user)
    await db.delete(mcp)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="mcp_deleted",
            detail={"mcp_id": str(mcp_id)},
        )
    )
    await db.commit()
    return OkOut(ok=True, detail="deleted")


def _mock_tools(mcp: McpServer) -> list[dict[str, Any]]:
    names = list(mcp.tool_allowlist or []) or ["echo", "search"]
    return [
        {"name": n, "description": f"{mcp.name} tool {n} (local mock)", "inputSchema": {"type": "object"}}
        for n in names
    ]


@router.get("/integrations/mcp/{mcp_id}/tools", response_model=McpToolsOut)
async def list_mcp_tools(
    mcp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> McpToolsOut:
    """Live `tools/list` against the server, with a mock when unreachable."""
    mcp = await _get_mcp(db, mcp_id, user)

    service_fn = getattr(mcp_registry, "list_tools", None)
    if service_fn is not None:
        result = await service_fn(db, mcp_id=mcp_id)
        tools = result.get("tools", []) if isinstance(result, dict) else list(result or [])
        return McpToolsOut(
            mcp_id=mcp_id,
            name=mcp.name,
            tools=tools,
            mock=bool(isinstance(result, dict) and result.get("mock")),
            error=result.get("error") if isinstance(result, dict) else None,
        )

    if mcp.transport in ("sse", "http") and mcp.endpoint:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    mcp.endpoint.rstrip("/") + "/tools/list",
                    json={},
                )
                resp.raise_for_status()
                data = resp.json()
            tools = data.get("tools", data) if isinstance(data, dict) else data
            allow = mcp.tool_allowlist or []
            if allow:
                tools = [t for t in tools if t.get("name") in allow]
            return McpToolsOut(mcp_id=mcp_id, name=mcp.name, tools=list(tools))
        except Exception as exc:  # noqa: BLE001 - unreachable server falls back to mock
            logger.info("mcp tools/list unreachable for %s: %s", mcp_id, exc)
            return McpToolsOut(
                mcp_id=mcp_id,
                name=mcp.name,
                tools=_mock_tools(mcp),
                mock=True,
                error=str(exc),
            )

    return McpToolsOut(mcp_id=mcp_id, name=mcp.name, tools=_mock_tools(mcp), mock=True)


@router.post("/bots/{bot_id}/mcp/{mcp_id}")
async def attach_mcp(
    bot_id: uuid.UUID,
    mcp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await get_visible_bot(db, bot_id, user)
    await _get_mcp(db, mcp_id, user)
    await mcp_registry.attach_mcp(db, bot_id, mcp_id)
    return {"ok": True}


@router.delete("/bots/{bot_id}/mcp/{mcp_id}", response_model=OkOut)
async def detach_mcp(
    bot_id: uuid.UUID,
    mcp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkOut:
    await get_visible_bot(db, bot_id, user)
    row = await db.execute(select(BotMcp).where(BotMcp.bot_id == bot_id, BotMcp.mcp_id == mcp_id))
    link = row.scalar_one_or_none()
    if link is None:
        raise AppError(404, "mcp_not_attached", "MCP server is not attached to this bot")
    await db.delete(link)
    await db.commit()
    return OkOut(ok=True, detail="detached")


async def _hold_mcp_call(
    db: AsyncSession,
    *,
    bot_id: uuid.UUID,
    mcp: McpServer,
    body: McpCallIn,
    risk: str,
    user: User,
) -> dict[str, Any]:
    """Park a risky MCP tool call in the approval queue.

    The held payload is the `mcp_tool` shape `services.approvals` already knows
    how to execute, so approving it runs the call back through the same
    chokepoint — and lands it in the undo log attributed to the approval.
    """
    approval = await create_gated_approval(
        db,
        bot_id=bot_id,
        risk=risk,
        title=f"{mcp.name}: {body.tool}",
        summary=f"MCP tool '{body.tool}' on {mcp.name} requires approval (risk={risk})",
        payload={
            "kind": "mcp_tool",
            "mcp_id": str(mcp.id),
            "tool": body.tool,
            "arguments": body.arguments,
            REQUESTED_BY_KEY: str(user.id),
        },
        actor=user,
    )
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="mcp_call_held",
            detail={
                "mcp_id": str(mcp.id),
                "tool": body.tool,
                "risk": risk,
                "approval_id": str(approval.id),
            },
        )
    )
    await db.commit()
    return PendingApprovalOut(
        approval_id=approval.id,
        status="pending_approval",
        risk=risk,
        title=approval.title,
    ).model_dump(mode="json")


@router.post("/bots/{bot_id}/mcp/{mcp_id}/call")
async def call_mcp(
    bot_id: uuid.UUID,
    mcp_id: uuid.UUID,
    body: McpCallIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Call an MCP tool, or park it for approval when the tool name is risky.

    MCP was the one execution path with no risk gate: a tool called
    `send_invoice` ran unattended and left no undo-log entry either. Both holes
    close with one change — the call now goes through `simulation.perform`, the
    same chokepoint the routine runner and the approval executor use.

    The rule is the one every other path applies: the tool *name* is classified
    by `services.risk.classify_action_risk`, a declared risk may only escalate
    that classification, and a gated call comes back as 201
    `PendingApprovalOut` — the shape the connector and desktop routes already
    return, so a client needs no new branch.
    """
    await get_visible_bot(db, bot_id, user)
    mcp = await _get_mcp(db, mcp_id, user)

    # One call, both holes closed: `perform` classifies the tool name through
    # the shared classifier, refuses to execute when the gate fires, and writes
    # the undo-log entry when it does not. No risk decision is taken here — a
    # second one in this router is exactly how the three paths would drift.
    outcome = await simulation.perform(
        db,
        Effect(
            kind="mcp",
            bot_id=bot_id,
            mcp_id=mcp_id,
            action=body.tool,
            input_data=body.arguments,
            declared_risk=body.risk,
            actor_user_id=user.id,
        ),
    )
    if outcome.gated:
        response.status_code = status.HTTP_201_CREATED
        return await _hold_mcp_call(
            db, bot_id=bot_id, mcp=mcp, body=body, risk=outcome.risk, user=user
        )

    result = outcome.result
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot_id,
            event_type="mcp_tool_call",
            detail={"mcp_id": str(mcp_id), "tool": body.tool, "ok": result.get("ok")},
        )
    )
    await db.commit()
    return result
