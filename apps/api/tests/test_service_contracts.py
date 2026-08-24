"""The Python service-layer contracts in docs/API.md.

The HTTP surface is guarded by `test_contract_docs.py`; this module guards the
other half of the contract — the service functions the routers, the worker and
the docs all agree on. A rename or a dropped keyword here is a cross-lane break
that no route test would catch.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest

pytestmark = pytest.mark.contract


def _params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


def _accepts(fn, names: set[str]) -> bool:
    parameters = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return names <= set(parameters)


# ---------------------------------------------------------------------------
# app/services/approvals.py
# ---------------------------------------------------------------------------


def test_execute_approved_signature():
    from app.services.approvals import execute_approved

    assert inspect.iscoroutinefunction(execute_approved)
    assert _accepts(execute_approved, {"db", "approval", "user"})


def test_create_approval_signature():
    from app.services.approvals import create_approval

    assert inspect.iscoroutinefunction(create_approval)
    assert _accepts(
        create_approval, {"db", "run_id", "bot_id", "risk", "title", "summary", "payload"}
    )


def test_the_four_documented_approval_kinds_are_the_ones_implemented():
    from app.services.approvals import KINDS

    assert set(KINDS) == {"connector_action", "mcp_tool", "desktop_steps", "message_only"}


# ---------------------------------------------------------------------------
# app/services/rag.py
# ---------------------------------------------------------------------------


def test_rag_surface():
    from app.services import rag

    assert _accepts(rag.embed, {"texts"})
    assert _accepts(rag.upsert_memory_embedding, {"db", "memory"})
    assert _accepts(rag.upsert_kb_embedding, {"db", "article"})
    assert _accepts(rag.search_kb, {"db", "query", "limit"})
    assert _accepts(rag.search_memories, {"db", "bot_id", "user_id", "query", "limit"})
    for fn in (
        rag.embed,
        rag.upsert_memory_embedding,
        rag.upsert_kb_embedding,
        rag.search_kb,
        rag.search_memories,
    ):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} must be async"


def test_the_documented_default_limits():
    from app.services import rag

    assert inspect.signature(rag.search_kb).parameters["limit"].default == 5
    assert inspect.signature(rag.search_memories).parameters["limit"].default == 8


# ---------------------------------------------------------------------------
# app/services/events.py
# ---------------------------------------------------------------------------


def test_events_surface():
    from app.services import events

    assert _accepts(events.publish, {"channel", "event", "data"})
    assert inspect.iscoroutinefunction(events.publish)
    assert inspect.isasyncgenfunction(events.subscribe)
    assert _accepts(events.subscribe, {"channel"})
    assert not inspect.iscoroutinefunction(events.thread_channel)


# ---------------------------------------------------------------------------
# app/services/model_router.py
# ---------------------------------------------------------------------------


def test_stream_chat_signature():
    from app.services.model_router import ModelRouter

    assert inspect.isasyncgenfunction(ModelRouter.stream_chat)
    assert _params(ModelRouter.stream_chat) >= {"task", "messages", "tools", "fail_count"}
    signature = inspect.signature(ModelRouter.stream_chat)
    assert signature.parameters["tools"].default is None
    assert signature.parameters["fail_count"].default == 0


def test_stream_chat_returns_an_async_iterator_of_str():
    from app.services.model_router import ModelRouter

    annotation = inspect.signature(ModelRouter.stream_chat).return_annotation
    assert "AsyncIterator[str]" in str(annotation) or annotation is AsyncIterator


def test_last_result_is_exposed_on_the_router():
    from app.services.model_router import ModelRouter

    assert hasattr(ModelRouter(), "last_result")


def test_the_documented_request_timeout_and_retry_budget():
    from app.config import get_settings
    from app.services.model_router import RETRY_ATTEMPTS

    assert RETRY_ATTEMPTS == 3
    assert get_settings().request_timeout_seconds == 60.0


# ---------------------------------------------------------------------------
# app/services/desktop.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["resume", "screenshot", "windows"])
def test_the_documented_desktop_additions_exist(name):
    """docs/API.md writes these as module-level functions; they are implemented
    as `DesktopManager` methods and the router resolves either form. Accept both
    so the contract test tracks the capability, not the spelling."""
    from app.services import desktop

    manager_method = getattr(desktop.DesktopManager, name, None)
    module_function = getattr(desktop, name, None)
    target = manager_method or module_function
    assert target is not None, f"desktop.{name} is missing in both forms"
    assert inspect.iscoroutinefunction(target)
    assert _accepts(target, {"db", "bot_id"})


# ---------------------------------------------------------------------------
# app/services/temporal_client.py
# ---------------------------------------------------------------------------


def test_temporal_client_surface():
    from app.services import temporal_client

    assert inspect.iscoroutinefunction(temporal_client.get_client)
    assert _params(temporal_client.get_client) == set()
    assert _accepts(temporal_client.sync_routine_schedule, {"routine"})
    assert _accepts(temporal_client.delete_routine_schedule, {"routine_id"})
    assert _accepts(temporal_client.start_routine_now, {"routine"})


async def test_get_client_returns_none_when_temporal_is_unreachable():
    """The suite pins TEMPORAL_HOST at a dead port; None is the documented signal."""
    from app.services import temporal_client

    assert await temporal_client.get_client() is None


async def test_start_routine_now_returns_an_empty_dict_when_temporal_is_down(make_bot, make_user):
    from app.models import Routine
    from app.services import temporal_client

    user = await make_user()
    bot = await make_bot(user)
    routine = Routine(bot_id=bot.id, name="r", steps=[], version=1)
    assert await temporal_client.start_routine_now(routine) == {}


async def test_sync_and_delete_schedule_are_no_ops_when_temporal_is_down(make_bot, make_user):
    from app.models import Routine
    from app.services import temporal_client

    user = await make_user()
    bot = await make_bot(user)
    routine = Routine(bot_id=bot.id, name="r", steps=[], version=1, schedule_cron="0 9 * * *")
    assert await temporal_client.sync_routine_schedule(routine) is None
    assert await temporal_client.delete_routine_schedule(routine.id) is None


def test_schedule_ids_are_derived_from_the_routine_id():
    import uuid

    from app.services import temporal_client

    routine_id = uuid.uuid4()
    assert temporal_client.schedule_id_for(routine_id) == f"routine-{routine_id}"
