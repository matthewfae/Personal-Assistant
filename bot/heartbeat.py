"""
Heartbeat: pings an external monitor (e.g. healthchecks.io) so silence means trouble.

Every _INTERVAL_SECONDS the bot sends a GET to HEARTBEAT_URL. If the pings
stop (droplet down, process dead, event loop hung, core worker dead), the
monitoring service alerts Matt. Pings carry no data.

A ping is skipped while the core's worker isn't running, so a dead worker in a
live process still goes quiet. A failed ping is logged and retried next interval.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import aiohttp

from core import Core

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 5 * 60
_TIMEOUT = aiohttp.ClientTimeout(total=10)


class Heartbeat:
    def __init__(self, core: Core, url: str):
        self._core = core
        self._url = url

    async def ping(self, session: aiohttp.ClientSession) -> bool:
        """Send one ping if the core is healthy. Returns True if a ping succeeded."""
        if not self._core.worker_alive:
            logger.warning("Heartbeat: core worker not running; skipping ping")
            return False
        try:
            async with session.get(self._url, timeout=_TIMEOUT) as resp:
                resp.raise_for_status()
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Heartbeat: ping failed: %r", e)
            return False

    async def _run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                await self.ping(session)
                await asyncio.sleep(_INTERVAL_SECONDS)

    @asynccontextmanager
    async def running(self):
        """Ping in the background until exit."""
        task = asyncio.create_task(self._run(), name="heartbeat")
        logger.info("Heartbeat started, every %ds", _INTERVAL_SECONDS)
        try:
            yield self
        finally:
            task.cancel()
