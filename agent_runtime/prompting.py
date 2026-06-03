from __future__ import annotations

import re
from pathlib import Path
from typing import Any


REQUIRED_PROMPT_SECTIONS = (
    "System Role",
    "Task Definition",
    "Context / Input",
    "Action Space (Tools)",
    "Constraints & Rules",
    "Output Format",
    "Examples",
    "Reflection / Memory",
)


class PromptTemplateLoader:
    """Loads repo prompt markdown files and renders chat-style prompts."""

    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[1] / "prompts"

    def load(self, relative_path: str) -> str:
        path = self.root / relative_path
        return path.read_text(encoding="utf-8")

    def validate_required_sections(self, relative_path: str) -> list[str]:
        text = self.load(relative_path)
        return [section for section in REQUIRED_PROMPT_SECTIONS if f"# {section}" not in text]

    def render_chat_prompt(self, relative_path: str, variables: dict[str, Any]) -> tuple[str, str]:
        rendered = self._render(self.load(relative_path), variables)
        system = self._section_body(rendered, "System Role").strip()
        user = self._remove_section(rendered, "System Role").strip()
        return system, user

    @staticmethod
    def _render(template: str, variables: dict[str, Any]) -> str:
        rendered = template
        for key, value in variables.items():
            rendered = rendered.replace("{{" + key + "}}", str(value))
        return rendered

    @staticmethod
    def _section_body(text: str, section: str) -> str:
        pattern = re.compile(rf"^# {re.escape(section)}\s*$", re.MULTILINE)
        match = pattern.search(text)
        if not match:
            return ""
        next_heading = re.search(r"^# .+?$", text[match.end() :], re.MULTILINE)
        end = match.end() + next_heading.start() if next_heading else len(text)
        return text[match.end() : end]

    @staticmethod
    def _remove_section(text: str, section: str) -> str:
        pattern = re.compile(rf"^# {re.escape(section)}\s*$", re.MULTILINE)
        match = pattern.search(text)
        if not match:
            return text
        next_heading = re.search(r"^# .+?$", text[match.end() :], re.MULTILINE)
        end = match.end() + next_heading.start() if next_heading else len(text)
        return (text[: match.start()] + text[end:]).strip()
