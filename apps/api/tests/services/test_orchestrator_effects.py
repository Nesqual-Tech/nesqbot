"""`app.services.orchestrator` — chat-turn tool use goes through the chokepoint.

Tool use during a chat turn is the most common path in the product, so an undo
log that covers routines but not chat covers almost nothing. These tests hold
the orchestrator to the same three properties as the routine runner: the
read-only sweep is suppressed inside a `SimulationContext`, it writes an
undo-log entry outside one, and a gated effect is held rather than run.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.models import ActionLog, Bot, Connector
from app.services import simulation
from app.services.orchestrator import Orchestrator

INBOX_QUESTION = "anything in the inbox about an invoice?"


@pytest.fixture
def outbound(monkeypatch):
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"value": []})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


@pytest.fixture
async def ops_bot(db):
    """The seeded `ops` bot — `_gather_tools` keys its inbox sweep off the slug."""
    rows = await db.execute(select(Bot).where(Bot.slug == "ops"))
    bot = rows.scalar_one_or_none()
    if bot is None:
        pytest.skip("the ops system bot is not seeded in this build")
    return bot


async def test_the_read_only_sweep_writes_an_undo_log_entry(db, ops_bot, make_user):
    user = await make_user()
    results, notes = await Orchestrator()._gather_tools(
        db, ops_bot, INBOX_QUESTION, actor_user_id=user.id
    )
    assert [r["action"] for r in results] == ["list_inbox"]
    assert results[0]["ok"] is True

    rows = await db.execute(select(ActionLog).where(ActionLog.bot_id == ops_bot.id))
    entries = list(rows.scalars().all())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.connector_id == "microsoft_graph"
    assert entry.action == "list_inbox"
    assert entry.actor_user_id == user.id
    # Honest: a read changed nothing, so there is nothing to take back.
    assert entry.reversible is False
    assert "read-only" in (entry.irreversible_reason or "")
    assert notes


async def test_the_sweep_is_suppressed_inside_a_simulation(db, ops_bot, outbound):
    """A dry run of a chat turn must not sweep the inbox for real."""
    before = await db.execute(select(ActionLog))
    baseline = len(list(before.scalars().all()))

    with simulation.SimulationContext(bot_id=ops_bot.id) as context:
        results, _ = await Orchestrator()._gather_tools(db, ops_bot, INBOX_QUESTION)

    assert outbound == []
    assert [c.action for c in context.calls] == ["list_inbox"]
    assert results[0]["simulated"] is True
    assert results[0]["result"] is None, "a rehearsed sweep must not fabricate tool output"

    after = await db.execute(select(ActionLog))
    assert len(list(after.scalars().all())) == baseline


async def test_a_gated_sweep_action_is_skipped_rather_than_run_or_parked(
    db, ops_bot, make_user, monkeypatch, outbound
):
    """Nobody asked for a speculative read, so a gate means skip, not approve.

    Running it anyway would be a bypass; filing an approval for context the user
    never requested would be noise. Skipping and saying so is the coherent
    answer, and the model still sees the note.
    """
    from app.models import Approval

    connector = await db.get(Connector, "microsoft_graph")
    actions = [dict(a) for a in connector.actions]
    for action in actions:
        if action["name"] == "list_inbox":
            action["risk"] = "send"
    connector.actions = actions
    await db.commit()

    user = await make_user()
    results, notes = await Orchestrator()._gather_tools(
        db, ops_bot, INBOX_QUESTION, actor_user_id=user.id
    )

    assert results[0]["gated"] is True
    assert results[0]["ok"] is False
    assert any("needs approval" in note for note in notes)
    assert outbound == []

    rows = await db.execute(select(Approval).where(Approval.bot_id == ops_bot.id))
    assert list(rows.scalars().all()) == []
    logged = await db.execute(select(ActionLog).where(ActionLog.bot_id == ops_bot.id))
    assert list(logged.scalars().all()) == [], "a skipped action was logged as executed"


async def test_the_orchestrator_no_longer_calls_the_connector_directly():
    """The migration itself: no bypass of the chokepoint is left in the module."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "app" / "services" / "orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "execute_connector_action" not in source
    assert "simulation.perform" in source
