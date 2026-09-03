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


# ---------------------------------------------------------------------------
# The other half of the same bug: reconcile from *what*
# ---------------------------------------------------------------------------
#
# Reconciling on every boot is only an improvement if `load_bot_specs` can find
# the YAML. In production it could not, and that turned the fix above into a
# second, worse version of the same failure — every restart overwrote all five
# real prompts with the fallback skeletons.
#
# Measured on the image that was running as `nesqbot-api--0000044`:
#
#     $ docker run --rm --entrypoint sh \
#         nesqacrprodCHANGE_ME.azurecr.io/nesqbot/api:v0.12.10 -c 'ls -d /bots'
#     ls: cannot access '/bots': No such file or directory
#
# `BOTS_DIR=/bots` was set on the Container App, only `/mnt/bot-homes` was
# mounted, and the Dockerfile copied `app` and `sql` and nothing else. Local
# development never noticed because `docker-compose.yml` bind-mounts
# `./bots:/bots:ro`, which is exactly the kind of difference that only shows up
# as "the bots feel stupid in production".
#
# Three files have to agree for this to work, so the test reads all three. It is
# a source-level check rather than a container build for the reason the sidecar
# route tests are: a build is minutes and a CI runner, and the property worth
# defending is the agreement, not the bytes.


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[3].parent


def _text(*parts: str) -> str:
    path = _repo_root().joinpath(*parts)
    if not path.exists():  # pragma: no cover - infra/ is absent in the api-only lane
        import pytest

        pytest.skip(f"{'/'.join(parts)} is not on disk in this lane")
    return path.read_text(encoding="utf-8")


def test_the_api_image_carries_the_bot_prompts():
    """The Dockerfile must COPY the YAML the seeder reads."""
    dockerfile = _text("apps", "api", "Dockerfile")
    assert "COPY bots /bots" in dockerfile, (
        "the api image must bake in bots/: a container with no /bots seeds the "
        "DEFAULT_BOTS skeletons and reconcile then overwrites the real prompts"
    )


def test_the_baked_path_is_the_path_every_environment_asks_for():
    """`BOTS_DIR` in Bicep, the compose mount and the COPY target are one path."""
    assert "'/bots'" in _text("infra", "azure", "main.bicep")
    assert "BOTS_DIR: /bots" in _text("docker-compose.yml")
    assert "COPY bots /bots" in _text("apps", "api", "Dockerfile")


def test_the_api_build_context_is_the_repo_root_everywhere_it_is_declared():
    """`bots/` is outside `apps/api`, so the context has to be the root.

    Both build declarations are checked because a mismatch is silent: compose
    would keep building an image with the prompts while CI shipped one without,
    and the difference would only be visible in production behaviour.
    """
    compose = _text("docker-compose.yml")
    assert "dockerfile: apps/api/Dockerfile" in compose
    assert "context: ./apps/api" not in compose

    workflow = _text(".github", "workflows", "docker.yml")
    assert "context: apps/api\n" not in workflow


def test_the_fallback_prompts_say_they_are_fallbacks():
    """The skeletons are a diagnostic, not a product. Keep them self-announcing.

    This is what made the production bug findable at all: a bot whose prompt
    says "bots/chief_of_staff.yaml was not found" names its own cause. A
    plausible-looking default would have been indistinguishable from a bad
    prompt somebody wrote on purpose.
    """
    from app.services.seed import DEFAULT_BOTS

    for spec in DEFAULT_BOTS:
        assert "fallback prompt" in spec["system_prompt"], spec["slug"]
        assert f"bots/{spec['slug']}.yaml was not found" in spec["system_prompt"]
