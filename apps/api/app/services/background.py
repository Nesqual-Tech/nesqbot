"""Work that outlives the request that asked for it.

The bug this exists for, reported as: *"when i am asked to do something, the
button that i am done it just keeps loading and it does nothing else so the
task remains like that hanging."*

That button is `POST /runs/{run_id}/resume` — a bot parked itself
`awaiting_human` to have somebody sign in on its desktop, and this is the "I've
finished, carry on" press. The route claimed the run and then drove *the entire
remaining agent loop inside the HTTP request*. Its own comment said so: "it may
take minutes". Two consequences, and the second is the damage:

* the button span the whole time, because the client's `await` does not resolve
  until the loop finishes. Minutes of a spinner is indistinguishable from
  broken, and a person presses it again;
* when the connection went away first — the Container Apps ingress idle
  timeout, a laptop lid, a reload — Starlette cancels the handler, which
  cancels the loop mid-step. The run keeps the `running` it was claimed with
  and nothing is driving it. Hanging, exactly as described, until the reaper
  notices 45 minutes later.

So the claim stays in the request and the loop does not. The press returns as
soon as the run is *claimed*, which is the only fact the person needs — the
work then appears in the thread over SSE, the same way a chat turn does.

Deliberately not a queue. `services.work_dispatch` is a queue because an
assigned work item must survive a deploy and be picked up by whichever replica
is alive; a resume is a *continuation of a run that is already claimed*, so the
durable record is the run row itself and the recovery path already exists: the
reaper reclaims a `running` run whose process went away and says so in the
thread. Adding a second queue would mean two mechanisms that can both think
they own the same loop.

The one thing a fire-and-forget task genuinely needs is a reference. A bare
`asyncio.create_task` may be garbage collected mid-flight, so tasks are held
here until they finish.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app import db as db_module

logger = logging.getLogger("nesqbot.background")

#: Live detached tasks, held so the event loop's only reference is not a weak
#: one. Discarded on completion by the done callback.
_tasks: set[asyncio.Task] = set()


def run_detached(work: Callable[[AsyncSession], Awaitable[object]], *, label: str) -> asyncio.Task:
    """Run `work` after the response has gone, on a session of its own.

    `work` is handed a **new** `AsyncSession`, never the request's: that one is
    closed by the dependency teardown the moment the response is sent, and a
    loop still holding it would fail on its next query with "connection is
    closed" — a failure this codebase has already had once, from the other
    direction, in `db.release_transaction`.

    The factory is read off `app.db` at call time rather than imported by name,
    which is not a style preference: the test harness redirects
    `db.SessionLocal` onto its own connection so background work sees the rows
    a test created, and a by-name import would keep a reference to the real one
    and try to open a second connection to a database that is not there. The
    conftest already carries one line of that mistake for `routers.threads`;
    this way there is nothing to remember.

    Exceptions are logged and swallowed. The caller has already returned a
    response and the claim is already committed; the recovery path for a
    continuation that dies is the run reaper, not an exception nobody is
    waiting to catch.

    Returns the task, which production ignores and `drain()` uses.
    """

    async def _runner() -> None:
        try:
            async with db_module.SessionLocal() as db:
                await work(db)
        except asyncio.CancelledError:
            logger.info("background %s cancelled", label)
            raise
        except Exception:  # noqa: BLE001 - nobody is awaiting this to catch it
            logger.exception("background %s failed", label)

    task = asyncio.create_task(_runner(), name=label)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    logger.info("background %s started", label)
    return task


async def cancel_all() -> None:
    """Cancel everything detached, without waiting for it to finish.

    Test hygiene, and it is not optional. The suite binds every session to one
    asyncpg connection, so a task still running when a test ends holds
    something the *next* test needs — and the next test's `drain()` then waits
    out its whole budget for work that can never finish. That turned a
    seven-minute suite into a twenty-minute one before this existed. An autouse
    fixture calls it after every test.
    """
    live = [task for task in _tasks if not task.done()]
    for task in live:
        task.cancel()
    if live:
        await asyncio.gather(*live, return_exceptions=True)


def pending() -> int:
    """How many detached tasks are still running. For tests and diagnostics."""
    return len([task for task in _tasks if not task.done()])


#: How long `drain` waits on one round of tasks, and how many rounds it will
#: wait at all. Only a test concern; nothing in production calls `drain`.
DRAIN_ROUND_SECONDS = 20.0
DRAIN_MAX_ROUNDS = 3


async def drain() -> None:
    """Wait for everything detached so far. Give up rather than hang.

    For tests, and it is the reason `run_detached` returns its task rather than
    hiding it: a test that presses resume and then asserts what the bot did
    needs the same code path production takes, plus a place to wait. The
    alternative — running inline when some `NESQ_ENV` says so — would mean the
    thing under test is not the thing that ships.

    Loops, because a detached continuation may detach another (a resume that
    stops on a second approval). Bounded, because it can genuinely deadlock on
    the test harness: the suite binds every session to one asyncpg connection
    so a stream can see uncommitted rows, and a connection is not safe to use
    from two places at once — so a task can be stuck behind the very test that
    is waiting for it. An unbounded wait there is a suite that hangs for ever
    with no output, which is exactly how this function was first written and
    exactly how long it took to notice. Remaining tasks are cancelled, which
    also stops one test's stray work from bleeding into the next.
    """
    for _ in range(DRAIN_MAX_ROUNDS):
        live = [task for task in _tasks if not task.done()]
        if not live:
            return
        await asyncio.wait(live, timeout=DRAIN_ROUND_SECONDS)
    stuck = [task for task in _tasks if not task.done()]
    if not stuck:
        return
    logger.warning(
        "drain gave up on %d background task(s): %s",
        len(stuck),
        ", ".join(task.get_name() for task in stuck),
    )
    for task in stuck:
        task.cancel()
    await asyncio.gather(*stuck, return_exceptions=True)
