"""Tests for db.query.run_query: read-only ad-hoc SQL."""

import pytest

from db import query
from db.connection import get_db
from db.query import run_query


@pytest.fixture
def tasks():
    with get_db() as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO tasks (area, summary, created_at, updated_at) VALUES (?, ?, 'now', 'now')",
                ("home", f"task {i}"),
            )


def test_select(tasks):
    rows = run_query("SELECT summary FROM tasks ORDER BY id")
    assert rows == [{"summary": "task 0"}, {"summary": "task 1"}, {"summary": "task 2"}]


def test_with_clause_allowed(tasks):
    rows = run_query("WITH h AS (SELECT * FROM tasks WHERE area = 'home') SELECT COUNT(*) AS n FROM h")
    assert rows == [{"n": 3}]


def test_semicolon_inside_string_literal_allowed(tasks):
    assert run_query("SELECT 'a;b' AS s") == [{"s": "a;b"}]


@pytest.mark.parametrize("sql", [
    "DELETE FROM tasks",
    "UPDATE tasks SET status = 'done'",
    "INSERT INTO shopping (item, created_at) VALUES ('x', 'now')",
    "DROP TABLE tasks",
    "CREATE TABLE evil (x)",
])
def test_writes_rejected(tasks, sql):
    with pytest.raises(ValueError, match="readonly"):
        run_query(sql)
    assert len(run_query("SELECT * FROM tasks")) == 3


def test_stacked_statements_rejected(tasks):
    with pytest.raises(ValueError):
        run_query("SELECT 1; DELETE FROM tasks")
    assert len(run_query("SELECT * FROM tasks")) == 3


def test_bad_sql_reported_as_value_error():
    with pytest.raises(ValueError, match="no such column"):
        run_query("SELECT nope FROM tasks")


def test_row_cap(tasks, monkeypatch):
    monkeypatch.setattr(query, "_MAX_ROWS", 2)
    assert len(run_query("SELECT * FROM tasks")) == 2
