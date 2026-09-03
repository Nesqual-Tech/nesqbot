"""A throttled model must not read as a broken product.

Measured on the live deployment, 2026-09-03, while the owner reported "same
shit, nothing works as it should":

    az cognitiveservices account deployment list -g rg-nesqbot \\
       -n nesqbot-xai-CHANGE_ME
    grok-4-1-fast-reasoning   GlobalStandard   capacity 50

Capacity 50 on GlobalStandard is **50,000 tokens per minute**, and
`route_task` sends every agent-loop step (`deep_plan`) to that one deployment.
The mean agent-loop request measured in `test_agent_context_budget.py` is
~9,800 prompt tokens, so the reason tier supports roughly **five calls a
minute** while a single run makes dozens and a delegated chain makes hundreds.

What that looked like from the outside, and why it read as "nothing works":

* `_retrying` gives a throttled call three attempts with backoff capped at 8s
  and then re-raises, so a long turn dies partway through.
* The turn handler marked the run `failed` and emitted an SSE `error` frame,
  and wrote **nothing to the thread** - an assistant message is only written
  when a turn *finishes*. The SSE frame is gone the moment the person switches
  tabs.
* So the transcript kept the person's own message and nothing else, which is
  indistinguishable from never having sent it. Confirmed from the live
  database: a user message at 18:14:07 whose run never produced a reply.

Two fixes, one per half of that: finish the call on a tier that is not
throttled, and when a turn does die, say so where the person is looking.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai import RateLimitError
from sqlalchemy import select

from app.models import Message
from app.services.model_router import (
    THROTTLE_FALLBACK,
    ModelRouter,
    _throttle_fallback,
    route_task,
)
from app.services.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# 1. Where a throttled call goes instead of dying
# ---------------------------------------------------------------------------


def _rate_limited() -> RateLimitError:
    """The real exception type, built the way the SDK builds it."""
    response = SimpleNamespace(status_code=429, headers={}, request=None)
    return RateLimitError("Requests to the model deployment have exceeded the rate limit", response=response, body=None)


def test_the_agent_loop_runs_on_the_tier_that_is_rate_limited():
    """The premise. If this ever stops being true the fallback is pointless."""
    from app.services.orchestrator import AGENT_LOOP_TASK

    assert route_task(AGENT_LOOP_TASK) == "reason"


def test_only_the_reason_tier_falls_back():
    """`mini` and `nano` have nowhere better to go; `embed` must never move.

    A different embedding model would produce vectors that do not compare with
    the ones already stored, so a "helpful" fallback there corrupts recall
    rather than degrading it.
    """
    assert THROTTLE_FALLBACK == {"reason": "mini"}
    assert _throttle_fallback("reason", False) == "mini"
    assert _throttle_fallback("mini", False) is None
    assert _throttle_fallback("nano", False) is None
    assert _throttle_fallback("embed", False) is None


def test_a_bot_pinned_to_a_model_is_never_moved_off_it():
    """Somebody chose that model. Answering on another one is undebuggable."""
    assert _throttle_fallback("reason", True) is None


class _ThrottledReasonRouter(ModelRouter):
    """A router whose `reason` client throttles and whose `mini` client works.

    Both are the real code path: `chat()` resolves a client per tier, and this
    only replaces the two clients and the request issuer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attempted: list[str] = []

    def client(self, tier):  # noqa: D102 - see class docstring
        return SimpleNamespace(tier=tier)

    async def _create(self, client, kwargs):
        self.attempted.append(client.tier)
        if client.tier == "reason":
            raise _rate_limited()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answered on mini", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=80),
        )


async def test_a_throttled_reason_call_finishes_on_mini():
    """The whole point: the step completes instead of taking the turn down."""
    router = _ThrottledReasonRouter()

    result = await router.chat(task="deep_plan", messages=[{"role": "user", "content": "go"}])

    assert router.attempted == ["reason", "mini"], "it did not try the fallback exactly once"
    assert result.tier == "mini", "the result must be billed as what actually served it"
    assert result.content == "answered on mini"


class _EverythingThrottled(_ThrottledReasonRouter):
    async def _create(self, client, kwargs):
        self.attempted.append(client.tier)
        raise _rate_limited()


async def test_when_the_fallback_is_throttled_too_the_error_is_raised():
    """No loop. One fallback, then the honest failure.

    A fallback that fell back again would be a retry loop wearing a different
    hat, and the caller has a failure path that now writes a sentence a person
    can read - see the second half of this file.
    """
    router = _EverythingThrottled()

    with pytest.raises(RateLimitError):
        await router.chat(task="deep_plan", messages=[{"role": "user", "content": "go"}])

    assert router.attempted == ["reason", "mini"]


# ---------------------------------------------------------------------------
# 2. A turn that dies says so where the person is looking
# ---------------------------------------------------------------------------


async def test_a_failed_turn_writes_a_message_the_person_can_read(db, user_a, make_thread, agent_bot):
    orchestrator = Orchestrator()
    thread = await make_thread(user_a, [agent_bot])

    await orchestrator._say_the_turn_failed(db, thread_id=thread.id, exc=RuntimeError("boom"))

    posted = (
        (await db.execute(select(Message).where(Message.thread_id == thread.id))).scalars().all()
    )
    assert len(posted) == 1
    note = posted[0]
    assert note.role == "assistant"
    assert "broke partway through" in note.content
    assert "Send it again" in note.content
    # The exception text belongs on `runs.error`, not in the person's face: what
    # reached them before this existed was a raw asyncpg InterfaceError with an
    # INSERT statement in it.
    assert "boom" not in note.content
    assert note.meta["turn_failed"] is True
    assert note.meta["error_type"] == "RuntimeError"


async def test_a_rate_limited_turn_says_that_specifically(db, user_a, make_thread, agent_bot):
    """The one failure that is neither a bug nor the person's fault.

    The advice for it - wait a minute, or ask for less - is different from the
    advice for anything else, so it gets its own sentence.
    """
    orchestrator = Orchestrator()
    thread = await make_thread(user_a, [agent_bot])

    await orchestrator._say_the_turn_failed(db, thread_id=thread.id, exc=_rate_limited())

    note = (
        (await db.execute(select(Message).where(Message.thread_id == thread.id))).scalars().all()
    )[0]
    assert "ran out of model capacity" in note.content
    assert "tokens a minute" in note.content
    assert "narrower piece" in note.content
    assert note.meta["error_type"] == "RateLimitError"


async def test_a_failure_with_no_thread_is_not_an_error(db):
    """Routine and inbound runs have no transcript to apologise in."""
    orchestrator = Orchestrator()
    await orchestrator._say_the_turn_failed(db, thread_id=None, exc=RuntimeError("boom"))


async def test_the_note_never_raises_out_of_the_failure_handler(db, user_a, make_thread, agent_bot, monkeypatch):
    """It runs inside `except`. A second failure here loses the first one."""
    orchestrator = Orchestrator()
    thread = await make_thread(user_a, [agent_bot])

    async def broken():
        raise RuntimeError("the session is gone too")

    monkeypatch.setattr(db, "commit", broken)
    await orchestrator._say_the_turn_failed(db, thread_id=thread.id, exc=RuntimeError("boom"))


# ---------------------------------------------------------------------------
# 3. Two reasoning deployments, because quota is per model
# ---------------------------------------------------------------------------
#
# The primary reason deployment holds its model's entire regional allowance —
# `grok-4-1-fast-reasoning`, 50 of a limit of 50 — so there is no raising it.
# A different xAI model has its own untouched 50, which is the only way to buy
# reasoning throughput today. `grok-4-6-reasoning` was created on the same
# account for exactly that, so the hop order is: primary reason deployment, the
# second one, then down a tier.


class _AltAwareRouter(ModelRouter):
    """Throttles the primary reason deployment; the named alternate works."""

    def __init__(self, alt: str = "grok-4-6-reasoning") -> None:
        super().__init__()
        self.settings = SimpleNamespace(
            azure_deployment_reason_alt=alt,
            request_timeout_seconds=60.0,
            azure_openai_api_version="2024-12-01-preview",
        )
        self.models: list[str] = []

    def client(self, tier):  # noqa: D102
        return SimpleNamespace(tier=tier)

    def model_name(self, tier):  # noqa: D102
        return {"reason": "grok-4-1-fast-reasoning", "mini": "gpt-5.4-mini"}[tier]

    def _request_kwargs(self, tier, messages, tools, tool_choice, effort, model_override=None):
        return {"model": model_override or self.model_name(tier), "messages": messages}

    async def _create(self, client, kwargs):
        self.models.append(kwargs["model"])
        if kwargs["model"] == "grok-4-1-fast-reasoning":
            raise _rate_limited()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=50),
        )


async def test_a_throttled_reason_call_tries_the_second_deployment_first():
    """It stays on a reasoning model rather than dropping to mini."""
    router = _AltAwareRouter()

    result = await router.chat(task="deep_plan", messages=[{"role": "user", "content": "plan"}])

    assert router.models == ["grok-4-1-fast-reasoning", "grok-4-6-reasoning"]
    assert result.tier == "reason", "the answer came from a reasoning model, so bill it as one"
    assert result.alt_deployment is True, "a reader has to be able to tell the good model was busy"


class _BothReasonDeploymentsThrottled(_AltAwareRouter):
    async def _create(self, client, kwargs):
        self.models.append(kwargs["model"])
        if kwargs["model"].startswith("grok"):
            raise _rate_limited()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="on mini", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=50),
        )


async def test_when_both_reason_deployments_are_full_it_drops_a_tier():
    """The last resort, and still an answer rather than a dead turn."""
    router = _BothReasonDeploymentsThrottled()

    result = await router.chat(task="deep_plan", messages=[{"role": "user", "content": "plan"}])

    assert router.models == [
        "grok-4-1-fast-reasoning",
        "grok-4-6-reasoning",
        "gpt-5.4-mini",
    ]
    assert result.tier == "mini"
    assert result.content == "on mini"


async def test_with_no_alternate_configured_it_drops_a_tier_immediately():
    """Empty is a valid deployment: one reason model, then mini."""
    router = _AltAwareRouter(alt="")

    result = await router.chat(task="deep_plan", messages=[{"role": "user", "content": "plan"}])

    assert router.models == ["grok-4-1-fast-reasoning", "gpt-5.4-mini"]
    assert result.tier == "mini"


# ---------------------------------------------------------------------------
# 4. Most loop steps never ask the scarce model at all
# ---------------------------------------------------------------------------


def test_the_loop_spends_reasoning_on_decisions_and_mini_on_execution():
    """The change that actually removes the pressure, rather than routing round it.

    A twenty-step browse is two or three decisions and seventeen mechanical
    moves. Sending all twenty to a 50,000-token-a-minute deployment is what
    made the product look broken; sending the seventeen to a deployment with
    forty times the headroom is what fixes it.
    """
    from app.services.orchestrator import (
        AGENT_LOOP_TASK,
        AGENT_REASONING_STEPS,
        AGENT_STEP_TASK,
    )

    agent = Orchestrator()

    def task_for(step_no, failures=0, unchanged=0, nudged=False):
        return agent._step_task(
            step_no=step_no,
            consecutive_failures=failures,
            unchanged_screens=unchanged,
            nudged=nudged,
        )

    # The opening of a run is where the approach is chosen.
    for step in range(1, AGENT_REASONING_STEPS + 1):
        assert task_for(step) == AGENT_LOOP_TASK, step
    # After that it is executing a plan.
    assert task_for(AGENT_REASONING_STEPS + 1) == AGENT_STEP_TASK
    assert task_for(40) == AGENT_STEP_TASK

    # And it escalates straight back the moment execution stops working.
    assert task_for(40, failures=1) == AGENT_LOOP_TASK
    assert task_for(40, unchanged=3) == AGENT_LOOP_TASK
    assert task_for(40, nudged=True) == AGENT_LOOP_TASK


def test_the_step_tier_has_headroom_the_reason_tier_does_not():
    """Documents the measurement the split was made from.

    Measured 2026-09-03 with `az cognitiveservices usage list -l swedencentral`:

        grok-4-1-fast-reasoning                  50.0 / 50.0     (maxed)
        gpt-5.4-mini - GlobalStandard            300  / 2000

    If `agent_step` is ever pointed at the reason tier again, this fails and
    the reason why is right here.
    """
    assert route_task("agent_step") == "mini"
    assert route_task("deep_plan") == "reason"
