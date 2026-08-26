"""Anthropic — the one provider whose wire format genuinely differs from
OpenAI's, translated at the edges by `_AnthropicAdapter` and the
`_anthropic_*` module functions in `model_router.py`.

No live Anthropic account is used or needed anywhere in this file. The
request/response/streaming shapes asserted against were read directly off the
installed `anthropic` 1.0.0 SDK's own pydantic type definitions
(`Message`, `Usage`, `TextBlock`, `ToolUseBlock`, `RawMessageStartEvent`,
`RawContentBlockStartEvent`, `RawContentBlockDeltaEvent`, `TextDelta`,
`InputJSONDelta`, `RawMessageDeltaEvent`) — not guessed, not copied from
memory of an older SDK version.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.models import Bot
from app.services.model_router import (
    _LOGGED_AUTH_MODES,
    _WARNED_OFF_DEFAULT,
    ANTHROPIC_DEFAULT_MAX_TOKENS,
    ModelRouter,
    _accumulate_tool_call_deltas,
    _anthropic_content,
    _anthropic_messages,
    _anthropic_request,
    _anthropic_response_to_openai_shape,
    _anthropic_stream_to_openai_chunks,
    _anthropic_tool_choice,
    _anthropic_tools,
    _finish_tool_call_deltas,
    assistant_tool_call_message,
    parse_tool_calls,
    tool_result_message,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "azure_openai_endpoint": "",
        "azure_openai_api_key": "",
        "azure_managed_identity_client_id": "",
    }
    return Settings(**{**base, **overrides})


def _bot(**overrides: Any) -> Bot:
    return Bot(slug="test-bot", name="Test", role="", system_prompt="", is_system=False, **overrides)


@pytest.fixture(autouse=True)
def _reset_process_state():
    _LOGGED_AUTH_MODES.clear()
    _WARNED_OFF_DEFAULT.clear()
    yield
    _LOGGED_AUTH_MODES.clear()
    _WARNED_OFF_DEFAULT.clear()


# ---------------------------------------------------------------------------
# Client construction / auth_mode
# ---------------------------------------------------------------------------


def test_anthropic_with_no_key_is_mock():
    router = ModelRouter(_settings(model_provider="anthropic"))
    assert router.client("mini") is None
    assert router.auth_mode == "mock"


def test_anthropic_with_a_key_builds_a_real_adapter():
    router = ModelRouter(_settings(model_provider="anthropic", anthropic_api_key="sk-ant-test"))
    client = router.client("mini")
    assert client is not None
    assert router.auth_mode == "api_key"
    # The adapter, not the raw AsyncAnthropic client - `_create()` calls
    # `.chat.completions.create`, which only the adapter exposes.
    assert hasattr(client, "chat")
    assert hasattr(client.chat.completions, "create")


def test_anthropic_client_is_cached_per_key():
    router = ModelRouter(_settings(model_provider="anthropic", anthropic_api_key="sk-ant-test"))
    first = router.client("mini")
    second = router.client("nano")
    assert first is second


def test_anthropic_per_tier_key_overrides_the_shared_one():
    router = ModelRouter(
        _settings(
            model_provider="anthropic",
            anthropic_api_key="shared-key",
            anthropic_api_key_mini="mini-only-key",
        )
    )
    assert router._anthropic_config_for("mini") == "mini-only-key"
    assert router._anthropic_config_for("nano") == "shared-key"


def test_model_name_resolves_the_anthropic_model():
    router = ModelRouter(_settings(model_provider="anthropic", anthropic_model_mini="claude-opus-4-5"))
    assert router.model_name("mini") == "claude-opus-4-5"


def test_model_name_is_empty_for_anthropic_with_no_model_configured():
    router = ModelRouter(_settings(model_provider="anthropic", anthropic_api_key="sk-ant-test"))
    assert router.model_name("mini") == ""


# ---------------------------------------------------------------------------
# Request translation — `_anthropic_messages` / `_anthropic_tools` /
# `_anthropic_tool_choice` / `_anthropic_request`
# ---------------------------------------------------------------------------


def test_system_message_becomes_the_top_level_system_param():
    system, messages = _anthropic_messages(
        [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "hi"}]
    )
    assert system == "Be helpful."
    assert messages == [{"role": "user", "content": "hi"}]


def test_a_second_system_message_is_folded_in_as_a_user_turn_not_dropped():
    system, messages = _anthropic_messages(
        [
            {"role": "system", "content": "First."},
            {"role": "system", "content": "Second."},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "First."
    assert messages == [{"role": "user", "content": "Second."}, {"role": "user", "content": "hi"}]


def test_tool_result_message_becomes_a_user_turn_with_a_tool_result_block():
    msg = tool_result_message("call_1", "the result")
    _, messages = _anthropic_messages([msg])
    assert messages == [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "the result"}]}
    ]


def test_assistant_tool_call_message_round_trips_through_the_translation():
    """`assistant_tool_call_message` (what the orchestrator appends to
    history) -> Anthropic assistant blocks -> and the arguments must survive
    as the same dict, not a mangled string."""
    from app.services.model_router import ToolCall

    call = ToolCall(id="call_1", name="click", arguments={"x": 10, "y": 20}, raw_arguments=json.dumps({"x": 10, "y": 20}))
    msg = assistant_tool_call_message("I'll click that.", [call])
    _, messages = _anthropic_messages([msg])
    assert messages == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll click that."},
                {"type": "tool_use", "id": "call_1", "name": "click", "input": {"x": 10, "y": 20}},
            ],
        }
    ]


def test_assistant_tool_call_message_with_no_text_has_no_text_block():
    from app.services.model_router import ToolCall

    call = ToolCall(id="call_1", name="click", arguments={}, raw_arguments="{}")
    msg = assistant_tool_call_message("", [call])
    _, messages = _anthropic_messages([msg])
    assert messages[0]["content"] == [{"type": "tool_use", "id": "call_1", "name": "click", "input": {}}]


def test_plain_user_and_assistant_text_messages_pass_through():
    _, messages = _anthropic_messages([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    assert messages == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]


def test_an_unrecognised_role_becomes_user():
    _, messages = _anthropic_messages([{"role": "developer", "content": "hi"}])
    assert messages[0]["role"] == "user"


def test_image_content_part_is_translated_to_anthropic_shape():
    payload = "aGVsbG8="  # base64 for "hello"
    content = [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}", "detail": "high"}},
    ]
    translated = _anthropic_content(content)
    assert translated == [
        {"type": "text", "text": "look at this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}},
    ]


def test_a_plain_string_content_passes_through_unchanged():
    assert _anthropic_content("just text") == "just text"


def test_tools_are_translated_to_name_description_input_schema():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "click",
                "description": "Click something.",
                "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
            },
        }
    ]
    assert _anthropic_tools(tools) == [
        {
            "name": "click",
            "description": "Click something.",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
        }
    ]


def test_no_tools_is_none_not_an_empty_list():
    assert _anthropic_tools(None) is None
    assert _anthropic_tools([]) is None


@pytest.mark.parametrize(
    "openai_choice,expected",
    [
        (None, None),
        ("auto", {"type": "auto"}),
        ("none", {"type": "none"}),
        ({"type": "function", "function": {"name": "click"}}, {"type": "tool", "name": "click"}),
    ],
)
def test_tool_choice_translation(openai_choice, expected):
    assert _anthropic_tool_choice(openai_choice) == expected


def test_anthropic_request_sets_a_default_max_tokens():
    request = _anthropic_request(
        {"model": "claude-opus-4-5", "messages": [{"role": "user", "content": "hi"}], "timeout": 60.0}
    )
    assert request["max_tokens"] == ANTHROPIC_DEFAULT_MAX_TOKENS
    assert request["model"] == "claude-opus-4-5"


def test_anthropic_request_never_forwards_the_raw_reasoning_effort_string():
    """`reasoning_effort` itself is an Azure/OpenAI-shaped param name and must
    never appear verbatim on an Anthropic request, mapped or not."""
    request = _anthropic_request(
        {
            "model": "claude-opus-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "timeout": 60.0,
            "reasoning_effort": "high",
        }
    )
    assert "reasoning_effort" not in request


def test_anthropic_request_omits_thinking_for_none_or_unset_effort():
    for effort in (None, "none", "bogus"):
        request = _anthropic_request(
            {
                "model": "claude-opus-4-5",
                "messages": [{"role": "user", "content": "hi"}],
                "timeout": 60.0,
                "reasoning_effort": effort,
            }
        )
        assert "thinking" not in request
        assert request["max_tokens"] == ANTHROPIC_DEFAULT_MAX_TOKENS


@pytest.mark.parametrize(
    ("effort", "expected_budget"),
    [("minimal", 1024), ("low", 2048), ("medium", 4096), ("high", 8192)],
)
def test_anthropic_request_maps_reasoning_effort_to_a_thinking_budget(effort, expected_budget):
    request = _anthropic_request(
        {
            "model": "claude-opus-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "timeout": 60.0,
            "reasoning_effort": effort,
        }
    )
    assert request["thinking"] == {"type": "enabled", "budget_tokens": expected_budget}
    # Anthropic 400s unless max_tokens exceeds the thinking budget.
    assert request["max_tokens"] > expected_budget


def test_anthropic_request_drops_a_forced_tool_choice_when_thinking_is_enabled():
    """Extended thinking only permits `tool_choice: auto` (or none) - a
    forced choice would otherwise ship a request Anthropic rejects outright."""
    request = _anthropic_request(
        {
            "model": "claude-opus-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "timeout": 60.0,
            "reasoning_effort": "high",
            "tools": [{"type": "function", "function": {"name": "click", "parameters": {}}}],
            "tool_choice": {"type": "function", "function": {"name": "click"}},
        }
    )
    assert "thinking" in request
    assert "tool_choice" not in request


def test_anthropic_request_keeps_an_auto_tool_choice_when_thinking_is_enabled():
    request = _anthropic_request(
        {
            "model": "claude-opus-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "timeout": 60.0,
            "reasoning_effort": "medium",
            "tools": [{"type": "function", "function": {"name": "click", "parameters": {}}}],
            "tool_choice": "auto",
        }
    )
    assert request["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert request["tool_choice"] == {"type": "auto"}


def test_anthropic_request_omits_tools_and_tool_choice_when_absent():
    request = _anthropic_request(
        {"model": "claude-opus-4-5", "messages": [{"role": "user", "content": "hi"}], "timeout": 60.0}
    )
    assert "tools" not in request
    assert "tool_choice" not in request


# ---------------------------------------------------------------------------
# Response translation — `_anthropic_response_to_openai_shape`
# ---------------------------------------------------------------------------


def _fake_text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _fake_tool_use_block(call_id: str, name: str, input_: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=input_)


def _fake_usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=cache_read_input_tokens
    )


def test_text_only_response_translates_cleanly():
    message = SimpleNamespace(content=[_fake_text_block("hello there")], usage=_fake_usage(10, 5))
    resp = _anthropic_response_to_openai_shape(message)
    assert resp.choices[0].message.content == "hello there"
    assert resp.choices[0].message.tool_calls is None
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 5
    assert resp.usage.total_tokens == 15


def test_multiple_text_blocks_concatenate():
    message = SimpleNamespace(content=[_fake_text_block("a"), _fake_text_block("b")], usage=_fake_usage())
    resp = _anthropic_response_to_openai_shape(message)
    assert resp.choices[0].message.content == "ab"


def test_tool_use_block_becomes_a_tool_call_with_json_arguments():
    message = SimpleNamespace(
        content=[_fake_tool_use_block("toolu_1", "click", {"x": 10, "y": 20})], usage=_fake_usage()
    )
    resp = _anthropic_response_to_openai_shape(message)
    call = resp.choices[0].message.tool_calls[0]
    assert call.id == "toolu_1"
    assert call.function.name == "click"
    assert json.loads(call.function.arguments) == {"x": 10, "y": 20}

    # And it round-trips through the same parser a real OpenAI response uses.
    parsed = parse_tool_calls(resp.choices[0].message)
    assert parsed[0].name == "click"
    assert parsed[0].arguments == {"x": 10, "y": 20}
    assert parsed[0].parse_error is None


def test_mixed_text_and_tool_use_blocks():
    message = SimpleNamespace(
        content=[_fake_text_block("I'll click it."), _fake_tool_use_block("toolu_1", "click", {})],
        usage=_fake_usage(),
    )
    resp = _anthropic_response_to_openai_shape(message)
    assert resp.choices[0].message.content == "I'll click it."
    assert len(resp.choices[0].message.tool_calls) == 1


def test_cache_read_tokens_map_onto_prompt_tokens_details():
    message = SimpleNamespace(content=[_fake_text_block("hi")], usage=_fake_usage(cache_read_input_tokens=7))
    resp = _anthropic_response_to_openai_shape(message)
    assert resp.usage.prompt_tokens_details.cached_tokens == 7

    from app.services.model_router import cached_prompt_tokens

    assert cached_prompt_tokens(resp.usage) == 7


# ---------------------------------------------------------------------------
# Streaming translation — `_anthropic_stream_to_openai_chunks`
# ---------------------------------------------------------------------------


def _message_start(input_tokens=10, cache_read_input_tokens=0) -> SimpleNamespace:
    return SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(usage=_fake_usage(input_tokens=input_tokens, cache_read_input_tokens=cache_read_input_tokens)),
    )


def _text_block_start(index: int) -> SimpleNamespace:
    return SimpleNamespace(type="content_block_start", index=index, content_block=SimpleNamespace(type="text"))


def _text_delta(index: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(type="content_block_delta", index=index, delta=SimpleNamespace(type="text_delta", text=text))


def _tool_use_start(index: int, call_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_start", index=index, content_block=SimpleNamespace(type="tool_use", id=call_id, name=name)
    )


def _input_json_delta(index: int, fragment: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta", index=index, delta=SimpleNamespace(type="input_json_delta", partial_json=fragment)
    )


def _content_block_stop(index: int) -> SimpleNamespace:
    return SimpleNamespace(type="content_block_stop", index=index)


def _message_delta(output_tokens: int) -> SimpleNamespace:
    # Realistic shape: `message_delta` usage typically carries only
    # `output_tokens` (input_tokens/cache counts were already given on
    # `message_start`), which is exactly the case
    # `test_message_start_input_tokens_survive_...` below exercises on
    # purpose. `None`, not `_fake_usage()`'s default, so the fallback-to-
    # message_start path this test suite cares about is what actually runs.
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason="end_turn"),
        usage=SimpleNamespace(output_tokens=output_tokens, input_tokens=None, cache_read_input_tokens=None),
    )


def _message_stop() -> SimpleNamespace:
    return SimpleNamespace(type="message_stop")


async def _aiter(items):
    for item in items:
        yield item


async def test_text_only_stream_yields_content_deltas():
    events = _aiter(
        [
            _message_start(input_tokens=12),
            _text_block_start(0),
            _text_delta(0, "hel"),
            _text_delta(0, "lo"),
            _content_block_stop(0),
            _message_delta(output_tokens=3),
            _message_stop(),
        ]
    )
    chunks = [c async for c in _anthropic_stream_to_openai_chunks(events)]
    text_chunks = [c for c in chunks if c.choices and c.choices[0].delta.content]
    assert [c.choices[0].delta.content for c in text_chunks] == ["hel", "lo"]

    usage_chunks = [c for c in chunks if c.usage is not None]
    assert usage_chunks, "message_delta must yield a usage-bearing chunk"
    final = usage_chunks[-1]
    assert final.usage.prompt_tokens == 12
    assert final.usage.completion_tokens == 3


async def test_tool_use_stream_reconstructs_the_full_call_via_the_real_accumulator():
    """Feeds the translated chunks through the exact same
    `_accumulate_tool_call_deltas`/`_finish_tool_call_deltas` the router's
    `stream_chat` uses - proving the chunk shape this adapter yields is
    compatible with the real folding logic, not just superficially similar."""
    events = _aiter(
        [
            _message_start(),
            _tool_use_start(0, "toolu_1", "click"),
            _input_json_delta(0, '{"x": '),
            _input_json_delta(0, "10}"),
            _content_block_stop(0),
            _message_delta(output_tokens=2),
            _message_stop(),
        ]
    )
    buffer: dict[int, dict] = {}
    async for chunk in _anthropic_stream_to_openai_chunks(events):
        for choice in chunk.choices or []:
            delta = getattr(choice, "delta", None)
            if delta is not None:
                _accumulate_tool_call_deltas(buffer, delta)

    calls = _finish_tool_call_deltas(buffer)
    assert len(calls) == 1
    assert calls[0].id == "toolu_1"
    assert calls[0].name == "click"
    assert calls[0].arguments == {"x": 10}
    assert calls[0].parse_error is None


async def test_message_start_input_tokens_survive_to_the_final_usage_chunk_even_if_message_delta_omits_them():
    events = _aiter(
        [
            _message_start(input_tokens=42),
            _text_block_start(0),
            _text_delta(0, "hi"),
            SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"), usage=SimpleNamespace(output_tokens=3, input_tokens=None, cache_read_input_tokens=None)),
            _message_stop(),
        ]
    )
    chunks = [c async for c in _anthropic_stream_to_openai_chunks(events)]
    final_usage = [c for c in chunks if c.usage is not None][-1].usage
    assert final_usage.prompt_tokens == 42
    assert final_usage.completion_tokens == 3


# ---------------------------------------------------------------------------
# End to end — through `ModelRouter.chat()` / `.stream_chat()` with a bot
# pinned to anthropic, fake client injected. No network access.
# ---------------------------------------------------------------------------


class _FakeAnthropicMessages:
    def __init__(self, response=None, stream_events=None):
        self._response = response
        self._stream_events = stream_events or []
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _aiter(self._stream_events)
        return self._response


def _fake_anthropic_adapter(response=None, stream_events=None):
    from app.services.model_router import _AnthropicAdapter

    fake_messages = _FakeAnthropicMessages(response=response, stream_events=stream_events)
    return _AnthropicAdapter(SimpleNamespace(messages=fake_messages)), fake_messages


async def test_chat_reaches_a_pinned_anthropic_bot_end_to_end():
    router = ModelRouter(_settings())
    bot = _bot(model_provider="anthropic", model_name="claude-opus-4-5")
    adapter, fake = _fake_anthropic_adapter(
        response=SimpleNamespace(content=[_fake_text_block("Done.")], usage=_fake_usage(10, 4))
    )
    router._anthropic_client = lambda tier: adapter

    result = await router.chat(
        task="agent_turn",
        messages=[{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "go"}],
        bot=bot,
    )
    assert result.content == "Done."
    assert fake.calls[0]["model"] == "claude-opus-4-5"
    assert fake.calls[0]["system"] == "Be helpful."


async def test_stream_chat_reaches_a_pinned_anthropic_bot_end_to_end():
    router = ModelRouter(_settings())
    bot = _bot(model_provider="anthropic", model_name="claude-opus-4-5")
    adapter, fake = _fake_anthropic_adapter(
        stream_events=[
            _message_start(input_tokens=8),
            _text_block_start(0),
            _text_delta(0, "hi"),
            _text_delta(0, " there"),
            _content_block_stop(0),
            _message_delta(output_tokens=2),
            _message_stop(),
        ]
    )
    router._anthropic_client = lambda tier: adapter

    chunks = [c async for c in router.stream_chat(task="agent_turn", messages=[{"role": "user", "content": "go"}], bot=bot)]
    assert chunks == ["hi", " there"]
    assert router.last_result is not None
    assert router.last_result.content == "hi there"
    assert fake.calls[0]["model"] == "claude-opus-4-5"


async def test_chat_with_tool_calls_reaches_the_orchestrator_shaped_result():
    router = ModelRouter(_settings())
    bot = _bot(model_provider="anthropic", model_name="claude-opus-4-5")
    adapter, fake = _fake_anthropic_adapter(
        response=SimpleNamespace(
            content=[_fake_tool_use_block("toolu_1", "click", {"x": 5})], usage=_fake_usage(20, 8)
        )
    )
    router._anthropic_client = lambda tier: adapter

    tools = [{"type": "function", "function": {"name": "click", "description": "click", "parameters": {}}}]
    result = await router.chat(
        task="deep_plan", messages=[{"role": "user", "content": "click the button"}], tools=tools, bot=bot
    )
    assert result.tool_calls[0].name == "click"
    assert result.tool_calls[0].arguments == {"x": 5}
    assert fake.calls[0]["tools"] == [{"name": "click", "description": "click", "input_schema": {}}]
