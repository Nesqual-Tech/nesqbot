"""`app.services.events` — the in-process fallback used when Redis is absent."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services import events


@pytest.fixture(autouse=True)
def _clean_bus():
    _reset_bus()
    yield
    _reset_bus()


def _reset_bus() -> None:
    """Forget the cached client, the subscribers, *and* the connect lock.

    `events._redis_lock` is a module-global `asyncio.Lock`. An uncontended
    `acquire()` never touches the running loop, so the lock survives a test
    unbound; the first *contended* acquire binds it to whichever loop is
    running then, and pytest-asyncio hands every test a new one. A later
    contended acquire - two subscribers racing to open the same connection -
    raises "bound to a different event loop" from inside `subscribe`, and the
    second subscriber silently never registers. Purely a harness concern, the
    same shape as `_reset_sse_app_status` in conftest: under uvicorn there is
    one loop for the life of the process.
    """
    events.reset_redis()
    events._redis_lock = None
    events._local_subscribers.clear()


async def _registered(channel: str, count: int = 1, timeout: float = 5.0) -> None:
    """Block until `count` in-process queues are listening on `channel`.

    A fixed sleep is not a handshake. The first `subscribe()` in a test attempts
    a real Redis connection - the suite points REDIS_URL at a dead port and the
    autouse fixture re-arms the attempt before every test - so a subscriber can
    still be inside that connect when a 50ms sleep expires. The publish then
    fans out to nobody, and the test fails on machine load rather than on
    behaviour. Waiting on the registry itself is deterministic.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(events._local_subscribers.get(channel, ())) < count:
        assert loop.time() < deadline, f"{count} subscriber(s) never registered on {channel}"
        await asyncio.sleep(0.005)


def _channel() -> str:
    return events.thread_channel(uuid.uuid4())


async def _drain(channel: str, count: int, timeout: float = 5.0) -> list[tuple[str, dict]]:
    got: list[tuple[str, dict]] = []

    async def _run() -> None:
        async for item in events.subscribe(channel):
            got.append(item)
            if len(got) >= count:
                return

    task = asyncio.create_task(_run())
    await _registered(channel)
    return got, task  # type: ignore[return-value]


def test_thread_channel_is_the_documented_format():
    thread_id = uuid.uuid4()
    assert events.thread_channel(thread_id) == f"thread:{thread_id}"
    assert events.thread_channel("abc") == "thread:abc"


async def test_redis_is_unreachable_in_this_configuration():
    """The suite pins REDIS_URL at a dead port so the fallback is what runs."""
    assert await events._get_redis() is None
    assert events._redis_disabled is True


async def test_publish_without_a_subscriber_does_not_raise():
    await events.publish(_channel(), "done", {"ok": True})


async def test_publish_and_subscribe_round_trip_in_process():
    channel = _channel()
    received: list[tuple[str, dict]] = []

    async def _listen() -> None:
        async for name, data in events.subscribe(channel):
            received.append((name, data))
            return

    task = asyncio.create_task(_listen())
    await _registered(channel)
    await events.publish(channel, "done", {"message": "hi"})
    await asyncio.wait_for(task, timeout=5.0)

    assert received == [("done", {"message": "hi"})]


async def test_every_subscriber_on_a_channel_gets_the_event():
    channel = _channel()
    a: list[tuple[str, dict]] = []
    b: list[tuple[str, dict]] = []

    async def _listen(sink: list) -> None:
        async for item in events.subscribe(channel):
            sink.append(item)
            return

    tasks = [asyncio.create_task(_listen(a)), asyncio.create_task(_listen(b))]
    await _registered(channel, 2)
    await events.publish(channel, "tool", {"connector": "crm"})
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    assert a == b == [("tool", {"connector": "crm"})]


async def test_channels_are_isolated():
    first, second = _channel(), _channel()
    received: list[tuple[str, dict]] = []

    async def _listen() -> None:
        async for item in events.subscribe(first):
            received.append(item)

    task = asyncio.create_task(_listen())
    await _registered(first)
    await events.publish(second, "done", {"wrong": True})
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert received == []


async def test_events_arrive_in_order():
    channel = _channel()
    received: list[str] = []

    async def _listen() -> None:
        async for name, _ in events.subscribe(channel):
            received.append(name)
            if len(received) == 3:
                return

    task = asyncio.create_task(_listen())
    await _registered(channel)
    for name in ("turn_started", "tool", "done"):
        await events.publish(channel, name, {})
    await asyncio.wait_for(task, timeout=5.0)

    assert received == ["turn_started", "tool", "done"]


async def test_a_subscriber_deregisters_when_it_stops():
    channel = _channel()

    async def _listen() -> None:
        async for _ in events.subscribe(channel):
            return

    task = asyncio.create_task(_listen())
    await _registered(channel)
    assert channel in events._local_subscribers

    await events.publish(channel, "done", {})
    await asyncio.wait_for(task, timeout=5.0)
    await asyncio.sleep(0.05)
    assert channel not in events._local_subscribers, "the queue leaked after the consumer stopped"


async def test_a_slow_consumer_drops_the_oldest_rather_than_blocking():
    """A full queue must never stall the publisher — a turn outranks a viewer."""
    channel = _channel()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    events._local_subscribers.setdefault(channel, set()).add(queue)
    try:
        for i in range(5):
            await asyncio.wait_for(events.publish(channel, "tool", {"i": i}), timeout=1.0)
        assert queue.qsize() == 2
        newest = [queue.get_nowait()["data"]["i"] for _ in range(2)]
        assert newest == [3, 4], "the newest events must be the ones kept"
    finally:
        events._local_subscribers.pop(channel, None)


async def test_publish_serialises_non_json_values():
    channel = _channel()
    received: list[tuple[str, dict]] = []

    async def _listen() -> None:
        async for item in events.subscribe(channel):
            received.append(item)
            return

    task = asyncio.create_task(_listen())
    await _registered(channel)
    await events.publish(channel, "done", {"approval_id": uuid.uuid4()})
    await asyncio.wait_for(task, timeout=5.0)
    assert received[0][0] == "done"


def test_reset_redis_re_arms_the_connection_attempt():
    events._redis_disabled = True
    events.reset_redis()
    assert events._redis_disabled is False
    assert events._redis_client is None
