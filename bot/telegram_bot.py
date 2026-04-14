"""
Telegram bot entry point (Stage 5).

Starts the Telegram bot with long polling, routes incoming messages through
the agent loop, and replies with the result. The MCP server is spawned once
at startup and kept alive for the process lifetime.

Run from the bot/ directory:
    python telegram_bot.py
"""

import asyncio
import logging
import os
import uuid

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from agent import run_loop
from db.connection import init_db
from mcp_client import mcp_client

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _make_handler(client: AsyncAnthropic, mcp, conversation_id: str, allowed_user_id: int):
    """Return a Telegram message handler closed over the shared resources."""

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Allowlist check — silently ignore unauthorized senders.
        user = update.effective_user
        if user is None or user.id != allowed_user_id:
            logger.warning(
                "Rejected message from user_id=%s (not in allowlist)",
                user.id if user else "unknown",
            )
            return

        user_text = update.message.text
        if not user_text:
            return

        # Acknowledge immediately so the user knows the message landed.
        await update.message.reply_text("⏳ working…")

        try:
            response = await run_loop(
                client,
                mcp,
                conversation_id,
                user_text,
                source="telegram",
            )
            await update.message.reply_text(response)
        except Exception as e:
            logger.exception("run_loop raised an exception")
            await update.message.reply_text(f"Error: {e}")

    return handle_message


async def run_bot() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    allowed_user_id_str = os.getenv("TELEGRAM_ALLOWED_USER_ID")
    if not allowed_user_id_str:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID is not set in .env")
    allowed_user_id = int(allowed_user_id_str)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")

    init_db()

    client = AsyncAnthropic(api_key=api_key)
    conversation_id = str(uuid.uuid4())

    logger.info("Starting Telegram bot (conversation_id=%s)", conversation_id)

    async with mcp_client() as mcp:
        app = Application.builder().token(token).build()

        handler = _make_handler(client, mcp, conversation_id, allowed_user_id)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))

        # Use the async lifecycle API so we stay inside the mcp_client context
        # for the full process lifetime.
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Bot is running. Press Ctrl-C to stop.")

        try:
            # Keep alive until interrupted.
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received")
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run_bot())
