"""Google (Gemini) — the third provider whose wire format genuinely differs
from OpenAI's, translated at the edges by `_GoogleAdapter` and the
`_google_*` module functions in `model_router.py`.

No live Google account is used or needed anywhere in this file. The
request/response/streaming shapes asserted against were read directly off the
installed `google-genai` 2.20.0 SDK's own pydantic type definitions
(`Content`, `Part`, `FunctionCall`, `FunctionDeclaration`, `Tool`,
`ToolConfig`, `FunctionCallingConfig`, `ThinkingConfig`, `ThinkingLevel`,
`GenerateContentResponse`, `Candidate`, `GenerateContentResponseUsageMetadata`)
— not guessed, not copied from memory of a different SDK generation. Built
against `models.generate_content`/`generate_content_stream`, the mature,
strongly-typed entry point; the SDK also exposes a newer, loosely-typed
`client.interactions.create` surface that this adapter deliberately does not
use.
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
    ModelRouter,
    _accumulate_tool_call_deltas,
    _finish_tool_call_deltas,
    _google_content,
    _google_messages,
    _google_request,
    _google_response_to_openai_shape,
    _google_stream_to_openai_chunks,
    _google_thinking_config,
    _google_tool_config,
    _google_tools,
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


def test_google_with_no_key_is_mock():
    router = ModelRouter(_settings(model_provider="google"))
    assert router.client("mini") is None
    assert router.auth_mode == "mock"


def test_google_with_a_key_builds_a_real_adapter():
    router = ModelRouter(_settings(model_provider="google", google_api_key="AIza-test"))
    client = router.client("mini")
    assert client is not None
    assert router.auth_mode == "api_key"
    assert hasattr(client, "chat")
    assert hasattr(client.chat.completions, "create")


def test_google_client_is_cached_per_key():
    router = ModelRouter(_settings(model_provider="google", google_api_key="AIza-test"))
    first = router.client("mini")
    second = router.client("nano")
    assert first is second


def test_google_per_tier_key_overrides_the_shared_one():
    router = ModelRouter(
        _settings(model_provider="google", google_api_key="shared-key", google_api_key_mini="mini-only-key")
    )
    assert router._google_config_for("mini") == "mini-only-key"
    assert router._google_config_for("nano") == "shared-key"


def test_model_name_resolves_the_google_model():
    router = ModelRouter(_settings(model_provider="google", google_model_mini="gemini-3.5-flash"))
    assert router.model_name("mini") == "gemini-3.5-flash"


def test_model_name_is_empty_for_google_with_no_model_configured():
    router = ModelRouter(_settings(model_provider="google", google_api_key="AIza-test"))
    assert router.model_name("mini") == ""


# ---------------------------------------------------------------------------
# Request translation — `_google_messages` / `_google_tools` /
# `_google_tool_config` / `_google_thinking_config` / `_google_request`
# ---------------------------------------------------------------------------


def test_system_message_becomes_system_instruction():
    system, contents = _google_messages(
        [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "hi"}]
    )
    assert system == "Be helpful."
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_a_second_system_message_is_folded_in_as_a_user_turn_not_dropped():
    system, contents = _google_messages(
        [
            {"role": "system", "content": "First."},
            {"role": "system", "content": "Second."},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "First."
    assert contents == [
        {"role": "user", "parts": [{"text": "Second."}]},
        {"role": "user", "parts": [{"text": "hi"}]},
    ]


def test_assistant_role_becomes_model_gemini_has_no_assistant_role():
    _, contents = _google_messages([{"role": "assistant", "content": "hello"}])
    assert contents == [{"role": "model", "parts": [{"text": "hello"}]}]


def test_an_unrecognised_role_becomes_user():
    _, contents = _google_messages([{"role": "developer", "content": "hi"}])
    assert contents[0]["role"] == "user"


def test_tool_result_message_becomes_a_user_turn_with_a_function_response_part():
    msg = tool_result_message("call_1", "the result")
    _, contents = _google_messages([msg])
    assert contents == [
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": "call_1",
                        "name": "",
                        "response": {"output": "the result"},
                    }
                }
            ],
        }
    ]


def test_assistant_tool_call_message_round_trips_through_the_translation():
    """`assistant_tool_call_message` (what the orchestrator appends to
    history) -> Gemini model-role function_call parts -> and the arguments
    must survive as the same dict, not a mangled string."""
    from app.services.model_router import ToolCall

    call = ToolCall(id="call_1", name="click", arguments={"x": 10, "y": 20}, raw_arguments=json.dumps({"x": 10, "y": 20}))
    msg = assistant_tool_call_message("I'll click that.", [call])
    _, contents = _google_messages([msg])
    assert contents == [
        {
            "role": "model",
            "parts": [
                {"text": "I'll click that."},
                {"function_call": {"id": "call_1", "name": "click", "args": {"x": 10, "y": 20}}},
            ],
        }
    ]


def test_assistant_tool_call_message_with_no_text_has_no_text_part():
    from app.services.model_router import ToolCall

    call = ToolCall(id="call_1", name="click", arguments={}, raw_arguments="{}")
    msg = assistant_tool_call_message("", [call])
    _, contents = _google_messages([msg])
    assert contents[0]["parts"] == [{"function_call": {"id": "call_1", "name": "click", "args": {}}}]


def test_image_content_part_is_translated_to_google_shape():
    payload = "aGVsbG8="  # base64 for "hello"
    content = [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}", "detail": "high"}},
    ]
    translated = _google_content(content)
    assert translated == [
        {"text": "look at this"},
        {"inline_data": {"data": b"hello", "mime_type": "image/png"}},
    ]


def test_a_plain_string_content_becomes_one_text_part():
    assert _google_content("just text") == [{"text": "just text"}]


def test_tools_are_grouped_under_one_tool_with_function_declarations():
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
    assert _google_tools(tools) == [
        {
            "function_declarations": [
                {
                    "name": "click",
                    "description": "Click something.",
                    "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
                }
            ]
        }
    ]


def test_no_tools_is_none_not_an_empty_list():
    assert _google_tools(None) is None
    assert _google_tools([]) is None


@pytest.mark.parametrize(
    "openai_choice,expected",
    [
        (None, None),
        ("auto", None),
        ("none", {"function_calling_config": {"mode": "NONE"}}),
        (
            {"type": "function", "function": {"name": "click"}},
            {"function_calling_config": {"mode": "ANY", "allowed_function_names": ["click"]}},
        ),
    ],
)
def test_tool_choice_translation(openai_choice, expected):
    assert _google_tool_config(openai_choice) == expected


@pytest.mark.parametrize("effort", [None, "none", "bogus"])
def test_thinking_config_omitted_for_none_or_unset_effort(effort):
    assert _google_thinking_config(effort) is None


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high"])
def test_thinking_config_maps_directly_to_the_thinking_level_enum(effort):
    """Gemini's ThinkingLevel enum values are literally MINIMAL/LOW/MEDIUM/HIGH
    - the same vocabulary REASONING_EFFORTS already uses, just upper-cased."""
    assert _google_thinking_config(effort) == {"thinking_level": effort.upper()}


def test_google_request_has_no_default_max_output_tokens():
    """Unlike Anthropic, Gemini does not require max_output_tokens - setting
    one anyway would risk truncating a real reply for no justified reason."""
    request = _google_request(
        {"model": "gemini-3.5-flash", "messages": [{"role": "user", "content": "hi"}], "timeout": 60.0}
    )
    assert "max_output_tokens" not in request["config"]
    assert request["model"] == "gemini-3.5-flash"
    assert request["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_google_request_omits_tools_and_tool_config_when_absent():
    request = _google_request(
        {"model": "gemini-3.5-flash", "messages": [{"role": "user", "content": "hi"}], "timeout": 60.0}
    )
    assert "tools" not in request["config"]
    assert "tool_config" not in request["config"]
    assert "thinking_config" not in request["config"]


def test_google_request_carries_system_instruction_when_present():
    request = _google_request(
        {
            "model": "gemini-3.5-flash",
            "messages": [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "hi"}],
            "timeout": 60.0,
        }
    )
    assert request["config"]["system_instruction"] == "Be helpful."


def test_google_request_maps_reasoning_effort_into_the_config():
    request = _google_request(
        {
            "model": "gemini-3.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "timeout": 60.0,
            "reasoning_effort": "high",
        }
    )
    assert request["config"]["thinking_config"] == {"thinking_level": "HIGH"}


def test_google_request_never_forwards_the_raw_reasoning_effort_string():
    request = _google_request(
        {
            "model": "gemini-3.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "timeout": 60.0,
            "reasoning_effort": "high",
        }
    )
    assert "reasoning_effort" not in request
    assert "reasoning_effort" not in request["config"]


# ---------------------------------------------------------------------------
# Response translation — `_google_response_to_openai_shape`
# ---------------------------------------------------------------------------


def _fake_text_part(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, function_call=None)


def _fake_function_call_part(call_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(text=None, function_call=SimpleNamespace(id=call_id, name=name, args=args))


def _fake_usage_metadata(prompt_token_count=10, candidates_token_count=5, total_token_count=None, cached=0) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=prompt_token_count,
        candidates_token_count=candidates_token_count,
        total_token_count=total_token_count if total_token_count is not None else prompt_token_count + candidates_token_count,
        cached_content_token_count=cached,
    )


def _fake_response(parts: list, usage: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
        usage_metadata=usage or _fake_usage_metadata(),
    )


def test_text_only_response_translates_cleanly():
    resp = _google_response_to_openai_shape(
        _fake_response([_fake_text_part("hello there")], _fake_usage_metadata(10, 5))
    )
    assert resp.choices[0].message.content == "hello there"
    assert resp.choices[0].message.tool_calls is None
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 5
    assert resp.usage.total_tokens == 15


def test_multiple_text_parts_concatenate():
    resp = _google_response_to_openai_shape(
        _fake_response([_fake_text_part("a"), _fake_text_part("b")])
    )
    assert resp.choices[0].message.content == "ab"


def test_function_call_part_becomes_a_tool_call_with_json_arguments():
    resp = _google_response_to_openai_shape(
        _fake_response([_fake_function_call_part("call_1", "click", {"x": 10, "y": 20})])
    )
    call = resp.choices[0].message.tool_calls[0]
    assert call.id == "call_1"
    assert call.function.name == "click"
    assert json.loads(call.function.arguments) == {"x": 10, "y": 20}

    # Round-trips through the same parser a real OpenAI response uses.
    parsed = parse_tool_calls(resp.choices[0].message)
    assert parsed[0].name == "click"
    assert parsed[0].arguments == {"x": 10, "y": 20}
    assert parsed[0].parse_error is None


def test_mixed_text_and_function_call_parts():
    resp = _google_response_to_openai_shape(
        _fake_response([_fake_text_part("I'll click it."), _fake_function_call_part("call_1", "click", {})])
    )
    assert resp.choices[0].message.content == "I'll click it."
    assert len(resp.choices[0].message.tool_calls) == 1


def test_cached_tokens_map_onto_prompt_tokens_details():
    resp = _google_response_to_openai_shape(
        _fake_response([_fake_text_part("hi")], _fake_usage_metadata(cached=7))
    )
    assert resp.usage.prompt_tokens_details.cached_tokens == 7

    from app.services.model_router import cached_prompt_tokens

    assert cached_prompt_tokens(resp.usage) == 7


def test_no_candidates_does_not_crash():
    resp = _google_response_to_openai_shape(SimpleNamespace(candidates=[], usage_metadata=_fake_usage_metadata()))
    assert resp.choices[0].message.content == ""
    assert resp.choices[0].message.tool_calls is None


# ---------------------------------------------------------------------------
# Streaming translation — `_google_stream_to_openai_chunks`
# ---------------------------------------------------------------------------


async def _aiter(items):
    for item in items:
        yield item


def _stream_chunk(parts: list, usage: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
        usage_metadata=usage,
    )


async def test_text_only_stream_yields_content_deltas():
    events = _aiter(
        [
            _stream_chunk([_fake_text_part("hel")]),
            _stream_chunk([_fake_text_part("lo")], usage=_fake_usage_metadata(12, 3)),
        ]
    )
    chunks = [c async for c in _google_stream_to_openai_chunks(events)]
    text_chunks = [c for c in chunks if c.choices and c.choices[0].delta.content]
    assert [c.choices[0].delta.content for c in text_chunks] == ["hel", "lo"]

    usage_chunks = [c for c in chunks if c.usage is not None]
    assert usage_chunks, "the stream must yield a trailing usage-bearing chunk"
    final = usage_chunks[-1]
    assert final.usage.prompt_tokens == 12
    assert final.usage.completion_tokens == 3


async def test_function_call_stream_reconstructs_the_full_call_via_the_real_accumulator():
    """Feeds the translated chunks through the exact same
    `_accumulate_tool_call_deltas`/`_finish_tool_call_deltas` the router's
    `stream_chat` uses - proving the chunk shape this adapter yields is
    compatible with the real folding logic, not just superficially similar.
    Unlike Anthropic, Gemini hands back complete arguments in one part rather
    than fragmenting them, so a single chunk carries the whole call."""
    events = _aiter(
        [
            _stream_chunk([_fake_function_call_part("call_1", "click", {"x": 10})], usage=_fake_usage_metadata(8, 2)),
        ]
    )
    buffer: dict[int, dict] = {}
    async for chunk in _google_stream_to_openai_chunks(events):
        for choice in chunk.choices or []:
            delta = getattr(choice, "delta", None)
            if delta is not None:
                _accumulate_tool_call_deltas(buffer, delta)

    calls = _finish_tool_call_deltas(buffer)
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "click"
    assert calls[0].arguments == {"x": 10}
    assert calls[0].parse_error is None


async def test_parallel_function_calls_in_one_chunk_get_distinct_indices():
    events = _aiter(
        [
            _stream_chunk(
                [
                    _fake_function_call_part("call_1", "click", {"x": 1}),
                    _fake_function_call_part("call_2", "type", {"text": "hi"}),
                ],
                usage=_fake_usage_metadata(),
            ),
        ]
    )
    buffer: dict[int, dict] = {}
    async for chunk in _google_stream_to_openai_chunks(events):
        for choice in chunk.choices or []:
            delta = getattr(choice, "delta", None)
            if delta is not None:
                _accumulate_tool_call_deltas(buffer, delta)

    calls = _finish_tool_call_deltas(buffer)
    assert len(calls) == 2
    assert {c.name for c in calls} == {"click", "type"}


async def test_usage_missing_on_earlier_chunks_still_survives_to_the_final_chunk():
    events = _aiter(
        [
            _stream_chunk([_fake_text_part("hi")], usage=None),
            _stream_chunk([], usage=_fake_usage_metadata(20, 4)),
        ]
    )
    chunks = [c async for c in _google_stream_to_openai_chunks(events)]
    final_usage = [c for c in chunks if c.usage is not None][-1].usage
    assert final_usage.prompt_tokens == 20
    assert final_usage.completion_tokens == 4


# ---------------------------------------------------------------------------
# End to end — through `ModelRouter.chat()` / `.stream_chat()` with a bot
# pinned to google, fake client injected. No network access.
# ---------------------------------------------------------------------------


class _FakeGoogleModels:
    def __init__(self, response=None, stream_events=None):
        self._response = response
        self._stream_events = stream_events or []
        self.calls: list[dict] = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self._response

    def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        return _aiter(self._stream_events)


def _fake_google_adapter(response=None, stream_events=None):
    from app.services.model_router import _GoogleAdapter

    fake_models = _FakeGoogleModels(response=response, stream_events=stream_events)
    fake_client = SimpleNamespace(aio=SimpleNamespace(models=fake_models))
    return _GoogleAdapter(fake_client), fake_models


async def test_chat_reaches_a_pinned_google_bot_end_to_end():
    router = ModelRouter(_settings())
    bot = _bot(model_provider="google", model_name="gemini-3.5-flash")
    adapter, fake = _fake_google_adapter(
        response=_fake_response([_fake_text_part("Done.")], _fake_usage_metadata(10, 4))
    )
    router._google_client = lambda tier: adapter

    result = await router.chat(
        task="agent_turn",
        messages=[{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "go"}],
        bot=bot,
    )
    assert result.content == "Done."
    assert fake.calls[0]["model"] == "gemini-3.5-flash"
    assert fake.calls[0]["config"]["system_instruction"] == "Be helpful."


async def test_stream_chat_reaches_a_pinned_google_bot_end_to_end():
    router = ModelRouter(_settings())
    bot = _bot(model_provider="google", model_name="gemini-3.5-flash")
    adapter, fake = _fake_google_adapter(
        stream_events=[
            _stream_chunk([_fake_text_part("hi")]),
            _stream_chunk([_fake_text_part(" there")], usage=_fake_usage_metadata(8, 2)),
        ]
    )
    router._google_client = lambda tier: adapter

    chunks = [c async for c in router.stream_chat(task="agent_turn", messages=[{"role": "user", "content": "go"}], bot=bot)]
    assert chunks == ["hi", " there"]
    assert router.last_result is not None
    assert router.last_result.content == "hi there"
    assert fake.calls[0]["model"] == "gemini-3.5-flash"


async def test_chat_with_tool_calls_reaches_the_orchestrator_shaped_result():
    router = ModelRouter(_settings())
    bot = _bot(model_provider="google", model_name="gemini-3.5-flash")
    adapter, fake = _fake_google_adapter(
        response=_fake_response([_fake_function_call_part("call_1", "click", {"x": 5})], _fake_usage_metadata(20, 8))
    )
    router._google_client = lambda tier: adapter

    tools = [{"type": "function", "function": {"name": "click", "description": "click", "parameters": {}}}]
    result = await router.chat(
        task="deep_plan", messages=[{"role": "user", "content": "click the button"}], tools=tools, bot=bot
    )
    assert result.tool_calls[0].name == "click"
    assert result.tool_calls[0].arguments == {"x": 5}
    assert fake.calls[0]["config"]["tools"] == [
        {"function_declarations": [{"name": "click", "description": "click", "parameters": {}}]}
    ]


async def test_chat_bills_a_pinned_google_bot_at_its_models_real_price():
    router = ModelRouter(_settings())
    bot = _bot(model_provider="google", model_name="gemini-2.5-flash")  # $0.30/$2.50 per 1M
    adapter, _fake = _fake_google_adapter(
        response=_fake_response(
            [_fake_text_part("ok")], _fake_usage_metadata(1_000_000, 1_000_000, total_token_count=2_000_000)
        )
    )
    router._google_client = lambda tier: adapter

    result = await router.chat(task="agent_turn", messages=[{"role": "user", "content": "hi"}], bot=bot)
    assert float(result.cost_usd) == pytest.approx(0.30 + 2.50)
