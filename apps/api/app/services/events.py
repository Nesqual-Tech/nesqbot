"""Thread event bus — Redis pub/sub with a transparent in-process fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# In-process fanout used whenever Redis is unavailable. Keyed by channel.
_local_subscribers: dict[str, set[asyncio.Queue]] = {}
_redis_client: Any | None = None
_redis_lock: asyncio.Lock | None = None
_redis_disabled = False

QUEUE_MAXSIZE = 512


def thread_channel(thread_id: uuid.UUID | str) -> str:
    return f"thread:{thread_id}"


def _lock() -> asyncio.Lock:
    global _redis_lock
    if _redis_lock is None:
        _redis_lock = asyncio.Lock()
    return _redis_lock


async def _get_redis() -> Any | None:
    """Lazily connect to Redis. Returns None (and stays None) when unreachable."""
    global _redis_client, _redis_disabled
    if _redis_disabled:
        return None
    if _redis_client is not None:
        return _redis_client

    async with _lock():
        if _redis_client is not None:
            return _redis_client
        if _redis_disabled:
            return None
        settings = get_settings()
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=settings.redis_connect_timeout_seconds,
                socket_timeout=settings.redis_connect_timeout_seconds,
            )
            await asyncio.wait_for(client.ping(), timeout=settings.redis_connect_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - local dev runs without Redis
            logger.info("redis unavailable (%s) — using in-process event bus", exc)
            _redis_disabled = True
            return None
        _redis_client = client
        return _redis_client


def reset_redis() -> None:
    """Forget the cached client so the next call retries the connection."""
    global _redis_client, _redis_disabled
    _redis_client = None
    _redis_disabled = False


def _fanout_local(channel: str, payload: dict[str, Any]) -> None:
    for queue in list(_local_subscribers.get(channel, ())):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:  # slow consumer — drop the oldest and retry once
            try:
                queue.get_nowait()
                queue.put_nowait(payload)
            except Exception:  # noqa: BLE001
                pass


async def publish(channel: str, event: str, data: dict) -> None:
    """Publish one event. Never raises — a dead bus must not break a turn."""
    payload = {"event": event, "data": data}
    client = await _get_redis()
    if client is not None:
        try:
            await client.publish(channel, json.dumps(payload, default=str))
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis publish failed (%s) — falling back in-process", exc)
            reset_redis()
    _fanout_local(channel, payload)


async def subscribe(channel: str) -> AsyncIterator[tuple[str, dict]]:
    """Yield `(event_name, data)` for a channel until the consumer stops."""
    client = await _get_redis()
    if client is not None:
        try:
            async for item in _subscribe_redis(client, channel):
                yield item
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis subscribe failed (%s) — falling back in-process", exc)
            reset_redis()
    async for item in _subscribe_local(channel):
        yield item


async def _subscribe_redis(client: Any, channel: str) -> AsyncIterator[tuple[str, dict]]:
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is None:
                continue
            raw = message.get("data")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            yield str(payload.get("event", "message")), dict(payload.get("data") or {})
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


async def _subscribe_local(channel: str) -> AsyncIterator[tuple[str, dict]]:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    _local_subscribers.setdefault(channel, set()).add(queue)
    try:
        while True:
            payload = await queue.get()
            yield str(payload.get("event", "message")), dict(payload.get("data") or {})
    finally:
        subs = _local_subscribers.get(channel)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                _local_subscribers.pop(channel, None)
