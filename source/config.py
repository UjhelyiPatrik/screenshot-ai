"""Application configuration and logging setup."""

from __future__ import annotations

import logging
import queue
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Immutable application configuration derived from the project root."""

    project_root: Path
    api_key_file: Path = field(init=False)
    prompt_dir: Path = field(init=False)
    token_db_file: Path = field(init=False)
    settings_file: Path = field(init=False)
    history_file: Path = field(init=False)
    history_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "api_key_file", self.project_root / "apikeys.txt")
        object.__setattr__(self, "prompt_dir", self.project_root / "prompt_files")
        object.__setattr__(self, "token_db_file", self.project_root / "token_usage.json")
        object.__setattr__(self, "settings_file", self.project_root / "settings.json")
        object.__setattr__(self, "history_file", self.project_root / "history.json")
        object.__setattr__(self, "history_dir", self.project_root / "history_images")


class AnsiFormatter(logging.Formatter):
    """Logging formatter that produces ANSI-colored level prefixes.

    The UI's JavaScript ANSI parser renders these colours in the log pane.
    """

    RESET = "\033[0m"
    LEVEL_STYLES: dict[int, str] = {
        logging.DEBUG: "\033[96m",           # Cyan
        logging.INFO: "\033[94m",            # Blue
        logging.WARNING: "\033[93m",         # Yellow
        logging.ERROR: "\033[91m",           # Red
        logging.CRITICAL: "\033[91m\033[1m", # Red + Bold
    }

    def format(self, record: logging.LogRecord) -> str:
        style = self.LEVEL_STYLES.get(record.levelno, self.RESET)
        return f"{style}{record.levelname}: {self.RESET}{record.getMessage()}"


class QueueHandler(logging.Handler):
    """Logging handler that enqueues formatted messages for the UI."""

    def __init__(self, log_queue: queue.Queue[str | None]) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record) + "\n")
        except Exception:
            self.handleError(record)


def set_console_log_level(level: int | str) -> None:
    """Dynamically adjust the console stream handler log level."""
    lvl = getattr(logging, level.upper()) if isinstance(level, str) else level
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, QueueHandler):
            h.setLevel(lvl)


def setup_logging(
    log_queue: queue.Queue[str | None] | None = None,
    level: int | str = logging.INFO,
) -> None:
    """Configure the root logger with a console handler and an optional UI queue handler."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Clean existing handlers to avoid duplicates on re-initialization
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = AnsiFormatter()

    console_level = getattr(logging, level.upper()) if isinstance(level, str) else level
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(console_level)
    root.addHandler(console)

    if log_queue is not None:
        queue_handler = QueueHandler(log_queue)
        queue_handler.setFormatter(formatter)
        queue_handler.setLevel(logging.DEBUG)
        root.addHandler(queue_handler)

    # Suppress verbose HTTP client & library debug dumps
    for noisy in ("httpcore", "httpx", "google", "PIL", "urllib3", "pystray", "webview", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
