"""
Main entry point for testing the agent loop.
"""

import asyncio
import os

from anthropic import Anthropic
from dotenv import load_dotenv

from agent import run_loop
from mcp_client import mcp_client

load_dotenv()


async def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env file")
        return

    client = Anthropic(api_key=api_key)

    test_messages = [
        "Please remember that I like coffee.",
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
