"""The work-item tools an agent can actually call, and what they answer with.

Why this module exists
----------------------
`services/work_items.py` gives a work item an owner, a status and a ledger, and
`routers/work_items.py` puts all of it behind HTTP. Neither is reachable from
inside a turn. The agent loop's whole vocabulary was thirty-nine desktop, DOM
and control tools plus `delegate_to_bot`, and not one of them could write a row
— so the object the cowork story is built on was invisible to the only actor
that was supposed to use it. A bot asked to "log every qualified prospect" had
nowhere to put one, and a bot told to "hand it to Sales with the diagnosis"
could hand over a *sentence* and nothing else.

These four tools close that. They are the same four verbs the HTTP lane has —
create, find, update, transfer — expressed for a caller that has no session, no
request body and no way to read a 404.

What is deliberately the same as HTTP
-------------------------------------
* **Scope is the human, and not-yours is indistinguishable from not-there.**
  Every statement here filters on `owner_user_id`, which for a delegated run is
  still the *originating* human — the actor inherited down the whole chain. A
  work item belonging to somebody else answers "there is no work item with that
  id", never "you may not touch that", for the reason `_get_owned_work_item`
  gives: the second sentence confirms the id exists.
* **One function moves ownership.** `work_items.transfer_work_item` is called
  here exactly as the router calls it, so the ledger row cannot be skipped by
  reaching the entity from a new direction. That function's docstring already
  anticipated this caller.
* **The status vocabulary does not grow.** See `_STATUS_PROPERTY`.

What is deliberately different
------------------------------
* **`detail` is merged, not replaced.** `PATCH /work-items/{id}` replaces it,
  and it is right to: an HTTP client holds the object it just read. A model does
  not. Asking it to re-send the whole blob to add a quote number is both a bill
  and a data-loss bug waiting for the one turn where it forgets a field.
* **`keys` are added, not replaced**, for the same reason. Removing a stale
  address stays a job for the API, where the caller can see the full set.
* **Refusals are sentences.** A tool result is the only thing the model reads,
  so every refusal says what was wrong, what to send instead, and — the part
  that matters — whether anything was written. "Nothing was created" is a fact a
  model needs before it decides whether to retry.
* **Ambiguity is reported, never collapsed.** `resolve_by_key` returns ordered
  candidates on purpose; a tool that turned that into "the" work item would be
  inventing a certainty the schema explicitly refuses to have.

What is not here
----------------
No risk classification and no gate. `services/risk.py::classify_action_risk`
stays the single classifier and `simulation.perform` the single chokepoint, and
both exist for effects that leave the tenant. Creating a row, editing a row and
moving a row between the customer's own bots reach nothing outside, are undone
by the same tools, and are all recorded. The argument is set out in full in
`services/work_items.py`'s module docstring and this module does not reopen it.

No waking the receiving bot on a transfer either — see `TOOL_TRANSFER_WORK_ITEM`.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Bot, Thread, User, WorkItem, WorkItemKey, WorkItemTransfer
from app.services import work_items as work_items_service
from app.services.work_items import WORK_ITEM_STATUSES

logger = logging.getLogger("nesqbot.agent_work_items")

TOOL_CREATE_WORK_ITEM = "create_work_item"
TOOL_FIND_WORK_ITEMS = "find_work_items"
TOOL_UPDATE_WORK_ITEM = "update_work_item"
TOOL_TRANSFER_WORK_ITEM = "transfer_work_item"

#: Every tool this module dispatches. The orchestrator folds it into
#: `agent_tool_names()` so a model naming one of these gets the action rather
#: than "there is no tool called that", whether or not it was advertised on the
#: request it named it in.
WORK_ITEM_TOOL_NAMES: frozenset[str] = frozenset(
    {
        TOOL_CREATE_WORK_ITEM,
        TOOL_FIND_WORK_ITEMS,
        TOOL_UPDATE_WORK_ITEM,
        TOOL_TRANSFER_WORK_ITEM,
    }
)

#: How the ledger records a handover a bot drove from inside a turn. Distinct
#: from `SOURCE_API` (a person, over HTTP) and from the delegation lane's own
#: value, so "who handed this to Sales" is answerable without joining anything.
SOURCE_AGENT = "agent"

# ---------------------------------------------------------------------------
# Limits. Every one of these is a cap on what a *model* can put in a row.
# ---------------------------------------------------------------------------
#
# The row is read back into a later request, shown to a person, and in the case
# of `detail` re-sent whenever the item is looked up. A model that decides to
# paste an accessibility snapshot into `summary` should lose the paste, not the
# turn — so these truncate quietly where the excess is obviously noise, and
# refuse loudly where the excess means the model misunderstood the field.

TYPE_MAX_CHARS = 40
TITLE_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 2000
RESOLUTION_MAX_CHARS = 120
REASON_MAX_CHARS = 400

#: `detail` is refused rather than truncated past this. Truncating a JSON object
#: means dropping whichever fields sorted last, silently, and a pipeline missing
#: its quote amount for that reason is worse than a refused call the model can
#: retry smaller.
DETAIL_MAX_CHARS = 4000

#: External identities accepted in one call. Eight covers an email, a work
#: email, a phone, a mobile, a LinkedIn profile and a CRM id with room over.
KEYS_MAX = 8

FIND_LIMIT_DEFAULT = 5
FIND_LIMIT_MAX = 10

#: How much of a summary a list row shows. A find that returns five items with
#: 2,000 characters of summary each is 2,500 tokens of tool result riding along
#: on every subsequent request of the run.
SUMMARY_PREVIEW_CHARS = 160
#: The single-item view is allowed more, because the model asked for that one.
SUMMARY_FULL_CHARS = 600
DETAIL_PREVIEW_CHARS = 800


# ---------------------------------------------------------------------------
# The schemas
# ---------------------------------------------------------------------------
#
# Same shape as `orchestrator.CONTROL_TOOL_SCHEMAS` — `{description,
# properties, required}` — because the orchestrator renders both through the
# same `_function_tool`, and a second shape would be a second place for a
# property to go missing.
#
# Terse on purpose. These are re-sent on every request of every turn that can
# reach them; the measured cost is asserted in
# `tests/services/test_agent_work_item_tools.py`, and anything that can be said
# once in a tool *result* is said there instead, where it is paid for once.

#: The one paragraph worth its tokens, and the design decision this lane was
#: asked to defend.
#:
#: The brief that motivated this asks for `qualified → messaged → replied →
#: handed to sales → quoting → quote sent → won/lost`. Those are not statuses.
#: They are one Romanian studio's sales process, and a status column that fits
#: it fits nobody else — the next customer's is `triaged → dispatched →
#: on site → invoiced`, and the third's has no pipeline at all.
#:
#: `open/working/waiting/closed` answers a different and much more stable
#: question: **who is expected to act next.** Nobody yet, us, them, no-one ever
#: again. Every stage in that brief maps onto it without loss —
#:
#:     qualified      -> open      (logged, nothing done)
#:     messaged       -> waiting   (we acted; the lead has the ball)
#:     replied        -> working   (the ball is back)
#:     quoting        -> working
#:     quote sent     -> waiting
#:     won / lost     -> closed + resolution
#:
#: — and the one stage that has no status at all, *handed to sales*, is the
#: interesting one: it is not a state of the work, it is a change of owner. The
#: brief wants it as a column because a spreadsheet has no other way to record
#: it. `work_item_transfers` records it with who, to whom, when and why, which
#: is strictly more than the column could have held.
#:
#: So the stage stays the customer's, in `detail`, where a vocabulary can be
#: whatever that customer's process is and nobody has to migrate a database to
#: add "nurturing".
_STATUS_PROPERTY: dict[str, Any] = {
    "type": "string",
    "enum": list(WORK_ITEM_STATUSES),
    "description": (
        "Who acts next: open = nobody yet, working = you, waiting = them, closed = done "
        "(say how in `resolution`). A pipeline stage - qualified, messaged, quoted - is "
        "not a status; put it in `detail`."
    ),
}

WORK_ITEM_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    TOOL_CREATE_WORK_ITEM: {
        "description": (
            "Log work you own - a lead, a ticket, a job - as a record that outlives this "
            "conversation and can be handed to another bot. find_work_items first, so the "
            "same one is not logged twice."
        ),
        "properties": {
            "type": {
                "type": "string",
                "description": "lead, ticket, invoice. One lowercase word, the same one every time.",
            },
            "title": {
                "type": "string",
                "description": "A few words - usually the company or the person.",
            },
            "summary": {
                "type": "string",
                "description": "What you found and why it is worth working.",
            },
            # The short form. `update_work_item` carries the full vocabulary
            # note, because that is where a wrong status is actually reached
            # for; paying for the paragraph here as well would be paying twice
            # for a lesson only one of the two tools needs to teach.
            "status": {
                "type": "string",
                "enum": list(WORK_ITEM_STATUSES),
                "description": "open unless you have already acted; waiting if it is with them.",
            },
            "detail": {
                "type": "object",
                "description": (
                    "Your own field names: role, profile_url, verdict, pitch, quote, "
                    "stage. Facts you saw. Never credentials."
                ),
            },
            "keys": {
                "type": "object",
                "description": (
                    "channel: address - email, linkedin, phone. How a reply finds this "
                    "record later."
                ),
            },
        },
        "required": ["type", "title"],
    },
    TOOL_FIND_WORK_ITEMS: {
        "description": (
            "Records your person already has - check before logging a new one, and get "
            "the id you need to update or hand one on. Returns every candidate, most "
            "recent activity first; more than one can match."
        ),
        "properties": {
            "id": {"type": "string", "description": "An id you have. Returns it in full."},
            "key": {"type": "string", "description": "Or an exact address: email, LinkedIn URL, phone."},
            "query": {"type": "string", "description": "Or words from the title or summary."},
            "status": {
                "type": "string",
                "enum": list(WORK_ITEM_STATUSES),
                "description": "One state only - waiting is your stalled outreach.",
            },
            # `include_closed` is deliberately not offered. The one place a
            # finished record matters is "have we had this company before",
            # and `create_work_item` already answers that unasked: its
            # duplicate check searches closed items too and reports what it
            # found. A flag would be 22 tokens a request for a question that
            # is answered without it.
        },
        "required": [],
    },
    TOOL_UPDATE_WORK_ITEM: {
        "description": (
            "Change a work item you have the id for: where it got to, what you now know, "
            "how it ended. Only the fields you send are touched; `detail` is merged into "
            "what is there, not replacing it."
        ),
        "properties": {
            "id": {"type": "string", "description": "From find_work_items or create_work_item."},
            "status": _STATUS_PROPERTY,
            "summary": {
                "type": "string",
                "description": "Replaces it. What happened, not what you plan.",
            },
            "detail": {"type": "object", "description": "Fields to add or overwrite."},
            "resolution": {
                "type": "string",
                "description": "With closed: won, lost, not a fit, no reply.",
            },
            "keys": {"type": "object", "description": "More addresses to recognise a reply on."},
        },
        "required": ["id"],
    },
    # The second design question this lane was asked, answered in the one place
    # a model will read it.
    #
    # A transfer and a delegation look alike and are not. `delegate_to_bot` is
    # synchronous, capped at three hops, six per chain and thirty minutes,
    # because a person is sitting on the other end of the request while every
    # bot in the chain works. A transfer is a durable change of owner that is
    # most useful at three in the morning with nobody waiting.
    #
    # Fusing them - "transferring wakes the receiving bot" - fails on the caps.
    # A bot at hop three could then no longer hand a lead over *at all*, because
    # a delegation cap would be refusing a database write that has nothing to do
    # with it. The reverse fusion is worse: an ungated action that starts a run
    # smuggles token spend, and a path to outbound effects, past the one place
    # this system decides whether a run may start.
    #
    # So they stay separate and compose in one batch: transfer, then delegate.
    # That order is deliberate and the description says it - the handover
    # survives even if the delegation is then refused for a cap, which is
    # exactly the failure the fused design would have made unrecoverable.
    TOOL_TRANSFER_WORK_ITEM: {
        "description": (
            "Hand a work item to another bot on this thread. It becomes theirs and the "
            "handover goes on the record with your reason. It does not start them working "
            "- call delegate_to_bot as well, after this, if it has to happen now."
        ),
        "properties": {
            "id": {"type": "string", "description": "From find_work_items or create_work_item."},
            "to_slug": {
                "type": "string",
                "description": "A bot on this thread. A wrong one is refused, listing the right ones.",
            },
            "reason": {
                "type": "string",
                "description": "Why they are getting it. The ledger line a person reads later.",
            },
        },
        "required": ["id", "to_slug", "reason"],
    },
}


@dataclass(frozen=True)
class WorkItemToolResult:
    """One tool call's outcome, in the words each layer needs.

    Same three-audience shape as `orchestrator.DelegationResult`, and for the
    same reason: the model needs to know what to do next, the person needs a
    sentence in the reply, and the loop needs a boolean and any ids the call
    surfaced. Writing one string for all three produces a model instruction in
    a user-facing summary.
    """

    ok: bool
    code: str
    to_model: str
    to_human: str
    #: Work-item ids this call put in front of the model. What makes
    #: `update_work_item` and `transfer_work_item` advertisable on the next
    #: request — see `context_budget.ToolContext.work_item_held`.
    ids: tuple[str, ...] = field(default=())


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------


def _text(arguments: dict[str, Any], key: str, limit: int) -> str:
    """One string argument, trimmed and capped. Absent and empty are the same."""
    value = arguments.get(key)
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _work_item_id(arguments: dict[str, Any], key: str = "id") -> uuid.UUID | None:
    raw = arguments.get(key)
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_keys(raw: Any) -> list[tuple[str, str]]:
    """`{"email": "a@b.test", "phone": ["+40…", "+41…"]}` → normalised pairs.

    An object keyed by channel rather than the API's `[{channel, value}]`,
    because that is the shape a model gets right first time and it is a third
    of the schema. Lists are accepted per channel so a second address is not a
    second call. Everything goes through `work_items.normalise_key`, the one
    function both the writer and the inbound reader use — a key normalised any
    other way is a reply that never finds its lead.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(channel: Any, value: Any) -> None:
        channel_norm, value_norm = work_items_service.normalise_key(str(channel), str(value))
        if not channel_norm or not value_norm or (channel_norm, value_norm) in seen:
            return
        seen.add((channel_norm, value_norm))
        pairs.append((channel_norm, value_norm))

    if isinstance(raw, dict):
        for channel, value in raw.items():
            if isinstance(value, (list, tuple)):
                for one in value:
                    add(channel, one)
            elif value is not None:
                add(channel, value)
    elif isinstance(raw, list):
        # The API's shape, accepted rather than refused: a model that has read
        # the HTTP contract is not wrong, only verbose.
        for entry in raw:
            if isinstance(entry, dict):
                add(entry.get("channel", ""), entry.get("value", ""))
    return pairs[:KEYS_MAX]


def _detail(raw: Any) -> tuple[dict[str, Any], str]:
    """A JSON-serialisable `detail` object, or a sentence saying why not."""
    if raw is None:
        return {}, ""
    if not isinstance(raw, dict):
        return {}, "`detail` has to be an object of your own field names, not a bare value."
    try:
        encoded = json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return {}, "`detail` could not be encoded as JSON. Send plain strings and numbers."
    if len(encoded) > DETAIL_MAX_CHARS:
        return {}, (
            f"`detail` is {len(encoded)} characters and the limit is {DETAIL_MAX_CHARS}. "
            "Nothing was written. Keep it to the facts a person would want in a pipeline "
            "row and put long text in `summary`."
        )
    return json.loads(encoded), ""


def _status(arguments: dict[str, Any]) -> tuple[str, str]:
    """The requested status, or a refusal that teaches the vocabulary once.

    Refused rather than coerced, and refused rather than quietly filed into
    `detail`. A model that sends `status: "qualified"` and gets a 200 back
    believes that status exists and will build the rest of the run on it; the
    row would then say `open` while the model reports `qualified`, which is the
    single worst outcome available here. One refusal costs a step and buys a
    model that knows both vocabularies.
    """
    raw = arguments.get("status")
    if raw is None:
        return "", ""
    status = str(raw).strip().lower()
    if not status:
        return "", ""
    if status in WORK_ITEM_STATUSES:
        return status, ""
    return "", (
        f"'{status}' is not a status, so nothing was written. The statuses are "
        f"{', '.join(WORK_ITEM_STATUSES)} - they say who acts next, not where in your "
        f"pipeline this is. Put '{status}' in `detail` (for example `stage`) and send the "
        "call again with the status that says who has the ball."
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _day(value: Any) -> str:
    return value.date().isoformat() if value is not None else "-"


def _when(item: WorkItem) -> str:
    """The one date worth a line: when the outside world last did something.

    `last_event_at` where there is one, because "the lead answered on the 20th"
    is the fact that decides what to do next. `updated_at` otherwise, labelled
    differently so the two are never read as the same thing.
    """
    if item.last_event_at is not None:
        return f"reply {_day(item.last_event_at)}"
    return f"updated {_day(item.updated_at)}"


def _holder(item: WorkItem, slugs: dict[uuid.UUID, str]) -> str:
    if item.owner_bot_id is None:
        return "nobody (its bot was deleted)"
    return slugs.get(item.owner_bot_id, "a bot you cannot see")


def _line(item: WorkItem, slugs: dict[uuid.UUID, str]) -> str:
    state = item.status
    if item.resolution:
        state = f"{state}/{item.resolution}"
    head = (
        f"- {item.id} {item.type} \"{item.title}\" - {state}, "
        f"held by {_holder(item, slugs)}, {_when(item)}"
    )
    summary = (item.summary or "").strip()
    if summary:
        if len(summary) > SUMMARY_PREVIEW_CHARS:
            summary = summary[:SUMMARY_PREVIEW_CHARS].rstrip() + "…"
        head += f"\n  {summary}"
    return head


def _full(item: WorkItem, keys: list[WorkItemKey], slugs: dict[uuid.UUID, str]) -> str:
    lines = [
        f"{item.id} - {item.type} \"{item.title}\"",
        f"status {item.status}"
        + (f", resolution {item.resolution}" if item.resolution else "")
        + f", held by {_holder(item, slugs)}, created {_day(item.created_at)}, {_when(item)}",
    ]
    summary = (item.summary or "").strip()
    if summary:
        lines.append(summary[:SUMMARY_FULL_CHARS])
    if keys:
        lines.append("keys: " + ", ".join(f"{k.channel}:{k.value}" for k in keys))
    if item.detail:
        encoded = json.dumps(item.detail, default=str, sort_keys=True)[:DETAIL_PREVIEW_CHARS]
        lines.append("detail: " + encoded)
    return "\n".join(lines)


async def _slug_map(db: AsyncSession, items: list[WorkItem]) -> dict[uuid.UUID, str]:
    """Bot ids → slugs, in one query for the whole page.

    Slugs and not names: the model addresses `transfer_work_item` and
    `delegate_to_bot` by slug, so a list view that showed "Sales Bot" would be
    showing a word that is not usable in the call it is meant to prompt.
    """
    ids = {item.owner_bot_id for item in items if item.owner_bot_id is not None}
    if not ids:
        return {}
    rows = await db.execute(select(Bot.id, Bot.slug).where(Bot.id.in_(ids)))
    return {row.id: row.slug for row in rows}


# ---------------------------------------------------------------------------
# Loading, always inside the human's scope
# ---------------------------------------------------------------------------


async def _owned(db: AsyncSession, work_item_id: uuid.UUID, user: User) -> WorkItem | None:
    """The item if it is this human's, else None.

    The caller turns None into "there is no work item with that id", never into
    "that is not yours". Same rule as `_get_owned_work_item`'s 404 and the same
    reason: the second sentence confirms the id exists, and an agent loop is a
    place where a model can be talked into reading ids out loud.
    """
    item = await db.get(WorkItem, work_item_id)
    if item is None or item.owner_user_id != user.id:
        return None
    return item


async def _add_keys(db: AsyncSession, item: WorkItem, pairs: list[tuple[str, str]]) -> int:
    """Add external identities, skipping the ones already on the item.

    Read-then-insert rather than an upsert because the duplicate is the normal
    case: a model updating an item usually re-sends the address it already
    logged, and the primary key would take the whole transaction with it.
    """
    if not pairs:
        return 0
    existing = {(k.channel, k.value) for k in await work_items_service.keys_for(db, item.id)}
    added = 0
    for channel, value in pairs:
        if (channel, value) in existing:
            continue
        existing.add((channel, value))
        db.add(
            WorkItemKey(
                work_item_id=item.id,
                channel=channel,
                value=value,
                owner_user_id=item.owner_user_id,
            )
        )
        added += 1
    return added


# ---------------------------------------------------------------------------
# The four tools
# ---------------------------------------------------------------------------


async def perform(
    db: AsyncSession,
    *,
    name: str,
    arguments: dict[str, Any],
    user: User,
    bot: Bot,
    thread: Thread | None,
    targets: list[Bot],
    run_id: uuid.UUID | None = None,
) -> WorkItemToolResult:
    """Run one work-item tool call. Never raises for a bad argument.

    `targets` is the roster a transfer may name — the other bots on this
    thread, the same boundary delegation uses. It is passed in rather than
    queried here because the loop has already read it once for the turn and the
    answer cannot move underneath a running turn.
    """
    if name == TOOL_CREATE_WORK_ITEM:
        return await _create(db, arguments, user=user, bot=bot, thread=thread, run_id=run_id)
    if name == TOOL_FIND_WORK_ITEMS:
        return await _find(db, arguments, user=user)
    if name == TOOL_UPDATE_WORK_ITEM:
        return await _update(db, arguments, user=user, bot=bot, run_id=run_id)
    if name == TOOL_TRANSFER_WORK_ITEM:
        return await _transfer(db, arguments, user=user, bot=bot, targets=targets, run_id=run_id)
    raise ValueError(f"not a work-item tool: {name}")  # pragma: no cover - guarded by the caller


async def _create(
    db: AsyncSession,
    arguments: dict[str, Any],
    *,
    user: User,
    bot: Bot,
    thread: Thread | None,
    run_id: uuid.UUID | None,
) -> WorkItemToolResult:
    """Log a new item, owned by the bot that called it.

    Owned by the caller and not by a bot it names: a create that could assign
    to somebody else would be a handover with no ledger row, which is the exact
    hole `PATCH` refuses `owner_bot_id` to keep shut. Log it, then transfer it —
    two rows in the ledger and both of them true.
    """
    item_type = _text(arguments, "type", TYPE_MAX_CHARS).lower()
    title = _text(arguments, "title", TITLE_MAX_CHARS)
    if not item_type:
        return WorkItemToolResult(
            False,
            "no_type",
            "Nothing was created: you called create_work_item without a `type`. Send it "
            "again with one lowercase word - lead, ticket, invoice.",
            "I tried to log a record without saying what kind it was.",
        )
    if not title:
        return WorkItemToolResult(
            False,
            "no_title",
            "Nothing was created: you called create_work_item without a `title`. Send it "
            "again with the company or person it is about.",
            "I tried to log a record with no title, so there was nothing to file it under.",
        )

    status, status_error = _status(arguments)
    if status_error:
        return WorkItemToolResult(
            False, "bad_status", status_error, f"I tried to log {title} with a status that does not exist."
        )
    detail, detail_error = _detail(arguments.get("detail"))
    if detail_error:
        return WorkItemToolResult(
            False,
            "bad_detail",
            detail_error,
            f"I tried to log {title} with a detail block I could not store.",
        )

    pairs = _parse_keys(arguments.get("keys"))
    # Looked up *before* the insert so the answer describes the world as it was
    # without this row in it. Duplicates are reported, never refused: the schema
    # deliberately has no unique constraint on `(channel, value)`, because the
    # same person honestly can be two work items, and refusing here would be
    # inventing that constraint one layer up where it is even less visible.
    collisions = await _collisions(db, pairs, user=user)

    item = WorkItem(
        type=item_type,
        title=title,
        summary=_text(arguments, "summary", SUMMARY_MAX_CHARS),
        owner_bot_id=bot.id,
        owner_user_id=user.id,
        thread_id=thread.id if thread is not None else None,
        detail=detail,
    )
    work_items_service.apply_status(item, status or "open")
    db.add(item)
    await db.flush()
    await _add_keys(db, item, pairs)

    reason = f"logged by {bot.slug} while working on this thread"
    db.add(
        WorkItemTransfer(
            work_item_id=item.id,
            owner_user_id=user.id,
            from_bot_id=None,
            to_bot_id=bot.id,
            actor_user_id=user.id,
            # The one field the HTTP create cannot fill in: a person creating
            # from the UI is the actor and there is no initiating bot. Here
            # there is, and the ledger says so.
            actor_bot_id=bot.id,
            reason=reason,
            source=work_items_service.SOURCE_CREATE,
        )
    )
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=bot.id,
            event_type="work_item_created",
            # Same keys as the router's event so one query answers "what was
            # created today" whichever door it came through, plus `via` and the
            # run, which is the part a person auditing an unattended run needs.
            detail={
                "work_item_id": str(item.id),
                "type": item.type,
                "status": item.status,
                "keys": len(pairs),
                "via": "agent_tool",
                "run_id": str(run_id) if run_id else None,
            },
        )
    )
    await db.commit()
    await db.refresh(item)

    to_model = (
        f"Logged. Work item {item.id} - {item.type} \"{item.title}\", status {item.status}, "
        f"held by you. Use that id to update it or hand it on."
    )
    if collisions:
        to_model += "\n" + _collision_sentence(collisions)
    return WorkItemToolResult(
        True,
        "created",
        to_model,
        f"I logged **{item.title}** as a {item.type} ({item.status}).",
        (str(item.id),),
    )


async def _collisions(
    db: AsyncSession, pairs: list[tuple[str, str]], *, user: User
) -> list[WorkItem]:
    """Existing items already carrying one of these addresses. Never blocking.

    This is the de-duplication the brief actually wanted, done honestly: the
    model is told that Acme is already in the pipeline and given the ids, and
    it decides. A hard refusal here would be the unique index the schema
    rejected, and a silent merge would be worse — two sellers legitimately
    working the same account would find their rows quietly fused.
    """
    found: dict[uuid.UUID, WorkItem] = {}
    for channel, value in pairs:
        for item in await work_items_service.resolve_by_key(
            db, channel, value, owner_user_id=user.id, include_closed=True
        ):
            found[item.id] = item
    return list(found.values())[:FIND_LIMIT_MAX]


def _collision_sentence(items: list[WorkItem]) -> str:
    ids = ", ".join(str(item.id) for item in items)
    return (
        f"Note: {len(items)} work item(s) already carry one of those addresses ({ids}). "
        "The same person can honestly be two records, so nothing was merged - look before "
        "you work this one, and close it if it is a duplicate."
    )


async def _find(db: AsyncSession, arguments: dict[str, Any], *, user: User) -> WorkItemToolResult:
    """Look items up by id, by address, or by words — always owner-scoped.

    Three modes rather than three tools, because they are one question ("which
    record is this?") asked with whatever the model happens to be holding, and
    three schemas would be three times the standing cost for a saving of
    nothing.
    """
    include_closed = bool(arguments.get("include_closed"))

    work_item_id = _work_item_id(arguments)
    if arguments.get("id") is not None:
        if work_item_id is None:
            return WorkItemToolResult(
                False,
                "bad_id",
                f"'{arguments.get('id')}' is not a work item id. They look like "
                "'0f8c1d2e-...'. Search by `key` or `query` instead if you do not have one.",
                "I looked for a work item with an id that was not an id.",
            )
        item = await _owned(db, work_item_id, user)
        if item is None:
            return WorkItemToolResult(
                False,
                "not_found",
                f"There is no work item {work_item_id} that you can see. Search by `key` "
                "or `query`, or log a new one.",
                "I looked up a work item that does not exist here.",
            )
        keys = await work_items_service.keys_for(db, item.id)
        slugs = await _slug_map(db, [item])
        return WorkItemToolResult(
            True,
            "found_one",
            _full(item, keys, slugs),
            f"I looked up **{item.title}**.",
            (str(item.id),),
        )

    key = _text(arguments, "key", 400)
    if key:
        items = await work_items_service.resolve_by_value(
            db, key, owner_user_id=user.id, include_closed=include_closed
        )
        items = _post_filter(items, arguments)[:FIND_LIMIT_MAX]
        return await _render_candidates(
            db,
            items,
            found_by=f"the address '{key}'",
            empty_hint=(
                "No work item is recognised by that address. Either this is somebody new "
                "- log them - or the record has it spelled differently, in which case "
                "search by `query`."
            ),
        )

    query = _text(arguments, "query", 200)
    stmt = select(WorkItem).where(WorkItem.owner_user_id == user.id)
    if query:
        like = f"%{query.lower()}%"
        stmt = stmt.where(
            or_(WorkItem.title.ilike(like), WorkItem.summary.ilike(like))
        )
    item_type = _text(arguments, "type", TYPE_MAX_CHARS).lower()
    if item_type:
        stmt = stmt.where(WorkItem.type == item_type)
    status, status_error = _status(arguments)
    if status_error:
        return WorkItemToolResult(
            False, "bad_status", status_error, "I searched for a status that does not exist."
        )
    if status:
        stmt = stmt.where(WorkItem.status == status)
    elif not include_closed:
        stmt = stmt.where(WorkItem.status.notin_(tuple(work_items_service.TERMINAL_STATUSES)))
    # Same ordering as `resolve_by_key`, so "most recent activity first" means
    # one thing whichever way the model got here.
    stmt = stmt.order_by(
        WorkItem.closed_at.is_(None).desc(),
        WorkItem.last_event_at.desc().nullslast(),
        WorkItem.created_at.desc(),
    ).limit(FIND_LIMIT_DEFAULT if not query else FIND_LIMIT_MAX)
    result = await db.execute(stmt)
    items = list(result.scalars().unique().all())
    return await _render_candidates(
        db,
        items,
        found_by=f"'{query}'" if query else "that filter",
        empty_hint=(
            "Nothing matches. If this is new work, log it with create_work_item."
        ),
    )


def _post_filter(items: list[WorkItem], arguments: dict[str, Any]) -> list[WorkItem]:
    """`type`/`status` narrowing applied to an address lookup, in Python.

    In Python and not in SQL because `resolve_by_key` owns that statement and
    its ordering, and threading two optional filters through the inbound lane's
    resolver to save one pass over at most ten rows would be paying in the
    wrong currency.
    """
    item_type = str(arguments.get("type") or "").strip().lower()
    status = str(arguments.get("status") or "").strip().lower()
    if item_type:
        items = [i for i in items if i.type == item_type]
    if status in WORK_ITEM_STATUSES:
        items = [i for i in items if i.status == status]
    return items


async def _render_candidates(
    db: AsyncSession, items: list[WorkItem], *, found_by: str, empty_hint: str
) -> WorkItemToolResult:
    """Candidates as the model must read them: plural, ordered, never collapsed."""
    if not items:
        return WorkItemToolResult(
            True,
            "found_none",
            f"No work item matches {found_by}. {empty_hint}",
            "I checked our records and found nothing matching.",
            (),
        )
    slugs = await _slug_map(db, items)
    head = f"{len(items)} work item(s) match {found_by}, most recent activity first."
    if len(items) > 1:
        # The sentence `resolve_by_key`'s docstring exists to protect. The
        # ordering is defined, and it is still only an ordering: two rows can
        # honestly carry the same address, and a model that takes the first
        # because it was printed first has invented a certainty the schema
        # refuses to have.
        head += (
            " More than one record can carry the same address, so choose by what you "
            "read here rather than by which came first - and say which one you chose."
        )
    body = "\n".join(_line(item, slugs) for item in items)
    return WorkItemToolResult(
        True,
        "found",
        f"{head}\n{body}",
        f"I found {len(items)} matching record(s).",
        tuple(str(item.id) for item in items),
    )


async def _update(
    db: AsyncSession,
    arguments: dict[str, Any],
    *,
    user: User,
    bot: Bot,
    run_id: uuid.UUID | None,
) -> WorkItemToolResult:
    """Edit an item in place. Ownership is not editable here, exactly as in PATCH."""
    work_item_id = _work_item_id(arguments)
    if work_item_id is None:
        return WorkItemToolResult(
            False,
            "no_id",
            "Nothing was changed: update_work_item needs the `id` of the item. Get one "
            "from find_work_items.",
            "I tried to update a record without saying which one.",
        )
    item = await _owned(db, work_item_id, user)
    if item is None:
        return WorkItemToolResult(
            False,
            "not_found",
            f"There is no work item {work_item_id} that you can see, so nothing was "
            "changed. Find it first.",
            "I tried to update a work item that does not exist here.",
        )
    if "owner_bot_id" in arguments or "to_slug" in arguments:
        # The same refusal `UpdateWorkItemIn` makes, made in the same spirit:
        # loudly, because a 200 that dropped the field reads as a handover that
        # happened.
        return WorkItemToolResult(
            False,
            "owner_not_editable",
            "Nothing was changed: who owns a work item does not move through "
            "update_work_item, because that would leave no record of the handover. Use "
            "transfer_work_item.",
            "I tried to change who owns a record without recording the handover.",
        )

    status, status_error = _status(arguments)
    if status_error:
        return WorkItemToolResult(
            False, "bad_status", status_error, "I tried to set a status that does not exist."
        )
    detail, detail_error = _detail(arguments.get("detail"))
    if detail_error:
        return WorkItemToolResult(
            False, "bad_detail", detail_error, "I tried to store a detail block I could not store."
        )

    changed: list[str] = []
    if status:
        work_items_service.apply_status(item, status)
        changed.append("status")
    title = _text(arguments, "title", TITLE_MAX_CHARS)
    if title:
        item.title = title
        changed.append("title")
    summary = _text(arguments, "summary", SUMMARY_MAX_CHARS)
    if summary:
        item.summary = summary
        changed.append("summary")
    resolution = _text(arguments, "resolution", RESOLUTION_MAX_CHARS)
    if resolution:
        item.resolution = resolution
        changed.append("resolution")
    if detail:
        # Merged, and re-assigned rather than mutated: `detail` is a JSONB
        # column and SQLAlchemy will not see an in-place `dict.update` on it,
        # so the write would be silently dropped.
        merged = dict(item.detail or {})
        merged.update(detail)
        encoded = len(json.dumps(merged, default=str))
        if encoded > DETAIL_MAX_CHARS:
            return WorkItemToolResult(
                False,
                "detail_too_large",
                f"Nothing was changed: merging that would make `detail` {encoded} "
                f"characters and the limit is {DETAIL_MAX_CHARS}. Send fewer fields, or "
                "put the long text in `summary`.",
                "I tried to add more detail to a record than it can hold.",
            )
        item.detail = merged
        changed.append("detail")
    added = await _add_keys(db, item, _parse_keys(arguments.get("keys")))
    if added:
        changed.append("keys")

    if not changed:
        return WorkItemToolResult(
            False,
            "no_change",
            "Nothing was changed: that call named no field to change. Send `status`, "
            "`summary`, `detail`, `resolution` or `keys`.",
            "I tried to update a record without saying what to change.",
        )

    db.add(item)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            bot_id=item.owner_bot_id,
            event_type="work_item_updated",
            # Field names and the resulting state, never the values: an audit
            # row is read by more people than the record it describes, and this
            # one's values were written by a model.
            detail={
                "work_item_id": str(item.id),
                "fields": sorted(changed),
                "status": item.status,
                "via": "agent_tool",
                "run_id": str(run_id) if run_id else None,
                "by_bot_id": str(bot.id),
            },
        )
    )
    await db.commit()
    await db.refresh(item)

    note = ""
    if item.owner_bot_id != bot.id:
        # Allowed - the scope is the human's, exactly as it is over HTTP - but
        # said out loud, because a bot editing another bot's row and not knowing
        # it is how two bots overwrite each other's summaries all afternoon.
        slugs = await _slug_map(db, [item])
        note = f" Note: this item is held by {_holder(item, slugs)}, not by you."
    return WorkItemToolResult(
        True,
        "updated",
        f"Updated {item.id}: {', '.join(sorted(changed))}. It is now {item.status}"
        + (f"/{item.resolution}" if item.resolution else "")
        + f".{note}",
        f"I updated **{item.title}** — now {item.status}.",
        (str(item.id),),
    )


async def _transfer(
    db: AsyncSession,
    arguments: dict[str, Any],
    *,
    user: User,
    bot: Bot,
    targets: list[Bot],
    run_id: uuid.UUID | None,
) -> WorkItemToolResult:
    """Move ownership to another bot on this thread, through the one ledger path.

    Thread membership is the boundary, which is narrower than `POST
    /work-items/{id}/transfer` (any bot the caller can see) and narrower on
    purpose. A model can only name a bot it has been told about, and the only
    roster it is ever told is this thread's; a handover to a bot that is not
    here would also be a handover to an owner that will never be woken about it.
    Same boundary as `_delegate_targets`, for the same reason it has one.
    """
    work_item_id = _work_item_id(arguments)
    slug = _text(arguments, "to_slug", 120).lower()
    reason = _text(arguments, "reason", REASON_MAX_CHARS)
    available = ", ".join(sorted(b.slug for b in targets)) or "nobody"

    if work_item_id is None:
        return WorkItemToolResult(
            False,
            "no_id",
            "Nothing was handed over: transfer_work_item needs the `id` of the item. Get "
            "one from find_work_items.",
            "I tried to hand over a record without saying which one.",
        )
    if not slug:
        return WorkItemToolResult(
            False,
            "no_slug",
            f"Nothing was handed over: you did not say who to. On this thread: {available}.",
            "I tried to hand a record over without saying who to.",
        )
    if not reason:
        return WorkItemToolResult(
            False,
            "no_reason",
            "Nothing was handed over: a handover with no reason is a timestamp nobody can "
            "read back. Send it again saying why they are getting it.",
            "I tried to hand a record over without saying why.",
        )
    if slug == bot.slug:
        return WorkItemToolResult(
            False,
            "self_transfer",
            "You already hold it, or you are asking to hand it to yourself. Nothing was "
            "changed.",
            "I tried to hand a record to myself.",
        )

    target = next((b for b in targets if b.slug == slug), None)
    if target is None:
        # The negative control this lane owes: a bot that is not on this
        # thread — including one belonging to somebody else entirely — is not
        # distinguishable here from one that does not exist, and neither of
        # them gets the row.
        return WorkItemToolResult(
            False,
            "unknown_target",
            f"There is no bot called '{slug}' on this thread, so nothing was handed over. "
            f"On this thread: {available}. A bot cannot add another bot to a person's "
            "thread.",
            f"I wanted to hand this to '{slug}', who is not on this thread. Here with me: "
            f"{available}.",
        )

    item = await _owned(db, work_item_id, user)
    if item is None:
        return WorkItemToolResult(
            False,
            "not_found",
            f"There is no work item {work_item_id} that you can see, so nothing was handed "
            "over. Find it first.",
            "I tried to hand over a work item that does not exist here.",
        )
    if item.status in work_items_service.TERMINAL_STATUSES:
        return WorkItemToolResult(
            False,
            "closed",
            f"Work item {item.id} is closed, so it was not handed over. Reopen it with "
            "update_work_item first if there is really more to do.",
            "I tried to hand over a record that is already closed.",
        )

    row = await work_items_service.transfer_work_item(
        db,
        item,
        to_bot_id=target.id,
        reason=reason,
        actor_user_id=user.id,
        actor_bot_id=bot.id,
        source=SOURCE_AGENT,
        detail={"run_id": str(run_id) if run_id else None, "from_slug": bot.slug},
    )
    if row is None:
        # The idempotent case the service refuses to write twice. `ok` is True
        # because the world is in the state the model asked for; a second ledger
        # row would assert a handover that never happened.
        return WorkItemToolResult(
            True,
            "already_theirs",
            f"{target.slug} already holds {item.id}. Nothing was written and no second "
            "handover was recorded.",
            f"**{item.title}** was already with {target.slug}.",
            (str(item.id),),
        )
    await db.commit()
    await db.refresh(item)
    return WorkItemToolResult(
        True,
        "transferred",
        f"{target.slug} now owns {item.id} (\"{item.title}\") and the handover is on the "
        f"record with your reason. They have not been told and nothing has started - call "
        f"delegate_to_bot if it needs doing now.",
        f"I handed **{item.title}** to {target.slug}: {reason}",
        (str(item.id),),
    )
