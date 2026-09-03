"""A bot pinned to a model must be sent to the account that has it.

The production failure, in full. The Lead Generator was pinned — from the
Builder's own live model dropdown — to `grok-4.3`, and every turn it took died
with:

    openai.NotFoundError: Error code: 404 - {'error': {'code':
    'DeploymentNotFound', 'message': 'The API deployment for this resource
    does not exist.'}}

Nothing was misconfigured. `grok-4.3` *is* deployed, on the second Azure
account this deployment uses (`AZURE_OPENAI_ENDPOINT_REASON`, a Foundry
resource carrying Grok), and `_azure_list_all_deployments` lists both accounts
into one dropdown — so the app offered a model that the chat path then could
not place. `_client_for` matched the pinned name against the four *configured*
tier deployments and, finding none, fell back to the shared GPT account, which
has never carried a Grok deployment.

Two things are therefore worth pinning down: that a name only the second
account reports lands on the second account, and that the cheap path still
takes no network call at all for the ordinary case.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.models import Bot
from app.services.model_router import ModelRouter

SHARED = "https://shared.openai.azure.com/"
FOUNDRY = "https://foundry.openai.azure.com/"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "azure_openai_endpoint": SHARED,
        "azure_openai_api_key": "k",
        "azure_openai_endpoint_reason": FOUNDRY,
        "azure_deployment_nano": "gpt-nano",
        "azure_deployment_mini": "gpt-mini",
        "azure_deployment_reason": "grok-4-1-fast-reasoning",
        "azure_deployment_reason_alt": "grok-4-6-reasoning",
        "azure_deployment_embed": "text-embedding-3-small",
        "azure_managed_identity_client_id": "",
    }
    return Settings(**{**base, **overrides})


class _FakeClient:
    """Only what the deployments listing touches."""

    def __init__(self, label: str, deployments: list[str]) -> None:
        self.label = label
        self.deployments = deployments
        self.listings = 0

    async def get(self, path: str, *, cast_to: type, options: dict[str, Any]) -> dict[str, Any]:
        self.listings += 1
        return {"data": [{"id": name} for name in self.deployments]}


def _router(shared: _FakeClient, foundry: _FakeClient) -> ModelRouter:
    """A router whose two azure accounts are the two fakes.

    `_azure_client` is patched rather than the SDK: what is under test is which
    of the two accounts a pinned model resolves to, and building real clients
    would need credentials for both.
    """
    router = ModelRouter(_settings())

    def azure_client(tier: str | None) -> _FakeClient:
        return foundry if tier == "reason" else shared

    router._azure_client = azure_client  # type: ignore[method-assign, assignment]
    return router


def _pinned(model: str) -> Bot:
    bot = Bot(slug="lead_generator", name="Lead Generator", role="", system_prompt="x")
    bot.model_provider = "azure"
    bot.model_name = model
    return bot


async def test_a_model_only_the_second_account_has_goes_to_the_second_account():
    """The production bug, as a test."""
    shared = _FakeClient("shared", ["gpt-nano", "gpt-mini", "text-embedding-3-small"])
    foundry = _FakeClient("foundry", ["grok-4-1-fast-reasoning", "grok-4.3", "grok-4-6-reasoning"])
    router = _router(shared, foundry)

    client = await router._azure_client_for_model("grok-4.3")

    assert client is foundry, "a Grok deployment was sent to the GPT account, which 404s"


async def test_the_configured_deployments_need_no_listing():
    """The ordinary case must not pay for a network call.

    Every bot pinned to a model the router itself routes to is answerable from
    settings alone, and a `/deployments` round trip on the first turn of every
    such bot would be a real cost for no information.
    """
    shared = _FakeClient("shared", ["gpt-mini"])
    foundry = _FakeClient("foundry", ["grok-4-1-fast-reasoning"])
    router = _router(shared, foundry)

    assert await router._azure_client_for_model("gpt-mini") is shared
    assert await router._azure_client_for_model("grok-4-1-fast-reasoning") is foundry

    assert shared.listings == 0
    assert foundry.listings == 0


async def test_the_alternate_reason_deployment_resolves_without_a_listing():
    """`azure_deployment_reason_alt` is not a tier, so the tier loop cannot see
    it — and it lives on the overridden endpoint, which is exactly the case
    that 404s when it is missed."""
    shared = _FakeClient("shared", ["gpt-mini"])
    foundry = _FakeClient("foundry", ["grok-4-6-reasoning"])
    router = _router(shared, foundry)

    client = await router._azure_client_for_model("grok-4-6-reasoning")

    assert client is foundry
    assert foundry.listings == 0


async def test_the_listing_happens_once_per_process():
    shared = _FakeClient("shared", ["gpt-mini"])
    foundry = _FakeClient("foundry", ["grok-4.3"])
    router = _router(shared, foundry)

    await router._azure_client_for_model("grok-4.3")
    await router._azure_client_for_model("grok-4.3")
    await router._azure_client_for_model("something-else-entirely")

    assert shared.listings == 1
    assert foundry.listings == 1


async def test_an_unknown_model_still_reaches_the_shared_account():
    """Unchanged behaviour for a name nobody reports.

    It will very likely 404 — but it may also be a deployment created since the
    listing, and refusing to send it would turn a recoverable mistake into a
    dead bot.
    """
    shared = _FakeClient("shared", ["gpt-mini"])
    foundry = _FakeClient("foundry", ["grok-4.3"])
    router = _router(shared, foundry)

    assert await router._azure_client_for_model("gpt-5.9-imaginary") is shared


async def test_a_failed_listing_does_not_fail_the_turn():
    """A listing is a diagnostic, not a dependency."""
    shared = _FakeClient("shared", ["gpt-mini"])
    foundry = _FakeClient("foundry", [])
    router = _router(shared, foundry)

    async def explode(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("the deployments endpoint said no")

    shared.get = explode  # type: ignore[method-assign, assignment]
    foundry.get = explode  # type: ignore[method-assign, assignment]

    assert await router._azure_client_for_model("grok-4.3") is shared


@pytest.mark.parametrize("model", ["grok-4.3", "gpt-mini"])
async def test_chat_uses_the_resolved_account_for_a_pinned_bot(model: str, monkeypatch):
    """The resolver is wired into the path that actually calls the model.

    `chat` used to call `_client_for`, which is synchronous and therefore
    cannot list anything — the whole bug. Asserted here on the call itself
    rather than on the resolver, because "resolves correctly and is not used"
    was the previous state of the world.
    """
    shared = _FakeClient("shared", ["gpt-mini"])
    foundry = _FakeClient("foundry", ["grok-4.3"])
    router = _router(shared, foundry)

    used: list[Any] = []

    async def fake_create(client: Any, kwargs: dict[str, Any]) -> Any:
        used.append(client)
        raise RuntimeError("stop here — the client is what this test is about")

    monkeypatch.setattr(router, "_create", fake_create)

    with pytest.raises(RuntimeError):
        await router.chat(
            task="agent_step",
            messages=[{"role": "user", "content": "hi"}],
            bot=_pinned(model),
        )

    assert used == [foundry if model == "grok-4.3" else shared]
