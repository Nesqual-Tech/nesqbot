"""Retrieval must not hold a transaction across the embeddings HTTP call.

The 2026-09-02 incident (`app.db.release_transaction`) was a model call made
with a transaction open against a Postgres running
`idle_in_transaction_session_timeout = 60000`. That docstring counts two such
awaits in the codebase, "a model call and a Bot Desktop step". There is a
third: `rag.embed` awaits `client.embeddings.create(...)` with
`timeout=request_timeout_seconds` (60.0), and the OpenAI clients are built with
no `max_retries` override, so the SDK's default of two retries makes the worst
case three 60-second attempts inside one `await`.

Every caller reaches it with a transaction already open, because
`get_current_user` shares the request's session and does its own SELECTs before
the handler runs — `GET /kb`, `POST /kb` (which commits, then `db.refresh`es the
article, which re-opens the transaction, and only then embeds), `POST
/bots/{id}/memories`, and four orchestrator sites mid-turn.

It is dormant only because embeddings are mocked in this deployment (STATUS.md:
"No Azure ⇒ `embed()` returns `None`"), which returns before any HTTP call
happens. It arms itself on the first deployment that configures the embed tier
— the same deployment that has the sixty-second timer. So these tests supply the
client that a configured deployment would have, and assert at the moment of the
call, which is the state Postgres was looking at when it killed the connection.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import CostLedger, KbArticle
from app.services import rag

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: `models.EMBEDDING_DIM`. The vector has to be the real width or the UPDATE in
#: `_write_embedding` fails on the column's own type and the write half of these
#: tests would pass for the wrong reason.
DIM = 1536


class RecordingEmbeddings:
    """Stands in for `client.embeddings`, recording the session state on entry."""

    def __init__(self, db) -> None:
        self._db = db
        #: `db.in_transaction()` as observed at each call, in order.
        self.in_transaction_at_call: list[bool] = []
        self.inputs: list[list[str]] = []

    async def create(self, *, model, input, timeout=None):  # noqa: A002 - the SDK's own name
        self.in_transaction_at_call.append(self._db.in_transaction())
        self.inputs.append(list(input))
        vector = [0.001] * DIM
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=list(vector)) for _ in input],
            usage=SimpleNamespace(prompt_tokens=11),
        )


@pytest.fixture
def embeddings(db, monkeypatch) -> RecordingEmbeddings:
    """Configure the `embed` tier with a client that watches the transaction."""
    recorder = RecordingEmbeddings(db)
    client = SimpleNamespace(embeddings=recorder)
    monkeypatch.setattr(
        rag,
        "_router",
        SimpleNamespace(
            client=lambda tier=None: client,
            model_name=lambda tier=None: "text-embedding-3-large",
        ),
    )
    return recorder


async def _open_a_transaction(db) -> None:
    """Leave the session in the state every real caller arrives in."""
    await db.execute(select(func.count()).select_from(CostLedger))
    assert db.in_transaction(), "the fixture is not exercising an open transaction"


def _assert_released(recorder: RecordingEmbeddings) -> None:
    assert recorder.in_transaction_at_call, "the embeddings client was never called"
    assert not any(recorder.in_transaction_at_call), (
        "the embeddings call was made with a transaction open; three 60-second "
        "attempts would have the connection terminated under it"
    )


# ---------------------------------------------------------------------------
# The four entry points, each against a real session
# ---------------------------------------------------------------------------


async def test_search_kb_closes_the_transaction_before_embedding_the_query(db, embeddings):
    await _open_a_transaction(db)

    await rag.search_kb(db, "flange calibration")

    _assert_released(embeddings)
    # `_contained` opens a SAVEPOINT, which only works on a live transaction —
    # so this also proves the session re-autobegins cleanly after the release.
    await db.execute(select(func.count()).select_from(KbArticle))


async def test_search_memories_closes_the_transaction_before_embedding_the_query(
    db, embeddings, bot_a, user_a, make_memory
):
    await make_memory(bot_a, user_a, content="the flange is calibrated on Tuesdays")
    await _open_a_transaction(db)

    await rag.search_memories(db, bot_a.id, user_a.id, "flange")

    _assert_released(embeddings)


async def test_upsert_memory_embedding_closes_the_transaction_before_embedding(
    db, embeddings, bot_a, user_a, make_memory
):
    memory = await make_memory(bot_a, user_a, content="remember the flange")
    await _open_a_transaction(db)

    await rag.upsert_memory_embedding(db, memory)

    _assert_released(embeddings)
    stored = await db.execute(
        select(func.count()).select_from(CostLedger).where(CostLedger.bot_id == bot_a.id)
    )
    assert int(stored.scalar_one()) == 1, "the embed cost row is still written after the release"


async def test_upsert_kb_embedding_closes_the_transaction_before_embedding(db, embeddings):
    article = KbArticle(title="Flanges", body="Calibrate on Tuesdays.")
    db.add(article)
    await db.commit()
    await db.refresh(article)  # exactly what POST /kb does — and it re-opens the transaction
    assert db.in_transaction()

    await rag.upsert_kb_embedding(db, article)

    _assert_released(embeddings)


async def test_a_search_with_no_session_still_works(db, embeddings):
    """`embed` is called with `db=None` from nowhere in the tree today, but the
    signature allows it and the release must not become a required argument."""
    vectors = await rag.embed(["anything"])
    assert vectors is not None and len(vectors[0]) == DIM


# ---------------------------------------------------------------------------
# The other two owned slow awaits, and the guard
# ---------------------------------------------------------------------------


async def test_call_mcp_tool_closes_the_transaction_before_the_http_post(
    db, bot_a, make_mcp, monkeypatch
):
    """`mcp_registry` awaits an httpx POST with `timeout=30.0` holding the
    transaction the auth dependency's own reads opened.

    Thirty seconds is the whole budget — connect and read — against a server
    this deployment does not control, so it is half the window that kills a
    backend on one call. Unlike the incident nothing raises afterwards, because
    the handler never touches `db` again, which makes the damage silent rather
    than absent: a terminated Postgres backend per call and a dead connection
    handed back to the pool for `pool_pre_ping` to find on someone else's
    request.
    """
    from app.models import BotMcp
    from app.services import mcp_registry

    server = await make_mcp(
        transport="http",
        endpoint="http://mcp.invalid",
        tool_allowlist=["send_invoice"],
    )
    db.add(BotMcp(bot_id=bot_a.id, mcp_id=server.id))
    await db.commit()

    seen: list[bool] = []

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            seen.append(db.in_transaction())
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, json=None):
            return SimpleNamespace(status_code=200, json=lambda: {"ok": 1}, text="")

    monkeypatch.setattr(mcp_registry.httpx, "AsyncClient", FakeClient)
    await _open_a_transaction(db)

    result = await mcp_registry.call_mcp_tool(
        db, bot_id=bot_a.id, mcp_id=server.id, tool="send_invoice", arguments={}
    )

    assert result["ok"] is True
    assert seen, "the HTTP client was never entered"
    assert not any(seen), "the MCP call was made with a transaction open"


#: Source patterns that introduce an await which can outlast the sixty-second
#: `idle_in_transaction_session_timeout`. `to_thread` covers the Azure and
#: kubernetes SDKs, which are synchronous clients run on a worker thread.
_SLOW_AWAIT_PATTERNS = (
    r"httpx\.AsyncClient\(",
    r"asyncio\.to_thread\(",
    r"model_router\.chat\(",
    r"router\.chat\(",
    r"embeddings\.create\(",
)

#: Every module that matches one of those patterns *and* handles an
#: `AsyncSession`, with the outcome of looking at it. Adding an entry is the
#: deliberate act this test exists to force; the value is the reason, and a
#: reason of `None` means "no release needed, and here is why".
_REVIEWED: dict[str, str | None] = {
    "services/rag.py": "the embeddings call — see the tests above",
    "services/desktop.py": "resume/suspend across a 180s container cold start",
    "services/mcp_registry.py": "the MCP tool POST — see the test above",
    "services/orchestrator.py": "the model lane — see test_idle_in_transaction.py",
    "routers/usage.py": "one model call per eval case, serially",
    "auth.py": (
        None
        # `_fetch_jwks` runs while validating the bearer token, which is before
        # `get_current_user` issues its first SELECT — there is no transaction
        # open yet to release.
    ),
    "routers/desktop.py": (
        None
        # The stream proxy's `httpx` call is made from a handler returning a
        # `StreamingResponse`, and FastAPI closes the `yield` dependency — and
        # with it the session — before the body runs. The exposure is bounded by
        # `sidecar_timeout_seconds = 30.0` on the headers round trip, inside the
        # timer rather than past it. Worth revisiting if that budget grows.
    ),
    "routers/integrations.py": (
        None
        # A 15-second manifest fetch, held across the auth dependency's reads.
        # Genuinely in scope and genuinely unfixed here: this file belongs to
        # another lane in this change. Recorded rather than quietly excluded.
    ),
    "services/notifications.py": (
        None
        # The Expo push, held across the approval read. Same shape as
        # `mcp_registry`, and unfixed for a different reason: this one is called
        # from inside a turn, so committing the caller's in-flight work to close
        # the transaction is a semantic change that needs its own test rather
        # than a one-line sweep.
    ),
}


def _files_with_a_slow_await_and_a_session() -> set[str]:
    found: set[str] = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "AsyncSession" not in source:
            continue
        if any(re.search(pattern, source) for pattern in _SLOW_AWAIT_PATTERNS):
            found.add(path.relative_to(APP_ROOT).as_posix())
    return found


def test_no_unreviewed_module_makes_a_slow_call_while_holding_a_session():
    """The durable half, and an honest one: a grep is a reminder, not a proof.

    `test_idle_in_transaction.py` can prove the model lane because every model
    call goes through one door. The HTTP lane has no such door — an `httpx`
    client or an `asyncio.to_thread` around a vendor SDK can be written anywhere
    — so this cannot show the release sits on the right side of the await. The
    behavioural tests above do that, one call site at a time.

    What it *can* do is make a new one impossible to add silently. A failure
    here is not a wall: it is a prompt to look at the new module and write down
    which of the two answers applies.
    """
    found = _files_with_a_slow_await_and_a_session()
    unreviewed = found - set(_REVIEWED)
    assert not unreviewed, (
        "these modules hold a session and make a call that can outlast "
        "idle_in_transaction_session_timeout, and nobody has decided about "
        f"them: {sorted(unreviewed)}. See app/db.py::release_transaction, then "
        "add them to _REVIEWED with the reason."
    )

    for module, reason in _REVIEWED.items():
        if reason is None:
            continue
        source = (APP_ROOT / module).read_text(encoding="utf-8")
        assert "release_transaction" in source, (
            f"{module} was reviewed as needing the release ({reason}) and no "
            "longer calls it"
        )
