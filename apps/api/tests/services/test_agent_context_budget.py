"""What an agent-loop request carries, and what it costs to carry it.

The session this file is the regression suite for is not a bug report, it is a
bill. From the live `cost_ledger`, the 24 hours to 2026-08-23:

    tier    calls   input tokens   output   cost
    reason    128      1,793,358    5,597   $9.13
    mini       12         42,041      850   $0.04

96% of the spend, and 99.7% of *that* is input: ~14,000 prompt tokens per call
for ~44 tokens of reply. An earlier lane had already bounded the image half
(`orchestrator.prune_screenshots`), so the money was going on text that the
loop re-sent on every step for the rest of the run.

Measured off the real requests of a scripted 35-step run — `long_run` below,
the same fixture shape `test_agent_cost.py` uses, and every number in this file
is counted off `router.seen` / `router.tools_seen` rather than modelled — the
average 11,114-token request was:

    6,279  tool schemas        38 function definitions, re-sent verbatim
    2,199  system prompt       constant
    2,206 → 4,402  conversation
    1,466  screenshots         already bounded

The largest line is the vocabulary, not the conversation. That is what
`context_budget.select_tools` is for, and it is why the headline test here is
`test_the_whole_run_costs_what_it_says_on_the_tin` rather than anything about
history: the fix that mattered most was to stop re-describing thirty-seven
tools that could not be called.

Two properties are asserted, and both have to hold:

* **A ceiling per request.** `test_no_request_may_exceed_the_ceiling` is the
  guard. It is per request rather than in aggregate so a failure names the one
  that broke it.
* **Nothing was lost to save it.** A compacted result keeps its head, keeps its
  `tool_call_id`, and says out loud what is no longer being sent. The tests
  under "2." are the ones that would fail if this became a `[:400]`.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.services import simulation
from app.services.context_budget import (
    DIGEST_MARKER,
    DOM_ENTRY_SET,
    KEEP_RESULTS_VERBATIM,
    PIXEL_ESSENTIALS,
    STALE_RESULT_MAX_CHARS,
    WORK_ITEM_TOOLS,
    ToolContext,
    compact_conversation,
    count_request_tokens,
    select_tools,
)
from app.services.model_router import (
    TIER_PRICES,
    count_image_tokens,
    tool_result_message,
)
from app.services.orchestrator import (
    BROWSER_TOOL_NAMES,
    DESKTOP_ACTIONS,
    TOOL_TASK_COMPLETE,
    agent_tools,
    agent_tools_for,
)
from tests.services.conftest import acts, call, turn
from tests.services.screens import patch_real_sized_screens

#: USD per input token at the tier the agent loop runs on.
REASON_INPUT_USD = Decimal(str(TIER_PRICES["reason"][0])) / Decimal(1_000_000)

#: Steps the scripted runs take. The number from the reported session.
SCRIPTED_STEPS = 35

ALL_TOOLS = agent_tools()
BROWSER = BROWSER_TOOL_NAMES
DESKTOP = frozenset(DESKTOP_ACTIONS)


def _names(tools) -> set[str]:
    return {t["function"]["name"] for t in tools}


def _tool_tokens(tools) -> int:
    return len(json.dumps(tools)) // 4 if tools else 0


# ---------------------------------------------------------------------------
# 1. Only advertise what could run
# ---------------------------------------------------------------------------


def test_the_full_vocabulary_is_what_it_was_measured_at():
    """Guards the arithmetic every dollar figure below is derived from.

    If this moves, the savings quoted in `context_budget`'s module docstring
    are quoting a request that no longer exists.

    `delegate_to_bot` is held out of that arithmetic rather than folded into it,
    because it is not part of the request those figures describe: it is gated on
    `ToolContext.delegates_available`, which is False on every single-bot thread
    — the shape the 38-tool session was measured on — so the measured request is
    still, exactly, these 38 schemas and these 6,279 tokens. Its own cost is
    asserted below where it is actually paid.

    The four work-item tools are held out for the same reason and on the same
    terms. Every one of them is gated — three of them on state the measured
    session never reached — and none of them existed when the figures below were
    taken, so folding them in would silently restate a measurement of a request
    that was never sent. What they cost, and where, is asserted in
    `test_agent_work_item_tools.py`.
    """
    generated = {"delegate_to_bot", *WORK_ITEM_TOOLS}
    measured = [t for t in ALL_TOOLS if t["function"]["name"] not in generated]
    assert len(measured) == 38
    # 6,419: the `risk` description on the declarable DOM tools grew by ~140 tokens
    # to state when NOT to declare, after a model declared `send` on a click that
    # only opened a profile and parked the whole task. The pin moves with the
    # measurement, not the other way round.
    assert _tool_tokens(measured) == pytest.approx(6419, abs=60)
    assert len(ALL_TOOLS) == 43


def test_delegation_is_free_until_there_is_somebody_to_delegate_to():
    """The whole tool costs nothing on the threads that cannot use it.

    A single-bot thread is the common case and the one the 14,010 -> 9,000
    per-call saving was measured on. Adding a tool that is always advertised
    would have given ~233 tokens of that back on every request of every run,
    for a capability those runs do not have.
    """
    without = agent_tools_for(ToolContext(desktop_running=True))
    with_ = agent_tools_for(ToolContext(desktop_running=True, delegates_available=True))
    assert "delegate_to_bot" not in _names(without)
    assert "delegate_to_bot" in _names(with_)
    # What it costs where it is paid, and only there.
    assert _tool_tokens(with_) - _tool_tokens(without) == pytest.approx(233, abs=30)


def test_delegation_does_not_wait_for_a_desktop():
    """It is the one tool whose availability has nothing to do with a machine.

    A lead-gen bot handing a warm lead to sales touches no desktop at all, and
    the desktop is cold on most opening turns — so routing this through
    `desktop_running` would withhold the tool from precisely the runs that
    exist to use it.
    """
    cold = _names(
        agent_tools_for(ToolContext(desktop_running=False, delegates_available=True))
    )
    assert cold == {"start_desktop", "task_complete", "request_human_takeover", "delegate_to_bot"}


def test_a_cold_desktop_is_offered_the_one_tool_that_changes_that():
    """37 of 38 tools can only return "no desktop". Sending them costs $0.03 a call."""
    tools = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=False),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    assert _names(tools) == {"start_desktop", "task_complete", "request_human_takeover"}
    # The number that makes it worth doing: an opening turn goes from ~6,300
    # prompt tokens of schema to ~420.
    assert _tool_tokens(tools) < 600


def test_a_desktop_with_no_browser_lane_is_not_sold_nineteen_browser_tools():
    """`browser_not_supported` is an absent capability, not a failed action.

    A container from before the DOM release has no `/browser` route at all, so
    every one of these will 404 for as long as it lives. One real session spent
    thirty-six steps rediscovering that.
    """
    tools = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, browser_available=False),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    assert not (_names(tools) & BROWSER)
    # And the pixel surface is offered *whole*, because it is now the only one.
    assert DESKTOP <= _names(tools)


def test_before_the_first_snapshot_only_the_way_into_the_page_is_offered():
    tools = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, browser_available=True, dom_live=False),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    assert _names(tools) & BROWSER == set(DOM_ENTRY_SET)
    # `browser_navigate` and `browser_snapshot` are both here, so the DOM lane
    # is reachable in one step from the opening state. Withholding either would
    # make the saving permanent by making the capability unreachable.
    assert {"browser_navigate", "browser_snapshot"} <= _names(tools)


def test_once_the_page_is_open_the_rest_of_the_dom_surface_appears():
    tools = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, browser_available=True, dom_live=True),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    assert BROWSER <= _names(tools)
    assert "browser_dialog" in _names(tools), "an alert() freezes the page; this answers it"


def test_working_in_the_dom_drops_the_pixel_tools_the_dom_supersedes():
    """A guessed drag on a page addressable by `ref` is a worse action, not a cheaper one."""
    tools = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, browser_available=True, dom_live=True),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    kept = _names(tools) & DESKTOP
    assert kept == set(PIXEL_ESSENTIALS)
    # The ones the degrade prelude names by hand must survive: when a browser
    # call comes back 503 the loop tells the model to "carry on with the pixel
    # tools (`click`, `type`, `key`, `scroll` at coordinates)", and that
    # sentence is read against this list.
    assert {"click", "type", "key", "scroll", "screenshot"} <= kept


def test_a_lost_browser_lane_brings_the_whole_pixel_surface_back():
    """The gate is state, not a decision. It has to be reversible in one request."""
    live = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, browser_available=True, dom_live=True),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    degraded = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, browser_available=False, dom_live=True),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    assert "drag" not in _names(live)
    assert "drag" in _names(degraded)


def test_the_exit_is_never_withheld():
    """`task_complete` is the only clean exit. A state that hides it strands the run."""
    for context in (
        ToolContext(desktop_running=False),
        ToolContext(desktop_running=True, browser_available=False),
        ToolContext(desktop_running=True, dom_live=True),
    ):
        names = _names(
            select_tools(ALL_TOOLS, context, browser_names=BROWSER, desktop_names=DESKTOP)
        )
        assert "task_complete" in names
        assert "request_human_takeover" in names


def test_the_pixel_risk_lever_says_the_same_thing_in_a_third_of_the_words():
    """~34 tokens x 15 tools x every request. The DOM half was cut for this reason already."""
    tools = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, browser_available=False),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    click = next(t for t in tools if t["function"]["name"] == "click")
    risk = click["function"]["parameters"]["properties"]["risk"]
    # Still the same lever with the same values — only the prose is shorter.
    assert set(risk["enum"]) == {"mutate", "send", "spend", "delete"}
    assert "escalate-only" in risk["description"]
    assert len(risk["description"]) < 130


def test_a_tool_that_cannot_change_anything_is_not_offered_a_risk_field():
    """Offering the field on `screenshot` is an invitation to fill it in."""
    tools = select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, browser_available=False),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    for name in ("screenshot", "windows"):
        tool = next(t for t in tools if t["function"]["name"] == name)
        assert "risk" not in tool["function"]["parameters"]["properties"]


def test_selecting_tools_never_mutates_the_vocabulary_it_was_given():
    """`agent_tools()` is the dispatch table. A trim that edited it in place would
    silently change what the loop *runs*, not just what it advertises."""
    before = json.dumps(ALL_TOOLS)
    select_tools(
        ALL_TOOLS,
        ToolContext(desktop_running=True, dom_live=True),
        browser_names=BROWSER,
        desktop_names=DESKTOP,
    )
    assert json.dumps(ALL_TOOLS) == before


def test_an_unadvertised_tool_is_still_dispatchable():
    """Advertising is a cost decision; dispatching is a capability one.

    A model that reaches for `drag` on a step where it was not offered gets the
    drag, not "there is no tool called that". Collapsing the two would turn a
    saving into a capability regression.
    """
    from app.services.orchestrator import agent_tool_names

    offered = _names(agent_tools_for(ToolContext(desktop_running=False)))
    assert "drag" not in offered
    assert "drag" in agent_tool_names()


# ---------------------------------------------------------------------------
# 2. Compacting without forgetting
# ---------------------------------------------------------------------------

#: A real-shaped accessibility snapshot result, at the size `browser.py`
#: actually clips to. This is the message that is worthless at step 30 and
#: expensive on every request between here and there.
BIG_SNAPSHOT = 'browser_snapshot of "Cart" — https://shop.test/cart (snapshot_id=s1). 3 rows.\n' + (
    "\n".join(f'e{n} button "Row {n} action"' for n in range(400))
)


def _convo_with_results(count: int, body: str) -> list[dict]:
    convo: list[dict] = [
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "empty my cart"},
    ]
    for n in range(count):
        convo.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{n}",
                        "type": "function",
                        "function": {"name": "browser_snapshot", "arguments": "{}"},
                    }
                ],
            }
        )
        convo.append(tool_result_message(f"c{n}", body))
        convo.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Desktop step {n}: ran and reported success."},
                    {"type": "text", "text": "3 step(s) left in this run. Call the next tool."},
                ],
            }
        )
    return convo


def test_a_superseded_snapshot_keeps_what_it_found_and_drops_what_is_void():
    convo = _convo_with_results(5, BIG_SNAPSHOT)
    compact_conversation(convo)

    stale = convo[3]["content"]
    # The fact of what was found survives: the page, its URL, the row count.
    assert stale.startswith('browser_snapshot of "Cart" — https://shop.test/cart')
    assert "3 rows." in stale
    # The body does not, and the model is told *why* rather than left to guess.
    assert 'e200 button "Row 200 action"' not in stale
    assert "refs are void" in stale
    assert "snapshot again to act" in stale
    assert len(stale) < 250


def test_the_newest_results_are_left_exactly_alone():
    """The same argument `AGENT_SCREENSHOT_HISTORY` makes for frames: the model has
    to be able to answer "did my last action do what I expected"."""
    convo = _convo_with_results(5, "browser_extract returned 3 rows:\n" + "row\n" * 400)
    compact_conversation(convo)

    fresh = [m["content"] for m in convo if m.get("role") == "tool"][-KEEP_RESULTS_VERBATIM:]
    assert len(fresh) == KEEP_RESULTS_VERBATIM
    for content in fresh:
        assert DIGEST_MARKER not in content


def test_only_one_snapshot_is_ever_kept_whole():
    """Snapshots get a harder rule than other results, and it is the sidecar's own.

    A `ref` is valid only against the snapshot that minted it, so the moment a
    newer snapshot exists the older one's element list cannot be acted on at
    all. Keeping two "for continuity" is keeping one that is void — and at
    ~3,000 tokens each, keeping a void one is the single most expensive thing
    a DOM conversation can do.
    """
    convo = _convo_with_results(5, BIG_SNAPSHOT)
    compact_conversation(convo)

    whole = [m["content"] for m in convo if m.get("role") == "tool" and DIGEST_MARKER not in m["content"]]
    assert whole == [BIG_SNAPSHOT]


def test_nothing_is_ever_cut_in_the_middle_of_a_line():
    """A half-rendered `e17 button "Sign` is a fact with its meaning removed."""
    rows = "\n".join(f"row {n}: some value that goes on for a while" for n in range(200))
    convo = _convo_with_results(4, rows)
    compact_conversation(convo)

    digested = convo[3]["content"]
    head, marker, _ = digested.partition(DIGEST_MARKER)
    assert marker, "this result should have been digested"
    for line in head.rstrip("\n").split("\n"):
        assert line == "" or line.startswith("row ")
        assert line == "" or line.endswith("while")


def test_a_digest_says_how_much_it_is_no_longer_sending():
    rows = "\n".join(f"row {n}" for n in range(500))
    convo = _convo_with_results(4, rows)
    compact_conversation(convo)

    digested = convo[3]["content"]
    assert "more line(s)" in digested
    assert "more character(s)" in digested
    assert "Re-run the tool if you need this again." in digested


def test_a_single_line_longer_than_the_budget_is_cut_at_a_word():
    """The one case where a structure is entered at all. It still ends on a word."""
    convo = _convo_with_results(4, "alpha beta gamma delta " * 200)
    compact_conversation(convo)

    head = convo[3]["content"].partition(DIGEST_MARKER)[0]
    assert not head.endswith(" ")
    assert head.split()[-1] in {"alpha", "beta", "gamma", "delta"}


def test_a_tool_result_is_shrunk_and_never_removed():
    """Chat completions rejects a `tool_call_id` that was announced and not answered."""
    convo = _convo_with_results(6, BIG_SNAPSHOT)
    announced = [
        c["id"] for m in convo if m.get("tool_calls") for c in m["tool_calls"]
    ]
    compact_conversation(convo)
    answered = [m["tool_call_id"] for m in convo if m.get("role") == "tool"]
    assert answered == announced


def test_the_system_message_and_the_goal_are_never_touched():
    convo = _convo_with_results(6, BIG_SNAPSHOT)
    compact_conversation(convo)
    assert convo[0] == {"role": "system", "content": "protocol"}
    assert convo[1] == {"role": "user", "content": "empty my cart"}


def test_only_the_newest_step_budget_is_re_sent():
    """A stale "3 step(s) left" is not merely wasted, it is wrong."""
    convo = _convo_with_results(6, "small")
    compact_conversation(convo)

    trailers = [
        part
        for m in convo
        for part in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(part, dict) and "step(s) left" in str(part.get("text", ""))
    ]
    assert len(trailers) == 1
    # …and the facts either side of it are still there, on every step.
    facts = [
        part
        for m in convo
        for part in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(part, dict) and "ran and reported success" in str(part.get("text", ""))
    ]
    assert len(facts) == 6


def test_old_screenshot_placeholders_keep_their_step_and_lose_their_explanation():
    """`prune_screenshots` leaves ~27 tokens explaining that a picture is gone.

    Right the first time, wasteful the thirty-third. The step number is the
    fact and it survives — and so does the opening of the sentence, because
    `test_agent_cost.py` reconstructs the pre-pruning request by counting these.
    """
    convo = [
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "go"},
    ]
    for step in range(1, 5):
        convo.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"[Screenshot from desktop step {step} omitted — a newer screen "
                            "is attached below. Work from the most recent one.]"
                        ),
                    }
                ],
            }
        )
    compact_conversation(convo)

    texts = [m["content"][0]["text"] for m in convo[2:]]
    for step, text in zip(range(1, 4), texts, strict=False):
        assert text == f"[Screenshot from desktop step {step} not re-sent.]"
    # The newest keeps the full sentence: it is the one sitting next to the
    # frames it is talking about.
    assert "Work from the most recent one." in texts[-1]
    # And every one of them is still countable as a placeholder.
    assert all(t.startswith("[Screenshot from desktop step") for t in texts)


def test_a_message_is_never_emptied_down_to_nothing():
    """A `user` turn with no content is not a thing the API accepts."""
    convo = [
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "go"},
        {"role": "user", "content": [{"type": "text", "text": "3 step(s) left in this run."}]},
        {"role": "user", "content": [{"type": "text", "text": "1 step(s) left in this run."}]},
    ]
    compact_conversation(convo)
    for message in convo:
        assert message["content"], message


def test_compaction_is_idempotent():
    """It runs before *every* request, so a second pass must cost one walk and change
    nothing — otherwise a long run would digest its own digests."""
    convo = _convo_with_results(6, BIG_SNAPSHOT)
    compact_conversation(convo)
    once = json.dumps(convo)
    assert compact_conversation(convo) == 0
    assert json.dumps(convo) == once


def test_a_conversation_with_nothing_stale_is_left_alone():
    """One step in: nothing is superseded yet, so nothing is touched."""
    convo = _convo_with_results(1, "ok")
    before = json.dumps(convo)
    assert compact_conversation(convo) == 0
    assert json.dumps(convo) == before


def test_a_short_result_is_not_worth_explaining_away():
    """Below the threshold the explanatory sentence costs more than the body."""
    convo = _convo_with_results(6, "x" * (STALE_RESULT_MAX_CHARS - 1))
    compact_conversation(convo)
    assert all(
        DIGEST_MARKER not in m["content"] for m in convo if m.get("role") == "tool"
    )


def test_a_hard_budget_squeezes_harder_rather_than_giving_up():
    """A second, harder bite out of results the ordinary pass already trimmed.

    Not snapshots: those are already at their minimum after one pass, because
    what is left of one is a head line that cannot be usefully shortened. This
    is the shape that can still give — a long extraction, kept in part.
    """
    rows = "browser_extract returned 800 rows:\n" + "\n".join(
        f"row {n}: a value long enough to matter" for n in range(800)
    )
    loose = _convo_with_results(20, rows)
    tight = _convo_with_results(20, rows)
    compact_conversation(loose)
    compact_conversation(tight, budget_chars=4_000)
    assert len(json.dumps(tight)) < len(json.dumps(loose))
    # It says "at least", so a twice-bitten result is still telling the truth
    # about how much of itself is missing.
    assert "at least" in next(m["content"] for m in tight if m.get("role") == "tool")


# ---------------------------------------------------------------------------
# 3. Counting a whole request
# ---------------------------------------------------------------------------


def test_the_request_counter_sees_the_two_things_the_text_counter_misses():
    """`count_text_tokens` reads `content` and nothing else, which on this loop
    misses the function calls *and* the tool array — the biggest item in the
    request."""
    from app.services.model_router import count_text_tokens

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "click", "arguments": '{"x": 10, "y": 20}'},
                }
            ],
        },
    ]
    assert count_text_tokens(messages) < 5
    assert count_request_tokens(messages) > 20
    assert count_request_tokens(messages, ALL_TOOLS) > 6_000


# ---------------------------------------------------------------------------
# 4. The measurement: a scripted 35-step run, before and after
# ---------------------------------------------------------------------------


def _unbudgeted(monkeypatch):
    """Put the loop back exactly as it was before this lane, for the `before` leg.

    The two levers are disabled at their call sites rather than reimplemented,
    so `before` is the requests the previous code really sent and not a model
    of them.
    """
    from app.services import orchestrator as orch

    monkeypatch.setattr(orch, "compact_conversation", lambda convo, **kw: 0)
    monkeypatch.setattr(orch, "agent_tools_for", lambda context: orch.agent_tools())


async def _pixel_run(agent_with, db, user_a, make_thread, bot, monkeypatch):
    """The reported session: 35 pixel steps against a 1280x800 mock desktop."""
    patch_real_sized_screens(monkeypatch)
    script = [acts("", call("click", x=100, y=100 + n)) for n in range(SCRIPTED_STEPS)]
    script.append(acts("", call(TOOL_TASK_COMPLETE, summary="Finished the search.")))
    orchestrator = agent_with(script)
    thread = await make_thread(user_a, [bot])
    await turn(orchestrator, db, user_a, thread, "open linkedin and search")
    return orchestrator.router


def _profile(router) -> dict:
    """Prompt tokens per request, counted off what was actually handed to `chat`."""
    text = [
        count_request_tokens(messages, tools)
        for messages, tools in zip(router.seen, router.tools_seen, strict=True)
    ]
    images = [count_image_tokens(messages) for messages in router.seen]
    total = [t + i for t, i in zip(text, images, strict=True)]
    return {
        "calls": len(total),
        "per_call": total,
        "tools": [_tool_tokens(t) for t in router.tools_seen],
        "input_tokens": sum(total),
        "usd": sum(total) * REASON_INPUT_USD,
    }


@pytest.fixture
async def before_after(agent_with, db, user_a, make_thread, make_bot, monkeypatch):
    """The same 35-step script run twice: without this lane's levers, and with them.

    A *fresh bot per leg*, which is not incidental. The mock desktop writes a
    `BotDesktop` row the first time a run touches it, so replaying the script
    against the same bot would open the second leg with a warm desktop and a
    different tool surface — and the comparison would be measuring the fixture.
    """

    async def _bot(name):
        return await make_bot(
            user_a,
            name=name,
            system_prompt="You are a test bot. You file expenses.",
            daily_budget_usd=500.0,
        )

    with monkeypatch.context() as off:
        _unbudgeted(off)
        before = _profile(
            await _pixel_run(agent_with, db, user_a, make_thread, await _bot("Cold"), off)
        )
    after = _profile(
        await _pixel_run(agent_with, db, user_a, make_thread, await _bot("Warm"), monkeypatch)
    )
    assert before["calls"] == after["calls"] == SCRIPTED_STEPS + 1, "the script did not run"
    return before, after


async def test_the_whole_run_costs_what_it_says_on_the_tin(before_after):
    """The headline, in tokens and dollars, off the real requests of both legs.

    Captured unless the suite is run with `-s`. `pytest -s -k says_on_the_tin`
    is how the number comes back without reading the assertions.
    """
    before, after = before_after
    print(
        f"\n  {after['calls']} model calls over {SCRIPTED_STEPS} desktop steps"
        f"\n  before: {before['input_tokens']:>9,} input tokens  "
        f"${before['usd']:.4f}  "
        f"({before['input_tokens'] // before['calls']:,}/call, "
        f"tools {before['tools'][0]:,}/call)"
        f"\n  after:  {after['input_tokens']:>9,} input tokens  "
        f"${after['usd']:.4f}  "
        f"({after['input_tokens'] // after['calls']:,}/call, "
        f"tools {min(after['tools']):,}-{max(after['tools']):,}/call)"
        f"\n  saved:  {before['input_tokens'] - after['input_tokens']:>9,} tokens  "
        f"${before['usd'] - after['usd']:.4f}  "
        f"({100 - 100 * after['input_tokens'] // before['input_tokens']}%)"
    )
    assert after["input_tokens"] < before["input_tokens"]
    assert after["usd"] < before["usd"]


#: The most any *one* agent-loop request may carry, in prompt tokens, counted
#: with `count_request_tokens` + `count_image_tokens` over exactly what went to
#: `router.chat`.
#:
#: Set from the measured worst request of the two runs in this file, plus
#: headroom — and the DOM one is deliberately adversarial: its snapshots carry
#: 300 interactive elements against the shipped
#: `agent_browser_snapshot_max_elements` default of 100, so the live page in
#: the request is three times the size the sidecar would normally return. A
#: ceiling derived from the comfortable case is a ceiling that fails on a
#: Tuesday.
AGENT_REQUEST_TOKEN_CEILING = 12_500

#: What an *average* request may carry. This is the number that decides whether
#: Grok is reachable, because 50,000-tokens-per-60-seconds is a rate and a rate
#: is paid on the mean, not on the worst.
AGENT_REQUEST_TOKEN_MEAN = 9_500


def _mean(values) -> int:
    return sum(values) // len(values)


async def test_no_request_may_exceed_the_ceiling(before_after):
    """The guard. This is the assertion that stops the quadratic coming back a third time.

    It is a *per request* ceiling, so it fails on the request that broke it
    rather than on a total that could hide one enormous call among thirty-five
    small ones.
    """
    _, after = before_after
    worst = max(after["per_call"])
    assert worst <= AGENT_REQUEST_TOKEN_CEILING, (
        f"a request carried {worst:,} prompt tokens; the ceiling is "
        f"{AGENT_REQUEST_TOKEN_CEILING:,}"
    )
    assert _mean(after["per_call"]) <= AGENT_REQUEST_TOKEN_MEAN


async def test_the_mean_is_what_makes_a_50k_per_minute_endpoint_reachable():
    """The reason there is a mean as well as a ceiling.

    `grok-4-1-fast-reasoning` is $0.20/1M input against the reason tier's
    $5.00, but it is served from an account measured at 50,000 tokens **and**
    50 requests per 60 seconds — `x-ratelimit-limit-tokens: 50000`,
    `x-ratelimit-renewalperiod-tokens: 60`, read off a live call on
    2026-08-23. Not 50 TPM: the `sku.capacity: 50` on the deployment is
    thousands.

    At the ~14,000 prompt tokens a call the ledger recorded, that budget is 3.5
    steps a minute, and a 35-step run would spend most of its wall clock in
    429s. This is the assertion that says the loop now fits inside it with the
    request cap, not the token cap, as the binding constraint.
    """
    steps_per_minute = 50_000 // AGENT_REQUEST_TOKEN_MEAN
    assert steps_per_minute >= 5
    # And a sanity check on the other limit, so a future reduction does not
    # quietly move the bottleneck without anyone noticing which one it is.
    assert steps_per_minute <= 50, "at this size the 50-requests-a-minute cap binds first"


async def test_the_run_never_pays_to_describe_a_tool_it_could_not_call(before_after):
    """Where the saving actually came from, so the next reader does not have to guess."""
    before, after = before_after
    assert set(before["tools"]) == {_tool_tokens(ALL_TOOLS)}, "the before leg was not unbudgeted"
    # The opening call has no desktop, so it is offered four tools: the three
    # that are always advertised plus `create_work_item`, which is the standing
    # price of a bot that can write down what it found and is deliberately not
    # gated on a machine — see `context_budget._is_usable`. 600 -> 800 is that
    # one schema, measured at 280 tokens, and nothing else: this fixture's
    # tenant has no work items and holds no id, so the other three work-item
    # schemas are not on the wire at all.
    assert min(after["tools"]) < 800
    # And the steady state is smaller than the whole vocabulary on every call.
    assert max(after["tools"]) < _tool_tokens(ALL_TOOLS)
    assert sum(after["tools"]) < sum(before["tools"])


# ---------------------------------------------------------------------------
# 5. The same measurement for a DOM run, where the text really is quadratic
# ---------------------------------------------------------------------------
#
# The pixel run above under-states the conversation half: a `click` result is a
# dozen characters. A DOM run is where the text quadratic lives, because
# `browser.RESULT_MAX_CHARS` is 12,000 and every one of those snapshots rides
# along on every subsequent request even though the next navigation voided
# every reference in it.


@pytest.fixture
def browsing_bot(db, make_bot):
    """A bot whose desktop row says running, so the DOM lane is reachable."""

    async def _make(user):
        from app.models import BotDesktop

        bot = await make_bot(user, name="Browsy", daily_budget_usd=500.0)
        db.add(
            BotDesktop(
                bot_id=bot.id,
                state="running",
                control_url="http://desktop.test:7910",
                stream_url="http://desktop.test:6901",
            )
        )
        await db.flush()
        return bot

    return _make


def _sidecar_returning(elements: int):
    """A `/browser` lane whose snapshots carry `elements` interactive rows."""
    body = "\n".join(f'e{n} button "Result {n} — connect"' for n in range(elements))

    async def _call(_db, _bot_id, action, payload=None):
        if action == "browser_snapshot":
            return {
                "ok": True,
                "status": 200,
                "snapshot_id": "s1",
                "target_id": "T1",
                "url": "https://linkedin.test/search",
                "title": "Search",
                "interactive_total": elements,
                "matched": elements,
                "returned": elements,
                "truncated": False,
                "frames": 1,
                "snapshot": body,
            }
        return {"ok": True, "action": action, "status": 200, "role": "button", "name": "Connect"}

    return _call


@pytest.fixture
def big_sidecar(monkeypatch):
    """Snapshots at the shipped `agent_browser_snapshot_max_elements` default.

    A hundred rows is what the sidecar actually returns unless the model asks
    for more, so this is the run the mean is measured on. The adversarial case
    — a model that raised the cap — gets its own test against the ceiling.
    """
    monkeypatch.setattr(simulation._desktop, "browser_call", _sidecar_returning(100))


async def _dom_run(agent_with, db, user_a, make_thread, bot, monkeypatch):
    patch_real_sized_screens(monkeypatch)
    script = []
    for n in range(SCRIPTED_STEPS // 2):
        script.append(acts("", call("browser_snapshot")))
        script.append(acts("", call("browser_click", ref=f"e{n}")))
    script.append(acts("", call(TOOL_TASK_COMPLETE, summary="Done.")))
    orchestrator = agent_with(script)
    thread = await make_thread(user_a, [bot])
    await turn(orchestrator, db, user_a, thread, "connect with everyone")
    return orchestrator.router


@pytest.fixture
async def dom_before_after(
    agent_with, db, user_a, make_thread, browsing_bot, big_sidecar, monkeypatch
):
    bot = await browsing_bot(user_a)
    with monkeypatch.context() as off:
        _unbudgeted(off)
        before = _profile(await _dom_run(agent_with, db, user_a, make_thread, bot, off))
    after = _profile(await _dom_run(agent_with, db, user_a, make_thread, bot, monkeypatch))
    assert before["calls"] > 20, "the DOM script did not run"
    return before, after


async def test_a_dom_run_stops_re_sending_snapshots_it_has_moved_past(dom_before_after):
    before, after = dom_before_after
    print(
        f"\n  DOM run: {after['calls']} model calls"
        f"\n  before: {before['input_tokens']:>9,} input tokens  ${before['usd']:.4f}  "
        f"({before['input_tokens'] // before['calls']:,}/call)"
        f"\n  after:  {after['input_tokens']:>9,} input tokens  ${after['usd']:.4f}  "
        f"({after['input_tokens'] // after['calls']:,}/call)"
        f"\n  reduction: {before['input_tokens'] / max(after['input_tokens'], 1):.1f}x"
    )
    assert after["input_tokens"] * 2 < before["input_tokens"], (
        "compaction should more than halve a snapshot-heavy run"
    )


async def test_a_dom_run_stays_under_the_same_ceiling(dom_before_after):
    """Same ceiling, against a run whose pages are three times the shipped cap."""
    _, after = dom_before_after
    worst = max(after["per_call"])
    assert worst <= AGENT_REQUEST_TOKEN_CEILING, (
        f"a DOM request carried {worst:,} prompt tokens; the ceiling is "
        f"{AGENT_REQUEST_TOKEN_CEILING:,}"
    )
    assert _mean(after["per_call"]) <= AGENT_REQUEST_TOKEN_MEAN


async def test_a_page_three_times_the_shipped_cap_still_fits_under_the_ceiling(
    agent_with, db, user_a, make_thread, browsing_bot, monkeypatch
):
    """The adversarial case: a model that raised `max_elements` to 300.

    That is a legal thing for it to do — the whole point of the parameter is
    that the model can ask for more — so the ceiling has to survive it. This is
    where the headroom between the mean and the ceiling is spent.
    """
    monkeypatch.setattr(simulation._desktop, "browser_call", _sidecar_returning(300))
    bot = await browsing_bot(user_a)
    after = _profile(await _dom_run(agent_with, db, user_a, make_thread, bot, monkeypatch))
    worst = max(after["per_call"])
    print(f"\n  300-element pages: worst request {worst:,} tokens")
    assert worst <= AGENT_REQUEST_TOKEN_CEILING


async def test_the_growth_is_bounded_rather_than_merely_slower(dom_before_after):
    """A linear-but-still-growing request is a quadratic bill with a smaller constant.

    The last request of a 35-step run must not be dramatically larger than the
    tenth, which is the property `[:N]`-style truncation would also give — and
    the tests in section 2 are what say this one was not bought that way.
    """
    _, after = dom_before_after
    tenth = after["per_call"][10]
    last = after["per_call"][-1]
    assert last < tenth * 1.5, f"request 10 was {tenth:,} tokens and the last was {last:,}"
