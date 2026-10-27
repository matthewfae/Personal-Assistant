"""Tests for the heartbeat: pings when healthy, stays quiet when not."""

import asyncio

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from heartbeat import Heartbeat


class FakeCore:
    def __init__(self, worker_alive=True):
        self.worker_alive = worker_alive


def _run(core, status=200):
    """Ping a local test server once. Returns (ping result, number of requests received)."""
    hits = []

    async def handler(request):
        hits.append(request.path)
        return web.Response(status=status)

    async def main():
        app = web.Application()
        app.router.add_get("/ping", handler)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            return await Heartbeat(core, str(server.make_url("/ping"))).ping(session)

    return asyncio.run(main()), len(hits)


def test_pings_when_worker_alive():
    assert _run(FakeCore()) == (True, 1)


def test_skips_ping_when_worker_dead():
    assert _run(FakeCore(worker_alive=False)) == (False, 0)


def test_error_status_is_a_failed_ping():
    assert _run(FakeCore(), status=500) == (False, 1)


def test_unreachable_url_is_a_failed_ping_not_an_exception():
    async def main():
        async with aiohttp.ClientSession() as session:
            return await Heartbeat(FakeCore(), "http://127.0.0.1:1/ping").ping(session)

    assert asyncio.run(main()) is False
