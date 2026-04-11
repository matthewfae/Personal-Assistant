"""
Agent loop: handles multi-turn conversation with Claude API and tool use.
Tools are discovered from the MCP server at startup rather than hardcoded here.
Implements prompt caching for cost reduction on repeated context.
"""

from typing import Any

from anthropic import Anthropic

from mcp_client import MCPClient
from prompt_builder import PromptBuilder

_prompt_builder = PromptBuilder()


async def run_loop(
    client: Anthropic,
    mcp: MCPClient,
    messages: list[dict[str, Any]],
    user_message: str,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Run the agent loop for one user turn.

    Takes the existing message history and a new user message, runs until
    Claude produces a final text response, and returns that response along
    with the updated message history.
    """
    messages = list(messages)
    messages.append({"role": "user", "content": user_message})

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": _prompt_builder.build(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=mcp.tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final_text = next(
                (block.text for block in response.content if block.type == "text"),
                "",
            )
            return final_text, messages

        elif response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await mcp.call_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        else:
            return f"Unexpected stop reason: {response.stop_reason}", messages
