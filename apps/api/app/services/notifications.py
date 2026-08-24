"""Mobile push — Expo notifications for pending approvals."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Approval, Bot, Run, Thread, UserDevice

logger = logging.getLogger(__name__)

RISK_LABELS: dict[str, str] = {
    "send": "Send approval needed",
    "spend": "Spend approval needed",
    "delete": "Delete approval needed",
    "mutate": "Change approval needed",
}


def _title_for(approval: Approval) -> str:
    return RISK_LABELS.get(approval.risk, "Approval needed")


async def _owner_user_id(db: AsyncSession, approval: Approval):
    """Approvals usually hang off a run; routine-created ones only carry a thread."""
    if approval.run_id is not None:
        result = await db.execute(
            select(Thread.owner_user_id)
            .join(Run, Run.thread_id == Thread.id)
            .where(Run.id == approval.run_id)
        )
        owner = result.scalar_one_or_none()
        if owner:
            return owner

    thread_id = (approval.payload or {}).get("thread_id")
    if not thread_id:
        return None
    try:
        thread_uuid = thread_id if isinstance(thread_id, uuid.UUID) else uuid.UUID(str(thread_id))
    except ValueError:
        return None
    result = await db.execute(select(Thread.owner_user_id).where(Thread.id == thread_uuid))
    return result.scalar_one_or_none()


async def notify_approval(db: AsyncSession, approval: Approval) -> None:
    """Push a pending approval to the owner's registered devices.

    Silently no-ops when push is disabled, no devices are registered, or the
    Expo call fails — a notification must never break an approval.
    """
    settings = get_settings()
    if not settings.expo_push_enabled:
        return

    try:
        user_id = await _owner_user_id(db, approval)
        if not user_id:
            return

        devices = await db.execute(select(UserDevice).where(UserDevice.user_id == user_id))
        tokens = [d.token for d in devices.scalars().all() if d.token]
        if not tokens:
            return

        bot = await db.get(Bot, approval.bot_id)
        body = approval.title or approval.summary[:120]
        if bot is not None:
            body = f"{bot.name}: {body}"

        messages: list[dict[str, Any]] = [
            {
                "to": token,
                "title": _title_for(approval),
                "body": body[:180],
                "sound": "default",
                "data": {
                    "approval_id": str(approval.id),
                    "bot_id": str(approval.bot_id),
                    "risk": approval.risk,
                    "kind": (approval.payload or {}).get("kind"),
                },
            }
            for token in tokens
        ]

        import httpx

        async with httpx.AsyncClient(timeout=settings.sidecar_timeout_seconds) as client:
            r = await client.post(
                settings.expo_push_url,
                json=messages,
                headers={"accept": "application/json", "content-type": "application/json"},
            )
            if r.status_code >= 400:
                logger.warning("expo push rejected (%s): %s", r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001 - push is strictly best effort
        logger.warning("approval push notification failed: %s", exc)
