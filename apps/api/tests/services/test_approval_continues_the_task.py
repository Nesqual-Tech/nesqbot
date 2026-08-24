"""Deciding a held action is one step of a task, not the end of it.

The owner's report was three words long: *"whenever i reject or approve, the
agent stops."* A thirty-six step run reached "click Send", the gate held it, the
person pressed Approve, that one click ran — and the task ended there, leaving
them to re-drive everything that led up to it. A gate that costs you the task is
a gate people learn to route around, which is the same way the whole feature
fails as the `409` did.

The takeover flow already solved this exact problem: park the run with enough
state to continue, and pick it up when the human is done. A decision is the same
shape of pause, so it reuses the same machinery — the persisted `runs.detail`,
the conversation rebuild, the conditional-`UPDATE` claim, the owner scoping.

Both answers continue. Approved carries what the execution *actually did*,
including an approved action that honestly did not run. Refused is told plainly
that a person said no, and is expected to take a different route or stop and say
what is left undone — never to go looking for a way round the gate.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models import Approval, AuditEvent, BotDesktop, Message, Run
from app.services import browser as B
from app.services.orchestrator import (
    RUN_AGENT_KEY,
    RUN_AWAITING_APPROVAL,
    TOOL_TASK_COMPLETE,
    Orchestrator,
)
from tests.services.conftest import ScriptedToolRouter, acts, call, turn


@pytest.fixture
def stub_api_router(monkeypatch):
    """Install a scripted router on the process-wide orchestrator the API uses."""
    from app.routers import deps

    def _install(script, **kwargs) -> ScriptedToolRouter:
        router = ScriptedToolRouter(script, **kwargs)
        monkeypatch.setattr(deps.orchestrator, "router", router)
        return router

    return _install


@pytest.fixture
async def desktop_bot(db, make_bot, user_a):
    bot = await make_bot(
        user_a,
        name="Agent",
        system_prompt="You are a test bot. You file expenses.",
        daily_budget_usd=500.0,
    )
    db.add(BotDesktop(bot_id=bot.id, state="running", control_url="http://desktop.test:7910"))
    await db.flush()
    return bot


async def hold_a_step(agent_with, db, user_a, make_thread, bot):
    """Drive a turn that gets a step held, and hand back the parked run."""
    orchestrator = agent_with(
        [
            acts("", call("click", x=10, y=20)),
            acts("", call("click", x=900, y=40, risk="send")),
        ]
    )
    thread = await make_thread(user_a, [bot])
    _frames, done = await turn(orchestrator, db, user_a, thread, "send the invoice")
    approval = (await db.execute(select(Approval))).scalars().one()
    run = await db.get(Run, done["run_id"])
    return thread, run, approval


# ---------------------------------------------------------------------------
# Parking
# ---------------------------------------------------------------------------


async def test_a_held_step_parks_the_run_with_enough_to_continue(
    agent_with, db, user_a, make_thread, desktop_bot, varying_screens
):
    """Without this the gate is a dead end whatever the person answers."""
    _thread, run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )

    assert run.status == RUN_AWAITING_APPROVAL
    agent = run.detail[RUN_AGENT_KEY]
    assert agent["state"] == RUN_AWAITING_APPROVAL
    assert agent["approval_id"] == str(approval.id)
    assert agent["goal"] == "send the invoice"
    # The work already done, so the continuation does not start over.
    assert any(step["action"] == "click" for step in agent["steps"])
    assert agent["conversation"]
    # …and no stale system prompt travelled with it.
    assert all(m["role"] != "system" for m in agent["conversation"])

    parked = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "run_parked_for_approval")
        )
    ).scalars().all()
    assert len(parked) == 1
    assert parked[0].detail["approval_id"] == str(approval.id)


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------


async def test_approving_runs_the_action_and_then_the_task_carries_on(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router
):
    """The headline: the click happens *and* the run keeps going."""
    thread, run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    stub_api_router(
        [
            acts("", call("type", text="thanks")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Sent it and confirmed.")),
        ]
    )

    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )

    assert response.status_code == 200
    continuation = response.json()["execution"]["continuation"]
    assert continuation["continued"] is True
    assert continuation["outcome"] == "completed"

    run_id, thread_id = run.id, thread.id
    db.expire_all()
    stored = await db.get(Run, run_id)
    assert stored.status == "completed"
    # The hold is gone, so a later decision cannot look like it belongs here.
    assert "approval_id" not in stored.detail[RUN_AGENT_KEY]

    replies = (
        await db.execute(
            select(Message).where(Message.thread_id == thread_id, Message.role == "assistant")
        )
    ).scalars().all()
    assert any("Sent it and confirmed." in m.content for m in replies)


async def test_the_model_is_told_the_action_ran_and_not_to_repeat_it(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router
):
    _thread, _run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    router = stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Done."))])

    await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})

    blob = json.dumps(router.seen[0])
    assert "APPROVED" in blob
    assert "Do not repeat that action." in blob
    assert "send the invoice" in blob  # same task, not a fresh one
    # And a fresh look at the page before acting on any old reference.
    assert "browser_snapshot" in blob


async def test_an_approved_action_that_did_not_run_says_so(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router,
    monkeypatch,
):
    """The dangerous case: approved, but re-resolution honestly refused.

    A model that assumes its approved click landed builds everything after it on
    a fiction. This is the one thing the continuation must never smooth over.
    """
    _thread, _run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    approval.payload = {
        "kind": "desktop_steps",
        "steps": [
            {
                "action": "browser_click",
                "ref": "e4",
                B.REF_LABEL_KEY: 'button "Send invoice"',
                B.REF_PAGE_KEY: "https://shop.test/invoice",
            }
        ],
    }
    await db.commit()

    from app.services import simulation

    async def _call(_db, _bot_id, action, payload=None):
        if action == "browser_snapshot":
            return {
                "ok": True,
                "status": 200,
                "snapshot_id": "s2",
                "url": "https://shop.test/invoice",
                "snapshot": 'e1 button "Something else"',
                "truncated": False,
            }
        return {"ok": True, "status": 200}

    monkeypatch.setattr(simulation._desktop, "browser_call", _call)
    router = stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Could not send."))])

    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )

    assert response.json()["execution"]["ok"] is False
    blob = json.dumps(router.seen[0])
    assert "did NOT run" in blob
    assert B.APPROVED_ELEMENT_MISSING in blob


# ---------------------------------------------------------------------------
# Refusing
# ---------------------------------------------------------------------------


async def test_rejecting_runs_nothing_and_still_carries_on(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router
):
    thread, run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    router = stub_api_router(
        [acts("", call(TOOL_TASK_COMPLETE, summary="You said no, so I stopped there."))]
    )

    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "rejected"}
    )

    assert response.status_code == 200
    assert response.json()["execution"]["continuation"]["continued"] is True

    blob = json.dumps(router.seen[0])
    assert "REFUSED" in blob
    assert "did not run" in blob
    assert "not an error" in blob
    assert "task_complete" in blob

    run_id, thread_id = run.id, thread.id
    db.expire_all()
    stored = await db.get(Run, run_id)
    assert stored.status == "completed"
    replies = (
        await db.execute(
            select(Message).where(Message.thread_id == thread_id, Message.role == "assistant")
        )
    ).scalars().all()
    assert any("You said no" in m.content for m in replies)


# ---------------------------------------------------------------------------
# The edges that make it safe to leave switched on
# ---------------------------------------------------------------------------


async def test_a_second_decision_cannot_start_a_second_loop(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router
):
    """The claim is a conditional UPDATE, exactly as the resume button's is."""
    _thread, _run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Done."))])

    first = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )
    second = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "approval_not_pending"


async def test_an_approval_with_no_parked_run_decides_without_continuing(
    authed, make_approval, bot_a
):
    """A routine's approval has no agent run behind it. Nothing to continue."""
    approval = await make_approval(bot_a)

    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )

    assert response.status_code == 200
    assert "continuation" not in (response.json()["execution"] or {})


async def test_a_decision_about_a_different_hold_never_continues_a_run(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, make_approval
):
    """A run is parked on one specific approval, and only that one may wake it."""
    _thread, run, _held = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    other = await make_approval(desktop_bot, run=run)

    response = await authed.post(
        f"/api/approvals/{other.id}/decide", json={"decision": "approved"}
    )

    assert response.status_code == 200
    assert "continuation" not in (response.json()["execution"] or {})
    run_id = run.id
    db.expire_all()
    assert (await db.get(Run, run_id)).status == RUN_AWAITING_APPROVAL


async def test_a_continuation_that_hits_another_gate_parks_again(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router
):
    """Two holds on one task is normal, and the second must be resumable too."""
    _thread, run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    stub_api_router([acts("", call("click", x=5, y=5, risk="delete"))])

    await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})

    run_id = run.id
    db.expire_all()
    stored = await db.get(Run, run_id)
    assert stored.status == RUN_AWAITING_APPROVAL
    second = (
        await db.execute(select(Approval).where(Approval.status == "pending"))
    ).scalars().one()
    assert stored.detail[RUN_AGENT_KEY]["approval_id"] == str(second.id)


async def test_a_dead_desktop_stops_the_continuation_honestly(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router
):
    """No cold start. A fresh machine is not the machine the task was on."""
    thread, run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    thread_id = thread.id
    desktop = await db.get(BotDesktop, desktop_bot.id)
    desktop.state = "absent"
    await db.commit()
    stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="never reached"))])

    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )

    assert response.json()["execution"]["continuation"]["outcome"] == "desktop_unavailable"
    replies = (
        await db.execute(
            select(Message).where(Message.thread_id == thread_id, Message.role == "assistant")
        )
    ).scalars().all()
    assert any("no longer running" in m.content for m in replies)


async def test_the_continuation_is_audited(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router
):
    _thread, run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Done."))])

    await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})

    events = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "run_continued_after_decision"
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].detail["run_id"] == str(run.id)
    assert events[0].detail["phase"] == "approved"


async def test_a_takeover_still_resumes_as_a_takeover(
    agent_with, db, user_a, make_thread, desktop_bot, varying_screens
):
    """The two pauses share machinery; they must not become the same event."""
    orchestrator = agent_with(
        [
            acts("", call("open_chromium")),
            acts(
                "",
                call(
                    "request_human_takeover",
                    reason="It wants a password",
                    what_you_need="Sign in, then press Continue.",
                ),
            ),
        ]
    )
    thread = await make_thread(user_a, [desktop_bot])
    _frames, done = await turn(orchestrator, db, user_a, thread, "sign me in")
    run = await db.get(Run, done["run_id"])

    resumed = Orchestrator()
    resumed.router = ScriptedToolRouter([acts("", call(TOOL_TASK_COMPLETE, summary="Done."))])
    out = await resumed.resume_run(db, user=user_a, run=run, note="signed in")

    assert out["resumed"] is True
    blob = json.dumps(resumed.router.seen[0])
    assert "pressed Continue" in blob
    assert "signed in" in blob
    events = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "human_takeover_resumed")
        )
    ).scalars().all()
    assert len(events) == 1


async def test_the_model_is_told_which_element_the_approved_click_landed_on(
    agent_with, authed, db, user_a, make_thread, desktop_bot, varying_screens, stub_api_router,
    monkeypatch,
):
    """"It succeeded" is not the same as knowing what succeeded.

    An approved click is re-resolved by identity, so what ran is worth naming:
    the continuation reads the sidecar's own answer back to the model rather
    than a boolean.
    """
    _thread, _run, approval = await hold_a_step(
        agent_with, db, user_a, make_thread, desktop_bot
    )
    approval.payload = {
        "kind": "desktop_steps",
        "steps": [
            {
                "action": "browser_click",
                "ref": "e4",
                B.REF_LABEL_KEY: 'button "Send invoice"',
                B.REF_PAGE_KEY: "https://shop.test/invoice",
            }
        ],
    }
    await db.commit()

    from app.services import simulation

    async def _call(_db, _bot_id, action, payload=None):
        if action == "browser_snapshot":
            return {
                "ok": True,
                "status": 200,
                "snapshot_id": "s2",
                "url": "https://shop.test/invoice",
                "snapshot": 'e8 button "Send invoice"',
                "truncated": False,
            }
        return {"ok": True, "status": 200, "ref": "e8", "role": "button", "name": "Send invoice"}

    monkeypatch.setattr(simulation._desktop, "browser_call", _call)
    router = stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Sent."))])

    await authed.post(f"/api/approvals/{approval.id}/decide", json={"decision": "approved"})

    blob = json.dumps(router.seen[0])
    assert 'browser_click ran on button \\"Send invoice\\"' in blob or (
        "browser_click ran on button" in blob and "Send invoice" in blob
    )
