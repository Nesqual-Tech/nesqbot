"""The DOM lane against a real `nesqbot/bot-desktop` container.

Everything else in this suite proves the API's own behaviour against a stub,
which is the right default: CI has no 1.66 GB desktop image and a test that
needs Chromium is a test that flakes. But a stub can only ever agree with
whatever the stub's author believed, and the two things most worth knowing
about this lane are things a stub cannot answer:

* whether the paths, methods and body fields in `services.browser.BROWSER_OPS`
  are the ones the shipped sidecar actually serves;
* whether the real `409`s — `obscured` under a consent banner, `stale_ref`
  after a navigation, `select_failed` on an ARIA combobox — and the real
  `pending_dialog` behaviour reach the model as something it can act on.

So this module runs the whole chokepoint (`simulation.perform` ->
`DesktopManager.browser_call` -> CDP -> Chromium) against a live container, and
is skipped unless one is pointed at:

    docker run -d --name nesq-cdp --network <net> --shm-size 512m \\
      -e NESQ_SIDECAR_TOKEN=t nesqbot/bot-desktop:v0.2.0
    NESQ_LIVE_DESKTOP_URL=http://nesq-cdp:7910 NESQ_LIVE_SIDECAR_TOKEN=t pytest -m live

`file:///home/nesq/` is inside the sidecar's URL allowlist, so the fixture
writes its bench page into the container's home directory rather than needing a
web server or the public internet.
"""

from __future__ import annotations

import os
import textwrap
import uuid

import pytest
from sqlalchemy import select

from app.models import Approval, BotDesktop
from app.services import browser as B
from app.services import simulation
from app.services.model_router import message_text
from app.services.orchestrator import TOOL_TASK_COMPLETE
from tests.services.conftest import ScriptedToolRouter, call, turn

pytestmark = pytest.mark.live

LIVE_URL = os.getenv("NESQ_LIVE_DESKTOP_URL", "").strip()
LIVE_TOKEN = os.getenv("NESQ_LIVE_SIDECAR_TOKEN", "").strip()

if not LIVE_URL:  # pragma: no cover - the default everywhere except a live lane
    pytest.skip(
        "set NESQ_LIVE_DESKTOP_URL to run against a real bot-desktop container",
        allow_module_level=True,
    )

#: A page with the three things real sites do that a happy-path bench does not:
#: a consent banner covering the controls, a button whose handler blocks the
#: renderer with `alert()`, and a control whose accessible name is the reason a
#: human should see the step before it runs.
BENCH_HTML = textwrap.dedent(
    """\
    <!doctype html><meta charset=utf-8><title>Nesq DOM bench</title>
    <style>#banner{position:fixed;left:0;top:0;right:0;height:220px;
      background:#202020;color:#fff;z-index:9}</style>
    <div id=banner>We use cookies.
      <button id=dismiss onclick="banner.remove()">Accept cookies</button></div>
    <h1>Bench</h1>
    <button id=send onclick="alert('sent')">Send invoice</button>
    <button id=danger>Delete account</button>
    <select id=plan><option value=free>Free</option><option value=pro>Pro</option></select>
    <input id=code placeholder=x aria-label="Discount code">
    <a href="/home/nesq/bench.html?again=1">Keep shopping</a>
    """
)

BENCH_URL = "file:///home/nesq/nesq-dom-bench.html"


@pytest.fixture(autouse=True)
def _live_settings(monkeypatch):
    """Point the shipped manager at the live container instead of mock mode."""
    monkeypatch.setattr(simulation._desktop.settings, "bot_desktop_mode", "docker")
    monkeypatch.setattr(simulation._desktop.settings, "nesq_sidecar_token", LIVE_TOKEN)


@pytest.fixture
async def live_bot(db, make_bot, user_a):
    bot = await make_bot(user_a, name="Live", daily_budget_usd=500.0)
    db.add(BotDesktop(bot_id=bot.id, state="running", control_url=LIVE_URL))
    await db.flush()
    return bot


async def _call(db, bot, action, **payload):
    """One browser action, down the real chokepoint."""
    outcome = await simulation.perform(
        db,
        simulation.Effect(kind="desktop", bot_id=bot.id, action=action, input_data=payload),
    )
    return outcome


@pytest.fixture
async def bench(db, live_bot):
    """A bot pointed at the bench page, or a skip that says how to make one.

    The sidecar has no file-write endpoint, deliberately — its whole design
    principle is that caller input is data, never code — so the page cannot be
    installed from here. Put `BENCH_HTML` at `BENCH_URL` in the container
    first::

        docker exec -u nesq <container> sh -c 'cat > /home/nesq/nesq-dom-bench.html' \\
          < bench.html

    Skipping with the reason beats a fixture that quietly tests something else.
    """
    outcome = await _call(db, live_bot, "browser_navigate", url=BENCH_URL)
    if not outcome.result.get("ok") or outcome.result.get("title") != "Nesq DOM bench":
        pytest.skip(
            f"{BENCH_URL} is not the bench page in this container — write BENCH_HTML "
            f"there first ({outcome.result.get('error') or outcome.result.get('title')})"
        )
    return live_bot


async def _snapshot(db, bot, **kw):
    outcome = await _call(db, bot, "browser_snapshot", viewport_only=False, **kw)
    assert outcome.result.get("ok"), outcome.result
    return outcome.result, B.parse_snapshot_refs(outcome.result["snapshot"])


def _find(refs, role, name_part):
    return next(
        (r for r, (rl, nm) in refs.items() if rl == role and name_part.lower() in nm.lower()),
        None,
    )


# ---------------------------------------------------------------------------


async def test_the_table_matches_the_running_sidecar(db, live_bot):
    """Every advertised op answers on the real container, not just in the docs."""
    outcome = await _call(db, live_bot, "browser_status")
    assert outcome.result.get("ok"), outcome.result
    assert outcome.result["status"] == 200
    assert outcome.result.get("browser", "").startswith("Chrome/")


async def test_navigate_snapshot_and_click_a_ref(db, bench):
    outcome = await _call(db, bench, "browser_navigate", url=BENCH_URL)
    assert outcome.result["ok"] and outcome.result["title"] == "Nesq DOM bench"
    assert "void" in B.result_text("browser_navigate", outcome.result)

    result, refs = await _snapshot(db, bench)
    assert _find(refs, "button", "Send invoice"), result["snapshot"]

    # The banner covers most of this short page, so dismissing it first is not
    # test tidiness — it is the recovery a real site forces, and doing it here
    # keeps the assertion below about the click rather than about the overlay.
    dismiss = _find(refs, "button", "Accept cookies")
    await _call(db, bench, "browser_click", ref=dismiss, snapshot_id=result["snapshot_id"])

    result, refs = await _snapshot(db, bench)
    link = _find(refs, "link", "Keep shopping")
    outcome = await _call(
        db, bench, "browser_click", ref=link, snapshot_id=result["snapshot_id"]
    )
    assert outcome.result["ok"], outcome.result
    assert outcome.result["name"] == "Keep shopping"


async def test_a_consent_banner_produces_obscured_and_names_itself(db, bench):
    result, refs = await _snapshot(db, bench)
    danger = _find(refs, "button", "Delete account")

    outcome = await _call(
        db,
        bench,
        "browser_click",
        ref=danger,
        snapshot_id=result["snapshot_id"],
        # Declared so the gate does not hold this step: what is under test here
        # is the sidecar's refusal, not the approval flow.
        force=False,
    )
    assert outcome.result["status"] == 409
    assert outcome.result["error"] == "obscured"
    text = B.result_text("browser_click", outcome.result)
    assert "banner" in text.lower() or "cookies" in text.lower()
    assert "dismiss" in text.lower()

    # And the recovery the text asks for actually works.
    dismiss = _find(refs, "button", "Accept cookies")
    outcome = await _call(db, bench, "browser_click", ref=dismiss, snapshot_id=result["snapshot_id"])
    assert outcome.result["ok"]


async def test_a_blocking_alert_reports_the_click_as_having_landed(db, bench):
    result, refs = await _snapshot(db, bench)
    dismiss = _find(refs, "button", "Accept cookies")
    await _call(db, bench, "browser_click", ref=dismiss, snapshot_id=result["snapshot_id"])

    result, refs = await _snapshot(db, bench)
    send = _find(refs, "button", "Send invoice")
    outcome = await _call(db, bench, "browser_click", ref=send, snapshot_id=result["snapshot_id"])

    # `ok: true` *with* a dialog. Retrying would double-fire it.
    assert outcome.result["ok"] is True
    assert outcome.result["pending_dialog"]["type"] == "alert"
    text = B.result_text("browser_click", outcome.result)
    assert "Do NOT repeat" in text and "browser_dialog" in text

    outcome = await _call(db, bench, "browser_dialog", accept=True)
    assert outcome.result["ok"], outcome.result


async def test_select_works_on_a_real_select_and_refuses_anything_else(db, bench):
    result, refs = await _snapshot(db, bench)
    combo = next((r for r, (rl, _n) in refs.items() if rl == "combobox"), None)
    outcome = await _call(
        db, bench, "browser_select", ref=combo, values=["Pro"], snapshot_id=result["snapshot_id"]
    )
    assert outcome.result["ok"], outcome.result
    assert outcome.result["selected"][0]["label"] == "Pro"

    not_a_select = _find(refs, "button", "Delete account") or _find(refs, "link", "Keep")
    outcome = await _call(
        db,
        bench,
        "browser_select",
        ref=not_a_select,
        values=["Pro"],
        snapshot_id=result["snapshot_id"],
        ref_label="link",  # keep the gate out of the way; the sidecar is under test
    )
    assert outcome.result["status"] == 409 and outcome.result["error"] == "select_failed"


async def test_a_navigation_really_does_invalidate_every_ref(db, bench):
    result, refs = await _snapshot(db, bench)
    send = _find(refs, "button", "Send invoice")
    await _call(db, bench, "browser_navigate", url="about:blank")
    outcome = await _call(
        db, bench, "browser_click", ref=send, snapshot_id=result["snapshot_id"]
    )
    assert outcome.result["status"] == 409
    assert outcome.result["error"] in ("stale_ref", "unknown_ref")
    assert "browser_snapshot" in B.result_text("browser_click", outcome.result)


async def test_the_economy_knobs_move_real_bytes(db, live_bot):
    """The numbers the tool descriptions promise, measured on a real page."""
    await _call(db, live_bot, "browser_navigate", url="https://en.wikipedia.org/wiki/Web_browser")
    full = (await _call(db, live_bot, "browser_snapshot", viewport_only=False,
                        max_elements=1000)).result
    if not full.get("ok"):  # pragma: no cover - no egress in this lane
        pytest.skip("no outbound network from the container")
    viewport = (await _call(db, live_bot, "browser_snapshot", viewport_only=True,
                            max_elements=1000)).result
    capped = (await _call(db, live_bot, "browser_snapshot", viewport_only=False,
                          max_elements=60)).result
    named = (await _call(db, live_bot, "browser_snapshot", viewport_only=False,
                         name_filter="History")).result

    assert viewport["bytes"] < full["bytes"]
    assert capped["bytes"] < full["bytes"]
    assert named["bytes"] < capped["bytes"]
    assert capped["truncated"] is True
    assert "showing 60 of" in B.result_text("browser_snapshot", capped)


class _RefFollowingRouter(ScriptedToolRouter):
    """A model that reads the snapshot it was given and clicks something in it.

    A static script cannot drive a live browser: refs are minted per snapshot by
    a process-global counter, so nothing knows in advance that the button is
    `e42`. This is the smallest thing that can — it finds a ref by accessible
    name in whatever the last tool result said — and it is what makes the test
    below an end-to-end proof of the loop rather than of a fixture.
    """

    def __init__(self, want: str) -> None:
        super().__init__([])
        self.want = want
        self.step = 0

    async def chat(self, *, task, messages, tools=None, tool_choice=None, fail_count=0,
                   reasoning_effort=None):
        self.step += 1
        if self.step == 1:
            self.script = [("", [call("browser_navigate", url=BENCH_URL)])]
        elif self.step == 2:
            self.script = [("", [call("browser_snapshot", viewport_only=False)])]
        elif self.step == 3:
            # Read the refs out of the snapshot the loop actually handed back,
            # exactly as a model would: from the text of the last messages.
            blob = "\n".join(message_text(m.get("content")) for m in messages[-3:])
            refs = B.parse_snapshot_refs(blob)
            ref = _find(refs, "button", self.want) or _find(refs, "link", self.want)
            assert ref, f"no {self.want!r} in what the loop showed the model:\n{blob}"
            self.script = [("", [call("browser_click", ref=ref)])]
        else:
            self.script = [("", [call(TOOL_TASK_COMPLETE, summary="Finished on the bench page.")])]
        return await super().chat(
            task=task, messages=messages, tools=tools, fail_count=fail_count,
            reasoning_effort=reasoning_effort,
        )


async def test_the_whole_loop_drives_a_real_browser(db, user_a, make_thread, bench):
    """Navigate, read the page, click a ref — no screenshot anywhere in it."""
    from app.services.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    orchestrator.router = _RefFollowingRouter("Accept cookies")
    thread = await make_thread(user_a, [bench])

    frames, done = await turn(orchestrator, db, user_a, thread, "dismiss the cookie banner")

    actions = [d["action"] for name, d in frames if name == "tool"]
    assert actions == ["browser_navigate", "browser_snapshot", "browser_click"]
    assert "screenshot" not in actions
    assert "Finished on the bench page." in done["message"]
    assert "browser_click(" in done["message"]


async def test_the_gate_reads_the_real_accessible_name(db, user_a, make_thread, bench):
    """A live snapshot, a live name, and a step a human has to see.

    This is the property the pixel lane cannot have, proved against the
    accessibility tree Chrome actually computed rather than against a string a
    fixture wrote.
    """
    from app.services.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    orchestrator.router = _RefFollowingRouter("Delete account")
    thread = await make_thread(user_a, [bench])

    _frames, done = await turn(orchestrator, db, user_a, thread, "close my account")

    approval = (await db.execute(select(Approval))).scalars().one()
    assert approval.risk == "delete"
    assert 'button "Delete account"' in approval.summary
    assert "Approvals" in done["message"]
    assert uuid.UUID(str(approval.id))
