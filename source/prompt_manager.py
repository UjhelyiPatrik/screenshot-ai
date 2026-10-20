"""Prompt management service.

Handles loading, validating, listing, and saving prompt preset files
on disk in the configured prompt directory.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class PromptManager:
    """Manages prompt presets stored as .txt files in the prompt directory."""

    def __init__(self, prompt_dir: Path) -> None:
        self._prompt_dir = prompt_dir

    @property
    def prompt_dir(self) -> Path:
        return self._prompt_dir

    def resolve_default_prompt(self, preferred_name: str | None = None) -> str:
        """Resolve the best default prompt preset name based on available files."""
        if preferred_name and (self._prompt_dir / self._normalize_name(preferred_name)).exists():
            return self._normalize_name(preferred_name)
        if (self._prompt_dir / "default_prompt_ultra.txt").exists():
            return "default_prompt_ultra.txt"
        return "default_prompt.txt"

    def load_prompt(self, prompt_file_name: str) -> str:
        """Read and validate prompt text from disk. Raises FileNotFoundError or ValueError."""
        name = self._normalize_name(prompt_file_name)
        path = self._prompt_dir / name
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")

        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"Prompt file is empty: {path}")

        logger.info("Loaded prompt: %s", path.name)
        return text

    def get_content(self, prompt_file_name: str) -> str:
        """Return the raw text of a prompt file, or empty string if not found."""
        name = self._normalize_name(prompt_file_name)
        path = self._prompt_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def save_prompt(self, prompt_file_name: str, content: str) -> bool:
        """Save a new or updated prompt preset to disk."""
        if not prompt_file_name:
            return False
        name = self._normalize_name(prompt_file_name)
        path = self._prompt_dir / name
        try:
            self._prompt_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content.strip(), encoding="utf-8")
            logger.info("Saved prompt preset: %s", path.name)
            return True
        except OSError as exc:
            logger.error("Failed to save prompt %s: %s", path, exc)
            return False

    def list_prompts(self) -> list[str]:
        """Return sorted list of available prompt filenames in prompt_dir."""
        if not self._prompt_dir.exists():
            return ["default_prompt.txt"]
        prompts = [p.name for p in self._prompt_dir.glob("*.txt")]
        return sorted(prompts) if prompts else ["default_prompt.txt"]

    @staticmethod
    def _normalize_name(prompt_file_name: str | None) -> str:
        clean = (prompt_file_name or "default_prompt.txt").strip()
        if not clean.endswith(".txt"):
            clean += ".txt"
        return clean
