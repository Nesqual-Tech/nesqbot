"""Standing approvals — *"don't ask again for this button"*.

The evidence this exists for is one row in the database: approval `5ad46fc5`,
approved, with **"don't ask again for this button"** typed into the note field.
Nothing read it. The next identical click asked again.

The owner chose the design, including the part I argued with — that a rule may
be acquired automatically, without being asked for. So this file is mostly not
about the happy path. It is about the five ways a standing permission could
become indefensible, each of which is a test with a name that says what would go
wrong:

* it learns from something that was **not** a human's explicit yes;
* it matches a **label** rather than an element on a page, so a page that
  renders an attacker-chosen "Message" button inherits somebody's consent;
* it **guesses** when the page is ambiguous, which is the one thing the whole
  DOM lane refuses to do;
* it learns **money or destruction**;
* it is acquired **silently**, or cannot be taken back.

The positive tests exist to prove the feature works at all. The negative ones
are the reason it is allowed to ship.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (
    ActionLog,
    Approval,
    AuditEvent,
    BotDesktop,
    Message,
    Run,
    StandingApproval,
)
from app.services import background, simulation, standing_approvals
from app.services import browser as B
from app.services.orchestrator import (
    RUN_AGENT_KEY,
    STANDING_GRANTED,
    TOOL_TASK_COMPLETE,
)
from tests.services.conftest import ScriptedToolRouter, acts, call, turn

#: The owner's actual morning: one lead's profile, one Message button.
LEAD = "https://www.linkedin.com/in/andrei-pop"
MESSAGE = 'button "Message"'
ASK = "don't ask again for this button"

#: The page as the sidecar renders it — one Message button and some noise.
PROFILE = (
    'e1 heading "Andrei Pop"\n'
    'e2 link "Contact info"\n'
    'e3 button "Message"\n'
    'e4 button "More"'
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def live_desktop(db, make_bot):
    async def _make(user, name="Lead Bot"):
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


def snapshot(
    body: str = PROFILE, *, url: str = LEAD, truncated: bool = False, snapshot_id: str = "s9"
) -> dict:
    return {
        "ok": True,
        "status": 200,
        "snapshot_id": snapshot_id,
        "target_id": "T1",
        "url": url,
        "title": "Andrei Pop | LinkedIn",
        "interactive_total": 4,
        "matched": 4,
        "returned": 4,
        "truncated": truncated,
        "frames": 1,
        "snapshot": body,
    }


def step(*, action: str = "browser_click", label: str = MESSAGE, url: str = LEAD, ref="e3") -> dict:
    """The held step exactly as the agent loop writes it.

    Not invented here: `test_approved_browser_actions.py::
    test_the_gate_stores_the_identity_the_execution_will_need` drives the real
    loop and asserts this shape, so a change to the loop breaks that test rather
    than silently making this one test a fiction.
    """
    return {
        "action": action,
        "ref": ref,
        "snapshot_id": "s3",
        B.REF_LABEL_KEY: label,
        B.REF_PAGE_KEY: url,
        B.REF_TARGET_KEY: "T1",
    }


def payload(**kwargs) -> dict:
    return {"kind": "desktop_steps", "steps": [step(**kwargs)], "thread_id": None}


async def decide(authed, approval, decision="approved", note=None) -> dict:
    """Decide, then wait for the continuation the route detached.

    The decide route claims the run and hands the agent loop to
    `services.background` rather than driving it inside the request — see the
    note there on the Approve button that span for minutes and left runs
    hanging. Anything a test asserts about what the bot *then said* has to wait
    for that task, and on this harness it also has to: the test session and the
    background one share a single asyncpg connection, which is not safe to use
    from two places at once.
    """
    body: dict = {"decision": decision}
    if note is not None:
        body["note"] = note
    response = await authed.post(f"/api/approvals/{approval.id}/decide", json=body)
    assert response.status_code == 200, response.text
    await background.drain()
    return response.json()


async def rules(db, *, live_only: bool = True) -> list[StandingApproval]:
    stmt = select(StandingApproval).order_by(StandingApproval.created_at)
    if live_only:
        stmt = stmt.where(StandingApproval.revoked_at.is_(None))
    return list((await db.execute(stmt)).scalars().all())


async def a_send_effect(db, bot, user, **kwargs):
    """One `send`-graded DOM click, straight down the chokepoint."""
    return await simulation.perform(
        db,
        simulation.Effect(
            kind="desktop",
            bot_id=bot.id,
            action="browser_click",
            input_data={k: v for k, v in step(**kwargs).items() if k != "action"},
            declared_risk="send",
            actor_user_id=user.id,
        ),
    )


async def a_live_rule(db, bot, user, **overrides) -> StandingApproval:
    """A rule written directly, for tests about *applying* one."""
    rule = StandingApproval(
        owner_user_id=user.id,
        bot_id=bot.id,
        action="browser_click",
        risk="send",
        ref_role="button",
        ref_name="Message",
        url_key=LEAD,
        origin="note",
        note_text=ASK,
        source_approval_ids=["11111111-1111-1111-1111-111111111111"],
        **overrides,
    )
    db.add(rule)
    await db.flush()
    return rule


# ---------------------------------------------------------------------------
# 1. Learning: only ever from a human's explicit yes
# ---------------------------------------------------------------------------


async def test_the_note_that_started_all_this_now_creates_a_rule(
    authed, db, user_a, live_desktop, sidecar
):
    """Approval `5ad46fc5`, replayed. The note is read this time.

    Everything about the rule is traceable off the row: the words that asked for
    it, the approval it came from, and what exactly it permits.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]
    approval = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(approval)
    await db.commit()

    await decide(authed, approval, note=ASK)

    rule = (await rules(db))[0]
    assert rule.origin == "note"
    assert rule.note_text == ASK
    assert rule.source_approval_ids == [str(approval.id)]
    assert (rule.action, rule.ref_role, rule.ref_name, rule.url_key) == (
        "browser_click", "button", "Message", LEAD,
    )
    assert rule.risk == "send"
    assert rule.revoked_at is None


async def test_a_rejection_never_creates_a_rule_however_it_is_worded(
    authed, db, user_a, live_desktop, sidecar
):
    """A refusal that happens to contain the magic words is still a refusal.

    The failure this stops is not hypothetical: the note field is one text box
    for both answers, and "no — and stop asking me about this" is a sentence a
    person would obviously write. Reading a *grant* out of a *refusal* would be
    the single worst bug this feature could have.
    """
    bot = await live_desktop(user_a)
    approval = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(approval)
    await db.commit()

    await decide(authed, approval, decision="rejected", note=f"no. {ASK}")

    assert await rules(db) == []


async def test_an_expired_approval_never_creates_a_rule(
    authed, db, user_a, live_desktop, sidecar
):
    """Nobody said yes. Nobody said anything."""
    bot = await live_desktop(user_a)
    approval = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(approval)
    await db.commit()

    response = await authed.post(f"/api/approvals/{approval.id}/expire")
    assert response.status_code == 200

    assert await rules(db) == []


async def test_an_approved_action_that_did_not_run_teaches_nothing(
    authed, db, user_a, live_desktop, sidecar
):
    """The subtle one, and the reason it is checked at all.

    A standing permission is only safe because the element it names can be
    proved unique on that page. An approved click that refused itself —
    `approved_element_missing` here — is precisely the statement that it could
    *not* be proved. Learning from that would mint a permission over an identity
    nothing has ever resolved.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot('e1 heading "Andrei Pop"')]
    approval = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(approval)
    await db.commit()

    body = await decide(authed, approval, note=ASK)

    assert body["execution"]["ok"] is False
    assert B.APPROVED_ELEMENT_MISSING in body["execution"]["error"]
    assert await rules(db) == []


@pytest.mark.parametrize("risk", ["spend", "delete"])
async def test_money_and_destruction_are_never_learned(
    authed, db, user_a, live_desktop, sidecar, risk
):
    """Asked for in writing, approved, executed — and still refused.

    Two independent refusals: `LEARNABLE_RISKS` here, and a CHECK constraint in
    `sql/init.sql` underneath it. The second one is the promise; the first is
    this week's policy.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]
    approval = Approval(
        bot_id=bot.id, risk=risk, title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(approval)
    await db.commit()

    await decide(authed, approval, note=ASK)

    assert await rules(db) == []
    assert risk in standing_approvals.NEVER_LEARNED


async def test_the_database_refuses_a_rule_with_no_traceable_origin(db, user_a, live_desktop):
    """A rule that cannot say why it exists is unwritable, not merely unwritten.

    The CHECK is the safeguard; the service is only the code that happens to
    respect it. If a future caller forgets, there is still no row.
    """
    from sqlalchemy.exc import IntegrityError

    bot = await live_desktop(user_a)
    db.add(
        StandingApproval(
            owner_user_id=user_a.id, bot_id=bot.id, action="browser_click", risk="send",
            ref_role="button", ref_name="Message", url_key=LEAD,
            origin="repetition", source_approval_ids=[],
        )
    )
    with pytest.raises(IntegrityError) as caught:
        await db.flush()
    assert "origin_is_traceable" in str(caught.value)
    await db.rollback()


async def test_the_database_refuses_a_spend_rule_even_if_the_service_is_wrong(
    db, user_a, live_desktop
):
    from sqlalchemy.exc import IntegrityError

    bot = await live_desktop(user_a)
    db.add(
        StandingApproval(
            owner_user_id=user_a.id, bot_id=bot.id, action="browser_click", risk="spend",
            ref_role="button", ref_name="Buy now", url_key=LEAD,
            origin="note", note_text=ASK, source_approval_ids=["x"],
        )
    )
    with pytest.raises(IntegrityError) as caught:
        await db.flush()
    assert "never_money_or_destruction" in str(caught.value)
    await db.rollback()


# ---------------------------------------------------------------------------
# 2. Learning by repetition — and the threshold
# ---------------------------------------------------------------------------


async def _approve_one(authed, db, bot, note=None) -> Approval:
    approval = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(approval)
    await db.commit()
    await decide(authed, approval, note=note)
    return approval


async def test_two_yeses_are_a_coincidence_and_three_are_a_habit(
    authed, db, user_a, live_desktop, sidecar
):
    """The threshold, stated as the behaviour it produces.

    One yes is consent to an instance. Two is what one task done twice looks
    like. Three separate approvals of the identical control on the identical
    page is the first point at which "they keep saying yes to this" describes
    behaviour rather than guessing at it. The rule that appears on the third
    names all three approvals, so the evidence is on the row.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]

    first = await _approve_one(authed, db, bot)
    assert await rules(db) == []
    second = await _approve_one(authed, db, bot)
    assert await rules(db) == [], "two is a coincidence"
    third = await _approve_one(authed, db, bot)

    rule = (await rules(db))[0]
    assert rule.origin == "repetition"
    assert rule.note_text == ""
    assert rule.source_approval_ids == [str(first.id), str(second.id), str(third.id)]
    assert standing_approvals.REPETITION_THRESHOLD == 3


async def test_a_refusal_in_the_middle_breaks_the_run(
    authed, db, user_a, live_desktop, sidecar
):
    """Yes, no, yes, yes is not three yeses.

    A refusal is evidence that the permission is *situational*, which is exactly
    the claim an automatic rule contradicts. So counting starts again, and it
    takes three more.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]

    await _approve_one(authed, db, bot)
    refused = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(refused)
    await db.commit()
    await decide(authed, refused, decision="rejected")

    await _approve_one(authed, db, bot)
    await _approve_one(authed, db, bot)
    assert await rules(db) == [], "the refusal is inside the window of three"

    await _approve_one(authed, db, bot)
    assert len(await rules(db)) == 1, "three clean yeses after the refusal"


async def test_repetition_is_counted_per_page_not_per_label(
    authed, db, user_a, live_desktop, sidecar
):
    """Three Message buttons on three leads' profiles is not three yeses.

    This is the negative control that matters most for the automatic trigger: a
    lead-generation run sees the *same label* on every profile it opens, so
    counting by label alone would mint a standing permission after three leads —
    a permission over a control the person has never seen twice.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]
    for lead in ("andrei-pop", "maria-ionescu", "radu-stan"):
        url = f"https://www.linkedin.com/in/{lead}"
        sidecar.replies["browser_snapshot"] = [snapshot(url=url)]
        approval = Approval(
            bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
            payload=payload(url=url), status="pending",
        )
        db.add(approval)
        await db.commit()
        await decide(authed, approval)

    assert await rules(db) == []


async def test_a_query_string_is_not_a_different_page(
    authed, db, user_a, live_desktop, sidecar
):
    """`?trk=nav` is tracking, not identity — the same comparison `_same_page` makes.

    The other half of the test above. If the query counted, a real run would
    never reach three, because LinkedIn appends a different tracking parameter
    every time.
    """
    bot = await live_desktop(user_a)
    for trk in ("?trk=a", "?trk=b", "#top"):
        sidecar.replies["browser_snapshot"] = [snapshot(url=LEAD + trk)]
        approval = Approval(
            bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
            payload=payload(url=LEAD + trk), status="pending",
        )
        db.add(approval)
        await db.commit()
        await decide(authed, approval)

    assert len(await rules(db)) == 1
    assert (await rules(db))[0].url_key == LEAD


async def test_one_persons_yeses_do_not_count_towards_anothers(
    authed, other, db, user_a, user_b, make_bot, sidecar
):
    """A shared system bot must not pool two people's consent.

    Both users can see and decide approvals on a bot they share. Their yeses are
    theirs, and three yeses from two people is not a habit — it is two people
    each answering a question, and neither of them has said "stop asking me".
    """
    from app.models import Bot

    shared = (
        await db.execute(select(Bot).where(Bot.is_system.is_(True)).limit(1))
    ).scalars().one()
    db.add(BotDesktop(bot_id=shared.id, state="running", control_url="http://desktop.test:7910"))
    await db.flush()
    sidecar.replies["browser_snapshot"] = [snapshot()]

    for client in (authed, other, authed):
        approval = Approval(
            bot_id=shared.id, risk="send", title="Click \"Message\"", summary="",
            payload=payload(), status="pending",
        )
        db.add(approval)
        await db.commit()
        await decide(client, approval)

    assert await rules(db) == [], "two of those three yeses were a different person's"


# ---------------------------------------------------------------------------
# 3. Applying a rule at the gate
# ---------------------------------------------------------------------------


async def test_a_covered_send_goes_through_the_gate_and_is_recorded_as_such(
    db, user_a, live_desktop, sidecar
):
    """The feature, and the whole audit trail it leaves behind.

    Note what is *not* claimed: the gate did not stop, and everything else about
    the step is unchanged. It is still classified `send`, it still goes through
    `_execute`, it still lands in the undo log — and that row now names the
    permission that let it past, which is the answer to the first question an
    audit asks about an unattended send.
    """
    bot = await live_desktop(user_a)
    rule = await a_live_rule(db, bot, user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]
    sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Message"}
    ]

    outcome = await a_send_effect(db, bot, user_a)

    assert outcome.gated is False
    assert outcome.risk == "send"
    assert outcome.ok is True

    logged = (await db.execute(select(ActionLog))).scalars().one()
    assert logged.risk == "send"
    assert logged.approval_id is None
    assert logged.standing_approval_id == rule.id

    applied = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "standing_approval_applied")
        )
    ).scalars().one()
    assert applied.detail["element"] == MESSAGE
    assert applied.detail["url"] == LEAD
    assert applied.detail["ok"] is True

    await db.refresh(rule)
    assert rule.use_count == 1
    assert rule.last_used_at is not None


async def test_no_rule_means_the_gate_still_stops(db, user_a, live_desktop, sidecar):
    """The control for everything above. Same effect, no rule, held."""
    bot = await live_desktop(user_a)

    outcome = await a_send_effect(db, bot, user_a)

    assert outcome.gated is True
    assert (await db.execute(select(ActionLog))).scalars().all() == []


async def test_a_whitelisted_name_on_another_page_does_not_match(
    db, user_a, live_desktop, sidecar
):
    """The attack this feature would otherwise open.

    A page the bot is steered onto renders `button "Message"`. The name is
    identical, Chrome computes the identical accessible name, and the rule does
    not apply — because the page is half the key and the host is part of the
    page. The step is held for a human exactly as it would have been with no
    rule at all.
    """
    bot = await live_desktop(user_a)
    await a_live_rule(db, bot, user_a)

    outcome = await a_send_effect(db, bot, user_a, url="https://evil.test/in/andrei-pop")

    assert outcome.gated is True
    assert not sidecar.sent, "nothing was even looked at, let alone clicked"


async def test_a_lookalike_host_does_not_match(db, user_a, live_desktop, sidecar):
    """`linkedin.com.evil.test` is `evil.test`, and string containment is not a check."""
    bot = await live_desktop(user_a)
    await a_live_rule(db, bot, user_a)

    outcome = await a_send_effect(
        db, bot, user_a, url="https://www.linkedin.com.evil.test/in/andrei-pop"
    )

    assert outcome.gated is True


async def test_another_users_rule_does_not_apply(db, user_a, user_b, live_desktop, sidecar):
    """Consent is a person's, not a bot's."""
    bot = await live_desktop(user_a)
    await a_live_rule(db, bot, user_b)

    outcome = await a_send_effect(db, bot, user_a)

    assert outcome.gated is True


async def test_an_effect_with_no_human_behind_it_matches_nothing(
    db, user_a, live_desktop, sidecar
):
    """A rule is one person's consent, so an unattributed effect inherits none.

    This is what keeps a background sweep, or any future caller that forgets to
    stamp an actor, from spending somebody's standing permission.
    """
    bot = await live_desktop(user_a)
    await a_live_rule(db, bot, user_a)

    outcome = await simulation.perform(
        db,
        simulation.Effect(
            kind="desktop",
            bot_id=bot.id,
            action="browser_click",
            input_data={k: v for k, v in step().items() if k != "action"},
            declared_risk="send",
        ),
    )

    assert outcome.gated is True


async def test_a_revoked_rule_stops_applying_immediately(
    authed, db, user_a, live_desktop, sidecar
):
    """One call, and the next step through the gate is held again.

    "Immediately" is structural: `revoked_at` is part of the partial unique
    index the lookup reads, so there is no cache and no window.
    """
    bot = await live_desktop(user_a)
    rule = await a_live_rule(db, bot, user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]
    sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Message"}
    ]
    assert (await a_send_effect(db, bot, user_a)).gated is False

    response = await authed.post(f"/api/standing-approvals/{rule.id}/revoke")
    assert response.status_code == 200
    assert response.json()["revoked_at"]

    assert (await a_send_effect(db, bot, user_a)).gated is True


async def test_a_rule_never_covers_a_different_action_on_the_same_element(
    db, user_a, live_desktop, sidecar
):
    """A permission to click Message is not a permission to type into it."""
    bot = await live_desktop(user_a)
    await a_live_rule(db, bot, user_a)

    outcome = await simulation.perform(
        db,
        simulation.Effect(
            kind="desktop",
            bot_id=bot.id,
            action="browser_type",
            input_data={
                "ref": "e3", B.REF_LABEL_KEY: MESSAGE, B.REF_PAGE_KEY: LEAD,
                "text": "hello", "submit": True,
            },
            declared_risk="send",
            actor_user_id=user_a.id,
        ),
    )

    assert outcome.gated is True


async def test_a_dry_run_says_a_standing_permission_would_open_the_gate(
    db, user_a, live_desktop, sidecar
):
    """The most consequential surface: a *scheduled* run nobody is watching.

    A routine step carries the triggering person's id, so a permission they
    granted applies to a routine of theirs on the same element and page. That is
    what "this element, this page, this bot, until revoked" means, and it is
    exactly the case where a reviewer needs to be told the truth in advance.

    So the lookup runs *before* the rehearsal branch in `perform`, not after: a
    dry run that reports "this would be held for a person" about a step that
    would sail through is wrong in the dangerous direction, because the reviewer
    concludes there is a human in the loop.
    """
    bot = await live_desktop(user_a)
    rule = await a_live_rule(db, bot, user_a)

    with simulation.SimulationContext(bot_id=bot.id) as sim:
        outcome = await a_send_effect(db, bot, user_a)

    assert outcome.simulated is True
    planned = sim.plan().calls[0]
    assert planned.risk == "send"
    assert planned.requires_approval is False, "it would not stop for a person"
    note = next(n for n in planned.notes if "standing permission" in n)
    assert MESSAGE in note
    assert LEAD in note
    assert "revoked from Standing permissions" in note
    # And a rehearsal spends nothing: no call went out, and the permission's
    # own counter did not move.
    assert not sidecar.sent
    await db.refresh(rule)
    assert rule.use_count == 0


# ---------------------------------------------------------------------------
# 4. A covered send still has to prove what it is about to touch
# ---------------------------------------------------------------------------
#
# This is the part that makes the automatic trigger survivable. The ordinary
# execution path trusts the ref the model just read and lets the sidecar refuse
# a stale one, which is right when somebody is watching. A standing permission
# means nobody is, so the element is re-derived from its recorded identity and
# the action refuses rather than guesses. All three refusals below come back
# from `browser.resolve_approved`, unchanged and already verified against real
# Chromium — reused, not reimplemented.


async def test_two_matching_elements_refuse_rather_than_guess(
    db, user_a, live_desktop, sidecar
):
    bot = await live_desktop(user_a)
    await a_live_rule(db, bot, user_a)
    sidecar.replies["browser_snapshot"] = [
        snapshot('e3 button "Message"\ne9 button "Message"')
    ]

    outcome = await a_send_effect(db, bot, user_a)

    assert outcome.ok is False
    assert outcome.result["error"] == B.APPROVED_ELEMENT_AMBIGUOUS
    assert "choosing between them is the guess this gate exists to prevent" in (
        outcome.result["detail"]
    )
    assert not any(action == "browser_click" for action, _ in sidecar.sent)


async def test_a_truncated_snapshot_cannot_prove_uniqueness_so_it_refuses(
    db, user_a, live_desktop, sidecar
):
    """One match in a snapshot that admits it is incomplete is not a unique match."""
    bot = await live_desktop(user_a)
    await a_live_rule(db, bot, user_a)
    sidecar.replies["browser_snapshot"] = [snapshot(truncated=True)]

    outcome = await a_send_effect(db, bot, user_a)

    assert outcome.ok is False
    assert outcome.result["error"] == B.APPROVED_ELEMENT_AMBIGUOUS
    assert "more elements than one snapshot can show" in outcome.result["detail"]
    assert not any(action == "browser_click" for action, _ in sidecar.sent)


async def test_a_tab_that_moved_between_the_match_and_the_click_refuses(
    db, user_a, live_desktop, sidecar
):
    """The rule matched on the payload's page; the tab is somewhere else now.

    Two independent checks have to agree before an unattended send lands: the
    recorded page matches the rule, and the *live* tab matches the recorded
    page. This is the second one failing.
    """
    bot = await live_desktop(user_a)
    await a_live_rule(db, bot, user_a)
    sidecar.replies["browser_snapshot"] = [snapshot(url="https://evil.test/in/andrei-pop")]

    outcome = await a_send_effect(db, bot, user_a)

    assert outcome.ok is False
    assert outcome.result["error"] == B.APPROVED_PAGE_CHANGED
    assert "evil.test" in outcome.result["detail"]
    assert not any(action == "browser_click" for action, _ in sidecar.sent)


# ---------------------------------------------------------------------------
# 5. Announced, listed, revocable
# ---------------------------------------------------------------------------


async def test_the_decision_that_creates_a_rule_says_so_in_its_own_answer(
    authed, db, user_a, live_desktop, sidecar
):
    """Not every approval has a parked run to carry a sentence into a reply.

    A routine-created hold has none, so "announced" cannot mean "announced when
    the architecture happens to allow it". The decision's own response carries
    it too.
    """
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]
    approval = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(approval)
    await db.commit()

    body = await decide(authed, approval, note=ASK)

    said = body["execution"]["standing_announcement"]
    assert said.startswith(STANDING_GRANTED)
    assert ASK in said
    assert 'click "Message"' in said
    assert "linkedin.com/in/andrei-pop" in said
    assert "Standing permissions" in said
    assert "spends money or deletes" in said
    granted = body["execution"]["standing_approval"]
    assert granted["permits"] == 'click "Message" on linkedin.com/in/andrei-pop'
    assert granted["origin"] == "note"


async def test_the_reply_of_the_turn_that_acquires_a_rule_announces_it(
    agent_with, authed, db, user_a, make_thread, live_desktop, sidecar,
    varying_screens, monkeypatch,
):
    """The whole safeguard, end to end, through the real loop.

    Silent acquisition of a standing permission is the indefensible version of
    this feature. So the reply of the turn that acquires one says what is now
    allowed, why it became allowed, and where to take it back — and it says it
    in the reply itself rather than hoping the model mentions it.
    """
    from app.routers import deps

    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]
    sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e3", "role": "button", "name": "Message"}
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e3", risk="send")),
        ]
    )
    thread = await make_thread(user_a, [bot])
    _frames, done = await turn(orchestrator, db, user_a, thread, "message this lead")

    approval = (await db.execute(select(Approval))).scalars().one()
    run = await db.get(Run, done["run_id"])
    assert run.detail[RUN_AGENT_KEY]["approval_id"] == str(approval.id)

    monkeypatch.setattr(
        deps.orchestrator,
        "router",
        ScriptedToolRouter([acts("", call(TOOL_TASK_COMPLETE, summary="Message sent."))]),
    )
    await decide(authed, approval, note=ASK)

    replies = (
        await db.execute(
            select(Message).where(Message.thread_id == thread.id, Message.role == "assistant")
        )
    ).scalars().all()
    latest = replies[-1].content
    assert STANDING_GRANTED in latest
    assert 'click "Message"' in latest
    assert "Standing permissions" in latest
    # And the result of the task still leads. The announcement is a note, not a
    # headline: what the person asked for is still the first thing they read.
    assert latest.startswith("Message sent.")


async def test_a_rule_is_only_announced_the_turn_it_is_acquired(
    authed, db, user_a, live_desktop, sidecar
):
    """Repeating it every time would train the reader to skip the sentence."""
    bot = await live_desktop(user_a)
    sidecar.replies["browser_snapshot"] = [snapshot()]
    first = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(first)
    await db.commit()
    assert "standing_announcement" in (await decide(authed, first, note=ASK))["execution"]

    second = Approval(
        bot_id=bot.id, risk="send", title="Click \"Message\"", summary="",
        payload=payload(), status="pending",
    )
    db.add(second)
    await db.commit()

    body = await decide(authed, second, note=ASK)
    assert "standing_announcement" not in body["execution"]
    assert len(await rules(db)) == 1


async def test_the_list_says_what_each_rule_permits_and_when_it_was_granted(
    authed, db, user_a, live_desktop
):
    bot = await live_desktop(user_a)
    rule = await a_live_rule(db, bot, user_a)
    await db.commit()

    response = await authed.get("/api/standing-approvals")
    assert response.status_code == 200
    body = response.json()

    item = next(row for row in body["items"] if row["id"] == str(rule.id))
    assert item["permits"] == 'click "Message" on linkedin.com/in/andrei-pop'
    assert item["place"] == "linkedin.com/in/andrei-pop"
    assert item["element"] == MESSAGE
    assert item["origin"] == "note"
    assert item["note"] == ASK
    assert item["granted_at"]
    assert item["used"] == 0
    # The limit is stated, not discovered.
    assert "spends money or deletes" in body["always_asks"]


async def test_the_list_is_one_persons_and_not_a_bots(
    authed, other, db, user_a, user_b, live_desktop
):
    bot = await live_desktop(user_a)
    mine = await a_live_rule(db, bot, user_a)
    theirs = await a_live_rule(db, bot, user_b)
    await db.commit()

    ids = {row["id"] for row in (await authed.get("/api/standing-approvals")).json()["items"]}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids


async def test_revoking_somebody_elses_rule_is_a_404_not_a_403(
    other, db, user_a, live_desktop
):
    """Existence stays private, exactly as everywhere else in this API."""
    bot = await live_desktop(user_a)
    rule = await a_live_rule(db, bot, user_a)
    await db.commit()

    response = await other.post(f"/api/standing-approvals/{rule.id}/revoke")
    assert response.status_code == 404

    await db.refresh(rule)
    assert rule.revoked_at is None


async def test_a_revoked_rule_is_kept_as_a_record_not_deleted(
    authed, db, user_a, live_desktop
):
    """"What was this bot allowed to do in March" has to stay answerable."""
    bot = await live_desktop(user_a)
    rule = await a_live_rule(db, bot, user_a)
    await db.commit()

    await authed.post(f"/api/standing-approvals/{rule.id}/revoke")

    assert await rules(db, live_only=True) == []
    kept = await rules(db, live_only=False)
    assert [r.id for r in kept] == [rule.id]
    assert kept[0].revoked_by == user_a.id

    listed = (await authed.get("/api/standing-approvals?include_revoked=true")).json()
    assert [row["id"] for row in listed["items"]] == [str(rule.id)]

    revoked = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "standing_approval_revoked")
        )
    ).scalars().one()
    assert revoked.detail["element"] == MESSAGE


async def test_revoking_twice_keeps_the_first_revocation(authed, db, user_a, live_desktop):
    bot = await live_desktop(user_a)
    rule = await a_live_rule(db, bot, user_a)
    await db.commit()

    first = (await authed.post(f"/api/standing-approvals/{rule.id}/revoke")).json()
    second = (await authed.post(f"/api/standing-approvals/{rule.id}/revoke")).json()

    assert first["revoked_at"] == second["revoked_at"]


# ---------------------------------------------------------------------------
# 6. The pieces, in isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("note", "asked"),
    [
        ("don't ask again for this button", True),
        ("dont ask me again", True),
        ("stop asking about this one", True),
        ("always approve this", True),
        ("nu mă mai întreba", True),
        ("looks good", False),
        ("", False),
        ("ask me again tomorrow", False),
        ("I had to ask again about the wording", False),
    ],
)
def test_only_an_actual_request_reads_as_one(note, asked):
    """Conservative, and the asymmetry is the point.

    A phrasing that is missed costs one more click and the person can say it
    again. A phrasing that is matched when it should not be costs a standing
    permission nobody granted.
    """
    assert standing_approvals.asks_to_stop_asking(note) is asked


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://www.linkedin.com/in/x?trk=nav#top", "https://www.linkedin.com/in/x"),
        ("http://a.test/b", "http://a.test/b"),
        ("about:blank", ""),
        ("file:///home/nesq/page.html", ""),
        ("", ""),
    ],
)
def test_only_a_real_web_page_can_be_the_subject_of_a_grant(url, key):
    assert standing_approvals.url_key(url) == key


@pytest.mark.parametrize(
    "action", sorted({"browser_hover", "browser_scroll", "browser_navigate", "click"})
)
def test_only_the_ops_whose_target_is_classified_can_be_learned(action):
    """A rule over `browser_scroll` would be a permission to move the page."""
    assert standing_approvals.identity_of(action, step()) is None


def test_an_element_with_no_name_is_not_something_a_person_can_point_at():
    assert standing_approvals.identity_of("browser_click", step(label="button")) is None


def test_a_step_with_no_recorded_label_records_no_identity():
    assert standing_approvals.identity_of("browser_click", {"ref": "e3"}) is None
