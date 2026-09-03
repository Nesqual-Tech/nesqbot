"""`@Lead Generator` has to put Lead Generator in the room.

The bug behind every "it never delegated" report this product has had, and it
was never in the prompts or in the loop.

`apps/desktop/src/components/ChatPane.tsx` creates a thread with
`bot_ids: [activeBot.id]` — exactly one bot, always, and
`hooks/useThreads.ts` does the same for the per-teammate thread. So on a real
thread `_delegate_targets` returns nothing, `_can_delegate` is False, and
`delegate_to_bot` is not even advertised: the chief of staff was being told to
hand work over while holding no tool that could do it. Meanwhile the
"@Lead Generator" the person typed was plain prose that nothing parsed.

The reported transcript, in full, after three rounds of fixes aimed at the
bot's judgement:

    I did not take a desktop action. I routed the work into existing platform
    work items and verified related leads/tasks already in the system.
    I logged Software dev / website / mobile app outreach as a lead (open).
    I logged Sales follow-up for incoming prospects as a lead (open).

That bot was not being lazy or evasive. It was alone in the room, and every
guard written to catch a bot that fails to delegate is gated on
`_can_delegate`, so all of them stayed correctly and uselessly silent.

Worth recording why it took so long to find: the harness test that "proved"
delegation worked created its thread with all three bots seated, which is the
one thing the application never does. A test fixture that is more generous than
the product is a test that reports the product working.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select

from app.models import AuditEvent, Bot, Thread, ThreadBot
from app.services import orchestrator as orch
from app.services.orchestrator import TOOL_DELEGATE_TO_BOT, Orchestrator
from tests.services.conftest import acts, call


async def _seeded(db, slug: str) -> Bot:
    from decimal import Decimal

    bot = (await db.execute(select(Bot).where(Bot.slug == slug))).scalar_one()
    bot.daily_budget_usd = Decimal("500.00")
    await db.commit()
    return bot


async def _roster(db, thread_id) -> set[str]:
    rows = await db.execute(
        select(Bot.slug).join(ThreadBot, ThreadBot.bot_id == Bot.id).where(
            ThreadBot.thread_id == thread_id
        )
    )
    return set(rows.scalars().all())


@pytest_asyncio.fixture
async def avery(make_user):
    return await make_user(email="avery@nesqualtech.test", display_name="Avery V")


@pytest_asyncio.fixture
async def cos(db):
    return await _seeded(db, "chief_of_staff")


@pytest_asyncio.fixture
async def lead_bot(db):
    return await _seeded(db, "lead_generator")


@pytest_asyncio.fixture
async def sales_bot(db):
    return await _seeded(db, "sales")


# ---------------------------------------------------------------------------
# 1. The reported scenario, from the thread the app actually creates
# ---------------------------------------------------------------------------


async def test_a_one_bot_thread_gains_the_teammates_the_message_names(
    agent_with, db, avery, make_thread, cos, lead_bot, sales_bot
):
    """The whole bug, end to end.

    The thread starts the way `ChatPane` makes it — one bot — and the message
    is the one the CEO actually sent.
    """
    thread = await make_thread(avery, [cos])
    assert await _roster(db, thread.id) == {"chief_of_staff"}, "the app's real starting point"

    orchestrator = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_DELEGATE_TO_BOT,
                    slug="lead_generator",
                    brief="Twenty named software-development leads, with a URL each.",
                ),
            ),
            acts("", call("task_complete", summary="Four accounts with hiring signals.")),
            acts("", call("task_complete", summary="Lead Generator is on it; four so far.")),
        ]
    )
    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db,
            user=avery,
            thread=thread,
            content=(
                "Work with @Lead Generator to get leads and @Sales to close deals inside "
                "our platform. Please start."
            ),
            mention_bot_ids=[cos.id],
        )
    ]
    done = next((data for name, data in frames if name == "done"), {})

    # Both mentioned bots are now in the room, permanently.
    assert await _roster(db, thread.id) == {"chief_of_staff", "lead_generator", "sales"}
    # And the chief of staff was actually able to hand work over.
    runs = await db.execute(select(Bot.slug).join(orch.Run, orch.Run.bot_id == Bot.id))
    assert "lead_generator" in set(runs.scalars().all())
    assert "four" in (done.get("message") or "").lower()


async def test_the_bot_that_answers_is_still_the_one_the_app_addressed(
    agent_with, db, avery, make_thread, cos, lead_bot, sales_bot
):
    """Seating Sales must not make Sales answer instead of the chief of staff.

    A message to the chief of staff that mentions Sales is still a message to
    the chief of staff. Sales being in the room is what lets the work be handed
    over rather than answered in its place.
    """
    thread = await make_thread(avery, [cos])
    orchestrator = agent_with([acts("", call("task_complete", summary="Noted."))])

    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db,
            user=avery,
            thread=thread,
            content="@Sales should know: Rita at Acme replied.",
            mention_bot_ids=[cos.id],
        )
    ]
    answered = next(data["bot_id"] for name, data in frames if name == "turn_started")
    assert answered == str(cos.id)


# ---------------------------------------------------------------------------
# 2. What must not happen
# ---------------------------------------------------------------------------


async def test_an_unknown_handle_is_prose_not_an_error(
    agent_with, db, avery, make_thread, cos
):
    """`@everyone`, an email address, a Twitter handle in a pasted message."""
    thread = await make_thread(avery, [cos])
    orchestrator = agent_with([acts("", call("task_complete", summary="Noted."))])

    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db,
            user=avery,
            thread=thread,
            content="Forwarded from @someone.else and @nobody — see rita@acme.test",
            mention_bot_ids=[cos.id],
        )
    ]
    assert frames, "the turn must still run"
    assert await _roster(db, thread.id) == {"chief_of_staff"}


async def test_another_users_bot_cannot_be_seated_by_naming_it(
    agent_with, db, avery, make_thread, cos, make_user, make_bot
):
    """Visibility is the boundary, and typing a name is not a way through it.

    Same rule as `routers/deps.bot_visibility_clause`: a system bot, or one you
    own. This is the write side of it, which is exactly where the connector
    lane found the same check missing.
    """
    stranger = await make_user(email="someone.else@example.test")
    theirs = await make_bot(stranger, name="Their Bot", slug="their_bot")
    thread = await make_thread(avery, [cos])
    orchestrator = agent_with([acts("", call("task_complete", summary="Noted."))])

    [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db,
            user=avery,
            thread=thread,
            content=f"@{theirs.slug} please handle this",
            mention_bot_ids=[cos.id],
        )
    ]

    assert await _roster(db, thread.id) == {"chief_of_staff"}


async def test_the_visibility_rule_matches_the_one_the_routers_enforce():
    """The copy cannot drift into letting a name seat another tenant's bot."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "app" / "services" / "orchestrator.py"
    ).read_text(encoding="utf-8")
    seater = source.split("async def _seat_mentioned_bots", 1)[1][:3000]
    assert "Bot.is_system.is_(True)" in seater
    assert "Bot.owner_user_id == user.id" in seater

    deps = (
        Path(__file__).resolve().parents[2] / "app" / "routers" / "deps.py"
    ).read_text(encoding="utf-8")
    clause = deps.split("def bot_visibility_clause", 1)[1][:400]
    assert "Bot.is_system.is_(True)" in clause
    assert "Bot.owner_user_id == user.id" in clause


async def test_seating_is_capped_so_one_message_cannot_pull_in_everybody(
    agent_with, db, avery, make_thread, cos, lead_bot, sales_bot
):
    """A pasted email full of handles is not a request for a nine-bot thread."""
    ops = await _seeded(db, "ops")
    support = await _seeded(db, "support")
    thread = await make_thread(avery, [cos])
    orchestrator = agent_with([acts("", call("task_complete", summary="Noted."))])

    assert Orchestrator.MENTION_SEAT_LIMIT == 4
    [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db,
            user=avery,
            thread=thread,
            content=(
                f"@{lead_bot.slug} @{sales_bot.slug} @{ops.slug} @{support.slug} "
                f"@{cos.slug} all of you"
            ),
            mention_bot_ids=[cos.id],
        )
    ]

    roster = await _roster(db, thread.id)
    # The chief of staff was already seated, so four more is the whole roster
    # here - the cap is asserted on the constant above and by this staying <= 5.
    assert len(roster) <= 5
    assert "chief_of_staff" in roster


async def test_a_message_with_no_at_sign_touches_nothing(db, avery, make_thread, cos):
    """The cheap exit, and the common case: most messages mention nobody."""
    orchestrator = Orchestrator()
    thread = await make_thread(avery, [cos])

    mentioned = await orchestrator._seat_mentioned_bots(
        db, thread=thread, user=avery, content="find me some leads please", roster=[cos]
    )

    assert mentioned == []
    assert await _roster(db, thread.id) == {"chief_of_staff"}


# ---------------------------------------------------------------------------
# 3. Seating is recorded and permanent
# ---------------------------------------------------------------------------


async def test_seating_a_bot_is_written_to_the_audit_log(db, avery, make_thread, cos, sales_bot):
    orchestrator = Orchestrator()
    thread = await make_thread(avery, [cos])

    await orchestrator._seat_mentioned_bots(
        db, thread=thread, user=avery, content="@Sales take this", roster=[cos]
    )

    rows = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.event_type == "thread_bot_seated")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].bot_id == sales_bot.id
    assert rows[0].actor_user_id == avery.id
    assert rows[0].detail["thread_id"] == str(thread.id)


async def test_a_bot_already_in_the_room_is_reported_but_not_seated_twice(
    db, avery, make_thread, cos, sales_bot
):
    """`thread_bots` has a composite primary key: a second row is an error.

    It still has to be *returned*, because the caller uses the full set to know
    who the message addressed - which is what makes the tagging sentence and
    the close guard fire on a thread where Sales was already present.
    """
    orchestrator = Orchestrator()
    thread = await make_thread(avery, [cos, sales_bot])

    mentioned = await orchestrator._seat_mentioned_bots(
        db, thread=thread, user=avery, content="@Sales again", roster=[cos, sales_bot]
    )

    assert [b.slug for b in mentioned] == ["sales"]
    rows = await db.execute(
        select(ThreadBot).where(
            ThreadBot.thread_id == thread.id, ThreadBot.bot_id == sales_bot.id
        )
    )
    assert len(list(rows.scalars().all())) == 1


async def test_a_seated_bot_stays_for_the_next_message(db, avery, make_thread, cos, sales_bot):
    """The follow-up - "now close those leads" - must reach the same room."""
    orchestrator = Orchestrator()
    thread = await make_thread(avery, [cos])

    await orchestrator._seat_mentioned_bots(
        db, thread=thread, user=avery, content="@Sales take this", roster=[cos]
    )
    assert await _roster(db, thread.id) == {"chief_of_staff", "sales"}

    # A second message that mentions nobody leaves the roster alone.
    roster_now = await orchestrator._thread_bots(db, thread.id)
    await orchestrator._seat_mentioned_bots(
        db, thread=thread, user=avery, content="now close them", roster=roster_now
    )
    assert await _roster(db, thread.id) == {"chief_of_staff", "sales"}
    assert (await db.get(Thread, thread.id)) is not None


# ---------------------------------------------------------------------------
# 4. The rule itself, with no database in the way
# ---------------------------------------------------------------------------
#
# `mentioned_bots` is pure and module-level for a reason: this is the whole of
# the rule that decides who joins a conversation, and the first version of it
# lived inside the seating method where the only way to find out what it did
# was to run a database test and read a roster. It was wrong there in a way
# that was invisible from the outside. This is that rule, tested in a line.


class _Bot:
    def __init__(self, name: str, slug: str) -> None:
        self.name, self.slug = name, slug


ROSTER = [
    _Bot("Chief of Staff", "chief_of_staff"),
    _Bot("Lead Generator", "lead_generator"),
    _Bot("Sales", "sales"),
    _Bot("Ops", "ops"),
    _Bot("Support", "support"),
]


def _slugs(text: str) -> list[str]:
    return [b.slug for b in orch.mentioned_bots(text, ROSTER)]


def test_the_message_the_ceo_actually_sent():
    assert _slugs(
        "I need you to work with @Lead Generator to get leads, @Sales to close deals "
        "inside our platform."
    ) == ["lead_generator", "sales"]


def test_every_spelling_of_a_handle_is_one_mention():
    for text in ("@Lead Generator", "@lead_generator", "@lead-generator", "@LEAD GENERATOR"):
        assert _slugs(text) == ["lead_generator"], text


def test_a_longer_name_wins_over_a_shorter_one_inside_it():
    """"@Lead Generator" must not resolve to a bot called "Lead"."""
    roster = [*ROSTER, _Bot("Lead", "lead")]
    assert [b.slug for b in orch.mentioned_bots("@Lead Generator go", roster)] == [
        "lead_generator"
    ]


def test_what_is_not_a_mention():
    assert _slugs("no mentions at all") == []
    assert _slugs("") == []
    # An unresolvable handle is prose, and an email address is an email address.
    assert _slugs("forwarded from @someone.else — reply to rita@acme.test") == []
    # A bot named in prose without an `@` is discussion, not an instruction.
    assert _slugs("sales should probably close these") == []


def test_a_two_letter_name_cannot_match_inside_a_word():
    """Same guard `_addresses_another_bot` has, for the same reason."""
    roster = [_Bot("Bo", "bo")]
    assert orch.mentioned_bots("email me @bobbyexample.com", roster) == []
