"""System bot prompts must be reconciled on boot, not frozen at first seed.

Production regression, and a quietly expensive one. `seed_system` used to
`continue` past any bot whose slug already existed, so a system bot kept
whatever prompt it was first created with, forever. Every subsequent fix to
`seed.py` or `bots/*.yaml` only ever reached environments that had never been
seeded.

What that looked like: the deployed Lead Generator was still carrying
"Always report draft counts as N drafts, 0 sent" long after that sentence was
deleted from the source, so it kept inventing a draft count it had never
produced. The prompt edit appeared to have silently not worked, and the obvious
next suspicion was the model rather than the seeder.

A user's own bots are a different matter and must never be rewritten.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Bot
from app.services.seed import seed_system


async def _lead_generator(db) -> Bot:
    result = await db.execute(select(Bot).where(Bot.slug == "lead_generator"))
    return result.scalar_one()


async def test_a_stale_system_prompt_is_replaced_on_the_next_boot(db):
    await seed_system(db)
    bot = await _lead_generator(db)

    bot.system_prompt = "Always report draft counts as N drafts, 0 sent."
    bot.role = "stale role"
    await db.commit()

    await seed_system(db)
    db.expunge_all()

    refreshed = await _lead_generator(db)
    assert "N drafts, 0 sent" not in refreshed.system_prompt
    assert refreshed.role != "stale role"


async def test_reseeding_is_idempotent(db):
    await seed_system(db)
    first = (await _lead_generator(db)).system_prompt
    await seed_system(db)
    db.expunge_all()
    assert (await _lead_generator(db)).system_prompt == first

    rows = await db.execute(select(Bot).where(Bot.slug == "lead_generator"))
    assert len(list(rows.scalars().all())) == 1, "reconcile must not duplicate the bot"


async def test_a_custom_bot_is_never_rewritten(db, make_user, make_bot):
    """The reconcile is scoped to `is_system`; a user's bot is their own."""
    user = await make_user()
    custom = await make_bot(user, slug="lead_generator_custom", name="My Lead Bot")
    custom.system_prompt = "My own carefully tuned prompt."
    custom.is_system = False
    await db.commit()
    custom_id = custom.id

    await seed_system(db)
    db.expunge_all()

    unchanged = await db.get(Bot, custom_id)
    assert unchanged.system_prompt == "My own carefully tuned prompt."
    assert unchanged.name == "My Lead Bot"


async def test_an_operator_tuned_budget_survives_reconcile(db):
    """Only the fields the spec owns are reconciled.

    A budget raised by an operator is deliberate configuration, not drift, and
    silently resetting it every deploy would be its own bug.
    """
    await seed_system(db)
    bot = await _lead_generator(db)
    bot.daily_budget_usd = 42
    await db.commit()

    await seed_system(db)
    db.expunge_all()

    assert int((await _lead_generator(db)).daily_budget_usd) == 42
