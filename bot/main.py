"""
Entry point for the Personal Assistant Telegram bot.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from log_setup import configure_logging
from telegram_handler import main

if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
