"""Executing what a human approved, an hour after they read it.

The gate is the product's central claim, and until now approving a DOM click
almost always failed. The loop pinned a `snapshot_id` into the held payload; by
the time anyone pressed Approve that snapshot was evicted and the sidecar
answered `409 stale_ref`. The refusal was correct — it will not click whatever
now occupies node `e9` — and it made approving mean *re-run the whole task*.

What the person read was `browser_click on button "Delete account"`. So that is
what executes: a fresh snapshot, the element re-found by the role and accessible
name in the sentence they approved, and the action taken on that. The property
under test throughout this file is one sentence long — **what runs is the
element the approval described, or nothing** — and every way "or nothing"
happens has to come back as something a person can act on.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Approval, BotDesktop
from app.services import approvals as approvals_service
from app.services import browser as B
from app.services import simulation

BENCH = (
    'e1 heading "Your account"\n'
    'e2 textbox "Discount code"\n'
    'e3 link "Keep shopping" -> /shop\n'
    'e4 button "Delete account"'
)

PAGE = "https://shop.test/account"


@pytest.fixture
def live_desktop(db, make_bot):
    async def _make(user, name="Browsy"):
        bot = await make_bot(user, name=name, daily_budget_usd=500.0)
        db.add(
            BotDesktop(bot_id=bot.id, state="running", control_url="http://desktop.test:7910")
        )
        await db.flush()
        return bot

    return _make


@pytest.fixture
def sidecar(monkeypatch):
    """A scriptable `/browser/*`, recording exactly what was sent to it."""
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
    snapshot_id: str = "s9",
    url: str = PAGE,
    truncated: bool = False,
) -> dict:
    return {
        "ok": True,
        "status": 200,
        "snapshot_id": snapshot_id,
        "target_id": "T1",
        "url": url,
        "title": "Account",
        "interactive_total": 4,
        "matched": 4,
        "returned": 4,
        "truncated": truncated,
        "frames": 1,
        "snapshot": snapshot,
    }


def held(**overrides) -> dict:
    """The payload the gate stored when it held a click on Delete account."""
    return {
        "ref": "e4",
        "snapshot_id": "s1",
        B.REF_LABEL_KEY: 'button "Delete account"',
        B.REF_PAGE_KEY: PAGE,
        B.REF_TARGET_KEY: "T1",
        **overrides,
    }


async def approved(db, bot, payload, action="browser_click") -> dict:
    """One approved step, down the real chokepoint."""
    outcome = await simulation.perform(
        db,
        simulation.Effect(
            kind="desktop",
            bot_id=bot.id,
            action=action,
            input_data=payload,
            pre_approved=True,
        ),
    )
    return outcome.result


# ---------------------------------------------------------------------------
# The thing that was broken
# ---------------------------------------------------------------------------


async def test_an_approved_click_re_resolves_the_element_and_lands(db, user_a, live_desktop, sidecar):
    """The headline. The pinned snapshot is long dead and the click still lands.

    Note what is asserted about the ref: `e4` in the held payload was a
    coincidence of the snapshot that minted it, and what goes on the wire is the
    ref the *fresh* snapshot gave the element with that name.
    """
    bot = await live_desktop(user_a)
    fresh = 'e77 button "Delete account"\ne78 link "Keep shopping" -> /shop'
    sidecar.replies["browser_snapshot"] = [snapshot_reply(fresh, snapshot_id="s42")]
    sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e77", "role": "button", "name": "Delete account"}
    ]

    result = await approved(db, bot, held())

    assert result["ok"], result
    click = next(payload for action, payload in sidecar.sent if action == "browser_click")
    assert click["ref"] == "e77"
    assert click["snapshot_id"] == "s42"


async def test_the_re_resolution_snapshot_asks_for_the_whole_page(db, user_a, live_desktop, sidecar):
    """A scrolled page must not read as a missing element.

    `viewport_only` false is the difference between "it is not there" and "it is
    not on screen", and only one of those is true.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]

    await approved(db, bot, held())

    snap = next(payload for action, payload in sidecar.sent if action == "browser_snapshot")
    assert snap["viewport_only"] is False
    assert snap["name_filter"] == "Delete account"
    assert snap["target_id"] == "T1"


# ---------------------------------------------------------------------------
# …or nothing
# ---------------------------------------------------------------------------


async def test_a_missing_element_refuses_and_clicks_nothing(db, user_a, live_desktop, sidecar):
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply('e1 button "Something else"')]

    result = await approved(db, bot, held())

    assert result["ok"] is False
    assert result["error"] == B.APPROVED_ELEMENT_MISSING
    assert not any(action == "browser_click" for action, _ in sidecar.sent)
    text = B.result_text("browser_click", result)
    assert 'button "Delete account"' in text
    assert "Nothing was clicked" in text


async def test_two_matching_elements_refuse_rather_than_pick_one(db, user_a, live_desktop, sidecar):
    """The whole safety property, in one test.

    Two Delete account buttons is exactly the moment a positional fallback or a
    "closest match" would click the wrong one, and a gate that guesses when it
    is unsure is not a gate.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [
        snapshot_reply('e5 button "Delete account"\ne9 button "Delete account"')
    ]

    result = await approved(db, bot, held())

    assert result["error"] == B.APPROVED_ELEMENT_AMBIGUOUS
    assert result["matched"] == 2
    assert not any(action == "browser_click" for action, _ in sidecar.sent)
    assert "e5, e9" in result["detail"]


async def test_a_truncated_snapshot_cannot_prove_uniqueness(db, user_a, live_desktop, sidecar):
    """One match in an admittedly incomplete snapshot is not a unique match."""
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply(truncated=True)]

    result = await approved(db, bot, held())

    assert result["error"] == B.APPROVED_ELEMENT_AMBIGUOUS
    assert not any(action == "browser_click" for action, _ in sidecar.sent)


async def test_a_navigated_page_refuses_even_when_the_name_matches(db, user_a, live_desktop, sidecar):
    """A same-named button on another page is a different button.

    This is the case identity-matching alone would get wrong, and it is why the
    approval carries the page it was read off as well as the element.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [
        snapshot_reply(url="https://other.test/settings")
    ]

    result = await approved(db, bot, held())

    assert result["error"] == B.APPROVED_PAGE_CHANGED
    assert result["approved_url"] == PAGE
    assert result["current_url"] == "https://other.test/settings"
    assert not any(action == "browser_click" for action, _ in sidecar.sent)


async def test_the_same_page_with_a_different_query_still_counts(db, user_a, live_desktop, sidecar):
    """Query and fragment are in-page state, not a different page.

    Refusing on a re-sorted results page would reintroduce the failure this
    whole path exists to fix.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply(url=PAGE + "?tab=danger#zone")]

    result = await approved(db, bot, held())

    assert result["ok"], result


async def test_a_restarted_browser_says_so(db, user_a, live_desktop, sidecar):
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply("", url="about:blank")]

    result = await approved(db, bot, held())

    assert result["error"] == B.BROWSER_SESSION_LOST
    assert "restarted" in B.result_text("browser_click", result)


async def test_a_stopped_desktop_is_not_a_missing_element(db, user_a, live_desktop, sidecar):
    """`503` on the look must not be reported as "the button is gone"."""
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [
        {
            "ok": False,
            "status": 503,
            "error": B.BROWSER_UNAVAILABLE,
            "detail": "desktop not running",
        }
    ]

    result = await approved(db, bot, held())

    assert result["error"] == B.BROWSER_UNAVAILABLE
    assert "did not run" in result["detail"]
    assert not any(action == "browser_click" for action, _ in sidecar.sent)


async def test_an_off_screen_element_is_scrolled_to_rather_than_refused(
    db, user_a, live_desktop, sidecar
):
    """`not_actionable` because the page scrolled is not a reason to refuse.

    Scrolling commits nothing, and the retry is on the *resolved* ref — so this
    can still only ever act on the element the approval named.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]
    sidecar.replies["browser_click"] = [
        {"ok": False, "status": 409, "error": "not_actionable", "detail": "outside the viewport"},
        {"ok": True, "status": 200, "ref": "e4", "role": "button", "name": "Delete account"},
    ]
    sidecar.replies["browser_scroll"] = [
        {"ok": True, "status": 200, "ref": "e4", "scrolled_into_view": True}
    ]

    result = await approved(db, bot, held())

    assert result["ok"], result
    assert [action for action, _ in sidecar.sent] == [
        "browser_snapshot",
        "browser_click",
        "browser_scroll",
        "browser_click",
    ]


async def test_an_obscured_element_is_still_a_refusal(db, user_a, live_desktop, sidecar):
    """The sidecar's own honest refusals are passed through, not retried."""
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]
    sidecar.replies["browser_click"] = [
        {
            "ok": False,
            "status": 409,
            "error": "obscured",
            "detail": 'div#banner "We use cookies" is on top of it',
        }
    ]

    result = await approved(db, bot, held())

    assert result["error"] == "obscured"
    assert sum(1 for action, _ in sidecar.sent if action == "browser_click") == 1
    assert "cookie" in B.result_text("browser_click", result).lower()


# ---------------------------------------------------------------------------
# What is deliberately left alone
# ---------------------------------------------------------------------------


async def test_a_payload_with_no_recorded_identity_runs_verbatim(db, user_a, live_desktop, sidecar):
    """Then the approval really did only say "click e9 of snapshot s1".

    Running exactly that — including the pinned snapshot the sidecar may well
    refuse — is the truthful execution of what the person read.
    """
    bot = await live_desktop(user_a)

    await approved(db, bot, {"ref": "e4", "snapshot_id": "s1"})

    assert [action for action, _ in sidecar.sent] == ["browser_click"]
    assert sidecar.sent[0][1]["snapshot_id"] == "s1"


async def test_an_action_with_no_element_runs_verbatim(db, user_a, live_desktop, sidecar):
    bot = await live_desktop(user_a)

    await approved(db, bot, {"url": "https://shop.test/"}, action="browser_navigate")

    assert [action for action, _ in sidecar.sent] == ["browser_navigate"]


async def test_an_unapproved_call_is_not_re_resolved(db, user_a, live_desktop, sidecar):
    """Mid-loop, the pinned snapshot is fresh and the stale-ref guard is useful.

    Re-resolving there would spend a snapshot on every step and throw away the
    one check that catches a page changing under the model's feet.
    """
    bot = await live_desktop(user_a)

    await simulation.perform(
        db,
        simulation.Effect(
            kind="desktop", bot_id=bot.id, action="browser_hover", input_data=held()
        ),
    )

    assert [action for action, _ in sidecar.sent] == ["browser_hover"]
    assert sidecar.sent[0][1]["snapshot_id"] == "s1"


# ---------------------------------------------------------------------------
# Through the approval service, which is how it actually happens
# ---------------------------------------------------------------------------


async def test_the_whole_approval_path_re_resolves(db, user_a, live_desktop, sidecar):
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [
        snapshot_reply('e31 button "Delete account"', snapshot_id="s55")
    ]
    sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e31", "role": "button", "name": "Delete account"}
    ]
    approval = Approval(
        bot_id=bot.id,
        risk="delete",
        title="Desktop action: browser_click",
        summary='wants to run browser_click on button "Delete account"',
        payload={"kind": "desktop_steps", "steps": [{"action": "browser_click", **held()}]},
        status="approved",
    )
    db.add(approval)
    await db.flush()

    outcome = await approvals_service.execute_approved(db, approval, user_a)

    assert outcome["ok"], outcome
    click = next(payload for action, payload in sidecar.sent if action == "browser_click")
    assert click["ref"] == "e31"
    assert uuid.UUID(str(approval.id))


async def test_a_refused_re_resolution_is_reported_as_a_failed_execution(
    db, user_a, live_desktop, sidecar
):
    """An approved action that could not run must not read as one that did."""
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply("e1 button \"Something else\"")]
    approval = Approval(
        bot_id=bot.id,
        risk="delete",
        title="Desktop action: browser_click",
        summary="…",
        payload={"kind": "desktop_steps", "steps": [{"action": "browser_click", **held()}]},
        status="approved",
    )
    db.add(approval)
    await db.flush()

    outcome = await approvals_service.execute_approved(db, approval, user_a)

    assert outcome["ok"] is False
    assert outcome["results"][0]["error"] == B.APPROVED_ELEMENT_MISSING
    # …and the sentence the owner reads on the approval, not just the code.
    assert B.APPROVED_ELEMENT_MISSING in outcome["error"]
    assert 'button "Delete account"' in outcome["error"]


async def test_the_gate_stores_the_identity_the_execution_will_need(
    agent_with, db, user_a, make_thread, live_desktop, sidecar, varying_screens
):
    """The link between the two halves, driven through the real loop.

    The re-resolution can only work if the hold recorded what to re-resolve *by*.
    Everything above builds that payload by hand; this asserts the loop really
    writes it — the element's description, the page it was read off, and the tab
    it belonged to — into the approval a human will read.
    """
    from sqlalchemy import select

    from tests.services.conftest import acts, call, turn

    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot_reply()]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e4")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "close my account")

    approval = (await db.execute(select(Approval))).scalars().one()
    step = approval.payload["steps"][0]
    assert step["action"] == "browser_click"
    assert step[B.REF_LABEL_KEY] == 'button "Delete account"'
    assert step[B.REF_PAGE_KEY] == PAGE
    assert step[B.REF_TARGET_KEY] == "T1"
    # The human-facing summary still names the element, not the ref — and now
    # names it the way the reply names it. `button "Delete account"` was the
    # accessibility contract read out loud at a salesperson; the payload above
    # still carries it verbatim, which is what the re-resolution matches on.
    assert 'click "Delete account"' in approval.summary
    assert "e4" not in approval.summary
    assert "browser_click" not in approval.summary
    # And nothing was clicked.
    assert not any(action == "browser_click" for action, _ in sidecar.sent)
