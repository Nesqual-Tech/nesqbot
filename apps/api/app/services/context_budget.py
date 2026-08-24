"""What one agent-loop request is allowed to carry, and what it re-sends.

The measured problem this module exists for, off the live `cost_ledger` for the
24 hours to 2026-08-23:

    tier    calls   input tokens   output   cost
    reason    128      1,793,358    5,597   $9.13
    mini       12         42,041      850   $0.04

96% of the spend, and it is ~14,000 **input** tokens a call. Almost none of it
is new: an agent loop re-sends its whole conversation on every step, so the
same bytes are paid for once per remaining step. A previous lane fixed the
image half of that (`orchestrator.prune_screenshots`) and the text half was
still quadratic.

Measured off the requests of a scripted 35-step run — see
`tests/services/test_agent_context_budget.py`, which is where the numbers below
are asserted rather than claimed — the text half of an 11,114-token average
request broke down as:

    tool schemas   6,279  re-sent verbatim on every one of 36 calls
    system prompt  2,199  constant
    conversation   2,206 growing to 4,402
    screenshots    ~1,466 (already bounded by `prune_screenshots`)

So the largest single line is not the conversation at all — it is 38 function
schemas, 56% of every request, describing tools that mostly cannot be called.
That is what `select_tools` addresses. `compact_conversation` addresses the
growth, which in a *DOM-driven* run is much steeper than the scripted figure
above suggests: `browser.RESULT_MAX_CHARS` is 12,000, so one accessibility
snapshot is ~3,000 tokens that then ride along on every subsequent request
even though every element reference in it was voided by the next navigation.

Both are applied immediately before the model call, next to `prune_screenshots`
and for the same reason spelled out in its docstring: a conversation cannot
escape into a request by way of a code path that forgot to shrink it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 1. Only advertise tools that could actually be called
# ---------------------------------------------------------------------------
#
# 38 function schemas, 6,279 tokens, on every request. Over the 128 reason-tier
# calls in the measured day that is ~800k tokens and ~$4.00 spent re-describing
# a vocabulary that had not changed since the previous request.
#
# The saving is not "describe them more briefly" — that lane has already been
# run once, from ~4,600 tokens down to ~3,060 on the DOM half — it is that most
# of them are not usable in the state the loop is actually in:
#
# * With no desktop running there is exactly one useful action, and it is
#   `start_desktop`. The other 37 tools will fail with the same error.
# * `browser_*` against a desktop whose sidecar has no CDP is not a failure
#   mode, it is an absent capability: `browser_not_supported` means the
#   container predates the DOM release and every one of those 19 tools will 404
#   for as long as it lives. The loop already detects this to degrade to
#   pixels; it may as well stop paying to advertise them.
# * Once the model is holding live element references, the pixel primitives
#   that the DOM lane strictly supersedes are dead weight. Not all of them —
#   `screenshot` still sees a canvas or a CAPTCHA, and `click`/`type` are the
#   documented fallback when the browser lane 503s mid-run — but a `drag`
#   between two guessed coordinates on a page the model can address by `ref` is
#   a worse action than the one it replaces.
#
# The gate is recomputed before every call from the state the loop already
# tracks, so it is never sticky: the moment the browser lane degrades, the full
# pixel surface is advertised again on the very next request.


@dataclass(frozen=True)
class ToolContext:
    """What the loop knows about its own capabilities, right now.

    Independent booleans rather than an enum because they genuinely are: a
    running desktop may or may not have a browser lane, a browser lane may or
    may not currently hold live references, and whether there is another bot to
    hand work to has nothing to do with any of it.
    """

    #: A Bot Desktop is up. False before `start_desktop` and after `stop_desktop`.
    desktop_running: bool = True
    #: The sidecar's `/browser` lane answers. False once it has returned
    #: `browser_unavailable` or `browser_not_supported` — see
    #: `browser.BROWSER_ABSENT`.
    browser_available: bool = True
    #: The model is holding element references from a snapshot that has not been
    #: invalidated. Implies `browser_available`.
    dom_live: bool = False
    #: There is at least one *other* bot on this thread and the chain still has
    #: room for a hop. Default False: a single-bot thread is the common case and
    #: must not pay for a schema describing something it cannot do.
    delegates_available: bool = False
    #: This run can reach work items at all: there is a human it is answerable
    #: to and a conversation to attach a record to. Advertises `create_work_item`.
    #:
    #: Default False like `delegates_available`, so a caller that has not
    #: thought about it pays nothing — and so the tests that pin an exact
    #: advertised set keep meaning what they meant.
    work_items_available: bool = False
    #: The human this run answers to has at least one work item already.
    #: Advertises `find_work_items`.
    #:
    #: The `delegates_available` argument, restated: a search over nothing can
    #: only answer "nothing matches", and 194 tokens a request is a lot to pay
    #: for that sentence. The flag flips the moment the first record exists —
    #: including one this run just created, because `work_item_held` is enough
    #: on its own — so the tool appears exactly when it can do something.
    #:
    #: The tool stays *dispatchable* throughout, like every other gated tool
    #: here: a model that asks for it anyway gets the real empty answer rather
    #: than "there is no tool called that".
    work_items_exist: bool = False
    #: The model is holding at least one work-item id, from a `create` or a
    #: `find` earlier in this run. Advertises `update_work_item`.
    #:
    #: The same gate `dom_live` is, and true in the same literal way rather than
    #: as a heuristic: `update_work_item` takes a required `id`, so before one
    #: exists there is no valid call to make and the schema can only be read and
    #: re-read. The moment a create or a find returns one, the next request
    #: advertises it — so the cost of being wrong is nothing, and the saving is
    #: two schemas on every request of every run that never touches a record.
    work_item_held: bool = False
    #: There is another bot on this thread to hand a record to. Separate from
    #: `delegates_available` because a transfer is not a delegation and does not
    #: care whether the delegation chain has hops left — a bot out of hops can
    #: still hand a lead over, it just cannot wake anyone.
    handover_available: bool = False


#: Advertised whatever the state is. `task_complete` is the only clean exit, so
#: withholding it would strand a run; `request_human_takeover` is how a run
#: survives an MFA prompt.
ALWAYS_ADVERTISED: frozenset[str] = frozenset(
    {"task_complete", "request_human_takeover", "start_desktop"}
)

#: Advertised on its own terms: `delegate_to_bot` is the one tool whose
#: availability has nothing to do with whether a machine is up. It is gated on
#: `ToolContext.delegates_available` instead — see `_is_usable`.
DELEGATION_TOOL = "delegate_to_bot"

#: The work-item tools, gated the same way and for the same reason: logging a
#: prospect, looking one up and handing it to Sales touch no machine at all, and
#: the desktop is cold on most opening turns — which are exactly the turns where
#: a bot decides there is something worth recording.
#:
#: Named as literals rather than imported from `services.agent_work_items`, in
#: the same style as `DOM_ENTRY_SET` above, so this module stays a pure function
#: of strings with no import into the service layer. A test asserts the two
#: tables agree, which is what stops that costing anything.
#:
#: The split is the `DOM_ENTRY_SET` split, restated across four steps rather
#: than two, because each of the four has a different thing that has to be true
#: before it can do anything at all:
#:
#:     create    a human to own the record             — always, on this loop
#:     find      a record in existence to be found     — `work_items_exist`
#:     update    an id in the model's hands            — `work_item_held`
#:     transfer  ...and another bot to hand it to      — `handover_available`
#:
#: Measured on the shipped schemas: 194 tokens a request for `find` on a tenant
#: with nothing logged, 285 for `update` before an id exists, 183 for
#: `transfer` on a single-bot thread. The full four are 951; an opening turn on
#: a cold tenant pays 289 of that.
WORK_ITEM_CREATE_TOOL = "create_work_item"
WORK_ITEM_FIND_TOOL = "find_work_items"
WORK_ITEM_UPDATE_TOOL = "update_work_item"
WORK_ITEM_TRANSFER_TOOL = "transfer_work_item"
WORK_ITEM_TOOLS: frozenset[str] = frozenset(
    {
        WORK_ITEM_CREATE_TOOL,
        WORK_ITEM_FIND_TOOL,
        WORK_ITEM_UPDATE_TOOL,
        WORK_ITEM_TRANSFER_TOOL,
    }
)

#: Pixel primitives kept while the model is working through the DOM.
#:
#: The test for membership is "can the browser lane do this at all", not "is
#: this convenient": `screenshot` is the only way to see a canvas, an embedded
#: PDF or a CAPTCHA; `windows`/`focus_window` are the only way to reach
#: anything outside the browser; and `click`/`double_click`/`type`/`key`/
#: `key_combo`/`scroll` are named in the prelude the loop prints when a browser
#: call 503s, so they must still be on the tool list at the moment that
#: sentence is read.
PIXEL_ESSENTIALS: frozenset[str] = frozenset(
    {
        "screenshot",
        "windows",
        "focus_window",
        "click",
        "double_click",
        "type",
        "key",
        "key_combo",
        "scroll",
    }
)

#: The DOM tools offered before the model has actually opened a page.
#:
#: The nineteen `browser_*` schemas are 3,062 tokens on every request, and ten
#: of them cannot do anything until there is a page in front of the model:
#: hovering, waiting on a selector, answering a JavaScript dialog, going
#: `back`, and the four tab operations all describe things that only exist once
#: a tab is on a site. `browser_snapshot` is what makes them reachable, and the
#: gate flips on the request straight after the first successful one — so the
#: cost of being wrong is one extra step, and the saving is 1,350 tokens on
#: every request of every run that never leaves the pixel surface, which is
#: what the reported 35-step session was.
#:
#: `browser_navigate` is in here because it is the entrance: withhold it and
#: the DOM lane is unreachable, which would be the opposite of the point.
DOM_ENTRY_SET: frozenset[str] = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_scroll",
        "browser_key",
        "browser_text",
        "browser_extract",
    }
)

#: The compact form of the escalate-only `risk` lever, applied to the pixel
#: primitives. It is the one the DOM tools already use, and the argument for it
#: is the same one, restated: the long version is ~34 tokens and it was on all
#: fifteen pixel tools, ~500 tokens a request, ~64k over the measured day, to
#: say at length what one sentence says.
#:
#: It is also removed from the tools that cannot mutate anything. Offering a
#: `risk` field on `screenshot` is an invitation to declare one.
_COMPACT_RISK: dict[str, Any] = {
    "type": "string",
    "enum": ["mutate", "send", "spend", "delete"],
    "description": (
        "Declare when this sends, buys or deletes. Holds it for a human; escalate-only."
    ),
}

#: Tools that read without changing anything, so a declared risk is meaningless.
_OBSERVE_ONLY: frozenset[str] = frozenset({"screenshot", "windows"})


def select_tools(
    tools: list[dict[str, Any]],
    context: ToolContext,
    *,
    browser_names: frozenset[str] | None = None,
    desktop_names: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """The subset of `tools` that could actually run, with the descriptions trimmed.

    Pure: `tools` is not mutated and the returned schemas are fresh dicts, so a
    caller may keep the full list as its dispatch vocabulary. That separation
    is the point — `orchestrator.agent_tool_names()` stays the complete set, so
    a model that names a tool it was not offered still gets an honest "there is
    no tool called that" rather than a silent guess.

    `browser_names` and `desktop_names` are injected rather than imported to
    keep this module free of a cycle back into the orchestrator; the caller
    passes the same two tables the loop dispatches on.
    """
    browser = browser_names or frozenset()
    desktop = desktop_names or frozenset()
    keep: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("function", {}).get("name", "")
        if not _is_usable(name, context, browser, desktop):
            continue
        keep.append(_trimmed(tool, name, desktop))
    return keep


def _is_usable(
    name: str, context: ToolContext, browser: frozenset[str], desktop: frozenset[str]
) -> bool:
    if name in ALWAYS_ADVERTISED:
        # `start_desktop` while one is already running is a no-op the model
        # should not be tempted into, but withholding it would leave a loop
        # that lost its desktop mid-run with no way to say so.
        return True
    if name == DELEGATION_TOOL:
        # Deliberately decided *before* the desktop gate below, not after. A
        # lead-gen bot handing a warm lead to sales does not touch a machine at
        # all, and the desktop is cold on most opening turns — so routing this
        # through `desktop_running` would withhold the tool from precisely the
        # runs that exist to use it.
        return context.delegates_available
    if name in WORK_ITEM_TOOLS:
        # Decided before the desktop gate for the delegation reason above —
        # logging a prospect touches no machine, and the desktop is cold on the
        # opening turn, which is where a bot decides there is something worth
        # recording — and then in four steps of its own.
        if not context.work_items_available:
            return False
        if name == WORK_ITEM_CREATE_TOOL:
            return True
        if name == WORK_ITEM_FIND_TOOL:
            # A record this run created counts: it exists, and it is exactly the
            # one the model is most likely to look for again.
            return context.work_items_exist or context.work_item_held
        if not context.work_item_held:
            return False
        return name != WORK_ITEM_TRANSFER_TOOL or context.handover_available
    if not context.desktop_running:
        return False
    if name in browser:
        if not context.browser_available:
            return False
        return context.dom_live or name in DOM_ENTRY_SET
    if name in desktop:
        if context.dom_live and context.browser_available:
            return name in PIXEL_ESSENTIALS
        return True
    return True


def _trimmed(tool: dict[str, Any], name: str, desktop: frozenset[str]) -> dict[str, Any]:
    """One schema, with the risk lever normalised. Never mutates the input."""
    function = dict(tool.get("function") or {})
    parameters = dict(function.get("parameters") or {})
    properties = dict(parameters.get("properties") or {})
    if "risk" in properties and name in desktop:
        if name in _OBSERVE_ONLY:
            properties.pop("risk")
        else:
            properties["risk"] = dict(_COMPACT_RISK)
    parameters["properties"] = properties
    function["parameters"] = parameters
    return {**tool, "function": function}


# ---------------------------------------------------------------------------
# 2. Stop re-sending what the run has already moved past
# ---------------------------------------------------------------------------
#
# The rule the design has to satisfy, and the reason this is not a `[:N]`:
#
#   A 12,000-byte accessibility snapshot from step 3 is worthless at step 30,
#   but the *fact* of what was found may still matter.
#
# So nothing is dropped, and nothing is cut mid-structure. A stale result keeps
# its head — which for every shape the browser lane emits is the line that
# names what happened, the page it happened on and how many rows came back —
# and loses its body, with a sentence saying what went and why. The model can
# still see that it looked at that page, and it is told plainly to look again
# rather than left to reason off half a table.
#
# Three things are deliberately never touched, because touching them breaks the
# request rather than shrinking it:
#
# * **The system message and the goal.** The first is the whole protocol and
#   the second is what the run is for.
# * **`tool` messages as messages.** Chat completions rejects a request whose
#   assistant turn announced a `tool_call_id` with no matching reply, so a
#   stale result is shrunk in place and never removed.
# * **The newest results.** `KEEP_RESULTS_VERBATIM` of them survive whole,
#   which is what makes "did my last action do what I expected" answerable —
#   the same argument `AGENT_SCREENSHOT_HISTORY` makes for frames.

#: Tool results left completely alone, newest first. Two is the result of the
#: step just taken plus the one before it.
KEEP_RESULTS_VERBATIM = 2

#: A stale tool result longer than this is digested. Below it, digesting would
#: cost more in explanatory text than it saves.
STALE_RESULT_MAX_CHARS = 400

#: Progressively harder passes, used only when a conversation is still over
#: `budget_chars` after the ordinary one. Reached by a run that took many large
#: snapshots; an ordinary run never gets past the first entry.
_ESCALATION: tuple[int, ...] = (STALE_RESULT_MAX_CHARS, 200, 100)

#: Marker that ends a digested result. Also how the pass recognises its own
#: previous work, which is what makes calling this before every request cost
#: one pass over the list instead of compounding.
DIGEST_MARKER = "[…superseded:"

#: The sentence appended to a shrunk result. Says what is gone, how much of it,
#: and what to do about it — a model told only "truncated" retries the same
#: tool for the same reason.
#: "at least", not an exact figure, because a result that has been through two
#: escalating passes has already lost the tail the second pass would have
#: measured against. Understating what is missing would be the one dishonest
#: thing this module could say to a model.
DIGEST_TEMPLATE = (
    "\n{marker} at least {lines} more line(s) and {chars} more character(s) from this "
    "result are no longer being re-sent. Later steps have moved past it. Re-run the "
    "tool if you need this again.]"
)

#: Head line of a snapshot result, from `browser.success_text`. A superseded
#: snapshot is the worst offender in the conversation and also the easiest
#: call: every `ref` in the body was voided by the next navigation, so the body
#: has no action value left at all, only the head's record that the page was
#: seen.
_SNAPSHOT_PREFIX = "browser_snapshot of"
#: Deliberately curt. A run that snapshots seventeen times pays for this
#: sentence seventeen times, and the head line above it has already said what
#: page it was and how many rows came back.
_SNAPSHOT_DIGEST = "\n{marker} element list dropped, its refs are void — snapshot again to act.]"

#: Sentences an observation message repeats verbatim on every single step, from
#: `orchestrator._observation_message` and `_dom_observation_message`. They are
#: standing instructions, not facts about what happened, and one copy of a
#: standing instruction is as instructive as thirty-four.
#:
#: Measured on a 35-step DOM run: the "no screenshot was taken" paragraph is
#: ~70 tokens and the step budget another ~25, so this is ~3,200 tokens of the
#: last request — more than the whole conversation is otherwise worth. The
#: *newest* copy of each survives, and the step budget in particular has to: a
#: stale "3 step(s) left" is not merely wasted, it is wrong.
_BOILERPLATE_MARKERS = (
    "step(s) left in this run.",
    "desktop step(s) left this turn.",
    "No screenshot was taken for this step",
)


def compact_conversation(
    convo: list[dict[str, Any]],
    *,
    keep: int = KEEP_RESULTS_VERBATIM,
    budget_chars: int | None = None,
) -> int:
    """Shrink the stale half of a live conversation. Returns characters removed.

    Mutates `convo` in place, like `prune_screenshots`, and is called from the
    same place for the same reason — immediately before the model call, not at
    the point content is added, so a future edit cannot forget it.

    Idempotent: a conversation already compacted is left alone.
    """
    removed = 0
    for limit in _ESCALATION:
        removed += _one_pass(convo, keep=keep, limit=limit)
        if budget_chars is None or _text_chars(convo) <= budget_chars:
            break
    return removed


def _one_pass(convo: list[dict[str, Any]], *, keep: int, limit: int) -> int:
    result_indexes = [i for i, m in enumerate(convo) if m.get("role") == "tool"]
    stale_results = set(result_indexes[: max(len(result_indexes) - keep, 0)])

    # Snapshots get a harder rule than everything else, and it is the sidecar's
    # own rule rather than a judgement call: a `ref` is only valid against the
    # snapshot that minted it, so the moment a newer snapshot exists the older
    # one's four thousand tokens of element list cannot be acted on at all.
    # Keeping two of those "for continuity" is keeping one that is void.
    snapshot_indexes = [i for i in result_indexes if _is_snapshot(convo[i])]
    stale_results |= set(snapshot_indexes[:-1])

    removed = 0
    for index in sorted(stale_results):
        before = convo[index]
        after = _digest_result(before, limit)
        if after is not before:
            removed += len(str(before.get("content") or "")) - len(
                str(after.get("content") or "")
            )
            convo[index] = after
    removed += _drop_stale_boilerplate(convo)
    removed += _fold_old_placeholders(convo)
    return removed


def _drop_stale_boilerplate(convo: list[dict[str, Any]]) -> int:
    """Leave one copy of each standing instruction — the newest — and drop the rest.

    The newest copy is tracked *per marker*, not per message: the pixel and DOM
    observation shapes carry different boilerplate, and a run that switched
    lanes half way through must keep the newest of each rather than the newest
    message that happened to have one.

    All markers are then removed in a single walk. Doing one marker at a time
    was measured to save nothing at all on a DOM run: those messages carry two
    boilerplate lines and no facts, so the first marker's pass emptied them
    down to the second's, and the never-empty guard then refused to remove
    that. A message that turns out to hold nothing but standing instructions
    the model has already been given is removed outright — the step it belongs
    to is still recorded by its `assistant` call and its `tool` reply, which
    are the two the API would reject the request for losing.
    """
    newest = {
        marker: max(
            (i for i, m in enumerate(convo) if i > 0 and m.get("role") == "user" and _carries(m, marker)),
            default=-1,
        )
        for marker in _BOILERPLATE_MARKERS
    }
    removed = 0
    emptied: list[int] = []
    for index, message in enumerate(convo):
        if index == 0 or message.get("role") != "user" or not _parts(message):
            continue
        drop = [m for m in _BOILERPLATE_MARKERS if newest[m] != index]
        after = _drop_lines(message, drop)
        if after is message:
            continue
        removed += _message_chars(message) - _message_chars(after)
        if _parts(after):
            convo[index] = after
        else:
            emptied.append(index)
    for index in reversed(emptied):
        del convo[index]
    return removed


def _is_snapshot(message: dict[str, Any]) -> bool:
    return str(message.get("content") or "").startswith(_SNAPSHOT_PREFIX)


#: What `prune_screenshots` leaves behind where a frame used to be, and the
#: shorter form the older ones are folded to.
#:
#: The full sentence is right the first time and wasteful the thirty-third:
#: ~27 tokens explaining that a picture is gone and that the model should work
#: from the newest one — an explanation it does not need repeating once per
#: step. The step number is the fact and it survives; the newest placeholder
#: keeps the full sentence, because that is the one sitting next to the frames
#: it is talking about.
#:
#: The folded form keeps the same opening words on purpose.
#: `test_agent_cost.py::test_pruning_removes_the_quadratic` reconstructs the
#: unpruned request by counting these markers, which is what makes its
#: before-and-after exact rather than modelled; a fold that renamed them would
#: quietly turn that measurement into a smaller number with no failure.
_PLACEHOLDER_PREFIX = "[Screenshot from desktop step "
_PLACEHOLDER_SHORT = "[Screenshot from desktop step {step} not re-sent.]"


def _fold_old_placeholders(convo: list[dict[str, Any]]) -> int:
    carriers = [
        (i, p)
        for i, m in enumerate(convo)
        for p in _parts(m)
        if isinstance(p, dict) and str(p.get("text", "")).startswith(_PLACEHOLDER_PREFIX)
    ]
    removed = 0
    for index, part in carriers[:-1]:
        text = str(part["text"])
        step = text[len(_PLACEHOLDER_PREFIX) :].split(" ", 1)[0].rstrip("]")
        short = _PLACEHOLDER_SHORT.format(step=step)
        if len(short) >= len(text):
            continue
        removed += len(text) - len(short)
        convo[index] = {
            **convo[index],
            "content": [
                {**p, "text": short} if p is part else p for p in _parts(convo[index])
            ],
        }
    return removed


def _digest_result(message: dict[str, Any], limit: int) -> dict[str, Any]:
    """Shrink one stale result to `limit` characters of head, or leave it alone.

    Re-entrant on its own output. An already-digested result is unwrapped back
    to its head before being measured again, so the escalating passes can take
    a second, harder bite out of the same message — and so the ordinary case,
    where the head already fits, still costs one comparison and returns the
    message unchanged. That is what makes calling this before every request
    idempotent rather than compounding.
    """
    text = message.get("content")
    if not isinstance(text, str):
        return message
    already, marker, _ = text.partition(DIGEST_MARKER)
    if marker:
        if already.rstrip("\n").startswith(_SNAPSHOT_PREFIX) or len(already) <= limit:
            return message
        text = already.rstrip("\n")
    elif text.startswith(_SNAPSHOT_PREFIX):
        head, _, body = text.partition("\n")
        if not body:
            return message
        return {
            **message,
            "content": head + _SNAPSHOT_DIGEST.format(marker=DIGEST_MARKER),
        }
    if len(text) <= limit:
        return message
    head, dropped_lines, dropped_chars = _head_within(text, limit)
    return {
        **message,
        "content": head
        + DIGEST_TEMPLATE.format(
            marker=DIGEST_MARKER, lines=dropped_lines, chars=dropped_chars
        ),
    }


def _head_within(text: str, limit: int) -> tuple[str, int, int]:
    """As many whole leading lines as fit in `limit`, and what that left behind.

    Whole lines, because the shapes that get here are line-oriented — an
    accessibility tree, a table of rows, a stack of `code: detail: remedy`
    lines — and half a line of one of those is a fact with its meaning removed.
    A first line that is on its own longer than the budget is cut at a word
    boundary instead, which is the only case where a structure is entered at
    all.
    """
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if kept else 0)
        if kept and used + cost > limit:
            break
        if not kept and len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            kept.append(line[: cut if cut > 0 else limit])
            used = len(kept[0])
            break
        kept.append(line)
        used += cost
    head = "\n".join(kept)
    return head, max(len(lines) - len(kept), 0), max(len(text) - len(head), 0)


def _carries(message: dict[str, Any], marker: str) -> bool:
    return any(
        isinstance(part, dict) and part.get("type") == "text" and marker in str(part.get("text", ""))
        for part in _parts(message)
    )


def _drop_lines(message: dict[str, Any], markers: list[str]) -> dict[str, Any]:
    """Remove stale standing-instruction *lines* from one observation message.

    By line rather than by part, because the two observation shapes disagree
    about how they pack their text. `_observation_message` puts the step budget
    in a content part of its own; `_dom_observation_message` joins the "no
    screenshot was taken" paragraph and the budget into one part with newlines.
    Dropping whole parts is right for the first and would throw away the action
    result on the second.

    An image part is never a candidate and is copied through untouched — the
    frames are `prune_screenshots`'s business and double-handling them here is
    how two pruners disagree.
    """
    if not markers:
        return message
    rebuilt: list[Any] = []
    changed = False
    for part in _parts(message):
        if not (isinstance(part, dict) and part.get("type") == "text"):
            rebuilt.append(part)
            continue
        text = str(part.get("text", ""))
        kept_lines = [
            line for line in text.split("\n") if not any(m in line for m in markers)
        ]
        if len(kept_lines) == len(text.split("\n")):
            rebuilt.append(part)
            continue
        changed = True
        if kept_lines:
            rebuilt.append({**part, "text": "\n".join(kept_lines)})
    if not changed:
        return message
    return {**message, "content": rebuilt}


def _parts(message: dict[str, Any]) -> list[Any]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _message_chars(message: dict[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    return sum(
        len(str(p.get("text", "")))
        for p in _parts(message)
        if isinstance(p, dict) and p.get("type") == "text"
    )


def _text_chars(convo: list[dict[str, Any]]) -> int:
    return sum(_message_chars(m) for m in convo)


# ---------------------------------------------------------------------------
# 3. What a request actually costs, counted off the request
# ---------------------------------------------------------------------------


def count_request_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> int:
    """Prompt tokens for one whole request: messages, tool calls and schemas.

    `model_router.count_text_tokens` counts only the *text* of the messages. It
    is the right function for pricing images against text, and the wrong one
    for asking what a request costs: it sees `content: None` on an assistant
    turn that is carrying three function calls, and it never sees the `tools`
    array at all — which on this loop is the single largest thing in the
    request. Both are counted here, at the same four-characters-a-token the
    rest of the module estimates with, so the two numbers are comparable.

    Images are deliberately excluded; `count_image_tokens` prices those, and
    adding them here would double-count in any caller that uses both.
    """
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        else:
            chars += sum(
                len(str(p.get("text", "")))
                for p in _parts(message)
                if isinstance(p, dict) and p.get("type") == "text"
            )
        calls = message.get("tool_calls")
        if calls:
            chars += len(json.dumps(calls))
        for field in ("role", "tool_call_id", "name"):
            value = message.get(field)
            if value:
                chars += len(str(value))
    if tools:
        chars += len(json.dumps(tools))
    return max(chars // 4, 1)
