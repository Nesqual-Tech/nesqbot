"""Aggregated API router.

``main.py`` mounts a single router at ``/api``; each module below owns one
cohesive slice of the contract in ``docs/API.md``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.routers import (
    agent,
    approvals,
    auth,
    bots,
    desktop,
    health,
    inbound,
    integrations,
    knowledge,
    rehearsal,
    routines,
    threads,
    usage,
    work_items,
)
from app.routers.deps import API_VERSION

#: OpenAPI tag metadata, surfaced at /api/openapi.json.
OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {"name": "auth", "description": "Dev login, Entra sign-in, current user, push devices."},
    {"name": "bots", "description": "System and custom bots, plus per-bot budgets."},
    {"name": "threads", "description": "Threads, messages, and SSE streams."},
    {"name": "approvals", "description": "Human-in-the-loop queue for risky actions."},
    {"name": "integrations", "description": "Connector catalog, bindings, and MCP servers."},
    {"name": "desktop", "description": "Bot Desktop lifecycle and computer-use actions."},
    {"name": "routines", "description": "Scheduled and taught routines."},
    {"name": "knowledge", "description": "Per-bot memories and the shared knowledge base."},
    {"name": "usage", "description": "Spend, evals, run history, and audit log."},
    {"name": "rehearsal", "description": "Dry runs, saved plans, and the undo log."},
    {
        "name": "agent",
        "description": "Autonomous agent runs: human takeover and resume.",
    },
    {
        "name": "work-items",
        "description": "Owned, transferable units of work and the ledger of who held them.",
    },
    {
        "name": "inbound",
        "description": (
            "Inbound events: signed webhooks, connector polling, and the queue of "
            "replies that matched no work item."
        ),
    },
]

router = APIRouter()

for _module in (
    health,
    auth,
    bots,
    threads,
    approvals,
    integrations,
    desktop,
    routines,
    knowledge,
    usage,
    rehearsal,
    agent,
    work_items,
    inbound,
):
    router.include_router(_module.router)

__all__ = ["API_VERSION", "OPENAPI_TAGS", "router"]
