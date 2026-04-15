"""
Main entry point for testing the agent loop.
"""

import asyncio
import os
import sys
from pathlib import Path

# Ensure the project root (parent of bot/) is on sys.path so that the
# top-level packages (db, tools, …) are importable when running this script
# directly with "uv run python bot/main.py".
sys.path.insert(0, str(Path(__file__).parent.parent))

from anthropic import Anthropic
from dotenv import load_dotenv

from agent import run_loop
from db.connection import init_db
from mcp_client import mcp_client

load_dotenv()


async def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env file")
        return

    init_db()
    client = Anthropic(api_key=api_key)

    test_messages = [
        "Please add a note for me to get coffee.",
        "What's 2 + 2?",
    ]

    async with mcp_client() as mcp:
        for message in test_messages:
            print(f"\nUser: {message}")
            response = await run_loop(client, mcp, message)
            print(f"Assistant: {response}")
            print("-" * 60)


if __name__ == "__main__":
    asyncio.run(main())
