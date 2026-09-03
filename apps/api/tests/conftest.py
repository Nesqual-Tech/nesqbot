"""Shared fixtures for the API suite.

Design notes
------------
* **Environment first.** Every environment variable the app reads is pinned at
  import time, before any ``app.*`` module is imported, so the suite always runs
  in the "no Azure keys, no Redis, no Temporal" configuration the product has to
  support.
* **A real Postgres.** ``TEST_DATABASE_URL`` wins; otherwise a throwaway
  ``pgvector/pgvector:pg16`` container is started with the Docker CLI; otherwise
  the whole suite skips with an explicit message.
* **Per-test rollback.** Each test runs inside one outer transaction on a single
  connection, with the ORM session joined to it via ``create_savepoint``. The
  app's own ``commit()`` calls become savepoint releases, so nothing leaks
  between tests and the suite is order-agnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[3]
API_ROOT = Path(__file__).resolve().parents[1]
DOCS_API_MD = REPO_ROOT / "docs" / "API.md"

# --------------------------------------------------------------------------
# Environment - pinned before `app` is imported anywhere.
# --------------------------------------------------------------------------

#: A port nothing listens on, so redis/temporal fail fast and the app exercises
#: its documented fallbacks instead of quietly using a dev service.
_DEAD_PORT = 63999

os.environ["NESQ_ENV"] = "development"
os.environ["REDIS_URL"] = f"redis://127.0.0.1:{_DEAD_PORT}/0"
os.environ["TEMPORAL_HOST"] = f"127.0.0.1:{_DEAD_PORT}"
os.environ["TEMPORAL_CONNECT_TIMEOUT_SECONDS"] = "0.5"
os.environ["REDIS_CONNECT_TIMEOUT_SECONDS"] = "0.5"
os.environ["AZURE_OPENAI_ENDPOINT"] = ""
os.environ["AZURE_OPENAI_API_KEY"] = ""
os.environ["AZURE_TENANT_ID"] = ""
os.environ["AZURE_CLIENT_ID"] = ""
os.environ["AZURE_CLIENT_SECRET"] = ""
os.environ["AZURE_KEY_VAULT_URL"] = ""
os.environ["EXPO_PUSH_ENABLED"] = "false"
os.environ["BOT_DESKTOP_MODE"] = "mock"
# Out of the repo: nothing should be able to write bot homes into the working tree.
os.environ["BOT_DESKTOP_HOME_ROOT"] = str(Path(tempfile.gettempdir()) / "nesqbot-pytest-bot-homes")
os.environ["BOTS_DIR"] = str(REPO_ROOT / "bots")
os.environ["JWT_SECRET"] = "test-secret"
os.environ.setdefault("CORS_ORIGINS", "http://localhost:1420")

# --------------------------------------------------------------------------
# Database discovery / bootstrap
# --------------------------------------------------------------------------

_PG_IMAGE = "pgvector/pgvector:pg16"
_CONTAINER_NAME = "nesqbot-api-pytest-pg"
_SKIP_MESSAGE = (
    "No test database. Set TEST_DATABASE_URL to a Postgres with the pgvector "
    "extension available (e.g. postgresql+asyncpg://user:pw@host:5432/db), or "
    "make the `docker` CLI usable so a throwaway pgvector/pgvector:pg16 "
    "container can be started."
)


def _as_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(  # noqa: S603
                ["docker", "info", "--format", "{{.ServerVersion}}"],  # noqa: S607
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _start_pg_container() -> str | None:
    """Boot a throwaway pgvector container and return its asyncpg URL."""
    if not _docker_available():
        return None
    subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)  # noqa: S603,S607
    started = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            _CONTAINER_NAME,
            "-e",
            "POSTGRES_USER=nesq",
            "-e",
            "POSTGRES_PASSWORD=nesq",
            "-e",
            "POSTGRES_DB=nesqbot_test",
            "-P",
            _PG_IMAGE,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if started.returncode != 0:
        return None
    port = None
    for _ in range(60):
        probe = subprocess.run(  # noqa: S603
            ["docker", "port", _CONTAINER_NAME, "5432/tcp"],  # noqa: S607
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            port = probe.stdout.strip().splitlines()[0].rsplit(":", 1)[-1]
            break
        time.sleep(0.5)
    if port is None:
        _stop_pg_container()
        return None
    for _ in range(120):
        ready = subprocess.run(  # noqa: S603
            ["docker", "exec", _CONTAINER_NAME, "pg_isready", "-U", "nesq", "-d", "nesqbot_test"],  # noqa: S607
            capture_output=True,
        )
        if ready.returncode == 0:
            break
        time.sleep(0.5)
    return f"postgresql+asyncpg://nesq:nesq@127.0.0.1:{port}/nesqbot_test"


def _stop_pg_container() -> None:
    subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)  # noqa: S603,S607


def _wait_for_postgres(url: str, timeout: float = 90.0) -> str | None:
    """Poll until a real query succeeds, or return the last error.

    `pg_isready` is not sufficient: the official Postgres image runs a temporary
    server over the unix socket while it executes the init scripts, then
    restarts. A TCP client that connects in that window gets its connection
    reset. Only a successful `SELECT 1` over TCP proves the database is up.
    """

    async def _probe() -> str | None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        deadline = time.monotonic() + timeout
        last = "timed out before the first attempt"
        while time.monotonic() < deadline:
            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                return None
            except Exception as exc:  # noqa: BLE001 - any failure means "not yet"
                last = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5)
            finally:
                await engine.dispose()
        return last

    return asyncio.run(_probe())


@pytest.fixture(scope="session")
def database_url() -> AsyncIterator[str]:
    provided = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if provided:
        url = _as_asyncpg_url(provided)
        error = _wait_for_postgres(url, timeout=30.0)
        if error is not None:
            pytest.skip(f"TEST_DATABASE_URL is not reachable ({error})")
        os.environ["DATABASE_URL"] = url
        yield url
        return

    url = _start_pg_container()
    if url is None:
        pytest.skip(_SKIP_MESSAGE)
    error = _wait_for_postgres(url)
    if error is not None:
        _stop_pg_container()
        pytest.skip(f"the test Postgres container never became reachable ({error})")
    os.environ["DATABASE_URL"] = url
    try:
        yield url
    finally:
        _stop_pg_container()


async def _bootstrap_schema(url: str) -> None:
    """Create the schema and seed system rows once, committed, for the session."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services.schema import ensure_schema
    from app.services.seed import seed_system

    engine = create_async_engine(url)
    try:
        await ensure_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await seed_system(session)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def bootstrap(database_url: str) -> str:
    """Schema + system bots + first-party connectors, committed once."""
    asyncio.run(_bootstrap_schema(database_url))
    return database_url


# --------------------------------------------------------------------------
# Per-test connection / session
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_connection(database_url: str, bootstrap: str):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(database_url, poolclass=NullPool)
    connection = await engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def db(db_connection):
    """ORM session joined to the test transaction; commits become savepoints."""
    from sqlalchemy.ext.asyncio import AsyncSession

    session = AsyncSession(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()


# --------------------------------------------------------------------------
# Application under test
# --------------------------------------------------------------------------


@pytest.fixture
def app(db, db_connection, monkeypatch):
    """The real FastAPI app, wired to the test session.

    ``get_db`` is overridden, and the module-level ``SessionLocal`` that the SSE
    producer opens for itself is redirected onto the same connection so a stream
    sees the rows the test created.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    import app.db as db_module
    import app.routers.threads as threads_module
    from app.db import get_db
    from app.main import app as fastapi_app

    def session_factory() -> AsyncSession:
        return AsyncSession(
            bind=db_connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )

    monkeypatch.setattr(db_module, "SessionLocal", session_factory)
    monkeypatch.setattr(threads_module, "SessionLocal", session_factory)

    async def _override_get_db():
        yield db

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)


#: Every (METHOD, concrete path) the suite sends, for the route-coverage guard.
REQUESTED: set[tuple[str, str]] = set()


def _client_for(app_obj, headers: dict[str, str] | None = None, *, raise_app_exceptions: bool = True):
    import httpx

    class RecordingTransport(httpx.ASGITransport):
        async def handle_async_request(self, request):  # type: ignore[override]
            REQUESTED.add((request.method.upper(), request.url.path))
            return await super().handle_async_request(request)

    transport = RecordingTransport(app=app_obj, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=headers or {},
        timeout=30.0,
    )


def pytest_collection_modifyitems(config, items):
    """Run the route-coverage guard last; it reads what every other test sent."""
    positional = [a for a in config.invocation_params.args if not str(a).startswith("-")]
    config.stash_nesq_full_run = not positional  # type: ignore[attr-defined]

    guards = [i for i in items if i.fspath.basename == "test_route_coverage.py"]
    if guards:
        rest = [i for i in items if i not in guards]
        items[:] = rest + guards


@pytest_asyncio.fixture
async def client(app):
    """Unauthenticated client - in development this lands on the dev bypass user."""
    async with _client_for(app) as c:
        yield c


@pytest_asyncio.fixture
async def tolerant_client(app):
    """Client that renders a 500 instead of re-raising, for error-envelope tests."""
    async with _client_for(app, raise_app_exceptions=False) as c:
        yield c


# --------------------------------------------------------------------------
# Factories
# --------------------------------------------------------------------------


@pytest.fixture
def make_user(db):
    from app.models import User

    async def _make(email: str | None = None, display_name: str = "Test User"):
        user = User(
            email=email or f"user-{uuid.uuid4().hex[:12]}@example.test",
            display_name=display_name,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    return _make


@pytest.fixture
def make_bot(db):
    from app.models import Bot

    async def _make(
        owner=None,
        *,
        name: str = "Custom Bot",
        role: str = "Tester",
        system_prompt: str = "You are a test bot.",
        is_system: bool = False,
        daily_budget_usd: float = 5.0,
        desktop_profile: str = "xfce",
        slug: str | None = None,
    ):
        bot = Bot(
            slug=slug or f"bot_{uuid.uuid4().hex[:10]}",
            name=name,
            role=role,
            system_prompt=system_prompt,
            is_system=is_system,
            owner_user_id=None if is_system else getattr(owner, "id", owner),
            desktop_profile=desktop_profile,
            daily_budget_usd=Decimal(str(daily_budget_usd)),
        )
        db.add(bot)
        await db.commit()
        await db.refresh(bot)
        return bot

    return _make


@pytest.fixture
def make_thread(db):
    from app.models import Thread, ThreadBot

    async def _make(owner, bots=(), *, title: str = "Test thread"):
        thread = Thread(title=title, owner_user_id=owner.id)
        db.add(thread)
        await db.commit()
        await db.refresh(thread)
        for bot in bots:
            db.add(ThreadBot(thread_id=thread.id, bot_id=getattr(bot, "id", bot)))
        if bots:
            await db.commit()
        return thread

    return _make


@pytest.fixture
def make_run(db):
    from app.models import Run

    async def _make(thread, bot, *, status: str = "running"):
        run = Run(thread_id=thread.id, bot_id=bot.id, status=status)
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    return _make


@pytest.fixture
def make_approval(db):
    from app.models import Approval
    from app.routers.deps import REQUESTED_BY_KEY

    async def _make(
        bot,
        *,
        run=None,
        risk: str = "send",
        title: str = "Approve something risky",
        summary: str = "A held action awaiting a human.",
        payload: dict[str, Any] | None = None,
        status: str = "pending",
        requested_by=None,
    ):
        body = dict(payload or {"kind": "message_only", "draft": "hello"})
        if requested_by is not None:
            body[REQUESTED_BY_KEY] = str(getattr(requested_by, "id", requested_by))
        approval = Approval(
            run_id=getattr(run, "id", run),
            bot_id=bot.id,
            risk=risk,
            title=title,
            summary=summary,
            payload=body,
            status=status,
        )
        db.add(approval)
        await db.commit()
        await db.refresh(approval)
        return approval

    return _make


@pytest.fixture
def make_routine(db):
    from app.models import Routine

    async def _make(
        bot,
        *,
        name: str = "Nightly sweep",
        description: str = "",
        steps: list[dict[str, Any]] | None = None,
        schedule_cron: str | None = None,
        enabled: bool = True,
    ):
        routine = Routine(
            bot_id=bot.id,
            name=name,
            description=description,
            steps=steps if steps is not None else [{"type": "message", "text": "hi"}],
            schedule_cron=schedule_cron,
            enabled=enabled,
        )
        db.add(routine)
        await db.commit()
        await db.refresh(routine)
        return routine

    return _make


@pytest.fixture
def make_connector_binding(db):
    from app.models import BotConnector

    async def _make(
        bot,
        connector_id: str = "microsoft_graph",
        *,
        status: str = "connected",
        secret_ref: str | None = None,
    ):
        link = BotConnector(
            bot_id=bot.id,
            connector_id=connector_id,
            status=status,
            secret_ref=secret_ref,
        )
        db.add(link)
        await db.commit()
        return link

    return _make


@pytest.fixture
def make_memory(db):
    from app.models import Memory

    async def _make(bot=None, user=None, *, kind: str = "note", content: str = "remember this"):
        memory = Memory(
            bot_id=getattr(bot, "id", bot),
            user_id=getattr(user, "id", user),
            kind=kind,
            content=content,
        )
        db.add(memory)
        await db.commit()
        await db.refresh(memory)
        return memory

    return _make


@pytest.fixture
def make_mcp(db):
    from app.models import McpServer

    async def _make(
        owner=None,
        *,
        name: str = "Test MCP",
        transport: str = "stdio",
        endpoint: str | None = None,
        command: str | None = "echo",
        tool_allowlist: list[str] | None = None,
        enabled: bool = True,
    ):
        server = McpServer(
            name=name,
            transport=transport,
            endpoint=endpoint,
            command=command,
            enabled=enabled,
            tool_allowlist=tool_allowlist or [],
            owner_user_id=getattr(owner, "id", owner),
        )
        db.add(server)
        await db.commit()
        await db.refresh(server)
        return server

    return _make


# --------------------------------------------------------------------------
# Users + authenticated clients
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def user_a(make_user):
    return await make_user(display_name="User A")


@pytest_asyncio.fixture
async def user_b(make_user):
    return await make_user(display_name="User B")


def auth_headers(user) -> dict[str, str]:
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.email)}"}


@pytest_asyncio.fixture
async def authed(app, user_a):
    """Client authenticated as user A via a real signed JWT."""
    async with _client_for(app, auth_headers(user_a)) as c:
        yield c


@pytest_asyncio.fixture
async def other(app, user_b):
    """Second-user client - the authorization probe."""
    async with _client_for(app, auth_headers(user_b)) as c:
        yield c


@pytest_asyncio.fixture
async def anon(app):
    """A client whose bearer token is garbage, so the dev bypass does not apply."""
    async with _client_for(app, {"Authorization": "Bearer not-a-real-token"}) as c:
        yield c


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def system_bot(db):
    """A seeded system bot (shared by every user)."""
    from sqlalchemy import select

    from app.models import Bot

    row = await db.execute(select(Bot).where(Bot.is_system.is_(True)).order_by(Bot.slug).limit(1))
    bot = row.scalar_one_or_none()
    if bot is None:
        pytest.fail("system bots were not seeded - check services/seed.py")
    return bot


@pytest_asyncio.fixture
async def bot_a(make_bot, user_a):
    return await make_bot(user_a, name="A's bot")


@pytest_asyncio.fixture
async def bot_b(make_bot, user_b):
    return await make_bot(user_b, name="B's bot")


@pytest_asyncio.fixture(autouse=True)
async def _reset_event_bus():
    """Keep the in-process event bus out of neighbouring tests.

    ``events._redis_lock`` is dropped along with the client. It is a
    module-global ``asyncio.Lock``, and an *uncontended* acquire never touches
    the running loop - so the lock outlives a test unbound, and the first
    *contended* acquire binds it to whichever loop was running then.
    pytest-asyncio gives every test a fresh loop, so a later contended acquire
    (two subscribers racing to open the same connection, or a turn publishing
    while one subscribes) raises "bound to a different event loop" out of
    ``subscribe``/``publish``. Same class of harness-only problem as
    ``_reset_sse_app_status`` below: under uvicorn there is exactly one loop for
    the process lifetime.
    """
    from app.services import events

    events.reset_redis()
    events._redis_lock = None
    events._local_subscribers.clear()
    yield
    events._local_subscribers.clear()
    events._redis_lock = None
    events.reset_redis()


@pytest.fixture(autouse=True)
def _reset_sse_app_status():
    """sse-starlette caches one anyio.Event on the class, bound to the loop that
    created it. pytest-asyncio gives every test a fresh loop, so the cached event
    has to be dropped or the second SSE test in a session raises
    "bound to a different event loop". Purely a harness concern - under uvicorn
    there is exactly one loop for the process lifetime."""
    try:
        from sse_starlette.sse import AppStatus
    except ImportError:  # pragma: no cover - sse-starlette is a hard dependency
        yield
        return
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None


@pytest.fixture(autouse=True)
async def _no_stray_background_tasks():
    """Cancel anything `services.background` detached, after every test.

    `POST /runs/{id}/resume` and `POST /approvals/{id}/decide` hand the agent
    loop to a background task so the button answers immediately. Under this
    harness that task shares the one asyncpg connection every session is bound
    to, so a task still running when a test ends holds something the *next*
    test needs — and the next test's `drain()` then waits out its whole budget
    for work that can never finish. Measured: a seven-minute suite became a
    twenty-minute one, with unrelated failures scattered through it, before
    this fixture existed.

    A test that wants the work to *happen* awaits `background.drain()` itself,
    which is deliberate: waiting is an assertion about behaviour and belongs in
    the test that makes it.
    """
    yield
    from app.services import background

    await background.cancel_all()


@pytest.fixture(autouse=True)
def _reset_idempotency_cache():
    from app.routers import threads as threads_module

    threads_module._idempotency_cache.clear()
    yield
    threads_module._idempotency_cache.clear()


@pytest.fixture(autouse=True)
def _reset_provider_credential_overrides():
    """`provider_credentials._overrides` is a bare `dict[provider_name]`, not
    keyed per-test like a row in the database is — a test that sets an
    "openai" override and does not clean up would leak it into every test
    that runs after it in the same process, including ones asserting every
    provider reads unconfigured."""
    from app.services import provider_credentials

    provider_credentials.reset_cache()
    yield
    provider_credentials.reset_cache()


class SSEProbe:
    """Drives one ASGI request by hand and parses the SSE frames as they arrive.

    ``httpx.ASGITransport`` buffers the whole response before handing it back, so
    it cannot observe a stream that never ends on its own - which is exactly what
    ``GET /threads/{id}/events`` is. Talking to the ASGI callable directly also
    lets a test deliver a real ``http.disconnect``, so the endpoint's
    disconnect-and-finalise path is genuinely exercised.
    """

    def __init__(self, body: bytes = b"") -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.frames: list[tuple[str, Any]] = []
        self._buffer = ""
        self._event_name = "message"
        self._body = body
        self._body_sent = False
        self._disconnect = asyncio.Event()
        self._arrived = asyncio.Event()

    # -- ASGI callables ---------------------------------------------------
    async def receive(self) -> dict[str, Any]:
        if not self._body_sent:
            self._body_sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message["headers"]
            }
        elif message["type"] == "http.response.body":
            self._feed(message.get("body", b"").decode("utf-8", "replace"))

    # -- parsing ----------------------------------------------------------
    def _feed(self, text: str) -> None:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                self._event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                raw = line.split(":", 1)[1].strip()
                try:
                    payload: Any = json.loads(raw)
                except (TypeError, ValueError):
                    payload = raw
                self.frames.append((self._event_name, payload))
                self._event_name = "message"
                self._arrived.set()

    # -- control ----------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.frames]

    def disconnect(self) -> None:
        self._disconnect.set()

    async def wait_until_open(self, timeout: float = 15.0) -> bool:
        """Wait until the response has started. Returns False on timeout.

        A fixed `asyncio.sleep(...)` here is a latency assertion nobody meant to
        make. Measured over eight full-suite runs the response start lands at
        ~5.5ms, and the one time it did not was a stall in the hundreds of
        milliseconds under a loaded container — nothing to do with the endpoint,
        and a flake in a suite of 1400 costs more than the sleep saved. This
        still fails loudly if the stream never opens, which is the only thing
        the caller actually cares about.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while self.status is None:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.005)
        return True

    async def wait_for(self, *names: str, timeout: float = 15.0) -> bool:
        """Wait until one of `names` has arrived. Returns False on timeout."""
        deadline = asyncio.get_running_loop().time() + timeout
        wanted = set(names)
        while True:
            if wanted & set(self.names):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            self._arrived.clear()
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout=min(remaining, 0.25))
            except TimeoutError:
                continue


@contextlib.asynccontextmanager
async def sse_probe(app_obj, method: str, path: str, *, headers=None, body: bytes = b""):
    """Open an SSE stream straight against the ASGI app; disconnects on exit."""
    header_pairs = [(b"host", b"testserver")]
    for key, value in (headers or {}).items():
        header_pairs.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    if body:
        header_pairs.append((b"content-type", b"application/json"))
        header_pairs.append((b"content-length", str(len(body)).encode("ascii")))

    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": header_pairs,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    REQUESTED.add((method.upper(), raw_path))
    probe = SSEProbe(body)
    task = asyncio.create_task(app_obj(scope, probe.receive, probe.send))
    try:
        yield probe
    finally:
        probe.disconnect()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def read_sse(
    response,
    *,
    stop_on: set[str] | None = None,
    limit: int = 400,
    timeout: float = 20.0,
) -> list[tuple[str, Any]]:
    """Collect `(event, data)` frames from a streaming SSE response."""
    frames: list[tuple[str, Any]] = []
    event_name = "message"

    async def _pump() -> None:
        nonlocal event_name
        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                raw = line.split(":", 1)[1].strip()
                try:
                    payload: Any = json.loads(raw)
                except (TypeError, ValueError):
                    payload = raw
                frames.append((event_name, payload))
                event_name = "message"
                if len(frames) >= limit:
                    return
                if stop_on and frames[-1][0] in stop_on:
                    return

    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        pass
    return frames
