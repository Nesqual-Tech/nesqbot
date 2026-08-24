"""The owner's two failures, against real Chromium.

Both Lead Bot runs died on the same shape of thing::

    browser_click(ref='e49')  - failed - unknown_ref (409): e49 is not from a live snapshot
    browser_click(ref='e514') - failed - stale_ref  (409): e514 belongs to snapshot s14, not s15

The second run did 33 desktop actions and 32 of them ran. The one that did not
was reaching for a reference minted by the previous snapshot after the page had
moved on.

Everything here runs the whole chokepoint (`simulation.perform` ->
`DesktopManager.browser_call` -> CDP -> Chromium) against a live container,
because the load-bearing claims are claims about what the *shipped sidecar*
does with a reference — that it keeps four snapshots resolvable, that it
re-verifies role and accessible name against Chrome's own computation on every
resolve, and that `stale_ref` and `unknown_ref` are raised before it dispatches
anything. A stub can only ever agree with whoever wrote it.

    docker run -d --name nesq-cdp --network <net> --shm-size 512m \\
      -e NESQ_SIDECAR_TOKEN=t nesqbot/bot-desktop:v0.2.0
    NESQ_LIVE_DESKTOP_URL=http://nesq-cdp:7910 NESQ_LIVE_SIDECAR_TOKEN=t pytest -m live

`test_stale_ref_recovery.py` is the stub half of the same argument and runs
everywhere.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from app.models import BotDesktop
from app.services import browser as B
from app.services import simulation
from app.services.model_router import message_text
from app.services.orchestrator import TOOL_TASK_COMPLETE
from tests.services.conftest import ScriptedToolRouter, actions_in, call, turn

pytestmark = pytest.mark.live

LIVE_URL = os.getenv("NESQ_LIVE_DESKTOP_URL", "").strip()
LIVE_TOKEN = os.getenv("NESQ_LIVE_SIDECAR_TOKEN", "").strip()

if not LIVE_URL:  # pragma: no cover - the default everywhere except a live lane
    pytest.skip(
        "set NESQ_LIVE_DESKTOP_URL to run against a real bot-desktop container",
        allow_module_level=True,
    )

LEAD_URL = "file:///home/nesq/nesq-lead.html"
TWINS_URL = "file:///home/nesq/nesq-lead-twins.html"
ELSEWHERE_URL = "file:///home/nesq/nesq-elsewhere.html"

#: A prospect page with no consent banner, so what is under test is the
#: reference rather than an overlay — `test_browser_live.py` owns the overlay.
#: `Connect` is deliberately a name `risk.classify_label_risk` reads as
#: `observe`: a `send`/`spend`/`delete` label is held for a human before
#: `_execute` is ever reached, so it could not exercise this path at all.
PAGES = {
    "nesq-lead.html": textwrap.dedent(
        """\
        <!doctype html><meta charset=utf-8><title>Nesq lead bench</title>
        <h1>Ada Prospect</h1>
        <button id=go onclick="out.textContent='CONNECTED'">Connect</button>
        <button id=vanish onclick="vanish.remove()">Dismiss notice</button>
        <a href="/home/nesq/nesq-elsewhere.html">Northwind Ltd</a>
        <p id=out>idle</p>
        """
    ),
    "nesq-lead-twins.html": textwrap.dedent(
        """\
        <!doctype html><meta charset=utf-8><title>Nesq lead twins</title>
        <button>Connect</button><button>Connect</button>
        """
    ),
    "nesq-elsewhere.html": (
        "<!doctype html><meta charset=utf-8><title>Elsewhere</title>"
        "<button>Delete account</button><button>Connect</button>"
    ),
}


@pytest.fixture(autouse=True)
def _live_settings(monkeypatch):
    monkeypatch.setattr(simulation._desktop.settings, "bot_desktop_mode", "docker")
    monkeypatch.setattr(simulation._desktop.settings, "nesq_sidecar_token", LIVE_TOKEN)


@pytest.fixture
async def live_bot(db, make_bot, user_a):
    bot = await make_bot(user_a, name="Leady", daily_budget_usd=500.0)
    db.add(BotDesktop(bot_id=bot.id, state="running", control_url=LIVE_URL))
    await db.flush()
    return bot


async def _do(db, bot, action, **payload):
    outcome = await simulation.perform(
        db,
        simulation.Effect(kind="desktop", bot_id=bot.id, action=action, input_data=payload),
    )
    return outcome.result


async def _go(db, bot, url):
    result = await _do(db, bot, "browser_navigate", url=url)
    assert result.get("ok"), result
    return result


async def _snapshot(db, bot, **kw):
    result = await _do(db, bot, "browser_snapshot", viewport_only=False, **kw)
    assert result.get("ok"), result
    return result, B.parse_snapshot_refs(result["snapshot"])


def _find(refs, role, name):
    return next((r for r, (rl, nm) in refs.items() if rl == role and nm == name), None)


def _identity(url, target, label='button "Connect"') -> dict:
    """What `_annotate_browser_arguments` puts on a ref it has provenance for."""
    return {B.REF_LABEL_KEY: label, B.REF_PAGE_KEY: url, B.REF_TARGET_KEY: target}


async def _said(db, bot) -> str:
    return (await _do(db, bot, "browser_text", selector="#out"))["text"].strip()


@pytest.fixture
async def lead(db, live_bot):
    result = await _do(db, live_bot, "browser_navigate", url=LEAD_URL)
    if not result.get("ok") or result.get("title") != "Nesq lead bench":
        pytest.skip(
            "the lead bench pages are not in this container. Write them first, e.g.\n"
            + "\n".join(
                f"  docker exec -u nesq <container> sh -c 'cat > /home/nesq/{name}' < page\n"
                f"{body}"
                for name, body in PAGES.items()
            )
        )
    return live_bot


# ---------------------------------------------------------------------------
# The reproduction, and what it looks like now
# ---------------------------------------------------------------------------


async def test_the_sidecar_refuses_a_ref_pinned_to_the_wrong_snapshot(db, lead):
    """The owner's run 2, isolated: the pin was the whole problem.

    Nothing about the button changed. The page did not navigate, the element is
    still in the document, and Chrome still computes the same accessible name.
    Pinned to the snapshot it came from the sidecar acts; pinned to a later one
    — which is what the loop used to send — it refuses.
    """
    first, refs = await _snapshot(db, lead)
    connect = _find(refs, "button", "Connect")
    assert connect, first["snapshot"]

    second, _ = await _snapshot(db, lead)
    assert second["snapshot_id"] != first["snapshot_id"]

    refused = await _do(
        db, lead, "browser_click", ref=connect, snapshot_id=second["snapshot_id"]
    )
    assert refused["ok"] is False
    assert refused["error"] == "stale_ref"
    assert f"belongs to snapshot {first['snapshot_id']}" in refused["detail"]
    assert await _said(db, lead) == "idle"

    landed = await _do(
        db, lead, "browser_click", ref=connect, snapshot_id=first["snapshot_id"]
    )
    assert landed["ok"], landed
    assert landed["name"] == "Connect"
    assert await _said(db, lead) == "CONNECTED"


class _StaleRefRouter(ScriptedToolRouter):
    """A model that reads a page, reads it again, then acts on the first read.

    Not a contrived script — it is what a long task does. The bot snapshots,
    checks something, snapshots again, and acts on the element it decided about
    several steps ago. A static script cannot drive a live browser, because refs
    are minted by a process-global counter and nothing knows in advance that the
    button is `e42`, so this finds it by accessible name in whatever the loop
    actually showed the model.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.step = 0
        self.remembered = ""

    async def chat(self, *, task, messages, tools=None, tool_choice=None, fail_count=0,
                   reasoning_effort=None):
        self.step += 1
        if self.step == 1:
            self.script = [("", [call("browser_snapshot", viewport_only=False)])]
        elif self.step == 2:
            blob = "\n".join(message_text(m.get("content")) for m in messages[-3:])
            self.remembered = _find(B.parse_snapshot_refs(blob), "button", "Connect") or ""
            assert self.remembered, blob
            self.script = [("", [call("browser_snapshot", viewport_only=False)])]
        elif self.step == 3:
            self.script = [("", [call("browser_click", ref=self.remembered)])]
        else:
            self.script = [("", [call(TOOL_TASK_COMPLETE, summary="Connected.")])]
        return await super().chat(
            task=task, messages=messages, tools=tools, fail_count=fail_count,
            reasoning_effort=reasoning_effort,
        )


async def test_the_loop_no_longer_produces_the_owners_failure(db, user_a, make_thread, lead):
    """The same thing through the agent loop, which is where it actually bit.

    The model never asked for a `snapshot_id`. The loop pinned the newest one it
    had seen onto a ref from an older one, the sidecar compared the two and
    refused, and the run's step log read `3 desktop actions, 2 ran`. Now the pin
    names the ref's own snapshot, the sidecar's four real checks decide, and the
    page itself says the click landed.
    """
    from app.services.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    orchestrator.router = _StaleRefRouter()
    thread = await make_thread(user_a, [lead])

    frames, _done = await turn(orchestrator, db, user_a, thread, "connect with her")

    assert actions_in(frames) == ["browser_snapshot", "browser_snapshot", "browser_click"]
    click = next(d for name, d in frames if name == "tool" and d["action"] == "browser_click")
    assert click["ok"] is True
    assert await _said(db, lead) == "CONNECTED"


async def test_an_evicted_ref_recovers_by_identity_and_the_click_lands(db, lead):
    """The owner's run 1: `unknown_ref`, because a navigation voided every ref.

    There is nothing to re-verify — the reference names nothing at all — so
    recovery is the only thing that can work: find the element the loop
    recorded, on the page the loop recorded, and act on that.
    """
    first, refs = await _snapshot(db, lead)
    connect = _find(refs, "button", "Connect")
    page_url, target = first["url"], first["target_id"]

    await _go(db, lead, "about:blank")
    await _go(db, lead, LEAD_URL)

    # No identity recorded: the sidecar's own refusal is the honest answer.
    bare = await _do(db, lead, "browser_click", ref=connect)
    assert bare["ok"] is False
    assert bare["error"] == "unknown_ref"
    assert await _said(db, lead) == "idle"

    # With the identity the loop records, the same call recovers.
    landed = await _do(
        db, lead, "browser_click", ref=connect, **_identity(page_url, target)
    )
    assert landed["ok"], landed
    assert landed["name"] == "Connect"
    assert landed[B.RECOVERED_KEY] is True
    assert landed["recovered_from"] == "unknown_ref"
    assert landed["ref"] != connect
    assert await _said(db, lead) == "CONNECTED"

    text = B.result_text("browser_click", landed)
    assert "no longer valid (unknown_ref)" in text
    assert "browser_snapshot before your next one" in text


# ---------------------------------------------------------------------------
# Negative controls. Each must refuse, and say which.
# ---------------------------------------------------------------------------


async def test_an_element_that_is_genuinely_gone_refuses(db, lead):
    """`Dismiss notice` removes itself, so after one press there is no such
    element on a page that is otherwise unchanged.

    This is the case a positional fallback would get most wrong: the page is
    right, the tab is right, and the thing simply is not there.
    """
    first, refs = await _snapshot(db, lead)
    vanish = _find(refs, "button", "Dismiss notice")
    assert vanish, first["snapshot"]
    page_url, target = first["url"], first["target_id"]

    pressed = await _do(db, lead, "browser_click", ref=vanish, snapshot_id=first["snapshot_id"])
    assert pressed["ok"], pressed

    refused = await _do(
        db,
        lead,
        "browser_click",
        ref=vanish,
        **_identity(page_url, target, 'button "Dismiss notice"'),
    )

    assert refused["ok"] is False
    assert refused["error"] == B.REF_ELEMENT_MISSING
    assert refused["caused_by"] in B.RECOVERABLE_REF_ERRORS
    text = B.result_text("browser_click", refused)
    assert 'button "Dismiss notice"' in text
    assert "Nothing was clicked" in text


async def test_two_real_elements_with_the_same_name_refuse_rather_than_guess(db, lead):
    """Both buttons are genuinely `button "Connect"` to Chrome's own name
    computation. There is no non-arbitrary way to choose, so nothing is clicked.
    """
    await _go(db, lead, TWINS_URL)
    twins, refs = await _snapshot(db, lead)
    one = min(r for r, (rl, nm) in refs.items() if rl == "button" and nm == "Connect")
    page_url, target = twins["url"], twins["target_id"]

    # Void the refs without leaving the page, so the only thing wrong at the
    # moment of the click is the reference.
    await _go(db, lead, "about:blank")
    await _go(db, lead, TWINS_URL)

    refused = await _do(db, lead, "browser_click", ref=one, **_identity(page_url, target))

    assert refused["ok"] is False
    assert refused["error"] == B.REF_ELEMENT_AMBIGUOUS
    assert refused["matched"] == 2


async def test_a_page_that_navigated_refuses_rather_than_clicking_the_twin(db, lead):
    """`nesq-elsewhere.html` has a `button "Connect"` too.

    Identity alone would press it. The recorded page is why it does not.
    """
    first, refs = await _snapshot(db, lead)
    connect = _find(refs, "button", "Connect")
    page_url, target = first["url"], first["target_id"]

    moved = await _go(db, lead, ELSEWHERE_URL)

    refused = await _do(
        db, lead, "browser_click", ref=connect, **_identity(page_url, target)
    )

    assert refused["ok"] is False
    assert refused["error"] == B.REF_PAGE_CHANGED
    assert refused["recorded_url"] == page_url
    assert refused["current_url"].endswith("nesq-elsewhere.html")
    assert moved["url"].endswith("nesq-elsewhere.html")


async def test_a_restarted_browser_is_reported_as_lost_not_missing(db, lead):
    """`about:blank` means the pages and the signed-in session are both gone,
    which is a different problem from a button that moved."""
    first, refs = await _snapshot(db, lead)
    connect = _find(refs, "button", "Connect")
    page_url, target = first["url"], first["target_id"]

    await _go(db, lead, "about:blank")

    refused = await _do(
        db, lead, "browser_click", ref=connect, **_identity(page_url, target)
    )

    assert refused["ok"] is False
    assert refused["error"] == B.BROWSER_SESSION_LOST
    assert "restarted" in B.result_text("browser_click", refused)


async def test_a_truncated_snapshot_cannot_prove_uniqueness(db, lead, monkeypatch):
    """One match in a snapshot that admits it was cut short is not unique.

    The bench cannot produce a thousand elements, so the ceiling is lowered to
    one and the twins page is used: the sidecar really does then answer
    `matched: 2, returned: 1, truncated: true`, and the resolution sees exactly
    one row. Acting on it would be the positional guess in disguise — the second
    `button "Connect"` is in the part that was cut off.

    Distinguished from the plain two-match refusal by its sentence, because the
    remedy differs: one says narrow the page, the other says the page is bigger
    than one snapshot.
    """
    await _go(db, lead, TWINS_URL)
    twins, refs = await _snapshot(db, lead)
    one = min(r for r, (rl, nm) in refs.items() if rl == "button" and nm == "Connect")
    page_url, target = twins["url"], twins["target_id"]

    monkeypatch.setattr(B, "IDENTITY_SNAPSHOT_MAX_ELEMENTS", 1)

    await _go(db, lead, "about:blank")
    await _go(db, lead, TWINS_URL)

    refused = await _do(db, lead, "browser_click", ref=one, **_identity(page_url, target))

    assert refused["ok"] is False
    assert refused["error"] == B.REF_ELEMENT_AMBIGUOUS
    assert refused["matched"] == 1, "exactly one row came back, and it was still refused"
    assert "more elements than one snapshot can show" in refused["detail"]


async def test_nothing_is_retried_that_might_have_landed(db, lead):
    """The recovery is gated on two codes that prove the sidecar did nothing.

    `obscured` is the sidecar refusing about a *live* element, and it is not one
    of them: it passes straight back so the model dismisses whatever is on top
    instead of being told about a re-resolution that never happened.
    """
    await _go(db, lead, ELSEWHERE_URL)
    here, refs = await _snapshot(db, lead)
    connect = _find(refs, "button", "Connect")

    # A live ref, correctly pinned — so any failure here is about the element,
    # not about the reference, and recovery must stay out of it.
    landed = await _do(
        db, lead, "browser_click", ref=connect, snapshot_id=here["snapshot_id"]
    )
    assert landed["ok"], landed
    assert B.RECOVERED_KEY not in landed
