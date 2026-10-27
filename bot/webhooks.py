"""
Webhook listener: HTTP endpoints that start wake-up jobs.

Listens on 127.0.0.1 only; Caddy terminates HTTPS for pa.mattfae.com and
forwards /hooks/* here (see /etc/caddy/Caddyfile). Every request must carry
`Authorization: Bearer <WEBHOOK_TOKEN>`, since each accepted hook costs an API turn.

Routes are POST /hooks/<name>, mapped to jobs in HOOKS. A hook that fires again
within its cooldown is acknowledged but ignored: iPhone geofences can fire
several times on one arrival.
"""

import hmac
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiohttp import web

from core import Core
from jobs import ARRIVED_HOME, Job

logger = logging.getLogger(__name__)

_HOST = "127.0.0.1"
_PORT = 8787


@dataclass(frozen=True)
class Hook:
    job: Job
    cooldown_seconds: float


HOOKS = {
    "arrived-home": Hook(job=ARRIVED_HOME, cooldown_seconds=15 * 60),
}


class WebhookServer:
    def __init__(self, core: Core, token: str, hooks: dict[str, Hook] = HOOKS):
        self._core = core
        self._expected_auth = f"Bearer {token}".encode()
        self._hooks = hooks
        # Hook name -> time.monotonic() of the last accepted call. In memory, so
        # a restart resets cooldowns; that's acceptable.
        self._last_fired: dict[str, float] = {}
        self.app = web.Application()
        self.app.router.add_post("/hooks/{name}", self._handle)

    async def _handle(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(auth, self._expected_auth):
            logger.warning("Webhook %s: rejected request with bad or missing token", request.path)
            return web.Response(status=401, text="unauthorized\n")

        name = request.match_info["name"]
        hook = self._hooks.get(name)
        if hook is None:
            return web.Response(status=404, text="unknown hook\n")

        now = time.monotonic()
        last = self._last_fired.get(name)
        if last is not None and now - last < hook.cooldown_seconds:
            logger.info("Webhook %s: ignored, within %ds cooldown", name, hook.cooldown_seconds)
            return web.Response(status=200, text="ignored (cooldown)\n")

        self._last_fired[name] = now
        self._core.submit_wakeup(hook.job)
        return web.Response(status=202, text="accepted\n")

    @asynccontextmanager
    async def running(self):
        """Serve on 127.0.0.1 until exit."""
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, _HOST, _PORT)
        await site.start()
        logger.info("Webhook listener on http://%s:%d", _HOST, _PORT)
        try:
            yield self
        finally:
            await runner.cleanup()
