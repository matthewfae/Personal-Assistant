"""
Test harness: sends scripted messages through the agent loop directly,
without Telegram. Useful for local development and debugging.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent import run_loop
from db.connection import init_db
from log_setup import configure_logging
from mcp_client import mcp_client

load_dotenv()


async def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env file")
        return

    init_db()
    client = AsyncAnthropic(api_key=api_key)

    test_messages = [
        "Please add a note for me to get coffee.",
        "What's 2 + 2?",
    ]

    async with mcp_client() as mcp:
        for message in test_messages:
            print(f"\nUser: {message}")
            result = await run_loop(client, mcp, message)
            print(f"Assistant: {result.reply}")
            print("-" * 60)


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
