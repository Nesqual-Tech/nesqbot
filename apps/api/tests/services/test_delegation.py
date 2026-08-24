"""Bot-to-bot delegation: the hand-off, the actor it carries, and the caps.

The product case this exists for, in one line: a lead-gen bot works a list, a
lead answers, and the lead-gen bot hands that lead to Sales to close. Before
this, a bot could not hand work to a bot at all — `_select_bot` chose which bot
answered one *human* message from keyword rules and the "handoff" was a
hardcoded sentence with nothing behind it.

What is actually asserted here, in the order the module builds it:

1. the hand-off carries a brief and enough thread to read it, and the answer
   comes back to the caller as something it can act on;
2. the originating human stays the actor for the whole chain, which is what
   keeps owner-scoped approvals working three hops down;
3. every cap fires. Each one has a negative control that builds the chain that
   should be refused and shows it refused, because a guard nobody has watched
   bite is not a guard.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models import Approval, AuditEvent, Bot, Message, Run, ThreadBot
from app.services import orchestrator as orch
from app.services import routines as routines_service
from app.services.model_router import count_text_tokens
from app.services.orchestrator import (
    DELEGATION_HISTORY_MESSAGES,
    DELEGATION_MAX_DEPTH,
    DELEGATION_MAX_REFUSALS,
    DELEGATION_MAX_TOTAL,
    TOOL_DELEGATE_TO_BOT,
)
from app.services.risk import ACTION_RISKS, classify_action_risk
from tests.services.conftest import acts, call

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


async def turn_as(orchestrator, db, user, thread, bot, content: str = "work the list"):
    """One turn, with `bot` pinned as the responder.

    `_select_bot`'s keyword routing is not what is under test here and its
    tie-break between two custom bots depends on row order, so the mention
    filter is used to make the opening bot deterministic. That is also the
    interesting case for delegation: an `@lead_generator` narrows who *answers*
    and must not narrow who can be handed work.
    """
    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db, user=user, thread=thread, content=content, mention_bot_ids=[bot.id]
        )
    ]
    done = next((data for name, data in frames if name == "done"), {})
    return frames, done


async def seeded(db, slug: str, *, budget: str = "500.00") -> Bot:
    """A real seeded system bot, with room to spend inside this test."""
    bot = (await db.execute(select(Bot).where(Bot.slug == slug))).scalar_one()
    bot.daily_budget_usd = Decimal(budget)
    await db.commit()
    return bot


async def runs_for(db, bot: Bot) -> list[Run]:
    rows = await db.execute(select(Run).where(Run.bot_id == bot.id).order_by(Run.created_at))
    return list(rows.scalars().all())


async def audits(db, event_type: str) -> list[AuditEvent]:
    rows = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.event_type == event_type)
        .order_by(AuditEvent.created_at)
    )
    return list(rows.scalars().all())


async def messages_in(db, thread) -> list[Message]:
    rows = await db.execute(
        select(Message).where(Message.thread_id == thread.id).order_by(Message.created_at)
    )
    return list(rows.scalars().all())


def tool_results_seen(router) -> list[str]:
    """Every `tool` message the loop handed back to a model, in order."""
    out: list[str] = []
    for request in router.seen:
        for message in request:
            if message.get("role") == "tool":
                out.append(str(message.get("content") or ""))
    return out


@pytest_asyncio.fixture
async def avery(make_user):
    """The human at the head of every chain in this file."""
    return await make_user(email="avery@nesqualtech.test", display_name="Avery V")


@pytest_asyncio.fixture
async def lead_bot(db):
    return await seeded(db, "lead_generator")


@pytest_asyncio.fixture
async def sales_bot(db):
    return await seeded(db, "sales")


@pytest_asyncio.fixture
async def ops_bot(db):
    return await seeded(db, "ops")


@pytest_asyncio.fixture
async def support_bot(db):
    return await seeded(db, "support")


# ---------------------------------------------------------------------------
# 1. The hand-off itself
# ---------------------------------------------------------------------------


async def test_the_lead_bot_hands_a_warm_lead_to_sales_and_gets_the_answer_back(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """The motivating case, end to end.

    Sales must not start cold: the brief and the payload are in its opening
    request. And the lead bot must be able to *act on* what came back, so what
    Sales reported is handed to it as a tool result, not dropped into the void.
    """
    orchestrator = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_DELEGATE_TO_BOT,
                    slug="sales",
                    brief=(
                        "Rita Alvarez at Acme replied to our outreach and asked for "
                        "pricing. Close her: get a demo booked this week."
                    ),
                    payload={"lead": "rita@acme.test", "company": "Acme", "stage": "replied"},
                ),
            ),
            acts("", call("task_complete", summary="Demo booked with Rita for Thursday 10:00.")),
            acts("", call("task_complete", summary="Rita is with Sales now, demo Thursday.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    # A real run for the receiving bot, on the same thread, finished.
    sales_runs = await runs_for(db, sales_bot)
    assert len(sales_runs) == 1
    assert sales_runs[0].status == "completed"
    assert sales_runs[0].thread_id == thread.id
    assert sales_runs[0].finished_at is not None

    # It did not start cold: brief and payload are both in its opening request.
    child_request = orchestrator.router.seen[1]
    blob = "\n".join(str(m.get("content") or "") for m in child_request)
    assert "asked for pricing" in blob
    assert "rita@acme.test" in blob
    assert "Acme" in blob

    # And the caller can act on the answer — it came back as a tool result.
    handed_back = tool_results_seen(orchestrator.router)
    assert any("Demo booked with Rita for Thursday" in text for text in handed_back)
    assert any("`sales`" in text for text in handed_back)

    # The person sees both halves: Sales' own reply on the thread, and one line
    # in the lead bot's reply saying where their lead went.
    thread_messages = await messages_in(db, thread)
    assert any(
        m.bot_id == sales_bot.id and "Demo booked with Rita" in m.content for m in thread_messages
    )
    assert "handed this to" in done["message"]

    # And a client is told, on the event clients already render for this.
    handoffs = [d for name, d in frames if name == "handoff"]
    assert len(handoffs) == 1
    assert handoffs[0]["delegated"] is True
    assert handoffs[0]["bot_id"] == str(sales_bot.id)
    assert handoffs[0]["from_bot_id"] == str(lead_bot.id)


async def test_the_receiving_bot_is_not_handed_the_callers_transcript(
    agent_with, db, avery, make_thread, lead_bot, sales_bot, varying_screens
):
    """What Sales gets is a brief, not a recording of how lead-gen worked.

    The caller's screenshots, tool results and step log are how it did its job.
    Replaying them would multiply the delegated prompt by the length of the
    caller's run and hand over a history the receiving bot then has to reason
    its way back out of.
    """
    orchestrator = agent_with(
        [
            acts("", call("screenshot")),
            acts("", call("type", text="rita@acme.test")),
            acts(
                "",
                call(
                    TOOL_DELEGATE_TO_BOT,
                    slug="sales",
                    brief="Close Rita at Acme.",
                    payload={"lead": "rita@acme.test"},
                ),
            ),
            acts("", call("task_complete", summary="Booked.")),
            acts("", call("task_complete", summary="Passed to sales.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    # The request that opened the sales run: the one whose last message is the
    # brief. Nothing of the caller's own loop is in it.
    child = next(
        request
        for request in orchestrator.router.seen
        if "has handed you this piece of work" in str(request[-1].get("content") or "")
    )
    # Three messages: the bot's own prompt, the thread as background, the brief.
    # The caller took two desktop steps before delegating and none of them, nor
    # the frames it looked at, contributed anything here.
    assert [m["role"] for m in child] == ["system", "user", "user"]
    assert not any(m.get("role") == "tool" for m in child)
    assert not any(m.get("tool_calls") for m in child)
    for message in child:
        assert not isinstance(message.get("content"), list), "an image reached a delegated bot"
    blob = "\n".join(str(m.get("content") or "") for m in child)
    assert "steps left" not in blob
    assert "[screenshot" not in blob


async def test_the_recent_thread_travels_but_bounded_and_attributed(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """Background, capped, and never replayed as the receiving bot's own words.

    Replaying the lead bot's reply as `role: assistant` would put those words in
    the sales bot's mouth, and a model that believes it already said something
    will not say it again. So it arrives as one attributed `user` block, and the
    window is short enough that a long thread cannot quietly treble the prompt.
    """
    thread = await make_thread(avery, [lead_bot, sales_bot])
    for index in range(24):
        db.add(
            Message(
                thread_id=thread.id,
                user_id=avery.id if index % 2 == 0 else None,
                bot_id=None if index % 2 == 0 else lead_bot.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"filler message {index} " + ("x" * 900),
            )
        )
    await db.commit()

    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call("task_complete", summary="Booked.")),
            acts("", call("task_complete", summary="Passed on.")),
        ]
    )
    await turn_as(orchestrator, db, avery, thread, lead_bot)

    child = orchestrator.router.seen[1]
    assert [m["role"] for m in child] == ["system", "user", "user"]
    background = str(child[1]["content"])
    assert background.startswith("The last few messages on this thread")
    assert background.count("\n- ") == DELEGATION_HISTORY_MESSAGES
    # Attributed, and capped per message so one pasted email cannot dominate.
    assert "- the person:" in background
    assert f"- {lead_bot.name}:" in background
    assert "[...]" in background


async def test_the_delegated_prompt_is_a_brief_not_a_conversation(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """Measured: what the bounded window actually saves.

    The comparison is against the alternative that was genuinely on the table —
    replaying `history[-20:]` verbatim, which is what an ordinary `_turn` does.
    The assertion is a ratio rather than an absolute, because the absolute moves
    with the bots' own system prompts and this is a claim about the window.
    """
    thread = await make_thread(avery, [lead_bot, sales_bot])
    for index in range(24):
        db.add(
            Message(
                thread_id=thread.id,
                user_id=avery.id if index % 2 == 0 else None,
                bot_id=None if index % 2 == 0 else lead_bot.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"filler message {index} " + ("x" * 900),
            )
        )
    await db.commit()
    history = await messages_in(db, thread)

    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call("task_complete", summary="Booked.")),
            acts("", call("task_complete", summary="Passed on.")),
        ]
    )
    await turn_as(orchestrator, db, avery, thread, lead_bot)

    child = orchestrator.router.seen[1]
    actual = count_text_tokens(child)
    full_history = count_text_tokens(
        [child[0]]
        + [{"role": m.role, "content": m.content} for m in history[-20:]]
        + [child[-1]]
    )
    # Not a marginal trim: the window is what keeps a delegated opening call
    # roughly the size of the brief plus the bot's own prompt.
    assert actual < full_history / 2, (actual, full_history)


# ---------------------------------------------------------------------------
# 2. The actor is the human, all the way down
# ---------------------------------------------------------------------------


async def test_the_chain_is_stamped_with_the_person_who_started_it(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call("task_complete", summary="Booked.")),
            acts("", call("task_complete", summary="Passed on.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    child = (await runs_for(db, sales_bot))[0]
    ledger = child.context_ledger
    assert ledger["requested_by"] == str(avery.id)
    assert ledger["delegation"]["actor_user_id"] == str(avery.id)
    assert ledger["delegation"]["path"] == ["lead_generator", "sales"]
    assert ledger["delegation"]["depth"] == 1
    assert ledger["delegation"]["delegated_by"] == "lead_generator"
    # The chain, in one field, in the form a person reads.
    assert ledger["delegation"]["audit_path"] == "avery → lead_generator → sales"


async def test_the_audit_answers_on_whose_behalf(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """`avery → lead_generator → sales`, in the trail, at the moment it happens."""
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call("task_complete", summary="Booked.")),
            acts("", call("task_complete", summary="Passed on.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    started = await audits(db, "bot_delegation")
    assert len(started) == 1
    detail = started[0].detail
    assert started[0].actor_user_id == avery.id
    assert detail["chain"] == "avery → lead_generator → sales"
    assert detail["from_slug"] == "lead_generator"
    assert detail["to_slug"] == "sales"
    # Classified by the one classifier, not by a second table in the orchestrator.
    assert detail["risk"] == classify_action_risk(TOOL_DELEGATE_TO_BOT) == "mutate"

    finished = await audits(db, "bot_delegation_finished")
    assert len(finished) == 1
    assert finished[0].detail["outcome"] == "completed"
    assert finished[0].detail["status"] == "completed"


async def test_the_brief_never_reaches_an_audit_row(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """Audit rows are read more widely than the run they describe.

    Free text a model wrote is the field most likely to have picked up something
    that should not be in a log, so the trail carries the brief's size and the
    run carries its text.
    """
    secretish = "the shared inbox password is hunter2"
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief=secretish)),
            acts("", call("task_complete", summary="Booked.")),
            acts("", call("task_complete", summary="Passed on.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    for row in await audits(db, "bot_delegation"):
        assert "brief" not in row.detail
        assert row.detail["brief_chars"] == len(secretish)
        assert "hunter2" not in str(row.detail)
    for row in await audits(db, "bot_delegation_finished"):
        assert "hunter2" not in str(row.detail)
    # It is on the run, where only the run's owner can read it.
    assert (await runs_for(db, sales_bot))[0].context_ledger["delegation"]["brief"] == secretish


async def test_a_delegated_run_stays_the_humans_even_when_the_thread_is_gone(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """This is what the stamp is *for*.

    `resolve_run_owner` falls back to the thread owner, then to the owner of a
    *custom* bot. Sales is a shared system bot with no owner, so an orphaned
    delegated run with nothing stamped on it would belong to nobody — which
    under the scoping rules is not the same as belonging to everybody, and
    hides the person's own run from them.
    """
    from app.routers.deps import resolve_run_owner

    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call("task_complete", summary="Booked.")),
            acts("", call("task_complete", summary="Passed on.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])
    await turn_as(orchestrator, db, avery, thread, lead_bot)

    child = (await runs_for(db, sales_bot))[0]
    assert await resolve_run_owner(db, child) == avery.id

    child.thread_id = None
    await db.commit()
    assert await resolve_run_owner(db, child) == avery.id, "the stamp is what survives"

    # And the control: the same run without it belongs to nobody.
    child.context_ledger = {}
    await db.commit()
    assert await resolve_run_owner(db, child) is None


async def test_an_approval_raised_three_hops_down_belongs_to_the_person_who_started_it(
    agent_with, db, avery, user_b, make_thread, lead_bot, sales_bot, varying_screens
):
    """The reason actor inheritance is not bookkeeping.

    Approvals are scoped by requester. A `send` the sales bot raises inside a
    delegated run has to resolve to the person whose thread it is, or it lands
    in nobody's queue and the gate this product sells has quietly stopped
    working for exactly the runs no human was watching.
    """
    from app.errors import AppError
    from app.routers.deps import approval_owner, get_visible_approval

    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Email Rita the quote.")),
            acts("", call("click", x=900, y=40, risk="send")),
            acts("", call("task_complete", summary="Sales is waiting on you.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    held = (
        await db.execute(select(Approval).where(Approval.bot_id == sales_bot.id))
    ).scalars().all()
    assert len(held) == 1
    assert held[0].risk == "send"
    assert await approval_owner(db, held[0]) == avery.id

    # Avery can decide it; nobody else can even see it.
    assert await get_visible_approval(db, held[0].id, avery, for_decision=True) is held[0]
    with pytest.raises(AppError):
        await get_visible_approval(db, held[0].id, user_b, for_decision=True)

    # And the caller was told, in so many words, that the work is not done.
    assert any("NOT finished" in text for text in tool_results_seen(orchestrator.router))
    parked = (await runs_for(db, sales_bot))[0]
    assert parked.status == "awaiting_approval"


# ---------------------------------------------------------------------------
# 3. Negative controls — every cap, caught biting
# ---------------------------------------------------------------------------


async def test_a_bot_may_not_hand_work_to_itself(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """The one cycle with no honest reading, and the cheapest way to burn a chain."""
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="lead_generator", brief="do it again")),
            acts("", call("task_complete", summary="Did it myself.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    _frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert await runs_for(db, sales_bot) == []
    refusals = await audits(db, "bot_delegation_refused")
    assert [r.detail["reason"] for r in refusals] == ["self_delegation"]
    told = tool_results_seen(orchestrator.router)
    assert any("You are that bot" in text for text in told)
    assert "hand the work to myself" in done["message"]


async def test_the_chain_runs_out_of_depth_and_says_so(
    agent_with, db, avery, make_thread, lead_bot, sales_bot, ops_bot, support_bot
):
    """Negative control for the depth cap: build the chain that must be refused.

    `avery → lead_generator → sales → ops` is the deepest allowed. The fourth
    hop is the one under test, and it must be refused *before* anything is
    started, not after.
    """
    assert DELEGATION_MAX_DEPTH == 3
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="hop 1")),
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="ops", brief="hop 2")),
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="support", brief="hop 3")),
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="hop 4 — must not happen")),
            acts("", call("task_complete", summary="support: refused, stopping here.")),
            acts("", call("task_complete", summary="ops done.")),
            acts("", call("task_complete", summary="sales done.")),
            acts("", call("task_complete", summary="lead gen done.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot, ops_bot, support_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    # Three hops happened, the fourth did not.
    assert len(await runs_for(db, sales_bot)) == 1
    assert len(await runs_for(db, ops_bot)) == 1
    assert len(await runs_for(db, support_bot)) == 1
    assert len(await audits(db, "bot_delegation")) == 3

    refusals = await audits(db, "bot_delegation_refused")
    assert [r.detail["reason"] for r in refusals] == ["depth_cap"]
    assert refusals[0].detail["chain"] == "avery → lead_generator → sales → ops → support"
    told = tool_results_seen(orchestrator.router)
    refused_text = next(text for text in told if "Refused" in text)
    assert "3 hand-offs from avery" in refused_text
    assert "Nobody was started and nothing ran" in refused_text


async def test_the_chain_runs_out_of_allowance_and_says_so(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """Negative control for the total cap — the bound depth cannot make.

    One bot at depth 1 fanning out seven times never gets deeper, so only the
    shared allowance stops it. The counter is shared by the whole chain, which
    is why the seventh is refused even though each of the six before it was a
    perfectly legal single hop.
    """
    assert DELEGATION_MAX_TOTAL == 6
    fan = [
        call(TOOL_DELEGATE_TO_BOT, slug="sales", brief=f"lead number {index}")
        for index in range(7)
    ]
    orchestrator = agent_with(
        [acts("", *fan)]
        + [acts("", call("task_complete", summary=f"lead {index} closed.")) for index in range(6)]
        + [acts("", call("task_complete", summary="Six went over, the seventh did not."))]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert len(await runs_for(db, sales_bot)) == DELEGATION_MAX_TOTAL
    refusals = await audits(db, "bot_delegation_refused")
    assert [r.detail["reason"] for r in refusals] == ["total_cap"]
    refused_text = next(
        text for text in tool_results_seen(orchestrator.router) if "Refused" in text
    )
    assert "6 hand-offs have already been made" in refused_text


async def test_the_chain_runs_out_of_clock_and_says_so(
    agent_with, db, avery, make_thread, lead_bot, sales_bot, monkeypatch
):
    """Negative control for the wall clock, without a sleep in it.

    A hand-off is synchronous, so the depth and total caps together still permit
    seven full-length runs back to back — a request that terminates an hour and
    three quarters later, which is not an answer. Setting the window negative
    makes every chain born expired, which is the same code path a chain that
    genuinely ran long takes.
    """
    monkeypatch.setattr(orch, "DELEGATION_MAX_CHAIN_SECONDS", -1.0)
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call("task_complete", summary="Out of time, doing what I can.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert await runs_for(db, sales_bot) == []
    assert [r.detail["reason"] for r in await audits(db, "bot_delegation_refused")] == [
        "chain_timeout"
    ]
    # And it was not advertised on that turn either, so the model was not
    # invited to spend a call discovering the refusal.
    first_request_tools = orchestrator.router.tools_seen[0]
    assert TOOL_DELEGATE_TO_BOT not in {t["function"]["name"] for t in first_request_tools}


async def test_going_back_to_a_bot_that_already_worked_on_it_is_allowed(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """`A -> B -> A` is the case a naive cycle detector gets wrong.

    Sales asking lead-gen to enrich a record and getting it back is the whole
    point of the feature. Revisits cost a hop like anything else and are bounded
    by depth, not banned by shape.
    """
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts(
                "",
                call(
                    TOOL_DELEGATE_TO_BOT,
                    slug="lead_generator",
                    brief="Enrich Rita: find her title and company size before I call.",
                ),
            ),
            acts("", call("task_complete", summary="Rita is VP Ops at Acme, 400 staff.")),
            acts("", call("task_complete", summary="Called her with the enrichment. Demo booked.")),
            acts("", call("task_complete", summary="Sales has it.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert await audits(db, "bot_delegation_refused") == []
    chains = [row.detail["chain"] for row in await audits(db, "bot_delegation")]
    assert chains == [
        "avery → lead_generator → sales",
        "avery → lead_generator → sales → lead_generator",
    ]
    # And the enrichment came back to the bot that asked for it.
    assert any(
        "VP Ops at Acme" in text for text in tool_results_seen(orchestrator.router)
    )


async def test_an_unknown_slug_is_an_error_the_model_can_use(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """Not a crash, not a silent no-op: a sentence naming the alternatives."""
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="closer_bot", brief="Close Rita.")),
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call("task_complete", summary="Booked.")),
            acts("", call("task_complete", summary="Second try worked.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    _frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    refused_text = next(
        text for text in tool_results_seen(orchestrator.router) if "closer_bot" in text
    )
    assert "There is no bot called 'closer_bot' on this thread" in refused_text
    assert "On this thread: sales" in refused_text
    # Usable means the run carried on and got it right the second time.
    assert len(await runs_for(db, sales_bot)) == 1
    assert "Second try worked" in done["message"]


async def test_a_bot_cannot_pull_another_persons_bot_into_the_thread(
    agent_with, db, avery, user_b, make_bot, make_thread, lead_bot, sales_bot
):
    """Refuse, never auto-add. `bots.slug` is global; bot *visibility* is not.

    Auto-adding a slug the model produced would let a bot on one person's thread
    reach another person's custom bot by guessing a name — a model-authored
    string escalating what a run can touch. Refusing is also the recoverable
    option: the error names who is here, and an unwanted thread member would
    have to be noticed and removed by hand.
    """
    stranger = await make_bot(user_b, name="B's closer", slug="closer_bot")
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="closer_bot", brief="Close Rita.")),
            acts("", call("task_complete", summary="Did it myself.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert await runs_for(db, stranger) == []
    members = (
        await db.execute(select(ThreadBot).where(ThreadBot.thread_id == thread.id))
    ).scalars().all()
    assert {m.bot_id for m in members} == {lead_bot.id, sales_bot.id}
    refused_text = next(
        text for text in tool_results_seen(orchestrator.router) if "closer_bot" in text
    )
    assert "a bot cannot add another bot to a person's thread" in refused_text


async def test_a_hand_off_with_no_brief_is_refused(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """An empty brief is a bot started cold, which is the thing this replaces."""
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="   ")),
            acts("", call("task_complete", summary="Did it myself.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert await runs_for(db, sales_bot) == []
    assert [r.detail["reason"] for r in await audits(db, "bot_delegation_refused")] == ["no_brief"]


async def test_a_target_that_has_spent_its_budget_is_not_started(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """The receiving bot's own cap still applies, and it applies before the run."""
    sales_bot.daily_budget_usd = Decimal("0.00")
    await db.commit()
    orchestrator = agent_with(
        [
            acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="Close Rita at Acme.")),
            acts("", call("task_complete", summary="Did what I could myself.")),
        ]
    )
    thread = await make_thread(avery, [lead_bot, sales_bot])

    await turn_as(orchestrator, db, avery, thread, lead_bot)

    assert await runs_for(db, sales_bot) == []
    assert [r.detail["reason"] for r in await audits(db, "bot_delegation_refused")] == [
        "target_budget"
    ]
    assert any(
        "spent its daily budget" in text for text in tool_results_seen(orchestrator.router)
    )


async def test_a_model_that_keeps_being_refused_has_its_run_ended(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """A refusal is cheap for the model to produce and not free for us to answer."""
    assert DELEGATION_MAX_REFUSALS == 3
    bad = [call(TOOL_DELEGATE_TO_BOT, slug="nobody", brief="please") for _ in range(4)]
    orchestrator = agent_with([acts("", *bad)])
    thread = await make_thread(avery, [lead_bot, sales_bot])

    frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    # Exactly three were attempted: the fourth call in the batch was never run,
    # because the third ended the run.
    assert len(await audits(db, "bot_delegation_refused")) == DELEGATION_MAX_REFUSALS
    assert "refused every time" in done["message"]
    finished = next(d for name, d in frames if name == "desktop" and d["phase"] == "finished")
    assert finished["outcome"] == "delegation_refused"
    # And the run stopped rather than being asked for another turn.
    assert orchestrator.router.calls_made == 1


async def test_a_runaway_chain_terminates_and_says_why(
    agent_with, db, avery, make_thread, lead_bot, sales_bot
):
    """The property that matters most: a bot that only ever delegates still stops.

    The script has no end — every model call, from every bot, asks to delegate
    again. Nothing about the conversation makes this converge, so if it
    terminates it is the caps that terminated it, and the reply has to say
    which one.
    """
    thread = await make_thread(avery, [lead_bot, sales_bot])
    orchestrator = agent_with(
        [],
        tail=acts("", call(TOOL_DELEGATE_TO_BOT, slug="sales", brief="keep going")),
    )

    _frames, done = await turn_as(orchestrator, db, avery, thread, lead_bot)

    # It ended, and the allowance is what ended it.
    assert len(await runs_for(db, sales_bot)) == DELEGATION_MAX_TOTAL
    reasons = {r.detail["reason"] for r in await audits(db, "bot_delegation_refused")}
    assert reasons == {"self_delegation", "total_cap"}
    assert "refused every time" in done["message"]
    # A hard bound on what the runaway cost, so a regression here is visible as
    # a number rather than as a slow test.
    assert orchestrator.router.calls_made < 60


# ---------------------------------------------------------------------------
# 4. Contracts this lane must not quietly break
# ---------------------------------------------------------------------------


def test_the_requested_by_key_is_spelled_the_same_in_all_three_places():
    """It is written in three modules and read in one. Drift is silent."""

    from app.routers.deps import REQUESTED_BY_KEY, RUN_REQUESTED_BY_KEY

    assert orch.RUN_REQUESTED_BY_KEY == RUN_REQUESTED_BY_KEY == REQUESTED_BY_KEY == "requested_by"
    # Off the imported module, not off `__file__`: the module object knows
    # where it was loaded from without going through the working directory.
    routines = Path(routines_service.__file__).read_text(encoding="utf-8")
    assert '"requested_by"' in routines


def test_delegation_adds_no_second_risk_classifier():
    """`risk.py` is the one place a risk class is decided, and it stayed that way."""

    assert ACTION_RISKS["delegate_to_bot"] == "mutate"
    source = Path(orch.__file__).read_text(encoding="utf-8")
    # Imported and asked, never restated.
    assert "from app.services.risk import classify_action_risk" in source
    assert "ACTION_RISKS" not in source
    assert "RISK_KEYWORDS" not in source
    # And the hand-off itself is not an effect: it raises no approval of its own
    # and reaches no chokepoint, which is what keeps `simulation.perform` the
    # single one for the things that *are* effects.
    body = source.split("    async def _delegate(", 1)[1].split("\n    async def ", 1)[0]
    assert "simulation.perform" not in body
    assert "create_approval" not in body


def test_the_delegation_tool_is_dispatchable_even_where_it_is_not_advertised():
    """Advertising is a cost decision; dispatching is a capability one."""
    from app.services.context_budget import ToolContext
    from app.services.orchestrator import agent_tool_names, agent_tools_for

    offered = {
        t["function"]["name"] for t in agent_tools_for(ToolContext(desktop_running=True))
    }
    assert TOOL_DELEGATE_TO_BOT not in offered
    assert TOOL_DELEGATE_TO_BOT in agent_tool_names()


def test_the_delegation_block_is_absent_when_there_is_nobody_to_delegate_to():
    """Prompt text is paid for on every request; this one is not always earned."""
    chain = orch.DelegationChain(
        actor_user_id=uuid.uuid4(),
        actor_label="avery",
        path=("lead_generator",),
        root_run_id=uuid.uuid4(),
    )
    orchestrator = orch.Orchestrator()
    assert orchestrator._delegation_block([], chain) == ""
    assert orchestrator._delegation_block([Bot(slug="sales", name="Sales", role="AE")], None) == ""

    exhausted = orch.DelegationChain(
        actor_user_id=uuid.uuid4(),
        actor_label="avery",
        path=("lead_generator",),
        root_run_id=uuid.uuid4(),
        spent=[DELEGATION_MAX_TOTAL],
    )
    assert (
        orchestrator._delegation_block([Bot(slug="sales", name="Sales", role="AE")], exhausted)
        == ""
    )


def test_a_resumed_run_does_not_get_a_fresh_allowance():
    """Otherwise "park, resume, park, resume" is the way round every cap.

    A run parks for a takeover or a held action and is picked back up minutes or
    hours later, in a different process, with nothing in memory. The counts come
    off the row it parked with, not off what could be inferred from the bot and
    the thread.
    """
    parked = Run(
        id=uuid.uuid4(),
        bot_id=uuid.uuid4(),
        context_ledger={
            "requested_by": str(uuid.uuid4()),
            "delegation": {
                "actor_label": "avery",
                "path": ["lead_generator", "sales", "ops"],
                "depth": 2,
                "delegations_used": 5,
                "root_run_id": str(uuid.uuid4()),
            },
        },
    )
    user = type("U", (), {"id": uuid.uuid4(), "email": "someone@else.test"})()
    rebuilt = orch.DelegationChain.from_ledger(parked, user=user, bot=Bot(slug="ops"))

    assert rebuilt.depth == 2
    assert rebuilt.spent[0] == 5
    assert rebuilt.actor_label == "avery"
    assert rebuilt.audit_path == "avery → lead_generator → sales → ops"
    # One hop and one unit of allowance left, exactly as when it parked.
    assert DELEGATION_MAX_DEPTH - rebuilt.depth == 1
    assert DELEGATION_MAX_TOTAL - rebuilt.spent[0] == 1


def test_a_run_that_never_delegated_rebuilds_as_the_head_of_its_own_chain():
    """Every run predating this feature has an empty ledger and must still resume."""
    plain = Run(id=uuid.uuid4(), bot_id=uuid.uuid4(), context_ledger={})
    user = type("U", (), {"id": uuid.uuid4(), "email": "avery@nesqualtech.test"})()
    rebuilt = orch.DelegationChain.from_ledger(plain, user=user, bot=Bot(slug="lead_generator"))

    assert rebuilt.depth == 0
    assert rebuilt.spent[0] == 0
    assert rebuilt.audit_path == "avery → lead_generator"


def test_the_shared_allowance_is_shared_by_siblings_not_copied():
    """The mechanism the total cap rests on, asserted directly.

    A per-branch copy would let a fan-out spend the cap once per branch, which
    is the failure the counter exists to prevent.
    """
    root = orch.DelegationChain(
        actor_user_id=uuid.uuid4(),
        actor_label="avery",
        path=("lead_generator",),
        root_run_id=uuid.uuid4(),
    )
    left = root.extend("sales")
    right = root.extend("ops")
    left.spent[0] += 1
    assert right.spent[0] == 1
    assert root.spent[0] == 1
    assert left.depth == right.depth == 1
    assert root.audit_path == "avery → lead_generator"
