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
  4. The final assistant response is sent back to the user, split into
     multiple messages if it exceeds Telegram's length limit.
"""

import asyncio
import logging
import os
import signal
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from anthropic import Anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from agent import run_loop, TurnResult, write_event
from db.connection import get_db, init_db
from mcp_client import mcp_client

load_dotenv()

_REQUIRED_ENV_VARS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID", "ANTHROPIC_API_KEY"]

# Telegram's max message length, counted in UTF-16 code units (so characters
# outside the Basic Multilingual Plane, such as most emoji, count as 2).
_TELEGRAM_MAX_LEN = 4096

# Sent in place of an empty reply (e.g. a turn that ended after tool calls with
# no text). Telegram rejects empty messages.
_EMPTY_REPLY_PLACEHOLDER = "(done \u2014 no reply text)"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs every request URL at INFO, and Telegram API URLs contain the bot
# token. Raise it to WARNING so the token never lands in the journal.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _write_ack_event(turn_id: str, telegram_message_id: int) -> None:
    """Record the outgoing 'working…' ack as a telegram_ack event."""
    with get_db() as conn:
        write_event(conn, turn_id, "telegram_ack", {"v": 1, "telegram_message_id": telegram_message_id})


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = _TELEGRAM_MAX_LEN) -> list[str]:
    """Split text into chunks of at most `limit` UTF-16 code units.

    Prefers to break at a paragraph break, then a line break, then a space,
    as long as that keeps the chunk at least half the limit; otherwise cuts
    hard at the limit. Returns no empty or whitespace-only chunks.
    """
    chunks = []
    while _utf16_len(text) > limit:
        # Longest prefix (in code points) that fits within the limit.
        units = 0
        end = 0
        for ch in text:
            units += 2 if ord(ch) > 0xFFFF else 1
            if units > limit:
                break
            end += 1
        window = text[:end]

        cut = end
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx >= end // 2:
                cut = idx
                break

        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    chunks.append(text)
    return [c for c in chunks if c.strip()]


async def _send_reply(update: Update, text: str) -> None:
    """Send text to the user, splitting long text and substituting a placeholder for empty text."""
    chunks = split_message(text) or [_EMPTY_REPLY_PLACEHOLDER]
    for chunk in chunks:
        await update.message.reply_text(chunk)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an incoming Telegram text message."""
    allowed_id = context.bot_data["allowed_user_id"]
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
        result = await run_loop(client, mcp, user_message, turn_id=turn_id)
    except Exception:
        logger.exception("run_loop raised an exception")
        await update.message.reply_text("Something went wrong. Please try again.")
        return

    await _send_reply(update, result.reply)
    if result.proactive:
        await _send_reply(update, result.proactive)


async def main() -> None:
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}")

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    init_db()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async with mcp_client() as mcp:
        app.bot_data["client"] = client
        app.bot_data["mcp"] = mcp
        app.bot_data["allowed_user_id"] = allowed_user_id

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
