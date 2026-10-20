"""Token usage tracking and repository pattern persistence."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def _empty_data() -> dict:
    return {"total": 0, "daily": {}, "keys": {}}


def load_token_data(db_file: Path) -> dict:
    """Load token usage data from *db_file*, returning a safe default on any error."""
    if not db_file.exists():
        return _empty_data()

    try:
        data = json.loads(db_file.read_text(encoding="utf-8"))
        if "total" not in data or "daily" not in data:
            logger.warning("Token DB structure invalid in %s — resetting.", db_file)
            return _empty_data()
        if "keys" not in data or not isinstance(data["keys"], dict):
            data["keys"] = {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load token data from %s: %s", db_file, exc)
        return _empty_data()


def save_token_data(db_file: Path, data: dict) -> None:
    """Persist *data* as JSON to *db_file*."""
    try:
        db_file.write_text(json.dumps(data, indent=4), encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to save token data to %s: %s", db_file, exc)


def update_token_data(data: dict, tokens_used: int, key: str | None = None) -> dict:
    """Add *tokens_used* to *data*, optionally track per-key usage, and prune daily entries older than 30 days."""
    if not isinstance(tokens_used, int) or tokens_used < 0:
        logger.warning("Invalid token count (%s) — skipping update.", tokens_used)
        return data

    today_str = str(date.today())
    data["total"] = data.get("total", 0) + tokens_used
    if "daily" not in data or not isinstance(data["daily"], dict):
        data["daily"] = {}
    data["daily"][today_str] = data["daily"].get(today_str, 0) + tokens_used

    if key:
        if "keys" not in data or not isinstance(data["keys"], dict):
            data["keys"] = {}
        entry = data["keys"].setdefault(key, {"tokens": 0, "requests": 0, "last_used": ""})
        entry["tokens"] = entry.get("tokens", 0) + tokens_used
        entry["requests"] = entry.get("requests", 0) + 1
        entry["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")

    cutoff = date.today() - timedelta(days=30)
    data["daily"] = {
        day: count
        for day, count in data["daily"].items()
        if date.fromisoformat(day) >= cutoff
    }
    return data


class TokenRepository:
    """Thread-safe repository managing token consumption statistics and persistence."""

    def __init__(self, db_file: Path) -> None:
        self._db_file = db_file
        self._lock = threading.RLock()
        self._data: dict = load_token_data(db_file)

    def get_total(self) -> int:
        with self._lock:
            return self._data.get("total", 0)

    def get_today(self) -> int:
        with self._lock:
            today_str = str(date.today())
            return self._data.get("daily", {}).get(today_str, 0)

    def get_raw_data(self) -> dict:
        with self._lock:
            return dict(self._data)

    def record_usage(self, tokens_used: int, key: str | None = None) -> tuple[int, int]:
        """Record consumed tokens, persist changes, and return (total_tokens, today_tokens)."""
        with self._lock:
            self._data = update_token_data(self._data, tokens_used, key=key)
            save_token_data(self._db_file, self._data)
            return self.get_total(), self.get_today()

    def get_key_stats(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._data.get("keys", {}))
