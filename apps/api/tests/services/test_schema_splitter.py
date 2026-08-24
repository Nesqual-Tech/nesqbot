"""`app.services.schema.split_sql_statements` — the init.sql statement splitter.

A naive `raw.split(";")` mangles dollar-quoted function bodies, semicolons inside
string literals, and comments — and the bootstrap then executes half-statements
against a live database. Every case below is one way that goes wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.schema import _is_executable, split_sql_statements

INIT_SQL = Path(__file__).resolve().parents[2] / "sql" / "init.sql"


def test_simple_statements():
    assert split_sql_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_a_trailing_semicolon_is_not_an_empty_statement():
    assert split_sql_statements("SELECT 1;") == ["SELECT 1"]
    assert split_sql_statements("SELECT 1") == ["SELECT 1"]


def test_whitespace_only_input_yields_nothing():
    assert split_sql_statements("") == []
    assert split_sql_statements("   \n\n  ") == []
    assert split_sql_statements(";;;") == []


# ---------------------------------------------------------------------------
# Quoted semicolons
# ---------------------------------------------------------------------------


def test_a_semicolon_inside_a_string_literal_does_not_split():
    sql = "INSERT INTO t (v) VALUES ('a;b'); SELECT 1;"
    assert split_sql_statements(sql) == ["INSERT INTO t (v) VALUES ('a;b')", "SELECT 1"]


def test_a_doubled_quote_escape_is_respected():
    sql = "INSERT INTO t (v) VALUES ('it''s; fine'); SELECT 2;"
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0] == "INSERT INTO t (v) VALUES ('it''s; fine')"


def test_a_json_default_containing_a_semicolon_survives():
    sql = "ALTER TABLE t ADD COLUMN meta JSONB NOT NULL DEFAULT '{\"a\": \"x;y\"}';"
    assert split_sql_statements(sql) == [
        "ALTER TABLE t ADD COLUMN meta JSONB NOT NULL DEFAULT '{\"a\": \"x;y\"}'"
    ]


def test_multiple_literals_in_one_statement():
    sql = "SELECT 'a;', 'b;', 'c'; SELECT 1;"
    assert split_sql_statements(sql) == ["SELECT 'a;', 'b;', 'c'", "SELECT 1"]


# ---------------------------------------------------------------------------
# Dollar quoting
# ---------------------------------------------------------------------------


def test_a_dollar_quoted_function_body_is_one_statement():
    sql = """
CREATE FUNCTION bump() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
SELECT 1;
"""
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert "RETURN NEW;" in statements[0]
    assert statements[0].startswith("CREATE FUNCTION")
    assert statements[1] == "SELECT 1"


def test_a_tagged_dollar_quote_is_one_statement():
    sql = """
CREATE FUNCTION f() RETURNS text AS $body$
  SELECT 'a; b; c';
$body$ LANGUAGE sql;
SELECT 2;
"""
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert "'a; b; c'" in statements[0]


def test_nested_looking_dollar_tags_do_not_terminate_early():
    sql = "DO $outer$ BEGIN PERFORM 1; END $outer$;"
    assert split_sql_statements(sql) == ["DO $outer$ BEGIN PERFORM 1; END $outer$"]


def test_a_lone_dollar_sign_is_not_treated_as_a_quote():
    sql = "SELECT 100 AS price_usd; SELECT '$' AS symbol;"
    assert split_sql_statements(sql) == ["SELECT 100 AS price_usd", "SELECT '$' AS symbol"]


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def test_a_semicolon_in_a_line_comment_does_not_split():
    sql = "SELECT 1; -- trailing; comment\nSELECT 2;"
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[1].endswith("SELECT 2")


def test_a_semicolon_in_a_block_comment_does_not_split():
    sql = "SELECT 1 /* here; there */; SELECT 2;"
    assert split_sql_statements(sql) == ["SELECT 1 /* here; there */", "SELECT 2"]


def test_a_comment_only_chunk_is_dropped():
    sql = "-- just a comment\n;\nSELECT 1;"
    assert split_sql_statements(sql) == ["SELECT 1"]


def test_is_executable_ignores_comments_and_blank_lines():
    assert _is_executable("SELECT 1") is True
    assert _is_executable("-- nothing here") is False
    assert _is_executable("\n\n   \n") is False
    assert _is_executable("-- lead\nSELECT 1") is True


# ---------------------------------------------------------------------------
# The real init.sql
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not INIT_SQL.exists(), reason="sql/init.sql not found")
def test_the_real_init_sql_splits_into_balanced_statements():
    statements = split_sql_statements(INIT_SQL.read_text(encoding="utf-8"))
    assert statements, "init.sql produced no statements"
    for statement in statements:
        assert statement.strip()
        assert statement.count("'") % 2 == 0, f"unbalanced quotes in: {statement[:80]!r}"
        assert not statement.strip().endswith(";")


@pytest.mark.skipif(not INIT_SQL.exists(), reason="sql/init.sql not found")
def test_every_init_sql_statement_is_idempotent():
    """The bootstrap runs on every boot, so nothing may be create-once."""
    allowed_prefixes = (
        "create extension if not exists",
        "create table if not exists",
        "create index if not exists",
        "create unique index if not exists",
        "alter table",
        "insert into",
        "do ",
        "comment on",
        "create or replace",
    )
    for statement in split_sql_statements(INIT_SQL.read_text(encoding="utf-8")):
        head = " ".join(
            line for line in statement.lower().splitlines() if not line.strip().startswith("--")
        ).strip()
        assert head.startswith(allowed_prefixes), f"non-idempotent statement: {head[:100]!r}"
