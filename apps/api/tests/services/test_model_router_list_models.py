"""`ModelRouter.list_models` — the Builder's model dropdown, live-queried
from the provider rather than hardcoded.

Each provider's client-building method (`_azure_client`, `_openai_client`,
`_anthropic_client`, `_google_client`) is monkeypatched per-instance to a
minimal stand-in exposing only the surface `list_models` actually calls —
`AsyncAzureOpenAI.get()`'s documented escape hatch for undocumented Azure
endpoints, and the three SDKs' own `.models.list()` — so these tests exercise
the dispatch and shaping logic without a real network call or a full fake
SDK client.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.services.model_router import ModelRouter


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "azure_openai_endpoint": "",
        "azure_openai_api_key": "",
        "azure_managed_identity_client_id": "",
    }
    return Settings(**{**base, **overrides})


class _AsyncIter:
    """The minimal shape `async for x in page` needs."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> Any:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeAzureClient:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, path: str, *, cast_to: type, options: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, options))
        return self.body


class _FakeModel:
    def __init__(self, id_: str) -> None:
        self.id = id_


class _FakeModelsResource:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def list(self) -> _AsyncIter:
        return _AsyncIter(self._items)


class _FakeOpenAIClient:
    def __init__(self, ids: list[str]) -> None:
        self.models = _FakeModelsResource([_FakeModel(i) for i in ids])


class _FakeAnthropicRaw:
    def __init__(self, ids: list[str]) -> None:
        self.models = _FakeModelsResource([_FakeModel(i) for i in ids])


class _FakeAnthropicAdapter:
    def __init__(self, ids: list[str]) -> None:
        self.raw = _FakeAnthropicRaw(ids)


class _FakeGoogleModel:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeGoogleAio:
    def __init__(self, names: list[str]) -> None:
        self.models = _FakeGoogleModelsResource(names)


class _FakeGoogleModelsResource:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    async def list(self) -> _AsyncIter:
        return _AsyncIter([_FakeGoogleModel(n) for n in self._names])


class _FakeGoogleRaw:
    def __init__(self, names: list[str]) -> None:
        self.aio = _FakeGoogleAio(names)


class _FakeGoogleAdapter:
    def __init__(self, names: list[str]) -> None:
        self.raw = _FakeGoogleRaw(names)


async def test_azure_hits_the_deployments_endpoint_not_the_model_catalog(monkeypatch):
    # `azure_openai_api_version` set to something the deployments-list
    # endpoint does NOT accept, on purpose - see the pinned-version comment
    # on `_azure_list_all_deployments`. This proves the call ignores the
    # configured chat api-version entirely for this one endpoint.
    router = ModelRouter(_settings(azure_openai_api_version="2024-10-21"))
    fake = _FakeAzureClient({"data": [{"id": "gpt-5.6-luna"}, {"id": "gpt-5.4-mini"}]})
    monkeypatch.setattr(router, "_azure_client", lambda tier: fake)

    models = await router.list_models("azure")

    assert models == ["gpt-5.4-mini", "gpt-5.6-luna"]
    path, options = fake.calls[0]
    assert path == "/deployments"
    assert options["params"]["api-version"] == "2023-03-15-preview"


async def test_azure_drops_deployment_rows_with_no_id(monkeypatch):
    router = ModelRouter(_settings())
    fake = _FakeAzureClient({"data": [{"id": "gpt-5.6-luna"}, {"model": "gpt-5.4-mini"}]})
    monkeypatch.setattr(router, "_azure_client", lambda tier: fake)

    assert await router.list_models("azure") == ["gpt-5.6-luna"]


async def test_azure_raises_when_no_credential_resolves(monkeypatch):
    router = ModelRouter(_settings())
    monkeypatch.setattr(router, "_azure_client", lambda tier: None)

    with pytest.raises(RuntimeError, match="azure"):
        await router.list_models("azure")


async def test_azure_merges_deployments_across_a_tier_override_account(monkeypatch):
    """The real shape of this deployment: `reason` is overridden to a
    separate Foundry resource carrying Grok, invisible to the shared
    account's own `/deployments`. The dropdown has to show both."""
    router = ModelRouter(_settings())
    shared = _FakeAzureClient({"data": [{"id": "gpt-5.6-luna"}, {"id": "gpt-5.4-mini"}]})
    xai = _FakeAzureClient({"data": [{"id": "grok-4-1-fast-reasoning"}, {"id": "grok-4.3"}]})

    def fake_client(tier):
        return xai if tier == "reason" else shared

    monkeypatch.setattr(router, "_azure_client", fake_client)

    models = await router.list_models("azure")

    assert models == ["gpt-5.4-mini", "gpt-5.6-luna", "grok-4-1-fast-reasoning", "grok-4.3"]
    assert len(shared.calls) == 1  # nano/mini/embed all share it - queried once
    assert len(xai.calls) == 1


async def test_azure_queries_a_shared_account_only_once_across_all_tiers(monkeypatch):
    router = ModelRouter(_settings())
    shared = _FakeAzureClient({"data": [{"id": "gpt-5.6-luna"}]})
    monkeypatch.setattr(router, "_azure_client", lambda tier: shared)

    await router.list_models("azure")

    assert len(shared.calls) == 1


async def test_openai_returns_sorted_deduplicated_ids(monkeypatch):
    router = ModelRouter(_settings())
    fake = _FakeOpenAIClient(["gpt-4o", "gpt-4o-mini", "gpt-4o"])
    monkeypatch.setattr(router, "_openai_client", lambda tier: fake)

    assert await router.list_models("openai") == ["gpt-4o", "gpt-4o-mini"]


async def test_openai_raises_when_no_credential_resolves(monkeypatch):
    router = ModelRouter(_settings())
    monkeypatch.setattr(router, "_openai_client", lambda tier: None)

    with pytest.raises(RuntimeError, match="openai"):
        await router.list_models("openai")


async def test_anthropic_reads_through_the_adapters_raw_client(monkeypatch):
    router = ModelRouter(_settings())
    fake = _FakeAnthropicAdapter(["claude-opus-4-5", "claude-sonnet-4-5"])
    monkeypatch.setattr(router, "_anthropic_client", lambda tier: fake)

    assert await router.list_models("anthropic") == ["claude-opus-4-5", "claude-sonnet-4-5"]


async def test_anthropic_raises_when_no_credential_resolves(monkeypatch):
    router = ModelRouter(_settings())
    monkeypatch.setattr(router, "_anthropic_client", lambda tier: None)

    with pytest.raises(RuntimeError, match="anthropic"):
        await router.list_models("anthropic")


async def test_google_strips_the_models_prefix(monkeypatch):
    router = ModelRouter(_settings())
    fake = _FakeGoogleAdapter(["models/gemini-2.5-pro", "models/gemini-2.5-flash"])
    monkeypatch.setattr(router, "_google_client", lambda tier: fake)

    assert await router.list_models("google") == ["gemini-2.5-flash", "gemini-2.5-pro"]


async def test_google_raises_when_no_credential_resolves(monkeypatch):
    router = ModelRouter(_settings())
    monkeypatch.setattr(router, "_google_client", lambda tier: None)

    with pytest.raises(RuntimeError, match="google"):
        await router.list_models("google")
