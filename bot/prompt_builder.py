"""
Loads and renders prompt templates from the prompts/ directory.

Templates are static: nothing time-varying goes in a system prompt, because any
change to it invalidates the prompt cache for everything after it. The current
time reaches Claude through the messages instead (see format_time).
Callers pass template-specific variables as kwargs to build().
"""

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))


def format_time(dt: datetime) -> str:
    """Render an aware datetime in the user's timezone, e.g. 'Monday, October 12, 2026 at 5:03 PM CDT'."""
    return dt.astimezone(_TZ).strftime("%A, %B %-d, %Y at %-I:%M %p %Z")


class PromptBuilder:
    def __init__(self, template: str):
        self._template = (PROMPTS_DIR / template).read_text()

    def build(self, **kwargs) -> str:
        return self._template.format(**kwargs)
