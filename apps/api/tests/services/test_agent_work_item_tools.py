"""The work-item tools, as an agent actually reaches them.

Three things are being checked here and they are worth keeping apart.

**1. That the object exists for the agent at all.** The owner's Lead Bot brief
asks it to log every qualified prospect with a company, a contact, a verdict, a
pitch and a handed-to-sales date. There is no spreadsheet connector, so before
this lane the bot could not record one prospect. These tests log one and hand it
over, and assert the row and the ledger entry that come out.

**2. What it costs.** Four tool schemas are re-sent on every model call forever.
The numbers are pinned below, cold and warm, in tokens.

**3. That it cannot reach past its own tenant.** An agent runs unattended. The
negative controls are the point of the module, not an appendix to it.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.models import AuditEvent, Run, WorkItem, WorkItemKey, WorkItemTransfer
from app.services import work_items as work_items_service
from app.services.agent_work_items import (
    SOURCE_AGENT,
    TOOL_CREATE_WORK_ITEM,
    TOOL_FIND_WORK_ITEMS,
    TOOL_TRANSFER_WORK_ITEM,
    TOOL_UPDATE_WORK_ITEM,
    WORK_ITEM_TOOL_NAMES,
    WORK_ITEM_TOOL_SCHEMAS,
)
from app.services.context_budget import (
    WORK_ITEM_CREATE_TOOL,
    WORK_ITEM_FIND_TOOL,
    WORK_ITEM_TOOLS,
    WORK_ITEM_TRANSFER_TOOL,
    WORK_ITEM_UPDATE_TOOL,
    ToolContext,
)
from app.services.orchestrator import (
    # The reply-prose lane's phrase table, imported to assert these four tools
    # are deliberately *absent* from it — see the test that says why.
    _STEP_PHRASES,
    BROWSER_ACTIONS,
    DESKTOP_ACTIONS,
    TOOL_TASK_COMPLETE,
    agent_tool_names,
    agent_tools,
    agent_tools_for,
)
from tests.services.conftest import acts, call, turn

ALL_TOOLS = agent_tools()


def _slug(stem: str) -> str:
    """A bot slug nothing else in the run can collide with.

    `bots.slug` is globally unique, so two tests that both want a bot called
    `sales` are an IntegrityError rather than a failure with a readable name.
    The tests below therefore refer to `bot.slug` rather than to a literal, and
    the refusals they assert on are built from it.
    """
    return f"{stem}_{uuid.uuid4().hex[:8]}"


def _tokens(tools) -> int:
    """The same four-characters-a-token estimate the rest of the budget suite uses."""
    return len(json.dumps(tools)) // 4 if tools else 0


def _names(tools) -> set[str]:
    return {t["function"]["name"] for t in tools}


def _one(name: str) -> list[dict]:
    return [t for t in ALL_TOOLS if t["function"]["name"] == name]


def _tool_replies(router) -> list[str]:
    """Every `tool` message the loop put in front of the model, in order."""
    seen: list[str] = []
    for request in router.seen:
        for message in request:
            if message.get("role") == "tool":
                text = str(message.get("content") or "")
                if text not in seen:
                    seen.append(text)
    return seen


async def _items(db, user) -> list[WorkItem]:
    result = await db.execute(
        select(WorkItem)
        .where(WorkItem.owner_user_id == user.id)
        .order_by(WorkItem.created_at)
    )
    return list(result.scalars().all())


async def _ledger(db, work_item_id) -> list[WorkItemTransfer]:
    result = await db.execute(
        select(WorkItemTransfer)
        .where(WorkItemTransfer.work_item_id == work_item_id)
        .order_by(WorkItemTransfer.created_at)
    )
    return list(result.scalars().all())


#: The prospect row the brief describes, in the shape the entity actually has:
#: the pipeline columns as `detail`, the addresses as `keys`, and no
#: handed-to-sales date at all — that is what the ledger is for.
PROSPECT = {
    "type": "lead",
    "title": "Star Dental SRL",
    "summary": (
        "Two-chair clinic in Cluj. Bookings go through a contact form that emails "
        "reception; no online calendar, no reminders."
    ),
    "detail": {
        "contact": "Ana Pop",
        "role": "Practice manager",
        "platform": "linkedin",
        "profile_url": "https://linkedin.com/in/anapop",
        "website_verdict": "wordpress, no booking",
        "automation_gap": "manual appointment reminders",
        "app_case": "booking + SMS reminders",
        "pitch": "Two weeks to a booking flow that texts patients the day before.",
        "stage": "qualified",
    },
    "keys": {"linkedin": "https://LinkedIn.com/in/AnaPop", "email": " Ana@stardental.RO "},
}


# ---------------------------------------------------------------------------
# 1. What it costs, cold and warm
# ---------------------------------------------------------------------------
#
# `test_agent_context_budget.py` holds these four out of its baseline
# arithmetic, because folding them in would restate a measurement of a request
# that was never sent. This is where they are paid for instead.


def test_each_schema_costs_what_it_says_it_costs():
    """The four pins every other number in this section is derived from.

    Written as one assertion per tool rather than a total, so a description that
    grows names itself. The tolerance is deliberately tight: a previous lane
    added ~140 tokens to one `risk` description and two guards caught it, which
    is the behaviour worth keeping.
    """
    measured = {
        WORK_ITEM_CREATE_TOOL: 280,
        WORK_ITEM_FIND_TOOL: 194,
        WORK_ITEM_UPDATE_TOOL: 274,
        WORK_ITEM_TRANSFER_TOOL: 185,
    }
    for name, expected in measured.items():
        assert _tokens(_one(name)) == pytest.approx(expected, abs=25), name
    assert sum(measured.values()) == 933


def test_a_bot_with_nothing_logged_pays_for_one_schema_and_not_four():
    """The gate, stated as money.

    All four unconditionally would be 933 tokens on every model call of every
    run, including the runs that never touch a record. Three of them are gated
    on state that is genuinely absent — no record exists to be found, no id is
    in hand, and on a single-bot thread there is nobody to hand one to — so an
    opening turn on a fresh tenant pays 280 and the rest is bought only where
    it can be used.
    """
    plain = ToolContext(desktop_running=True)
    fresh = ToolContext(desktop_running=True, work_items_available=True)
    holding = ToolContext(
        desktop_running=True,
        work_items_available=True,
        work_items_exist=True,
        work_item_held=True,
        handover_available=True,
    )

    assert _names(agent_tools_for(fresh)) - _names(agent_tools_for(plain)) == {
        WORK_ITEM_CREATE_TOOL
    }
    assert _tokens(agent_tools_for(fresh)) - _tokens(agent_tools_for(plain)) == pytest.approx(
        280, abs=25
    )
    assert _tokens(agent_tools_for(holding)) - _tokens(agent_tools_for(plain)) == pytest.approx(
        933, abs=60
    )


def test_the_four_gates_open_one_at_a_time_and_in_that_order():
    """Each tool appears exactly when it has something it could do.

    `find_work_items` on a tenant with nothing logged can only answer "nothing
    matches"; `update_work_item` and `transfer_work_item` both take a required
    `id`, so before one exists there is no valid call to make. That is the same
    thing `DOM_ENTRY_SET` says about `browser_click` before a snapshot, and it
    is true in the same literal way rather than as a preference.
    """
    base = {"desktop_running": True, "work_items_available": True}
    assert _names(agent_tools_for(ToolContext(**base))) & WORK_ITEM_TOOLS == {
        WORK_ITEM_CREATE_TOOL
    }
    assert _names(
        agent_tools_for(ToolContext(**base, work_items_exist=True))
    ) & WORK_ITEM_TOOLS == {WORK_ITEM_CREATE_TOOL, WORK_ITEM_FIND_TOOL}
    assert _names(
        agent_tools_for(ToolContext(**base, work_items_exist=True, work_item_held=True))
    ) & WORK_ITEM_TOOLS == {WORK_ITEM_CREATE_TOOL, WORK_ITEM_FIND_TOOL, WORK_ITEM_UPDATE_TOOL}
    assert (
        _names(
            agent_tools_for(
                ToolContext(
                    **base,
                    work_items_exist=True,
                    work_item_held=True,
                    handover_available=True,
                )
            )
        )
        & WORK_ITEM_TOOLS
        == WORK_ITEM_TOOLS
    )


def test_a_record_this_run_just_created_is_enough_to_look_one_up():
    """`work_items_exist` is about the tenant; a fresh create satisfies it too.

    Without this, a bot that logged its first prospect would be told to check
    for duplicates by a tool it was not being offered, on the one request where
    it had just proved there was something to find.
    """
    fresh = ToolContext(desktop_running=True, work_items_available=True, work_item_held=True)
    assert WORK_ITEM_FIND_TOOL in _names(agent_tools_for(fresh))


def test_none_of_them_waits_for_a_desktop():
    """Logging a prospect touches no machine, and the desktop is cold on the
    opening turn — which is the turn where a bot decides there is something
    worth writing down."""
    cold = _names(
        agent_tools_for(
            ToolContext(
                desktop_running=False,
                work_items_available=True,
                work_items_exist=True,
                work_item_held=True,
                handover_available=True,
            )
        )
    )
    assert WORK_ITEM_TOOLS <= cold


def test_they_are_dispatchable_even_on_the_requests_they_are_not_sold_on():
    """Advertising is a cost decision; dispatch is a capability one.

    A model that names `find_work_items` on a tenant with nothing logged gets
    the real empty answer, not "there is no tool called that". Collapsing the
    two would turn a gate into a lie.
    """
    offered = _names(agent_tools_for(ToolContext(desktop_running=True, work_items_available=True)))
    assert WORK_ITEM_FIND_TOOL not in offered
    assert WORK_ITEM_TOOLS <= agent_tool_names()


def test_the_budget_module_and_the_schema_table_name_the_same_four_tools():
    """`context_budget` spells the names as literals, like `DOM_ENTRY_SET`.

    That keeps it a pure function of strings with no import into the service
    layer, and it is only safe because of this assertion: a rename that touched
    one table and not the other would silently stop gating anything.
    """
    assert WORK_ITEM_TOOLS == set(WORK_ITEM_TOOL_SCHEMAS) == set(WORK_ITEM_TOOL_NAMES)


def test_none_of_them_offers_a_risk_lever():
    """A field that can be declared will be declared.

    These reach nothing outside the tenant — that is `services/work_items.py`'s
    decision and this lane did not reopen it — so offering `risk` here would be
    an invitation to park a lead behind a human for the crime of writing a row.
    """
    for name in WORK_ITEM_TOOL_NAMES:
        properties = _one(name)[0]["function"]["parameters"]["properties"]
        assert "risk" not in properties, name


# ---------------------------------------------------------------------------
# 2. The thing the brief asked for
# ---------------------------------------------------------------------------


async def test_a_bot_logs_a_prospect_with_the_columns_the_brief_wanted(
    agent_with, db, user_a, make_thread, agent_bot
):
    """The spreadsheet row, as a row that has an owner and a history.

    Every column of `nesqual-leads.xlsx` lands somewhere: company in `title`,
    the diagnosis in `summary`, contact/role/platform/verdict/gap/case/pitch in
    `detail`, the addresses in `keys`. The one column that does not is
    "handed to sales date", and it is missing on purpose — see the transfer test
    below, where it turns into a ledger row with a reason attached.
    """
    orchestrator = agent_with(
        [
            acts("", call(TOOL_CREATE_WORK_ITEM, **PROSPECT)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Logged Star Dental.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread, "qualify star dental and log it")

    items = await _items(db, user_a)
    assert len(items) == 1
    item = items[0]
    assert item.type == "lead"
    assert item.title == "Star Dental SRL"
    assert item.status == "open"
    assert item.owner_bot_id == agent_bot.id
    assert item.owner_user_id == user_a.id
    # Pinned to the conversation it came out of, which is where the inbound lane
    # publishes "the lead replied".
    assert item.thread_id == thread.id
    assert item.detail["pitch"].startswith("Two weeks")
    assert item.detail["stage"] == "qualified"

    keys = {(k.channel, k.value) for k in await work_items_service.keys_for(db, item.id)}
    # Normalised through the one function the inbound reader also uses. A key
    # stored with the capitals and the stray spaces the model sent is a reply
    # that never finds its lead.
    assert keys == {
        ("linkedin", "https://linkedin.com/in/anapop"),
        ("email", "ana@stardental.ro"),
    }

    ledger = await _ledger(db, item.id)
    assert len(ledger) == 1
    assert ledger[0].from_bot_id is None
    assert ledger[0].to_bot_id == agent_bot.id
    # The field the HTTP create cannot fill in: a person creating from the UI is
    # the actor and there is no initiating bot. Here there is one.
    assert ledger[0].actor_bot_id == agent_bot.id
    assert ledger[0].actor_user_id == user_a.id
    assert ledger[0].source == work_items_service.SOURCE_CREATE

    audit = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "work_item_created")
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].actor_user_id == user_a.id
    assert audit[0].detail["via"] == "agent_tool"
    assert audit[0].detail["keys"] == 2


async def test_handing_it_to_sales_is_one_call_and_a_ledger_row(
    agent_with, db, user_a, make_thread, agent_bot, make_bot
):
    """"Handed to sales" as a recorded event with a reason, not a date column."""
    sales = await make_bot(user_a, name="Sales", slug=_slug("sales"), daily_budget_usd=500.0)
    thread = await make_thread(user_a, [agent_bot, sales])

    await turn(
        agent_with(
            [
                acts("", call(TOOL_CREATE_WORK_ITEM, **PROSPECT)),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged.")),
            ]
        ),
        db,
        user_a,
        thread,
        "log star dental",
        mention_bot_ids=[agent_bot.id],
    )
    item = (await _items(db, user_a))[0]

    orchestrator = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_UPDATE_WORK_ITEM,
                    id=str(item.id),
                    status="working",
                    detail={"stage": "replied", "reply": "asked for a price"},
                ),
                call(
                    TOOL_TRANSFER_WORK_ITEM,
                    id=str(item.id),
                    to_slug=sales.slug,
                    reason="They asked for a price; that is yours, not mine.",
                ),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Ana replied; Sales has it.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "ana replied", mention_bot_ids=[agent_bot.id])

    await db.refresh(item)
    assert item.owner_bot_id == sales.id
    # `working` was set by the update call above and the transfer put it back to
    # `open`: a new owner has not started yet, and `open` with an owner is
    # exactly what `services.work_dispatch` claims. The old expectation here —
    # that a transfer left the status alone — was the same assumption as "a
    # transfer wakes nobody".
    assert item.status == "open"
    assert item.dispatched_at is None, "the new owner is not owed a run"
    assert item.transferred_at is not None
    # Merged, not replaced: the eight fields the first call wrote are still
    # there, and the two new ones sit alongside them.
    assert item.detail["pitch"].startswith("Two weeks")
    assert item.detail["stage"] == "replied"
    assert item.detail["reply"] == "asked for a price"

    ledger = await _ledger(db, item.id)
    assert len(ledger) == 2
    handover = ledger[-1]
    assert handover.from_bot_id == agent_bot.id
    assert handover.to_bot_id == sales.id
    assert handover.actor_user_id == user_a.id, "the actor is the human, not the bot"
    assert handover.actor_bot_id == agent_bot.id, "and the bot that drove it is named"
    assert handover.source == SOURCE_AGENT
    assert "asked for a price" in handover.reason


async def test_a_transfer_queues_the_other_bot_without_running_it_inline(
    agent_with, db, user_a, make_thread, agent_bot, make_bot
):
    """A transfer starts the new owner — out of band, not inside this turn.

    The original version of this test asserted that a transfer started nobody
    at all, and the argument for that was half right. The half that still holds
    is asserted here: a transfer must not run the receiving bot *inline*. A
    delegation is synchronous and capped — three hops, six per chain, thirty
    minutes — because a person is waiting on the request; fusing the two would
    mean a bot out of hops could no longer hand a lead over, and would smuggle
    a run start into a tool that is not the place where this system decides a
    run may begin.

    The half that was wrong is the conclusion. "Ownership moves and nothing
    starts" is what produced a chief of staff which decomposed a month-long
    goal into assigned items, reported that it had routed the work, and started
    nobody — reported by the owner as the product being in a worse state than
    it had been in months. So the transfer marks the row and
    `services.work_dispatch` runs the new owner seconds later, through
    `Orchestrator.handle_user_message`: the same single door a person's message
    and an inbound email both go through, so nothing is smuggled past it.
    """
    sales = await make_bot(user_a, name="Sales", slug=_slug("sales"), daily_budget_usd=500.0)
    thread = await make_thread(user_a, [agent_bot, sales])
    await turn(
        agent_with(
            [
                acts("", call(TOOL_CREATE_WORK_ITEM, **PROSPECT)),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged.")),
            ]
        ),
        db,
        user_a,
        thread,
        "log it",
        mention_bot_ids=[agent_bot.id],
    )
    item = (await _items(db, user_a))[0]

    orchestrator = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_TRANSFER_WORK_ITEM,
                    id=str(item.id),
                    to_slug=sales.slug,
                    reason="Warm; close it.",
                ),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Sales has it.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "hand it over", mention_bot_ids=[agent_bot.id])

    runs = (
        await db.execute(select(Run).where(Run.bot_id == sales.id))
    ).scalars().all()
    assert runs == [], "a transfer ran the receiving bot inside the caller's turn"

    # What it did instead: left the row owed a run, which is the dispatcher's
    # queue. One indexed predicate, asserted here so a change to either half
    # cannot silently stop matching the other.
    await db.refresh(item)
    assert item.owner_bot_id == sales.id
    assert item.status == "open"
    assert item.dispatched_at is None

    reply = next(t for t in _tool_replies(orchestrator.router) if "now owns" in t)
    assert "being started on it now" in reply
    assert "report back on this thread" in reply


async def test_the_briefs_pipeline_stages_are_refused_as_statuses(
    agent_with, db, user_a, make_thread, agent_bot
):
    """`qualified → messaged → replied → …` is one studio's sales process.

    `open/working/waiting/closed` answers a different and much more stable
    question — who acts next — and every stage in the brief maps onto it. The
    refusal teaches both vocabularies in one sentence and, critically, writes
    nothing: a model that got a 200 back would believe `qualified` is a status
    and build the rest of the run on it, while the row said `open`.
    """
    orchestrator = agent_with(
        [
            acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Acme", status="qualified")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Could not log it.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread, "log acme")

    assert await _items(db, user_a) == [], "a refused status still created a row"
    refusal = _tool_replies(orchestrator.router)[0]
    assert "nothing was written" in refusal.lower()
    for status in ("open", "working", "waiting", "closed"):
        assert status in refusal
    assert "detail" in refusal


async def test_the_same_address_on_two_records_is_reported_as_two(
    agent_with, db, user_a, make_thread, agent_bot
):
    """`resolve_by_key` returns ordered candidates on purpose. Nothing here may
    turn that into a single answer.

    Two sellers on one account, or a lead that closed in March and came back in
    August, are both honest. A tool that printed the first row and called it
    "the" work item would be inventing a uniqueness the schema explicitly
    refuses to have.
    """
    thread = await make_thread(user_a, [agent_bot])
    shared = {"email": "ana@stardental.ro"}
    await turn(
        agent_with(
            [
                acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Star Dental", keys=shared)),
                acts(
                    "",
                    call(TOOL_CREATE_WORK_ITEM, type="lead", title="Star Dental (2024)", keys=shared),
                ),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged both.")),
            ]
        ),
        db,
        user_a,
        thread,
        "log both",
    )
    assert len(await _items(db, user_a)) == 2

    orchestrator = agent_with(
        [
            acts("", call(TOOL_FIND_WORK_ITEMS, key="Ana@StarDental.ro")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Two of them.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "who is ana")

    found = _tool_replies(orchestrator.router)[0]
    assert found.startswith("2 work item(s) match")
    assert "Star Dental" in found and "Star Dental (2024)" in found
    assert "more than one" in found.lower()
    assert "rather than by which came first" in found


async def test_logging_a_duplicate_is_allowed_and_said_out_loud(
    agent_with, db, user_a, make_thread, agent_bot
):
    """The de-duplication the brief wanted, done without inventing a constraint.

    A hard refusal here would be the unique index on `(channel, value)` that the
    schema rejected, moved one layer up where it is less visible; a silent merge
    would fuse two sellers' rows. So the row is written and the collision is
    reported with ids, and the model decides.
    """
    thread = await make_thread(user_a, [agent_bot])
    orchestrator = agent_with(
        [
            acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Star Dental", keys={"email": "ana@stardental.ro"})),
            acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Star Dental again", keys={"email": "ana@stardental.ro"})),
            acts("", call(TOOL_TASK_COMPLETE, summary="Logged.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "log it twice")

    assert len(await _items(db, user_a)) == 2
    second = _tool_replies(orchestrator.router)[1]
    assert "already carry one of those addresses" in second
    assert "nothing was merged" in second


async def test_handing_the_same_record_over_twice_records_one_handover(
    agent_with, db, user_a, make_thread, agent_bot, make_bot
):
    """A model can call a tool twice. The ledger has to be *true* before it is
    complete, so the second call writes nothing and says so."""
    sales = await make_bot(user_a, name="Sales", slug=_slug("sales"), daily_budget_usd=500.0)
    thread = await make_thread(user_a, [agent_bot, sales])
    await turn(
        agent_with(
            [
                acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Acme")),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged.")),
            ]
        ),
        db,
        user_a,
        thread,
        "log acme",
        mention_bot_ids=[agent_bot.id],
    )
    item = (await _items(db, user_a))[0]

    orchestrator = agent_with(
        [
            acts("", call(TOOL_TRANSFER_WORK_ITEM, id=str(item.id), to_slug=sales.slug, reason="theirs")),
            acts("", call(TOOL_TRANSFER_WORK_ITEM, id=str(item.id), to_slug=sales.slug, reason="theirs")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "hand it over", mention_bot_ids=[agent_bot.id])

    ledger = await _ledger(db, item.id)
    assert len(ledger) == 2, "create + one handover, not two handovers"
    assert "already holds" in _tool_replies(orchestrator.router)[1]


async def test_the_ledger_outlives_the_record_an_agent_created(
    agent_with, db, user_a, make_thread, agent_bot, make_bot
):
    """`work_item_transfers` carries no foreign key, and this is why.

    Deleting a lead must not delete the record that a bot handed it to Sales on
    a Tuesday, or the delete becomes the way to erase the audit trail.
    """
    sales = await make_bot(user_a, name="Sales", slug=_slug("sales"), daily_budget_usd=500.0)
    thread = await make_thread(user_a, [agent_bot, sales])
    await turn(
        agent_with(
            [
                acts(
                    "",
                    call(TOOL_CREATE_WORK_ITEM, type="lead", title="Acme"),
                ),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged.")),
            ]
        ),
        db,
        user_a,
        thread,
        "log acme",
        mention_bot_ids=[agent_bot.id],
    )
    item = (await _items(db, user_a))[0]
    await turn(
        agent_with(
            [
                acts(
                    "",
                    call(TOOL_TRANSFER_WORK_ITEM, id=str(item.id), to_slug=sales.slug, reason="theirs"),
                ),
                acts("", call(TOOL_TASK_COMPLETE, summary="Handed over.")),
            ]
        ),
        db,
        user_a,
        thread,
        "hand it over",
        mention_bot_ids=[agent_bot.id],
    )

    item_id = item.id
    await db.delete(item)
    await db.commit()

    assert len(await _ledger(db, item_id)) == 2
    keys = (
        await db.execute(select(WorkItemKey).where(WorkItemKey.work_item_id == item_id))
    ).scalars().all()
    assert keys == [], "the keys should go with the row; only the ledger survives"


# ---------------------------------------------------------------------------
# 3. Negative controls
# ---------------------------------------------------------------------------


async def test_another_persons_record_is_not_found_rather_than_forbidden(
    agent_with, db, user_a, user_b, make_thread, make_bot, agent_bot
):
    """404-shaped, never 403-shaped, and unchanged afterwards.

    A 403 confirms the id exists, which is the one fact another tenant must not
    be able to probe for — and an agent loop is precisely where a model can be
    talked into reading an id out loud. The same rule `_get_owned_work_item`
    applies over HTTP, applied to a caller that cannot read a status code.
    """
    other_bot = await make_bot(user_b, name="Theirs", slug=_slug("theirs"))
    other_thread = await make_thread(user_b, [other_bot])
    await turn(
        agent_with(
            [
                acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Not yours", summary="private")),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged.")),
            ]
        ),
        db,
        user_b,
        other_thread,
        "log it",
    )
    theirs = (await _items(db, user_b))[0]

    thread = await make_thread(user_a, [agent_bot])
    orchestrator = agent_with(
        [
            acts(
                "",
                call(TOOL_FIND_WORK_ITEMS, id=str(theirs.id)),
                call(TOOL_UPDATE_WORK_ITEM, id=str(theirs.id), status="closed", resolution="lost"),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Nothing there.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "look at that lead")

    replies = _tool_replies(orchestrator.router)
    assert len(replies) == 2
    for reply in replies:
        assert f"There is no work item {theirs.id}" in reply
        for leak in ("forbidden", "not yours", "permission", "another", "Not yours", "private"):
            assert leak not in reply, reply

    await db.refresh(theirs)
    assert theirs.status == "open" and theirs.resolution is None
    assert await _items(db, user_a) == []


async def test_a_transfer_can_only_reach_this_persons_own_bots(
    agent_with, db, user_a, user_b, make_thread, make_bot, agent_bot
):
    """Two bots the model cannot hand to, one answer.

    `off_thread` belongs to the same human and is simply not on this thread;
    `theirs` belongs to somebody else entirely, and that is the one that is
    still refused. `off_thread` is this person's own bot and is now a legitimate
    target: an assignment wakes its owner (`services.work_dispatch`), so
    "they are not in this room" stopped being a reason to refuse the row.

    The boundary that matters — and the only one a model could try to probe —
    is visibility, and it is unchanged.
    """
    off_thread = await make_bot(user_a, name="Elsewhere", slug=_slug("elsewhere"), daily_budget_usd=500.0)
    theirs = await make_bot(user_b, name="Theirs", slug=_slug("theirs"))
    on_thread = await make_bot(user_a, name="Sales", slug=_slug("sales"), daily_budget_usd=500.0)
    thread = await make_thread(user_a, [agent_bot, on_thread])

    await turn(
        agent_with(
            [
                acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Acme")),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged.")),
            ]
        ),
        db,
        user_a,
        thread,
        "log acme",
    )
    item = (await _items(db, user_a))[0]

    orchestrator = agent_with(
        [
            acts(
                "",
                call(TOOL_TRANSFER_WORK_ITEM, id=str(item.id), to_slug=theirs.slug, reason="go"),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Could not hand it over.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "hand it to somebody else's bot")

    replies = _tool_replies(orchestrator.router)
    assert len(replies) == 1
    assert f"There is no bot called '{theirs.slug}' on this person's team" in replies[0]
    assert "nothing was handed over" in replies[0]
    # The roster it offers instead is this person's own team and no part of
    # anybody else's — the refusal must not become a directory of bot slugs.
    offered = replies[0].split("Your teammates: ", 1)[1]
    assert theirs.slug not in offered

    await db.refresh(item)
    assert item.owner_bot_id == agent_bot.id
    assert item.transferred_at is None
    assert len(await _ledger(db, item.id)) == 1, "a refused transfer wrote a ledger row"

    # And the same call for this person's *own* bot, which is not on the
    # thread, is now accepted: that is the change.
    accepted = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_TRANSFER_WORK_ITEM,
                    id=str(item.id),
                    to_slug=off_thread.slug,
                    reason="theirs to own",
                ),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Handed it over.")),
        ]
    )
    await turn(accepted, db, user_a, thread, "hand it to elsewhere")
    await db.refresh(item)
    assert item.owner_bot_id == off_thread.id
    assert item.dispatched_at is None, "an assignment left nobody owed a run"


async def test_a_value_lookup_cannot_reach_across_tenants(db, user_a, user_b, make_bot):
    """`resolve_by_value` drops the channel from the predicate, so `owner_user_id`
    is the only thing left between it and every tenant's addresses. It is
    required rather than optional for exactly that reason."""
    mine = await make_bot(user_a, name="Mine", slug=_slug("mine"))
    theirs = await make_bot(user_b, name="Theirs", slug=_slug("theirs"))
    for user, bot, title in ((user_a, mine, "Mine"), (user_b, theirs, "Theirs")):
        item = WorkItem(
            type="lead", title=title, owner_bot_id=bot.id, owner_user_id=user.id, detail={}
        )
        db.add(item)
        await db.flush()
        db.add(
            WorkItemKey(
                work_item_id=item.id,
                channel="email",
                value="shared@example.test",
                owner_user_id=user.id,
            )
        )
    await db.commit()

    for user, expected in ((user_a, "Mine"), (user_b, "Theirs")):
        found = await work_items_service.resolve_by_value(
            db, " Shared@Example.TEST ", owner_user_id=user.id
        )
        assert [i.title for i in found] == [expected]


async def test_a_closed_record_is_not_handed_over(
    agent_with, db, user_a, make_thread, agent_bot, make_bot
):
    """The 409 the HTTP lane raises, as a sentence. Reopening is a real decision
    and is not made on the way past."""
    sales = await make_bot(user_a, name="Sales", slug=_slug("sales"), daily_budget_usd=500.0)
    thread = await make_thread(user_a, [agent_bot, sales])
    await turn(
        agent_with(
            [
                acts(
                    "",
                    call(
                        TOOL_CREATE_WORK_ITEM,
                        type="lead",
                        title="Acme",
                        status="closed",
                    ),
                ),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged and closed.")),
            ]
        ),
        db,
        user_a,
        thread,
        "log and close",
    )
    item = (await _items(db, user_a))[0]
    assert item.closed_at is not None

    orchestrator = agent_with(
        [
            acts("", call(TOOL_TRANSFER_WORK_ITEM, id=str(item.id), to_slug=sales.slug, reason="go")),
            acts("", call(TOOL_TASK_COMPLETE, summary="It is closed.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "hand it over", mention_bot_ids=[agent_bot.id])

    assert "is closed" in _tool_replies(orchestrator.router)[0]
    await db.refresh(item)
    assert item.owner_bot_id == agent_bot.id
    assert len(await _ledger(db, item.id)) == 1


async def test_ownership_cannot_be_moved_through_the_update_tool(
    agent_with, db, user_a, make_thread, agent_bot, make_bot
):
    """`UpdateWorkItemIn` answers 422 to `owner_bot_id` rather than dropping it,
    because a 200 that dropped the field reads as a handover that happened. The
    tool makes the same refusal for the same reason."""
    sales = await make_bot(user_a, name="Sales", slug=_slug("sales"), daily_budget_usd=500.0)
    thread = await make_thread(user_a, [agent_bot])
    await turn(
        agent_with(
            [
                acts("", call(TOOL_CREATE_WORK_ITEM, type="lead", title="Acme")),
                acts("", call(TOOL_TASK_COMPLETE, summary="Logged.")),
            ]
        ),
        db,
        user_a,
        thread,
        "log acme",
        mention_bot_ids=[agent_bot.id],
    )
    item = (await _items(db, user_a))[0]

    orchestrator = agent_with(
        [
            acts("", call(TOOL_UPDATE_WORK_ITEM, id=str(item.id), to_slug=sales.slug, status="working")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Refused.")),
        ]
    )
    await turn(orchestrator, db, user_a, thread, "give it to sales")

    reply = _tool_replies(orchestrator.router)[0]
    assert "nothing was changed" in reply.lower()
    assert TOOL_TRANSFER_WORK_ITEM in reply
    await db.refresh(item)
    assert item.owner_bot_id == agent_bot.id
    assert item.status == "open", "the rest of the call was applied anyway"


async def test_a_detail_block_too_large_to_store_is_refused_whole(
    agent_with, db, user_a, make_thread, agent_bot
):
    """Refused rather than truncated: truncating a JSON object drops whichever
    fields sorted last, silently, and a pipeline missing its quote amount for
    that reason is worse than a call the model can retry smaller."""
    orchestrator = agent_with(
        [
            acts(
                "",
                call(
                    TOOL_CREATE_WORK_ITEM,
                    type="lead",
                    title="Acme",
                    detail={"notes": "x" * 6000},
                ),
            ),
            acts("", call(TOOL_TASK_COMPLETE, summary="Too big.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread, "log acme")

    assert await _items(db, user_a) == []
    assert "Nothing was written" in _tool_replies(orchestrator.router)[0]


async def test_a_work_item_call_never_becomes_a_step_in_the_transcript(
    agent_with, db, user_a, make_thread, agent_bot
):
    """Why these four are absent from `_STEP_PHRASES`, asserted rather than assumed.

    That table renders the "what happened on the machine" transcript, and its
    drift guard covers `DESKTOP_ACTIONS` and `BROWSER_ACTIONS` — the two
    surfaces that produce `steps` rows. A work-item call touches no machine and
    writes no row, on the same precedent as `delegate_to_bot`: what the person
    reads about it arrives through `notes`.

    If that ever changes, this fails first and the phrase table gets four rows
    before a reply says "Ran create work item".
    """
    orchestrator = agent_with(
        [
            acts("", call(TOOL_CREATE_WORK_ITEM, **PROSPECT)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Logged Star Dental.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "log it")

    # The phrase table's guard is scoped to the two surfaces that produce step
    # rows. These are in neither, deliberately, and none of them has a phrase.
    assert not WORK_ITEM_TOOL_NAMES & (set(DESKTOP_ACTIONS) | set(BROWSER_ACTIONS))
    assert not WORK_ITEM_TOOL_NAMES & set(_STEP_PHRASES)
    # The transcript the reply renders is the machine's, and nothing here
    # touched a machine — so it stays empty rather than growing a row the
    # phrase table would have to describe.
    assert "Ran create work item" not in str(done.get("message") or "")
    # It is still a visible event, on its own connector rather than dressed up
    # as a desktop action.
    tools = [d for name, d in frames if name == "tool"]
    assert [(d["connector"], d["action"], d["ok"]) for d in tools] == [
        ("work_items", TOOL_CREATE_WORK_ITEM, True)
    ]


async def test_the_reply_the_person_reads_never_names_a_tool(
    agent_with, db, user_a, make_thread, agent_bot, make_bot
):
    """`test_reply_wording.py` forbids a reply naming a `snake_case` tool. These
    write into the same `notes` a hand-off does, so they are held to it."""
    sales = await make_bot(user_a, name="Sales", slug=_slug("sales"), daily_budget_usd=500.0)
    thread = await make_thread(user_a, [agent_bot, sales])
    orchestrator = agent_with(
        [
            acts("", call(TOOL_CREATE_WORK_ITEM, **PROSPECT)),
            acts("", call(TOOL_UPDATE_WORK_ITEM, id="not-an-id", status="working")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Logged Star Dental for you.")),
        ]
    )

    _frames, done = await turn(orchestrator, db, user_a, thread, "log star dental", mention_bot_ids=[agent_bot.id])

    reply = str(done.get("message") or "")
    assert "Star Dental" in reply
    for name in agent_tool_names():
        if "_" in name:
            assert name not in reply, f"the reply names the tool {name!r}"
