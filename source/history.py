"""Query history repository and screenshot asset manager."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)


class HistoryRepository:
    """Thread-safe repository managing query history records and disk-persisted screenshots."""

    def __init__(self, history_file: Path, history_dir: Path, max_entries: int = 100) -> None:
        self._history_file = history_file
        self._history_dir = history_dir
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._entries: list[dict] = self._load_and_heal()

    def get_all(self) -> list[dict]:
        with self._lock:
            return list(self._entries)

    def add(self, entry: dict) -> None:
        """Insert a new history entry at the front, prune to max entries, and persist."""
        with self._lock:
            self._entries.insert(0, entry)
            if len(self._entries) > self._max_entries:
                self._entries.pop()
            self._save()

    def clear(self) -> None:
        """Clear all in-memory history entries and update history.json."""
        with self._lock:
            self._entries.clear()
            self._save()
        logger.info("Query history cleared.")

    def delete_item(self, item_id: Any) -> bool:
        """Delete a single query from history by ID, timestamp, or path, and remove its screenshot."""
        with self._lock:
            target_str = str(item_id).strip()
            found_idx = -1
            for i, entry in enumerate(self._entries):
                if (
                    str(entry.get("id")) == target_str
                    or str(entry.get("time")) == target_str
                    or str(entry.get("image_path")) == target_str
                ):
                    found_idx = i
                    break

            if found_idx == -1:
                return False

            removed = self._entries.pop(found_idx)
            img_path = removed.get("image_path")
            if img_path:
                try:
                    clean_path = os.path.normpath(img_path)
                    if os.path.exists(clean_path):
                        os.remove(clean_path)
                        logger.debug("Removed screenshot file from disk: %s", clean_path)
                except Exception as exc:
                    logger.debug("Could not delete image file %s: %s", img_path, exc)

            self._save()
            logger.info("Deleted history entry #%s (%s).", item_id, removed.get("time"))
            return True

    def persist_screenshot(self, request_id: int, image_bytes: bytes) -> tuple[str | None, str | None]:
        """Save screenshot bytes to history_images and return (image_path, thumbnail_b64)."""
        t_start = time.perf_counter()
        image_path: str | None = None
        thumbnail_b64: str | None = None

        try:
            self._history_dir.mkdir(parents=True, exist_ok=True)
            saved_file = self._history_dir / f"shot_{int(time.time())}_{request_id}.jpg"
            saved_file.write_bytes(image_bytes)
            image_path = saved_file.resolve().as_posix()

            with Image.open(io.BytesIO(image_bytes)) as pil_img:
                pil_img.thumbnail((360, 220))
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85)
                thumbnail_b64 = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

            t_elapsed = (time.perf_counter() - t_start) * 1000
            logger.debug(
                "Persisted history screenshot: %s (%.1f KB), thumbnail generated in %.1f ms",
                saved_file.name,
                len(image_bytes) / 1024,
                t_elapsed,
            )
        except Exception as exc:
            logger.warning("Could not persist screenshot or thumbnail: %s", exc)

        return image_path, thumbnail_b64

    def get_image_base64(self, image_path: str) -> str | None:
        """Read a screenshot image file from disk and return as a data:image/jpeg;base64,... string."""
        if not image_path:
            return None
        clean_path = os.path.normpath(image_path)
        if not os.path.exists(clean_path):
            return None
        try:
            data = Path(clean_path).read_bytes()
            mime = "image/png" if clean_path.lower().endswith(".png") else "image/jpeg"
            logger.debug(
                "Loaded full-resolution history image %s: %d bytes (%.1f KB)",
                clean_path,
                len(data),
                len(data) / 1024,
            )
            return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        except Exception as exc:
            logger.debug("Failed reading image %s as base64: %s", clean_path, exc)
            return None

    def _save(self) -> None:
        try:
            with self._lock:
                to_save = list(self._entries[:self._max_entries])
            self._history_file.write_text(json.dumps(to_save, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save history: %s", exc)

    def _load_and_heal(self) -> list[dict]:
        """Load history from history.json and regenerate any missing thumbnails from disk."""
        if not self._history_file.exists():
            return []

        try:
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []

            history = data[:self._max_entries]
            needs_save = False

            for entry in history:
                if not entry.get("thumbnail") and entry.get("image_path"):
                    clean_p = os.path.normpath(entry["image_path"])
                    if os.path.exists(clean_p):
                        try:
                            with Image.open(clean_p) as img:
                                img.thumbnail((360, 220))
                                buf = io.BytesIO()
                                img.save(buf, format="JPEG", quality=85)
                                entry["thumbnail"] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                                needs_save = True
                        except Exception as exc:
                            logger.debug("Failed to build thumbnail for %s: %s", clean_p, exc)

            if needs_save:
                try:
                    self._history_file.write_text(json.dumps(history, indent=2), encoding="utf-8")
                    logger.info("Regenerated missing history thumbnails in history.json.")
                except Exception as exc:
                    logger.debug("Failed saving history after thumbnail regeneration: %s", exc)

            return history

        except Exception as exc:
            logger.warning("Failed to load history from %s: %s", self._history_file, exc)
            return []
