"""Daily budget enforcement — a bot at or over its cap refuses the turn."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.models import CostLedger, Message, Run


async def test_a_bot_at_its_cap_refuses_the_turn(authed, db, make_bot, make_thread, user_a):
    bot = await make_bot(user_a, name="Skint", daily_budget_usd=0.0)
    thread = await make_thread(user_a, [bot])

    response = await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "do work"})
    assert response.status_code == 200
    body = response.json()
    assert body["budget_blocked"] is True
    assert "daily budget" in body["message"]
    assert "run_id" not in body, "a refused turn must not open a run"


async def test_a_bot_over_its_cap_refuses_the_turn(
    authed, db, make_bot, make_thread, user_a
):
    bot = await make_bot(user_a, name="Overspent", daily_budget_usd=1.0)
    db.add(
        CostLedger(
            bot_id=bot.id, tier="mini", input_tokens=10, output_tokens=10, cost_usd=Decimal("2.50")
        )
    )
    await db.commit()
    thread = await make_thread(user_a, [bot])

    response = await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "hi"})
    assert response.json()["budget_blocked"] is True


async def test_a_refused_turn_still_records_the_refusal_as_a_message(
    authed, db, make_bot, make_thread, user_a
):
    bot = await make_bot(user_a, daily_budget_usd=0.0)
    thread = await make_thread(user_a, [bot])
    await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "hi"})

    rows = await db.execute(select(Message).where(Message.thread_id == thread.id))
    messages = rows.scalars().all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert "daily budget" in messages[1].content


async def test_a_refused_turn_spends_nothing(authed, db, make_bot, make_thread, user_a):
    bot = await make_bot(user_a, daily_budget_usd=0.0)
    thread = await make_thread(user_a, [bot])
    await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "hi"})

    rows = await db.execute(select(CostLedger).where(CostLedger.bot_id == bot.id))
    assert rows.scalars().all() == []
    runs = await db.execute(select(Run).where(Run.bot_id == bot.id))
    assert runs.scalars().all() == []


async def test_a_bot_under_its_cap_runs_normally(authed, db, make_bot, make_thread, user_a):
    bot = await make_bot(user_a, daily_budget_usd=50.0)
    thread = await make_thread(user_a, [bot])
    response = await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "hi"})
    body = response.json()
    assert "budget_blocked" not in body
    assert body["run_id"]


async def test_raising_the_cap_unblocks_the_bot(authed, make_bot, make_thread, user_a):
    bot = await make_bot(user_a, daily_budget_usd=0.0)
    thread = await make_thread(user_a, [bot])
    blocked = await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "one"})
    assert blocked.json()["budget_blocked"] is True

    await authed.patch(f"/api/bots/{bot.id}/budget", json={"daily_budget_usd": 25.0})
    allowed = await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "two"})
    assert "budget_blocked" not in allowed.json()


async def test_usage_reports_spend_against_the_cap(authed, db, make_bot, make_thread, user_a):
    bot = await make_bot(user_a, name="Spender", daily_budget_usd=7.0)
    thread = await make_thread(user_a, [bot])
    await authed.post(f"/api/threads/{thread.id}/messages", json={"content": "spend a little"})

    usage = await authed.get("/api/usage")
    assert usage.status_code == 200
    row = next(u for u in usage.json() if u["bot_id"] == str(bot.id))
    assert row["budget_usd"] == 7.0
    assert row["spent_usd_today"] > 0
    assert row["entries"], "the ledger entry for the turn must be visible"
    assert row["entries"][0]["tier"] == "mini"
