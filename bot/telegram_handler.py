"""
Telegram channel: a long-polling adapter between Telegram and the assistant core.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID from the environment.
Only messages from the allowed user ID are processed; all others are dropped
with a WARNING.

For each accepted message:
  1. A "working…" reply is sent immediately for responsiveness.
  2. A telegram_ack event is written to the DB under a pre-generated turn_id.
  3. The message is submitted to the core under the same turn_id, so the ack
     and agent events share a single turn in the event log. The handler
     returns right away; the core runs turns one at a time.
  4. The core calls deliver() with the reply, which is sent to the user's chat,
     split into multiple messages if it exceeds Telegram's length limit.
"""

import logging
import os
import uuid
from contextlib import asynccontextmanager

from telegram import Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from agent import write_event
from core import Core
from db.connection import get_db

REQUIRED_ENV_VARS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID"]

# Telegram's max message length, counted in UTF-16 code units (so characters
# outside the Basic Multilingual Plane, such as most emoji, count as 2).
_TELEGRAM_MAX_LEN = 4096

# Sent in place of an empty reply (e.g. a turn that ended after tool calls with
# no text). Telegram rejects empty messages.
_EMPTY_REPLY_PLACEHOLDER = "(done \u2014 no reply text)"

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


def _on_polling_error(exc: TelegramError) -> None:
    """Log an error from polling Telegram for updates. python-telegram-bot retries on its own.

    Network errors (a 502 from Telegram, a dropped connection) are routine and
    transient, so they get one WARNING line instead of a full traceback.
    Anything else is logged with its traceback. BadRequest subclasses
    NetworkError but means the request itself was wrong, so it is excluded.
    """
    if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
        logger.warning("Polling Telegram failed, retrying: %s", exc)
    else:
        logger.error("Polling Telegram failed, retrying", exc_info=exc)


class TelegramChannel:
    """Telegram adapter: submits the allowed user's messages to the core and delivers replies to their chat."""

    name = "telegram"

    def __init__(self, token: str, allowed_user_id: int, core: Core):
        self._allowed_user_id = allowed_user_id
        self._core = core
        self._app = ApplicationBuilder().token(token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

    @classmethod
    def from_env(cls, core: Core) -> "TelegramChannel":
        return cls(
            token=os.environ["TELEGRAM_BOT_TOKEN"],
            allowed_user_id=int(os.environ["TELEGRAM_ALLOWED_USER_ID"]),
            core=core,
        )

    async def deliver(self, text: str) -> None:
        """Send text to the user, splitting long text and substituting a placeholder for empty text.

        In a private chat the chat id is the user id, so this needs no incoming
        message to reply to (wake-ups will deliver unprompted).
        """
        chunks = split_message(text) or [_EMPTY_REPLY_PLACEHOLDER]
        for chunk in chunks:
            await self._app.bot.send_message(chat_id=self._allowed_user_id, text=chunk)

    @asynccontextmanager
    async def running(self):
        """Initialize and start the bot (without polling), and shut it down on exit."""
        async with self._app:
            await self._app.start()
            try:
                yield self
            finally:
                await self._app.stop()

    @asynccontextmanager
    async def polling(self):
        """Poll Telegram for updates; stop polling on exit. Must be used inside running()."""
        await self._app.updater.start_polling(error_callback=_on_polling_error)
        try:
            yield self
        finally:
            await self._app.updater.stop()

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ack an incoming text message and submit it to the core."""
        if update.effective_user.id != self._allowed_user_id:
            logger.warning("Rejected message from user_id=%s", update.effective_user.id)
            return

        turn_id = uuid.uuid4().hex
        ack = await update.message.reply_text("working\u2026")
        _write_ack_event(turn_id, ack.message_id)
        self._core.submit(update.message.text, self, turn_id=turn_id)
