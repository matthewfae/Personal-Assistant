"""
Loads and renders prompt templates from the prompts/ directory.

PromptBuilder injects ambient context (current_time) automatically.
Callers pass template-specific variables as kwargs to build().
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_TZ = ZoneInfo("America/Chicago")


class PromptBuilder:
    def __init__(self, template: str):
        self._template = (PROMPTS_DIR / template).read_text()

    def build(self, **kwargs) -> str:
        ambient = {
            "current_time": datetime.now(_TZ).strftime("%A, %B %-d, %Y at %-I:%M %p %Z"),
        }
        return self._template.format(**ambient, **kwargs)
