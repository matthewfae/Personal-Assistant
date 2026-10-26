"""
Assistant core: owns the Anthropic client, the MCP session and the turn queue.

Channels (Telegram today) are thin adapters around the core. A channel submits
inbound messages with `Core.submit()` and implements `deliver()` for outbound
text. The core never imports a channel.

Wake-ups (`Core.submit_wakeup()`) are turns with no inbound message. They are
silent: the reply is not delivered anywhere. To reach the user, Claude calls
the notify_user tool, which delivers to the primary channel.

Every turn goes through one asyncio.Queue drained by a single worker, so turns
never overlap. Concurrent turns would interleave their events and race on the
context bound. A message that arrives mid-turn waits in the queue.
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from anthropic import AsyncAnthropic

from agent import LocalTool, run_loop
from jobs import Job
from mcp_client import MCPClient

logger = logging.getLogger(__name__)

# Sent to the channel when a turn raises.
_ERROR_REPLY = "Something went wrong. Please try again."


class Channel(Protocol):
    """A comms channel the core can send text to."""

    name: str

    async def deliver(self, text: str) -> None:
        """Send text to the user. Transport details (splitting, placeholders) are the channel's job."""
        ...


_NOTIFY_USER_DEFINITION = {
    "name": "notify_user",
    "description": (
        "Send Matt a message. Only available in wake-up turns, whose final reply is not "
        "sent to anyone; this is the only way to reach him. Use it only when it's worth "
        "interrupting him."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The message to send."}},
        "required": ["text"],
    },
}


@dataclass
class _Turn:
    text: str
    channel: Channel | None  # None for wake-ups
    turn_id: str
    job: str | None = None


class Core:
    def __init__(self, client: AsyncAnthropic, mcp: MCPClient):
        self._client = client
        self._mcp = mcp
        # None is the stop sentinel; see stop().
        self._queue: asyncio.Queue[_Turn | None] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._stopping = False
        # Where unprompted messages (notify_user) go.
        self.primary_channel: Channel | None = None

    def submit(self, text: str, channel: Channel, turn_id: str | None = None) -> str:
        """Queue a user message as a turn. Returns the turn_id.

        turn_id may be supplied by a channel that has already written a pre-turn
        event (e.g. telegram_ack) under it.
        """
        if self._stopping:
            raise RuntimeError("Core is stopping; not accepting new turns")
        turn = _Turn(text=text, channel=channel, turn_id=turn_id or uuid.uuid4().hex)
        self._queue.put_nowait(turn)
        return turn.turn_id

    def submit_wakeup(self, job: Job) -> str:
        """Queue a silent wake-up turn for job. Returns the turn_id."""
        if self._stopping:
            raise RuntimeError("Core is stopping; not accepting new turns")
        turn = _Turn(text=job.prompt, channel=None, turn_id=uuid.uuid4().hex, job=job.name)
        self._queue.put_nowait(turn)
        logger.info("Turn %s: wake-up queued for job %s", turn.turn_id, job.name)
        return turn.turn_id

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run_worker(), name="core-worker")

    async def stop(self) -> None:
        """Let the in-flight turn finish, then stop the worker.

        The in-flight turn is not cancelled: cancelling mid-tool-call would leave
        a tool_call event with no tool_result, which breaks every later projection.
        Turns still waiting in the queue are dropped (their user_message events
        were never written, so the history stays consistent).
        """
        self._stopping = True
        # Wakes the worker if it is idle; if it is busy, it checks _stopping after the turn.
        self._queue.put_nowait(None)
        if self._worker is not None:
            await self._worker
        dropped = sum(1 for t in self._drain() if t is not None)
        if dropped:
            logger.warning("Dropped %d queued turn(s) on shutdown", dropped)

    @asynccontextmanager
    async def running(self):
        """Start the worker on entry and stop it on exit."""
        self.start()
        try:
            yield self
        finally:
            await self.stop()

    def _drain(self) -> list[_Turn | None]:
        items = []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
        return items

    async def _run_worker(self) -> None:
        while not self._stopping:
            turn = await self._queue.get()
            if turn is None:
                break
            await self._process(turn)

    async def _process(self, turn: _Turn) -> None:
        """Run one turn and deliver its output. Never raises, so one bad turn can't kill the worker."""
        if turn.job is not None:
            await self._process_wakeup(turn)
            return
        try:
            result = await run_loop(self._client, self._mcp, turn.text, turn_id=turn.turn_id)
        except Exception:
            logger.exception("Turn %s: run_loop raised an exception", turn.turn_id)
            await self._deliver(turn.channel, _ERROR_REPLY, turn.turn_id)
            return

        await self._deliver(turn.channel, result.reply, turn.turn_id)
        if result.proactive:
            await self._deliver(turn.channel, result.proactive, turn.turn_id)

    async def _process_wakeup(self, turn: _Turn) -> None:
        async def notify_user(tool_input: dict) -> str:
            if self.primary_channel is None:
                raise RuntimeError("No primary channel configured")
            await self.primary_channel.deliver(tool_input["text"])
            return "Sent."

        try:
            await run_loop(
                self._client, self._mcp, turn.text, turn_id=turn.turn_id, job=turn.job,
                local_tools=[LocalTool(_NOTIFY_USER_DEFINITION, notify_user)],
            )
        except Exception:
            # Silent by design: a failed wake-up is logged, not sent to the user.
            logger.exception("Turn %s: wake-up %s raised an exception", turn.turn_id, turn.job)

    async def _deliver(self, channel: Channel, text: str, turn_id: str) -> None:
        try:
            await channel.deliver(text)
        except Exception:
            logger.exception("Turn %s: delivery to %s failed", turn_id, channel.name)
