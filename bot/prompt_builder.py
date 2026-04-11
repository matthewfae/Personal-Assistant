"""
Loads and builds the system prompt from a template file, injecting dynamic context.
"""

from pathlib import Path
from datetime import datetime

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class PromptBuilder:
    def __init__(self, template: str = "system.md"):
        self._template = (PROMPTS_DIR / template).read_text()

    def build(self) -> str:
        return self._template.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
