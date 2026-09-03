"""Desktop agency — the bots know they have a computer, and can actually drive it.

Every bot in this product runs on a private Linux desktop with a browser on it,
and until now nothing told the model that. These tests hold the new
perception-action loop to the four properties that make it safe to ship:

* **One chokepoint.** Every desktop effect — the cold start and the observation
  screenshot included — goes through `simulation.perform`, so the risk gate, the
  approval flow and the undo log apply without a second copy of any of them.
* **Bounded.** The loop stops on its step cap, on a screen that stops changing,
  and on the bot's daily budget.
* **Priced.** A screenshot is roughly a thousand prompt tokens. The cost ledger
  has to see them, or the budget silently under-counts a vision turn.
* **Honest.** A bot may only report an action it actually performed. When the
  desktop will not start, the answer says so and claims nothing.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import ActionLog, Approval, CostLedger
from app.services import simulation
from app.services.model_router import (
    ChatResult,
    ModelRouter,
    count_image_tokens,
    count_text_tokens,
    estimate_cost_usd,
    estimate_image_tokens,
    image_content_part,
    message_text,
    png_dimensions,
    route_task,
)
from app.services.orchestrator import (
    DESKTOP_ACTIONS,
    DESKTOP_CAPABILITY,
    DESKTOP_DONE,
    DESKTOP_MAX_STEPS,
    DESKTOP_MAX_UNCHANGED_SCREENS,
    Orchestrator,
    desktop_protocol_block,
)

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def directive(action: str, **payload) -> str:
    """One `nesq_desktop` block, the way a model would emit it."""
    risk = payload.pop("risk", None)
    node: dict = {"action": action}
    if payload:
        node["input"] = payload
    if risk:
        node["risk"] = risk
    return "```json\n" + json.dumps({"nesq_desktop": node}) + "\n```"


class ScriptedRouter(ModelRouter):
    """A router whose replies are a script, with the real cost accounting kept.

    Only `content` is faked. Token counting, image pricing and `record_cost` are
    the production code paths, because the budget assertions below are only
    worth anything if they run against the real estimator.
    """

    def __init__(self, replies: list[str], *, tail: str = "Nothing further."):
        super().__init__()
        self.replies = list(replies)
        self.tail = tail
        #: The `messages` list handed to each call, in order.
        self.seen: list[list[dict]] = []
        #: The `reasoning_effort` each call asked for, in order. `None` means
        #: the caller sent none.
        self.efforts: list[str | None] = []

    async def chat(
        self,
        *,
        task,
        messages,
        tools=None,
        tool_choice=None,
        fail_count=0,
        reasoning_effort=None,
        bot=None,
    ) -> ChatResult:
        self.seen.append(messages)
        self.efforts.append(reasoning_effort)
        content = self.replies.pop(0) if self.replies else self.tail
        result = self._estimated_result(route_task(task, fail_count), messages, content)
        self.last_result = result
        return result

    async def stream_chat(
        self,
        *,
        task,
        messages,
        tools=None,
        tool_choice=None,
        fail_count=0,
        reasoning_effort=None,
        bot=None,
    ):
        result = await self.chat(
            task=task,
            messages=messages,
            tools=tools,
            fail_count=fail_count,
            reasoning_effort=reasoning_effort,
            bot=bot,
        )
        yield result.content


@pytest.fixture
def orchestrator_with():
    def _build(replies: list[str], **kwargs) -> Orchestrator:
        orchestrator = Orchestrator()
        orchestrator.router = ScriptedRouter(replies, **kwargs)
        return orchestrator

    return _build


async def turn(orchestrator: Orchestrator, db, user, thread, content: str = "do it"):
    """Drive one real streamed turn and hand back `(frames, done_payload)`."""
    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db, user=user, thread=thread, content=content
        )
    ]
    done = next((data for name, data in frames if name == "done"), {})
    return frames, done


@pytest.fixture
async def desk_bot(make_bot, user_a):
    """A bot whose own prompt says nothing whatsoever about a desktop."""
    return await make_bot(
        user_a,
        name="Desker",
        system_prompt="You are a test bot. You file expenses.",
        daily_budget_usd=50.0,
    )


@pytest.fixture
def varying_screens(monkeypatch):
    """Make each screenshot differ, so the stuck-UI detector stays out of the way."""
    from tests.services.screens import patch_varying_screens

    return patch_varying_screens(monkeypatch)


def step_log(reply: str) -> list[str]:
    """The numbered step lines out of a reply's "What I did" block.

    Markdown, not HTML: the desktop renderer has no raw-HTML path at all (the
    text is written by a model reading attacker-controlled pages), so a
    `<details>` block printed its tags on screen instead of folding.

    The step log stopped being the headline of a desktop reply — the user's
    verdict on the shipped one was *"it's telling me things, i don't care"* —
    so it now lives folded away under the prose. It is still exactly what ran,
    and these assertions still read it line for line.
    """
    if "**What I did" not in reply:
        return []
    body = reply.split("**What I did", 1)[1].split(chr(10), 1)[1]
    return [line for line in (raw.strip() for raw in body.splitlines()) if line]


def observation_text(message: dict) -> str:
    """Every text part of one observation message, joined.

    An observation is several content parts rather than one, so that
    `prune_screenshots` can swap out the sentence describing the image without
    touching the facts around it. Tests read the whole thing.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text", ""))
        for part in content or []
        if isinstance(part, dict) and part.get("type") == "text"
    )


# ---------------------------------------------------------------------------
# 1. The bots are told what they have
# ---------------------------------------------------------------------------


async def test_every_bot_is_told_about_its_desktop(orchestrator_with, db, user_a, make_thread, desk_bot):
    """The capability text is composed in, not copied into the bot's own prompt."""
    assert "desktop" not in desk_bot.system_prompt.lower()
    orchestrator = orchestrator_with(["Noted."])
    thread = await make_thread(user_a, [desk_bot])

    await turn(orchestrator, db, user_a, thread, "can you look at a website for me?")

    system = orchestrator.router.seen[0][0]
    assert system["role"] == "system"
    assert desk_bot.system_prompt in system["content"]
    for promise in (
        "your own computer",
        "Chromium browser",
        "It persists",
        "A human can take over",
        "held as an approval request",
        "report only what actually",
        "never report held or planned work as",
    ):
        assert promise in system["content"], promise


def test_the_capability_text_lives_in_exactly_one_place():
    """Bots differ by role, not by hardware — so five YAML copies is the bug."""
    from pathlib import Path

    bots_dir = Path(__file__).resolve().parents[3].parent / "bots"
    if not bots_dir.exists():  # pragma: no cover - some CI lanes copy only apps/
        pytest.skip("bots/ is not on disk in this lane")
    for path in sorted(bots_dir.glob("*.yaml")):
        prompt = path.read_text(encoding="utf-8").split("connectors:", 1)[0]
        assert "screenshot" not in prompt.lower(), f"{path.name} re-describes the desktop"
        assert "nesq_desktop" not in prompt, f"{path.name} re-describes the protocol"


def test_the_advertised_vocabulary_is_the_accepted_vocabulary():
    """The protocol block is generated from the table the loop validates against."""
    block = desktop_protocol_block()
    for action in DESKTOP_ACTIONS:
        assert f"- {action} —" in block
    assert f"- {DESKTOP_DONE} —" in block
    assert str(DESKTOP_MAX_STEPS) in block


def test_every_advertised_action_exists_on_the_real_sidecar():
    """A hint the sidecar rejects is a step that fails for no visible reason.

    `screenshot` and `windows` are GET endpoints rather than `/action` names, so
    they are checked separately; everything else must appear in the sidecar's
    own `ActionName` literal.
    """
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
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) or not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "ActionName" not in targets or not isinstance(node.value, ast.Subscript):
            continue
        for element in getattr(node.value.slice, "elts", []):
            if isinstance(element, ast.Constant):
                names.add(str(element.value))
    assert names, "could not read ActionName out of the sidecar"

    advertised = set(DESKTOP_ACTIONS) - {"screenshot", "windows"}
    assert advertised <= names, f"the prompt offers actions the sidecar has no branch for: {advertised - names}"


def test_the_capability_text_promises_nothing_the_loop_cannot_do():
    assert "30-90 seconds" in desktop_protocol_block()
    assert "Do not type credentials you were not given" in DESKTOP_CAPABILITY


# ---------------------------------------------------------------------------
# 2. A real perception-action loop, through the one chokepoint
# ---------------------------------------------------------------------------


async def test_a_bot_looks_at_its_screen_and_acts_on_what_it_sees(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    orchestrator = orchestrator_with(
        [
            "Let me look first.\n" + directive("screenshot"),
            "Opening the site.\n" + directive("open_chromium", text="https://example.test"),
            "That is done." + directive(DESKTOP_DONE),
        ]
    )
    thread = await make_thread(user_a, [desk_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "open example.test")

    tools = [data for name, data in frames if name == "tool"]
    assert [t["action"] for t in tools] == ["screenshot", "open_chromium"]
    assert all(t["connector"] == "desktop" for t in tools)
    assert all(t["ok"] for t in tools)

    # The screen actually reached the model as an image, not as a description.
    observation = orchestrator.router.seen[-1][-2]
    assert observation["role"] == "user"
    kinds = [part["type"] for part in observation["content"]]
    assert "image_url" in kinds
    image = next(p for p in observation["content"] if p["type"] == "image_url")
    assert image["image_url"]["url"].startswith("data:image/png;base64,")

    # The reply leads with what the bot achieved; the transcript is folded away
    # underneath it rather than being the first thing the user reads.
    assert done["message"].startswith("That is done.")
    # Sentences, not tool calls: a person who opens the fold because a step
    # went wrong used to find a stack frame in there.
    assert step_log(done["message"]) == [
        "1. Started my desktop",
        "2. Looked at the screen",
        "3. Opened example.test",
    ]


async def test_the_desktop_is_started_on_demand_and_the_user_is_told(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    """`state == "absent"` is not a reason to fail — it is a reason to boot."""
    from app.models import BotDesktop

    assert await db.get(BotDesktop, desk_bot.id) is None
    orchestrator = orchestrator_with(["Looking.\n" + directive("screenshot")])
    thread = await make_thread(user_a, [desk_bot])

    frames, _ = await turn(orchestrator, db, user_a, thread)

    phases = [data for name, data in frames if name == "desktop"]
    # A slow boot adds `starting` heartbeats; the mock desktop is instant, so
    # collapse repeats rather than assert on how many ticks happened to fire.
    seen: list[str] = []
    for phase in (p["phase"] for p in phases):
        if not seen or seen[-1] != phase:
            seen.append(phase)
    assert seen == ["starting", "ready", "finished"]
    assert "30-90 seconds" in phases[0]["detail"]
    desktop = await db.get(BotDesktop, desk_bot.id)
    assert desktop is not None and desktop.state == "running"


async def test_a_slow_cold_start_streams_progress_instead_of_going_quiet(
    orchestrator_with, db, user_a, make_thread, desk_bot, monkeypatch
):
    """90 seconds of silence is indistinguishable from a hung turn."""
    import asyncio

    from app.services import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "DESKTOP_BOOT_TICK_SECONDS", 0.01)
    real_start = simulation._desktop.start

    async def _slow(db_, bot):
        await asyncio.sleep(0.05)
        return await real_start(db_, bot)

    monkeypatch.setattr(simulation._desktop, "start", _slow)
    orchestrator = orchestrator_with(["Looking.\n" + directive("screenshot")])
    thread = await make_thread(user_a, [desk_bot])

    frames, _ = await turn(orchestrator, db, user_a, thread)

    ticks = [d for name, d in frames if name == "desktop" and "elapsed_seconds" in d]
    assert ticks, "the boot went quiet for its whole duration"
    assert ticks[0]["phase"] == "starting"
    assert "Still booting" in ticks[0]["detail"]
    assert [d["phase"] for name, d in frames if name == "desktop"][-1] == "finished"


async def test_every_desktop_effect_goes_through_the_chokepoint(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    """Start, action and observation all land in the undo log, from one path."""
    orchestrator = orchestrator_with(
        ["Clicking.\n" + directive("click", x=10, y=20), "Done." + directive(DESKTOP_DONE)]
    )
    thread = await make_thread(user_a, [desk_bot])

    await turn(orchestrator, db, user_a, thread)

    rows = await db.execute(select(ActionLog).where(ActionLog.bot_id == desk_bot.id))
    logged = [(e.kind, e.action) for e in rows.scalars().all()]
    # Compared as a multiset: three writes inside one turn can share a
    # `created_at` to the microsecond, so ordering by it is not stable. What is
    # under test is that all three reached the log at all — the boot, the action
    # and the observation, none of them on a private path around the gate.
    assert sorted(logged) == sorted(
        [("desktop", "start_desktop"), ("desktop", "click"), ("desktop", "screenshot")]
    )


async def test_every_desktop_step_writes_an_audit_row(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    """`docs/architecture.md` claims an audit row per desktop action."""
    from app.models import AuditEvent

    orchestrator = orchestrator_with(
        ["Clicking.\n" + directive("click", x=3, y=4), "Done." + directive(DESKTOP_DONE)]
    )
    thread = await make_thread(user_a, [desk_bot])

    await turn(orchestrator, db, user_a, thread)

    rows = await db.execute(
        select(AuditEvent).where(
            AuditEvent.bot_id == desk_bot.id, AuditEvent.event_type == "desktop_action"
        )
    )
    audited = list(rows.scalars().all())
    # The bot brought its own machine up before it could click, and that is a
    # desktop action a reader of the audit log needs to see: "who started this
    # container" is the first question asked about a bot that did something.
    assert [e.detail["action"] for e in audited] == ["start_desktop", "click"]
    assert all(e.actor_user_id == user_a.id for e in audited)
    assert all(e.detail["via"] == "chat_turn" for e in audited)
    assert all(e.detail["result_ok"] is True for e in audited)


async def test_a_held_step_is_audited_as_held(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    from app.models import AuditEvent

    orchestrator = orchestrator_with(["Sending.\n" + directive("click", x=1, y=2, risk="send")])
    thread = await make_thread(user_a, [desk_bot])
    await turn(orchestrator, db, user_a, thread)

    rows = await db.execute(
        select(AuditEvent).where(AuditEvent.event_type == "desktop_action_held")
    )
    held = list(rows.scalars().all())
    assert len(held) == 1
    assert held[0].detail["risk"] == "send"
    assert held[0].detail["approval_id"]


async def test_the_turn_cost_covers_every_model_call_the_loop_made(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    orchestrator = orchestrator_with(
        [
            "One.\n" + directive("click", x=1, y=1),
            "Two.\n" + directive("click", x=2, y=2),
            "Done." + directive(DESKTOP_DONE),
        ]
    )
    thread = await make_thread(user_a, [desk_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    rows = await db.execute(select(CostLedger).where(CostLedger.bot_id == desk_bot.id))
    ledger = list(rows.scalars().all())
    assert len(ledger) == 3, "opening turn plus one follow-up per observation"
    total = sum((entry.cost_usd for entry in ledger), Decimal("0"))
    assert abs(Decimal(str(done["cost_usd"])) - total) < Decimal("0.000005")


#: The only names `orchestrator.py` may take from `app.services.desktop`.
#:
#: The rule this enforces is that the orchestrator cannot reach the machine
#: except through `simulation.perform`. Neither of these can: `ScreenGeometry`
#: is a frozen dataclass of arithmetic over a screenshot payload, and
#: `screenshot_image` reads two keys out of a dict. They exist because
#: downscaled captures changed the coordinate space the model clicks in, and
#: the conversion has to happen where the model's numbers are — before the
#: action becomes an `Effect`.
#:
#: Anything that can *do* something to a desktop belongs on the far side of the
#: chokepoint, so this list stays short and every addition to it has to argue
#: that it is inert.
ORCHESTRATOR_DESKTOP_IMPORTS = {"ScreenGeometry", "screenshot_image"}


async def test_the_orchestrator_never_reaches_for_the_desktop_manager():
    """The migration itself: no second path around the gate is left in the module."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "app" / "services" / "orchestrator.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.desktop":
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            assert not any(
                alias.name.startswith("app.services.desktop") for alias in node.names
            ), "orchestrator.py imported the desktop module wholesale"
    assert imported <= ORCHESTRATOR_DESKTOP_IMPORTS, (
        f"orchestrator.py imports {sorted(imported - ORCHESTRATOR_DESKTOP_IMPORTS)} from "
        "app.services.desktop — a second path around the chokepoint"
    )
    assert "DesktopManager(" not in code
    assert "computer_action(" not in code
    assert "simulation.perform" in code


async def test_a_rehearsed_desktop_step_performs_nothing(db, desk_bot, make_user):
    """The screenshot is an effect like any other, so a dry run must not take one."""
    user = await make_user()
    calls: list[str] = []

    async def _boom(*args, **kwargs):  # pragma: no cover - must never be reached
        calls.append("screenshot")
        raise AssertionError("a rehearsal took a real screenshot")

    with simulation.SimulationContext(bot_id=desk_bot.id) as context:
        outcome = await simulation.perform(
            db,
            simulation.Effect(
                kind="desktop",
                bot_id=desk_bot.id,
                action="screenshot",
                actor_user_id=user.id,
            ),
        )

    assert calls == []
    assert outcome.simulated is True
    assert [call.action for call in context.calls] == ["screenshot"]


async def test_starting_the_desktop_is_not_reported_as_a_blocked_step(db, desk_bot):
    """Its precondition is that the machine is down; that cannot be a 'problem'."""
    assessment = await simulation.assess(
        db,
        simulation.Effect(kind="desktop", bot_id=desk_bot.id, action="start_desktop"),
    )
    assert assessment.problems == ()
    assert "cold start takes 30-90s" in " ".join(assessment.notes)


# ---------------------------------------------------------------------------
# 3. The gate
# ---------------------------------------------------------------------------


async def test_a_gated_desktop_action_creates_an_approval_instead_of_running(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    orchestrator = orchestrator_with(
        ["Sending it now.\n" + directive("click", x=900, y=40, risk="send")]
    )
    thread = await make_thread(user_a, [desk_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "click send")

    rows = await db.execute(select(Approval).where(Approval.bot_id == desk_bot.id))
    held = list(rows.scalars().all())
    assert len(held) == 1
    assert held[0].risk == "send"
    assert held[0].status == "pending"
    assert held[0].payload["kind"] == "desktop_steps"
    assert held[0].payload["steps"] == [{"action": "click", "x": 900, "y": 40}]
    assert done["approval_id"] == str(held[0].id)

    # It was held, not run: no click in the undo log, and the reply says so.
    logged = await db.execute(select(ActionLog).where(ActionLog.action == "click"))
    assert list(logged.scalars().all()) == []
    # Said as a consequence rather than as a risk grade: the reader needs to
    # know what happens when they say yes, not what `services.risk` called it.
    assert "**Waiting on your go-ahead.**" in done["message"]
    assert "sends something out on your behalf" in done["message"]
    assert "it has not happened" in done["message"]
    assert "classifies as" not in done["message"]


async def test_a_declared_risk_cannot_lower_a_desktop_action(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    """Escalate-only, the same rule the HTTP body and routine steps obey."""
    orchestrator = orchestrator_with(
        ["Wiping.\n" + directive("clipboard_set", text="x", risk="observe"), "Done."]
    )
    thread = await make_thread(user_a, [desk_bot])
    await turn(orchestrator, db, user_a, thread)

    rows = await db.execute(select(ActionLog).where(ActionLog.action == "clipboard_set"))
    entry = rows.scalar_one()
    assert entry.risk == "mutate", "a declared 'observe' must not lower the classifier"


async def test_a_held_step_stops_the_loop_and_nothing_after_it_runs(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    orchestrator = orchestrator_with(
        [
            "Sending.\n" + directive("click", x=1, y=2, risk="send"),
            "And now typing.\n" + directive("type", text="more"),
        ]
    )
    thread = await make_thread(user_a, [desk_bot])
    _, done = await turn(orchestrator, db, user_a, thread)

    logged = await db.execute(select(ActionLog).where(ActionLog.action == "type"))
    assert list(logged.scalars().all()) == []
    assert len(orchestrator.router.replies) == 1, "the loop asked the model again after a gate"
    assert "type" not in done["message"]


# ---------------------------------------------------------------------------
# 4. The loop is bounded
# ---------------------------------------------------------------------------


async def test_the_loop_terminates_on_its_step_cap(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    """A model that never says `done` still stops."""
    orchestrator = orchestrator_with([], tail="Still going.\n" + directive("click", x=1, y=1))
    thread = await make_thread(user_a, [desk_bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    clicks = [d for name, d in frames if name == "tool" and d["action"] == "click"]
    assert len(clicks) == DESKTOP_MAX_STEPS
    assert f"limit of {DESKTOP_MAX_STEPS} steps in one turn" in done["message"]
    assert "Ask me again and I will carry on" in done["message"]


async def test_the_loop_terminates_on_a_screen_that_stops_changing(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    """The mock desktop renders a fixed image, which is exactly a stuck UI."""
    orchestrator = orchestrator_with([], tail="Again.\n" + directive("click", x=5, y=5))
    thread = await make_thread(user_a, [desk_bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    clicks = [d for name, d in frames if name == "tool" and d["action"] == "click"]
    # First click sets the baseline; the next `DESKTOP_MAX_UNCHANGED_SCREENS`
    # find it unchanged and the loop gives up well short of the cap.
    assert len(clicks) == 1 + DESKTOP_MAX_UNCHANGED_SCREENS < DESKTOP_MAX_STEPS
    # "byte-identical" and "the UI" are both this repo talking to itself.
    assert "the screen did not change at all" in done["message"]
    assert "is not responding to me" in done["message"]


async def test_the_loop_terminates_when_the_bot_only_looks(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    orchestrator = orchestrator_with([], tail="Looking again.\n" + directive("screenshot"))
    thread = await make_thread(user_a, [desk_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    assert "times in a row without acting" in done["message"]


async def test_the_loop_stops_when_the_bot_reaches_its_daily_budget(
    orchestrator_with, db, user_a, make_bot, make_thread, varying_screens
):
    """A vision turn is far more expensive than a chat reply — the cap must bite."""
    bot = await make_bot(
        user_a,
        name="Frugal",
        system_prompt="You are a test bot.",
        daily_budget_usd=0.01,
    )
    # Under the cap when the turn opens, so the turn runs; the opening model
    # call is enough to cross it, so the loop stops after the first step.
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
    orchestrator = orchestrator_with([], tail="More.\n" + directive("click", x=2, y=2))
    thread = await make_thread(user_a, [bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    assert done.get("budget_blocked") is not True, "the turn itself must still have run"
    clicks = [d for name, d in frames if name == "tool" and d["action"] == "click"]
    assert len(clicks) == 1
    assert "daily budget" in done["message"]
    # The numbers, not just the word: "I hit my budget" with nothing behind it
    # is what made the cap look like a bug rather than a cap.
    assert "$0.01 spent today against a $0.01 cap" in done["message"]
    assert "Looking at a screen costs a lot more" in done["message"]
    # And what it got done comes before where it stopped.
    assert done["message"].index("More.") < done["message"].index("I stopped because")


async def test_an_action_outside_the_vocabulary_stops_the_loop(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    orchestrator = orchestrator_with(['Doing it.\n```json\n{"nesq_desktop": {"action": "hack"}}\n```'])
    thread = await make_thread(user_a, [desk_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    assert "a desktop action I do not have ('hack')" in done["message"]
    logged = await db.execute(select(ActionLog).where(ActionLog.action == "hack"))
    assert list(logged.scalars().all()) == []


# ---------------------------------------------------------------------------
# 5. Image tokens reach the budget
# ---------------------------------------------------------------------------


def test_image_tokens_are_billed_at_the_input_rate_and_added():
    text_only = estimate_cost_usd("mini", 1_000, 200)
    with_image = estimate_cost_usd("mini", 1_000, 200, image_tokens=1_105)
    assert with_image > text_only
    surcharge = estimate_cost_usd("mini", 1_105, 0)
    assert abs((with_image - text_only) - surcharge) < Decimal("0.0000001")
    assert estimate_cost_usd("mini", 0, 0, image_tokens=-5) == Decimal("0")


def test_estimate_cost_usd_still_prices_a_text_turn_unchanged():
    assert estimate_cost_usd("mini", 1_000_000, 1_000_000) == estimate_cost_usd(
        "mini", 1_000_000, 1_000_000, image_tokens=0
    )


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (320, 200, 85 + 170),  # one tile
        (1024, 512, 85 + 170 * 2),  # two tiles wide
        (512, 512, 85 + 170),
        (4096, 4096, 85 + 170 * 4),  # scaled to 768x768 first
    ],
)
def test_the_image_token_formula(width, height, expected):
    assert estimate_image_tokens(width, height) == expected


def test_low_detail_images_are_a_flat_charge():
    assert estimate_image_tokens(4096, 4096, detail="low") == 85


def test_png_dimensions_are_read_from_the_header():
    from app.services.desktop import make_placeholder_png

    assert png_dimensions(make_placeholder_png(320, 200)) == (320, 200)
    assert png_dimensions(b"not a png at all, really not") is None


def test_a_screenshot_in_the_prompt_is_counted_as_an_image_not_as_text():
    """Base64 must never be priced as prose — it is 350x the real number."""
    import base64

    from app.services.desktop import make_placeholder_png

    png = base64.b64encode(make_placeholder_png(320, 200)).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "what is on screen?"}, image_content_part(png)],
        }
    ]
    assert count_image_tokens(messages) == 85 + 170
    assert count_text_tokens(messages) < 20
    assert message_text(messages[0]["content"]) == "what is on screen?"


def test_an_unreadable_image_is_costed_as_a_full_screen_not_as_free():
    messages = [
        {"role": "user", "content": [image_content_part("not-base64-at-all", media_type="image/webp")]}
    ]
    assert count_image_tokens(messages) == estimate_image_tokens(1280, 800)


async def test_a_vision_turn_bills_more_than_the_same_turn_without_the_screen(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    orchestrator = orchestrator_with(
        ["Looking.\n" + directive("screenshot"), "Seen." + directive(DESKTOP_DONE)]
    )
    thread = await make_thread(user_a, [desk_bot])

    await turn(orchestrator, db, user_a, thread)

    rows = await db.execute(select(CostLedger).where(CostLedger.bot_id == desk_bot.id))
    entries = list(rows.scalars().all())
    assert len(entries) == 2, "the opening turn and the one follow-up after the screenshot"

    follow_up = entries[-1]
    # Priced at the row's OWN tier, not a hardcoded one. This compared a
    # `reason` row against a `mini` counterfactual and passed only because
    # `reason` happened to be 25x `mini`; when the reason tier moved to Grok it
    # became the cheaper of the two and the assertion inverted. The claim under
    # test is "the image cost something", which is a same-tier question.
    priced_as_text = estimate_cost_usd(
        follow_up.tier, follow_up.input_tokens - 255, follow_up.output_tokens
    )
    assert follow_up.cost_usd > priced_as_text
    # And the router said so out loud, so a reader can see where it went.
    assert orchestrator.router.last_result.image_tokens == 255
    assert orchestrator.router.last_result.input_tokens > 255


async def test_spent_today_includes_the_image_tokens(db, desk_bot):
    router = ModelRouter()
    result = await router.chat(
        task="agent_turn",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    image_content_part("Zm9v", media_type="image/png"),
                ],
            }
        ],
    )
    await router.record_cost(db, desk_bot.id, result)
    assert result.image_tokens > 0
    assert result.input_tokens > result.image_tokens
    # `cost_ledger.cost_usd` is Numeric(12, 6), so compare at the stored scale.
    spent = await router.spent_today_usd(db, desk_bot.id)
    assert spent == result.cost_usd.quantize(Decimal("0.000001"))
    text_only = estimate_cost_usd("mini", result.input_tokens - result.image_tokens, 0)
    assert spent > text_only, "the ledger would under-count a vision turn"


# ---------------------------------------------------------------------------
# 6. Truthfulness
# ---------------------------------------------------------------------------


async def test_a_bot_with_no_desktop_says_so_and_invents_nothing(
    orchestrator_with, db, user_a, make_thread, desk_bot, monkeypatch
):
    """The failure mode this whole lane exists to avoid: reporting work not done."""

    class Dead:
        state = "error"
        last_error = "the container group could not be scheduled in swedencentral"
        control_url = None

    async def _fail(db_, bot):
        return Dead()

    monkeypatch.setattr(simulation._desktop, "start", _fail)

    orchestrator = orchestrator_with(
        ["Opening LinkedIn.\n" + directive("open_chromium", text="https://linkedin.test")]
    )
    thread = await make_thread(user_a, [desk_bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "message someone on LinkedIn")

    assert "could not start my desktop" in done["message"]
    assert "could not be scheduled in swedencentral" in done["message"]
    assert "I did no desktop work this turn" in done["message"]
    for lie in ("opened", "clicked", "sent", "drafts prepared"):
        assert lie not in done["message"].lower().replace("nothing was opened, clicked or typed", "")

    assert [d["phase"] for name, d in frames if name == "desktop"] == [
        "starting",
        "unavailable",
        "finished",
    ]
    assert [d for name, d in frames if name == "desktop"][-1]["outcome"] == "desktop_unavailable"
    logged = await db.execute(select(ActionLog).where(ActionLog.action == "open_chromium"))
    assert list(logged.scalars().all()) == []
    approvals = await db.execute(select(Approval).where(Approval.bot_id == desk_bot.id))
    assert list(approvals.scalars().all()) == []


async def test_a_failed_step_is_reported_as_failed(
    orchestrator_with, db, user_a, make_thread, desk_bot, monkeypatch
):
    async def _refuse(db_, bot_id, action, payload):
        return {"ok": False, "action": action, "error": "xdotool: no display"}

    monkeypatch.setattr(simulation._desktop, "computer_action", _refuse)
    orchestrator = orchestrator_with(["Typing.\n" + directive("type", text="hello")])
    thread = await make_thread(user_a, [desk_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    # The sidecar's own words survive. What went is the `— failed —` scaffolding
    # around them and the function call in front: a person reading this needs to
    # know that typing "hello" is what did not happen.
    assert 'typing "hello" did not work: xdotool: no display' in done["message"]
    # And outside the fold, where it will be read. A failure cannot be left to
    # the prose: the prose is the model's account, and this is the one thing
    # the module will not take the model's word for.
    assert "**What did not work:**" in done["message"]
    assert done["message"].index("What did not work") < done["message"].index("**What I did")


async def test_a_held_step_is_explained_once_not_twice(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    """The gate's note carries the held step; the failure block does not repeat it.

    Both come from the same real decision, so saying it twice adds nothing —
    and three lines of the same fact is how a reply starts reading like a log
    again, which is what this whole shape exists to stop.
    """
    orchestrator = orchestrator_with(
        ["About to send.\n" + directive("click", x=9, y=9, risk="send")]
    )
    thread = await make_thread(user_a, [desk_bot])

    _, done = await turn(orchestrator, db, user_a, thread)
    message = done["message"]

    assert "Say yes in Approvals and I will carry on from there." in message
    assert "**What did not work:**" not in message
    # It is still in the transcript, in full — and not described as done there
    # either, which is the property the whole gate rests on.
    assert any("has not happened" in line for line in step_log(message))


async def test_a_run_of_failures_stops_the_loop_but_one_failure_does_not(
    orchestrator_with, db, user_a, make_thread, desk_bot, monkeypatch
):
    """One missed click is ordinary; three in a row is a bot that cannot see.

    The old six-step loop stopped dead on the first failed action, which is the
    right call when six steps is the whole budget and the wrong one when the
    task is twenty steps of a live web page.
    """
    from app.services.orchestrator import AGENT_MAX_CONSECUTIVE_FAILURES

    async def _refuse(db_, bot_id, action, payload):
        return {"ok": False, "action": action, "error": "xdotool: no display"}

    monkeypatch.setattr(simulation._desktop, "computer_action", _refuse)
    orchestrator = orchestrator_with([], tail="Again.\n" + directive("type", text="hello"))
    thread = await make_thread(user_a, [desk_bot])

    frames, done = await turn(orchestrator, db, user_a, thread)

    typed = [d for name, d in frames if name == "tool" and d["action"] == "type"]
    assert len(typed) == AGENT_MAX_CONSECUTIVE_FAILURES
    assert all(t["ok"] is False for t in typed)
    assert f"failed {AGENT_MAX_CONSECUTIVE_FAILURES} times in a row" in done["message"]
    assert "I stopped because" in done["message"]


async def test_the_model_is_told_when_the_screen_it_is_shown_is_a_placeholder(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    """A mock deployment must not let the model narrate a screen that is not real."""
    orchestrator = orchestrator_with(
        ["Looking.\n" + directive("screenshot"), "Seen." + directive(DESKTOP_DONE)]
    )
    thread = await make_thread(user_a, [desk_bot])

    await turn(orchestrator, db, user_a, thread)

    observation = orchestrator.router.seen[-1][-2]
    text = observation_text(observation)
    assert "placeholder image, not the real desktop" in text
    assert "Do not describe its contents" in text


async def test_the_transcript_lists_only_steps_that_really_ran(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    orchestrator = orchestrator_with(
        [
            "Step one.\n" + directive("click", x=1, y=1),
            "I have also emailed the team and filed the report." + directive(DESKTOP_DONE),
        ]
    )
    thread = await make_thread(user_a, [desk_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    transcript = step_log(done["message"])
    # The boot is a real action the bot took and appears; the email and the
    # report the model claimed in its prose did not happen and do not.
    assert transcript == [
        "1. Started my desktop",
        "2. Clicked at (1, 1)",
    ]


async def test_no_second_approval_is_stacked_on_a_desktop_turn(
    orchestrator_with, db, user_a, make_thread, desk_bot, varying_screens
):
    """The keyword send-fallback must not re-ask for work the gate already judged."""
    orchestrator = orchestrator_with(
        ["Send it via the browser.\n" + directive("click", x=1, y=1), "Done." + directive(DESKTOP_DONE)]
    )
    thread = await make_thread(user_a, [desk_bot])

    await turn(orchestrator, db, user_a, thread, "send this to them")

    rows = await db.execute(select(Approval).where(Approval.bot_id == desk_bot.id))
    assert list(rows.scalars().all()) == []


async def test_the_directive_never_leaks_into_the_visible_reply(
    orchestrator_with, db, user_a, make_thread, desk_bot
):
    orchestrator = orchestrator_with([directive("screenshot")])
    thread = await make_thread(user_a, [desk_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    assert "nesq_desktop" not in done["message"]
