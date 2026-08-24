"""ARM's "not yet" is not a failure.

A person pressing Start shortly after Stop lands in the window where ARM refuses
the PUT with `ContainerGroupTransitioning`. That reached the product owner as a
raw Azure sentence with the desktop parked in `error`:

    (ContainerGroupTransitioning) The container group
    'nesq-desktop-lead-generator-26e5b4b9-a4f' is still transitioning,
    please retry later.

The group was healthy the whole time — `provisioningState: Succeeded`, state
`Running`. The failure was ours: we invented a terminal error out of a condition
that resolves itself in seconds.
"""

from __future__ import annotations

import pytest

from app.services.desktop import (
    ACI_RETRYABLE_CODES,
    ACI_TRANSITION_RETRY_SECONDS,
    _aci_retrying,
    aci_error_code,
)


class _ArmError(Exception):
    """Shaped like `azure.core.exceptions.HttpResponseError`."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or f"({code}) something about {code}")
        self.error = type("_Err", (), {"code": code})()


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def test_a_transitioning_group_is_waited_out_not_reported_as_an_error():
    """The reported bug, as a test."""
    clock = _Clock()
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _ArmError("ContainerGroupTransitioning", "still transitioning, please retry later")
        return "created"

    result = _aci_retrying(call, what="create x", sleep=clock.sleep, now=clock.now)

    assert result == "created"
    assert attempts["n"] == 3
    assert clock.slept, "it did not actually wait"


def test_the_backoff_grows_rather_than_hammering_arm():
    clock = _Clock()
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] < 5:
            raise _ArmError("ContainerGroupTransitioning")
        return "ok"

    _aci_retrying(call, what="create x", sleep=clock.sleep, now=clock.now)

    assert clock.slept == sorted(clock.slept), "backoff must not shrink"
    assert clock.slept[0] < clock.slept[-1], "backoff never grew"
    assert max(clock.slept) <= 12.0, "a single wait longer than the cap"


def test_a_real_error_is_raised_at_once_and_not_retried():
    """Retrying a genuine failure only delays the diagnosis."""
    clock = _Clock()
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise _ArmError("InvalidResourceReference", "that subnet does not exist")

    with pytest.raises(_ArmError):
        _aci_retrying(call, what="create x", sleep=clock.sleep, now=clock.now)

    assert attempts["n"] == 1, "a non-retryable error was retried"
    assert clock.slept == [], "it slept on an error it could not fix"


def test_it_gives_up_inside_its_budget_and_raises_the_real_exception():
    """A group stuck transitioning forever must still end, and say what ARM said."""
    clock = _Clock()
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise _ArmError("ContainerGroupTransitioning")

    with pytest.raises(_ArmError):
        _aci_retrying(call, what="create x", sleep=clock.sleep, now=clock.now)

    assert clock.t <= ACI_TRANSITION_RETRY_SECONDS + 12.0
    assert attempts["n"] > 1, "it gave up without retrying at all"


def test_the_code_is_read_from_the_message_when_the_sdk_offers_no_structure():
    """Older SDK paths raise a bare exception whose text carries the code."""
    plain = Exception(
        "(ContainerGroupTransitioning) The container group 'nesq-desktop-x' is "
        "still transitioning, please retry later."
    )
    assert aci_error_code(plain) == "ContainerGroupTransitioning"
    assert aci_error_code(Exception("connection reset by peer")) == ""


def test_a_message_only_transitioning_error_still_retries():
    """The fallback has to actually drive the retry, not just parse."""
    clock = _Clock()
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise Exception(  # noqa: TRY002 - deliberately structureless
                "(ContainerGroupTransitioning) still transitioning, please retry later."
            )
        return "ok"

    assert _aci_retrying(call, what="create x", sleep=clock.sleep, now=clock.now) == "ok"
    assert attempts["n"] == 2


@pytest.mark.parametrize("code", sorted(ACI_RETRYABLE_CODES))
def test_every_declared_retryable_code_is_actually_retried(code):
    """The set is the contract; a code in it that is not retried is a lie."""
    clock = _Clock()
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _ArmError(code)
        return "ok"

    assert _aci_retrying(call, what="create x", sleep=clock.sleep, now=clock.now) == "ok"
    assert attempts["n"] == 2
