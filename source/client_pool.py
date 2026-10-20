"""API client pool implementing the Object Pool and Factory patterns for Gemini clients."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from google import genai

logger = logging.getLogger(__name__)


class ApiClientPool:
    """Thread-safe pool managing multiple Gemini API keys and client instances.

    Implements round-robin rotation, dynamic key addition/removal,
    and automatic persistence of the active key index.
    """

    def __init__(self, key_file: Path) -> None:
        self._key_file = key_file
        self._lock = threading.RLock()
        self._api_keys: list[str] = []
        self._last_index: int = -1
        self._clients: dict[str, genai.Client] = {}

        self._load_keys()
        self._init_clients()

    @property
    def clients(self) -> dict[str, genai.Client]:
        with self._lock:
            return dict(self._clients)

    def get_keys(self) -> list[str]:
        """Return list of loaded API keys."""
        with self._lock:
            return list(self._api_keys)

    def get_last_index(self) -> int:
        """Return index of the most recently used API key."""
        with self._lock:
            return self._last_index

    def get_active_client(self) -> tuple[genai.Client, str, int]:
        """Rotate to next API key thread-safely and return (client, key, key_index)."""
        with self._lock:
            if not self._api_keys:
                raise ValueError("No API keys available.")
            self._last_index = (self._last_index + 1) % len(self._api_keys)
            key = self._api_keys[self._last_index]
            self._persist_last_index()
            client = self._clients[key]
            logger.info("Using API key #%d.", self._last_index + 1)
            return client, key, self._last_index

    def add_key(self, new_key: str) -> bool:
        """Add a new Gemini API key, initialize its client, and persist to key file."""
        clean = new_key.strip()
        if not clean or clean.startswith("#"):
            return False
        with self._lock:
            if clean in self._api_keys:
                return False
            self._api_keys.append(clean)
            try:
                self._clients[clean] = genai.Client(api_key=clean)
            except Exception as exc:
                logger.error("Failed to initialize client for key: %s", exc)
            self._persist_last_index()
            logger.info("Added new API key. Total keys: %d", len(self._api_keys))
            return True

    def remove_key(self, key_to_remove: str) -> bool:
        """Remove an API key and update client pool and key file."""
        clean = key_to_remove.strip()
        with self._lock:
            if clean not in self._api_keys:
                return False
            if len(self._api_keys) <= 1:
                logger.warning("Cannot remove the only remaining API key.")
                return False
            self._api_keys.remove(clean)
            self._clients.pop(clean, None)
            if self._last_index >= len(self._api_keys):
                self._last_index = 0
            self._persist_last_index()
            logger.info("Removed API key. Remaining keys: %d", len(self._api_keys))
            return True

    def _load_keys(self) -> None:
        if not self._key_file.exists():
            raise FileNotFoundError(f"API key file not found: {self._key_file}")

        lines = [ln.strip() for ln in self._key_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            raise ValueError(f"API key file is empty: {self._key_file}")

        first = lines[0]
        if first.startswith("#") and "last_index=" in first:
            try:
                self._last_index = int(first.split("last_index=")[1].strip())
            except (ValueError, IndexError):
                self._last_index = -1

        self._api_keys = [ln for ln in lines if not ln.startswith("#")]
        if not self._api_keys:
            raise ValueError(f"No valid API keys in: {self._key_file}")

        if self._last_index < -1 or self._last_index >= len(self._api_keys):
            self._last_index = -1

        logger.info("Loaded %d API key(s).", len(self._api_keys))

    def _init_clients(self) -> None:
        with self._lock:
            for key in self._api_keys:
                if key not in self._clients:
                    self._clients[key] = genai.Client(api_key=key)
            if self._last_index == -1 and self._api_keys:
                self._last_index = 0
                self._persist_last_index()
            logger.info("Initialized Gemini client pool for %d key(s).", len(self._clients))

    def _persist_last_index(self) -> None:
        try:
            content = f"# last_index={self._last_index}\n" + "\n".join(self._api_keys) + "\n"
            self._key_file.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to persist last_index: %s", exc)
