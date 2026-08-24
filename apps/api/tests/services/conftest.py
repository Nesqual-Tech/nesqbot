"""The scripted-agent harness, shared by every suite under `tests/services`.

`test_agent_loop.py` grew this and `test_agent_cost.py` needs all of it: a
router that answers from a script while keeping the real token accounting, a
bot with room to spend, and a mock desktop whose frames actually differ between
calls. It lives here rather than being imported across test modules because a
pytest fixture imported into another module is a redefinition, and the linter
is right to say so.

What is deliberately *not* faked: token counting, image pricing and
`record_cost` all stay on the production code paths. The cost assertions in
`test_agent_cost.py` are only worth something if they run against the estimator
that bills the ledger.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.services.model_router import ModelRouter, ToolCall, route_task
from app.services.orchestrator import Orchestrator
from tests.services.screens import patch_varying_screens


def call(name: str, *, call_id: str | None = None, **arguments) -> ToolCall:
    """One tool call, shaped the way the API hands them back."""
    return ToolCall(
        id=call_id or f"call_{name}_{uuid.uuid4().hex[:6]}",
        name=name,
        arguments=dict(arguments),
        raw_arguments=json.dumps(arguments),
    )


def says(content: str) -> tuple[str, list[ToolCall]]:
    """A reply that is prose and nothing else — the bug, as a script entry."""
    return content, []


def acts(content: str, *calls: ToolCall) -> tuple[str, list[ToolCall]]:
    return content, list(calls)


def _copy_content(content):
    """One level deeper than `dict(message)` — see `ScriptedToolRouter.chat`."""
    if isinstance(content, list):
        return [dict(part) if isinstance(part, dict) else part for part in content]
    return content


class ScriptedToolRouter(ModelRouter):
    """A router that reports tool support and answers from a script."""

    def __init__(
        self,
        script: list[tuple[str, list[ToolCall]]],
        *,
        tail: tuple[str, list[ToolCall]] | None = None,
    ) -> None:
        super().__init__()
        self.script = list(script)
        self.tail = tail if tail is not None else ("Nothing further.", [])
        #: A *copy* of the messages handed to each call — the loop mutates its
        #: conversation in place, so a reference would show only the last state.
        self.seen: list[list[dict]] = []
        self.tools_seen: list[list[dict] | None] = []
        self.tasks: list[str] = []
        #: The `reasoning_effort` each call asked for, in order. `None` means
        #: the caller sent none.
        self.efforts: list[str | None] = []

    @property
    def supports_tools(self) -> bool:
        return True

    async def chat(
        self, *, task, messages, tools=None, tool_choice=None, fail_count=0, reasoning_effort=None
    ):
        # A *deep* copy of the content lists, not just the message dicts: the
        # screenshot pruner rewrites those lists in place before every call, so
        # a shallow copy would show every recorded request already pruned and
        # the image-count assertions would pass for the wrong reason.
        self.seen.append([{**m, "content": _copy_content(m.get("content"))} for m in messages])
        self.tools_seen.append(tools)
        self.tasks.append(task)
        self.efforts.append(reasoning_effort)
        content, calls = self.script.pop(0) if self.script else self.tail
        result = self._estimated_result(route_task(task, fail_count), messages, content)
        result.tool_calls = list(calls)
        self.last_result = result
        return result

    async def stream_chat(
        self, *, task, messages, tools=None, tool_choice=None, fail_count=0, reasoning_effort=None
    ):
        result = await self.chat(
            task=task,
            messages=messages,
            tools=tools,
            fail_count=fail_count,
            reasoning_effort=reasoning_effort,
        )
        if result.content:
            yield result.content

    @property
    def calls_made(self) -> int:
        return len(self.seen)


async def turn(orchestrator: Orchestrator, db, user, thread, content: str = "do it"):
    frames = [
        frame
        async for frame in orchestrator.handle_user_message_stream(
            db, user=user, thread=thread, content=content
        )
    ]
    done = next((data for name, data in frames if name == "done"), {})
    return frames, done


def actions_in(frames) -> list[str]:
    return [d["action"] for name, d in frames if name == "tool" and d["connector"] == "desktop"]


@pytest.fixture
def agent_with():
    def _build(script, **kwargs) -> Orchestrator:
        orchestrator = Orchestrator()
        orchestrator.router = ScriptedToolRouter(script, **kwargs)
        return orchestrator

    return _build


@pytest.fixture
async def agent_bot(make_bot, user_a):
    """A bot whose own prompt says nothing about a desktop, with room to spend."""
    return await make_bot(
        user_a,
        name="Agent",
        system_prompt="You are a test bot. You file expenses.",
        daily_budget_usd=500.0,
    )


@pytest.fixture
def varying_screens(monkeypatch):
    """Make each screenshot differ, so the stuck-UI detector stays out of the way."""
    return patch_varying_screens(monkeypatch)
