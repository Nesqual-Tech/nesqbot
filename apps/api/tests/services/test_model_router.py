"""`app.services.model_router` — routing table, pricing, retry policy, streaming."""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import pytest
from openai import APIConnectionError, APIStatusError, RateLimitError

from app.services.model_router import (
    REASONING_EFFORTS,
    RETRY_ATTEMPTS,
    TIER_PRICES,
    ChatResult,
    ModelRouter,
    estimate_cost_usd,
    is_retryable,
    normalise_effort,
    route_task,
)
from tests.conftest import REPO_ROOT

# ---------------------------------------------------------------------------
# route_task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("classify", "nano"),
        ("route", "nano"),
        ("compact", "nano"),
        ("embed", "embed"),
        ("deep_plan", "reason"),
        ("agent_turn", "mini"),
        ("computer_use_recover", "mini"),
    ],
)
def test_route_task_table(task, expected):
    assert route_task(task) == expected


@pytest.mark.parametrize(
    ("fail_count", "expected"),
    [(0, "mini"), (1, "mini"), (2, "reason"), (7, "reason")],
)
def test_computer_use_recovery_escalates_after_two_failures(fail_count, expected):
    assert route_task("computer_use_recover", fail_count) == expected


def test_an_unknown_task_falls_back_to_mini():
    assert route_task("something_new") == "mini"  # type: ignore[arg-type]


def test_fail_count_does_not_move_other_tasks():
    for task in ("classify", "route", "compact", "embed", "deep_plan", "agent_turn"):
        assert route_task(task, 0) == route_task(task, 9)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_the_pricing_table_is_exactly_as_specified():
    """USD per million tokens, (input, output)."""
    assert TIER_PRICES == {
        "nano": (0.20, 1.20),
        "mini": (0.75, 4.50),
        "reason": (0.20, 0.50),
        "embed": (0.02, 0.0),
    }


@pytest.mark.parametrize(
    ("tier", "input_tokens", "output_tokens", "expected"),
    [
        ("nano", 1_000_000, 0, "0.20"),
        ("nano", 0, 1_000_000, "1.20"),
        ("mini", 1_000_000, 1_000_000, "5.25"),
        ("reason", 500_000, 250_000, "0.225"),
        ("embed", 1_000_000, 1_000_000, "0.02"),
        ("mini", 0, 0, "0"),
    ],
)
def test_estimate_cost_usd(tier, input_tokens, output_tokens, expected):
    actual = estimate_cost_usd(tier, input_tokens, output_tokens)
    assert abs(actual - Decimal(expected)) < Decimal("0.000001")


def test_estimate_cost_returns_a_decimal():
    assert isinstance(estimate_cost_usd("mini", 10, 10), Decimal)


def test_embed_output_tokens_are_free():
    assert estimate_cost_usd("embed", 0, 10_000_000) == Decimal("0")


def test_an_unknown_tier_raises_rather_than_billing_zero():
    with pytest.raises(KeyError):
        estimate_cost_usd("flagship", 1, 1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Retry policy — transient failures only
# ---------------------------------------------------------------------------


def test_retry_attempts_is_three():
    assert RETRY_ATTEMPTS == 3


def test_connection_and_rate_limit_errors_are_retryable():
    assert is_retryable(APIConnectionError.__new__(APIConnectionError))
    assert is_retryable(RateLimitError.__new__(RateLimitError))


@pytest.mark.parametrize(("status", "expected"), [(500, True), (503, True), (599, True), (400, False), (404, False), (429, False)])
def test_only_5xx_status_errors_are_retryable(status, expected):
    exc = APIStatusError.__new__(APIStatusError)
    exc.status_code = status
    assert is_retryable(exc) is expected


def test_arbitrary_errors_are_not_retryable():
    assert is_retryable(ValueError("nope")) is False
    assert is_retryable(KeyError("nope")) is False


# ---------------------------------------------------------------------------
# The keyless (mock) path
# ---------------------------------------------------------------------------


def test_the_client_is_none_without_azure_configuration():
    assert ModelRouter().client() is None


async def test_chat_returns_a_deterministic_mock_without_keys():
    router = ModelRouter()
    result = await router.chat(task="agent_turn", messages=[{"role": "user", "content": "hello"}])
    assert isinstance(result, ChatResult)
    assert result.tier == "mini"
    assert "[mock:mini]" in result.content
    assert "hello" in result.content
    assert result.input_tokens > 0
    assert result.cost_usd > 0
    assert router.last_result is result


async def test_stream_chat_yields_deltas_and_sets_last_result():
    router = ModelRouter()
    deltas = [
        chunk
        async for chunk in router.stream_chat(
            task="agent_turn", messages=[{"role": "user", "content": "stream me"}]
        )
    ]
    assert len(deltas) > 1
    assert router.last_result is not None
    assert "".join(deltas) == router.last_result.content
    assert router.last_result.tier == "mini"


async def test_stream_chat_clears_last_result_until_it_is_exhausted():
    router = ModelRouter()
    router.last_result = ChatResult("stale", "mini", 1, 1, Decimal("0"))
    stream = router.stream_chat(task="classify", messages=[{"role": "user", "content": "x"}])
    await stream.__anext__()
    assert router.last_result is None
    async for _ in stream:
        pass
    assert router.last_result is not None
    assert router.last_result.tier == "nano"


# ---------------------------------------------------------------------------
# Reasoning effort
# ---------------------------------------------------------------------------
#
# `gpt-5.6-sol` reasons at high effort unless told otherwise, and the agent loop
# routes every desktop step to it — so "click the search box" was being
# deliberated over as hard as the plan that produced it, on every step of a
# thirty-five step run.


class _FakeCompletions:
    """Captures the kwargs each request would have sent, and can refuse one."""

    def __init__(self, reject: Exception | None = None):
        self.calls: list[dict] = []
        self.reject = reject

    async def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.reject is not None and "reasoning_effort" in kwargs:
            raise self.reject
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
        )


class _FakeClient:
    def __init__(self, reject: Exception | None = None):
        self.completions = _FakeCompletions(reject)
        self.chat = SimpleNamespace(completions=self.completions)


def _wired(router: ModelRouter, client: _FakeClient) -> ModelRouter:
    router._client = client  # type: ignore[assignment]
    router.auth_mode = "api_key"
    return router


def _bad_request(message: str) -> APIStatusError:
    exc = APIStatusError.__new__(APIStatusError)
    exc.status_code = 400
    exc.message = message
    return exc


def test_the_effort_values_are_the_ones_the_deployments_accept():
    """Probed live, not read off the SDK's type hint.

    The pinned SDK types `reasoning_effort` as `low | medium | high`, which is
    not the set that works here: the gpt-5.6 family refuses all three next to
    function tools and accepts `"none"`, which the type hint does not mention.
    `minimal` is here for `gpt-5.4-mini`, which does take it.
    """
    assert REASONING_EFFORTS == {"none", "minimal", "low", "medium", "high"}
    for effort in REASONING_EFFORTS:
        assert normalise_effort(effort) == effort


@pytest.mark.parametrize("value", ["", None, "   ", "ultra", "HIGHEST", "0"])
def test_an_unusable_effort_is_dropped_rather_than_forwarded(value):
    """A typo in a config value must not 400 every model call in the process."""
    assert normalise_effort(value) is None


def test_effort_is_case_and_whitespace_insensitive():
    assert normalise_effort("  HIGH ") == "high"


async def test_the_effort_reaches_the_request():
    router = _wired(ModelRouter(), _FakeClient())

    await router.chat(
        task="deep_plan", messages=[{"role": "user", "content": "go"}], reasoning_effort="low"
    )

    assert router.client().completions.calls[0]["reasoning_effort"] == "low"


async def test_no_effort_means_no_parameter():
    """Callers that do not opt in send exactly what they sent before."""
    router = _wired(ModelRouter(), _FakeClient())

    await router.chat(task="agent_turn", messages=[{"role": "user", "content": "go"}])

    assert "reasoning_effort" not in router.client().completions.calls[0]


async def test_the_effort_reaches_a_streamed_request():
    router = _wired(ModelRouter(), _FakeClient())

    async def _stream():
        return
        yield  # pragma: no cover - an empty async iterator

    router.client().completions.create = _capture_stream(router.client().completions, _stream)
    async for _ in router.stream_chat(
        task="deep_plan", messages=[{"role": "user", "content": "go"}], reasoning_effort="medium"
    ):
        pass

    assert router.client().completions.calls[0]["reasoning_effort"] == "medium"
    assert router.client().completions.calls[0]["stream"] is True


def _capture_stream(completions, factory):
    async def create(**kwargs):
        completions.calls.append(dict(kwargs))
        return factory()

    return create


#: What `gpt-5.6-sol` actually answers when asked for graded effort alongside
#: function tools, verbatim from the live deployment.
_SOL_REFUSAL = (
    "Function tools with reasoning_effort are not supported for gpt-5.6-sol in "
    "/v1/chat/completions. To use function tools, use /v1/responses or set "
    "reasoning_effort to 'none'."
)


async def test_a_rejected_effort_is_retried_without_it():
    """One wasted request, once — not one per call for the life of the process."""
    router = _wired(ModelRouter(), _FakeClient(_bad_request(_SOL_REFUSAL)))
    deployment = router.settings.azure_deployment_reason

    result = await router.chat(
        task="deep_plan", messages=[{"role": "user", "content": "go"}], reasoning_effort="high"
    )

    assert result.content == "ok"
    assert (deployment, "high") in router.rejected_efforts
    calls = router.client().completions.calls
    assert "reasoning_effort" in calls[0]
    assert "reasoning_effort" not in calls[1]

    # And that pair is not sent again.
    await router.chat(
        task="deep_plan", messages=[{"role": "user", "content": "again"}], reasoning_effort="high"
    )
    assert "reasoning_effort" not in router.client().completions.calls[-1]


async def test_one_rejected_effort_does_not_disable_the_one_that_works():
    """The whole reason the cache is keyed by the pair.

    `gpt-5.6-sol` refuses `"high"` next to function tools and accepts
    `"none"` — which is the setting the agent loop depends on. Remembering only
    "sol said no" would switch off the fix while looking like it was working.
    """
    router = _wired(ModelRouter(), _FakeClient(_bad_request(_SOL_REFUSAL)))
    deployment = router.settings.azure_deployment_reason

    await router.chat(
        task="deep_plan", messages=[{"role": "user", "content": "go"}], reasoning_effort="high"
    )
    router.client().completions.reject = None
    await router.chat(
        task="deep_plan", messages=[{"role": "user", "content": "go"}], reasoning_effort="none"
    )

    assert router.rejected_efforts == {(deployment, "high")}
    assert router.client().completions.calls[-1]["reasoning_effort"] == "none"


async def test_a_rejection_on_one_deployment_does_not_speak_for_another():
    router = _wired(ModelRouter(), _FakeClient(_bad_request(_SOL_REFUSAL)))

    await router.chat(
        task="deep_plan", messages=[{"role": "user", "content": "go"}], reasoning_effort="high"
    )
    router.client().completions.reject = None
    # `agent_turn` routes to the mini tier, a different deployment entirely.
    await router.chat(
        task="agent_turn", messages=[{"role": "user", "content": "go"}], reasoning_effort="high"
    )

    assert router.client().completions.calls[-1]["reasoning_effort"] == "high"


async def test_a_400_about_something_else_is_raised_not_swallowed():
    """A bad-messages 400 retried without the effort hint would fail twice and
    hide its own cause."""
    router = _wired(ModelRouter(), _FakeClient(_bad_request("Invalid image content part.")))

    with pytest.raises(APIStatusError):
        await router.chat(
            task="deep_plan", messages=[{"role": "user", "content": "go"}], reasoning_effort="low"
        )

    assert router.rejected_efforts == set()


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------


async def test_record_cost_and_spent_today(db, make_bot, make_user):
    user = await make_user()
    bot = await make_bot(user)
    router = ModelRouter()

    assert await router.spent_today_usd(db, bot.id) == Decimal("0")
    await router.record_cost(db, bot.id, ChatResult("x", "mini", 1000, 500, Decimal("0.0012")))
    assert await router.spent_today_usd(db, bot.id) == Decimal("0.001200")


# ---------------------------------------------------------------------------
# Cross-language parity — `packages/model-router/src/index.ts`
# ---------------------------------------------------------------------------
#
# The TypeScript package re-implements the tier table so a client can price a
# turn without a round trip. If it drifts, the UI quotes one number and the
# ledger bills another. The Python table is the source of truth; this reads the
# TS source and compares the numbers it declares. It is skipped rather than
# failed when `packages/` is not on disk, because some CI lanes copy only
# `apps/api` into the runner.

_TS_ROUTER = REPO_ROOT / "packages" / "model-router" / "src" / "index.ts"

_TS_BLOCK_RE = re.compile(r"export const (\w+)[^=]*=\s*\{(.*?)\n\}", re.DOTALL)
_TS_PRICE_RE = re.compile(r"(\w+):\s*\{\s*input:\s*([\d.]+),\s*output:\s*([\d.]+)")
_TS_STRING_RE = re.compile(r'(\w+):\s*"([^"]+)"')


def _ts_block(name: str) -> str:
    if not _TS_ROUTER.exists():  # pragma: no cover - depends on the checkout layout
        pytest.skip(f"{_TS_ROUTER} is not in this checkout")
    source = _TS_ROUTER.read_text(encoding="utf-8")
    for found, body in _TS_BLOCK_RE.findall(source):
        if found == name:
            return body
    raise AssertionError(f"{name} is missing from {_TS_ROUTER}")


def test_the_typescript_tier_prices_are_numerically_identical():
    body = _ts_block("TIER_PRICES")
    ts_prices = {tier: (float(inp), float(out)) for tier, inp, out in _TS_PRICE_RE.findall(body)}
    assert ts_prices == {tier: (float(inp), float(out)) for tier, (inp, out) in TIER_PRICES.items()}


def test_the_typescript_default_deployments_match_the_settings_defaults():
    from app.config import Settings

    body = _ts_block("TIER_DEFAULT_DEPLOYMENTS")
    ts_deployments = dict(_TS_STRING_RE.findall(body))
    defaults = Settings.model_fields
    assert ts_deployments == {
        tier: defaults[f"azure_deployment_{tier}"].default for tier in ("nano", "mini", "reason", "embed")
    }
