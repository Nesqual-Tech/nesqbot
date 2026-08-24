"""`call_mcp_tool` must fail closed, and must never invent a success.

Both cases here were live defects, reported by the platform team against a
registry they were about to point 783 operations at — 250 of them classified
`dangerous` (deletes, money movement, messages to real people). Neither was
theoretical: the first is reachable the moment a server is attached to a bot,
because that is the state a newly registered server is in.
"""

from __future__ import annotations

import pytest

from app.services.mcp_registry import call_mcp_tool

pytestmark = pytest.mark.anyio


async def _attach(db, bot, mcp):
    from app.models import BotMcp

    db.add(BotMcp(bot_id=bot.id, mcp_id=mcp.id))
    await db.commit()


async def test_an_empty_allowlist_calls_nothing(db, user_a, make_bot, make_mcp):
    """The defect, as a test: empty allowlist used to mean *everything*.

    `if allow and tool not in allow` short-circuits when `allow` is falsy, so the
    guard was skipped entirely. `docs/connectors.md` promises the opposite — "an
    empty allowlist means nothing is callable, which is the safe default" — and a
    server is registered with `tool_allowlist = []`, so every tool it serves was
    callable from the moment it was attached to a bot.
    """
    bot = await make_bot(user_a)
    mcp = await make_mcp(user_a, tool_allowlist=[])
    await _attach(db, bot, mcp)

    result = await call_mcp_tool(
        db, bot_id=bot.id, mcp_id=mcp.id, tool="delete_everything", arguments={}
    )

    assert result["ok"] is False
    assert result["error"] == "tool not allowlisted"
    # And nothing was fabricated on the way out.
    assert "mock" not in result


async def test_a_populated_allowlist_still_permits_what_it_names(
    db, user_a, make_bot, make_mcp
):
    """The fix must not be "refuse everything" — that would pass the test above
    while breaking the feature."""
    bot = await make_bot(user_a)
    mcp = await make_mcp(user_a, tool_allowlist=["search_leads"])
    await _attach(db, bot, mcp)

    allowed = await call_mcp_tool(
        db, bot_id=bot.id, mcp_id=mcp.id, tool="search_leads", arguments={}
    )
    refused = await call_mcp_tool(
        db, bot_id=bot.id, mcp_id=mcp.id, tool="issue_refund", arguments={}
    )

    assert refused["ok"] is False
    assert refused["error"] == "tool not allowlisted"
    # `allowed` reached the transport branch; what it answers there is the
    # subject of the tests below, not of this one.
    assert allowed.get("error") != "tool not allowlisted"


async def test_an_unreachable_server_reports_failure_not_a_mock_success(
    db, user_a, make_bot, make_mcp, monkeypatch
):
    """A stdio/unreachable server used to answer `ok: True, mock: True`.

    A caller that checks `ok` — which is every caller — believed the action had
    happened. Against a server that can issue an invoice or send a WhatsApp
    message, that is a bot reporting work it did not do, and then either moving
    on or retrying, because nothing in the answer lets it tell the difference.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nesq_env", "production", raising=False)

    bot = await make_bot(user_a)
    mcp = await make_mcp(user_a, transport="stdio", endpoint=None, tool_allowlist=["pay"])
    await _attach(db, bot, mcp)

    result = await call_mcp_tool(db, bot_id=bot.id, mcp_id=mcp.id, tool="pay", arguments={})

    assert result["ok"] is False
    assert result.get("code") == "mcp_unreachable"
    assert result.get("mock") is not True
    assert "Nothing ran" in result["error"]


async def test_the_local_mock_survives_in_development(
    db, user_a, make_bot, make_mcp, monkeypatch
):
    """The mock is useful for a local stdio stub; it is only a lie in production.

    Asserted so the production guard cannot be "fixed" later by deleting the
    branch, which would take the local development path with it.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nesq_env", "development", raising=False)

    bot = await make_bot(user_a)
    mcp = await make_mcp(user_a, transport="stdio", endpoint=None, tool_allowlist=["pay"])
    await _attach(db, bot, mcp)

    result = await call_mcp_tool(db, bot_id=bot.id, mcp_id=mcp.id, tool="pay", arguments={})

    assert result["ok"] is True
    assert result["mock"] is True


async def test_the_documented_contract_and_the_code_agree(db):
    """The defect was a doc/code disagreement, so pin the sentence itself.

    If someone reverts the guard, this fails next to the behavioural test and
    says which document it broke.
    """
    from pathlib import Path

    from tests.conftest import DOCS_API_MD

    connectors = Path(DOCS_API_MD).parent / "connectors.md"
    text = connectors.read_text(encoding="utf-8")
    assert "an empty allowlist means nothing" in text, (
        "docs/connectors.md no longer promises fail-closed; if that is deliberate, "
        "the guard in mcp_registry.call_mcp_tool must change in the same commit."
    )
