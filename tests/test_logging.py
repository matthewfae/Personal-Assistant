"""Tests for log formatting, the per-turn summary, and Telegram polling error logging."""

import asyncio
import logging
import sys

from telegram.error import BadRequest, NetworkError

from agent import run_loop, write_event
from db.connection import get_db
from log_setup import JournalFormatter
from telegram_handler import _on_polling_error
from tests.test_run_loop import FakeClient, FakeMCP, _response, _text


def _record(level, msg, exc_info=None):
    return logging.LogRecord("x", level, __file__, 1, msg, None, exc_info)


def test_journal_formatter_prefixes_every_line_with_priority():
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record(logging.ERROR, "failed", sys.exc_info())

    lines = JournalFormatter("%(levelname)s %(message)s").format(record).splitlines()

    assert lines[0] == "<3>ERROR failed"
    assert len(lines) > 1  # traceback lines follow
    assert all(line.startswith("<3>") for line in lines)


def test_journal_formatter_maps_levels():
    fmt = JournalFormatter("%(message)s")
    assert fmt.format(_record(logging.WARNING, "w")) == "<4>w"
    assert fmt.format(_record(logging.INFO, "i")) == "<6>i"
    assert fmt.format(_record(logging.DEBUG, "d")) == "<7>d"


def test_turn_summary_logged(caplog):
    client = FakeClient([
        _response("end_turn", _text("hello")),
    ])

    with caplog.at_level(logging.INFO, logger="agent"):
        asyncio.run(run_loop(client, FakeMCP(), "hi", turn_id="t1"))

    [summary] = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert summary.startswith("Turn t1 finished in ")
    # One API call at 10 in / 5 out, from the fake client.
    assert "1 API calls, tokens in=10 cache_read=0 cache_write=0 out=5" in summary


def test_turn_summary_warns_on_error_events(caplog, monkeypatch):
    # The turn hits the API call cap; that's recorded as an error event and
    # surfaced as a WARNING.
    import agent
    from tests.test_run_loop import _tool_use

    monkeypatch.setattr(agent, "_MAX_API_CALLS", 1)
    client = FakeClient([_response("tool_use", _tool_use("toolu_1", "list_tasks", {}))])

    with caplog.at_level(logging.INFO, logger="agent"):
        asyncio.run(run_loop(client, FakeMCP(), "hi", turn_id="t2"))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith("Turn t2: main_turn error (event id=")


def test_turn_summary_ignores_other_turns(caplog):
    with get_db() as conn:
        write_event(conn, "other", "error", {"v": 1, "context": "main_turn", "message": "x"})
    client = FakeClient([
        _response("end_turn", _text("hello")),
    ])

    with caplog.at_level(logging.INFO, logger="agent"):
        asyncio.run(run_loop(client, FakeMCP(), "hi", turn_id="t3"))

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_polling_network_error_is_one_line_warning(caplog):
    with caplog.at_level(logging.INFO, logger="telegram_handler"):
        _on_polling_error(NetworkError("Bad Gateway"))

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "Bad Gateway" in record.getMessage()


def test_polling_other_error_keeps_traceback(caplog):
    with caplog.at_level(logging.INFO, logger="telegram_handler"):
        _on_polling_error(BadRequest("something odd"))

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
