"""Seed system bots from YAML definitions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Bot, KbArticle
from app.services import rag
from app.services.connectors import seed_connectors

logger = logging.getLogger(__name__)

REQUIRED_BOT_KEYS = ("slug", "name", "system_prompt")

# A bot's prompt here describes its *role* and nothing else. What every bot has
# — its Bot Desktop, the action protocol, the approval rules — is composed in at
# turn time by `services.orchestrator` from a single constant, because that text
# is identical for every bot and five copies of it drift five ways. It also
# reaches bots a user creates through the API, which a YAML block never could.
DEFAULT_BOTS = [
    {
        "slug": "chief_of_staff",
        "name": "Chief of Staff",
        "role": "Orchestrator",
        "desktop_profile": "icewm",
        "system_prompt": (
            "You are the Chief of Staff. Route work to specialists, "
            "track handoffs, nudge stalls, and compile briefs. Act on the routing "
            "yourself rather than proposing it. Never send externally. Escalate only "
            "judgment calls a bot cannot make."
        ),
    },
    {
        "slug": "lead_generator",
        "name": "Lead Generator",
        "role": "Outbound research & drafts",
        "desktop_profile": "xfce",
        "system_prompt": (
            "You research accounts, score intent, and queue personalized outreach drafts. "
            "Do the work in this turn; do not describe it and do not ask whether to start. "
            "You have a real Linux desktop with a browser: open the site, get to the "
            "sign-in screen, hand over for the login only, then search and read what is "
            "actually on screen. If the desktop is not running, start it and continue. "
            "Sending is the one thing you never do yourself — that gate is enforced by the "
            "API, so you do not need to refuse work to stay safe. Report the number of "
            "drafts you actually wrote and the number actually sent — never a count you did "
            "not produce."
        ),
    },
    {
        "slug": "sales",
        "name": "Sales",
        "role": "CRM & follow-ups",
        "desktop_profile": "xfce",
        "system_prompt": (
            "You keep CRM hygiene clean, draft follow-ups in the seller's voice, "
            "flag stalls, and produce Monday scoreboards. Prefer CRM connectors; when "
            "there is no API, drive the browser yourself rather than saying it cannot "
            "be done."
        ),
    },
    {
        "slug": "ops",
        "name": "Ops",
        "role": "Inbox, invoices, onboarding",
        "desktop_profile": "xfce",
        "system_prompt": (
            "You triage shared inbox, extract invoices into structured packets, "
            "run onboarding checklists, and surface calendar conflicts. Do each of these "
            "when asked rather than explaining how you would."
        ),
    },
    {
        "slug": "support",
        "name": "Support",
        "role": "Tickets & KB",
        "desktop_profile": "xfce",
        "system_prompt": (
            "You classify tickets, draft KB-grounded replies with citations, "
            "and escalate with context packs. Prefer ticketing connector + KB RAG. When "
            "the KB does not answer something, say so instead of inventing a procedure."
        ),
    },
]

DEFAULT_KB = [
    {
        "title": "Password reset",
        "body": (
            "Users can reset passwords via Account → Security. SSO users must use "
            "Entra self-service reset."
        ),
    },
    {
        "title": "Login timeout",
        "body": (
            "If sessions expire within 5 minutes, check Conditional Access and refresh "
            "token rotation. Escalate to identity eng if widespread."
        ),
    },
]


def validate_bot_spec(spec: Any, source: str) -> dict | None:
    """Return a usable bot spec, or None (with a log line) when the file is bad."""
    if not isinstance(spec, dict):
        logger.warning("skipping %s: expected a YAML mapping, got %s", source, type(spec).__name__)
        return None
    missing = [k for k in REQUIRED_BOT_KEYS if not str(spec.get(k) or "").strip()]
    if missing:
        logger.warning("skipping %s: missing required key(s) %s", source, ", ".join(missing))
        return None
    return spec


def load_bot_specs() -> list[dict]:
    """Defaults overlaid with any valid YAML definitions in `bots_dir`."""
    settings = get_settings()
    bots_dir = Path(settings.bots_dir)
    defs = list(DEFAULT_BOTS)
    if not bots_dir.exists():
        logger.info("bots dir %s not found — using built-in defaults", bots_dir)
        return defs

    for path in sorted(bots_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("skipping %s: could not parse YAML (%s)", path.name, exc)
            continue
        spec = validate_bot_spec(data, path.name)
        if spec is None:
            continue
        defs = [d for d in defs if d["slug"] != spec["slug"]] + [spec]
    return defs


async def seed_system(db: AsyncSession) -> None:
    await seed_connectors(db)

    for spec in load_bot_specs():
        existing = await db.execute(select(Bot).where(Bot.slug == spec["slug"]))
        bot = existing.scalar_one_or_none()
        if bot is not None:
            # Reconcile, do not skip. This used to `continue`, which meant a
            # system bot was frozen at whatever prompt it was first seeded with:
            # every later fix to seed.py or bots/*.yaml reached new environments
            # only. In production that left the lead generator still carrying
            # "Always report draft counts as N drafts, 0 sent" long after that
            # line was deleted here - so it kept inventing a draft count, and
            # the prompt edit looked like it had simply not worked.
            #
            # Only `is_system` bots are reconciled, and only the fields the spec
            # owns. A user's custom bot, and any budget an operator has tuned,
            # are never overwritten.
            if bot.is_system:
                bot.name = spec["name"]
                bot.role = spec.get("role", "")
                bot.system_prompt = spec["system_prompt"]
                bot.desktop_profile = spec.get("desktop_profile", bot.desktop_profile)
            continue
        db.add(
            Bot(
                slug=spec["slug"],
                name=spec["name"],
                role=spec.get("role", ""),
                system_prompt=spec["system_prompt"],
                is_system=True,
                desktop_profile=spec.get("desktop_profile", "xfce"),
                daily_budget_usd=spec.get("daily_budget_usd", 5),
            )
        )
    await db.commit()

    kb = await db.execute(select(KbArticle).limit(1))
    if kb.scalar_one_or_none():
        return

    articles = [KbArticle(title=a["title"], body=a["body"]) for a in DEFAULT_KB]
    db.add_all(articles)
    await db.commit()

    # Embed the starter KB so support gets vector recall on a fresh install.
    for article in articles:
        await db.refresh(article)
        await rag.upsert_kb_embedding(db, article)
