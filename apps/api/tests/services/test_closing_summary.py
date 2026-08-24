"""A run that stops without finishing still owes the person an answer.

From a real session, the whole opening of a reply after thirty-six steps:

    I ran 36 steps on my desktop this turn: 34 completed, 2 failed.
    I did not reach a summary of my own, so the log below is the whole account.

Truthful, and much better than inventing a result — but a person who asked for
something wants to know whether they got it, not a census of tool calls. The
model never called `task_complete`, because the run ended by running out of road
rather than by finishing, so there was no summary to print.

So one more call: the cheapest tier there is, no tools attached, the transcript
already in the request, asking only *what did you actually achieve*. It is a
summary of work that has already happened and can cause no effect — the loop is
over — and the machine-verified step log still sits underneath it.
"""

from __future__ import annotations

import pytest

from app.services.orchestrator import (
    AGENT_LOOP_TASK,
    SUMMARY_TASK,
    TOOL_TASK_COMPLETE,
)
from tests.services.conftest import acts, call, says, turn


@pytest.fixture
def capped(monkeypatch):
    from app.services import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "DESKTOP_MAX_STEPS", 2)


async def test_a_capped_run_asks_for_the_summary_it_never_gave(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, capped
):
    orchestrator = agent_with(
        [],
        tail=acts("", call("click", x=1, y=1)),
    )
    # The closing call has no tools, so the scripted tail's call list is ignored
    # and its content is what comes back as the summary.
    orchestrator.router.tail = acts("I opened the expenses page and filed two receipts.")
    orchestrator.router.script = [
        ("", [call("click", x=1, y=1)]),
        ("", [call("click", x=2, y=2)]),
    ]
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread)

    assert done["message"].startswith("I opened the expenses page and filed two receipts.")
    assert "did not reach a summary of my own" not in done["message"]
    # The reason it stopped still lands, underneath the achievement.
    assert "limit of 2 steps in one turn" in done["message"]
    # …and so does the verified log, now written as English rather than as the
    # tool calls that produced it.
    assert "Clicked at (1, 1)" in done["message"]


async def test_the_summary_is_asked_for_on_the_cheap_tier_with_no_tools(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, capped
):
    """It is a paragraph about finished work, not a decision.

    Paying reason-tier prices to write three sentences about what already
    happened would be the same mistake as the vision loop, in the other
    direction.
    """
    orchestrator = agent_with([], tail=acts("Filed the receipts."))
    orchestrator.router.script = [("", [call("click", x=1, y=1)])] * 2
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    assert orchestrator.router.tasks[-1] == SUMMARY_TASK
    assert AGENT_LOOP_TASK in orchestrator.router.tasks
    assert orchestrator.router.tools_seen[-1] is None
    assert orchestrator.router.efforts[-1] is None


async def test_a_finished_run_is_not_asked_twice(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """`task_complete` already carried a summary. Do not pay for a second one."""
    orchestrator = agent_with(
        [
            acts("", call("click", x=1, y=1)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Filed the expenses.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread)

    assert SUMMARY_TASK not in orchestrator.router.tasks
    assert done["message"].startswith("Filed the expenses.")


async def test_a_run_that_already_produced_prose_is_not_asked(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, capped
):
    orchestrator = agent_with(
        [
            acts("Working through the expenses page now.", call("click", x=1, y=1)),
            acts("Still going.", call("click", x=2, y=2)),
        ],
        tail=acts("Still going.", call("click", x=3, y=3)),
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    assert SUMMARY_TASK not in orchestrator.router.tasks


async def test_a_run_waiting_on_a_person_is_not_summarised_over(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The note says what the person has to do. Burying it would be worse."""
    orchestrator = agent_with(
        [
            acts("", call("click", x=1, y=1)),
            acts(
                "",
                call(
                    "request_human_takeover",
                    reason="It wants a password",
                    what_you_need="Sign in, then press Continue.",
                ),
            ),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread)

    assert SUMMARY_TASK not in orchestrator.router.tasks
    assert "I need you at the screen" in done["message"]


async def test_a_summary_call_that_says_nothing_leaves_the_honest_fallback(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, capped
):
    orchestrator = agent_with([], tail=says(""))
    orchestrator.router.script = [("", [call("click", x=1, y=1)])] * 2
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread)

    # The fallback the reply falls back to is no longer a census of tool calls
    # and an admission about this module.s control flow. It is the last thing
    # that actually worked, which is the most specific true statement available
    # when no model wrote one.
    assert done["message"].startswith(
        "I did not get to a result I can report — the last thing that worked was"
    )


async def test_a_failing_summary_call_does_not_fail_the_turn(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens, capped, monkeypatch
):
    """The reply is already written. One optional extra must not take it down."""
    orchestrator = agent_with([], tail=acts("", call("click", x=1, y=1)))
    original = orchestrator.router.chat

    async def _chat(**kwargs):
        if kwargs.get("task") == SUMMARY_TASK:
            raise RuntimeError("the model endpoint fell over")
        return await original(**kwargs)

    monkeypatch.setattr(orchestrator.router, "chat", _chat)
    thread = await make_thread(user_a, [agent_bot])

    _frames, done = await turn(orchestrator, db, user_a, thread)

    assert done["message"].startswith("I did not get to a result I can report")
    assert "limit of 2 steps in one turn" in done["message"]
