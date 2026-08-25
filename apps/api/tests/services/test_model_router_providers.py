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

from typing import Any

import pytest
from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.config import Settings
from app.services.model_router import (
    _LOGGED_AUTH_MODES,
    _WARNED_OFF_DEFAULT,
    ModelRouter,
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
