"""Query history repository and screenshot asset manager."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import statistics
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)


class HistoryRepository:
    """Thread-safe repository managing query history records and disk-persisted screenshots."""

    def __init__(self, history_file: Path, history_dir: Path, max_entries: int = 500) -> None:
        self._history_file = history_file
        self._history_dir = history_dir
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._cached_stats: dict[str, Any] | None = None
        self._entries: list[dict] = self._load_and_heal()

    def get_all(self) -> list[dict]:
        with self._lock:
            return list(self._entries)

    def add(self, entry: dict) -> None:
        """Insert a new history entry at the front, prune to max entries, and persist."""
        with self._lock:
            self._cached_stats = None
            self._entries.insert(0, entry)
            if len(self._entries) > self._max_entries:
                self._entries.pop()
            self._save()

    def clear(self) -> None:
        """Clear all in-memory history entries and update history.json."""
        with self._lock:
            self._cached_stats = None
            self._entries.clear()
            self._save()
        logger.info("Query history cleared.")

    def delete_item(self, item_id: Any) -> bool:
        """Delete a single query from history by ID, timestamp, or path, and remove its screenshot."""
        with self._lock:
            self._cached_stats = None
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

    def get_response_time_stats(self) -> dict[str, Any]:
        """Compute average and median response times broken down by model and time period.

        Uses lazy memoization: returns cached results until query history changes.
        Periods computed: today, week (7 days), month (30 days), all time.
        """
        with self._lock:
            if self._cached_stats is not None:
                return dict(self._cached_stats)

            now = time.time()
            today_str = str(date.today())

            periods: dict[str, list[dict[str, Any]]] = {
                "today": [],
                "week": [],
                "month": [],
                "all": [],
            }

            for entry in self._entries:
                dur_ms = self._parse_duration_ms(entry)
                if dur_ms is None or dur_ms <= 0:
                    continue

                ts = self._parse_timestamp(entry)
                entry_date = entry.get("date") or ""

                model = str(entry.get("model") or "Unknown").strip()
                item = {
                    "model": model,
                    "duration_ms": dur_ms,
                    "status": entry.get("status", "success"),
                }

                # All time
                periods["all"].append(item)

                # Today
                if entry_date == today_str or (now - ts < 86400 and time.strftime("%Y-%m-%d", time.localtime(ts)) == today_str):
                    periods["today"].append(item)

                # Week (last 7 days)
                if now - ts <= 7 * 86400:
                    periods["week"].append(item)

                # Month (last 30 days)
                if now - ts <= 30 * 86400:
                    periods["month"].append(item)

            result: dict[str, Any] = {}
            for p_key, items in periods.items():
                result[p_key] = self._calculate_period_stats(items)

            result["generated_at"] = time.strftime("%H:%M:%S")
            self._cached_stats = result
            return dict(result)

    @classmethod
    def _format_ms(cls, ms: float) -> str:
        if ms <= 0:
            return "0ms"
        if ms >= 1000.0:
            return f"{ms / 1000.0:.2f}s"
        return f"{int(round(ms))}ms"

    @classmethod
    def _compute_metrics(cls, durations: list[float]) -> dict[str, Any]:
        if not durations:
            return {
                "count": 0,
                "avg_ms": 0.0,
                "median_ms": 0.0,
                "min_ms": 0.0,
                "max_ms": 0.0,
                "avg_formatted": "—",
                "median_formatted": "—",
                "min_formatted": "—",
                "max_formatted": "—",
            }

        avg_val = round(statistics.mean(durations), 1)
        med_val = round(statistics.median(durations), 1)
        min_val = round(min(durations), 1)
        max_val = round(max(durations), 1)

        return {
            "count": len(durations),
            "avg_ms": avg_val,
            "median_ms": med_val,
            "min_ms": min_val,
            "max_ms": max_val,
            "avg_formatted": cls._format_ms(avg_val),
            "median_formatted": cls._format_ms(med_val),
            "min_formatted": cls._format_ms(min_val),
            "max_formatted": cls._format_ms(max_val),
        }

    @classmethod
    def _calculate_period_stats(cls, items: list[dict[str, Any]]) -> dict[str, Any]:
        all_durations = [it["duration_ms"] for it in items]
        summary = cls._compute_metrics(all_durations)

        by_model_map: dict[str, list[float]] = {}
        for it in items:
            m = it["model"]
            by_model_map.setdefault(m, []).append(it["duration_ms"])

        by_model: dict[str, Any] = {}
        sorted_models = sorted(by_model_map.keys(), key=lambda k: len(by_model_map[k]), reverse=True)
        for m in sorted_models:
            by_model[m] = cls._compute_metrics(by_model_map[m])

        return {
            "summary": summary,
            "by_model": by_model,
        }

    @staticmethod
    def _parse_duration_ms(entry: dict) -> float | None:
        if "duration_ms" in entry and isinstance(entry["duration_ms"], (int, float)):
            return float(entry["duration_ms"])
        raw = str(entry.get("duration", "")).strip().lower()
        if not raw:
            return None
        try:
            if raw.endswith("ms"):
                return float(raw[:-2])
            if raw.endswith("s"):
                return float(raw[:-1]) * 1000.0
            return float(raw)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_timestamp(entry: dict) -> float:
        if "timestamp" in entry and isinstance(entry["timestamp"], (int, float)):
            return float(entry["timestamp"])
        date_str = entry.get("date")
        if date_str:
            try:
                from datetime import datetime
                return datetime.fromisoformat(str(date_str)).timestamp()
            except Exception:
                pass
        return time.time()

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
