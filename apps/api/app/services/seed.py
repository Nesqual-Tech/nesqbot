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

#: Persona keys a bot YAML may carry — see `Bot.email` in models.py. Seeded
#: once and then owned by whoever edits the bot, unlike the prompt.
PERSONA_KEYS = ("email", "voice", "signature", "desktop_habits")

# The emergency skeleton, used only when `bots_dir` does not exist or has no
# YAML for a slug at all — a misconfigured deployment, not the normal path.
#
# This used to be a *second full copy* of each system bot's real prompt,
# name and role, hand-maintained next to `bots/*.yaml` on the theory that it
# was "the default, overridden by YAML". It was never actually served in any
# working deployment (every slug here has had a YAML file since day one), and
# it drifted: five bots, five system_prompts here that no longer matched their
# YAML - different wording, one missing its real `daily_budget_usd` entirely
# - discovered only by reading both side by side, not by anything failing.
#
# So this is deliberately minimal now rather than a second source of truth to
# keep in sync by hand: a name, a role, and a generic instruction to work from
# `bots_dir` once it exists. Editing a bot's real behaviour is exactly one
# edit - the YAML file - not two.
DEFAULT_BOTS = [
    {
        "slug": "chief_of_staff",
        "name": "Chief of Staff",
        "role": "Orchestrator",
        "desktop_profile": "icewm",
        "system_prompt": (
            "You are the Chief of Staff. Route work to the other system bots and track "
            "handoffs. This is a fallback prompt — bots/chief_of_staff.yaml was not found; "
            "add it for the real one."
        ),
    },
    {
        "slug": "lead_generator",
        "name": "Lead Generator",
        "role": "Outbound research & drafts",
        "desktop_profile": "xfce",
        "system_prompt": (
            "You research accounts and queue outreach drafts. This is a fallback prompt — "
            "bots/lead_generator.yaml was not found; add it for the real one."
        ),
    },
    {
        "slug": "sales",
        "name": "Sales",
        "role": "CRM & follow-ups",
        "desktop_profile": "xfce",
        "system_prompt": (
            "You keep CRM hygiene and draft follow-ups. This is a fallback prompt — "
            "bots/sales.yaml was not found; add it for the real one."
        ),
    },
    {
        "slug": "ops",
        "name": "Ops",
        "role": "Inbox, invoices, onboarding",
        "desktop_profile": "xfce",
        "system_prompt": (
            "You triage the shared inbox and run onboarding checklists. This is a fallback "
            "prompt — bots/ops.yaml was not found; add it for the real one."
        ),
    },
    {
        "slug": "support",
        "name": "Support",
        "role": "Tickets & KB",
        "desktop_profile": "xfce",
        "system_prompt": (
            "You classify tickets and draft KB-grounded replies. This is a fallback prompt "
            "— bots/support.yaml was not found; add it for the real one."
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
                # Persona is seeded, then left alone. The app tells people
                # "the standing prompt is locked. Voice, email, signature and
                # budget are yours to tune", and reconciling these the way the
                # prompt is reconciled would quietly undo that tuning on every
                # boot. So the YAML supplies a starting value for a bot that
                # has none and never overwrites one somebody set.
                for field in PERSONA_KEYS:
                    if getattr(bot, field, None) is None:
                        setattr(bot, field, spec.get(field) or None)
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
                **{field: spec.get(field) or None for field in PERSONA_KEYS},
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
