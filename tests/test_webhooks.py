"""Tests for the webhook listener: auth, routing and cooldown."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from jobs import Job
from webhooks import Hook, WebhookServer


class FakeCore:
    def __init__(self):
        self.wakeups = []

    def submit_wakeup(self, job):
        self.wakeups.append(job.name)


_HOOKS = {"arrived-home": Hook(job=Job(name="arrived_home", prompt="p"), cooldown_seconds=60)}
_AUTH = {"Authorization": "Bearer s3cret"}


def _run(scenario):
    """Run scenario(client, core) against a WebhookServer with token 's3cret'."""
    core = FakeCore()

    async def main():
        server = WebhookServer(core, "s3cret", hooks=_HOOKS)
        async with TestClient(TestServer(server.app)) as client:
            await scenario(client)

    asyncio.run(main())
    return core


def test_rejects_missing_or_wrong_token():
    async def scenario(client):
        assert (await client.post("/hooks/arrived-home")).status == 401
        resp = await client.post("/hooks/arrived-home", headers={"Authorization": "Bearer nope"})
        assert resp.status == 401

    assert _run(scenario).wakeups == []


def test_unknown_hook_is_404():
    async def scenario(client):
        assert (await client.post("/hooks/nope", headers=_AUTH)).status == 404

    assert _run(scenario).wakeups == []


def test_get_is_not_allowed():
    async def scenario(client):
        assert (await client.get("/hooks/arrived-home", headers=_AUTH)).status == 405

    assert _run(scenario).wakeups == []


def test_accepts_then_ignores_repeat_within_cooldown():
    async def scenario(client):
        assert (await client.post("/hooks/arrived-home", headers=_AUTH)).status == 202
        resp = await client.post("/hooks/arrived-home", headers=_AUTH)
        assert resp.status == 200
        assert "cooldown" in await resp.text()

    assert _run(scenario).wakeups == ["arrived_home"]
