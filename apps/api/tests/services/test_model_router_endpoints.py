"""One router, more than one account — and what is different about the second one.

Why this exists, in one line from the ledger: the `reason` tier spent $9.13 in
a day at $5.00 per 1M input tokens, and `grok-4-1-fast-reasoning` is $0.20 for
the same tokens. 25x on the line item that *is* the bill.

It could not be reached. `ModelRouter` had one endpoint, and an xAI model
cannot be deployed to an `OpenAI`-kind Azure account at all — the two Grok
deployments live on a separate `AIServices` account with its own hostname. So
the endpoint is now per tier, and this file pins both the wiring and the three
things that are genuinely different about the xAI account, all of which were
measured against the live endpoint on 2026-08-23 rather than read off a
datasheet:

* **The wire format is not different.** The classic
  `/openai/deployments/{name}/chat/completions?api-version=2024-12-01-preview`
  route answers 200, tools come back in OpenAI shape, streaming with
  `stream_options.include_usage` works and ends `[DONE]`, and vision content
  parts are accepted. `AsyncAzureOpenAI` needs no special casing, which is the
  single most useful thing about this whole change.
* **The token accounting is different**, in a way that silently under-bills.
  See `billable_output_tokens` and the test for it below.
* **The image pricing is different**, by 2.3x on the exact frame this loop
  sends. `estimate_image_tokens` describes the Azure OpenAI account and does
  not describe this one; the test at the bottom records the measured numbers so
  the next person does not have to re-measure them to find out.

The live call itself is `test_a_real_completion_reaches_grok`, skipped unless
`NESQ_LIVE_XAI_ENDPOINT` is set, because a test suite that needs an Azure
credential is a test suite that does not run.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.config import Settings
from app.services.model_router import (
    _LOGGED_AUTH_MODES,
    _TOKEN_PROVIDERS,
    _WARNED_OFF_DEFAULT,
    AGENT_LOOP_TASK,
    TIER_PRICES,
    ModelRouter,
    billable_output_tokens,
    estimate_image_tokens,
    route_task,
)

AOAI = "https://your-aoai.openai.azure.com/"
XAI = "https://your-ai-services.cognitiveservices.azure.com/"
UAMI_CLIENT_ID = "50000000-0000-0000-0000-000000000005"
GROK = "grok-4-1-fast-reasoning"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "azure_openai_endpoint": "",
        "azure_openai_api_key": "",
        "azure_managed_identity_client_id": "",
    }
    return Settings(**{**base, **overrides})


class _FakeCredential:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


@pytest.fixture
def azure_identity_spy(monkeypatch):
    """Patch both `azure.identity` flavours; report what the router asked them for.

    A local copy of `test_model_router_auth.py`'s fixture rather than a shared
    one: a pytest fixture imported across modules is a redefinition, and the
    linter is right to say so.
    """
    import azure.identity
    import azure.identity.aio

    seen: dict[str, Any] = {}

    def _patch(module: Any) -> None:
        def fake_credential(*args: Any, **kwargs: Any) -> _FakeCredential:
            credential = _FakeCredential(*args, **kwargs)
            seen["credential"] = credential
            seen["kwargs"] = kwargs
            return credential

        def fake_provider(credential: Any, *scopes: str, **kwargs: Any):
            seen["scopes"] = scopes
            return lambda: "fake-entra-token"

        monkeypatch.setattr(module, "ManagedIdentityCredential", fake_credential)
        monkeypatch.setattr(module, "get_bearer_token_provider", fake_provider)

    _patch(azure.identity.aio)
    _patch(azure.identity)
    return seen


@pytest.fixture(autouse=True)
def _clean_globals():
    _TOKEN_PROVIDERS.clear()
    _LOGGED_AUTH_MODES.clear()
    _WARNED_OFF_DEFAULT.clear()
    yield
    _TOKEN_PROVIDERS.clear()
    _LOGGED_AUTH_MODES.clear()
    _WARNED_OFF_DEFAULT.clear()


# ---------------------------------------------------------------------------
# 1. Per-tier endpoints
# ---------------------------------------------------------------------------


def test_the_default_is_still_one_account_for_everything():
    """The switch is a config decision. Nothing here changes what ships today."""
    router = ModelRouter(_settings(azure_openai_endpoint=AOAI, azure_openai_api_key="sk"))
    clients = {router.client(tier) for tier in ("nano", "mini", "reason", "embed")}
    assert len(clients) == 1
    assert next(iter(clients)) is not None


def test_a_tier_can_be_pointed_at_the_other_account():
    router = ModelRouter(
        _settings(
            azure_openai_endpoint=AOAI,
            azure_openai_api_key="sk-aoai",
            azure_openai_endpoint_reason=XAI,
            azure_openai_api_key_reason="sk-xai",
        )
    )
    assert str(router.client("reason").base_url).startswith(XAI.rstrip("/"))
    assert str(router.client("mini").base_url).startswith(AOAI.rstrip("/"))
    assert router.client("reason") is not router.client("mini")


def test_two_tiers_on_one_account_share_a_client():
    """A client owns a connection pool and a token cache. One per account, not per tier."""
    router = ModelRouter(
        _settings(
            azure_openai_endpoint=AOAI,
            azure_openai_api_key="sk",
            azure_openai_endpoint_reason=XAI,
            azure_openai_api_key_reason="sk-xai",
        )
    )
    assert router.client("nano") is router.client("mini")


def test_a_key_never_follows_a_tier_onto_a_different_account():
    """The trap this rule exists for.

    Presenting the Azure OpenAI account's key to the xAI account is a 401 that
    reads exactly like a broken deployment. A tier with its own endpoint gets
    its own key or no key at all — and no key means managed identity, which is
    the production path anyway.
    """
    router = ModelRouter(
        _settings(
            azure_openai_endpoint=AOAI,
            azure_openai_api_key="sk-aoai",
            azure_openai_endpoint_reason=XAI,
        )
    )
    endpoint, key, _ = router._endpoint_for("reason")
    assert endpoint == XAI
    assert key == ""
    # …and the default account still has its key.
    assert router._endpoint_for("mini")[1] == "sk-aoai"


def test_both_accounts_authenticate_the_same_way(azure_identity_spy):
    """One Entra scope, one explicit client id, any number of endpoints.

    `Cognitive Services OpenAI User` on both accounts and one
    `https://cognitiveservices.azure.com/.default` token is the whole story, so
    a second account costs one more `AsyncAzureOpenAI` and no new auth code.
    """
    router = ModelRouter(
        _settings(
            azure_openai_endpoint=AOAI,
            azure_openai_endpoint_reason=XAI,
            azure_managed_identity_client_id=UAMI_CLIENT_ID,
        )
    )
    assert router.client("mini") is not None
    assert router.client("reason") is not None
    assert router.auth_modes == {AOAI: "managed_identity", XAI: "managed_identity"}
    # One credential, built once, with the identity's client id passed
    # explicitly — a bare `DefaultAzureCredential` would read the ambient
    # `AZURE_CLIENT_ID`, which here is the Entra API app registration.
    assert azure_identity_spy["kwargs"]["client_id"] == UAMI_CLIENT_ID
    assert len(_TOKEN_PROVIDERS) == 1


def test_an_unreachable_second_account_does_not_mock_the_first():
    """A per-endpoint decision, not a per-router one.

    Collapsing these into one answer is how a perfectly good `mini` tier starts
    returning `[mock:mini] …` because someone typed the Grok hostname wrong.
    """
    router = ModelRouter(
        _settings(
            azure_openai_endpoint=AOAI,
            azure_openai_api_key="sk-aoai",
            azure_openai_endpoint_reason="   ",
        )
    )
    assert router.client("mini") is not None
    assert router.client("reason") is not None, "blank means 'use the default', not 'mock'"


def test_supports_tools_asks_about_the_tier_the_loop_actually_runs_on():
    """With per-tier endpoints, "is there a live account" has a per-tier answer.

    The caller is the desktop loop deciding whether prose-instead-of-a-tool-call
    is a protocol violation, and that question is only about its own tier.
    """
    reason_only = ModelRouter(
        _settings(azure_openai_endpoint_reason=XAI, azure_openai_api_key_reason="sk")
    )
    assert route_task(AGENT_LOOP_TASK) == "reason"
    assert reason_only.supports_tools is True

    mini_only = ModelRouter(
        _settings(azure_openai_endpoint_mini=AOAI, azure_openai_api_key_mini="sk")
    )
    assert mini_only.supports_tools is False


def test_the_loop_task_constant_matches_the_orchestrators():
    """It is duplicated to avoid an import cycle; this is what stops it drifting."""
    from app.services import orchestrator

    assert AGENT_LOOP_TASK == orchestrator.AGENT_LOOP_TASK


def test_moving_a_tier_says_out_loud_that_its_price_did_not_move(caplog):
    """`TIER_PRICES` is hand-maintained and mirrored into `packages/model-router`.

    Nothing in the config can move it, so pointing `reason` at a $0.20 account
    while the ledger still bills $5.00 is silently possible — and it would make
    the bot's daily budget trip 25x early and hide the saving the switch was
    made for. This is the warning that stops that being a surprise. It warns
    rather than refusing: an accuracy A/B on a second account should not be
    blocked from starting because its prices are not decided yet.
    """
    router = ModelRouter(
        _settings(
            azure_openai_endpoint=AOAI,
            azure_openai_endpoint_reason=XAI,
            azure_deployment_reason=GROK,
        )
    )
    with caplog.at_level("WARNING"):
        router._endpoint_for("reason")
        router._endpoint_for("reason")

    warnings = [r for r in caplog.records if "TIER_PRICES" in r.getMessage()]
    assert len(warnings) == 1, "once per process, not once per call"
    message = warnings[0].getMessage()
    assert GROK in message and XAI in message
    assert "packages/model-router" in message
    assert f"${TIER_PRICES['reason'][0]:.2f}" in message


# ---------------------------------------------------------------------------
# 2. What is actually different about the xAI account
# ---------------------------------------------------------------------------


class _Usage:
    def __init__(self, prompt: int, completion: int, total: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


def test_hidden_reasoning_tokens_are_billed_rather_than_lost():
    """The measured xAI usage block, verbatim from a live reply.

    `prompt 1802, completion 1, total 2330` — with 527 reasoning tokens that
    are billed and are *not* inside `completion_tokens`. Writing
    `completion_tokens` into the ledger records one output token for a reply
    that generated 528, which at $30/1M on the current reason tier is a 500x
    undercount on the output half of the bill.
    """
    assert billable_output_tokens(_Usage(1802, 1, 2330)) == 528


def test_the_azure_openai_accounting_is_untouched():
    """There, `total == prompt + completion` and reasoning is already inside
    `completion_tokens`. This must return exactly what it returned before."""
    assert billable_output_tokens(_Usage(1000, 250, 1250)) == 250


def test_a_missing_total_does_not_zero_the_output():
    """Both readings are taken and the larger wins, rather than trusting either.

    An endpoint that reports a sane `completion_tokens` and no `total_tokens`
    must not have its output silently zeroed.
    """
    assert billable_output_tokens(_Usage(1000, 250, 0)) == 250
    assert billable_output_tokens(_Usage(0, 0, 0)) == 0


def test_the_image_price_this_codebase_computes_is_the_azure_openai_one():
    """Measured, and recorded here because it is a trap for the Grok switch.

    A 1024x640 JPEG — exactly what `AGENT_SCREENSHOT_OPTIONS` produces — priced
    by three different counters on 2026-08-23:

        estimate_image_tokens (Azure OpenAI tiles)   765
        grok-4-1-fast-reasoning (measured)         1,792
        grok-4.3 (measured)                          640

    So the reason tier's vision steps would cost 2.3x more *tokens* on
    `grok-4-1-fast-reasoning` than this estimator thinks. Still ~10x cheaper in
    money at $0.20 against $5.00, but it eats a third of the 25x headline and
    it is the number that decides whether a vision loop fits in 50,000 tokens a
    minute. Nothing computes it yet; this test is the note saying so.
    """
    assert estimate_image_tokens(1024, 640) == 765
    measured_grok_fast = 1792
    assert measured_grok_fast / estimate_image_tokens(1024, 640) == pytest.approx(2.34, abs=0.01)


# ---------------------------------------------------------------------------
# 3. The live call
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("NESQ_LIVE_XAI_ENDPOINT"),
    reason="set NESQ_LIVE_XAI_ENDPOINT (and an Azure credential) to call the real account",
)
async def test_a_real_completion_reaches_grok():
    """A trivial completion against `grok-4-1-fast-reasoning`, through the router.

    Everything below the credential is the production path: the endpoint
    resolution, the api-version, the deployment name, `_request_kwargs`, the
    retry wrapper, `parse_tool_calls` and `billable_output_tokens`.

    The credential itself is the one thing a workstation cannot reproduce, so
    there are three ways in and they differ only in where the bearer token
    comes from:

    * nothing set — `ManagedIdentityCredential(client_id=…)` over IMDS, which
      is what the container does and the only one that is end-to-end;
    * `NESQ_LIVE_XAI_BEARER` — a token minted some other way (`az account
      get-access-token --resource https://cognitiveservices.azure.com`) seeded
      straight into the provider cache. Identical from `client()` down: same
      scope, same header, same account, same role check on the far side;
    * `NESQ_LIVE_XAI_KEY` — a key, if the account has them enabled.
    """
    bearer = os.getenv("NESQ_LIVE_XAI_BEARER", "").strip()
    client_id = os.getenv("NESQ_LIVE_UAMI_CLIENT_ID", "")
    if bearer:
        _TOKEN_PROVIDERS[client_id] = lambda: bearer
    router = ModelRouter(
        _settings(
            azure_openai_endpoint_reason=os.environ["NESQ_LIVE_XAI_ENDPOINT"],
            azure_openai_api_key_reason=os.getenv("NESQ_LIVE_XAI_KEY", ""),
            azure_managed_identity_client_id=client_id,
            azure_deployment_reason=os.getenv("NESQ_LIVE_XAI_DEPLOYMENT", GROK),
        )
    )
    assert router.client("reason") is not None
    assert router.auth_modes[os.environ["NESQ_LIVE_XAI_ENDPOINT"]] in (
        "managed_identity",
        "api_key",
    )
    result = await router.chat(
        task=AGENT_LOOP_TASK,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
    )
    assert result.tier == "reason"
    assert "OK" in result.content
    assert result.input_tokens > 0
    # The point of `billable_output_tokens`: the hidden reasoning tokens this
    # deployment generates must be on the result, not dropped.
    assert result.output_tokens >= 1
