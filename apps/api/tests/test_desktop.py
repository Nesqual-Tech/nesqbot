"""Bot Desktop lifecycle and the sidecar proxies (BOT_DESKTOP_MODE=mock)."""

from __future__ import annotations

import base64

import pytest


async def test_desktop_state_starts_absent(authed, bot_a):
    response = await authed.get(f"/api/bots/{bot_a.id}/desktop")
    assert response.status_code == 200
    body = response.json()
    assert body["bot_id"] == str(bot_a.id)
    assert body["state"] == "absent"
    assert body["container_id"] is None


async def test_start_brings_the_desktop_up(authed, bot_a):
    response = await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "running"
    assert body["container_id"].startswith("mock-")
    assert body["stream_url"]
    assert body["control_url"]


async def test_start_is_idempotent(authed, bot_a):
    first = await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    second = await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    assert second.json() == first.json()


async def test_stop_tears_the_desktop_down(authed, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    response = await authed.post(f"/api/bots/{bot_a.id}/desktop/stop")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "absent"
    assert body["container_id"] is None
    assert body["stream_url"] is None


async def test_stop_accepts_the_wipe_flag(authed, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    response = await authed.post(f"/api/bots/{bot_a.id}/desktop/stop?wipe=true")
    assert response.status_code == 200
    assert response.json()["state"] == "absent"


async def test_suspend_and_resume(authed, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")

    suspended = await authed.post(f"/api/bots/{bot_a.id}/desktop/suspend")
    assert suspended.status_code == 200
    assert suspended.json()["state"] == "suspended"

    resumed = await authed.post(f"/api/bots/{bot_a.id}/desktop/resume")
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "running"
    assert resumed.json()["last_error"] is None


async def test_resume_on_a_desktop_that_is_not_suspended_is_a_no_op(authed, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    response = await authed.post(f"/api/bots/{bot_a.id}/desktop/resume")
    assert response.status_code == 200
    assert response.json()["state"] == "running"


async def test_screenshot_returns_a_real_png_placeholder(authed, bot_a):
    response = await authed.get(f"/api/bots/{bot_a.id}/desktop/screenshot")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mock"] is True
    assert body["width"] > 0 and body["height"] > 0
    png = base64.b64decode(body["png_base64"])
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "png_base64 must decode to a real PNG"


async def test_windows_returns_the_canned_list(authed, bot_a):
    response = await authed.get(f"/api/bots/{bot_a.id}/desktop/windows")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mock"] is True
    assert body["windows"]
    assert {"id", "title"} <= set(body["windows"][0])


async def test_a_safe_action_runs_and_echoes_the_payload(authed, bot_a):
    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/desktop/action",
        json={"action": "click", "x": 12, "y": 34, "button": "right"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["action"] == "click"
    assert body["payload"]["button"] == "3", "button names are normalised before dispatch"


async def test_an_action_on_a_stopped_desktop_fails_cleanly(authed, bot_a):
    response = await authed.post(
        f"/api/bots/{bot_a.id}/desktop/action", json={"action": "click", "x": 1, "y": 1}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "desktop not running"}


async def test_desktop_action_requires_an_action_name(authed, bot_a):
    response = await authed.post(f"/api/bots/{bot_a.id}/desktop/action", json={"x": 1})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize("suffix", ["", "/screenshot", "/windows"])
async def test_desktop_routes_404_for_an_unknown_bot(authed, suffix):
    import uuid

    response = await authed.get(f"/api/bots/{uuid.uuid4()}/desktop{suffix}")
    assert response.status_code == 404
    assert response.json()["code"] == "bot_not_found"


async def test_deleting_a_bot_stops_its_desktop_first(authed, db, bot_a):
    from app.models import BotDesktop

    await authed.post(f"/api/bots/{bot_a.id}/desktop/start")
    assert (await db.get(BotDesktop, bot_a.id)).state == "running"

    response = await authed.delete(f"/api/bots/{bot_a.id}")
    assert response.status_code == 200
    db.expunge_all()
    assert await db.get(BotDesktop, bot_a.id) is None, "the desktop row cascades with the bot"
