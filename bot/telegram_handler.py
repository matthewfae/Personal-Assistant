"""
Telegram bot handler: long-polling bot that routes messages through the agent loop.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID from the environment.
Only messages from the allowed user ID are processed; all others are silently
dropped.

For each accepted message:
  1. A "working…" reply is sent immediately for responsiveness.
  2. A telegram_ack event is written to the DB under a pre-generated turn_id.
  3. run_loop is called with the same turn_id so the ack and agent events share
     a single turn in the event log.
  4. The final assistant response is sent back to the user.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from anthropic import Anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from agent import run_loop
from db.connection import get_db, init_db
from mcp_client import mcp_client

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _write_ack_event(turn_id: str, telegram_message_id: int) -> None:
    """Record the outgoing 'working…' ack as a telegram_ack event."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO events (timestamp, turn_id, type, parent_event_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.utcnow().isoformat() + "Z",
                turn_id,
                "telegram_ack",
                None,
                json.dumps({"v": 1, "telegram_message_id": telegram_message_id}),
            ),
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an incoming Telegram text message."""
    allowed_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    if update.effective_user.id != allowed_id:
        logger.warning("Rejected message from user_id=%s", update.effective_user.id)
        return

    user_message = update.message.text
    turn_id = uuid.uuid4().hex

    ack = await update.message.reply_text("working\u2026")
    _write_ack_event(turn_id, ack.message_id)

    client: Anthropic = context.bot_data["client"]
    mcp = context.bot_data["mcp"]

    try:
        response_text = await run_loop(client, mcp, user_message, turn_id=turn_id)
    except Exception as e:
        logger.exception("run_loop raised an exception")
        await update.message.reply_text(f"Error: {e}")
        return

    await update.message.reply_text(response_text)


async def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    init_db()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async with mcp_client() as mcp:
        app.bot_data["client"] = client
        app.bot_data["mcp"] = mcp

        async with app:
            await app.start()
            await app.updater.start_polling()

            stop_event = asyncio.Event()
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)

            logger.info("Bot started. Waiting for messages.")
            await stop_event.wait()

            logger.info("Shutdown signal received. Stopping.")
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
