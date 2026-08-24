"""Every browser failure carries a reason, including the ones nobody designed for.

From a real session, in the log the owner read:

    browser_tabs() — failed — no reason given
    browser_wait(timeout_ms=10000, until='load') — failed — no reason given

The desktop had been running for weeks on an image from before `/browser/*`
existed, so every one of those calls was a plain FastAPI `404 {"detail": "Not
Found"}`. The proxy trusted `ok` and `error` straight out of the body on the
grounds that the sidecar always sets both — true of a sidecar that has the lane,
and the whole point of version skew is that the thing answering might not be
one. Neither key was there, so the step logged as a failure with nothing
attached, the model had nothing to recover from, and it fell back to guessing
coordinates for thirty-six steps without ever saying why.

"No reason given" is the worst failure text a governance product can print.
"""

from __future__ import annotations

import httpx
import pytest

from app.models import BotDesktop
from app.services import browser as B
from app.services import simulation
from app.services.desktop import DesktopManager
from tests.services.conftest import acts, call, turn

BENCH = 'e1 button "Send"'


# ---------------------------------------------------------------------------
# The envelope, on its own
# ---------------------------------------------------------------------------


def test_a_404_becomes_an_absent_capability_not_a_blank_failure():
    result = B.envelope("browser_tabs", 404, {"detail": "Not Found"})

    assert result["ok"] is False
    assert result["error"] == B.BROWSER_NOT_SUPPORTED
    text = B.result_text("browser_tabs", result)
    assert "404" in text
    assert "before DOM browser control" in text
    # And the remedy, which is the part a person can act on.
    assert "stopping and starting" in text


def test_a_200_without_the_documented_envelope_is_not_a_success():
    """A bot claiming work it cannot vouch for is the failure mode above all."""
    result = B.envelope("browser_click", 200, {"something": "else"})

    assert result["ok"] is not True
    assert result["error"] == "cdp_error"
    assert "without the documented" in result["detail"]


def test_a_real_sidecar_error_passes_through_untouched():
    body = {"ok": False, "error": "obscured", "detail": "a banner is on top"}

    result = B.envelope("browser_click", 409, body)

    assert result["error"] == "obscured"
    assert result["detail"] == "a banner is on top"


def test_a_success_passes_through_untouched():
    result = B.envelope("browser_click", 200, {"ok": True, "ref": "e1"})

    assert result["ok"] is True
    assert "error" not in result


@pytest.mark.parametrize("status", [500, 502])
def test_any_other_uncoded_failure_still_names_its_status(status):
    result = B.envelope("browser_text", status, {})

    assert result["error"] == "cdp_error"
    assert str(status) in result["detail"]


def test_short_failure_carries_the_code_and_the_detail():
    line = B.short_failure(B.envelope("browser_tabs", 404, {"detail": "Not Found"}))

    assert line.startswith(f"{B.BROWSER_NOT_SUPPORTED} (404)")
    assert "no /browser lane" in line


# ---------------------------------------------------------------------------
# Through the proxy, against a sidecar that really answers 404
# ---------------------------------------------------------------------------


@pytest.fixture
def old_sidecar(monkeypatch):
    """An HTTP client that answers like a pre-DOM `bot-desktop` image."""

    class _Client:
        def __init__(self, *_a, **_kw) -> None: ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, **_kw):
            return httpx.Response(404, json={"detail": "Not Found"}, request=httpx.Request("POST", url))

        async def get(self, url, **_kw):
            return httpx.Response(404, json={"detail": "Not Found"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


async def test_the_proxy_turns_an_old_sidecar_into_a_diagnosable_failure(
    db, user_a, make_bot, monkeypatch, old_sidecar
):
    bot = await make_bot(user_a, name="Old", daily_budget_usd=500.0)
    db.add(BotDesktop(bot_id=bot.id, state="running", control_url="http://desktop.test:7910"))
    await db.flush()
    manager = DesktopManager()
    monkeypatch.setattr(manager.settings, "bot_desktop_mode", "docker")

    result = await manager.browser_call(db, bot.id, "browser_tabs", {})

    assert result["error"] == B.BROWSER_NOT_SUPPORTED
    assert result["status"] == 404


# ---------------------------------------------------------------------------
# …and what the loop does about it
# ---------------------------------------------------------------------------


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
def sidecar_404(monkeypatch):
    sent: list[str] = []

    async def _call(_db, _bot_id, action, payload=None):
        sent.append(action)
        return B.envelope(action, 404, {"detail": "Not Found"})

    monkeypatch.setattr(simulation._desktop, "browser_call", _call)
    return sent


async def test_the_step_log_says_why_rather_than_no_reason_given(
    agent_with, db, user_a, make_thread, live_desktop, sidecar_404, varying_screens
):
    """The exact line the owner read, replaced with one that explains itself.

    The assertions moved off `browser_not_supported` and `404` because the reply
    is written for the person who asked for the work now, and neither of those
    is a thing they have ever seen — `browser_not_supported` is the code the
    model recovers from and `404` is what one HTTP server said to another. The
    property this test is actually defending is unchanged and is checked harder:
    the failure names itself, in enough detail that the reader knows what went
    wrong *and* what to do about it, which the code and the status never did.

    Both still exist where they are read by something that can use them: on the
    step record as `error`, in the audit trail, and in the sentence handed back
    to the model — asserted below off the transcript the router saw.
    """
    bot = await live_desktop(user_a)
    orchestrator = agent_with(
        [
            acts("", call("browser_tabs")),
            acts("", call("task_complete", summary="Gave up on the DOM.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "list my tabs")

    assert "no reason given" not in done["message"]
    assert "checking which tabs were open did not work" in done["message"]
    assert "cannot be driven through the browser at all" in done["message"]
    assert "stopping and starting it" in done["message"]
    # The code and the status did not evaporate; they went to their reader.
    seen = str(orchestrator.router.seen)
    assert B.BROWSER_NOT_SUPPORTED in seen
    assert "404" in seen


async def test_an_absent_browser_lane_degrades_to_pixels_out_loud(
    agent_with, db, user_a, make_thread, live_desktop, sidecar_404, varying_screens
):
    """It is an absent capability, not a failed action.

    The old behaviour spent one of the loop's three lives on it and then went
    quiet; a `404` should hand the model a screenshot and say, in words, that no
    `browser_*` tool can work on this desktop.
    """
    bot = await live_desktop(user_a)
    orchestrator = agent_with(
        [
            acts("", call("browser_click", ref="e1")),
            acts("", call("task_complete", summary="Used pixels instead.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "click send")

    shown = "\n".join(
        str(part.get("text", ""))
        for message in orchestrator.router.seen[-1]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict)
    )
    assert "pixel tools" in shown
    assert "browser_*" in shown or "browser_" in shown
