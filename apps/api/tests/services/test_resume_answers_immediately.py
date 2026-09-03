""""I'm done, carry on" has to come back at once.

Reported, after a bot parked itself to have a login done on its desktop:

    "when i am asked to do something, the button that i am done it just keeps
    loading and it does nothing else so the task remains like that hanging"

`POST /runs/{run_id}/resume` claimed the run and then drove the whole remaining
agent loop inside the HTTP request — its own comment said "it may take
minutes". Two failures came out of that, and the second is the one that leaves
work stuck:

* the button span for the length of the loop, because the client's `await` does
  not resolve until the response does. Minutes of spinner is indistinguishable
  from broken;
* if the connection went first — an ingress idle timeout, a reload, a closed
  lid — Starlette cancels the handler, which cancels the loop mid-step. The run
  keeps the `running` it was claimed with and nothing is driving it. Hanging,
  until the reaper notices three quarters of an hour later.

So the claim stays in the request and the loop does not: `services.background`
runs it on its own task with its own session, and the press answers as soon as
the run is claimed. `resumed: true` means *started*.

The same shape applies to `POST /approvals/{id}/decide`, which had the identical
"it can take minutes" comment — see `test_approval_continues_the_task.py`.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models import Message, Run
from app.services import background
from app.services.orchestrator import RUN_AWAITING_HUMAN, TOOL_TASK_COMPLETE
from tests.services.conftest import ScriptedToolRouter, acts, call
from tests.services.test_agent_loop import take_over


@pytest.fixture
def stub_api_router(monkeypatch):
    """Script the process-wide orchestrator the API routes use.

    The same fixture `test_approval_continues_the_task.py` has, and needed for
    the same reason: the detached continuation runs through `deps.orchestrator`
    rather than through an instance a test made.
    """
    from app.routers import deps

    def _install(script, **kwargs) -> ScriptedToolRouter:
        router = ScriptedToolRouter(script, **kwargs)
        monkeypatch.setattr(deps.orchestrator, "router", router)
        return router

    return _install


@pytest_asyncio.fixture
async def parked(agent_with, db, user_a, make_thread, agent_bot, varying_screens):
    """A run stopped on a login, exactly as the product parks one."""
    _orchestrator, thread, run, _frames, _done = await take_over(
        agent_with, db, user_a, make_thread, agent_bot
    )
    return thread, run


async def test_the_press_answers_before_the_agent_has_done_anything(
    parked, authed, db, stub_api_router
):
    """The heart of it: a response in milliseconds, not in minutes."""
    thread, run = parked
    router = stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Signed in, carried on."))])

    response = await authed.post(f"/api/runs/{run.id}/resume", json={"note": "signed in"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed"] is True
    assert body["status"] == "running"
    # Nothing was asked of the model *before the response came back*. This is
    # the assertion that fails if the loop is ever moved back inside the
    # request, and it is the whole bug.
    assert router.seen == [], "the response waited for the agent loop"

    # And then the work actually happens.
    await background.drain()
    assert router.seen, "the detached continuation never ran"

    # What the run *became* is asserted in the next test, off the transcript.
    # Re-reading the run through this session here is not worth it: the harness
    # binds the test session and the background one to a single connection, so
    # a read straight after the task has been using it trips
    # `MissingGreenlet` — an artefact of the harness rather than anything the
    # product does.


async def test_what_the_bot_did_lands_in_the_thread(parked, authed, db, stub_api_router):
    """The response carries no summary any more, so the transcript has to.

    That is not a regression in what the person sees: they are looking at the
    thread, which is where a chat turn's reply appears too.
    """
    thread, run = parked
    stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Found the eleven profiles."))])

    await authed.post(f"/api/runs/{run.id}/resume", json={})
    await background.drain()

    replies = (
        await db.execute(
            select(Message).where(Message.thread_id == thread.id, Message.role == "assistant")
        )
    ).scalars().all()
    assert any("eleven profiles" in m.content for m in replies)


async def test_the_response_says_what_it_started(parked, authed, stub_api_router):
    """A button that returns instantly has to say why it looks finished."""
    _thread, run = parked
    stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Done."))])

    body = (await authed.post(f"/api/runs/{run.id}/resume", json={})).json()
    await background.drain()

    assert "Picking the task back up" in (body["detail"] or "")
    assert body["thread_id"] and body["bot_id"], "the caller cannot find the thread it went to"


async def test_a_second_press_is_told_it_already_started(parked, authed, stub_api_router):
    """The conditional claim is still the guard, and still not an error.

    Now that the first press returns immediately the second one is *more*
    likely, not less: the person sees a button that came back and no visible
    change yet.
    """
    _thread, run = parked
    stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="Done."))])

    first = (await authed.post(f"/api/runs/{run.id}/resume", json={})).json()
    second = (await authed.post(f"/api/runs/{run.id}/resume", json={})).json()
    await background.drain()

    assert first["resumed"] is True
    assert second["resumed"] is False
    assert second["status"] == "running"


async def test_a_run_with_no_agent_state_is_still_a_409(authed, db, user_a, make_bot, make_thread):
    """Unchanged, and worth keeping next to the rest: the fast path must not
    have turned an impossible resume into a cheerful `resumed: true`."""
    bot = await make_bot(user_a, name="Plain", slug="plain_resume")
    thread = await make_thread(user_a, [bot])
    run = Run(thread_id=thread.id, bot_id=bot.id, status=RUN_AWAITING_HUMAN, detail={})
    db.add(run)
    await db.commit()

    response = await authed.post(f"/api/runs/{run.id}/resume", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "run_not_resumable"


async def test_a_failed_continuation_does_not_reach_the_response(
    parked, authed, db, stub_api_router, monkeypatch
):
    """The press succeeded; the loop failing afterwards is a different event.

    Before, an exception in the loop became a 500 on the button — which reads
    as "the press did not register" when in fact the run had been claimed and
    the state had moved. Now there is no response left to fail: it is logged,
    and the run is left for the reaper, which says so in the thread.
    """
    _thread, run = parked
    stub_api_router([acts("", call(TOOL_TASK_COMPLETE, summary="never reached"))])

    from app.routers import deps

    async def explode(*_args, **_kwargs):
        raise RuntimeError("the desktop went away")

    monkeypatch.setattr(deps.orchestrator, "resume_run", explode)

    response = await authed.post(f"/api/runs/{run.id}/resume", json={})
    await background.drain()

    assert response.status_code == 200
    assert response.json()["resumed"] is True
    assert background.pending() == 0, "a failed task was left dangling"
