# Current State

## Layout
```
bot/agent.py              Agent loop (run_loop function)
bot/main.py               Test harness that sends scripted messages through the loop
bot/mcp_client.py         MCP client: spawns server subprocess, handshake, tool dispatch
bot/prompt_builder.py     Loads prompts/system.md and injects dynamic context
tools/server.py           MCP server exposing tools over stdio
db/                       Empty
tests/                    Empty
prompts/system.md         System prompt template with {current_time} placeholder
```

## Architecture

Tools are defined in `tools/server.py`, an MCP server that runs as a subprocess
communicating over stdio. The bot is an MCP client that spawns this server on startup.

## Flow

`main.py` opens an `mcp_client()` context, then sends test messages through `run_loop`.

`run_loop(client, mcp, messages, user_message)`:
1. Appends the user message to the conversation history.
2. Calls Claude with the history, rendered system prompt, and tool list from the MCP server.
3. On `end_turn`, extracts the final text response and returns it with updated history.
4. On `tool_use`, dispatches each tool call through `mcp.call_tool`, collects results
   into a single `user` message, appends it to history, and loops.

## MCP Client (`bot/mcp_client.py`)

`mcp_client()` is an async context manager that:
- Spawns `tools/server.py` as a subprocess via `StdioServerParameters`
- Performs the MCP initialization handshake (`session.initialize()`)
- Fetches the tool list from the server (`session.list_tools()`)
- Converts MCP `Tool` objects to the dict format the Anthropic API expects
- Adds `cache_control: ephemeral` to the last tool
- Yields an `MCPClient` instance with `.tools` and `.call_tool(name, arguments)`

## MCP Server (`tools/server.py`)

Registers two handlers via the raw MCP SDK:
- `@server.list_tools()` — returns the list of available tools
- `@server.call_tool()` — dispatches tool calls by name

Currently exposes one tool:
- `add_fact(key, value, category)` — placeholder; returns a confirmation string

## State

Conversation history is an in-memory list passed through `run_loop`. No persistence.

## System Prompt

`PromptBuilder` loads `prompts/system.md` at init time and injects `{current_time}`
via `.format()` on each `build()` call. The system prompt block is marked
`cache_control: ephemeral`.

## Config

`.env.example` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. Only `ANTHROPIC_API_KEY` is currently consumed.

## Model

`claude-haiku-4-5-20251001` with `max_tokens=8192`.
