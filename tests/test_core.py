"""Tests for core.Core: turn serialization, delivery and shutdown, with run_loop faked out."""

import asyncio

import pytest

import core
from agent import TurnResult


class FakeChannel:
    name = "fake"

    def __init__(self, fail=False):
        self.delivered = []
        self._fail = fail

    async def deliver(self, text):
        if self._fail:
            raise RuntimeError("send failed")
        self.delivered.append(text)


@pytest.fixture
def fake_run_loop(monkeypatch):
    """Replace run_loop with a fake that records overlap and can be gated per turn."""
    state = {"active": 0, "max_active": 0, "started": [], "gates": {}}

    async def run_loop(client, mcp, text, turn_id=None):
        state["started"].append(text)
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        try:
            if text in state["gates"]:
                await state["gates"][text].wait()
            await asyncio.sleep(0)
            if text == "boom":
                raise ValueError("boom")
            return TurnResult(reply=f"re: {text}", proactive="extra" if text == "proactive" else None)
        finally:
            state["active"] -= 1

    monkeypatch.setattr(core, "run_loop", run_loop)
    return state


async def _until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def test_turns_run_one_at_a_time_in_order(fake_run_loop):
    channel = FakeChannel()

    async def scenario():
        c = core.Core(client=None, mcp=None)
        async with c.running():
            for text in ("a", "b", "c"):
                c.submit(text, channel)
            await _until(lambda: len(channel.delivered) == 3)

    asyncio.run(scenario())
    assert channel.delivered == ["re: a", "re: b", "re: c"]
    assert fake_run_loop["max_active"] == 1


def test_proactive_text_is_delivered_after_reply(fake_run_loop):
    channel = FakeChannel()

    async def scenario():
        c = core.Core(client=None, mcp=None)
        async with c.running():
            c.submit("proactive", channel)
            await _until(lambda: len(channel.delivered) == 2)

    asyncio.run(scenario())
    assert channel.delivered == ["re: proactive", "extra"]


def test_failed_turn_sends_error_reply_and_worker_continues(fake_run_loop):
    channel = FakeChannel()

    async def scenario():
        c = core.Core(client=None, mcp=None)
        async with c.running():
            c.submit("boom", channel)
            c.submit("after", channel)
            await _until(lambda: len(channel.delivered) == 2)

    asyncio.run(scenario())
    assert channel.delivered == [core._ERROR_REPLY, "re: after"]


def test_failed_delivery_does_not_stop_worker(fake_run_loop):
    broken, ok = FakeChannel(fail=True), FakeChannel()

    async def scenario():
        c = core.Core(client=None, mcp=None)
        async with c.running():
            c.submit("a", broken)
            c.submit("b", ok)
            await _until(lambda: ok.delivered)

    asyncio.run(scenario())
    assert ok.delivered == ["re: b"]


def test_stop_finishes_in_flight_turn_and_drops_queued(fake_run_loop, caplog):
    channel = FakeChannel()
    gate = asyncio.Event()
    fake_run_loop["gates"]["slow"] = gate

    async def scenario():
        c = core.Core(client=None, mcp=None)
        c.start()
        c.submit("slow", channel)
        c.submit("queued", channel)
        await _until(lambda: fake_run_loop["started"] == ["slow"])

        stopping = asyncio.create_task(c.stop())
        await asyncio.sleep(0)
        assert not stopping.done()  # waits for the in-flight turn
        gate.set()
        await stopping

        with pytest.raises(RuntimeError):
            c.submit("late", channel)

    asyncio.run(scenario())
    assert channel.delivered == ["re: slow"]
    assert fake_run_loop["started"] == ["slow"]
    assert "Dropped 1 queued turn" in caplog.text


def test_stop_when_idle_returns_promptly(fake_run_loop):
    async def scenario():
        c = core.Core(client=None, mcp=None)
        c.start()
        await asyncio.wait_for(c.stop(), timeout=1)

    asyncio.run(scenario())
