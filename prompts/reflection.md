Hey Claude. This is the reflection step — the main turn just completed and a reply was sent.

Take a moment to do any housekeeping that makes sense:

- **Trim context** — call `set_context_bound` to advance the projection window. Trim aggressively; keep only what's still relevant.
- **Follow up** — if there's anything else you think Matt should know, include it as your response and it'll be sent to him. If not, just respond with `PASS`.

Datetime: {current_time}
