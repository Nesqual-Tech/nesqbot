"""A bot's identity, as opposed to its job.

`GET /bots/{id}/persona` closed one half of *"the bots have personas, with
emails and so on but on the desktop app, i can't see that"* — it made the
system prompt readable. The other half was that there was nothing else to
read. A bot row held a name, a one-line role and a prompt, so five teammates
wrote in one anonymous voice, signed nothing, and had no address a draft could
come from. `email`, `voice`, `signature` and `desktop_habits` are those four
missing facts.

What these tests pin down:

* the fields survive a round trip through create, list, read and patch;
* `null` and `""` clear a field, because removing an address has to be sayable;
* a system bot's persona is editable while its prompt stays locked — the app
  promises exactly that in the Builder ("the standing prompt is locked. Voice,
  email, signature and budget are yours to tune") and a 403 on the wrong half
  would make a liar of it;
* the seeder supplies a starting persona and then never touches it again,
  which is the same promise seen from the other side;
* the orchestrator actually *uses* them. A persona stored and not folded into
  the prompt is a settings page, not a persona.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Bot
from app.services.orchestrator import compose_system_prompt, persona_block
from app.services.seed import PERSONA_KEYS, seed_system

PERSONA = {
    "email": "maya@nesqualtech.com",
    "voice": "Short sentences. No throat-clearing.",
    "signature": "— Maya",
    "desktop_habits": "Browser and a spreadsheet, nothing else.",
}


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


async def test_a_bot_can_be_created_with_a_persona_and_read_back(authed):
    created = await authed.post(
        "/api/bots",
        json={"name": "Maya", "role": "Outbound", "system_prompt": "Find accounts.", **PERSONA},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    for field, value in PERSONA.items():
        assert body[field] == value, field

    persona = (await authed.get(f"/api/bots/{body['id']}/persona")).json()
    for field, value in PERSONA.items():
        assert persona[field] == value, field


async def test_the_list_request_carries_the_persona_but_not_the_prompt(authed):
    """The two halves are split on cost, not on secrecy.

    A sidebar draws every bot on launch, so it must not drag five system
    prompts along — `test_the_list_endpoint_stays_lean` in `test_bots.py`
    holds that line. Four short strings are a different matter: the chat
    header and the profile card both want the address and the sign-off, and a
    round trip per bot to fetch them is the worse trade.
    """
    created = (
        await authed.post(
            "/api/bots",
            json={"name": "Maya List", "role": "Outbound", "system_prompt": "x", **PERSONA},
        )
    ).json()

    listed = (await authed.get("/api/bots")).json()
    mine = next(bot for bot in listed if bot["id"] == created["id"])
    assert mine["email"] == PERSONA["email"]
    assert mine["signature"] == PERSONA["signature"]
    assert "system_prompt" not in mine


async def test_a_bot_created_without_a_persona_has_nulls_not_empty_strings(authed):
    """`""` would print a blank line in the prompt block. `None` prints nothing."""
    body = (
        await authed.post(
            "/api/bots",
            json={"name": "Plain Bot", "role": "Ops", "system_prompt": "x", "email": ""},
        )
    ).json()
    assert body["email"] is None
    assert body["voice"] is None


async def test_patch_sets_and_clears_persona_fields(authed, bot_a):
    patched = await authed.patch(f"/api/bots/{bot_a.id}", json=PERSONA)
    assert patched.status_code == 200, patched.text
    assert patched.json()["voice"] == PERSONA["voice"]

    # `null` clears. Somebody removing a bot's address has to be able to say so,
    # and every other field on this endpoint treats `null` as "leave it alone".
    cleared = await authed.patch(f"/api/bots/{bot_a.id}", json={"email": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["email"] is None
    assert cleared.json()["voice"] == PERSONA["voice"], "only the field sent may change"

    # And so does the empty string, because that is what an emptied text input
    # sends.
    emptied = await authed.patch(f"/api/bots/{bot_a.id}", json={"signature": ""})
    assert emptied.json()["signature"] is None


# ---------------------------------------------------------------------------
# System bots: the prompt is locked, the persona is not
# ---------------------------------------------------------------------------


async def _a_system_bot(db) -> Bot:
    await seed_system(db)
    return (await db.execute(select(Bot).where(Bot.slug == "sales"))).scalar_one()


async def test_a_system_bots_persona_is_editable(authed, db):
    bot = await _a_system_bot(db)

    response = await authed.patch(f"/api/bots/{bot.id}", json={"voice": "Blunter than usual."})

    assert response.status_code == 200, response.text
    assert response.json()["voice"] == "Blunter than usual."


async def test_a_system_bots_prompt_is_still_locked(authed, db):
    """The other half of the same promise — see `update_bot`."""
    bot = await _a_system_bot(db)

    response = await authed.patch(f"/api/bots/{bot.id}", json={"system_prompt": "Do whatever."})

    assert response.status_code == 403
    assert response.json()["code"] == "system_bot_immutable"


async def test_the_seeder_gives_a_system_bot_a_starting_persona(db):
    await seed_system(db)
    bot = (await db.execute(select(Bot).where(Bot.slug == "sales"))).scalar_one()

    assert bot.email and "@" in bot.email
    assert bot.voice and bot.signature and bot.desktop_habits


async def test_seeded_persona_is_never_reconciled_over(db):
    """Seeded once, then owned by whoever edits the bot.

    The prompt is deliberately reconciled on every boot — a system bot frozen
    at a prompt deleted months ago is the bug `test_seed_reconcile.py` exists
    for. Persona is the exact opposite case: the app hands these four fields to
    the user, so re-applying the YAML on every restart would quietly undo their
    edits and look like the save never worked.
    """
    await seed_system(db)
    bot = (await db.execute(select(Bot).where(Bot.slug == "sales"))).scalar_one()
    bot.voice = "Mine, tuned by hand."
    bot.email = "hand.tuned@example.com"
    await db.commit()

    await seed_system(db)
    db.expunge_all()

    again = (await db.execute(select(Bot).where(Bot.slug == "sales"))).scalar_one()
    assert again.voice == "Mine, tuned by hand."
    assert again.email == "hand.tuned@example.com"
    # ...while the prompt is still reconciled, so the two rules coexist.
    assert again.system_prompt


async def test_every_seeded_bot_yaml_carries_a_full_persona(db):
    """Not a style rule — a bot missing one of these is anonymous again."""
    await seed_system(db)
    bots = (await db.execute(select(Bot).where(Bot.is_system.is_(True)))).scalars().all()
    assert bots
    for bot in bots:
        for field in PERSONA_KEYS:
            assert getattr(bot, field), f"{bot.slug} has no {field}"


# ---------------------------------------------------------------------------
# The prompt is where it has to land
# ---------------------------------------------------------------------------


def _bot(**fields) -> Bot:
    bot = Bot(slug="x", name="X", role="R", system_prompt="Do the job.")
    for key, value in fields.items():
        setattr(bot, key, value)
    return bot


def test_persona_block_is_empty_when_a_bot_has_no_persona():
    """A bot with none of these behaves exactly as it did before."""
    assert persona_block(_bot()) == ""


def test_persona_block_names_the_address_the_voice_and_the_sign_off():
    block = persona_block(_bot(**PERSONA))

    assert PERSONA["email"] in block
    assert "No throat-clearing" in block
    assert "— Maya" in block
    assert "spreadsheet" in block


def test_the_address_is_described_as_an_identity_not_an_inbox():
    """Otherwise a model claims to have checked its mail, and there is no mail.

    Nothing arrives at this address unless an inbound source is configured, and
    sending is a `send`-class action that waits for a human either way. A bot
    that reports reading its inbox is inventing the whole thing.
    """
    block = persona_block(_bot(email=PERSONA["email"]))

    assert "not an inbox" in block
    assert "cannot read mail" in block


def test_persona_omits_the_fields_that_are_unset():
    block = persona_block(_bot(signature="— Ops"))

    assert "— Ops" in block
    assert "How you write" not in block
    assert "On your own machine" not in block


def test_persona_sits_in_the_cached_half_of_the_prompt():
    """Per-bot and stable between turns, so it extends the cache prefix.

    Azure re-bills from the first byte that differs, so a block that never
    changes has to sit ahead of every block that does — see the ordering table
    in `compose_system_prompt`. Persona behind the bot's own prompt and ahead
    of the memory block is the whole point of passing it in rather than
    stapling it onto `bot_prompt` at the call site.
    """
    prompt = compose_system_prompt(
        bot_prompt="Do the job.",
        persona=persona_block(_bot(**PERSONA)),
        memory_block="MEMORIES",
        ledger_block="LEDGER",
    )

    assert prompt.index("Do the job.") < prompt.index(PERSONA["email"])
    assert prompt.index(PERSONA["email"]) < prompt.index("MEMORIES")
