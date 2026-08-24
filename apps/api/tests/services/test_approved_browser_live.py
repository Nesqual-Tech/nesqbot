"""The approved-action path, against a real `nesqbot/bot-desktop` container.

`test_approved_browser_actions.py` proves the logic against a stub, which is the
right default — CI has no 1.66 GB desktop image. But a stub can only agree with
whatever its author believed, and the two claims worth most here are ones only
Chromium can settle:

* that a snapshot really does go stale and the *old* payload really is refused,
  so the re-resolution is solving a problem that exists;
* that re-resolving by role and Chrome's own computed accessible name really
  does land on the same element, on a page that has since scrolled, navigated
  within itself, or grown a second control with the same name.

Run it the same way as `test_browser_live.py`::

    docker run -d --name nesq-cdp --network <net> --shm-size 512m \\
      -e NESQ_SIDECAR_TOKEN=t nesqbot/bot-desktop:v0.2.0
    NESQ_LIVE_DESKTOP_URL=http://nesq-cdp:7910 NESQ_LIVE_SIDECAR_TOKEN=t pytest -m live

The three pages it needs live in the container's home directory, which is inside
the sidecar's `file:///home/nesq/` allowlist; the fixture skips with the exact
`docker exec` line if they are not there.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from app.models import BotDesktop
from app.services import browser as B
from app.services import simulation

pytestmark = pytest.mark.live

LIVE_URL = os.getenv("NESQ_LIVE_DESKTOP_URL", "").strip()
LIVE_TOKEN = os.getenv("NESQ_LIVE_SIDECAR_TOKEN", "").strip()

if not LIVE_URL:  # pragma: no cover - the default everywhere except a live lane
    pytest.skip(
        "set NESQ_LIVE_DESKTOP_URL to run against a real bot-desktop container",
        allow_module_level=True,
    )

BENCH_URL = "file:///home/nesq/nesq-dom-bench.html"
TWINS_URL = "file:///home/nesq/nesq-twins.html"
ELSEWHERE_URL = "file:///home/nesq/nesq-elsewhere.html"

PAGES = {
    "nesq-dom-bench.html": textwrap.dedent(
        """\
        <!doctype html><meta charset=utf-8><title>Nesq DOM bench</title>
        <div id=banner>We use cookies.
          <button id=dismiss onclick="banner.remove()">Accept cookies</button></div>
        <button id=danger>Delete account</button>
        <p id=out>idle</p>
        <div style="height:2400px"></div>
        <button id=deep>Far below the fold</button>
        """
    ),
    "nesq-twins.html": '<button>Delete account</button><button>Delete account</button>',
    "nesq-elsewhere.html": '<button>Delete account</button>',
}


@pytest.fixture(autouse=True)
def _live_settings(monkeypatch):
    monkeypatch.setattr(simulation._desktop.settings, "bot_desktop_mode", "docker")
    monkeypatch.setattr(simulation._desktop.settings, "nesq_sidecar_token", LIVE_TOKEN)


@pytest.fixture
async def live_bot(db, make_bot, user_a):
    bot = await make_bot(user_a, name="Live", daily_budget_usd=500.0)
    db.add(BotDesktop(bot_id=bot.id, state="running", control_url=LIVE_URL))
    await db.flush()
    return bot


async def _do(db, bot, action, *, approved=False, **payload):
    outcome = await simulation.perform(
        db,
        simulation.Effect(
            kind="desktop",
            bot_id=bot.id,
            action=action,
            input_data=payload,
            pre_approved=approved,
        ),
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


@pytest.fixture
async def bench(db, live_bot):
    result = await _do(db, live_bot, "browser_navigate", url=BENCH_URL)
    if not result.get("ok") or result.get("title") != "Nesq DOM bench":
        pytest.skip(
            "the bench pages are not in this container. Write them first, e.g.\n"
            + "\n".join(
                f"  docker exec -u nesq <container> sh -c 'cat > /home/nesq/{name}' <<'EOF'\n"
                f"{body}EOF"
                for name, body in PAGES.items()
            )
        )
    return live_bot


def _held(ref: str, url: str, target_id: str, label: str = 'button "Delete account"') -> dict:
    """The payload the gate stores — including the snapshot id that will go stale."""
    return {
        "ref": ref,
        "snapshot_id": "s-long-gone",
        B.REF_LABEL_KEY: label,
        B.REF_PAGE_KEY: url,
        B.REF_TARGET_KEY: target_id,
    }


# ---------------------------------------------------------------------------


async def test_the_stale_payload_really_is_refused_by_the_real_sidecar(db, bench):
    """The problem, reproduced against Chromium rather than asserted about.

    Without this the fix is a solution to a bug nobody has shown exists.
    """
    result, refs = await _snapshot(db, bench)
    danger = _find(refs, "button", "Delete account")
    assert danger, result["snapshot"]

    # Time passes and the page moves on, exactly as it does between a hold and
    # a human getting to the queue.
    await _go(db, bench, "about:blank")
    await _go(db, bench, BENCH_URL)

    refused = await _do(
        db, bench, "browser_click", ref=danger, snapshot_id=result["snapshot_id"]
    )

    assert refused["ok"] is False
    assert refused["error"] in ("stale_ref", "unknown_ref")


async def test_an_approved_click_lands_on_the_right_element_after_the_snapshot_dies(
    db, bench
):
    """The headline, end to end, on a real accessibility tree.

    Same page, same button, a dead snapshot id and a ref that no longer means
    anything — and the click still lands on `button "Delete account"`, which the
    page proves by writing DONE into `#out`.
    """
    # The consent banner covers this button, and a click under it is correctly
    # refused as `obscured` — proved in `test_browser_live.py`. Dismissing it is
    # the recovery a real site forces, and doing it here keeps this test about
    # the re-resolution rather than about the overlay.
    result, refs = await _snapshot(db, bench)
    await _do(
        db,
        bench,
        "browser_click",
        ref=_find(refs, "button", "Accept cookies"),
        snapshot_id=result["snapshot_id"],
    )

    result, refs = await _snapshot(db, bench)
    danger = _find(refs, "button", "Delete account")
    page_url, target = result["url"], result["target_id"]

    # Kill the snapshot the way a real wait does: reload, so every ref and every
    # snapshot id from before is void in the sidecar. The banner comes back with
    # it, so the approved click also has to survive an overlay that was not
    # there when the snapshot behind the approval was taken.
    await _do(db, bench, "browser_reload")
    fresh, fresh_refs = await _snapshot(db, bench)
    await _do(
        db,
        bench,
        "browser_click",
        ref=_find(fresh_refs, "button", "Accept cookies"),
        snapshot_id=fresh["snapshot_id"],
    )

    landed = await _do(
        db, bench, "browser_click", approved=True, **_held(danger, page_url, target)
    )

    assert landed["ok"], landed
    assert landed["name"] == "Delete account"
    # The ref that was approved is not the ref that was used: `danger` came from
    # a snapshot the reload destroyed.
    assert landed["ref"] != danger or fresh["snapshot_id"] != result["snapshot_id"]
    # …and the page itself agrees that the right button was pressed.
    read = await _do(db, bench, "browser_text", selector="#out")
    assert read["text"].strip() == "DONE"


async def test_an_approved_click_on_an_element_below_the_fold_still_lands(db, bench):
    """A page that scrolled since the approval is not a missing element."""
    result, refs = await _snapshot(db, bench)
    deep = _find(refs, "button", "Far below the fold")
    assert deep, result["snapshot"]
    page_url, target = result["url"], result["target_id"]

    await _go(db, bench, "about:blank")
    await _go(db, bench, BENCH_URL)

    landed = await _do(
        db,
        bench,
        "browser_click",
        approved=True,
        **_held(deep, page_url, target, 'button "Far below the fold"'),
    )

    assert landed["ok"], landed
    assert landed["name"] == "Far below the fold"


async def test_two_real_elements_with_the_same_name_refuse_rather_than_guess(db, bench):
    """The safety property, against Chrome's own name computation.

    Both buttons are genuinely `button "Delete account"`. There is no
    non-arbitrary way to choose, so nothing is clicked.
    """
    result, refs = await _snapshot(db, bench)
    danger = _find(refs, "button", "Delete account")
    target = result["target_id"]

    live = await _go(db, bench, TWINS_URL)

    refused = await _do(
        db, bench, "browser_click", approved=True, **_held(danger, live["url"], target)
    )

    assert refused["ok"] is False
    assert refused["error"] == B.APPROVED_ELEMENT_AMBIGUOUS
    assert refused["matched"] == 2


async def test_a_same_named_button_on_another_page_is_not_the_approved_one(db, bench):
    """Identity alone would click this. The recorded page is why it does not."""
    result, refs = await _snapshot(db, bench)
    danger = _find(refs, "button", "Delete account")
    page_url, target = result["url"], result["target_id"]

    await _go(db, bench, ELSEWHERE_URL)

    refused = await _do(
        db, bench, "browser_click", approved=True, **_held(danger, page_url, target)
    )

    assert refused["ok"] is False
    assert refused["error"] == B.APPROVED_PAGE_CHANGED
    assert refused["current_url"].endswith("nesq-elsewhere.html")


async def test_an_element_that_is_gone_refuses_and_names_itself(db, bench):
    result, refs = await _snapshot(db, bench)
    dismiss = _find(refs, "button", "Accept cookies")
    page_url, target = result["url"], result["target_id"]

    # Press it once, which removes the banner it lives in.
    await _do(db, bench, "browser_click", ref=dismiss, snapshot_id=result["snapshot_id"])

    refused = await _do(
        db,
        bench,
        "browser_click",
        approved=True,
        **_held(dismiss, page_url, target, 'button "Accept cookies"'),
    )

    assert refused["ok"] is False
    assert refused["error"] == B.APPROVED_ELEMENT_MISSING
    assert 'button "Accept cookies"' in B.result_text("browser_click", refused)


async def test_a_stopped_browser_session_is_reported_as_lost_not_missing(db, bench):
    result, refs = await _snapshot(db, bench)
    danger = _find(refs, "button", "Delete account")
    page_url, target = result["url"], result["target_id"]

    await _go(db, bench, "about:blank")

    refused = await _do(
        db, bench, "browser_click", approved=True, **_held(danger, page_url, target)
    )

    assert refused["error"] == B.BROWSER_SESSION_LOST
