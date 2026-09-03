"""A turn must survive a model call that takes longer than the database allows.

Production incident, 2026-09-02 09:31 UTC. A chief-of-staff turn logged two
work items and then died:

    find work items      2.8s
    create work item    57.0s
    create work item      46ms
    sqlalchemy.exc.InterfaceError: (asyncpg.InterfaceError) connection is closed
    [SQL: INSERT INTO cost_ledger (id, bot_id, tier, input_tokens, output_tokens,
        cost_usd) VALUES …]

`nesqbot-pg` is configured with `idle_in_transaction_session_timeout =
60000`, confirmed with `az postgres flexible-server parameter show`. SQLAlchemy
opens a transaction on the first statement and holds it until commit, so the
turn's opening reads left one open, the model then spent 57 seconds thinking at
the `reason` tier, Postgres terminated a backend that had been idle in a
transaction for over a minute, and the next statement found a closed socket.

Two independent faults, and each gets its own half of this file:

* **The transaction was open at all.** Nothing was being done with it across
  the model call. An idle connection is fine; an idle *transaction* is on a
  sixty-second timer, so every turn whose gap between two statements exceeded a
  minute was going to die, and a reason-tier call routinely takes 30-90 seconds.
* **A bookkeeping insert could kill a turn.** The work items were already
  written. The only thing left was the cost row, and losing it took down the
  whole reply.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models import CostLedger
from app.services.model_router import ChatResult, ModelRouter
from app.services.orchestrator import Orchestrator
from tests.services.conftest import acts, call, turn

# ---------------------------------------------------------------------------
# 1. The transaction is closed before the process waits on a model
# ---------------------------------------------------------------------------


async def test_an_open_transaction_is_committed_before_a_model_call(db, user_a):
    """`_release_db` is the whole fix, so it is asserted directly."""
    orchestrator = Orchestrator()
    await db.execute(select(func.count()).select_from(CostLedger))
    assert db.in_transaction(), "the fixture is not exercising an open transaction"

    await orchestrator._release_db(db)

    assert not db.in_transaction(), (
        "a transaction left open across a model call is what "
        "idle_in_transaction_session_timeout kills"
    )
    assert user_a is not None  # the session is still usable afterwards
    await db.execute(select(func.count()).select_from(CostLedger))


async def test_releasing_a_session_with_nothing_open_is_a_no_op(db):
    orchestrator = Orchestrator()
    await db.rollback()
    assert not db.in_transaction()
    await orchestrator._release_db(orchestrator and db)
    assert not db.in_transaction()


async def test_a_failing_commit_does_not_stop_the_turn_before_it_starts(db, monkeypatch):
    """This runs *before* the request. A turn that has not asked yet must proceed.

    The next statement will raise on its own if the connection really is gone,
    with the session in a state SQLAlchemy can recover by discarding it — which
    is a better failure than never making the call.
    """
    orchestrator = Orchestrator()
    await db.execute(select(func.count()).select_from(CostLedger))

    async def boom():
        raise OperationalError("commit", None, Exception("connection is closed"))

    monkeypatch.setattr(db, "commit", boom)
    rolled: list[bool] = []

    async def note_rollback():
        rolled.append(True)

    monkeypatch.setattr(db, "rollback", note_rollback)

    await orchestrator._release_db(db)  # must not raise

    assert rolled == [True], "a failed pre-model commit has to leave the session recoverable"


def test_every_model_call_in_the_orchestrator_goes_through_the_chokepoint():
    """The guard on the fix: one door, so the invariant cannot be forgotten.

    Ten call sites had to be changed to fix this once. A source check is what
    stops the eleventh being added straight onto `self.router`, which is how
    this incident comes back in a path nobody was thinking about.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "app" / "services" / "orchestrator.py"
    ).read_text(encoding="utf-8")

    # The only two permitted direct uses are inside the chokepoint itself.
    assert source.count("self.router.chat(") == 1
    assert source.count("self.router.stream_chat(") == 1
    chokepoint = source.split("async def _ask_model", 1)[1][:800]
    assert "_release_db" in chokepoint
    assert source.count("await self._release_db(db)") >= 2

    # And it is used: eight non-streaming calls plus one stream, at time of
    # writing. Asserted as a floor rather than an exact count so adding a model
    # call does not fail this test — only routing one around the door does.
    assert source.count("await self._ask_model(") >= 8
    assert source.count("self._stream_model(") >= 1


async def test_a_real_turn_leaves_no_transaction_open_across_the_model_call(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """End to end, through the code path the incident happened on.

    The scripted router records `db.in_transaction()` at the moment it is
    called, which is exactly the state Postgres was looking at when it killed
    the connection.
    """
    seen: list[bool] = []
    orchestrator = agent_with(
        [
            acts("", call("create_work_item", type="lead", title="Star Dental")),
            acts("", call("task_complete", summary="Logged.")),
        ]
    )
    original = orchestrator.router.chat

    async def watching(**kwargs):
        seen.append(db.in_transaction())
        return await original(**kwargs)

    monkeypatch.setattr(orchestrator.router, "chat", watching)
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread, "log star dental")

    assert seen, "no model call was made"
    assert not any(seen), (
        "a model call was made with a transaction open; a 57-second one would "
        "have the connection terminated under it"
    )


# ---------------------------------------------------------------------------
# 2. Losing the cost row does not lose the turn
# ---------------------------------------------------------------------------


def _result() -> ChatResult:
    return ChatResult("hi", "reason", 5441, 1730, Decimal("0.0019532"))


async def _ledger_rows(db, bot_id) -> int:
    found = await db.execute(
        select(func.count()).select_from(CostLedger).where(CostLedger.bot_id == bot_id)
    )
    return int(found.scalar_one())


async def test_a_dead_connection_is_retried_exactly_once(db, agent_bot, monkeypatch):
    """Once, after a rollback — which is what makes the retry worth making.

    A rollback invalidates the terminated connection, so the pool hands the
    second attempt a fresh, pre-pinged one. In production that attempt lands;
    here the commit is broken for both, so what is asserted is that the retry
    happens and that it happens once rather than in a loop.
    """
    router = ModelRouter()
    attempts: list[int] = []

    async def broken():
        attempts.append(1)
        raise OperationalError("INSERT INTO cost_ledger", None, Exception("connection is closed"))

    monkeypatch.setattr(db, "commit", broken)

    await router.record_cost(db, agent_bot.id, _result())

    monkeypatch.undo()
    assert len(attempts) == 2, "the ledger write was not retried, or was retried more than once"


# Deliberately not tested here: "the session is still usable afterwards". It is
# the property that keeps the rest of a turn alive, but monkeypatching
# `Session.commit` cannot model it — a commit that raises before SQLAlchemy's
# own bookkeeping runs leaves the session in a state the real driver path never
# produces, so the test would be asserting the patch rather than the code. The
# production behaviour it stands on is SQLAlchemy's: a driver-level disconnect
# invalidates the connection, and `pool_pre_ping` validates its replacement.


async def test_a_ledger_that_cannot_be_written_at_all_is_reported_not_raised(
    db, agent_bot, monkeypatch, caplog
):
    """The pathological case. The turn's real work has already happened."""
    router = ModelRouter()

    async def always_broken():
        raise OperationalError("INSERT INTO cost_ledger", None, Exception("connection is closed"))

    monkeypatch.setattr(db, "commit", always_broken)

    with caplog.at_level("ERROR"):
        await router.record_cost(db, agent_bot.id, _result())  # must not raise

    monkeypatch.undo()
    assert any("cost not recorded" in record.message for record in caplog.records)


@pytest.mark.parametrize("tier", ["reason", "mini", "nano"])
async def test_the_ordinary_path_is_unchanged(db, agent_bot, tier):
    router = ModelRouter()
    await router.record_cost(db, agent_bot.id, ChatResult("hi", tier, 10, 5, Decimal("0.001")))
    assert await _ledger_rows(db, agent_bot.id) == 1


# ---------------------------------------------------------------------------
# 3. The desktop lane, which is where most of the slow awaits actually are
# ---------------------------------------------------------------------------
#
# The incident happened on a model call, but `simulation.perform` has the same
# shape and worse numbers: reads (assess, the standing-approval lookup, prior
# state), then one outbound call to the bot's sidecar, then the write. A cold
# start is 30-90 seconds and a browser step waits up to `timeout_ms`, so a run
# doing real work crosses 60 seconds inside a transaction repeatedly.


async def test_a_desktop_step_does_not_hold_a_transaction_across_the_sidecar_call(
    db, agent_bot, make_user, monkeypatch
):
    """Asserted at the moment of the outbound call, which is when it matters."""
    from app.services import simulation
    from app.services.orchestrator import DESKTOP_SCREENSHOT
    from app.services.simulation import Effect

    user = await make_user(email="perform@nesqualtech.test")
    seen: list[bool] = []
    real_execute = simulation._execute

    async def watching(db_, effect, assessment):
        seen.append(db_.in_transaction())
        return await real_execute(db_, effect, assessment)

    monkeypatch.setattr(simulation, "_execute", watching)

    # A read first, so there really is a transaction to leak into the call.
    await db.execute(select(func.count()).select_from(CostLedger))
    assert db.in_transaction()

    await simulation.perform(
        db,
        Effect(
            kind="desktop",
            bot_id=agent_bot.id,
            action=DESKTOP_SCREENSHOT,
            input_data={},
            actor_user_id=user.id,
        ),
    )

    assert seen, "the effect never reached _execute"
    assert not any(seen), (
        "a desktop step was executed with a transaction open; a 90-second cold "
        "start would have the connection terminated under it"
    )


def test_both_slow_lanes_use_the_one_helper():
    """Two modules, one implementation. A second copy is a second behaviour."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app"
    orchestrator = (root / "services" / "orchestrator.py").read_text(encoding="utf-8")
    simulation = (root / "services" / "simulation.py").read_text(encoding="utf-8")
    database = (root / "db.py").read_text(encoding="utf-8")

    assert "async def release_transaction" in database
    assert "idle_in_transaction_session_timeout" in database
    for module in (orchestrator, simulation):
        assert "from app.db import release_transaction" in module
        assert "release_transaction(db)" in module
