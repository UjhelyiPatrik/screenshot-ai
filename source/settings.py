"""Application settings management and persistence."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AppSettings:
    """Strongly typed application configuration settings."""

    selected_model: str | None = None
    hotkey: str = "ctrl+shift+q"
    prompt_file: str = "default_prompt.txt"
    capture_mode: str = "cursor_monitor"
    pdf_sources: list[str] = field(default_factory=list)
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsManager:
    """Thread-safe manager for loading, updating, and saving user settings to settings.json."""

    def __init__(self, settings_file: Path) -> None:
        self._settings_file = settings_file
        self._lock = threading.RLock()
        self._settings: AppSettings = self._load()

    @property
    def settings(self) -> AppSettings:
        with self._lock:
            return self._settings

    def _load(self) -> AppSettings:
        if self._settings_file.exists():
            try:
                data = json.loads(self._settings_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return AppSettings.from_dict(data)
            except Exception as exc:
                logger.warning("Failed to load settings from %s: %s", self._settings_file, exc)
        return AppSettings()

    def save(self) -> None:
        """Persist current settings to settings.json."""
        with self._lock:
            try:
                payload = json.dumps(self._settings.to_dict(), indent=2)
                self._settings_file.write_text(payload, encoding="utf-8")
            except Exception as exc:
                logger.warning("Failed to save settings: %s", exc)

    def update(self, **kwargs: Any) -> None:
        """Update one or more settings fields and save to disk."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._settings, k):
                    setattr(self._settings, k, v)
            self.save()
