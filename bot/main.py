"""
Entry point for the Personal Assistant.

Starts the assistant core (Anthropic client, MCP session, turn queue), then
plugs in the Telegram channel and, if WEBHOOK_TOKEN is set, the webhook
listener, the scheduler and, if HEARTBEAT_URL is set, the heartbeat, and runs until SIGINT/SIGTERM.
"""

import asyncio
import logging
import os
import signal
import sys
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from core import Core
from db.connection import init_db
from heartbeat import Heartbeat
from log_setup import configure_logging
from mcp_client import mcp_client
from scheduler import Scheduler
from telegram_handler import REQUIRED_ENV_VARS as TELEGRAM_ENV_VARS, TelegramChannel
from webhooks import WebhookServer

load_dotenv()

_REQUIRED_ENV_VARS = ["ANTHROPIC_API_KEY", *TELEGRAM_ENV_VARS]

logger = logging.getLogger(__name__)


async def main() -> None:
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}")

    init_db()
    client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    async with mcp_client() as mcp:
        core = Core(client, mcp)
        telegram = TelegramChannel.from_env(core)
        core.primary_channel = telegram

        webhook_token = os.environ.get("WEBHOOK_TOKEN")
        if webhook_token:
            webhooks = WebhookServer(core, webhook_token).running()
        else:
            logger.warning("WEBHOOK_TOKEN not set; webhook listener disabled")
            webhooks = nullcontext()

        scheduler = Scheduler(core).running()

        heartbeat_url = os.environ.get("HEARTBEAT_URL")
        if heartbeat_url:
            heartbeat = Heartbeat(core, heartbeat_url).running()
        else:
            logger.warning("HEARTBEAT_URL not set; heartbeat disabled")
            heartbeat = nullcontext()

        # Shutdown runs inside-out: stop polling, webhooks and the scheduler so no new turns
        # arrive, then let the core finish its in-flight turn (which still
        # delivers through the bot), then shut the bot down.
        async with telegram.running(), core.running(), telegram.polling(), webhooks, scheduler, heartbeat:
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)

            logger.info("Assistant started. Waiting for messages.")
            await stop_event.wait()
            logger.info("Shutdown signal received. Stopping.")


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
