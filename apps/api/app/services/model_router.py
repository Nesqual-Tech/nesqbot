"""Cheap Azure AI Foundry model router."""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Literal, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncAzureOpenAI,
    AsyncOpenAI,
    RateLimitError,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.models import Bot, CostLedger

logger = logging.getLogger(__name__)

Tier = Literal["nano", "mini", "reason", "embed"]

#: `google` is an accepted config value but has no client implementation yet -
#: see ModelRouter.client(). Selecting it is honest about that (falls back to
#: mock, same as an unconfigured Azure tier) rather than silently mis-routing
#: to a provider that was never actually built. `azure`/`openai`/`anthropic`
#: are all real.
Provider = Literal["azure", "openai", "anthropic", "google"]
_OPENAI_PROTOCOL_PROVIDERS = frozenset({"azure", "openai"})
TaskClass = Literal[
    "classify",
    "route",
    "agent_turn",
    "computer_use_recover",
    "deep_plan",
    "compact",
    "embed",
]

# USD per 1M tokens, (input, output). These are billing inputs, not decoration:
# `estimate_cost_usd` writes them into `cost_ledger`, and the orchestrator's
# per-bot daily budget is enforced off that sum. Too low and a runaway bot never
# trips its cap; too high and healthy bots get throttled early.
#
# Source: Azure AI Foundry list pricing for the `swedencentral` deployments named
# in `app.config.Settings` (nano=gpt-5.6-luna, mini=gpt-5.4-mini,
# reason=gpt-5.6-sol, embed=text-embedding-3-small). Last checked 2026-08-22.
# Re-check when a deployment changes, and mirror any edit into
# `packages/model-router/src/index.ts` in the same commit.
TIER_PRICES: dict[Tier, tuple[float, float]] = {
    "nano": (0.20, 1.20),
    "mini": (0.75, 4.50),
    "reason": (0.20, 0.50),
    "embed": (0.02, 0.0),
}

RETRY_ATTEMPTS = 3

# ---------------------------------------------------------------------------
# Reasoning effort
# ---------------------------------------------------------------------------
#
# The agent loop routes every desktop step to the `reason` tier, and that model
# reasons on every one of them unless told not to. "Click the search box" was
# being deliberated over as hard as the plan that produced it.
#
# **What the deployed API actually accepts**, probed against the live
# swedencentral account on 2026-08-23 across api-versions `2024-12-01-preview`
# and `2025-04-01-preview`, sending function tools exactly as the agent loop
# does:
#
#   deployment      omitted   "none"   "minimal"/"low"/"medium"/"high"
#   gpt-5.6-sol     ok        ok       400
#   gpt-5.6-terra   ok        ok       400
#   gpt-5.4-mini    ok        ok       ok
#
# The 400 on the gpt-5.6 family is explicit: *"Function tools with
# reasoning_effort are not supported for gpt-5.6-sol in /v1/chat/completions.
# To use function tools, use /v1/responses or set reasoning_effort to
# 'none'."* So on the tier the loop runs on, this is not a dial — it is a
# switch, and the only two positions are "reason as you see fit" (omit) and
# "do not reason" (`"none"`). Graded effort on that family needs the Responses
# API, which is a larger migration than a performance fix should carry.
#
# Measured on a realistic desktop step (988-token prompt, one 1024x640
# screenshot, two tools), median of three:
#
#   gpt-5.6-sol   omitted  2.35s   83 reasoning tokens
#   gpt-5.6-sol   "none"   1.39s    0 reasoning tokens
#   gpt-5.6-terra omitted  1.77s   13 reasoning tokens
#   gpt-5.6-terra "none"   1.24s    0 reasoning tokens
#
# All of them still emitted the correct `click` call, three times out of three.
#
# `minimal` is in the accepted set below because `gpt-5.4-mini` takes it; the
# per-pair rejection cache handles the deployments that do not.
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high"})

#: Substrings that identify "this deployment does not know that parameter" in a
#: 400 body, as opposed to a 400 about the messages. Matched case-insensitively.
_UNSUPPORTED_PARAMETER_MARKERS = (
    "reasoning_effort",
    "unknown parameter",
    "unrecognized request argument",
    "extra inputs are not permitted",
)


def normalise_effort(effort: str | None) -> str | None:
    """The effort to send, or None to send none.

    Unknown values are dropped rather than forwarded. An effort the API does
    not recognise is a 400 on a call that would otherwise have worked, and a
    typo in a config value should not be able to break every model call.
    """
    text = (effort or "").strip().lower()
    if not text:
        return None
    if text not in REASONING_EFFORTS:
        logger.warning("ignoring unknown reasoning_effort=%r", effort)
        return None
    return text


def _rejects_reasoning_effort(exc: BaseException) -> bool:
    """Whether a 400 is the deployment refusing `reasoning_effort`."""
    if not isinstance(exc, APIStatusError):
        return False
    if int(getattr(exc, "status_code", 0) or 0) != 400:
        return False
    blob = str(getattr(exc, "message", "") or exc).lower()
    return any(marker in blob for marker in _UNSUPPORTED_PARAMETER_MARKERS)

#: Resource scope for Azure AI Foundry / Azure OpenAI data-plane calls. Not the
#: ARM scope (`https://management.azure.com/.default`) — a token for that audience
#: is rejected by the Foundry endpoint with a confusing 401.
AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"

#: "unauthenticated" is a self-hosted OpenAI-compatible server with no key
#: configured - most (Ollama, vLLM, LM Studio) accept any request. Distinct
#: from "mock" so the logs can tell "talking to a real local server" apart
#: from "nothing is configured at all".
AuthMode = Literal["api_key", "managed_identity", "unauthenticated", "mock"]

#: One INFO line per distinct auth decision per process. "Why is it mocking?" has
#: to be answerable from the logs without a redeploy.
_LOGGED_AUTH_MODES: set[str] = set()

#: Credentials are expensive to build and hold their own token cache, so one
#: provider per identity is shared by every ModelRouter instance (deps.py, the
#: orchestrator and rag.py each construct their own router).
_TOKEN_PROVIDERS: dict[str, Any] = {}


def _log_auth_mode(mode: AuthMode, detail: str) -> None:
    key = f"{mode}|{detail}"
    if key in _LOGGED_AUTH_MODES:
        return
    _LOGGED_AUTH_MODES.add(key)
    logger.info("azure openai auth mode=%s (%s)", mode, detail)


def _bearer_token_provider(client_id: str) -> Any | None:
    """Entra bearer-token provider over the container's managed identity.

    `azure.identity` is imported here, not at module scope, so a mock-mode or
    keyless-dev deployment never loads it — the same lazy pattern as
    `services/secrets.py` and `services/desktop.py`.

    `client_id` MUST be the user-assigned identity's client id. A bare
    `DefaultAzureCredential` would read the ambient `AZURE_CLIENT_ID`, which on
    this deployment is the Entra API app registration, and fail at IMDS trying
    to fetch a token for an app that has no managed identity.
    """
    cached = _TOKEN_PROVIDERS.get(client_id)
    if cached is not None:
        return cached

    # `azure.identity.aio` needs an async transport — `aiohttp`, pinned in
    # requirements.txt for exactly this. It is not a hard requirement, though:
    # the sync credential (requests, which azure-core already pulls in) is a
    # correct fallback, and only blocks the loop on an actual token fetch, once
    # per hour. Falling back to it beats silently mocking, which is the failure
    # this whole module exists to stop.
    errors: list[str] = []
    for flavour, module in (("aio", "azure.identity.aio"), ("sync", "azure.identity")):
        try:
            identity = importlib.import_module(module)
            # client_id="" would be looked up as a user-assigned identity; None
            # means "the system-assigned identity", the correct empty case.
            credential = identity.ManagedIdentityCredential(client_id=client_id or None)
            provider = identity.get_bearer_token_provider(credential, AZURE_OPENAI_SCOPE)
        except Exception as exc:  # noqa: BLE001 - missing transport, or no identity endpoint
            errors.append(f"{flavour}: {exc}")
            continue
        if flavour == "sync":
            logger.warning(
                "using the blocking managed-identity credential (%s) — install aiohttp",
                "; ".join(errors),
            )
        _TOKEN_PROVIDERS[client_id] = provider
        return provider

    logger.warning("managed identity unavailable (%s) — falling back to mock replies", "; ".join(errors))
    return None


#: The task class the desktop agent loop runs every step on. Declared here
#: rather than imported from `orchestrator`, which imports *this* module —
#: `test_model_router.py` asserts the two stay equal, so the duplication cannot
#: drift into a router that answers "can this tier call tools" about the wrong
#: tier.
AGENT_LOOP_TASK: TaskClass = "deep_plan"

#: Tiers already warned about, so a per-call resolution logs once per process
#: rather than 128 times a day.
_WARNED_OFF_DEFAULT: set[str] = set()


def _warn_tier_is_off_the_default_account(tier: str, endpoint: str, deployment: str) -> None:
    """Say, once, that a tier's *billing* did not move with its endpoint.

    `TIER_PRICES` is a hand-maintained table mirrored into
    `packages/model-router/src/index.ts`, and nothing in the config can move
    it. Point `reason` at a Grok deployment and every row this router writes
    into `cost_ledger` is still priced at the OpenAI-kind account's $5.00/1M —
    which is 25x the truth, so the bot's daily budget trips 25x early and the
    saving the switch was made for is invisible in the numbers that decide
    whether to keep it.

    That is a two-line edit in two files, not something to infer, so this says
    so out loud instead of failing: a deployment that is deliberately
    A/B-testing accuracy on a second account should not be blocked from
    starting because its prices have not been decided yet.
    """
    if tier in _WARNED_OFF_DEFAULT:
        return
    _WARNED_OFF_DEFAULT.add(tier)
    price_in, price_out = TIER_PRICES[tier]  # type: ignore[index]
    logger.warning(
        "tier %r is served by %s (deployment %s), not by AZURE_OPENAI_ENDPOINT — "
        "cost_ledger will still bill it at TIER_PRICES[%r] = $%.2f/$%.2f per 1M. "
        "Update TIER_PRICES in services/model_router.py AND packages/model-router/"
        "src/index.ts if that is not this account's price.",
        tier,
        endpoint,
        deployment,
        tier,
        price_in,
        price_out,
    )


def _warn_tier_pricing_may_not_match_provider(tier: str, provider: str) -> None:
    """The `openai`/`anthropic`/`google` sibling of `_warn_tier_is_off_the_default_account`.

    `TIER_PRICES` is Azure list pricing. Routing a tier to a different provider
    does not change what `cost_ledger` bills it at — a local model that costs
    nothing gets billed as if it were `gpt-5.4-mini`, and the daily budget trips
    on numbers that are not this provider's numbers. Said once per tier, same
    as the Azure cross-account case, because there is no reliable way to look
    up another vendor's live price from here.
    """
    key = f"provider::{tier}"
    if key in _WARNED_OFF_DEFAULT:
        return
    _WARNED_OFF_DEFAULT.add(key)
    price_in, price_out = TIER_PRICES[tier]  # type: ignore[index]
    logger.warning(
        "tier %r is served by provider %r, not azure — cost_ledger will still "
        "bill it at TIER_PRICES[%r] = $%.2f/$%.2f per 1M, which is Azure "
        "pricing and almost certainly wrong for this provider. Fix TIER_PRICES "
        "in services/model_router.py and packages/model-router/src/index.ts "
        "if per-bot cost accounting matters to you.",
        tier,
        provider,
        tier,
        price_in,
        price_out,
    )


def route_task(task: TaskClass, fail_count: int = 0) -> Tier:
    if task in ("classify", "route", "compact"):
        return "nano"
    if task == "embed":
        return "embed"
    if task == "deep_plan":
        return "reason"
    if task == "computer_use_recover":
        return "reason" if fail_count >= 2 else "mini"
    return "mini"


def estimate_cost_usd(
    tier: Tier,
    input_tokens: int,
    output_tokens: int,
    image_tokens: int = 0,
) -> Decimal:
    """USD for one call. Image tokens bill at the *input* rate, and are added.

    `image_tokens` exists because a vision turn is not a chat reply with a
    picture stapled on: one 1280x800 screenshot is roughly 1.1k prompt tokens,
    and a six-step desktop loop sends one per step. Counting only the text
    would let a bot run a whole afternoon of screen-reading against a budget
    that never moved.

    Pass it **only** when the count is not already inside `input_tokens`. A
    live Azure response reports images inside `usage.prompt_tokens`, so the
    live path passes zero and puts the estimate on `ChatResult.image_tokens`
    for visibility; the keyless/mock path has no usage block and passes the
    estimate here.
    """
    inp, out = TIER_PRICES[tier]
    billed_input = max(int(input_tokens), 0) + max(int(image_tokens), 0)
    return Decimal(str((billed_input / 1_000_000) * inp + (output_tokens / 1_000_000) * out))


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------
#
# The `mini` and `reason` deployments accept image content parts on the chat
# completions API, which is what makes a desktop agent able to *look* at its own
# screen rather than guess. The three constants below are the published
# image-token formula: a flat base charge, plus one charge per 512x512 tile of
# the image after it has been scaled to fit the long and short edge limits.
IMAGE_BASE_TOKENS = 85
IMAGE_TILE_TOKENS = 170
IMAGE_TILE_PX = 512
IMAGE_MAX_LONG_EDGE = 2048
IMAGE_MAX_SHORT_EDGE = 768

#: What a `detail: "low"` image costs, regardless of its size.
IMAGE_LOW_DETAIL_TOKENS = IMAGE_BASE_TOKENS

#: Fallback when an image's dimensions cannot be read (a format whose header we
#: do not parse). Assumes a full-screen capture rather than a thumbnail, because
#: under-counting the budget is the failure mode this whole function exists to
#: prevent.
IMAGE_UNKNOWN_SIZE = (1280, 800)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_DATA_URL_PREFIX = "data:"


def estimate_image_tokens(width: int, height: int, *, detail: str = "high") -> int:
    """Prompt tokens one image adds, from its pixel dimensions."""
    if detail == "low":
        return IMAGE_LOW_DETAIL_TOKENS
    width, height = max(int(width), 1), max(int(height), 1)
    longest, shortest = max(width, height), min(width, height)
    if longest > IMAGE_MAX_LONG_EDGE:
        scale = IMAGE_MAX_LONG_EDGE / longest
        longest, shortest = IMAGE_MAX_LONG_EDGE, max(int(shortest * scale), 1)
    if shortest > IMAGE_MAX_SHORT_EDGE:
        scale = IMAGE_MAX_SHORT_EDGE / shortest
        shortest, longest = IMAGE_MAX_SHORT_EDGE, max(int(longest * scale), 1)
    tiles = -(-longest // IMAGE_TILE_PX) * -(-shortest // IMAGE_TILE_PX)
    return IMAGE_BASE_TOKENS + IMAGE_TILE_TOKENS * tiles


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """`(width, height)` from a PNG's IHDR chunk, or None if it is not a PNG."""
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def image_content_part(
    image_base64: str,
    *,
    media_type: str = "image/png",
    detail: str = "high",
) -> dict[str, Any]:
    """One `image_url` content part carrying a base64 image inline."""
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{media_type};base64,{image_base64}",
            "detail": detail,
        },
    }


def _decode_data_url_head(url: str) -> bytes:
    """Enough leading bytes of a base64 data URL to read an image header."""
    import base64
    import binascii

    if not url.startswith(_DATA_URL_PREFIX) or ";base64," not in url:
        return b""
    payload = url.split(";base64,", 1)[1]
    head = payload[: (len(payload) // 4) * 4][:64]
    try:
        return base64.b64decode(head, validate=False)
    except (ValueError, binascii.Error):
        return b""


def _part_image_tokens(part: dict[str, Any]) -> int:
    node = part.get("image_url")
    url = node.get("url", "") if isinstance(node, dict) else str(node or "")
    detail = node.get("detail", "high") if isinstance(node, dict) else "high"
    size = png_dimensions(_decode_data_url_head(url)) or IMAGE_UNKNOWN_SIZE
    return estimate_image_tokens(size[0], size[1], detail=str(detail))


def count_image_tokens(messages: list[dict[str, Any]]) -> int:
    """Prompt tokens the images in `messages` are worth."""
    total = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                total += _part_image_tokens(part)
    return total


def message_text(content: Any) -> str:
    """The human-readable text of a message, images excluded.

    Content-part messages must never be stringified whole for a token estimate:
    a base64 screenshot is ~1.4MB of characters, which would read as 350k text
    tokens for an image that actually costs about 1.1k.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return " ".join(part for part in parts if part)
    return "" if content is None else str(content)


def count_text_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough prompt-token estimate for the text half of `messages`."""
    blob = " ".join(message_text(m.get("content")) for m in messages)
    return max(len(blob) // 4, 1)


def billable_output_tokens(usage: Any) -> int:
    """Output tokens actually generated, including any the model hid.

    A measured difference between the two accounts, and the reason this is not
    just `usage.completion_tokens`. From a real `grok-4-1-fast-reasoning` reply
    on the xAI account:

        prompt_tokens 1802, completion_tokens 1, total_tokens 2330,
        completion_tokens_details.reasoning_tokens 527

    1802 + 1 is 1803, not 2330. On that endpoint reasoning tokens are billed
    but are **not** counted inside `completion_tokens`, so a ledger written
    from `completion_tokens` records one output token for a reply that
    generated 528 — a 500x undercount on the half of the bill that is priced
    at $30/1M on the current reason tier. On the Azure OpenAI account
    `total == prompt + completion` and reasoning is already inside
    `completion_tokens`, so this returns exactly what it did before.

    Both readings are taken and the larger wins, rather than trusting either
    field: an endpoint that reports a sane `completion_tokens` and an absent or
    zero `total_tokens` must not have its output silently zeroed.
    """
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or 0)
    return max(completion, total - prompt, 0)


def cached_prompt_tokens(usage: object) -> int:
    """How many of `prompt_tokens` Azure served from its automatic prompt cache.

    Azure OpenAI and the OpenAI API both cache prompt prefixes automatically
    above 1,024 tokens, match in 128-token increments beyond that, and bill the
    matched part at 50% of the input rate. The only signal that it happened is
    `usage.prompt_tokens_details.cached_tokens`, and until this function
    existed nothing here read it: the cache could have been hitting on every
    call or on none of them and the product could not have told the difference.

    It is *not* subtracted from `input_tokens`. The cached tokens are a subset
    of `prompt_tokens`, not additional to it, and the same rule the image
    counter follows applies here: the ledger sees one number and this field
    only says what it was made of. Pricing the discount is a separate decision
    and belongs with whoever owns `TIER_PRICES`.

    Defensive about the shape because the field is absent on older API
    versions, on the keyless mock path, and on endpoints that are not Azure --
    an absent field means "no cache reported", which reads as zero.
    """
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    if isinstance(details, dict):
        return int(details.get("cached_tokens") or 0)
    return int(getattr(details, "cached_tokens", 0) or 0)


def is_retryable(exc: BaseException) -> bool:
    """Transient failures only: connection drops, throttling, and 5xx."""
    if isinstance(exc, (APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        return 500 <= int(getattr(exc, "status_code", 0) or 0) < 600
    return False


def _retrying() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(is_retryable),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# Tool calling
# ---------------------------------------------------------------------------
#
# The whole reason this section exists: a model that is *given* tools calls
# them, and a model asked to append a fenced JSON block to its prose
# editorialises instead. Three consecutive turns of the shipped product
# announced a plan and executed nothing, because the only way to act was to
# emit a directive the model kept declining to emit. `tool_calls` is the API's
# own channel for "I want to do this", and it is not prose, so it cannot be
# narrated away.


@dataclass(frozen=True)
class ToolCall:
    """One function call the model asked for, with its arguments decoded.

    `arguments` is always a dict. When the model emits arguments that are not
    valid JSON the raw string is kept and `parse_error` is set, so the caller
    can hand the model its own mistake back rather than quietly running the
    call with empty inputs.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None


def decode_tool_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    """`(arguments, parse_error)` from the JSON string the API returns."""
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        return {}, f"the arguments were not valid JSON ({exc})"
    if not isinstance(data, dict):
        return {}, f"the arguments decoded to {type(data).__name__}, not an object"
    return data, None


def parse_tool_calls(message: Any) -> list[ToolCall]:
    """`ToolCall`s off a non-streamed chat completion message."""
    calls: list[ToolCall] = []
    for index, call in enumerate(getattr(message, "tool_calls", None) or []):
        function = getattr(call, "function", None)
        name = str(getattr(function, "name", "") or "")
        if not name:
            continue
        raw = getattr(function, "arguments", None)
        arguments, error = decode_tool_arguments(raw)
        calls.append(
            ToolCall(
                id=str(getattr(call, "id", "") or f"call_{index}"),
                name=name,
                arguments=arguments,
                raw_arguments=str(raw or ""),
                parse_error=error,
            )
        )
    return calls


def _accumulate_tool_call_deltas(
    buffer: dict[int, dict[str, Any]], delta: Any
) -> None:
    """Fold one streamed `delta.tool_calls` chunk into `buffer`.

    A streamed function call arrives as a name on one chunk and its arguments a
    few characters at a time on the ones after it, keyed by `index`. Anything
    that drops the partial chunks reads as "the model said nothing", which is
    exactly the failure the streaming path had before: the desktop loop was
    unreachable from `POST /threads/{id}/messages/stream`.
    """
    for chunk in getattr(delta, "tool_calls", None) or []:
        index = int(getattr(chunk, "index", 0) or 0)
        slot = buffer.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_id = getattr(chunk, "id", None)
        if call_id:
            slot["id"] = str(call_id)
        function = getattr(chunk, "function", None)
        if function is None:
            continue
        name = getattr(function, "name", None)
        if name:
            slot["name"] = str(name)
        arguments = getattr(function, "arguments", None)
        if arguments:
            slot["arguments"] += str(arguments)


def _finish_tool_call_deltas(buffer: dict[int, dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index in sorted(buffer):
        slot = buffer[index]
        if not slot["name"]:
            continue
        arguments, error = decode_tool_arguments(slot["arguments"])
        calls.append(
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=arguments,
                raw_arguments=slot["arguments"],
                parse_error=error,
            )
        )
    return calls


def assistant_tool_call_message(content: str, tool_calls: list[ToolCall]) -> dict[str, Any]:
    """The assistant turn to append to the conversation before the tool results.

    Chat completions rejects a `tool` message whose `tool_call_id` was never
    announced, so the assistant message carrying the calls has to go back into
    the history verbatim.
    """
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.raw_arguments or json.dumps(call.arguments),
                },
            }
            for call in tool_calls
        ],
    }


def tool_result_message(tool_call_id: str, content: str) -> dict[str, Any]:
    """One `tool` role reply. Text only.

    Chat completions does not accept image parts on a `tool` message, so a
    screenshot cannot ride back on the result of the action that produced it.
    The orchestrator sends the text here and the picture as a separate `user`
    message immediately after.
    """
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


@dataclass
class ChatResult:
    content: str
    tier: Tier
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    raw: Any | None = None
    #: How many of `input_tokens` were images. Always *included* in
    #: `input_tokens`, never additional to it, so the cost ledger and the daily
    #: budget see one number and this field only says what it was made of.
    image_tokens: int = 0
    #: Function calls the model asked for. Empty on a plain text reply, and
    #: always empty in mock mode — a mock that invented tool calls would be
    #: fabricating actions, which is the one thing this codebase will not do.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: How many of `input_tokens` the provider served from its prompt cache.
    #: A subset of `input_tokens`, never additional to it. Zero means either a
    #: miss or an endpoint that does not report one -- see
    #: `cached_prompt_tokens`.
    cached_tokens: int = 0


# ---------------------------------------------------------------------------
# Anthropic — a genuinely different wire format, translated at the edges
# ---------------------------------------------------------------------------
#
# Unlike `azure`/`openai`, which share one client shape because `AsyncOpenAI`
# and `AsyncAzureOpenAI` are the same SDK, Anthropic's Messages API is not
# OpenAI-compatible: the system prompt is a top-level `system` param, not a
# message role; tool definitions are `{name, description, input_schema}`, not
# OpenAI's `{type:"function", function:{...}}`; a reply's content is a list of
# blocks (`text` / `tool_use`), not `message.content` + `message.tool_calls`;
# usage is `input_tokens`/`output_tokens`, not `prompt_tokens`/
# `completion_tokens`; streaming is SSE events (`message_start`,
# `content_block_start`, `content_block_delta` carrying `text_delta` or
# `input_json_delta`, `content_block_stop`, `message_delta` carrying the final
# usage, `message_stop`), not OpenAI-shaped chunks.
#
# Rather than teach `_create`/`chat`/`stream_chat` a second wire format, this
# translates at the edges: `_AnthropicAdapter` exposes
# `.chat.completions.create(**kwargs)` taking the exact kwargs
# `_request_kwargs` already builds, and returns objects shaped like the
# OpenAI SDK's — so every line downstream of `client()` (`parse_tool_calls`,
# `billable_output_tokens`, `_accumulate_tool_call_deltas`) stays exactly as
# blind to the provider as it already is for azure vs openai. Verified against
# the real `anthropic` 1.0.0 SDK's actual type definitions (field names below
# are not guessed), never against a live account — see
# `tests/services/test_model_router_anthropic.py`.

#: The Messages API requires `max_tokens` on every request; the OpenAI-shaped
#: `_request_kwargs` this adapter receives never sets one, because
#: `chat.completions.create` does not require it. There is no per-tier signal
#: to size this from — `_request_kwargs` deliberately does not pass `tier`
#: into the kwargs dict, the same "the adapter cannot tell which tier it is"
#: constraint that keeps it provider-blind — so one flat number, generous
#: enough for a `reason`/`deep_plan` turn's longer replies without being
#: unbounded.
ANTHROPIC_DEFAULT_MAX_TOKENS = 8192


def _anthropic_content(content: Any) -> Any:
    """An OpenAI-shaped message `content` value, translated to Anthropic's.

    A plain string passes through unchanged. A content-part list (vision
    turns — see `image_content_part`) needs its `image_url` parts rewritten:
    OpenAI inlines a data URL (`{type:"image_url", image_url:{url:"data:...
    ;base64,X", detail}}`); Anthropic wants the media type and the base64
    payload as separate fields (`{type:"image", source:{type:"base64",
    media_type, data:X}}`). A part this function does not recognise (neither
    `text` nor `image_url`) is dropped rather than sent malformed — better a
    turn is missing one part than a 400 on the whole request.
    """
    if not isinstance(content, list):
        return content
    out: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            out.append({"type": "text", "text": str(part.get("text") or "")})
        elif ptype == "image_url":
            node = part.get("image_url")
            url = node.get("url", "") if isinstance(node, dict) else str(node or "")
            if url.startswith(_DATA_URL_PREFIX) and ";base64," in url:
                header, _, payload = url.partition(";base64,")
                media_type = header[len(_DATA_URL_PREFIX) :] or "image/png"
                out.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": payload},
                    }
                )
    return out


def _anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI-shaped `messages` -> Anthropic's `(system, messages)`.

    Only the first `system` message becomes the top-level `system` param — the
    Messages API takes exactly one. A second one (a caller composing messages
    by hand, not something this codebase's own callers do today) is folded
    into the conversation as a user turn rather than silently dropped, since
    dropping a message an operator wrote is worse than misplacing it.

    A `tool` role (see `tool_result_message`) becomes a user turn carrying a
    `tool_result` block — Anthropic has no `tool` role. An assistant message
    carrying `tool_calls` (see `assistant_tool_call_message`) becomes an
    assistant turn whose content is a list of blocks: the text, if any, then
    one `tool_use` block per call, arguments parsed back from the JSON string
    `assistant_tool_call_message` encoded them as.
    """
    system = ""
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            if not system:
                system = message_text(message.get("content"))
                continue
            out.append({"role": "user", "content": message_text(message.get("content"))})
            continue
        if role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": message.get("content") or "",
                        }
                    ],
                }
            )
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = message.get("content")
            if text:
                blocks.append({"type": "text", "text": str(text)})
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                arguments, _ = decode_tool_arguments(function.get("arguments"))
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": arguments,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue
        out.append(
            {
                "role": role if role in ("user", "assistant") else "user",
                "content": _anthropic_content(message.get("content")),
            }
        )
    return system, out


def _anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """OpenAI function-tool defs -> Anthropic's `{name, description, input_schema}`."""
    if not tools:
        return None
    out = []
    for tool in tools:
        function = tool.get("function") or {}
        out.append(
            {
                "name": function.get("name") or "",
                "description": function.get("description") or "",
                "input_schema": (
                    function["parameters"]
                    if function.get("parameters") is not None
                    else {"type": "object", "properties": {}}
                ),
            }
        )
    return out


def _anthropic_tool_choice(tool_choice: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """No caller in this codebase sets `tool_choice` today (grep confirms it —
    every `chat()`/`stream_chat()` call site leaves it at the default `None`),
    so this exists for completeness and whatever calls it next, not because
    anything currently exercises it live."""
    if tool_choice is None:
        return None
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "none":
        return {"type": "none"}
    if isinstance(tool_choice, dict):
        name = ((tool_choice.get("function") or {}).get("name")) if isinstance(tool_choice, dict) else None
        if name:
            return {"type": "tool", "name": name}
    return {"type": "auto"}


def _anthropic_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    """The exact kwargs `_request_kwargs` builds -> an Anthropic Messages API request.

    `reasoning_effort` is deliberately dropped, not mapped. Anthropic's
    equivalent — extended thinking — is a differently-shaped `thinking` param
    (`{"type":"enabled","budget_tokens":N}`), not a graded string, and
    mapping one onto the other is a separate feature this adapter does not
    attempt. Silently dropping it here is intentional and safe: `_create`'s
    existing rejected-effort machinery exists for the case of a deployment
    that rejects an unknown parameter with a 400, but that machinery is for
    parameters this adapter *sends*, and this one simply never does.
    """
    system, messages = _anthropic_messages(kwargs["messages"])
    request: dict[str, Any] = {
        "model": kwargs["model"],
        "messages": messages,
        "max_tokens": ANTHROPIC_DEFAULT_MAX_TOKENS,
    }
    if system:
        request["system"] = system
    tools = _anthropic_tools(kwargs.get("tools"))
    if tools:
        request["tools"] = tools
    tool_choice = _anthropic_tool_choice(kwargs.get("tool_choice"))
    if tool_choice:
        request["tool_choice"] = tool_choice
    return request


def _openai_shaped_message(text: str, tool_calls: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        content=text,
        tool_calls=[
            SimpleNamespace(
                id=str(call.get("id") or ""),
                function=SimpleNamespace(
                    name=str(call.get("name") or ""),
                    arguments=json.dumps(call.get("input") or {}),
                ),
            )
            for call in tool_calls
        ]
        or None,
    )


def _anthropic_response_to_openai_shape(message: Any) -> SimpleNamespace:
    """An Anthropic `Message` -> an object shaped like an OpenAI chat completion.

    `message.content` is a list of blocks (`TextBlock`/`ToolUseBlock`); text
    blocks concatenate into `.choices[0].message.content`, `tool_use` blocks
    become `.choices[0].message.tool_calls` with JSON-encoded arguments —
    `parse_tool_calls` decodes that same string straight back with
    `decode_tool_arguments`, the same round trip an OpenAI response already
    takes. `usage.input_tokens`/`.output_tokens` map onto
    `prompt_tokens`/`completion_tokens`; `cache_read_input_tokens`, when
    present, becomes `prompt_tokens_details.cached_tokens` — the same field
    `cached_prompt_tokens()` already reads for Azure's own prompt cache.
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in message.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(str(getattr(block, "text", "") or ""))
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                }
            )
    usage = message.usage
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=_openai_shaped_message("".join(text_parts), tool_calls))],
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


async def _anthropic_stream_to_openai_chunks(events: Any) -> AsyncIterator[SimpleNamespace]:
    """Anthropic SSE events -> the OpenAI-shaped chunks `stream_chat` already
    knows how to fold (`_accumulate_tool_call_deltas`) and price
    (`billable_output_tokens`, `cached_prompt_tokens`).

    * `message_start` carries the prompt's `input_tokens` (and any cache-read
      count) up front; captured and re-attached to the usage chunk this
      yields at the end, since `message_delta` — the event that actually
      carries `output_tokens` — does not reliably repeat it.
    * `content_block_start` on a `tool_use` block is the one place a tool
      call's `id`/`name` are known; translated to a chunk carrying them with
      empty `arguments`, matching the first chunk of an OpenAI streamed tool
      call.
    * `content_block_delta` splits on `delta.type`: `text_delta` is a content
      chunk, `input_json_delta` is an arguments-fragment chunk for the tool
      call at the same block `index` — `_accumulate_tool_call_deltas` keys its
      buffer on exactly that index, so Anthropic's own content-block index is
      reused unchanged rather than remapped.
    * `message_delta` carries the final usage; yielded as a usage-only chunk
      (`choices=[]`) so `stream_chat`'s `getattr(chunk, "usage", None)` check
      picks it up without also (incorrectly) trying to read a `.delta` off it.
    * `content_block_stop`, `message_stop`, and anything else unrecognised
      carry nothing this router reads and are skipped.
    """
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    async for event in events:
        etype = getattr(event, "type", None)
        if etype == "message_start":
            usage = getattr(event.message, "usage", None)
            if usage is not None:
                input_tokens = int(getattr(usage, "input_tokens", 0) or 0) or input_tokens
                cached_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0) or cached_tokens
            continue
        if etype == "content_block_start":
            block = event.content_block
            if getattr(block, "type", None) == "tool_use":
                yield SimpleNamespace(
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=event.index,
                                        id=str(getattr(block, "id", "") or ""),
                                        function=SimpleNamespace(
                                            name=str(getattr(block, "name", "") or ""), arguments=""
                                        ),
                                    )
                                ],
                            )
                        )
                    ],
                )
            continue
        if etype == "content_block_delta":
            delta = event.delta
            dtype = getattr(delta, "type", None)
            if dtype == "text_delta":
                text = str(getattr(delta, "text", "") or "")
                yield SimpleNamespace(
                    usage=None,
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))],
                )
            elif dtype == "input_json_delta":
                yield SimpleNamespace(
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=event.index,
                                        id=None,
                                        function=SimpleNamespace(
                                            name=None, arguments=str(getattr(delta, "partial_json", "") or "")
                                        ),
                                    )
                                ],
                            )
                        )
                    ],
                )
            continue
        if etype == "message_delta":
            usage = getattr(event, "usage", None)
            if usage is not None:
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0) or output_tokens
                input_tokens = int(getattr(usage, "input_tokens", 0) or 0) or input_tokens
                cached_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0) or cached_tokens
            yield SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
                ),
                choices=[],
            )
            continue
        # content_block_stop, message_stop, ping: nothing to translate.


class _AnthropicCompletions:
    """`.create(**kwargs)` — the one method `_create()` ever calls."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> Any:
        request = _anthropic_request(kwargs)
        if kwargs.get("stream"):
            raw = await self._client.messages.create(**request, stream=True)
            return _anthropic_stream_to_openai_chunks(raw)
        return _anthropic_response_to_openai_shape(await self._client.messages.create(**request))


class _AnthropicAdapter:
    """Wraps a real `AsyncAnthropic` so `.chat.completions.create(**kwargs)`
    behaves exactly like the OpenAI SDK client `_create()` already expects —
    see the module-level docstring above this class for why."""

    def __init__(self, client: Any) -> None:
        self.chat = SimpleNamespace(completions=_AnthropicCompletions(client))


class ModelRouter:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        #: One client per `(endpoint, api_version)`, not one per router. Two
        #: tiers pointed at the same account share a client — and its
        #: connection pool and its token cache — while a tier pointed at the
        #: xAI account gets its own. Keyed rather than counted so adding a
        #: third account is a config change and not a code change.
        self._clients: dict[tuple[str, str], AsyncAzureOpenAI | AsyncOpenAI | _AnthropicAdapter | None] = {}
        #: An explicit override that serves *every* tier, ignoring the endpoint
        #: table entirely. Set by tests and by anything that has already built
        #: a client; a router handed one talks to that and nothing else, which
        #: is the only behaviour a caller who set it could reasonably expect.
        self._client: AsyncAzureOpenAI | AsyncOpenAI | _AnthropicAdapter | None = None
        self.last_result: ChatResult | None = None
        #: Set by `client()`; None until it has been called at least once. With
        #: more than one endpoint in play this is the mode of the *most recent*
        #: resolution, which is what the "why is it mocking?" question wants;
        #: `auth_modes` keeps the per-endpoint answer.
        self.auth_mode: AuthMode | None = None
        self.auth_modes: dict[str, AuthMode] = {}
        #: `(deployment, effort)` pairs this process has seen rejected with a
        #: 400. Keyed by the pair rather than by the deployment because the
        #: refusal depends on the *value*: `gpt-5.6-sol` takes `"none"` and
        #: refuses `"high"`, so remembering only "sol said no" would switch off
        #: the setting that works. One wasted request per pair, once. See
        #: `_create`.
        self.rejected_efforts: set[tuple[str, str]] = set()

    @property
    def supports_tools(self) -> bool:
        """Whether a `tools=` argument can actually produce a tool call.

        False in mock mode, where replies are canned text and no function call
        will ever come back. Callers use it to decide whether "the model
        replied in prose instead of calling a tool" is a protocol violation
        worth re-prompting over, or simply what this deployment does.

        Asked of the tier the agent loop actually runs on, not of the router as
        a whole: with per-tier endpoints, "is there a live account" has a
        different answer per tier, and the caller of this property is the
        desktop loop deciding whether prose-instead-of-a-tool-call is a bug.
        """
        return self.client(route_task(AGENT_LOOP_TASK)) is not None

    def supports_tools_for(self, bot: Bot | None = None) -> bool:
        """`supports_tools`, but honouring a bot's own provider/model pin.

        A self-hosted deployment can run entirely on a non-Azure provider —
        `AZURE_OPENAI_ENDPOINT` genuinely empty, on purpose — in which case
        the plain `supports_tools` property (always the default tier's
        account) reads False for every bot even when a bot's own override
        provider is live and perfectly capable of tool calls. Callers that
        have a bot in hand should use this instead.
        """
        override = self._bot_override(bot)
        if override is None:
            return self.supports_tools
        provider, _ = override
        return self._client_for(provider) is not None

    def _deployment(self, tier: Tier) -> str:
        return {
            "nano": self.settings.azure_deployment_nano,
            "mini": self.settings.azure_deployment_mini,
            "reason": self.settings.azure_deployment_reason,
            "embed": self.settings.azure_deployment_embed,
        }[tier]

    def _openai_model(self, tier: Tier) -> str:
        return {
            "nano": self.settings.openai_model_nano,
            "mini": self.settings.openai_model_mini,
            "reason": self.settings.openai_model_reason,
            "embed": self.settings.openai_model_embed,
        }[tier]

    def _anthropic_model(self, tier: Tier) -> str:
        return {
            "nano": self.settings.anthropic_model_nano,
            "mini": self.settings.anthropic_model_mini,
            "reason": self.settings.anthropic_model_reason,
            "embed": self.settings.anthropic_model_embed,
        }[tier]

    def model_name(self, tier: Tier) -> str:
        """The model/deployment name `tier` resolves to under its active provider.

        Empty for `google` (no client implementation yet, see `client()`) or
        for `openai`/`anthropic` with no model name configured - both read as
        "cannot be resolved", the same outcome an Azure tier with no
        deployment name would produce.
        """
        provider = self._provider_for(tier)
        if provider == "azure":
            return self._deployment(tier)
        if provider == "openai":
            return self._openai_model(tier)
        if provider == "anthropic":
            return self._anthropic_model(tier)
        return ""

    def _provider_for(self, tier: Tier | None) -> Provider:
        """Which provider serves `tier`. `tier=None` resolves the global
        default only, matching the `tier=None` case of `_endpoint_for` -
        no per-tier override lookup."""
        raw = self.settings.model_provider if tier is None else (
            getattr(self.settings, f"model_provider_{tier}", "") or ""
        ).strip() or self.settings.model_provider
        value = (raw or "azure").strip().lower()
        if value not in ("azure", "openai", "anthropic", "google"):
            logger.warning("unknown model_provider %r for tier %r — falling back to azure", raw, tier)
            return "azure"
        return value  # type: ignore[return-value]

    def _openai_config_for(self, tier: Tier | None) -> tuple[str, str]:
        """`(base_url, api_key)` for one tier on the `openai` provider.

        Blank `base_url` means the SDK's own default (real OpenAI). `tier` is
        None for the same "default account only" case `_endpoint_for`
        documents.
        """
        shared_url = (self.settings.openai_base_url or "").strip()
        shared_key = (self.settings.openai_api_key or "").strip()
        if tier is None:
            return shared_url, shared_key
        url = (getattr(self.settings, f"openai_base_url_{tier}", "") or "").strip() or shared_url
        key = (getattr(self.settings, f"openai_api_key_{tier}", "") or "").strip() or shared_key
        return url, key

    def _anthropic_config_for(self, tier: Tier | None) -> str:
        """The api key for one tier on the `anthropic` provider.

        No base_url counterpart: unlike `openai` (which also has to cover a
        self-hosted server), Anthropic is one fixed endpoint, so there is
        nothing here to route besides the key.
        """
        shared = (self.settings.anthropic_api_key or "").strip()
        if tier is None:
            return shared
        return (getattr(self.settings, f"anthropic_api_key_{tier}", "") or "").strip() or shared

    def _endpoint_for(self, tier: Tier | None) -> tuple[str, str, str]:
        """`(endpoint, api_key, api_version)` for one tier.

        The whole point of the per-tier override, and the one rule worth
        stating twice: **a key belongs to an account, not to the router.** A
        tier pointed at its own endpoint gets its own key or no key at all.
        Falling back to `azure_openai_api_key` would present the OpenAI-kind
        account's key to the xAI account, and the 401 that comes back reads
        exactly like a broken deployment rather than like a config mistake.

        `tier` is None for callers that only want "the default account" — the
        `supports_tools`-style question a per-tier answer would over-specify.
        """
        shared = (self.settings.azure_openai_endpoint or "").strip()
        shared_version = self.settings.azure_openai_api_version
        if tier is None:
            return shared, (self.settings.azure_openai_api_key or "").strip(), shared_version

        override = (getattr(self.settings, f"azure_openai_endpoint_{tier}", "") or "").strip()
        version = (
            getattr(self.settings, f"azure_openai_api_version_{tier}", "") or ""
        ).strip() or shared_version
        if not override or override == shared:
            key = (
                getattr(self.settings, f"azure_openai_api_key_{tier}", "") or ""
            ).strip() or (self.settings.azure_openai_api_key or "").strip()
            return shared, key, version

        _warn_tier_is_off_the_default_account(tier, override, self._deployment(tier))
        return override, (getattr(self.settings, f"azure_openai_api_key_{tier}", "") or "").strip(), version

    @staticmethod
    def _bot_override(bot: Bot | None) -> tuple[Provider, str] | None:
        """`(provider, model)` a bot has pinned itself to, or None to use tier
        routing - the default, and the only behaviour for every bot that
        predates this column.

        Both `model_provider` and `model_name` must be set: one without the
        other cannot be resolved to a model (the API layer already rejects
        this combination at write time — `schemas._validate_model_override` —
        this is the read-time half of the same rule, for rows written before
        that validation existed, or written directly against the database).
        An unrecognised `model_provider` string is treated the same as unset,
        for the same reason.
        """
        if bot is None:
            return None
        provider = (bot.model_provider or "").strip().lower()
        model = (bot.model_name or "").strip()
        if not provider or not model or provider not in ("azure", "openai", "anthropic", "google"):
            return None
        return provider, model  # type: ignore[return-value]

    def _client_for(self, provider: Provider) -> AsyncAzureOpenAI | AsyncOpenAI | _AnthropicAdapter | None:
        """The client for a bare provider name, independent of any tier.

        A bot override is not tied to a tier - there is one account per
        provider (the same global `AZURE_OPENAI_*`/`OPENAI_*`/`ANTHROPIC_*`
        settings tier routing already uses), so this always resolves the
        *shared* account, the same `tier=None` case
        `_azure_client`/`_openai_client`/`_anthropic_client` already handle
        for their tier-agnostic callers.
        """
        if provider == "openai":
            return self._openai_client(None)
        if provider == "azure":
            return self._azure_client(None)
        if provider == "anthropic":
            return self._anthropic_client(None)
        detail = f"provider {provider!r} has no client implementation yet"
        self._note_auth(f"{provider}::bot-override", "mock", detail)
        return None

    def provider_available(self, provider: Provider) -> bool:
        """Whether `provider` can actually be reached right now — a live
        credential resolves, not just an accepted config value.

        For the setup wizard and `GET /bots/providers`: a self-hoster should
        see which providers this deployment can actually use before being
        offered them per bot, rather than discovering "anthropic" quietly
        mocks because nobody set `ANTHROPIC_API_KEY`.
        """
        return self._client_for(provider) is not None

    def client(self, tier: Tier | None = None) -> AsyncAzureOpenAI | AsyncOpenAI | _AnthropicAdapter | None:
        """The client for `tier`'s active provider, or None when there is
        nothing to talk to.

        Dispatches on `_provider_for(tier)` first; everything else in this
        class (`_request_kwargs`, `parse_tool_calls`, `billable_output_tokens`,
        the streaming delta accumulator) reads the OpenAI response shape that
        `AsyncAzureOpenAI`, `AsyncOpenAI`, and `_AnthropicAdapter` all return,
        so nothing downstream of this method needs to know which provider
        produced a `ChatResult`.

        * **azure** — see `_azure_client`: api_key, managed_identity, or mock.
        * **openai** — see `_openai_client`: real OpenAI (api key required) or
          a self-hosted OpenAI-compatible server, "local models" included -
          same client, different `base_url`.
        * **anthropic** — see `_anthropic_client`: api_key or mock. Request/
          response translated to and from the OpenAI shape at the edges - see
          the `_Anthropic*` section above this class.
        * **google** — accepted config value, no client implementation yet.
          Falls back to mock rather than guessing at a wire format nobody has
          built.
        """
        if self._client is not None:
            return self._client

        provider = self._provider_for(tier)
        if provider == "anthropic":
            return self._anthropic_client(tier)
        if provider == "openai":
            return self._openai_client(tier)
        if provider != "azure":
            detail = f"provider {provider!r} has no client implementation yet"
            self._note_auth(f"{provider}::{tier}", "mock", detail)
            return None
        return self._azure_client(tier)

    def _azure_client(self, tier: Tier | None) -> AsyncAzureOpenAI | None:
        """Three outcomes, in order, and they are decided **per endpoint**
        rather than once for the router: a deployment can perfectly well have
        a live `reason` account and a mock everything-else, and collapsing
        that into one answer is how a working tier ends up returning canned
        text.

        * **api_key** — endpoint and key both set. Local dev against a real
          Foundry account; unchanged behaviour.
        * **managed_identity** — endpoint set, no key. Production: the container
          holds a user-assigned identity with `Cognitive Services OpenAI User`
          on the account, so we mint Entra tokens instead. The credential is
          built here rather than at import, so mock deployments never touch
          `azure.identity`. Both accounts take the same
          `https://cognitiveservices.azure.com/.default` token, which is why
          one provider serves any number of endpoints.
        * **mock** — no endpoint (or the identity could not be built).
          `chat`/`stream_chat` return canned text. Keyless local dev depends on
          this staying reachable.

        Returning None used to be the *only* keyless outcome, which is why every
        production reply read `[mock:mini] …`: the deployment has an endpoint but
        deliberately no key.
        """
        endpoint, api_key, api_version = self._endpoint_for(tier)
        cache_key = (endpoint, api_version)
        if cache_key in self._clients:
            cached = self._clients[cache_key]
            self.auth_mode = self.auth_modes.get(endpoint, "mock")
            # Every azure cache_key is only ever set by this method, below, to
            # an AsyncAzureOpenAI|None - the dict's value type is the union
            # only because `_openai_client` shares the same cache.
            return cast("AsyncAzureOpenAI | None", cached)

        if not endpoint:
            self._clients[cache_key] = None
            self._note_auth(endpoint, "mock", "AZURE_OPENAI_ENDPOINT is empty")
            return None

        common: dict[str, Any] = {
            "azure_endpoint": endpoint,
            "api_version": api_version,
            "timeout": self.settings.request_timeout_seconds,
        }

        if api_key:
            client = AsyncAzureOpenAI(api_key=api_key, **common)
            self._clients[cache_key] = client
            self._note_auth(endpoint, "api_key", f"endpoint={endpoint}")
            return client

        client_id = (self.settings.azure_managed_identity_client_id or "").strip()
        provider = _bearer_token_provider(client_id)
        if provider is None:
            self._clients[cache_key] = None
            self._note_auth(
                endpoint, "mock", "no AZURE_OPENAI_API_KEY and no usable managed identity"
            )
            return None

        client = AsyncAzureOpenAI(azure_ad_token_provider=provider, **common)
        self._clients[cache_key] = client
        self._note_auth(
            endpoint,
            "managed_identity",
            f"endpoint={endpoint} client_id={client_id or 'system-assigned'}",
        )
        return client

    def _openai_client(self, tier: Tier | None) -> AsyncOpenAI | None:
        """Real OpenAI, or a self-hosted OpenAI-compatible server ("local
        models" included — same client, a different `base_url`).

        * **api_key** — a key is configured. Real OpenAI requires one; a
          self-hosted server that also wants one (OpenRouter, a gateway with
          auth in front of it) gets it here too.
        * **unauthenticated** — `base_url` set, no key. Most self-hosted
          servers (Ollama, vLLM, LM Studio) accept any non-empty string, so one
          is sent rather than failing a request that would otherwise work.
        * **mock** — neither is set. Real OpenAI cannot be reached keyless, and
          nothing points at a self-hosted server either.
        """
        base_url, api_key = self._openai_config_for(tier)
        cache_key = (f"openai::{base_url}", "")
        if cache_key in self._clients:
            cached = self._clients[cache_key]
            self.auth_mode = self.auth_modes.get(base_url or "openai::default", "mock")
            return cast("AsyncOpenAI | None", cached)

        if not base_url and not api_key:
            self._clients[cache_key] = None
            self._note_auth(base_url or "openai::default", "mock", "no OPENAI_API_KEY and no OPENAI_BASE_URL")
            return None

        if tier is not None:
            _warn_tier_pricing_may_not_match_provider(tier, "openai")

        common: dict[str, Any] = {"timeout": self.settings.request_timeout_seconds}
        if base_url:
            common["base_url"] = base_url

        if api_key:
            client = AsyncOpenAI(api_key=api_key, **common)
            self._clients[cache_key] = client
            self._note_auth(base_url or "openai::default", "api_key", f"base_url={base_url or 'https://api.openai.com/v1'}")
            return client

        client = AsyncOpenAI(api_key="not-needed", **common)
        self._clients[cache_key] = client
        self._note_auth(base_url, "unauthenticated", f"base_url={base_url}")
        return client

    def _anthropic_client(self, tier: Tier | None) -> _AnthropicAdapter | None:
        """A real `AsyncAnthropic` client wrapped in `_AnthropicAdapter`, or
        None when no key is configured - there is no keyless outcome for
        Anthropic the way `_openai_client` has "unauthenticated" for a
        self-hosted server, since Anthropic's API is one fixed endpoint that
        always requires a key.

        Cached on the resolved key itself, not on `tier`: two tiers sharing
        the shared key (the common case - no per-tier override) must share
        one client and its connection pool, the same rule `_azure_client`/
        `_openai_client` follow, just keyed differently since there is no
        `base_url` to key on here.
        """
        api_key = self._anthropic_config_for(tier)
        cache_key = (f"anthropic::{api_key}", "")
        if cache_key in self._clients:
            cached = self._clients[cache_key]
            self.auth_mode = self.auth_modes.get("anthropic", "mock")
            return cast("_AnthropicAdapter | None", cached)

        if not api_key:
            self._clients[cache_key] = None
            self._note_auth("anthropic", "mock", "no ANTHROPIC_API_KEY")
            return None

        if tier is not None:
            _warn_tier_pricing_may_not_match_provider(tier, "anthropic")

        # Lazy import, same pattern as `_bearer_token_provider`'s
        # `azure.identity` and `services/secrets.py`'s Key Vault client: a
        # deployment that never configures Anthropic never pays to import it,
        # even though it is a hard `requirements.txt` pin like `openai` is.
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=api_key, timeout=self.settings.request_timeout_seconds)
        adapter = _AnthropicAdapter(client)
        self._clients[cache_key] = adapter
        self._note_auth("anthropic", "api_key", "configured")
        return adapter

    def _note_auth(self, endpoint: str, mode: AuthMode, detail: str) -> None:
        self.auth_mode = mode
        self.auth_modes[endpoint] = mode
        _log_auth_mode(mode, detail)

    def _mock_content(self, tier: Tier, messages: list[dict[str, Any]]) -> str:
        user_text = next(
            (message_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        images = sum(
            1
            for m in messages
            if isinstance(m.get("content"), list)
            for p in m["content"]
            if isinstance(p, dict) and p.get("type") == "image_url"
        )
        # Says how many screenshots it was handed, and never what is on them —
        # a mock that described a screen it cannot see would be the exact
        # fabrication this product exists to avoid.
        seen = f"\n\nScreenshots attached: {images} (mock replies do not read them)." if images else ""
        return (
            f"[mock:{tier}] Acknowledged. I'll handle this as the active bot.\n\n"
            f"Request: {user_text[:500]}{seen}\n\n"
            "Next: I'll use connectors when available, otherwise Bot Desktop."
        )

    def _estimated_result(
        self, tier: Tier, messages: list[dict[str, Any]], content: str
    ) -> ChatResult:
        """A ChatResult for a call with no usage block, images priced in."""
        text_tokens = count_text_tokens(messages)
        image_tokens = count_image_tokens(messages)
        out_tokens = max(len(content) // 4, 1)
        return ChatResult(
            content,
            tier,
            text_tokens + image_tokens,
            out_tokens,
            estimate_cost_usd(tier, text_tokens, out_tokens, image_tokens=image_tokens),
            image_tokens=image_tokens,
        )

    def _request_kwargs(
        self,
        tier: Tier,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        reasoning_effort: str | None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        model = model_override or self.model_name(tier)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "timeout": self.settings.request_timeout_seconds,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        effort = normalise_effort(reasoning_effort)
        if effort and (model, effort) not in self.rejected_efforts:
            kwargs["reasoning_effort"] = effort
        return kwargs

    async def _create(self, client: Any, kwargs: dict[str, Any]) -> Any:
        """One chat completion, retried on transient failures.

        Also the one place that learns a deployment will not take a particular
        `reasoning_effort`: a 400 naming the parameter drops it, remembers the
        `(deployment, effort)` pair, and reissues the identical request. Every
        other 400 is raised, because a 400 about the *messages* silently
        retried without the effort hint would just fail again and hide its own
        cause.

        The pair, not the deployment: `gpt-5.6-sol` refuses `"high"` next to
        function tools and accepts `"none"`, so forgetting per-deployment would
        throw away the setting that works.
        """
        try:
            async for attempt in _retrying():
                with attempt:
                    return await client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "reasoning_effort" not in kwargs or not _rejects_reasoning_effort(exc):
                raise
            logger.warning(
                "deployment %s rejected reasoning_effort=%r (%s) — not sending that pair again",
                kwargs.get("model"),
                kwargs.get("reasoning_effort"),
                exc,
            )
            self.rejected_efforts.add(
                (str(kwargs.get("model")), str(kwargs.get("reasoning_effort")))
            )
            kwargs.pop("reasoning_effort", None)
        async for attempt in _retrying():
            with attempt:
                return await client.chat.completions.create(**kwargs)
        raise RuntimeError("the retry loop produced no response")  # pragma: no cover

    async def chat(
        self,
        *,
        task: TaskClass,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        fail_count: int = 0,
        reasoning_effort: str | None = None,
        bot: Bot | None = None,
    ) -> ChatResult:
        tier = route_task(task, fail_count)
        override = self._bot_override(bot)
        if override is not None:
            provider, model_override = override
            client = self._client_for(provider)
            # cost_ledger still bills this call at the task's ordinary tier
            # price - there is no way to know a bot-pinned model's real price
            # from here, same limitation as a tier-level provider override.
            _warn_tier_pricing_may_not_match_provider(tier, f"{provider} (bot override)")
        else:
            model_override = None
            client = self.client(tier)
        if client is None:
            # Local mock — deterministic helpful reply without Azure keys.
            # Deliberately no `tool_calls`: a mock that invented function calls
            # would make the agent loop act on instructions nobody gave it.
            result = self._estimated_result(tier, messages, self._mock_content(tier, messages))
            self.last_result = result
            return result

        kwargs = self._request_kwargs(tier, messages, tools, tool_choice, reasoning_effort, model_override)
        resp = await self._create(client, kwargs)

        choice = resp.choices[0].message
        content = choice.content or ""
        calls = parse_tool_calls(choice)
        usage = resp.usage
        # Azure counts image tokens inside `prompt_tokens`, so they are already
        # billed here; the estimate goes on the result purely so a reader can
        # see how much of the prompt was pixels.
        images = count_image_tokens(messages)
        if usage is None:
            result = self._estimated_result(tier, messages, content)
            result.raw = resp
        else:
            in_tok = usage.prompt_tokens or 0
            out_tok = billable_output_tokens(usage)
            result = ChatResult(
                content,
                tier,
                in_tok,
                out_tok,
                estimate_cost_usd(tier, in_tok, out_tok),
                raw=resp,
                image_tokens=images,
                cached_tokens=cached_prompt_tokens(usage),
            )
        result.tool_calls = calls
        self.last_result = result
        return result

    async def stream_chat(
        self,
        *,
        task: TaskClass,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        fail_count: int = 0,
        reasoning_effort: str | None = None,
        bot: Bot | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas. `self.last_result` holds the ChatResult once exhausted.

        Tool calls arrive on the same stream, in fragments, and are folded back
        together onto `last_result.tool_calls`. They are never yielded as text:
        a caller rendering the stream to a user must not print the machinery.
        """
        tier = route_task(task, fail_count)
        self.last_result = None
        override = self._bot_override(bot)
        if override is not None:
            provider, model_override = override
            client = self._client_for(provider)
            _warn_tier_pricing_may_not_match_provider(tier, f"{provider} (bot override)")
        else:
            model_override = None
            client = self.client(tier)

        if client is None:
            content = self._mock_content(tier, messages)
            words = content.split(" ")
            for index, word in enumerate(words):
                yield word if index == 0 else " " + word
            self.last_result = self._estimated_result(tier, messages, content)
            return

        kwargs = self._request_kwargs(tier, messages, tools, tool_choice, reasoning_effort, model_override)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        stream = await self._create(client, kwargs)

        parts: list[str] = []
        call_buffer: dict[int, dict[str, Any]] = {}
        in_tok = 0
        out_tok = 0
        cached_tok = 0
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                in_tok = usage.prompt_tokens or in_tok
                out_tok = billable_output_tokens(usage) or out_tok
                cached_tok = cached_prompt_tokens(usage) or cached_tok
            for choice in chunk.choices or []:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                _accumulate_tool_call_deltas(call_buffer, delta)
                text = getattr(delta, "content", None)
                if text:
                    parts.append(text)
                    yield text

        content = "".join(parts)
        calls = _finish_tool_call_deltas(call_buffer)
        if not in_tok:
            # No usage block came back, so nothing counted the images either —
            # price them here rather than letting a vision turn bill as text.
            result = self._estimated_result(tier, messages, content)
            result.tool_calls = calls
            self.last_result = result
            return
        if not out_tok:
            out_tok = max(len(content) // 4, 1)
        self.last_result = ChatResult(
            content,
            tier,
            in_tok,
            out_tok,
            estimate_cost_usd(tier, in_tok, out_tok),
            image_tokens=count_image_tokens(messages),
            tool_calls=calls,
            cached_tokens=cached_tok,
        )

    async def record_cost(self, db: AsyncSession, bot_id, result: ChatResult) -> None:
        db.add(
            CostLedger(
                bot_id=bot_id,
                tier=result.tier,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
            )
        )
        await db.commit()

    async def spent_today_usd(self, db: AsyncSession, bot_id) -> Decimal:
        from datetime import datetime, timezone

        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        q = await db.execute(
            select(func.coalesce(func.sum(CostLedger.cost_usd), 0)).where(
                CostLedger.bot_id == bot_id,
                CostLedger.created_at >= start,
            )
        )
        return Decimal(str(q.scalar_one()))
