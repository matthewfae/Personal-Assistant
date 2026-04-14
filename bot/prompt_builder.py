"""
Loads and renders prompt templates from the prompts/ directory.

PromptBuilder injects ambient context (current_time) automatically.
Callers pass template-specific variables as kwargs to build().
"""

from pathlib import Path
from datetime import datetime

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class PromptBuilder:
    def __init__(self, template: str):
        self._template = (PROMPTS_DIR / template).read_text()

    def build(self, **kwargs) -> str:
        ambient = {
            "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self._template.format(**ambient, **kwargs)
