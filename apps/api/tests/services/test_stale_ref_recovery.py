"""A stale reference, recovered from once — and every way that must not happen.

The measured failure, from a real Lead Bot run against LinkedIn::

    33 desktop actions, 32 ran
    browser_click(ref='e514') - failed - stale_ref (409):
        e514 belongs to snapshot s14, not s15

The run had searched, found a qualified prospect, opened her profile, opened the
company page and cross-checked on Google. The one thing it could not do was act
on an element it had decided about two steps earlier. Every DOM action can
invalidate every ref, and asking a model to track that by hand across forty
steps is a tool-design mistake: each miss costs a step, a model call and real
money.

Two fixes, and the order matters because the cheap one removes most of the work
from the expensive one.

* **Provenance (free).** The loop pins the snapshot a ref *came from* instead of
  the newest one it has seen. The sidecar keeps four snapshots resolvable and
  re-verifies role, name, page and document membership on every resolve, so the
  pin was never what made a ref safe - it was only ever making an honest ref
  look stale. Nothing is added to any prompt.
* **Recovery.** When the sidecar genuinely does not know a ref, the element is
  found again by the identity the loop recorded and the action is retried once.

The property under test throughout is the same sentence the approved path is
built on - **what runs is the element that was described, or nothing** - plus
one the ordinary path adds: *and being retried never lets an action skip an
approval it would otherwise have needed.*
"""

from __future__ import annotations

import pytest

from app.models import ActionLog, BotDesktop
from app.services import browser as B
from app.services import simulation
from app.services.orchestrator import BROWSER_SNAPSHOT_MEMORY, TOOL_TASK_COMPLETE
from tests.services.conftest import acts, call, turn

PAGE = "https://linkedin.test/in/prospect"

BENCH = (
    'e1 heading "Ada Prospect"\n'
    'e2 link "Northwind Ltd" -> /company/northwind\n'
    'e3 button "Connect"\n'
    'e4 button "Delete account"'
)


@pytest.fixture
def live_desktop(db, make_bot):
    async def _make(user, name="Leady"):
        bot = await make_bot(user, name=name, daily_budget_usd=500.0)
        db.add(
            BotDesktop(bot_id=bot.id, state="running", control_url="http://desktop.test:7910")
        )
        await db.flush()
        return bot

    return _make


@pytest.fixture
def sidecar(monkeypatch):
    """A scriptable `/browser/*`, recording exactly what was sent to it.

    Patched one layer *below* the chokepoint, so everything still goes through
    `simulation.perform` and the risk gate, the approval flow and the undo log
    are all exercised for real.
    """
    sent: list[tuple[str, dict]] = []
    replies: dict[str, list[dict]] = {}

    async def _call(_db, _bot_id, action, payload=None):
        sent.append((action, dict(payload or {})))
        queue = replies.get(action)
        if queue:
            return queue.pop(0) if len(queue) > 1 else dict(queue[0])
        return {"ok": True, "action": action, "status": 200}

    monkeypatch.setattr(simulation._desktop, "browser_call", _call)
    return type("Sidecar", (), {"sent": sent, "replies": replies})()


def snapshot_reply(
    snapshot: str = BENCH,
    *,
    snapshot_id: str = "s15",
    url: str = PAGE,
    truncated: bool = False,
    byte_capped: bool = False,
) -> dict:
    return {
        "ok": True,
        "status": 200,
        "snapshot_id": snapshot_id,
        "target_id": "T1",
        "url": url,
        "title": "Ada Prospect",
        "interactive_total": 4,
        "matched": 4,
        "returned": 4,
        "truncated": truncated,
        "byte_capped": byte_capped,
        "frames": 1,
        "snapshot": snapshot,
    }


def stale(ref: str = "e514", *, code: str = "stale_ref") -> dict:
    """The sidecar refusing a ref, in its own words."""
    detail = (
        f"{ref} belongs to snapshot s14, not s15"
        if code == "stale_ref"
        else f"{ref} is not from a live snapshot"
    )
    return {"ok": False, "status": 409, "action": "browser_click", "error": code, "detail": detail}


def held(ref: str = "e514", *, label: str = 'button "Connect"', url: str = PAGE) -> dict:
    """The payload the loop builds for a ref it has provenance for."""
    return {
        "ref": ref,
        "snapshot_id": "s14",
        B.REF_LABEL_KEY: label,
        B.REF_PAGE_KEY: url,
        B.REF_TARGET_KEY: "T1",
    }


async def ordinary(db, bot, payload, action="browser_click", **kw) -> dict:
    """One un-approved step, down the real chokepoint."""
    outcome = await simulation.perform(
        db,
        simulation.Effect(
            kind="desktop", bot_id=bot.id, action=action, input_data=payload, **kw
        ),
    )
    return outcome


def actions(sidecar) -> list[str]:
    return [action for action, _ in sidecar.sent]


# ---------------------------------------------------------------------------
# The thing that was broken
# ---------------------------------------------------------------------------


async def test_a_stale_ref_is_re_resolved_and_the_action_lands(db, user_a, live_desktop, sidecar):
    """The headline. The model's ref is dead and its click still happens.

    Note what goes on the wire the second time: not `e514`, which is a fact
    about a snapshot that no longer exists, but the ref the *fresh* snapshot
    gave the element with that role and that accessible name.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [
        stale(),
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"},
    ]
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["ok"], outcome.result
    assert actions(sidecar) == ["browser_click", "browser_snapshot", "browser_click"]
    first, _snap, second = (payload for _a, payload in sidecar.sent)
    assert first["ref"] == "e514"
    assert second["ref"] == "e3"
    assert second["snapshot_id"] == "s15"
    # …and everything else about the call is carried through untouched.
    assert second[B.REF_LABEL_KEY] == 'button "Connect"'


async def test_the_result_tells_the_model_its_other_refs_are_dead_too(
    db, user_a, live_desktop, sidecar
):
    """A silent recovery would fix one step and leave the next four broken.

    The model is holding a whole snapshot of references that are exactly as
    stale as the one that just failed. Two sentences here are cheaper than four
    more failed steps.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [
        stale(),
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"},
    ]
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]

    outcome = await ordinary(db, bot, held())

    text = B.result_text("browser_click", outcome.result)
    assert "browser_click ran on button \"Connect\"" in text
    assert "no longer valid (stale_ref)" in text
    assert "browser_snapshot before your next one" in text
    assert outcome.result[B.RECOVERED_KEY] is True
    assert outcome.result["requested_ref"] == "e514"


async def test_the_lookup_asks_for_the_whole_page_by_name_only(db, user_a, live_desktop, sidecar):
    """The two choices that make the answer trustworthy.

    `viewport_only=False`, so "the page scrolled" cannot read as "the element is
    gone". Filtered by *name* and never by role, so the result is provably a
    superset of the exact matches and uniqueness stays sound; the role is
    matched here instead.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [stale()]
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]

    await ordinary(db, bot, held())

    snap = next(payload for action, payload in sidecar.sent if action == "browser_snapshot")
    assert snap["viewport_only"] is False
    assert snap["name_filter"] == "Connect"
    assert "role_filter" not in snap
    assert snap["target_id"] == "T1"
    assert snap["include_text"] is False


async def test_unknown_ref_recovers_the_same_way(db, user_a, live_desktop, sidecar):
    """The owner's other failure: `e49 is not from a live snapshot`."""
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [
        stale("e49", code="unknown_ref"),
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"},
    ]
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]

    outcome = await ordinary(db, bot, held("e49"))

    assert outcome.result["ok"], outcome.result
    assert outcome.result["recovered_from"] == "unknown_ref"


async def test_one_action_one_row_in_the_undo_log(db, user_a, live_desktop, sidecar):
    """The snapshot is how the effect was carried out, not an effect of its own.

    Routing the lookup back through the chokepoint would put a
    `browser_snapshot` in the audit trail as though the bot had decided to take
    one, and would make one click read as three actions.
    """
    from sqlalchemy import select

    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [
        stale(),
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"},
    ]
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]

    await ordinary(db, bot, held())

    logged = (await db.execute(select(ActionLog))).scalars().all()
    assert [row.action for row in logged] == ["browser_click"]


# ---------------------------------------------------------------------------
# Negative controls. Each must refuse, and say which.
# ---------------------------------------------------------------------------


async def test_an_element_that_is_genuinely_gone_refuses_and_names_itself(
    db, user_a, live_desktop, sidecar
):
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [stale()]
    sidecar.replies["browser_snapshot"] = [snapshot_reply('e9 button "Message"')]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["ok"] is False
    assert outcome.result["error"] == B.REF_ELEMENT_MISSING
    assert outcome.result["caused_by"] == "stale_ref"
    # Exactly one click was attempted, and it was the one that failed.
    assert actions(sidecar) == ["browser_click", "browser_snapshot"]
    text = B.result_text("browser_click", outcome.result)
    assert 'button "Connect"' in text
    assert "Nothing was clicked" in text
    assert "browser_snapshot" in text


async def test_two_matching_elements_refuse_rather_than_pick_one(
    db, user_a, live_desktop, sidecar
):
    """The safety property, in one test.

    Two Connect buttons is exactly the moment a positional fallback or a
    "closest match" would press the wrong one.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [stale()]
    sidecar.replies["browser_snapshot"] = [
        snapshot_reply('e7 button "Connect"\ne8 button "Connect"')
    ]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["error"] == B.REF_ELEMENT_AMBIGUOUS
    assert outcome.result["matched"] == 2
    assert "e7, e8" in outcome.result["detail"]
    assert actions(sidecar) == ["browser_click", "browser_snapshot"]


@pytest.mark.parametrize("flag", ["truncated", "byte_capped"])
async def test_a_snapshot_that_admits_it_is_short_cannot_prove_uniqueness(
    db, user_a, live_desktop, sidecar, flag
):
    """One match in an incomplete snapshot is not a unique match.

    Acting on it would be the positional guess in disguise: there may well be a
    second `button "Connect"` in the part that was cut off.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [stale()]
    sidecar.replies["browser_snapshot"] = [snapshot_reply(**{flag: True})]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["error"] == B.REF_ELEMENT_AMBIGUOUS
    assert "more elements than one snapshot can show" in outcome.result["detail"]
    assert actions(sidecar) == ["browser_click", "browser_snapshot"]


async def test_a_navigated_page_refuses_even_when_the_name_matches(
    db, user_a, live_desktop, sidecar
):
    """A same-named button on another page is a different button.

    This is the case identity-matching alone gets wrong, and it is why the loop
    records the page a ref was read off as well as what it was.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [stale()]
    sidecar.replies["browser_snapshot"] = [
        snapshot_reply('e2 button "Connect"', url="https://linkedin.test/in/someone-else")
    ]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["error"] == B.REF_PAGE_CHANGED
    assert outcome.result["recorded_url"] == PAGE
    assert outcome.result["current_url"] == "https://linkedin.test/in/someone-else"
    assert actions(sidecar) == ["browser_click", "browser_snapshot"]


async def test_the_same_page_with_a_different_query_still_counts(
    db, user_a, live_desktop, sidecar
):
    """Query and fragment are in-page state, not a different page.

    Refusing on a re-sorted results page would reintroduce the failure this
    whole path exists to fix.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [
        stale(),
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"},
    ]
    sidecar.replies["browser_snapshot"] = [snapshot_reply(url=PAGE + "?tab=about#top")]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["ok"], outcome.result


async def test_a_restarted_browser_says_so_rather_than_blaming_the_element(
    db, user_a, live_desktop, sidecar
):
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [stale()]
    sidecar.replies["browser_snapshot"] = [snapshot_reply("", url="about:blank")]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["error"] == B.BROWSER_SESSION_LOST
    assert "restarted" in B.result_text("browser_click", outcome.result)


async def test_a_stopped_desktop_is_not_a_missing_element(db, user_a, live_desktop, sidecar):
    """`503` on the look must not be reported as "the button is gone"."""
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [stale()]
    sidecar.replies["browser_snapshot"] = [
        {
            "ok": False,
            "status": 503,
            "error": B.BROWSER_UNAVAILABLE,
            "detail": "desktop not running",
        }
    ]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["error"] == B.BROWSER_UNAVAILABLE
    assert "could not read the page at all" in outcome.result["detail"]
    assert actions(sidecar) == ["browser_click", "browser_snapshot"]


async def test_the_retry_is_not_a_second_chance_at_the_action(db, user_a, live_desktop, sidecar):
    """Found the element, and the action still failed. That answer stands.

    `obscured` about a *live* element is far more useful than the `stale_ref`
    that started this, and it is not retried again: exactly one re-resolution,
    exactly one retry.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [
        stale(),
        {
            "ok": False,
            "status": 409,
            "error": "obscured",
            "detail": 'div#banner "We use cookies" is on top of it',
        },
    ]
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["error"] == "obscured"
    assert actions(sidecar) == ["browser_click", "browser_snapshot", "browser_click"]
    assert "cookie" in B.result_text("browser_click", outcome.result).lower()


# ---------------------------------------------------------------------------
# What is deliberately never recovered from
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    ["cdp_timeout", "cdp_error", "not_actionable", "obscured", "browser_unavailable"],
)
async def test_only_the_two_codes_that_prove_nothing_happened_are_retried(
    db, user_a, live_desktop, sidecar, code
):
    """`stale_ref` and `unknown_ref` are raised before the sidecar dispatches
    anything, so a retry is the first attempt rather than a second one.

    Everything else might have landed. A `cdp_timeout` on a click whose handler
    posted a form is precisely the case where retrying sends twice, so none of
    these buys a lookup — the failure goes back as the sidecar wrote it.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [
        {"ok": False, "status": 504, "error": code, "detail": "…"}
    ]

    outcome = await ordinary(db, bot, held())

    assert outcome.result["error"] == code
    assert actions(sidecar) == ["browser_click"]


async def test_a_ref_with_no_recorded_identity_gets_the_sidecars_own_answer(
    db, user_a, live_desktop, sidecar
):
    """Nothing was recorded about what that ref was, so there is nothing to find.

    Re-resolving without an identity would be the positional guess this whole
    module refuses to make. The sidecar's refusal already tells the model to
    snapshot, and that is the honest answer.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_click"] = [stale()]

    outcome = await ordinary(db, bot, {"ref": "e514", "snapshot_id": "s14"})

    assert outcome.result["error"] == "stale_ref"
    assert actions(sidecar) == ["browser_click"]


async def test_an_action_that_names_no_element_is_never_re_resolved(
    db, user_a, live_desktop, sidecar
):
    bot = await live_desktop(user_a)
    sidecar.replies["browser_key"] = [
        {"ok": False, "status": 409, "error": "stale_ref", "detail": "…"}
    ]

    await ordinary(db, bot, {"key": "Enter"}, action="browser_key")

    assert actions(sidecar) == ["browser_key"]


async def test_an_approved_action_still_takes_the_approved_path(
    db, user_a, live_desktop, sidecar
):
    """The two paths must not stack.

    The approved path re-resolves *before* it tries anything, because an
    approval's ref is hours old and stale essentially always. If the ordinary
    recovery also fired there, an approved click would pay for two snapshots and
    the audit trail would stop matching what a human agreed to.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]
    sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"}
    ]

    outcome = await ordinary(db, bot, held(), pre_approved=True)

    assert outcome.result["ok"], outcome.result
    assert actions(sidecar) == ["browser_snapshot", "browser_click"]


# ---------------------------------------------------------------------------
# …and never at the cost of an approval
# ---------------------------------------------------------------------------


async def test_a_gated_action_never_reaches_the_retry_at_all(db, user_a, live_desktop, sidecar):
    """`button "Delete account"` classifies as `delete` from its label alone.

    `simulation.perform` holds it before `_execute` is reached, so there is no
    call to fail and nothing to re-target. The retry cannot skip an approval
    because the approval happens first, upstream of it.
    """
    bot = await live_desktop(user_a)

    outcome = await ordinary(db, bot, held(label='button "Delete account"'))

    assert outcome.gated is True
    assert outcome.risk == "delete"
    assert actions(sidecar) == []


def test_the_retry_ceiling_is_stated_and_not_merely_inherited():
    """The rule written down where the retry happens, not inferred elsewhere.

    `connectors.requires_approval` gating send/spend/delete is what makes the
    test above pass today. This asserts the retry refuses them on its own, so
    a change to that module cannot silently hand auto-retry to a send.
    """
    from app.services.risk import RISK_ORDER, risk_rank

    for risk in RISK_ORDER:
        effect = simulation.Effect(kind="desktop", bot_id=None, action="browser_click")
        allowed = simulation._may_recover_ref(
            effect, simulation.Assessment(risk=risk, requires_approval=False)
        )
        assert allowed is (risk_rank(risk) < risk_rank("send")), risk
    assert simulation.RETRY_RISK_CEILING == "send"


def test_a_re_resolved_element_cannot_classify_differently():
    """Why re-targeting is safe at all, stated as an assertion.

    The gate classifies a DOM step from `ref_label`. The recovery finds an
    element whose role and accessible name are *that same string* — that is what
    matching by identity means — so the retried call classifies identically by
    construction. Re-targeting cannot turn a step a human would have seen into
    one they do not.
    """
    from app.services.risk import classify_label_risk

    target = B.ref_identity("browser_click", held(label='button "Send invoice"'))
    assert target is not None
    found = snapshot_reply('e88 button "Send invoice"')
    resolved = B.resolve_recovered(target, found, stale())

    retried = resolved["payload"]
    assert retried["ref"] == "e88"
    assert B.label_in(retried) == B.label_in(target.payload)
    assert classify_label_risk(B.label_in(retried)) == classify_label_risk(
        B.label_in(target.payload)
    )


# ---------------------------------------------------------------------------
# Provenance: the free half of the fix
# ---------------------------------------------------------------------------


async def test_the_loop_pins_the_snapshot_a_ref_actually_came_from(
    agent_with, db, user_a, make_thread, live_desktop, sidecar, varying_screens
):
    """The owner's `e514 belongs to snapshot s14, not s15`, at the source.

    The model reads the page, looks again, then acts on what it decided about —
    which is what a long task does. The ref came from `s14`; the loop used to
    pin `s15`, the newest snapshot *it* had seen, and the sidecar refused a live
    element on those grounds alone. Now the pin names the ref's own snapshot and
    the sidecar's four real checks are what decides.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [
        snapshot_reply(BENCH, snapshot_id="s14"),
        snapshot_reply('e90 heading "Ada Prospect"', snapshot_id="s15"),
    ]
    sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"}
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e3")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "connect with her")

    click = next(payload for action, payload in sidecar.sent if action == "browser_click")
    assert click["snapshot_id"] == "s14"
    assert click[B.REF_LABEL_KEY] == 'button "Connect"'
    assert click[B.REF_PAGE_KEY] == PAGE
    # One click, no recovery needed: the pin was the whole problem.
    assert actions(sidecar).count("browser_click") == 1


async def test_the_newest_snapshot_wins_for_a_ref_that_is_in_both(
    agent_with, db, user_a, make_thread, live_desktop, sidecar, varying_screens
):
    """History is consulted only for refs the current page does not have.

    The sidecar mints refs from a counter that never repeats, so this cannot
    happen against a real container — which is exactly why it is asserted here
    rather than assumed. A stale label on a live ref would mis-describe what is
    about to be clicked, and the gate reads that label.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [
        snapshot_reply('e3 button "Connect"', snapshot_id="s14"),
        snapshot_reply('e3 button "Follow"', snapshot_id="s15"),
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e3")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "follow her")

    click = next(payload for action, payload in sidecar.sent if action == "browser_click")
    assert click[B.REF_LABEL_KEY] == 'button "Follow"'
    assert click["snapshot_id"] == "s15"


async def test_a_ref_nobody_ever_saw_is_annotated_with_nothing(
    agent_with, db, user_a, make_thread, live_desktop, sidecar, varying_screens
):
    """A hallucinated ref has no page, and inventing one would be worse than
    letting the sidecar refuse it."""
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]
    sidecar.replies["browser_click"] = [stale("e999", code="unknown_ref")]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e999")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "click it")

    click = next(payload for action, payload in sidecar.sent if action == "browser_click")
    assert click == {"ref": "e999"}
    # No identity, so no lookup was bought.
    assert actions(sidecar) == ["browser_snapshot", "browser_click"]


async def test_provenance_costs_nothing_in_the_prompt(
    agent_with, db, user_a, make_thread, live_desktop, sidecar, varying_screens, monkeypatch
):
    """The measurement, in tokens, because the loop is priced per call.

    A lane measured this loop down to ~6 900 prompt tokens per call, and the
    obvious alternative fix — attach the fresh snapshot to every action result
    so the model always holds live refs — would add ~3 000 of those on a real
    page (measured on Wikipedia: 525 elements, 12 196 chars, 3 049 tokens),
    every action, for the whole run. That is the fix this one exists instead of.

    Provenance is bookkeeping the loop keeps about itself. `snapshot_id`,
    `ref_url` and `ref_target` are payload fields the proxy sends to the sidecar
    and `browser.QUIET_ANNOTATIONS` hides from anything a person reads; none of
    the three is ever rendered into a message. So the same turn, run with the
    memory at its real depth and at one snapshot — which is exactly the old
    behaviour — has to produce byte-identical prompts.
    """
    from app.services import orchestrator as orch
    from app.services.model_router import count_text_tokens

    async def _run(depth: int) -> list[int]:
        monkeypatch.setattr(orch, "BROWSER_SNAPSHOT_MEMORY", depth)
        sidecar.sent.clear()
        sidecar.replies["browser_snapshot"] = [
            snapshot_reply(BENCH, snapshot_id="s14"),
            snapshot_reply('e90 heading "Ada Prospect"', snapshot_id="s15"),
        ]
        sidecar.replies["browser_click"] = [
            {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"}
        ]
        orchestrator = agent_with(
            [
                acts("", call("browser_snapshot")),
                acts("", call("browser_snapshot")),
                acts("", call("browser_click", ref="e3")),
                acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
            ]
        )
        bot = await live_desktop(user_a, name=f"Leady{depth}")
        thread = await make_thread(user_a, [bot])
        await turn(orchestrator, db, user_a, thread, "connect with her")
        return [count_text_tokens(messages) for messages in orchestrator.router.seen]

    remembering = await _run(BROWSER_SNAPSHOT_MEMORY)
    forgetting = await _run(1)

    assert remembering == forgetting, (
        "remembering what a ref was must not put a single token in the prompt"
    )
    # And the thing it bought: the older ref is pinned to its own snapshot.
    assert remembering  # the turn really ran


def test_the_memory_is_bounded():
    """A forty-step run must not accumulate refs without limit."""
    from app.services.orchestrator import AgentSession, SnapshotRefs

    session = AgentSession()
    for n in range(BROWSER_SNAPSHOT_MEMORY + 4):
        session.remember_refs(
            SnapshotRefs(f"s{n}", PAGE, "T1", {f"e{n}": ("button", f"Connect {n}")})
        )

    assert len(session.browser_snapshots) == BROWSER_SNAPSHOT_MEMORY
    assert session.provenance("e0") is None
    assert session.provenance(f"e{BROWSER_SNAPSHOT_MEMORY + 3}").snapshot_id == (
        f"s{BROWSER_SNAPSHOT_MEMORY + 3}"
    )


async def test_the_whole_loop_recovers_and_carries_on(
    agent_with, db, user_a, make_thread, live_desktop, sidecar, varying_screens
):
    """End to end: a dead ref costs a sidecar round trip, not a model step.

    The measured failure was a run that spent one of its steps and one of its
    model calls discovering that a reference had gone stale. Here the same thing
    happens and the step log says the click ran.
    """
    from tests.services.conftest import actions_in

    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [
        # What the model reads: the element it wants is `e514`.
        snapshot_reply('e514 button "Connect"', snapshot_id="s14"),
        # What the recovery's own look finds: the page re-rendered and it is
        # `e3` now.
        snapshot_reply('e3 button "Connect"', snapshot_id="s15"),
    ]
    sidecar.replies["browser_click"] = [
        stale(),
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Connect"},
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e514")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Connected.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "connect with her")

    # One step in the transcript the human reads, and it ran.
    assert actions_in(frames) == ["browser_snapshot", "browser_click"]
    click = next(d for name, d in frames if name == "tool" and d["action"] == "browser_click")
    assert click["ok"] is True
    assert "did not go through" not in done["message"]
