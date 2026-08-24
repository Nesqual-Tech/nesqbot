"""What the bot says when a turn ends, and what it is not allowed to say.

The product owner raised the same complaint three times in three different
wordings — *"the outputs type of the agent... it's telling me things, i don't
care"*, *"the outputs still wrong"*, *"we need to make the output nicer, we need
to make everything better"* — about replies that looked like this:

    I ran 6 steps on my desktop this turn: 5 completed, 1 held for your
    approval. I did not reach a summary of my own, so the log below is the
    whole account.

    browser_click classifies as 'send', so it is waiting for you in Approvals.
    It has not run.

    <details><summary>Step log — 6 desktop actions, 5 ran</summary>
    browser_click(ref='e5', ref_label='button "Accept"') — ran

Every sentence of that is true, which is why it survived three rounds of
complaint: nothing in the suite could tell it was bad. This file is what can.
It holds the reply to four rules that the old one broke and no assertion caught:

1. **Mechanics do not lead.** Step counts belong in the fold, if anywhere.
2. **No internal vocabulary, anywhere in a reply.** Not a tool name, not a
   `ref`, not a risk grade, not this module's control flow.
3. **A blocked run says what would unblock it**, in the reader's terms.
4. **Nothing is invented, and prettier wording does not become softer wording.**
   A held action is never past tense; a failure is always a failure.

Rule 4 is the one worth being nervous about, and it is the reason this file
sits next to `test_mock_results_never_reach_the_model.py` in spirit: the whole
project has spent a session stopping a bot claiming work it did not do, and
"make the output nicer" is the most natural way in the world to undo that.
"""

from __future__ import annotations

import pytest

from app.models import BotDesktop
from app.services import browser as browser_ops
from app.services import simulation
from app.services.orchestrator import (
    _STEP_PHRASES,
    _VERB_FORMS,
    ASK_APPROVAL,
    ASK_TAKEOVER,
    BROWSER_ACTIONS,
    DESKTOP_ACTIONS,
    RISK_IN_PLAIN_WORDS,
    TOOL_TASK_COMPLETE,
    _plain_place,
    agent_tool_names,
    step_attempt,
    step_intent,
    step_phrase,
    why_it_needs_you,
)
from app.services.risk import RISK_ORDER
from app.services.simulation import DESKTOP_START, DESKTOP_STOP
from tests.services.conftest import acts, call, turn

# ---------------------------------------------------------------------------
# 1. The phrase table cannot drift away from the tool table
# ---------------------------------------------------------------------------
#
# `_step_phrases` has a fallback that turns an unknown action into "Ran browser
# tab activate". It is there so a table that has fallen behind degrades to a
# readable sentence instead of taking a reply down — not so that it can be
# relied on. These two make sure it never fires in production.


def test_every_action_the_loop_can_dispatch_has_something_to_say_about_itself():
    dispatchable = {*DESKTOP_ACTIONS, *BROWSER_ACTIONS, DESKTOP_START, DESKTOP_STOP}
    missing = dispatchable - set(_STEP_PHRASES)
    assert not missing, (
        "these actions would be rendered by the fallback, which reads badly on "
        f"purpose: {sorted(missing)}"
    )


def test_every_phrase_is_built_from_a_verb_the_renderer_knows_two_forms_of():
    """`_VERB_FORMS` and `_STEP_PHRASES` are one table read two ways.

    The reply needs the past tense for the log of what happened and the gerund
    for the block about what did not, and a verb missing from `_VERB_FORMS`
    silently becomes "Ran"/"running" — which is how "clicking Accept did not
    work" would quietly turn into "running Accept did not work".
    """
    unknown = {
        build({})[0] for build in _STEP_PHRASES.values() if build({})[0] not in _VERB_FORMS
    }
    assert not unknown, f"no past/gerund forms for: {sorted(unknown)}"


def test_every_risk_grade_can_be_explained_without_naming_itself():
    """A held action's grade always reaches the reader as a consequence.

    `services.risk` stays the single classifier. This only checks that whatever
    it can produce has a sentence here, so no reply ever falls through to
    telling somebody their click "classifies as 'send'".
    """
    for grade in RISK_ORDER:
        explained = why_it_needs_you(grade)
        assert explained in RISK_IN_PLAIN_WORDS.values()
        assert f"'{grade}'" not in explained, f"'{grade}' explains itself by name"


# ---------------------------------------------------------------------------
# 2. The phrases themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.linkedin.com/feed/?trk=nav_home", "linkedin.com/feed"),
        ("https://stardental.ro/contact/", "stardental.ro/contact"),
        ("http://example.test", "example.test"),
        ("https://www.google.com/search?q=star+dental#top", "google.com/search"),
        ("", ""),
    ],
)
def test_a_url_is_trimmed_to_the_part_a_person_reads(raw, expected):
    """Host and path survive; the scheme, the `www.` and the query do not.

    The host is what somebody recognises and the path is what tells two pages on
    it apart. `?trk=nav_home` is a tracking parameter, and a reply that carries
    it has gone back to printing a URL bar at somebody who asked for a summary.
    """
    assert _plain_place(raw) == expected


def test_an_element_is_named_by_its_label_and_never_by_its_reference():
    """`e5` names nothing outside this process. `"Accept"` names the button.

    The label is Chrome's own accessible name — the same string the approval
    gate shows a human — so it is both the most accurate description available
    and the one the reader saw on the page.
    """
    step = {
        "action": "browser_click",
        "input": {"ref": "e5", "ref_label": 'button "Accept"', "snapshot_id": "s2"},
    }
    assert step_phrase(step) == 'Clicked "Accept"'
    assert step_attempt(step) == 'clicking "Accept"'
    assert step_intent(step) == 'click "Accept"'
    assert "e5" not in step_phrase(step)
    assert "s2" not in step_phrase(step)


def test_an_element_with_no_label_left_is_not_described_as_one_that_had_one():
    """A ref whose snapshot is gone gets a noun, not a reassuring guess."""
    assert step_phrase({"action": "browser_click", "input": {"ref": "e514"}}) == (
        "Clicked something on the page"
    )


# ---------------------------------------------------------------------------
# 3. What a whole reply may and may not contain
# ---------------------------------------------------------------------------

#: Words this repo uses to talk to itself. None of them belongs in a reply.
HOUSE_VOCABULARY = (
    "classifies as",
    "did not reach a summary of my own",
    "ref=",
    "ref_label",
    "snapshot_id",
    "browser_click",
    "browser_navigate",
    "browser_snapshot",
    "open_chromium",
    "start_desktop",
    "task_complete",
    "EffectResult",
    "chokepoint",
    "pixel tools",
    "DOM",
)


def assert_speaks_english(message: str) -> None:
    for word in HOUSE_VOCABULARY:
        assert word not in message, f"the reply says {word!r}"
    # Only the namespaced names. `click`, `type` and `key` are also ordinary
    # English words, and a reply is meant to contain those.
    for name in agent_tool_names():
        if "_" in name:
            assert name not in message, f"the reply names the tool {name!r}"


async def test_a_completed_run_leads_with_the_result_and_never_with_the_count(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The shape the owner asked for: outcome first, mechanics folded away."""
    orchestrator = agent_with(
        [
            acts("", call("open_chromium", text="https://stardental.ro/contact")),
            acts("", call("browser_text")),
            acts(
                "",
                call(
                    TOOL_TASK_COMPLETE,
                    summary=(
                        "Star Dental takes bookings through a form on their contact page "
                        "rather than by email. I have drafted the Romanian outreach copy "
                        "against it."
                    ),
                ),
            ),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "find the clinic")
    message = done["message"]

    assert message.startswith("Star Dental takes bookings through a form")
    assert_speaks_english(message)
    # The count exists, once, and only inside the fold.
    head, _, folded = message.partition("**What I did")
    assert "steps on the desktop" not in head
    assert "steps on the desktop" in folded
    # And the fold is sentences.
    assert "Opened stardental.ro/contact" in folded


async def test_a_run_stopped_for_approval_leads_with_the_ask_and_says_what_unblocks_it(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The LinkedIn reply, rewritten.

    What shipped opened with "I ran 6 steps... 5 completed, 1 held" and then
    explained the hold as `browser_click classifies as 'send'`. A person who
    just wants to know why nothing happened learns neither what is waiting nor
    how to release it. Both are now the first thing in the reply, and no
    headline is manufactured above the ask — on a run that stopped for a person,
    anything printed over "here is what needs you" is a sentence in the way.
    """
    orchestrator = agent_with([acts("", call("click", x=900, y=40, risk="send"))])
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "message them")
    message = done["message"]

    assert message.startswith(ASK_APPROVAL)
    assert "sends something out on your behalf" in message
    assert "Say yes in Approvals" in message
    assert_speaks_english(message)
    # It has not happened, and nothing in the reply suggests otherwise.
    assert "It has not happened." in message
    assert "steps on the desktop" not in message.partition("**What I did")[0]


async def test_a_held_action_is_never_written_in_a_tense_that_says_it_happened(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The rule that survives every rewrite of this file.

    A held step is rendered from `step_intent` — *click "Send"* — and never from
    `step_phrase`, which would say *Clicked "Send"* about something that has not
    been clicked. The reply is the only place a person finds out what their bot
    did, and a nicer-reading past tense here would be a lie in the one sentence
    the approval gate exists to make true.
    """
    orchestrator = agent_with([acts("", call("click", x=900, y=40, risk="send"))])
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "message them")
    message = done["message"]

    assert "I need to click at (900, 40)" in message
    assert "Clicked at (900, 40) — waiting for your go-ahead" in message
    assert "Clicked at (900, 40)\n" not in message, "no line claims the held click landed"


async def test_a_takeover_says_what_the_person_has_to_do_before_anything_else(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    orchestrator = agent_with(
        [
            acts("", call("click", x=1, y=1)),
            acts(
                "",
                call(
                    "request_human_takeover",
                    reason="LinkedIn is asking for the code it just texted you",
                    what_you_need="Enter the code and dismiss the prompt.",
                ),
            ),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "sign me in")
    message = done["message"]

    assert message.startswith(ASK_TAKEOVER)
    assert "Enter the code" in message
    assert "press Continue" in message
    assert_speaks_english(message)


async def test_a_failure_is_translated_but_is_still_a_failure(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """Nicer wording must not become softer wording.

    `stale_ref (409): e514 belongs to snapshot s14, not s15` is unreadable
    outside this repo, so the reply says the page changed under it instead. What
    it must not do — the failure mode this whole rewrite could have introduced —
    is round that up into something that sounds like it worked.
    """
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_click"] = [
        {
            "ok": False,
            "status": 409,
            "error": "stale_ref",
            "detail": "e514 belongs to snapshot s14, not s15",
        }
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_click", ref="e514")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Could not get in.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "open the clinic")
    message = done["message"]

    assert "**What did not work:**" in message
    assert "the page had changed by the time I went to act on it" in message
    assert "did not work" in message
    assert_speaks_english(message)


# ---------------------------------------------------------------------------
# 3b. The approval card is the same vocabulary, on a different surface
# ---------------------------------------------------------------------------
#
# *"on approval i would like to see what the agent is trying to do, the message
# it's trying to send, not payloads."*
#
# The card used to render `{"ref": "e358", "text": "Salut! Am văzut…"}` under a
# heading reading `Desktop action: browser_click`. It is now built by
# `held_action_in_plain_words` from the same table `step_phrase` and
# `why_it_needs_you` are built from — which is the whole point. Two dialects for
# one product is worse than one technical dialect, so the guard that keeps the
# reply honest is pointed at the approval as well.


async def test_a_held_action_says_what_it_is_in_the_same_words_the_reply_uses(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """Title, summary and the card block. None of them names a tool or a ref."""
    from sqlalchemy import select

    from app.models import Approval

    bot = await live_desktop(user_a, name="Lead Bot")
    browser_sidecar.replies["browser_snapshot"] = [
        {
            "ok": True,
            "status": 200,
            "snapshot_id": "s1",
            "target_id": "T1",
            "url": "https://www.linkedin.com/in/andrei-pop?trk=nav",
            "title": "Andrei Pop",
            "snapshot": 'e2 textbox "Write a message…"\ne3 button "Send"',
        }
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_type", ref="e2", text="Salut! Am văzut clinica.")),
            acts("", call("browser_click", ref="e3", risk="send")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "message this lead")

    approval = (await db.execute(select(Approval))).scalars().one()
    plain = approval.payload["plain"]

    assert approval.title == 'Click "Send" on linkedin.com/in/andrei-pop'
    assert plain["intent"] == 'click "Send"'
    assert plain["place"] == "linkedin.com/in/andrei-pop"
    assert plain["why"] == "it sends something out on your behalf"
    # The thing the owner actually asked to see.
    assert plain["message"]["text"] == "Salut! Am văzut clinica."
    assert plain["message"]["into"] == '"Write a message…"'
    assert plain["leading_up_to"] == [
        "Read what was on the page",
        'Typed "Salut! Am văzut clinica." into "Write a message…"',
    ]
    for text in (approval.title, approval.summary, plain["intent"], *plain["leading_up_to"]):
        assert_speaks_english(text)
    # And the raw step is still there, unchanged, for whoever has to debug it.
    assert approval.payload["steps"][0]["ref"] == "e3"


async def test_the_plain_block_is_not_paid_for_on_every_model_call(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """It is written for a person and it never reaches the model.

    Tool schemas and conversation are re-sent on every call, so anything that
    leaks into either is paid for repeatedly. The plain block lives on
    `Approval.payload`, which the loop writes and never reads back: the model's
    own copy of the same event is the one sentence it already got — *HELD, not
    run* — and the sentence a person reads is not information it can act on.
    """
    from sqlalchemy import select

    from app.models import Approval

    bot = await live_desktop(user_a, name="Lead Bot")
    browser_sidecar.replies["browser_snapshot"] = [
        {
            "ok": True,
            "status": 200,
            "snapshot_id": "s1",
            "url": "https://www.linkedin.com/in/andrei-pop",
            "snapshot": 'e2 textbox "Write a message…"\ne3 button "Send"',
        }
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_type", ref="e2", text="Salut! Am văzut clinica.")),
            acts("", call("browser_click", ref="e3", risk="send")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "message this lead")

    approval = (await db.execute(select(Approval))).scalars().one()
    assert approval.payload["plain"]["leading_up_to"]

    # Every message the loop handed the model this turn, as the model saw it.
    sent = "\n".join(
        str(message.get("content") or "")
        for messages in orchestrator.router.seen
        for message in messages
    )
    assert "leading_up_to" not in sent
    assert "it sends something out on your behalf" not in sent
    assert "needs your say-so" not in sent
    assert 'Click "Send"' not in sent
    # The control's own description *is* in there — it came off the snapshot the
    # model asked for. That is the pre-existing cost of the DOM lane, and it is
    # one line of a page reading, not a block written for a person.
    assert 'button "Send"' in sent


async def test_the_held_action_is_never_written_as_though_it_had_happened(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The reply's rule, restated on the surface most likely to erode it.

    An approval card is a nicer-looking thing than a step log, and "Sent the
    message to Andrei Pop" is nicer-reading than "click Send". It is also a lie
    about the one fact the gate exists to establish.
    """
    from sqlalchemy import select

    from app.models import Approval

    bot = await live_desktop(user_a, name="Lead Bot")
    browser_sidecar.replies["browser_snapshot"] = [
        {
            "ok": True,
            "status": 200,
            "snapshot_id": "s1",
            "url": "https://www.linkedin.com/in/andrei-pop",
            "snapshot": 'e3 button "Send"',
        }
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e3", risk="send")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "message this lead")

    approval = (await db.execute(select(Approval))).scalars().one()
    assert "It has not happened." in approval.summary
    assert "Clicked" not in approval.summary
    assert "Clicked" not in approval.payload["plain"]["intent"]
    assert approval.payload["plain"]["message"] is None, "nothing was typed, so nothing is shown"


async def test_a_message_typed_on_another_page_is_not_this_ones_message(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The one way this block could show somebody the wrong text.

    A lead-generation run types into one profile, moves to the next, and holds a
    Send there. Carrying the previous lead's copy onto this card would be worse
    than showing nothing — it is a specific, plausible, wrong answer to "what is
    my bot about to send".
    """
    from sqlalchemy import select

    from app.models import Approval

    bot = await live_desktop(user_a, name="Lead Bot")
    browser_sidecar.replies["browser_snapshot"] = [
        {
            "ok": True,
            "status": 200,
            "snapshot_id": "s1",
            "url": "https://www.linkedin.com/in/maria-ionescu",
            "snapshot": 'e2 textbox "Write a message…"',
        },
        {
            "ok": True,
            "status": 200,
            "snapshot_id": "s2",
            "url": "https://www.linkedin.com/in/andrei-pop",
            "snapshot": 'e3 button "Send"',
        },
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_type", ref="e2", text="Salut Maria!")),
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e3", risk="send")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "message these leads")

    approval = (await db.execute(select(Approval))).scalars().one()
    assert approval.payload["plain"]["place"] == "linkedin.com/in/andrei-pop"
    assert approval.payload["plain"]["message"] is None


def test_a_failure_nobody_wrote_a_sentence_for_keeps_its_technical_one():
    """An unrecognised failure has to read as unrecognised.

    The comfortable bug would be a catch-all — "something went wrong" — which
    reads fine and tells a person nothing they can act on. `plain_failure`
    answers empty instead, and the caller prints `short_failure`.
    """
    assert browser_ops.plain_failure({"error": "a_code_from_the_future"}) == ""
    assert browser_ops.plain_failure({"error": "stale_ref"})


# ---------------------------------------------------------------------------
# Fixtures for the one case that needs a real browser failure
# ---------------------------------------------------------------------------
#
# Copies of the two in `test_browser_agency.py`. Duplicated rather than moved to
# `conftest.py` because that file is shared by every service suite and this is
# the only other user; a fixture promoted for two callers is how a conftest
# turns into a grab bag.


@pytest.fixture
def live_desktop(db, make_bot):
    """A bot whose desktop row says running, pointed at a control URL."""

    async def _make(user, name="Browsy", control_url="http://desktop.test:7910"):
        bot = await make_bot(user, name=name, daily_budget_usd=500.0)
        db.add(
            BotDesktop(
                bot_id=bot.id,
                state="running",
                control_url=control_url,
                stream_url="http://desktop.test:6901",
            )
        )
        await db.flush()
        return bot

    return _make


@pytest.fixture
def browser_sidecar(monkeypatch):
    """Answer `/browser/*` from a script, below the chokepoint."""
    replies: dict[str, list[dict]] = {}

    async def _call(_db, _bot_id, action, payload=None):
        queue = replies.get(action)
        if queue:
            return queue.pop(0) if len(queue) > 1 else dict(queue[0])
        return {"ok": True, "action": action, "status": 200}

    monkeypatch.setattr(simulation._desktop, "browser_call", _call)
    return type("Sidecar", (), {"replies": replies})()
