"""MCP registry — register, enable per bot, call tools with allowlist."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import release_transaction
from app.models import BotMcp, McpServer


async def list_mcp(db: AsyncSession) -> list[McpServer]:
    result = await db.execute(select(McpServer).order_by(McpServer.name))
    return list(result.scalars().all())


async def register_mcp(
    db: AsyncSession,
    *,
    name: str,
    transport: str,
    endpoint: str | None = None,
    command: str | None = None,
    tool_allowlist: list[str] | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> McpServer:
    row = McpServer(
        name=name,
        transport=transport,
        endpoint=endpoint,
        command=command,
        tool_allowlist=tool_allowlist or [],
        owner_user_id=owner_user_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def attach_mcp(db: AsyncSession, bot_id: uuid.UUID, mcp_id: uuid.UUID) -> None:
    existing = await db.execute(
        select(BotMcp).where(BotMcp.bot_id == bot_id, BotMcp.mcp_id == mcp_id)
    )
    if existing.scalar_one_or_none():
        return
    db.add(BotMcp(bot_id=bot_id, mcp_id=mcp_id))
    await db.commit()


async def call_mcp_tool(
    db: AsyncSession,
    *,
    bot_id: uuid.UUID,
    mcp_id: uuid.UUID,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    mcp = await db.get(McpServer, mcp_id)
    if not mcp or not mcp.enabled:
        return {"ok": False, "error": "mcp unavailable"}

    link = await db.execute(select(BotMcp).where(BotMcp.bot_id == bot_id, BotMcp.mcp_id == mcp_id))
    if not link.scalar_one_or_none():
        return {"ok": False, "error": "mcp not attached to bot"}

    # Fail CLOSED on an empty allowlist. `if allow and tool not in allow` read as
    # "no allowlist means no restriction", which is the opposite of what
    # docs/connectors.md promises: "an empty allowlist means nothing is callable,
    # which is the safe default." A newly registered server starts with
    # `tool_allowlist = []`, so attaching one exposed every tool it serves from
    # the moment it was attached — for a platform server that is deletes, money
    # movement and messages to real people, none of which anyone chose.
    #
    # An empty allowlist is not a configuration to be read generously. It is a
    # server nobody has decided about yet.
    allow = mcp.tool_allowlist or []
    if tool not in allow:
        return {"ok": False, "error": "tool not allowlisted"}

    if mcp.transport in ("sse", "http") and mcp.endpoint:
        # The reads above (the server, the link, and whatever the request's auth
        # dependency already did) leave a transaction open, and the POST below
        # allows 30 seconds — connect *and* read — against a server this
        # deployment does not control. `db.release_transaction` has the incident:
        # a backend idle in a transaction past sixty seconds is terminated. This
        # site does not raise today, because nothing touches `db` afterwards,
        # which makes it quieter than the incident rather than different from it:
        # the backend still dies and the pool still hands the dead connection to
        # whoever asks next, for `pool_pre_ping` to notice.
        await release_transaction(db)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    mcp.endpoint.rstrip("/") + "/tools/call",
                    json={"name": tool, "arguments": arguments},
                )
                if r.status_code < 400:
                    return {"ok": True, "result": r.json()}
                return {"ok": False, "error": r.text, "status": r.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # stdio / unreachable. The mock used to answer `ok: True` here, which is a lie
    # a caller cannot detect unless it happens to check `mock` as well — and
    # nothing does. Against a server that issues invoices and messages real
    # people, that is a bot reporting an invoice as sent when nothing was sent,
    # then either moving on or retrying, because it has no way to tell the
    # difference. A synthetic success is the one answer this function must never
    # give in production.
    if not get_settings().is_development:
        return {
            "ok": False,
            "error": (
                f"mcp server {mcp.name!r} is not reachable: transport "
                f"{mcp.transport!r} with no usable endpoint. Nothing ran."
            ),
            "code": "mcp_unreachable",
        }

    return {
        "ok": True,
        "mock": True,
        "mcp": mcp.name,
        "tool": tool,
        "arguments": arguments,
        "result": {"message": f"MCP tool {tool} invoked (local mock)"},
    }
