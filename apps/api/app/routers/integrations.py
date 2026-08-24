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
from app.services import mcp_registry, simulation
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
                risk_default=connector.risk_default if connector else "observe",
                first_party=bool(connector.first_party) if connector else False,
                actions=list(connector.actions or []) if connector else [],
            )
        )
    return out


@router.post("/bots/{bot_id}/connectors/{connector_id}")
async def bind_connector(
    bot_id: uuid.UUID,
    connector_id: str,
    body: BindConnectorIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await get_visible_bot(db, bot_id, user)
    row = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id, BotConnector.connector_id == connector_id
        )
    )
    link = row.scalar_one_or_none()
    if not link:
        link = BotConnector(bot_id=bot_id, connector_id=connector_id)
        db.add(link)
    link.secret_ref = body.secret_ref
    link.status = body.status
    await db.commit()
    return {"ok": True, "status": link.status}


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
