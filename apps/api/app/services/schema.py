"""Ensure core schema exists (dev-friendly; prod uses migrations)."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


def split_sql_statements(raw: str) -> list[str]:
    """Split a SQL script on top-level semicolons.

    Respects single-quoted literals (including doubled '' escapes), dollar-quoted
    bodies ($$ … $$ and $tag$ … $tag$), line comments and block comments, so
    function bodies and JSON defaults survive intact.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(raw)
    in_single = False
    in_line_comment = False
    block_depth = 0
    dollar_tag: str | None = None

    while i < n:
        ch = raw[i]
        nxt = raw[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if block_depth:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                block_depth -= 1
                i += 2
                continue
            if ch == "/" and nxt == "*":
                buf.append(nxt)
                block_depth += 1
                i += 2
                continue
            i += 1
            continue

        if dollar_tag is not None:
            if raw.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if in_single:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":  # doubled quote escape
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        # Default state
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_depth = 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "$":
            close = raw.find("$", i + 1)
            tag_body = raw[i + 1 : close] if close != -1 else ""
            if close != -1 and (tag_body == "" or tag_body.isidentifier()):
                dollar_tag = raw[i : close + 1]
                buf.append(dollar_tag)
                i = close + 1
                continue
        if ch == ";":
            statements.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    statements.append("".join(buf))
    return [s.strip() for s in statements if _is_executable(s)]


def _is_executable(stmt: str) -> bool:
    """True when the chunk has SQL in it (not just whitespace and comments)."""
    meaningful = []
    for line in stmt.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        meaningful.append(stripped)
    return bool(meaningful)


async def ensure_schema(engine: AsyncEngine) -> None:
    """Run sql/init.sql. Every statement is IF NOT EXISTS guarded, so this is
    safe on both empty and already-populated databases."""
    sql_path = Path(__file__).resolve().parents[2] / "sql" / "init.sql"
    if not sql_path.exists():
        logger.warning("init.sql not found at %s — skipping schema bootstrap", sql_path)
        return

    statements = split_sql_statements(sql_path.read_text(encoding="utf-8"))
    failures = 0
    for stmt in statements:
        # Each statement gets its own transaction so one failure (e.g. missing
        # pgvector extension) does not abort the rest of the bootstrap.
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception as exc:  # noqa: BLE001 - dev bootstrap must not crash boot
            failures += 1
            logger.warning("schema statement failed: %s | %s", stmt.split("\n")[0][:120], exc)
    logger.info("schema bootstrap ran %d statements (%d failed)", len(statements), failures)
