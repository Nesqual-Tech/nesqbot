"""Multi-provider dispatch — `azure` (unchanged), `openai`, and the two that
are accepted config but have no client yet (`anthropic`, `google`).

`AsyncOpenAI` and `AsyncAzureOpenAI` share one response shape, so this file
only tests what actually differs per provider: which client class gets built,
with what credentials, and what happens when nothing is configured. Everything
downstream (`_request_kwargs`, streaming, tool-call parsing, cost accounting)
is already covered against the Azure path and does not care which client
produced the response.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.config import Settings
from app.models import Bot
from app.services.model_router import (
    _LOGGED_AUTH_MODES,
    _WARNED_OFF_DEFAULT,
    ModelRouter,
)


def _bot(**overrides: Any) -> Bot:
    return Bot(
        slug="test-bot",
        name="Test",
        role="",
        system_prompt="",
        is_system=False,
        **overrides,
    )


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "azure_openai_endpoint": "",
        "azure_openai_api_key": "",
        "azure_managed_identity_client_id": "",
    }
    return Settings(**{**base, **overrides})


@pytest.fixture(autouse=True)
def _reset_process_state():
    _LOGGED_AUTH_MODES.clear()
    _WARNED_OFF_DEFAULT.clear()
    yield
    _LOGGED_AUTH_MODES.clear()
    _WARNED_OFF_DEFAULT.clear()


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def test_the_default_provider_is_azure_and_nothing_changed_for_it():
    router = ModelRouter(_settings())
    assert router._provider_for("mini") == "azure"
    assert router._provider_for(None) == "azure"


def test_an_unknown_provider_value_falls_back_to_azure_rather_than_crashing():
    router = ModelRouter(_settings(model_provider="bedrock"))
    assert router._provider_for("mini") == "azure"


def test_a_per_tier_override_beats_the_global_default():
    router = ModelRouter(_settings(model_provider="azure", model_provider_mini="openai"))
    assert router._provider_for("mini") == "openai"
    assert router._provider_for("nano") == "azure"


def test_tier_none_ignores_the_per_tier_override_same_as_endpoint_for():
    """Matches `_endpoint_for(None)`'s documented "default account only" rule."""
    router = ModelRouter(_settings(model_provider="azure", model_provider_mini="openai"))
    assert router._provider_for(None) == "azure"


# ---------------------------------------------------------------------------
# openai provider — client construction
# ---------------------------------------------------------------------------


def test_openai_with_no_key_and_no_base_url_is_mock():
    router = ModelRouter(_settings(model_provider="openai"))
    assert router.client("mini") is None
    assert router.auth_mode == "mock"


def test_openai_with_an_api_key_builds_a_real_openai_client():
    router = ModelRouter(_settings(model_provider="openai", openai_api_key="sk-test"))
    client = router.client("mini")
    assert isinstance(client, AsyncOpenAI)
    assert not isinstance(client, AsyncAzureOpenAI)
    assert router.auth_mode == "api_key"


def test_openai_base_url_with_no_key_is_unauthenticated_not_mock():
    """A self-hosted OpenAI-compatible server ("local models") - most accept
    any non-empty key, so one is sent rather than refusing to try."""
    router = ModelRouter(
        _settings(model_provider="openai", openai_base_url="http://localhost:11434/v1")
    )
    client = router.client("mini")
    assert isinstance(client, AsyncOpenAI)
    assert router.auth_mode == "unauthenticated"


def test_openai_client_is_cached_per_base_url():
    router = ModelRouter(_settings(model_provider="openai", openai_api_key="sk-test"))
    first = router.client("mini")
    second = router.client("nano")
    assert first is second  # same (empty) base_url -> same cache key


def test_openai_and_azure_clients_never_collide_in_the_cache():
    router = ModelRouter(
        _settings(
            azure_openai_endpoint="https://acct.openai.azure.com/",
            azure_openai_api_key="azure-key",
            model_provider="azure",
            model_provider_mini="openai",
            openai_api_key="sk-test",
        )
    )
    azure_client = router.client("nano")
    openai_client = router.client("mini")
    assert isinstance(azure_client, AsyncAzureOpenAI)
    assert isinstance(openai_client, AsyncOpenAI)
    assert not isinstance(openai_client, AsyncAzureOpenAI)


def test_per_tier_openai_base_url_and_key_override_the_shared_ones():
    router = ModelRouter(
        _settings(
            model_provider="openai",
            openai_base_url="http://shared:11434/v1",
            openai_api_key="shared-key",
            openai_base_url_mini="http://mini-only:8000/v1",
        )
    )
    url, key = router._openai_config_for("mini")
    assert url == "http://mini-only:8000/v1"
    assert key == "shared-key"  # not overridden for this tier -> inherits shared


# ---------------------------------------------------------------------------
# model_name resolution
# ---------------------------------------------------------------------------


def test_model_name_resolves_the_openai_model_for_the_active_provider():
    router = ModelRouter(_settings(model_provider="openai", openai_model_mini="gpt-5.1"))
    assert router.model_name("mini") == "gpt-5.1"


def test_model_name_is_empty_for_openai_with_no_model_configured():
    router = ModelRouter(_settings(model_provider="openai"))
    assert router.model_name("mini") == ""


def test_model_name_still_resolves_azure_deployments_unchanged():
    router = ModelRouter(_settings())
    assert router.model_name("mini") == router.settings.azure_deployment_mini


# ---------------------------------------------------------------------------
# anthropic / google — accepted config, honest about having no client yet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["anthropic", "google"])
def test_unimplemented_providers_fall_back_to_mock_not_a_crash(provider):
    router = ModelRouter(_settings(model_provider=provider))
    assert router.client("mini") is None
    assert router.auth_mode == "mock"
    assert router.model_name("mini") == ""


async def test_chat_still_replies_in_mock_mode_under_an_unimplemented_provider():
    """The end-to-end path a caller actually uses - never a crash, never a
    fabricated response, just the same mock envelope an unconfigured Azure
    tier would produce."""
    router = ModelRouter(_settings(model_provider="anthropic"))
    result = await router.chat(task="agent_turn", messages=[{"role": "user", "content": "hi"}])
    assert result.content.startswith("[mock:")
    assert result.tool_calls == []


# ---------------------------------------------------------------------------
# Bot-level override — the thing that makes per-bot config real, not
# decorative. The global router settings stay at their unrelated defaults
# throughout: the whole point is that a bot pins itself to a provider
# regardless of what the tiers are doing.
# ---------------------------------------------------------------------------


def test_no_bot_is_no_override():
    router = ModelRouter(_settings())
    assert router._bot_override(None) is None


def test_a_bot_with_neither_field_set_is_no_override():
    router = ModelRouter(_settings())
    assert router._bot_override(_bot()) is None


def test_a_bot_with_only_provider_set_is_no_override():
    """Half-written state (pre-validation row, or one written outside the
    API) must degrade to tier routing, not crash trying to resolve an empty
    model name."""
    router = ModelRouter(_settings())
    assert router._bot_override(_bot(model_provider="openai")) is None


def test_a_bot_with_only_model_set_is_no_override():
    router = ModelRouter(_settings())
    assert router._bot_override(_bot(model_name="gpt-5.1")) is None


def test_a_bot_with_both_set_is_an_override():
    router = ModelRouter(_settings())
    assert router._bot_override(_bot(model_provider="openai", model_name="gpt-5.1")) == ("openai", "gpt-5.1")


def test_an_unknown_provider_on_a_bot_is_no_override():
    router = ModelRouter(_settings())
    assert router._bot_override(_bot(model_provider="bedrock", model_name="claude")) is None


async def test_chat_uses_the_bots_pinned_provider_and_model_over_the_tier():
    """The global default is azure with nothing configured (mock); the bot
    pins itself to openai and must reach the openai client, not mock."""
    router = ModelRouter(_settings(openai_api_key="sk-test"))
    bot = _bot(model_provider="openai", model_name="gpt-5.1-mega")

    class _Recording:
        def __init__(self):
            self.calls = []
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
            )

    fake = _Recording()
    router._openai_client = lambda tier: fake  # bypass real client construction

    result = await router.chat(task="agent_turn", messages=[{"role": "user", "content": "hi"}], bot=bot)
    assert fake.calls[0]["model"] == "gpt-5.1-mega"
    assert result.content == "ok"


async def test_chat_ignores_the_bot_override_when_incomplete():
    """Only-provider or only-model on the bot must fall through to ordinary
    tier routing (mock here), not half-apply."""
    router = ModelRouter(_settings())
    bot = _bot(model_provider="openai")  # no model_name
    result = await router.chat(task="agent_turn", messages=[{"role": "user", "content": "hi"}], bot=bot)
    assert result.content.startswith("[mock:")


async def test_chat_falls_back_to_mock_when_the_override_provider_has_no_client():
    router = ModelRouter(_settings())  # no openai key/base_url configured
    bot = _bot(model_provider="openai", model_name="gpt-5.1")
    result = await router.chat(task="agent_turn", messages=[{"role": "user", "content": "hi"}], bot=bot)
    assert result.content.startswith("[mock:")


async def test_chat_falls_back_to_mock_for_a_bot_pinned_to_an_unimplemented_provider():
    router = ModelRouter(_settings())
    bot = _bot(model_provider="anthropic", model_name="claude-opus")
    result = await router.chat(task="agent_turn", messages=[{"role": "user", "content": "hi"}], bot=bot)
    assert result.content.startswith("[mock:")


def test_supports_tools_for_falls_back_to_the_plain_property_with_no_bot():
    router = ModelRouter(_settings())
    assert router.supports_tools_for(None) == router.supports_tools


def test_supports_tools_for_is_true_for_a_bot_pinned_to_a_live_provider_even_when_azure_is_dead():
    """The scenario that motivated this method: a fully non-Azure deployment
    where the plain `supports_tools` property (default tier = azure = dead)
    would wrongly read False for every bot."""
    router = ModelRouter(_settings(openai_api_key="sk-test"))  # azure still empty
    bot = _bot(model_provider="openai", model_name="gpt-5.1")
    assert router.supports_tools is False
    assert router.supports_tools_for(bot) is True


async def test_stream_chat_also_honours_the_bot_override():
    router = ModelRouter(_settings(openai_api_key="sk-test"))
    bot = _bot(model_provider="openai", model_name="gpt-5.1-mega")

    class _Chunk:
        def __init__(self, text):
            self.usage = None
            self.choices = [SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))]

    class _Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    class _Recording:
        def __init__(self):
            self.calls = []
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            stream = _Stream()
            stream._items = [_Chunk("hi")]
            return stream

    fake = _Recording()
    router._openai_client = lambda tier: fake

    chunks = [c async for c in router.stream_chat(task="agent_turn", messages=[{"role": "user", "content": "hi"}], bot=bot)]
    assert chunks == ["hi"]
    assert fake.calls[0]["model"] == "gpt-5.1-mega"
