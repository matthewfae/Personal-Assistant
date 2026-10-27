"""Tests for the MCP client against the real tools server, run as a subprocess on the test DB."""

import asyncio

import pytest

import db.connection
from mcp_client import mcp_client


@pytest.fixture(autouse=True)
def server_uses_test_db(monkeypatch):
    # The server is a separate process: hand it the test DB via the environment,
    # or it would read DB_PATH from .env and use the live database.
    monkeypatch.setenv("DB_PATH", db.connection.get_db_path())


def _run(scenario):
    async def main():
        async with mcp_client() as client:
            return await scenario(client)

    return asyncio.run(main())


def test_lists_tools_for_the_anthropic_api():
    async def scenario(client):
        return client.tools

    tools = _run(scenario)
    names = {t["name"] for t in tools}
    assert {"add_task", "list_tasks", "list_shopping", "log_symptom", "query"} <= names
    assert all({"name", "description", "input_schema"} <= t.keys() for t in tools)
    # Only the last tool carries cache_control, which caches the whole list.
    assert [t["name"] for t in tools if "cache_control" in t] == [tools[-1]["name"]]


def test_call_tool_writes_to_the_test_db():
    async def scenario(client):
        added = await client.call_tool("add_shopping", {"item": "milk"})
        listed = await client.call_tool("list_shopping", {})
        return added, listed

    added, listed = _run(scenario)
    assert not added.is_error
    assert "milk" in listed.content
    with db.connection.get_db() as conn:
        assert [r["item"] for r in conn.execute("SELECT item FROM shopping")] == ["milk"]


def test_tool_exception_is_an_error_result():
    async def scenario(client):
        return await client.call_tool("no_such_tool", {})

    result = _run(scenario)
    assert result.is_error
    assert "Unknown tool" in result.content
