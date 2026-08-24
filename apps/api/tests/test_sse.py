"""The two SSE streams.

Contract (docs/API.md):

* `POST /threads/{id}/messages/stream` emits `token` deltas and always ends on a
  terminal `done` or `error`.
* `GET /threads/{id}/events` carries `turn_started`, `handoff`, `tool`,
  `approval`, `done` and `error` — and **deliberately not** `token`. The
  streaming requester already has the deltas on its own response; a passive
  viewer gets a typing indicator from `turn_started` and the finished text from
  `done`. That omission is a decision, so it is asserted rather than assumed.
"""

from __future__ import annotations

import asyncio
import contextlib

from app.services import events as events_service
from app.services.orchestrator import PUBLISHED_EVENTS
from tests.conftest import auth_headers, read_sse, sse_probe

TERMINAL = {"done", "error"}


@contextlib.asynccontextmanager
async def subscribed(channel: str):
    """Collect everything published to `channel` for the duration of the block."""
    received: list[tuple[str, dict]] = []

    async def _collect() -> None:
        async for name, data in events_service.subscribe(channel):
            received.append((name, data))

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.05)  # let the subscriber register on the in-process bus
    try:
        yield received
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# POST /threads/{id}/messages/stream
# ---------------------------------------------------------------------------


async def test_message_stream_emits_tokens_and_a_terminal_done(
    authed, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    async with authed.stream(
        "POST", f"/api/threads/{thread.id}/messages/stream", json={"content": "hello"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        frames = await read_sse(response, stop_on=TERMINAL)

    names = [name for name, _ in frames]
    assert names[-1] in TERMINAL
    assert names.count("done") == 1
    assert "token" in names, "the streaming caller must receive content deltas"

    deltas = [data["delta"] for name, data in frames if name == "token"]
    assert deltas and all(isinstance(d, str) for d in deltas)
    assert "".join(deltas).strip()


async def test_message_stream_starts_with_turn_started_and_ends_with_done(
    authed, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    async with authed.stream(
        "POST", f"/api/threads/{thread.id}/messages/stream", json={"content": "hello"}
    ) as response:
        frames = await read_sse(response, stop_on=TERMINAL)

    names = [name for name, _ in frames]
    assert names[0] == "turn_started"
    done_payload = frames[-1][1]
    assert done_payload["message_id"]
    assert done_payload["bot_id"] == str(bot_a.id)
    assert done_payload["tier"] == "mini"
    assert "cost_usd" in done_payload


async def test_message_stream_persists_the_turn(authed, make_thread, user_a, bot_a):
    thread = await make_thread(user_a, [bot_a])
    async with authed.stream(
        "POST", f"/api/threads/{thread.id}/messages/stream", json={"content": "persist me"}
    ) as response:
        await read_sse(response, stop_on=TERMINAL)

    listed = await authed.get(f"/api/threads/{thread.id}/messages")
    assert [m["role"] for m in listed.json()] == ["user", "assistant"]


async def test_message_stream_emits_an_approval_event_for_a_held_send(
    authed, make_thread, user_a, bot_a, make_connector_binding
):
    await make_connector_binding(bot_a, "microsoft_graph", status="connected")
    thread = await make_thread(user_a, [bot_a])
    async with authed.stream(
        "POST",
        f"/api/threads/{thread.id}/messages/stream",
        json={"content": "send the email to buyer@example.com"},
    ) as response:
        frames = await read_sse(response, stop_on=TERMINAL)

    approvals = [data for name, data in frames if name == "approval"]
    assert approvals, "a held send must surface as an `approval` event"
    assert approvals[0]["approval_id"]
    assert approvals[0]["title"]


async def test_message_stream_ends_on_error_rather_than_hanging(
    authed, make_thread, user_a
):
    """A thread with no bots fails the turn; the stream must still terminate."""
    thread = await make_thread(user_a, [])
    async with authed.stream(
        "POST", f"/api/threads/{thread.id}/messages/stream", json={"content": "hi"}
    ) as response:
        frames = await read_sse(response, stop_on=TERMINAL)

    names = [name for name, _ in frames]
    assert names[-1] in TERMINAL
    assert "error" in names
    assert "thread has no bots" in dict(frames)["error"]["detail"]


async def test_message_stream_is_503_when_the_orchestrator_cannot_stream(
    authed, make_thread, user_a, bot_a, monkeypatch
):
    from app.routers import deps

    monkeypatch.setattr(deps.orchestrator, "handle_user_message_stream", None, raising=False)
    thread = await make_thread(user_a, [bot_a])
    response = await authed.post(
        f"/api/threads/{thread.id}/messages/stream", json={"content": "hi"}
    )
    assert response.status_code == 503
    assert response.json()["code"] == "streaming_unavailable"


# ---------------------------------------------------------------------------
# GET /threads/{id}/events
# ---------------------------------------------------------------------------


async def _publish_repeatedly(channel: str, payloads, stop: asyncio.Event) -> None:
    """The subscriber registers lazily, so keep publishing until the reader stops."""
    for _ in range(200):
        if stop.is_set():
            return
        for event, data in payloads:
            await events_service.publish(channel, event, data)
        await asyncio.sleep(0.05)


async def test_thread_events_stream_delivers_a_terminal_done(
    app, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    channel = events_service.thread_channel(thread.id)
    stop = asyncio.Event()

    async with sse_probe(
        app, "GET", f"/api/threads/{thread.id}/events", headers=auth_headers(user_a)
    ) as probe:
        pump = asyncio.create_task(
            _publish_repeatedly(channel, [("done", {"message_id": "m-1", "message": "final"})], stop)
        )
        try:
            assert await probe.wait_for("done", "error", timeout=15.0), "no terminal event arrived"
        finally:
            stop.set()
            await pump

    assert probe.status == 200
    assert probe.headers["content-type"].startswith("text/event-stream")
    assert probe.headers["cache-control"] == "no-cache"
    assert probe.headers["x-accel-buffering"] == "no"
    assert probe.frames[-1][0] == "done"
    assert probe.frames[-1][1]["message"] == "final"


async def test_thread_events_stream_carries_the_documented_event_names(
    app, make_thread, user_a, bot_a
):
    thread = await make_thread(user_a, [bot_a])
    channel = events_service.thread_channel(thread.id)
    stop = asyncio.Event()
    payloads = [
        ("turn_started", {"thread_id": str(thread.id), "bot_name": "A"}),
        ("handoff", {"bot_id": str(bot_a.id), "bot_name": "A"}),
        ("tool", {"connector": "crm", "action": "search_accounts", "ok": True}),
        ("approval", {"approval_id": "a-1", "title": "Approve"}),
        ("done", {"message": "done"}),
    ]

    async with sse_probe(
        app, "GET", f"/api/threads/{thread.id}/events", headers=auth_headers(user_a)
    ) as probe:
        pump = asyncio.create_task(_publish_repeatedly(channel, payloads, stop))
        try:
            assert await probe.wait_for("done", timeout=15.0)
        finally:
            stop.set()
            await pump

    names = set(probe.names)
    assert {"turn_started", "handoff", "tool", "approval", "done"} <= names
    assert "token" not in names
    assert probe.frames[-1][0] == "done"


async def test_thread_events_stream_finalises_with_done_when_the_client_goes_away(
    app, make_thread, user_a, bot_a
):
    """No events published, client disconnects: the stream must still close out."""
    thread = await make_thread(user_a, [bot_a])
    async with sse_probe(
        app, "GET", f"/api/threads/{thread.id}/events", headers=auth_headers(user_a)
    ) as probe:
        assert await probe.wait_until_open(), "the stream never sent a response"
        assert probe.status == 200
    # Exiting the block delivered http.disconnect and awaited the app.
    assert probe.names[-1:] == ["done"]
    assert probe.frames[-1][1]["reason"] == "closed"


async def test_thread_events_stream_is_503_without_the_events_service(
    authed, make_thread, user_a, bot_a, monkeypatch
):
    from app.routers import threads as threads_module

    monkeypatch.setattr(threads_module, "optional_service", lambda _name: None)
    thread = await make_thread(user_a, [bot_a])
    response = await authed.get(f"/api/threads/{thread.id}/events")
    assert response.status_code == 503
    assert response.json()["code"] == "events_unavailable"


# ---------------------------------------------------------------------------
# The `token` omission on the passive stream
# ---------------------------------------------------------------------------


def test_token_is_not_in_the_published_event_set():
    """Deliberate contract decision: passive viewers never get per-token traffic.

    `desktop` is on the list for the opposite reason `token` is off it: a cold
    ACI start takes 30-90 seconds, and every viewer of the thread needs to see
    that the bot is booting its machine rather than a turn that has silently
    hung. `takeover` is there for the same reason and more urgently — it is the
    event that puts "sign in and press Continue" on the screen, and a viewer who
    never receives it is looking at a run that will wait for them forever.

    `cost` is there for a money reason rather than a progress one. A vision
    turn can consume a bot's entire daily budget in one go — the run that
    prompted this frame spent $5.00 across 35 desktop steps and said nothing
    until it was gone — so what each step costs, and how much of the cap is
    left, has to reach every viewer of the thread while it is happening.
    """
    assert "token" not in PUBLISHED_EVENTS
    assert PUBLISHED_EVENTS == frozenset(
        {
            "turn_started",
            "handoff",
            "tool",
            "approval",
            "desktop",
            "takeover",
            "cost",
            "done",
            "error",
        }
    )


async def test_a_streaming_turn_publishes_no_token_to_the_thread_channel(
    authed, make_thread, user_a, bot_a
):
    """Run a real streamed turn while subscribed, and prove no `token` is fanned out."""
    thread = await make_thread(user_a, [bot_a])
    channel = events_service.thread_channel(thread.id)

    async with subscribed(channel) as received:
        async with authed.stream(
            "POST", f"/api/threads/{thread.id}/messages/stream", json={"content": "stream please"}
        ) as response:
            streamed = await read_sse(response, stop_on=TERMINAL)
        await asyncio.sleep(0.05)
        published = [name for name, _ in received]

    assert "token" in [name for name, _ in streamed], "the requester must still get deltas"
    assert published, "the turn published nothing to the thread channel"
    assert "token" not in published, "per-token traffic must not reach passive viewers"
    assert "turn_started" in published
    assert published[-1] == "done"
    assert set(published) <= PUBLISHED_EVENTS


async def test_the_published_done_carries_the_full_message_for_passive_viewers(
    authed, make_thread, user_a, bot_a
):
    """A viewer that never saw the tokens renders the finished text from `done`."""
    thread = await make_thread(user_a, [bot_a])
    channel = events_service.thread_channel(thread.id)

    async with subscribed(channel) as received:
        async with authed.stream(
            "POST", f"/api/threads/{thread.id}/messages/stream", json={"content": "hello"}
        ) as response:
            streamed = await read_sse(response, stop_on=TERMINAL)
        await asyncio.sleep(0.05)
        collected = list(received)

    by_name = dict(collected)
    assert "done" in by_name, "no terminal event was published to the thread channel"
    done = by_name["done"]
    assert done["message"], "`done` must carry the finished text"
    assert done["message_id"]
    streamed_text = "".join(d["delta"] for n, d in streamed if n == "token")
    assert done["message"].strip() in streamed_text.strip() or streamed_text.strip() in done["message"]
