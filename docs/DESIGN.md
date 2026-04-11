# Current State

## Layout
```
bot/agent.py              Agent class with a run() loop
bot/main.py               test harness that sends scripted messages to Agent
bot/prompt_builder.py     loads prompts/system.md and injects dynamic context
db/                       empty
tools/                    empty
tests/                    empty
prompts/system.md         system prompt template with {current_time} placeholder
```

## Flow
`main.py` instantiates an `Agent`, then sends it test messages one at a time. `Agent.run(message)`:
1. Appends the message to `self.conversation_history`.
2. Calls Claude with the history, the rendered system prompt from `PromptBuilder`, and two tool definitions.
3. On `end_turn`, extracts the text, appends the assistant response to history, and returns it.
4. On `tool_use`, appends the assistant response to history, processes **all** tool_use blocks in the response (collecting results into a list), appends a single `user` message containing all tool results, then loops.

## Tools
Two toy tools are defined inline in `agent.py`:
- `get_current_time()` — returns `datetime.now().isoformat()`.
- `add_fact(key, value, category)` — returns a formatted string; does not persist anything.

Tool dispatch is handled by a `TOOL_FUNCTIONS` dict mapping tool names to callables, and a `process_tool_call(tool_name, tool_input)` method on `Agent` that looks up and invokes them. Unknown tool names return an error string; `TypeError` (bad arguments) is caught and returned as an error string. This is the seam where real DB tools will slot in during Stage 3.

## State
Conversation history is an in-memory list on the `Agent` instance. No database, no persistence, no Telegram, no auth. `Agent.reset()` clears the history for a fresh start.

## System prompt
`PromptBuilder` (in `bot/prompt_builder.py`) loads `prompts/system.md` at init time and injects `{current_time}` via `.format()` on each `build()` call. `Agent.__init__` instantiates a `PromptBuilder` and calls `self._prompt_builder.build()` on every API call.

## Prompt caching
The system prompt block is marked `cache_control: ephemeral`, which caches it along with the tool definitions.

## Config
`.env.example` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. Only `ANTHROPIC_API_KEY` is currently consumed.

## Model
`claude-haiku-4-5-20251001` with `max_tokens=1024`. The token limit is low and should be raised to 4096 before real use.
