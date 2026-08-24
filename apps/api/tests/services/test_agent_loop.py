"""The agent loop: native tool calling, autonomy, and the human handoff.

The product failure this file is the regression suite for, verbatim from the
owner's session:

    User: connect to linkedin and start searching and messaging people
    Bot:  I can help research and draft outreach, but I can't actually send…
    User: ok, do it
    Bot:  I'm ready to start, but the desktop is currently absent…
    User: ready
    Bot:  I'm going to start by checking the desktop state and then open
          LinkedIn if needed.

Three turns, zero actions. The cause was the protocol: the model was asked to
append a fenced JSON directive to its prose, and a model that can narrate will
narrate. The fix is native function calling, and the properties that make it a
product rather than a demo are all asserted below.

* **It acts.** A scripted model driving real tools executes a multi-step task
  end to end without handing the turn back.
* **It owns its machine.** "The desktop is absent" produces a `start_desktop`
  and then the *original* task, in the same turn.
* **It stops for a human only where it must.** A gated action becomes an
  approval; an authentication wall becomes a persisted `awaiting_human` run with
  enough context to resume.
* **It resumes.** The same task, the same conversation, a fresh screenshot —
  owner-scoped, and idempotent under a double-click.
* **It cannot fake progress.** A model that narrates instead of acting is
  re-prompted once and then reported as having refused. Nothing in the reply is
  rendered from what the model said it would do.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import ActionLog, Approval, AuditEvent, BotDesktop, Message, Run
from app.services import simulation
from app.services.agent_work_items import TOOL_CREATE_WORK_ITEM
from app.services.model_router import (
    ChatResult,
    ModelRouter,
    parse_tool_calls,
    route_task,
)
from app.services.orchestrator import (
    AGENT_LOOP_TASK,
    ANNOUNCEMENT_PHRASES,
    BROWSER_ACTION_SCHEMAS,
    BROWSER_ACTIONS,
    CONTROL_TOOL_SCHEMAS,
    DESKTOP_ACTION_SCHEMAS,
    DESKTOP_ACTIONS,
    RUN_AGENT_KEY,
    RUN_AWAITING_HUMAN,
    TOOL_REQUEST_HUMAN_TAKEOVER,
    TOOL_START_DESKTOP,
    TOOL_STOP_DESKTOP,
    TOOL_TASK_COMPLETE,
    WORK_ITEM_TOOL_SCHEMAS,
    Orchestrator,
    agent_tool_names,
    agent_tools,
)

# The scripted-router harness lives in `tests/services/conftest.py` so both this
# suite and `test_agent_cost.py` can use it without importing each other's
# fixtures. `agent_with`, `agent_bot` and `varying_screens` are fixtures and
# arrive by name.
from tests.services.conftest import ScriptedToolRouter, actions_in, acts, call, says, turn

# ---------------------------------------------------------------------------
# 1. The tool surface
# ---------------------------------------------------------------------------


def test_every_desktop_action_is_offered_as_a_tool():
    """The vocabulary advertised and the vocabulary dispatched are one table.

    Stated against `agent_tool_names()` rather than against a hand-written union
    of the tables, because that function *is* the dispatch vocabulary — the loop
    answers "there is no tool called that" to anything outside it. Spelling the
    union out again here meant that adding a fifth surface produced a failure
    that could be silenced by adding a term, which is the opposite of what this
    invariant is for. The `<=` checks below keep each surface's membership
    explicit, so a table that vanished still fails.
    """
    offered = {t["function"]["name"] for t in agent_tools()}
    assert set(DESKTOP_ACTIONS) <= offered
    assert set(BROWSER_ACTIONS) <= offered
    assert set(CONTROL_TOOL_SCHEMAS) <= offered
    assert set(WORK_ITEM_TOOL_SCHEMAS) <= offered
    assert offered == agent_tool_names()


def test_every_desktop_action_has_a_parameter_schema():
    assert set(DESKTOP_ACTION_SCHEMAS) == set(DESKTOP_ACTIONS)
    assert set(BROWSER_ACTION_SCHEMAS) == set(BROWSER_ACTIONS)


def test_the_pixel_and_dom_vocabularies_cannot_collide():
    """`click` and `browser_click` are different things and must stay so.

    A model that confused them would be clicking a coordinate it never
    computed, or naming a ref to an API that has never heard of one.
    """
    assert not set(DESKTOP_ACTIONS) & set(BROWSER_ACTIONS)
    assert all(name.startswith("browser_") for name in BROWSER_ACTIONS)


def test_the_control_tools_the_brief_asked_for_exist():
    offered = {t["function"]["name"] for t in agent_tools()}
    for name in (
        TOOL_START_DESKTOP,
        TOOL_STOP_DESKTOP,
        TOOL_REQUEST_HUMAN_TAKEOVER,
        TOOL_TASK_COMPLETE,
        "screenshot",
    ):
        assert name in offered, name


@pytest.mark.parametrize("tool", agent_tools(), ids=lambda t: t["function"]["name"])
def test_each_tool_schema_is_well_formed(tool):
    function = tool["function"]
    assert tool["type"] == "function"
    assert function["description"].strip()
    params = function["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) <= set(params["properties"]), function["name"]
    for spec in params["properties"].values():
        assert spec.get("type"), function["name"]


def test_every_desktop_tool_can_declare_an_escalating_risk():
    """A primitive is named for the motion, not the consequence.

    `click` is `observe` whether it lands on a scrollbar or on Send, so the actor
    has to be able to say which one this is. The classifier still runs
    server-side and only ever escalates.
    """
    for tool in agent_tools():
        name = tool["function"]["name"]
        if name not in DESKTOP_ACTIONS:
            continue
        risk = tool["function"]["parameters"]["properties"].get("risk")
        assert risk is not None, name
        assert set(risk["enum"]) == {"mutate", "send", "spend", "delete"}


def test_the_tool_parameters_match_the_real_sidecar():
    """A property the sidecar rejects is a step that fails for no visible reason."""
    import ast
    from pathlib import Path

    sidecar = (
        Path(__file__).resolve().parents[3].parent
        / "infra"
        / "bot-desktop"
        / "sidecar"
        / "server.py"
    )
    if not sidecar.exists():  # pragma: no cover - the CI lane copies only apps/
        pytest.skip("infra/ is not on disk in this lane")

    tree = ast.parse(sidecar.read_text(encoding="utf-8"))
    fields: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "ActionIn":
            continue
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(
                statement.target, ast.Name
            ):
                fields.add(statement.target.id)
    assert fields, "could not read ActionIn out of the sidecar"

    for action, schema in DESKTOP_ACTION_SCHEMAS.items():
        if action in ("screenshot", "windows"):  # GET endpoints, not /action
            continue
        unknown = set(schema["properties"]) - fields
        assert not unknown, f"{action} offers {unknown}, which the sidecar has no field for"


def test_the_tools_are_attached_to_every_turn(agent_with, db, user_a, make_thread, agent_bot):
    """Tools the model is never given are tools the model can never call."""
    # (sync wrapper kept deliberately trivial; the assertion lives in the async body)


async def test_the_opening_model_call_is_given_the_tools(
    agent_with, db, user_a, make_thread, agent_bot
):
    orchestrator = agent_with([acts("", call(TOOL_TASK_COMPLETE, summary="Nothing to do."))])
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread, "hello")

    assert orchestrator.router.tools_seen[0], "the opening call was made without tools"
    names = {t["function"]["name"] for t in orchestrator.router.tools_seen[0]}
    # Which tools depends on whether there is a machine to run them on, and
    # this bot's desktop is cold. `services.context_budget` withholds the ones
    # that could only answer "no desktop" — 6,279 prompt tokens of schema to say
    # so — and keeps the one that changes that, plus the exit.
    #
    # `create_work_item` is here and is the only work-item tool that is: it
    # needs no machine, and the opening turn is where a bot decides there is
    # something worth writing down. The other three are gated on state this
    # thread does not have — no record exists to be found, none is in hand, and
    # there is nobody else on the thread — so they cost this request nothing.
    assert names == {
        TOOL_START_DESKTOP,
        TOOL_TASK_COMPLETE,
        TOOL_REQUEST_HUMAN_TAKEOVER,
        TOOL_CREATE_WORK_ITEM,
    }


async def test_a_warm_desktop_opens_with_the_whole_pixel_surface(
    agent_with, db, user_a, make_thread, agent_bot
):
    """The other half of the gate: with a machine up, the primitives are offered."""
    db.add(BotDesktop(bot_id=agent_bot.id, state="running", control_url="http://d.test:7910"))
    await db.flush()
    orchestrator = agent_with([acts("", call(TOOL_TASK_COMPLETE, summary="Nothing to do."))])
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread, "hello")

    names = {t["function"]["name"] for t in orchestrator.router.tools_seen[0]}
    assert "open_chromium" in names and TOOL_TASK_COMPLETE in names


# ---------------------------------------------------------------------------
# 2. Autonomy — the loop runs the task
# ---------------------------------------------------------------------------


async def test_a_multi_step_task_runs_to_completion_without_asking_the_user(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The headline. Five model turns, four real actions, one reply at the end."""
    orchestrator = agent_with(
        [
            acts("", call("screenshot")),
            acts("", call("open_chromium", text="https://example.test")),
            acts("", call("click", x=120, y=240)),
            acts("", call("type", text="hello there")),
            acts(
                "",
                call(
                    TOOL_TASK_COMPLETE,
                    summary="Opened example.test and typed a note into the page.",
                ),
            ),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "open example.test and type")

    assert actions_in(frames) == [
        "screenshot",
        "open_chromium",
        "click",
        "type",
    ]
    # One reply, at the end. Not one per step, and not a "shall I continue?".
    assert [name for name, _ in frames].count("done") == 1
    messages = (
        await db.execute(
            select(Message).where(Message.thread_id == thread.id, Message.role == "assistant")
        )
    ).scalars().all()
    assert len(messages) == 1

    assert "Opened example.test and typed a note into the page." in done["message"]
    # The step log is written in English now: `type(text='hello there')` was a
    # debugger's line printed at the person who asked for the work.
    assert 'Typed "hello there"' in done["message"]
    assert done["awaiting_human"] is False

    run = await db.get(Run, uuid.UUID(done["run_id"]))
    assert run.status == "completed"


async def test_the_loop_can_run_past_the_old_six_step_cap(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """Real web work is not six steps. The cap is a runaway guard, not a budget."""
    from app.services.orchestrator import DESKTOP_MAX_STEPS

    assert DESKTOP_MAX_STEPS > 6
    script = [acts("", call("click", x=i, y=i)) for i in range(1, 13)]
    script.append(acts("", call(TOOL_TASK_COMPLETE, summary="Clicked twelve times.")))
    orchestrator = agent_with(script)
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    assert len(actions_in(frames)) == 12
    assert "Clicked twelve times." in done["message"]


async def test_the_step_cap_is_configurable_and_still_bites(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, monkeypatch
):
    from app.services import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "DESKTOP_MAX_STEPS", 3)
    orchestrator = agent_with([], tail=acts("", call("click", x=1, y=1)))
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    assert len(actions_in(frames)) == 3
    assert "limit of 3 steps in one turn" in done["message"]


async def test_the_wall_clock_bounds_the_loop(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, monkeypatch
):
    """Budget and wall clock are the real bounds; the step cap is the backstop."""
    from app.services import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "DESKTOP_MAX_SECONDS", 0.0)

    orchestrator = agent_with([], tail=acts("", call("click", x=1, y=1)))
    thread = await make_thread(user_a, [agent_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    assert "ran out of time" in done["message"]


async def test_several_tool_calls_in_one_reply_all_run_and_all_get_answered(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """Chat completions rejects the next request if a call went unanswered."""
    orchestrator = agent_with(
        [
            acts("", call("screenshot", call_id="a"), call("windows", call_id="b")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Looked around.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, _ = await turn(orchestrator, db, user_a, thread)

    assert actions_in(frames) == ["screenshot", "windows"]
    convo = orchestrator.router.seen[-1]
    answered = {m["tool_call_id"] for m in convo if m.get("role") == "tool"}
    assert {"a", "b"} <= answered


# ---------------------------------------------------------------------------
# 3. The bot owns its machine
# ---------------------------------------------------------------------------


async def test_an_absent_desktop_is_started_and_the_task_continues_in_the_same_turn(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """"The desktop is absent" is a thing to fix, never an answer."""
    assert await db.get(BotDesktop, agent_bot.id) is None

    orchestrator = agent_with(
        [
            acts("", call("open_chromium", text="https://linkedin.test")),
            acts("", call(TOOL_TASK_COMPLETE, summary="LinkedIn is open.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(
        orchestrator, db, user_a, thread, "connect to linkedin and find leads"
    )

    phases: list[str] = []
    for phase in (d["phase"] for name, d in frames if name == "desktop"):
        if not phases or phases[-1] != phase:
            phases.append(phase)
    assert phases == ["starting", "ready", "finished"]

    desktop = await db.get(BotDesktop, agent_bot.id)
    assert desktop is not None and desktop.state == "running"

    # And crucially: the original task carried on. The turn did not end on
    # "your desktop is up now, shall I continue?".
    assert actions_in(frames) == ["open_chromium"]
    assert "Started my desktop" in done["message"]
    assert "Opened linkedin.test" in done["message"]
    assert "LinkedIn is open." in done["message"]


async def test_the_bot_can_start_and_stop_its_own_machine(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    orchestrator = agent_with(
        [
            acts("", call(TOOL_START_DESKTOP)),
            acts("", call(TOOL_STOP_DESKTOP)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Started it, then shut it down.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    assert actions_in(frames) == [TOOL_START_DESKTOP, TOOL_STOP_DESKTOP]
    desktop = await db.get(BotDesktop, agent_bot.id)
    assert desktop is not None and desktop.state == "absent"

    # Both lifecycle effects went through the one chokepoint, so both are in the
    # undo log alongside the clicks.
    logged = {
        entry.action
        for entry in (
            await db.execute(select(ActionLog).where(ActionLog.bot_id == agent_bot.id))
        ).scalars()
    }
    assert {TOOL_START_DESKTOP, TOOL_STOP_DESKTOP} <= logged
    assert "Shut down my desktop" in done["message"]


async def test_stopping_the_desktop_is_not_reported_as_a_blocked_step(db, agent_bot):
    """Its precondition is a *state* of the machine, which cannot be a 'problem'."""
    assessment = await simulation.assess(
        db,
        simulation.Effect(kind="desktop", bot_id=agent_bot.id, action="stop_desktop"),
    )
    assert assessment.problems == ()
    assert "no-op" in " ".join(assessment.notes)


async def test_a_rehearsed_stop_takes_nothing_down(db, agent_bot, make_user):
    user = await make_user()
    calls: list[str] = []

    async def _boom(*args, **kwargs):  # pragma: no cover - must never be reached
        calls.append("stop")
        raise AssertionError("a rehearsal stopped a real desktop")

    with simulation.SimulationContext(bot_id=agent_bot.id) as context:
        outcome = await simulation.perform(
            db,
            simulation.Effect(
                kind="desktop",
                bot_id=agent_bot.id,
                action="stop_desktop",
                actor_user_id=user.id,
            ),
        )

    assert calls == []
    assert outcome.simulated is True
    assert [c.action for c in context.calls] == ["stop_desktop"]


async def test_a_desktop_that_will_not_start_stops_the_task_honestly(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    class Dead:
        state = "error"
        last_error = "the container group could not be scheduled in swedencentral"
        control_url = None

    async def _fail(db_, bot):
        return Dead()

    monkeypatch.setattr(simulation._desktop, "start", _fail)
    orchestrator = agent_with(
        [acts("Opening LinkedIn.", call("open_chromium", text="https://linkedin.test"))]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    assert "could not start my desktop" in done["message"]
    assert "could not be scheduled in swedencentral" in done["message"]
    assert actions_in(frames) == []
    logged = await db.execute(select(ActionLog).where(ActionLog.action == "open_chromium"))
    assert list(logged.scalars().all()) == []


# ---------------------------------------------------------------------------
# 4. The gate still runs
# ---------------------------------------------------------------------------


async def test_a_gated_tool_call_becomes_an_approval_and_stops_the_loop(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    orchestrator = agent_with(
        [
            acts("", call("click", x=900, y=40, risk="send")),
            acts("", call("type", text="this must never run")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "click send")

    held = (
        await db.execute(select(Approval).where(Approval.bot_id == agent_bot.id))
    ).scalars().all()
    assert len(held) == 1
    assert held[0].risk == "send"
    assert held[0].payload["kind"] == "desktop_steps"
    assert held[0].payload["steps"] == [{"action": "click", "x": 900, "y": 40}]
    assert done["approval_id"] == str(held[0].id)

    assert "click" not in actions_in(frames)
    clicked = await db.execute(select(ActionLog).where(ActionLog.action == "click"))
    assert list(clicked.scalars().all()) == []
    # The reply now leads with the ask rather than with a tally of what ran, and
    # says why in the reader's terms — `browser_click classifies as 'send'` was
    # this service's risk table read out loud at a salesperson.
    assert done["message"].startswith("**Waiting on your go-ahead.**")
    assert "sends something out on your behalf" in done["message"]
    assert "classifies as" not in done["message"]
    # And nothing after it ran, nor was the model asked for more.
    typed = await db.execute(select(ActionLog).where(ActionLog.action == "type"))
    assert list(typed.scalars().all()) == []
    assert len(orchestrator.router.script) == 1


async def test_a_declared_risk_cannot_lower_the_classifier_through_a_tool_call(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    orchestrator = agent_with(
        [
            acts("", call("clipboard_set", text="x", risk="mutate")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Copied.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])
    await turn(orchestrator, db, user_a, thread)

    entry = (
        await db.execute(select(ActionLog).where(ActionLog.action == "clipboard_set"))
    ).scalar_one()
    assert entry.risk == "mutate"
    # The declared risk is not stored as the input, either: the recorded step is
    # the action the sidecar was asked for and nothing else.
    assert "risk" not in (entry.input_data or {})


# ---------------------------------------------------------------------------
# 5. Human handoff
# ---------------------------------------------------------------------------


async def take_over(agent_with, db, user_a, make_thread, agent_bot):
    """Drive a turn that ends in `awaiting_human`, and hand back the run."""
    orchestrator = agent_with(
        [
            acts("", call("open_chromium", text="https://linkedin.test")),
            acts(
                "",
                call(
                    TOOL_REQUEST_HUMAN_TAKEOVER,
                    reason="LinkedIn needs your password",
                    what_you_need="Sign in on the live screen, then press Continue.",
                ),
            ),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])
    frames, done = await turn(
        orchestrator, db, user_a, thread, "connect to linkedin and message people"
    )
    run = (
        await db.execute(select(Run).where(Run.thread_id == thread.id))
    ).scalars().first()
    return orchestrator, thread, run, frames, done


async def test_a_takeover_persists_the_run_with_enough_context_to_resume(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    _, thread, run, frames, done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )
    run_id, thread_id = run.id, thread.id

    # Not a flag in a process: a row that survives a restart. Everything the
    # session was holding is dropped first, so what is asserted below came back
    # out of Postgres rather than out of memory.
    db.expire_all()
    stored = await db.get(Run, run_id)
    assert stored.status == RUN_AWAITING_HUMAN
    assert stored.finished_at is None

    agent = stored.detail[RUN_AGENT_KEY]
    assert agent["state"] == RUN_AWAITING_HUMAN
    assert agent["goal"] == "connect to linkedin and message people"
    assert agent["takeover"]["reason"] == "LinkedIn needs your password"
    assert agent["takeover"]["what_you_need"].startswith("Sign in on the live screen")
    assert agent["takeover"]["asked_at"]
    assert agent["thread_id"] == str(thread_id)
    assert agent["resume_count"] == 0
    # What it was doing, not just that it stopped.
    assert [s["action"] for s in agent["steps"]] == ["start_desktop", "open_chromium"]
    assert agent["conversation"], "nothing was saved to resume from"

    # The banner the UI renders.
    takeovers = [d for name, d in frames if name == "takeover"]
    assert len(takeovers) == 1
    assert takeovers[0]["phase"] == "requested"
    assert takeovers[0]["reason"] == "LinkedIn needs your password"
    assert takeovers[0]["resume_url"] == f"/runs/{run_id}/resume"
    assert takeovers[0]["run_id"] == str(run_id)

    assert done["awaiting_human"] is True
    assert "I need you at the screen" in done["message"]
    assert "LinkedIn needs your password" in done["message"]


async def test_the_saved_conversation_carries_no_screenshots(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """A base64 PNG is ~1.4MB of characters and JSONB is not a blob store.

    Replaying a stale picture would be worse than useless anyway: the entire
    point of the resume is that a human changed the screen, so the resumed run
    takes a fresh one.
    """
    _, _thread, run, _frames, _done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )

    saved = json.dumps(run.detail[RUN_AGENT_KEY]["conversation"])
    assert "data:image/png;base64" not in saved
    assert "screenshot omitted" in saved
    assert len(saved) < 200_000
    assert all(isinstance(m["content"], str) for m in run.detail[RUN_AGENT_KEY]["conversation"])


async def test_a_takeover_is_audited(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    _, _thread, run, _frames, _done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )
    rows = await db.execute(
        select(AuditEvent).where(AuditEvent.event_type == "human_takeover_requested")
    )
    events = list(rows.scalars().all())
    assert len(events) == 1
    assert events[0].detail["run_id"] == str(run.id)
    assert events[0].detail["reason"] == "LinkedIn needs your password"
    assert events[0].actor_user_id == user_a.id


# ---------------------------------------------------------------------------
# 6. Resume
# ---------------------------------------------------------------------------


async def test_resume_continues_the_same_task_after_a_fresh_look_at_the_screen(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    _, thread, run, _frames, _done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )

    resumed = Orchestrator()
    resumed.router = ScriptedToolRouter(
        [
            acts("", call("type", text="software engineers in berlin")),
            acts("", call("key", keys=["Return"])),
            acts("", call(TOOL_TASK_COMPLETE, summary="Searched LinkedIn for the list.")),
        ]
    )

    out = await resumed.resume_run(db, user=user_a, run=run, note="I signed in for you.")

    assert out["resumed"] is True
    assert out["outcome"] == "completed"

    # It looked before it acted — the only honest way to know what the human did.
    first = resumed.router.seen[0]
    image_messages = [
        m
        for m in first
        if isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
    ]
    assert image_messages, "the resume acted without looking at the screen"

    # Working out what a person just did on a screen is a recover-shaped
    # decision, so the handback call is the one the loop lets reason normally
    # rather than the one it suppresses.
    from app.services.orchestrator import AGENT_EFFORT_RECOVER, AGENT_EFFORT_STEP

    assert resumed.router.efforts[0] == AGENT_EFFORT_RECOVER
    assert resumed.router.efforts[1:] == [AGENT_EFFORT_STEP] * (
        len(resumed.router.efforts) - 1
    )

    # Same task, same context: the goal and the earlier conversation came back.
    blob = json.dumps(first)
    assert "connect to linkedin and message people" in blob
    assert "pressed Continue" in blob
    assert "I signed in for you." in blob

    # And it carried on rather than starting over.
    assert "Searched LinkedIn for the list." in out["message"]
    assert 'Typed "software engineers in berlin"' in out["message"]

    run_id, thread_id = run.id, thread.id
    db.expire_all()
    stored = await db.get(Run, run_id)
    assert stored.status == "completed"
    assert stored.detail[RUN_AGENT_KEY]["resume_count"] == 1
    assert "takeover" not in stored.detail[RUN_AGENT_KEY]

    # The thread gained the resumed bot's reply, so a reader of the transcript
    # sees the whole story.
    replies = (
        await db.execute(
            select(Message).where(Message.thread_id == thread_id, Message.role == "assistant")
        )
    ).scalars().all()
    assert len(replies) == 2


async def test_a_resume_uses_the_current_prompt_not_the_one_it_parked_with(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """A run parked an hour ago must not resume on an hour-old system prompt.

    That is the same failure `seed_system`'s reconcile pass exists to stop: a
    prompt fix that reaches new work and never reaches old work looks like a
    prompt fix that did not work.
    """
    _, _thread, run, _frames, _done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )
    saved = run.detail[RUN_AGENT_KEY]["conversation"]
    assert all(m["role"] != "system" for m in saved), "a stale system prompt was stored"

    agent_bot.system_prompt = "You are a test bot. NEW INSTRUCTIONS LANDED."
    await db.commit()

    resumed = Orchestrator()
    resumed.router = ScriptedToolRouter(
        [acts("", call(TOOL_TASK_COMPLETE, summary="Carried on."))]
    )
    await resumed.resume_run(db, user=user_a, run=run)

    system = resumed.router.seen[0][0]
    assert system["role"] == "system"
    assert "NEW INSTRUCTIONS LANDED." in system["content"]
    assert "Your Bot Desktop" in system["content"]
    # And it was reminded of what it had already done, so it does not start over.
    assert "open_chromium" in json.dumps(resumed.router.seen[0])


async def test_a_resume_onto_a_dead_desktop_stops_instead_of_starting_a_fresh_one(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The point of a resume is the session the human just signed into.

    On the ACI driver a restart takes the filesystem with it, so a machine that
    died between the handoff and the button is a machine whose login is gone.
    Booting a fresh one would produce a bot working confidently on a signed-out
    browser — the exact class of confident wrongness this lane exists to remove.
    """
    _, _thread, run, _frames, _done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )
    desktop = await db.get(BotDesktop, agent_bot.id)
    desktop.state = "absent"
    desktop.control_url = None
    await db.commit()

    resumed = Orchestrator()
    resumed.router = ScriptedToolRouter(
        [acts("", call("type", text="this must not run"))]
    )
    out = await resumed.resume_run(db, user=user_a, run=run)

    assert out["outcome"] == "desktop_unavailable"
    assert "the session you signed into is gone" in out["message"]
    assert "start this from the beginning" in out["message"]
    assert resumed.router.calls_made == 0, "it asked the model to act on a machine that is gone"
    typed = await db.execute(select(ActionLog).where(ActionLog.action == "type"))
    assert list(typed.scalars().all()) == []
    started = await db.execute(select(ActionLog).where(ActionLog.action == "start_desktop"))
    assert len(list(started.scalars().all())) == 1, "a fresh machine was booted anyway"


async def test_a_resume_can_ask_for_the_human_again(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """MFA after a password is normal. The run parks again, resumable again."""
    _, _thread, run, _frames, _done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )

    resumed = Orchestrator()
    resumed.router = ScriptedToolRouter(
        [
            acts(
                "",
                call(
                    TOOL_REQUEST_HUMAN_TAKEOVER,
                    reason="LinkedIn is asking for the code from your phone",
                    what_you_need="Enter the six-digit code, then press Continue.",
                ),
            )
        ]
    )
    out = await resumed.resume_run(db, user=user_a, run=run)

    assert out["outcome"] == RUN_AWAITING_HUMAN
    run_id = run.id
    db.expire_all()
    stored = await db.get(Run, run_id)
    assert stored.status == RUN_AWAITING_HUMAN
    assert stored.detail[RUN_AGENT_KEY]["resume_count"] == 1
    assert "code from your phone" in stored.detail[RUN_AGENT_KEY]["takeover"]["reason"]


# ---------------------------------------------------------------------------
# 7. Resume over HTTP — authorisation and idempotency
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_api_router(monkeypatch):
    """Point the app's singleton orchestrator at a scripted router."""
    from app.routers import deps

    def _install(script, **kwargs) -> ScriptedToolRouter:
        router = ScriptedToolRouter(script, **kwargs)
        monkeypatch.setattr(deps.orchestrator, "router", router)
        return router

    return _install


@pytest.fixture
async def parked_run(db, user_a, make_thread, agent_bot):
    """An `awaiting_human` run, built the way a restart would find one.

    Constructed from the persisted shape rather than by running a turn, which is
    the point: if this is enough to resume from, the state really is durable.
    """
    thread = await make_thread(user_a, [agent_bot])
    run = Run(
        thread_id=thread.id,
        bot_id=agent_bot.id,
        status=RUN_AWAITING_HUMAN,
        detail={
            RUN_AGENT_KEY: {
                "state": RUN_AWAITING_HUMAN,
                "goal": "connect to linkedin and message people",
                "prose": "",
                "takeover": {
                    "reason": "LinkedIn needs your password",
                    "what_you_need": "Sign in, then press Continue.",
                    "asked_at": "2026-08-23T00:00:00+00:00",
                },
                "steps": [{"action": "open_chromium", "input": {}, "ok": True}],
                "notes": [],
                "conversation": [
                    {"role": "system", "content": "You are a test bot."},
                    {"role": "user", "content": "connect to linkedin and message people"},
                    {"role": "user", "content": "Tool result: open_chromium ran."},
                ],
                "cost_usd": 0.0,
                "resume_count": 0,
                "thread_id": str(thread.id),
                "bot_id": str(agent_bot.id),
            }
        },
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    # The desktop the human just signed into.
    db.add(
        BotDesktop(
            bot_id=agent_bot.id,
            state="running",
            control_url="http://desktop.test:8000",
        )
    )
    await db.commit()
    return thread, run


async def test_resume_is_owner_scoped(other, parked_run, stub_api_router):
    """A run belongs to the human behind it. Always 404, so existence stays private."""
    stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Done."))])
    _thread, run = parked_run

    response = await other.post(f"/api/runs/{run.id}/resume", json={})

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


async def test_resume_requires_authentication(anon, parked_run):
    _thread, run = parked_run
    response = await anon.post(f"/api/runs/{run.id}/resume", json={})
    assert response.status_code == 401


async def test_the_continue_button_resumes_the_run(authed, db, parked_run, stub_api_router):
    router = stub_api_router(
        [
            acts("", call("type", text="software engineers")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Searched and captured the list.")),
        ]
    )
    _thread, run = parked_run

    response = await authed.post(
        f"/api/runs/{run.id}/resume", json={"note": "signed in, all yours"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["resumed"] is True
    assert body["run_id"] == str(run.id)
    assert body["status"] == "completed"
    assert "Searched and captured the list." in body["message"]
    assert "signed in, all yours" in json.dumps(router.seen[0])


async def test_a_double_click_on_continue_does_not_start_a_second_loop(
    authed, db, parked_run, stub_api_router
):
    """Idempotency is the conditional UPDATE, not a hope about UI behaviour."""
    router = stub_api_router(
        [acts("", call(TOOL_TASK_COMPLETE, summary="Finished the search."))]
    )
    _thread, run = parked_run
    run_id = run.id

    first = await authed.post(f"/api/runs/{run_id}/resume", json={})
    calls_after_first = router.calls_made
    second = await authed.post(f"/api/runs/{run_id}/resume", json={})

    assert first.status_code == 200 and first.json()["resumed"] is True
    assert second.status_code == 200
    assert second.json()["resumed"] is False
    assert second.json()["status"] == "completed"
    assert "not waiting for a human" in second.json()["detail"]
    assert router.calls_made == calls_after_first, "the second press ran the loop again"

    db.expire_all()
    stored = await db.get(Run, run_id)
    assert stored.detail[RUN_AGENT_KEY]["resume_count"] == 1


async def test_resuming_a_run_that_never_asked_for_a_human_is_refused(
    authed, db, user_a, make_thread, agent_bot, make_run
):
    thread = await make_thread(user_a, [agent_bot])
    run = await make_run(thread, agent_bot, status="completed")

    response = await authed.post(f"/api/runs/{run.id}/resume", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "run_not_resumable"


# ---------------------------------------------------------------------------
# 8. Prose is never mistaken for progress
# ---------------------------------------------------------------------------


async def test_a_narrated_plan_is_re_prompted_and_then_the_task_actually_runs(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The owner's transcript, fixed: announce once, then act."""
    orchestrator = agent_with(
        [
            says("I'm going to start by checking the desktop state and then open LinkedIn."),
            acts("", call("open_chromium", text="https://linkedin.test")),
            acts("", call(TOOL_TASK_COMPLETE, summary="LinkedIn is open.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "ready")

    assert actions_in(frames) == ["open_chromium"]
    # The nudge went out once, and it escalated to the reasoning tier.
    nudged = [
        m
        for messages in orchestrator.router.seen
        for m in messages
        if m.get("role") == "user" and "Do not reply with prose" in str(m.get("content"))
    ]
    assert nudged, "the model narrated and was not asked again"
    assert orchestrator.router.tasks[1] == AGENT_LOOP_TASK
    assert "LinkedIn is open." in done["message"]


async def test_a_model_that_will_not_act_is_reported_as_refusing_not_as_planning(
    agent_with, db, user_a, make_thread, agent_bot
):
    """The one thing worse than not acting is a plan dressed up as progress."""
    orchestrator = agent_with(
        [
            says("I'm going to start by checking the desktop state."),
            says("Let me first confirm the browser is available."),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "ready")

    assert actions_in(frames) == []
    assert orchestrator.router.calls_made == 2, "exactly one second chance"
    assert "did not do it" in done["message"]
    assert "Nothing ran" in done["message"]
    # No machine was booted for a turn in which nothing happened.
    assert await db.get(BotDesktop, agent_bot.id) is None


async def test_prose_mid_task_is_re_prompted_and_the_refusal_is_stated(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    orchestrator = agent_with(
        [
            acts("", call("open_chromium", text="https://example.test")),
            says("Next, I'll search for the right person."),
            says("I'll go ahead and do that now."),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    assert actions_in(frames) == ["open_chromium"]
    assert "did not act when asked again" in done["message"]
    # The one thing that really happened is still reported, accurately.
    assert "Opened example.test" in done["message"]
    assert "search" not in done["message"].lower().split("---")[-1]


async def test_a_plain_answer_is_still_a_plain_answer(
    agent_with, db, user_a, make_thread, agent_bot
):
    """Not every turn is a task. `task_complete` on the opening call is an answer."""
    orchestrator = agent_with(
        [acts("", call(TOOL_TASK_COMPLETE, summary="Your budget is $8 a day."))]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "what is my budget?")

    assert done["message"] == "Your budget is $8 a day."
    assert [name for name, _ in frames if name == "desktop"] == []
    assert await db.get(BotDesktop, agent_bot.id) is None, "no machine booted to answer a question"


def test_the_announcement_phrases_are_only_used_to_ask_again():
    """A guard on the guard: this list must never reach an execution path.

    Deciding to re-prompt from a phrase list is cheap and safe — the worst case
    is one wasted model call. Deciding to *act* from one is how this codebase
    ended up reporting three outreach drafts it never wrote.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "app" / "services" / "orchestrator.py"
    ).read_text(encoding="utf-8")
    assert ANNOUNCEMENT_PHRASES
    # Exactly one caller: the `if` that decides whether to ask the model again.
    assert source.count("self._announces_action") == 1
    # And that call site reaches a re-prompt, never an executor.
    call_site = source.split("self._announces_action", 1)[1][:1500]
    assert "REPROMPT_FOR_ACTION" in call_site
    assert "simulation.perform" not in call_site
    assert "create_approval" not in call_site


# ---------------------------------------------------------------------------
# 9. The router really parses tool calls off the wire
# ---------------------------------------------------------------------------


def _function(name: str, arguments: str, call_id: str = "c1"):
    return SimpleNamespace(
        id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments)
    )


def test_tool_calls_are_parsed_off_a_completion():
    message = SimpleNamespace(
        content="", tool_calls=[_function("click", '{"x": 10, "y": 20}', "abc")]
    )
    calls = parse_tool_calls(message)
    assert len(calls) == 1
    assert calls[0].id == "abc"
    assert calls[0].name == "click"
    assert calls[0].arguments == {"x": 10, "y": 20}
    assert calls[0].parse_error is None


def test_unparseable_arguments_are_kept_and_flagged_not_silently_emptied():
    """Running a call with `{}` because its arguments failed to decode is a lie."""
    message = SimpleNamespace(content=None, tool_calls=[_function("type", "{not json")])
    call_ = parse_tool_calls(message)[0]
    assert call_.arguments == {}
    assert "not valid JSON" in (call_.parse_error or "")
    assert call_.raw_arguments == "{not json"


async def test_a_streamed_tool_call_is_reassembled_from_its_fragments():
    """A streamed call arrives as a name, then arguments a few characters at a time.

    A stream path that dropped them read as "the model said nothing", which made
    the whole agent loop unreachable from `POST /threads/{id}/messages/stream`.
    """

    def chunk(**delta_kwargs):
        return SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(**delta_kwargs))],
        )

    def fragment(index, *, call_id=None, name=None, arguments=None):
        return SimpleNamespace(
            index=index,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=arguments),
        )

    class FakeStream:
        def __aiter__(self):
            async def gen():
                yield chunk(content="Opening", tool_calls=None)
                yield chunk(content=None, tool_calls=[fragment(0, call_id="x1", name="click")])
                yield chunk(content=None, tool_calls=[fragment(0, arguments='{"x": 1')])
                yield chunk(content=None, tool_calls=[fragment(0, arguments=', "y": 2}')])

            return gen()

    class FakeCompletions:
        async def create(self, **kwargs):
            assert kwargs["stream"] is True
            assert kwargs["tools"]
            return FakeStream()

    router = ModelRouter()
    router._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    deltas = [
        d
        async for d in router.stream_chat(
            task="agent_turn", messages=[{"role": "user", "content": "go"}], tools=agent_tools()
        )
    ]

    assert deltas == ["Opening"]
    result = router.last_result
    assert result is not None
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("click", {"x": 1, "y": 2})]


def test_the_mock_router_never_invents_a_tool_call():
    """A mock that fabricated function calls would act on nobody's instruction."""
    router = ModelRouter()
    assert router.supports_tools is False


async def test_a_keyless_deployment_says_so_rather_than_pretending_to_act(
    db, user_a, make_thread, agent_bot
):
    """With no model behind it, the turn is a mock reply and no desktop is touched."""
    orchestrator = Orchestrator()
    thread = await make_thread(user_a, [agent_bot])

    _, done = await turn(orchestrator, db, user_a, thread, "open linkedin for me")

    assert "[mock:" in done["message"]
    assert await db.get(BotDesktop, agent_bot.id) is None


# ---------------------------------------------------------------------------
# 10. Spend
# ---------------------------------------------------------------------------


async def test_the_loop_runs_on_the_reasoning_tier_and_the_opening_turn_does_not(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """Driving a live UI from screenshots is the hard-reasoning case; chat is not.

    The escalation is paid for only once a task is actually in flight, which is
    the whole argument for it: `mini` opens the turn, and `sol` drives the loop.
    """
    orchestrator = agent_with(
        [
            acts("", call("click", x=1, y=1)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Clicked.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    assert orchestrator.router.tasks[0] == "agent_turn"
    assert route_task(orchestrator.router.tasks[0]) == "mini"
    assert orchestrator.router.tasks[1:] == [AGENT_LOOP_TASK]
    assert route_task(AGENT_LOOP_TASK) == "reason"


async def test_the_budget_stops_the_loop_rather_than_downgrading_it(
    agent_with, db, user_a, make_bot, make_thread, varying_screens
):
    """A cheaper model that misclicks is worse than an honest stop."""
    from app.models import CostLedger

    bot = await make_bot(
        user_a, name="Frugal", system_prompt="You are a test bot.", daily_budget_usd=0.01
    )
    db.add(
        CostLedger(
            bot_id=bot.id,
            tier="mini",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("0.009999"),
        )
    )
    await db.commit()

    orchestrator = agent_with([], tail=acts("", call("click", x=2, y=2)))
    thread = await make_thread(user_a, [bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    assert done.get("budget_blocked") is not True
    assert len(actions_in(frames)) == 1
    assert "daily budget" in done["message"]
    assert "$0.01 spent today against a $0.01 cap" in done["message"]
    assert "Looking at a screen costs a lot more" in done["message"]


async def test_the_budget_stop_reports_what_ran_before_it_says_it_stopped(
    agent_with, db, user_a, make_bot, make_thread, varying_screens
):
    """The order of the reply, when the cap is what ended the run.

    The reported session was 35 steps and a spent budget, and what came back
    led with the machinery. A run cut short still did something, and the person
    reading it needs that first — the reason it stopped is the footnote, not
    the headline.
    """
    from app.models import CostLedger

    bot = await make_bot(
        user_a, name="Frugal", system_prompt="You are a test bot.", daily_budget_usd=0.01
    )
    db.add(
        CostLedger(
            bot_id=bot.id,
            tier="mini",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("0.009999"),
        )
    )
    await db.commit()

    orchestrator = agent_with([], tail=acts("Searching for the login form.", call("click", x=2, y=2)))
    thread = await make_thread(user_a, [bot])

    _, done = await turn(orchestrator, db, user_a, thread)
    message = done["message"]

    assert message.startswith("Searching for the login form.")
    assert message.index("Searching for the login form.") < message.index("I stopped because")
    assert message.index("I stopped because") < message.index("**What I did")


async def test_a_cut_short_run_with_no_summary_still_leads_with_what_happened(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, monkeypatch
):
    """No `task_complete` means no summary of the bot's own, and no invention.

    The fallback headline is still read off steps that came back through the
    chokepoint, and it still refuses to write a result the bot never reached.
    What changed is *which* fact it reaches for. It used to open with a census —
    "I ran 4 steps on my desktop this turn: 4 completed. I did not reach a
    summary of my own" — whose first sentence is the least interesting thing
    about the turn and whose second describes this module's control flow to
    somebody who has never heard of it. It now names the last thing that
    actually worked, which is the most specific true statement available
    without paying a model for one, and answers the question the reader is
    actually holding: how far did it get?

    The counts did not disappear. They moved into the fold, which is the one
    place in the reply allowed to talk about how many of anything ran.
    """
    from app.services import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "DESKTOP_MAX_STEPS", 3)
    orchestrator = agent_with([], tail=acts("", call("click", x=1, y=1)))
    thread = await make_thread(user_a, [agent_bot])

    _, done = await turn(orchestrator, db, user_a, thread)
    message = done["message"]

    assert message.startswith(
        "I did not get to a result I can report — the last thing that worked was "
        "clicking at (1, 1)."
    )
    assert "did not reach a summary of my own" not in message
    assert message.index("I stopped because") < message.index("**What I did")
    # Four, not three: the boot the loop performed for itself is a step the bot
    # took and is counted like any other. It is counted in the fold now.
    assert "4 steps on the desktop" in message


async def test_the_turn_cost_covers_the_re_prompt_as_well(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """A second chance is a second bill, and the person paying for it sees it."""
    from app.models import CostLedger

    orchestrator = agent_with(
        [
            says("I'm going to start by opening the site."),
            acts("", call("click", x=1, y=1)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Clicked.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    ledger = (
        await db.execute(select(CostLedger).where(CostLedger.bot_id == agent_bot.id))
    ).scalars().all()
    assert len(ledger) == 3, "opening turn, re-prompt, and one follow-up"
    total = sum((entry.cost_usd for entry in ledger), Decimal("0"))
    assert abs(Decimal(str(done["cost_usd"])) - total) < Decimal("0.000005")


def test_chat_result_carries_tool_calls_without_breaking_its_shape():
    result = ChatResult("hi", "mini", 1, 1, Decimal("0"))
    assert result.tool_calls == []


# ---------------------------------------------------------------------------
# 11. Scrolling actually reaches the machine
# ---------------------------------------------------------------------------
#
# The sidecar has read `body.direction` and `body.amount` since it was written
# and the API body had nowhere to put them, so every scroll on every path
# collapsed to "down, 3 clicks" before it left the API. Reading a results page
# is mostly scrolling, and a loop that can only nudge three clicks at a time
# spends its whole step budget getting nowhere.


def test_the_desktop_action_body_carries_a_scroll_direction_and_amount():
    from app.schemas import DesktopActionIn

    body = DesktopActionIn(action="scroll", x=640, y=400, direction="up", amount=12)
    assert body.model_dump(exclude_none=True) == {
        "action": "scroll",
        "x": 640,
        "y": 400,
        "keys": [],
        "direction": "up",
        "amount": 12,
    }


def test_an_unset_scroll_field_is_not_forwarded_so_the_sidecar_default_stands():
    from app.schemas import DesktopActionIn

    dumped = DesktopActionIn(action="click", x=1, y=2).model_dump(exclude_none=True)
    assert "direction" not in dumped and "amount" not in dumped


@pytest.mark.parametrize("bad", [{"amount": 0}, {"amount": 10_000}, {"direction": "sideways"}])
def test_an_out_of_range_scroll_is_rejected_rather_than_sent(bad):
    """A model asking for ten thousand clicks is a bug, not an instruction."""
    import pydantic

    from app.schemas import DesktopActionIn

    with pytest.raises(pydantic.ValidationError):
        DesktopActionIn(action="scroll", x=1, y=1, **bad)


async def test_the_scroll_fields_survive_the_http_path_to_the_sidecar(
    authed, db, user_a, bot_a, monkeypatch
):
    sent: dict = {}

    async def _capture(db_, bot_id, action, payload):
        sent.update({"action": action, "payload": dict(payload)})
        return {"ok": True, "action": action}

    from app.routers import deps

    monkeypatch.setattr(deps.desktop_mgr, "computer_action", _capture)

    response = await authed.post(
        f"/api/bots/{bot_a.id}/desktop/action",
        json={"action": "scroll", "x": 640, "y": 400, "direction": "up", "amount": 12},
    )

    assert response.status_code == 200
    assert sent["action"] == "scroll"
    assert sent["payload"]["direction"] == "up"
    assert sent["payload"]["amount"] == 12


async def test_the_agent_loop_can_scroll_a_long_way_in_one_step(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, monkeypatch
):
    sent: list[dict] = []
    real = simulation._desktop.computer_action

    async def _capture(db_, bot_id, action, payload):
        sent.append({"action": action, **payload})
        return await real(db_, bot_id, action, payload)

    monkeypatch.setattr(simulation._desktop, "computer_action", _capture)

    orchestrator = agent_with(
        [
            acts("", call("scroll", x=640, y=400, direction="down", amount=25)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Read the whole results page.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    scrolls = [s for s in sent if s["action"] == "scroll"]
    assert scrolls == [
        {"action": "scroll", "x": 640, "y": 400, "direction": "down", "amount": 25}
    ]
    # The reply says which way it scrolled and no longer says how far. `amount`
    # is a count of wheel notches — it is the number the *sidecar* needs, which
    # is why the assertion above pins it exactly, and it means nothing to the
    # person reading what their bot did.
    assert "Scrolled down" in done["message"]
    assert "amount" not in done["message"]


def test_nothing_in_the_api_assumes_a_fixed_desktop_resolution():
    """Screen size is a property of the machine, read off each screenshot.

    The desktop image moved from 1440x900 to 1280x800 to cut ~11% of the pixels
    off every step's image bill; an API that had baked the old numbers in would
    have started mis-costing and mis-clicking on the day that landed.
    """
    from pathlib import Path

    api = Path(__file__).resolve().parents[2] / "app"
    for path in api.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for baked in ("1440x900", "1440, 900"):
            assert baked not in text, f"{path.name} hardcodes {baked}"


def test_the_step_cap_is_high_enough_to_finish_a_real_task():
    """40 was a guarantee the work never finished.

    A lead-generation run spent all forty steps on a single prospect — sign in,
    search, open the profile, open the company page, cross-check the site on
    Google, Bing and Instagram — and stopped before writing one message, against
    a brief asking for a hundred messages a day. The cap is a runaway guard; the
    bound that should bite is `daily_budget_usd`, which is checked every
    iteration and is an actual measure of cost, unlike a step count.

    Pinned as a floor, not an exact value, so tuning it stays free.
    """
    from app.services.orchestrator import DESKTOP_MAX_SECONDS, DESKTOP_MAX_STEPS

    assert DESKTOP_MAX_STEPS >= 200, (
        f"DESKTOP_MAX_STEPS={DESKTOP_MAX_STEPS} cannot diagnose and message even a "
        "handful of prospects in one turn"
    )
    # Raising one without the other only moves the premature stop to the other bound.
    assert DESKTOP_MAX_SECONDS >= 6.0 * DESKTOP_MAX_STEPS, (
        f"DESKTOP_MAX_SECONDS={DESKTOP_MAX_SECONDS} gives under 6s per step at the "
        f"cap of {DESKTOP_MAX_STEPS}, so the clock stops the run before the steps do"
    )


def test_navigation_clicks_are_not_advertised_as_something_to_declare():
    """The `risk` field on a DOM click invited a false positive that parked the run.

    A model declared `send` on `browser_click` for a LinkedIn *profile link* —
    pure navigation — and because a declared risk is escalate-only, the whole
    task stopped and waited for a human who had nothing to approve. The label
    classifier itself was innocent: it reads that name as `observe`.
    """
    from app.services.orchestrator import _BROWSER_RISK_PROPERTY
    from app.services.risk import classify_label_risk

    description = _BROWSER_RISK_PROPERTY["risk"]["description"].lower()
    assert "not" in description, "nothing tells the model when NOT to declare"
    for navigation in ("profile", "link", "filter"):
        assert navigation in description, f"navigation case not named: {navigation}"
    # And it has to stay cheap: this description rides on every declarable DOM
    # tool, on every model call. A verbose version cost ~1,100 tokens a request.
    assert len(description) < 200, f"description is {len(description)} chars a tool"

    # The evidence: the real label from the run that stalled.
    assert (
        classify_label_risk("Paula H. • 3rd+ Co-Owner Star Dental Clinic Timiş, Romania")
        == "observe"
    )
    # And the guard still bites where it should.
    assert classify_label_risk("Send message") == "send"


async def test_a_resume_carries_on_when_the_handback_photograph_fails(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, monkeypatch
):
    """A failed photograph is not a failed desktop.

    Reported from production: a person finished a LinkedIn login, handed the
    screen back, and the run ended with `decode failed: broken PNG file (chunk
    b'\x00\x00\x00\x00')` and "Nothing further ran" — the work abandoned at the
    exact moment it was being given back the session it had asked for.

    The machine is alive; only the picture failed. The DOM is a better way to
    read a page than a photograph of it anyway, so the loop must carry on and
    tell the model to look for itself.
    """
    from app.services import simulation as simulation_module

    _, _thread, run, _frames, _done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )

    # Fail only the handback frame; the loop's own screenshots still work, so
    # this proves the resume survives rather than that screenshots are optional.
    first = {"done": False}
    real = simulation_module._desktop.screenshot

    async def flaky(db_, bot_id, **options):
        if not first["done"]:
            first["done"] = True
            return {"ok": False, "error": "decode failed: broken PNG file"}
        return await real(db_, bot_id, **options)

    monkeypatch.setattr(simulation_module._desktop, "screenshot", flaky)

    resumed = Orchestrator()
    resumed.router = ScriptedToolRouter(
        [acts("", call(TOOL_TASK_COMPLETE, summary="Carried on without the opening frame."))]
    )
    out = await resumed.resume_run(db, user=user_a, run=run)

    assert out["outcome"] != "desktop_unavailable", (
        f"a broken photograph ended the run again: {out.get('message')}"
    )
    assert resumed.router.calls_made >= 1, "the model was never asked to carry on"
    assert "decode failed" in out["message"], "the failure was hidden rather than explained"
