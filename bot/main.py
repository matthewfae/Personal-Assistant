"""
Main entry point for testing the agent loop.
"""

import asyncio
import os
import uuid

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent import run_loop
from db.connection import init_db
from mcp_client import mcp_client

load_dotenv()


async def main():
    init_db()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env file")
        return

    client = AsyncAnthropic(api_key=api_key)

    test_messages = [
        "Please remember that I like coffee.",
        "What's 2 + 2?",
    ]

    async with mcp_client() as mcp:
        conversation_id = str(uuid.uuid4())
        for message in test_messages:
            print(f"\nUser: {message}")
            response = await run_loop(client, mcp, conversation_id, message)
            print(f"Assistant: {response}")
            print("-" * 60)


if __name__ == "__main__":
    asyncio.run(main())
