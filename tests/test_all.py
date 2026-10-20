"""Comprehensive unit test suite for Screenshot AI.

Covers all core subsystems, services, repositories, design patterns,
error classifications, resilience logic, preemption, and edge cases.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Ensure source/ is on sys.path
source_dir = Path(__file__).resolve().parent.parent / "source"
if str(source_dir) not in sys.path:
    sys.path.insert(0, str(source_dir))

import capture
import token_db
from capture import (
    ActiveWindowCaptureStrategy,
    AllScreensCaptureStrategy,
    CaptureStrategy,
    CursorMonitorCaptureStrategy,
    ScreenCaptureService,
)
from client_pool import ApiClientPool
from config import Config
from gemini import GeminiService, QuestionResult, _classify_error
from google.genai import errors as genai_errors
from history import HistoryRepository
from prompt_manager import PromptManager
from settings import AppSettings, SettingsManager
from trayicon import TrayIcon
from ui import AppCallbacks, AppControllerProtocol, UI


# =====================================================================
# 1. Configuration Subsystem Tests
# =====================================================================

class TestConfig(unittest.TestCase):
    """Test configuration path calculations and directory structures."""

    def test_config_paths_resolution(self):
        root = Path("/fake/custom_project_root")
        cfg = Config(project_root=root)
        self.assertEqual(cfg.api_key_file, root / "apikeys.txt")
        self.assertEqual(cfg.prompt_dir, root / "prompt_files")
        self.assertEqual(cfg.token_db_file, root / "token_usage.json")
        self.assertEqual(cfg.settings_file, root / "settings.json")
        self.assertEqual(cfg.history_file, root / "history.json")
        self.assertEqual(cfg.history_dir, root / "history_images")

    def test_default_config_root(self):
        cfg = Config(project_root=Path("."))
        self.assertTrue(cfg.project_root.exists())
        self.assertEqual(cfg.api_key_file.name, "apikeys.txt")


# =====================================================================
# 2. Token DB & Repository Subsystem Tests
# =====================================================================

class TestTokenDBFunctions(unittest.TestCase):
    """Test token_db module-level helper functions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "token_usage.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_non_existent_file(self):
        data = token_db.load_token_data(self.db_path)
        self.assertEqual(data, {"total": 0, "daily": {}, "keys": {}})

    def test_load_corrupted_json_file(self):
        self.db_path.write_text("{corrupt-json:::", encoding="utf-8")
        data = token_db.load_token_data(self.db_path)
        self.assertEqual(data, {"total": 0, "daily": {}, "keys": {}})

    def test_save_and_load_roundtrip(self):
        sample = {"total": 250, "daily": {"2026-10-20": 250}, "keys": {}}
        token_db.save_token_data(self.db_path, sample)
        loaded = token_db.load_token_data(self.db_path)
        self.assertEqual(loaded, sample)

    def test_update_token_data_increments(self):
        data = {"total": 100, "daily": {}, "keys": {}}
        updated = token_db.update_token_data(data, 75)
        self.assertEqual(updated["total"], 175)
        today = str(date.today())
        self.assertEqual(updated["daily"][today], 75)

    def test_update_token_data_with_api_key_stats(self):
        data = {"total": 100, "daily": {}, "keys": {}}
        updated = token_db.update_token_data(data, 60, key="KEY_ALPHA")
        self.assertEqual(updated["total"], 160)
        self.assertIn("KEY_ALPHA", updated["keys"])
        self.assertEqual(updated["keys"]["KEY_ALPHA"]["tokens"], 60)
        self.assertEqual(updated["keys"]["KEY_ALPHA"]["requests"], 1)
        self.assertTrue(bool(updated["keys"]["KEY_ALPHA"]["last_used"]))

        # Second update for same key
        updated2 = token_db.update_token_data(updated, 40, key="KEY_ALPHA")
        self.assertEqual(updated2["keys"]["KEY_ALPHA"]["tokens"], 100)
        self.assertEqual(updated2["keys"]["KEY_ALPHA"]["requests"], 2)

    def test_pruning_dates_older_than_30_days(self):
        old_day = str(date.today() - timedelta(days=35))
        recent_day = str(date.today() - timedelta(days=10))
        data = {"total": 200, "daily": {old_day: 100, recent_day: 100}, "keys": {}}
        updated = token_db.update_token_data(data, 50)
        self.assertNotIn(old_day, updated["daily"])
        self.assertIn(recent_day, updated["daily"])
        today = str(date.today())
        self.assertEqual(updated["daily"][today], 50)


class TestTokenRepository(unittest.TestCase):
    """Test TokenRepository thread-safe persistence and stats tracking."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "token_usage.json"
        self.repo = token_db.TokenRepository(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initial_state(self):
        self.assertEqual(self.repo.get_total(), 0)
        self.assertEqual(self.repo.get_today(), 0)
        self.assertEqual(self.repo.get_key_stats(), {})

    def test_record_usage_and_persistence(self):
        total, today = self.repo.record_usage(120, key="KEY_BETA")
        self.assertEqual(total, 120)
        self.assertEqual(today, 120)
        self.assertEqual(self.repo.get_total(), 120)
        self.assertEqual(self.repo.get_today(), 120)

        stats = self.repo.get_key_stats()
        self.assertIn("KEY_BETA", stats)
        self.assertEqual(stats["KEY_BETA"]["tokens"], 120)
        self.assertEqual(stats["KEY_BETA"]["requests"], 1)

        # Reload from disk into a separate repository instance
        reloaded = token_db.TokenRepository(self.db_path)
        self.assertEqual(reloaded.get_total(), 120)
        self.assertEqual(reloaded.get_today(), 120)

    def test_concurrent_usage_recording(self):
        threads = []

        def worker():
            for _ in range(10):
                self.repo.record_usage(5, key="THREAD_KEY")

        for _ in range(5):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # 5 threads * 10 iterations * 5 tokens = 250 tokens
        self.assertEqual(self.repo.get_total(), 250)
        self.assertEqual(self.repo.get_key_stats()["THREAD_KEY"]["requests"], 50)


# =====================================================================
# 3. Client Pool & Key Management Subsystem Tests
# =====================================================================

class TestClientPool(unittest.TestCase):
    """Test ApiClientPool Object Pool and Factory implementations."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.key_file = self.root / "apikeys.txt"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_file_raises_filenotfound(self):
        with self.assertRaises(FileNotFoundError):
            ApiClientPool(self.root / "nonexistent.txt")

    def test_empty_file_raises_valueerror(self):
        self.key_file.write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            ApiClientPool(self.key_file)

    def test_comments_only_file_raises_valueerror(self):
        self.key_file.write_text("# comment 1\n# last_index=0\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            ApiClientPool(self.key_file)

    @patch("client_pool.genai.Client")
    def test_round_robin_rotation(self, mock_client_cls):
        self.key_file.write_text("# last_index=0\nKEY1\nKEY2\nKEY3\n", encoding="utf-8")
        pool = ApiClientPool(self.key_file)
        self.assertEqual(pool.get_keys(), ["KEY1", "KEY2", "KEY3"])

        # First rotation advances from index 0 to index 1
        client1, key1, idx1 = pool.get_active_client()
        self.assertEqual(key1, "KEY2")
        self.assertEqual(idx1, 1)

        # Second rotation advances to index 2
        client2, key2, idx2 = pool.get_active_client()
        self.assertEqual(key2, "KEY3")
        self.assertEqual(idx2, 2)

        # Third rotation wraps around to index 0
        client3, key3, idx3 = pool.get_active_client()
        self.assertEqual(key3, "KEY1")
        self.assertEqual(idx3, 0)

        # Verifies index was persisted to disk
        content = self.key_file.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# last_index=0"))

    @patch("client_pool.genai.Client")
    def test_add_and_remove_keys(self, mock_client_cls):
        self.key_file.write_text("INITIAL_KEY_1\nINITIAL_KEY_2\n", encoding="utf-8")
        pool = ApiClientPool(self.key_file)

        # Add valid new key
        self.assertTrue(pool.add_key("NEW_KEY_A"))
        self.assertIn("NEW_KEY_A", pool.get_keys())
        self.assertEqual(len(pool.get_keys()), 3)

        # Add duplicate rejected
        self.assertFalse(pool.add_key("NEW_KEY_A"))

        # Add empty or comment rejected
        self.assertFalse(pool.add_key("   "))
        self.assertFalse(pool.add_key("# commented"))

        # Remove key
        self.assertTrue(pool.remove_key("INITIAL_KEY_2"))
        self.assertNotIn("INITIAL_KEY_2", pool.get_keys())

        # Remove non-existent key returns False
        self.assertFalse(pool.remove_key("DOES_NOT_EXIST"))

        # Safety: refuse to remove the last remaining key
        self.assertTrue(pool.remove_key("NEW_KEY_A"))
        self.assertEqual(len(pool.get_keys()), 1)
        self.assertFalse(pool.remove_key("INITIAL_KEY_1"))
        self.assertEqual(len(pool.get_keys()), 1)


# =====================================================================
# 4. Prompt Manager Subsystem Tests
# =====================================================================

class TestPromptManager(unittest.TestCase):
    """Test PromptManager preset CRUD and naming normalization."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.prompt_dir = Path(self.temp_dir.name) / "prompt_files"
        self.prompt_dir.mkdir()
        self.mgr = PromptManager(self.prompt_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_resolution_hierarchy(self):
        # 1. When only default_prompt.txt exists
        (self.prompt_dir / "default_prompt.txt").write_text("Default", encoding="utf-8")
        self.assertEqual(self.mgr.resolve_default_prompt(), "default_prompt.txt")

        # 2. When default_prompt_ultra.txt exists, it takes precedence as default
        (self.prompt_dir / "default_prompt_ultra.txt").write_text("Ultra", encoding="utf-8")
        self.assertEqual(self.mgr.resolve_default_prompt(), "default_prompt_ultra.txt")

        # 3. When a specific preferred prompt is requested and exists
        (self.prompt_dir / "custom.txt").write_text("Custom", encoding="utf-8")
        self.assertEqual(self.mgr.resolve_default_prompt("custom"), "custom.txt")
        self.assertEqual(self.mgr.resolve_default_prompt("custom.txt"), "custom.txt")

        # 4. Non-existent preferred falls back to ultra
        self.assertEqual(self.mgr.resolve_default_prompt("missing_prompt"), "default_prompt_ultra.txt")

    def test_load_and_validate_prompt(self):
        (self.prompt_dir / "valid.txt").write_text("   Answer correctly   ", encoding="utf-8")
        content = self.mgr.load_prompt("valid.txt")
        self.assertEqual(content, "Answer correctly")

        # Missing file
        with self.assertRaises(FileNotFoundError):
            self.mgr.load_prompt("nonexistent.txt")

        # Empty file
        (self.prompt_dir / "empty.txt").write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.mgr.load_prompt("empty.txt")

        # Whitespace-only file
        (self.prompt_dir / "whitespace.txt").write_text("   \n\t  ", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.mgr.load_prompt("whitespace.txt")

    def test_save_and_get_content(self):
        self.assertTrue(self.mgr.save_prompt("coding", "Write clean Python code."))
        self.assertEqual(self.mgr.get_content("coding.txt"), "Write clean Python code.")
        self.assertEqual(self.mgr.get_content("coding"), "Write clean Python code.")
        self.assertEqual(self.mgr.get_content("missing.txt"), "")

        # Empty filename rejected
        self.assertFalse(self.mgr.save_prompt("", "Content"))

    def test_list_prompts_filters_non_txt(self):
        (self.prompt_dir / "b_prompt.txt").write_text("B", encoding="utf-8")
        (self.prompt_dir / "a_prompt.txt").write_text("A", encoding="utf-8")
        (self.prompt_dir / "notes.md").write_text("Markdown", encoding="utf-8")
        (self.prompt_dir / "backup.bak").write_text("Backup", encoding="utf-8")

        prompts = self.mgr.list_prompts()
        self.assertEqual(prompts, ["a_prompt.txt", "b_prompt.txt"])


# =====================================================================
# 5. Settings Manager Subsystem Tests
# =====================================================================

class TestSettingsManager(unittest.TestCase):
    """Test AppSettings dataclass and SettingsManager persistence."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_file = Path(self.temp_dir.name) / "settings.json"
        self.mgr = SettingsManager(self.settings_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_app_settings_defaults_and_filtering(self):
        settings = AppSettings()
        self.assertEqual(settings.hotkey, "ctrl+shift+q")
        self.assertEqual(settings.capture_mode, "cursor_monitor")
        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(settings.pdf_sources, [])

        # from_dict ignores unknown/deprecated properties
        data = {"capture_mode": "active_window", "unknown_old_prop": 12345}
        filtered = AppSettings.from_dict(data)
        self.assertEqual(filtered.capture_mode, "active_window")
        self.assertFalse(hasattr(filtered, "unknown_old_prop"))

    def test_settings_persistence_and_reloading(self):
        self.mgr.update(
            capture_mode="all_screens",
            hotkey="ctrl+shift+s",
            log_level="DEBUG",
            pdf_sources=["doc1.pdf", "doc2.pdf"],
        )
        self.assertTrue(self.settings_file.exists())

        # Load into new manager
        mgr2 = SettingsManager(self.settings_file)
        self.assertEqual(mgr2.settings.capture_mode, "all_screens")
        self.assertEqual(mgr2.settings.hotkey, "ctrl+shift+s")
        self.assertEqual(mgr2.settings.log_level, "DEBUG")
        self.assertEqual(mgr2.settings.pdf_sources, ["doc1.pdf", "doc2.pdf"])

    def test_corrupted_settings_recovers_to_defaults(self):
        self.settings_file.write_text("{corrupt-json", encoding="utf-8")
        mgr = SettingsManager(self.settings_file)
        self.assertEqual(mgr.settings.hotkey, "ctrl+shift+q")
        self.assertEqual(mgr.settings.capture_mode, "cursor_monitor")


# =====================================================================
# 6. History Repository Subsystem Tests
# =====================================================================

class TestHistoryRepository(unittest.TestCase):
    """Test HistoryRepository CRUD, thumbnail generation, and disk asset cleanup."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.hist_file = self.root / "history.json"
        self.hist_dir = self.root / "history_images"
        self.repo = HistoryRepository(self.hist_file, self.hist_dir, max_entries=5)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_persist_screenshot_generates_valid_image_and_thumbnail(self):
        from PIL import Image

        img = Image.new("RGB", (300, 300), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        img_path, thumb_b64 = self.repo.persist_screenshot(101, raw_bytes)
        self.assertIsNotNone(img_path)
        self.assertTrue(Path(img_path).exists())
        self.assertIsNotNone(thumb_b64)
        self.assertTrue(thumb_b64.startswith("data:image/jpeg;base64,"))

    def test_history_entry_capping_max_entries(self):
        for i in range(10):
            entry = {"id": i, "time": f"10:0{i}:00", "answer": f"Answer {i}"}
            self.repo.add(entry)

        all_entries = self.repo.get_all()
        self.assertEqual(len(all_entries), 5)
        # Newest entry is at index 0
        self.assertEqual(all_entries[0]["id"], 9)
        self.assertEqual(all_entries[-1]["id"], 5)

    def test_delete_history_item_removes_file_from_disk(self):
        dummy_file = self.hist_dir / "screenshot_test.jpg"
        self.hist_dir.mkdir(parents=True, exist_ok=True)
        dummy_file.write_bytes(b"dummy image data")

        entry = {
            "id": 42,
            "time": "12:30:00",
            "image_path": str(dummy_file),
            "answer": "Test",
        }
        self.repo.add(entry)
        self.assertEqual(len(self.repo.get_all()), 1)
        self.assertTrue(dummy_file.exists())

        # Delete by ID
        self.assertTrue(self.repo.delete_item(42))
        self.assertEqual(len(self.repo.get_all()), 0)
        self.assertFalse(dummy_file.exists())

        # Non-existent item
        self.assertFalse(self.repo.delete_item(9999))

    def test_clear_history(self):
        self.repo.add({"id": 1, "answer": "A1"})
        self.repo.add({"id": 2, "answer": "A2"})
        self.assertEqual(len(self.repo.get_all()), 2)

        self.repo.clear()
        self.assertEqual(len(self.repo.get_all()), 0)

        # Reload from disk
        reloaded = HistoryRepository(self.hist_file, self.hist_dir)
        self.assertEqual(len(reloaded.get_all()), 0)

    def test_get_image_base64_mime_types(self):
        self.hist_dir.mkdir(parents=True, exist_ok=True)
        jpg_file = self.hist_dir / "shot.jpg"
        jpg_file.write_bytes(b"\xff\xd8\xff\xe0dummy")
        png_file = self.hist_dir / "shot.png"
        png_file.write_bytes(b"\x89PNG\r\n\x1a\ndummy")

        b64_jpg = self.repo.get_image_base64(str(jpg_file))
        self.assertIsNotNone(b64_jpg)
        self.assertTrue(b64_jpg.startswith("data:image/jpeg;base64,"))

        b64_png = self.repo.get_image_base64(str(png_file))
        self.assertIsNotNone(b64_png)
        self.assertTrue(b64_png.startswith("data:image/png;base64,"))

        # Missing or empty path returns None
        self.assertIsNone(self.repo.get_image_base64(str(self.root / "nonexistent.jpg")))
        self.assertIsNone(self.repo.get_image_base64(""))

    def test_self_healing_missing_thumbnail(self):
        from PIL import Image

        self.hist_dir.mkdir(parents=True, exist_ok=True)
        img_path = self.hist_dir / "healing_target.jpg"
        Image.new("RGB", (200, 200), color="green").save(img_path, format="JPEG")

        # Save history without thumbnail
        corrupt_history = [
            {"id": 1, "time": "11:00:00", "image_path": str(img_path), "thumbnail": None}
        ]
        self.hist_file.write_text(json.dumps(corrupt_history), encoding="utf-8")

        # Load repository -> triggers _load_and_heal
        healed_repo = HistoryRepository(self.hist_file, self.hist_dir)
        entries = healed_repo.get_all()
        self.assertEqual(len(entries), 1)
        self.assertIsNotNone(entries[0]["thumbnail"])
        self.assertTrue(entries[0]["thumbnail"].startswith("data:image/jpeg;base64,"))

    def test_get_response_time_stats_lazy_memoization(self):
        # Empty repo stats
        empty_stats = self.repo.get_response_time_stats()
        self.assertIn("today", empty_stats)
        self.assertIn("week", empty_stats)
        self.assertIn("month", empty_stats)
        self.assertIn("all", empty_stats)
        self.assertEqual(empty_stats["today"]["summary"]["count"], 0)
        self.assertEqual(empty_stats["today"]["summary"]["avg_formatted"], "—")

        # Add sample entries across different time windows and models
        now = time.time()
        today_date = str(date.today())

        # Today: 2 queries for gemini-3.8-flash (1000ms, 3000ms -> avg=2000ms, med=2000ms)
        self.repo.add({
            "id": 1,
            "timestamp": now - 100,
            "date": today_date,
            "duration_ms": 1000.0,
            "model": "gemini-3.8-flash",
            "status": "success",
        })
        self.repo.add({
            "id": 2,
            "timestamp": now - 50,
            "date": today_date,
            "duration_ms": 3000.0,
            "model": "gemini-3.8-flash",
            "status": "success",
        })

        # Earlier this week (3 days ago): 1 query for gemini-2.5-flash (4000ms)
        self.repo.add({
            "id": 3,
            "timestamp": now - (3 * 86400),
            "date": "2026-10-17",
            "duration_ms": 4000.0,
            "model": "gemini-2.5-flash",
            "status": "success",
        })

        # Earlier this month (20 days ago): 1 query for gemini-2.5-flash (2000ms)
        self.repo.add({
            "id": 4,
            "timestamp": now - (20 * 86400),
            "date": "2026-09-30",
            "duration_ms": 2000.0,
            "model": "gemini-2.5-flash",
            "status": "success",
        })

        stats = self.repo.get_response_time_stats()

        # Check Today: 2 queries, avg=2000ms, median=2000ms
        self.assertEqual(stats["today"]["summary"]["count"], 2)
        self.assertEqual(stats["today"]["summary"]["avg_ms"], 2000.0)
        self.assertEqual(stats["today"]["summary"]["median_ms"], 2000.0)
        self.assertEqual(stats["today"]["summary"]["avg_formatted"], "2.00s")
        self.assertIn("gemini-3.8-flash", stats["today"]["by_model"])

        # Check Week: 3 queries (id 1, 2, 3), total ms = 1000 + 3000 + 4000 = 8000 -> avg = 2666.7, med = 3000
        self.assertEqual(stats["week"]["summary"]["count"], 3)
        self.assertAlmostEqual(stats["week"]["summary"]["avg_ms"], 2666.7, places=1)
        self.assertEqual(stats["week"]["summary"]["median_ms"], 3000.0)

        # Check All Time: 4 queries
        self.assertEqual(stats["all"]["summary"]["count"], 4)
        self.assertIn("gemini-3.8-flash", stats["all"]["by_model"])
        self.assertIn("gemini-2.5-flash", stats["all"]["by_model"])

        # Verify Lazy Memoization: subsequent call returns cached dict without recomputing
        stats_cached = self.repo.get_response_time_stats()
        self.assertIs(stats["today"], stats_cached["today"])

        # Invalidation: adding new entry clears cache
        self.repo.add({
            "id": 5,
            "timestamp": now,
            "date": today_date,
            "duration_ms": 500.0,
            "model": "gemini-3.8-flash",
            "status": "success",
        })
        self.assertIsNone(self.repo._cached_stats)
        recalculated = self.repo.get_response_time_stats()
        self.assertEqual(recalculated["today"]["summary"]["count"], 3)

    def test_response_times_string_duration_parsing(self):
        # Legacy entry with duration string like "1.25s" or "450ms"
        now = time.time()
        self.repo.add({
            "id": 10,
            "timestamp": now,
            "date": str(date.today()),
            "duration": "1.50s",
            "model": "legacy-model",
        })
        self.repo.add({
            "id": 11,
            "timestamp": now,
            "date": str(date.today()),
            "duration": "500ms",
            "model": "legacy-model",
        })

        stats = self.repo.get_response_time_stats()
        self.assertEqual(stats["today"]["summary"]["count"], 2)
        # 1500ms + 500ms = 2000ms / 2 = 1000ms
        self.assertEqual(stats["today"]["summary"]["avg_ms"], 1000.0)
        self.assertEqual(stats["today"]["summary"]["median_ms"], 1000.0)


# =====================================================================
# 7. Screen Capture Subsystem Tests (Strategy Pattern)
# =====================================================================

class TestCaptureStrategySubsystem(unittest.TestCase):
    """Test ScreenCaptureService and Strategy pattern implementations."""

    def test_protocol_conformance(self):
        self.assertTrue(isinstance(CursorMonitorCaptureStrategy(), CaptureStrategy))
        self.assertTrue(isinstance(ActiveWindowCaptureStrategy(), CaptureStrategy))
        self.assertTrue(isinstance(AllScreensCaptureStrategy(), CaptureStrategy))

    @patch("capture.pyautogui.position")
    @patch("capture.pyautogui.screenshot")
    def test_cursor_monitor_strategy_multi_monitors(self, mock_screenshot, mock_pos):
        from PIL import Image

        # Cursor at (2500, 400) -> on monitor 2
        mock_pos.return_value = (2500, 400)

        fake_monitors = [
            {"left": 0, "top": 0, "width": 3840, "height": 1080},  # Monitor 0 (all)
            {"left": 0, "top": 0, "width": 1920, "height": 1080},  # Monitor 1
            {"left": 1920, "top": 0, "width": 1920, "height": 1080},  # Monitor 2
        ]

        class FakeMSS:
            monitors = fake_monitors
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def grab(self, box):
                return MagicMock(size=(1920, 1080), bgra=b"\x00" * (1920 * 1080 * 4))

        with patch("mss.MSS", FakeMSS):
            strat = CursorMonitorCaptureStrategy()
            img = strat.capture()
            self.assertEqual(img.size, (1920, 1080))

    @patch("capture.pyautogui.screenshot")
    def test_capture_strategy_mss_failure_fallback_to_pyautogui(self, mock_screenshot):
        from PIL import Image

        mock_screenshot.return_value = Image.new("RGB", (50, 50), color="red")

        class FailingMSS:
            monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]
            def __enter__(self): raise RuntimeError("MSS initialization failed")
            def __exit__(self, *args): pass

        with patch("mss.MSS", FailingMSS):
            img = CursorMonitorCaptureStrategy().capture()
            self.assertEqual(img.size, (50, 50))
            mock_screenshot.assert_called_once()

    @patch("capture.sys.platform", "win32")
    def test_active_window_strategy_clamping(self):
        from PIL import Image

        fake_rect = (-8, -8, 1928, 1088)
        fake_monitors = [
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
        ]

        class FakeMSS:
            monitors = fake_monitors
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def grab(self, box):
                return MagicMock(size=(box["width"], box["height"]), bgra=b"\x00" * (box["width"] * box["height"] * 4))

        with patch("win32gui.GetForegroundWindow", return_value=1234), \
             patch("win32gui.GetWindowRect", return_value=fake_rect), \
             patch("win32gui.GetWindowText", return_value="Test Window"), \
             patch("mss.MSS", FakeMSS):
            strat = ActiveWindowCaptureStrategy()
            img = strat.capture()
            self.assertEqual(img.size, (1920, 1080))

    def test_all_screens_strategy(self):
        fake_monitors = [
            {"left": 0, "top": 0, "width": 3840, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            {"left": 1920, "top": 0, "width": 1920, "height": 1080},
        ]
        class FakeMSS:
            monitors = fake_monitors
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def grab(self, box):
                return MagicMock(size=(box["width"], box["height"]), bgra=b"\x00" * (box["width"] * box["height"] * 4))

        with patch("mss.MSS", FakeMSS):
            img = AllScreensCaptureStrategy().capture()
            self.assertEqual(img.size, (3840, 1080))

    def test_screen_capture_service_custom_registration(self):
        from PIL import Image

        class CustomStrategy:
            def capture(self) -> Image.Image:
                return Image.new("RGB", (40, 40), color="yellow")

        ScreenCaptureService.register_strategy("custom_mode", CustomStrategy())
        raw_bytes, mime = ScreenCaptureService.capture(mode="custom_mode")
        self.assertEqual(mime, "image/jpeg")
        # Validate JPEG magic bytes: 0xFF, 0xD8, 0xFF
        self.assertTrue(raw_bytes.startswith(b"\xff\xd8\xff"))


# =====================================================================
# 8. Gemini API Error Classification & Discovery Tests
# =====================================================================

class TestGeminiErrorClassification(unittest.TestCase):
    """Test Gemini error classification helper."""

    def test_classify_503(self):
        err = genai_errors.APIError(503, {"error": {"message": "High demand"}})
        code, msg = _classify_error(err)
        self.assertEqual(code, "503")
        self.assertIn("busy", msg.lower())

    def test_classify_429(self):
        err = genai_errors.APIError(429, {"error": {"message": "Resource exhausted"}})
        code, msg = _classify_error(err)
        self.assertEqual(code, "429")
        self.assertIn("rate limit", msg.lower())

    def test_classify_400(self):
        err = genai_errors.APIError(400, {"error": {"message": "Bad request"}})
        code, msg = _classify_error(err)
        self.assertEqual(code, "400")
        self.assertIn("bad request", msg.lower())

    def test_classify_403(self):
        err = genai_errors.APIError(403, {"error": {"message": "Permission denied"}})
        code, msg = _classify_error(err)
        self.assertEqual(code, "AUTH")
        self.assertIn("invalid api key", msg.lower())

    def test_classify_404(self):
        err = genai_errors.APIError(404, {"error": {"message": "Not found"}})
        code, msg = _classify_error(err)
        self.assertEqual(code, "404")
        self.assertIn("not found", msg.lower())

    def test_classify_network_errors(self):
        err1 = ConnectionResetError("Connection reset by peer")
        code1, _ = _classify_error(err1)
        self.assertEqual(code1, "NET")

        err2 = ConnectionAbortedError("WinError 10053 connection aborted")
        code2, _ = _classify_error(err2)
        self.assertEqual(code2, "NET")

    def test_classify_generic_error(self):
        err = RuntimeError("Unexpected internal crash")
        code, msg = _classify_error(err)
        self.assertEqual(code, "ERR")
        self.assertIn("Unexpected internal crash", msg)


class TestGeminiModelDiscovery(unittest.TestCase):
    """Test model listing and newest flash model selection."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.key_file = self.root / "apikeys.txt"
        self.key_file.write_text("AIzaSyFakeKey\n", encoding="utf-8")
        (self.root / "prompt_files").mkdir()
        (self.root / "prompt_files" / "default_prompt.txt").write_text("Prompt", encoding="utf-8")
        self.cfg = Config(project_root=self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("client_pool.genai.Client")
    def test_list_models_filtering_and_caching(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        mock_client = MagicMock()

        m1 = MagicMock(name="models/gemini-2.5-flash", supported_actions=["generateContent"])
        m1.name = "models/gemini-2.5-flash"
        m2 = MagicMock(name="models/text-embedding-004", supported_actions=["embedContent"])
        m2.name = "models/text-embedding-004"
        m3 = MagicMock(name="models/gemini-3.8-flash", supported_actions=["generateContent"])
        m3.name = "models/gemini-3.8-flash"

        mock_client.models.list.return_value = [m1, m2, m3]
        service._client_pool._clients["AIzaSyFakeKey"] = mock_client

        models = service.list_models()
        self.assertEqual(models, ["models/gemini-2.5-flash", "models/gemini-3.8-flash"])

        # Second call returns cached list without calling API again
        mock_client.models.list.reset_mock()
        cached = service.list_models()
        self.assertEqual(cached, ["models/gemini-2.5-flash", "models/gemini-3.8-flash"])
        mock_client.models.list.assert_not_called()

    @patch("client_pool.genai.Client")
    def test_select_newest_flash_model(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        service.list_models = MagicMock(
            return_value=[
                "models/gemini-1.5-flash",
                "models/gemini-2.5-flash",
                "models/gemini-3.8-flash",
                "models/gemini-pro",
            ]
        )

        selected = service.select_newest_flash_model()
        self.assertEqual(selected, "models/gemini-3.8-flash")

        # When no flash model exists, returns fallback
        service.list_models = MagicMock(return_value=["models/gemini-pro"])
        fallback = service.select_newest_flash_model()
        self.assertEqual(fallback, "models/gemini-2.5-flash")


# =====================================================================
# 9. Gemini PDF Caching & Upload Tests
# =====================================================================

class TestGeminiPDFHandling(unittest.TestCase):
    """Test PDF upload caching and URL fetching."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.key_file = self.root / "apikeys.txt"
        self.key_file.write_text("AIzaSyFakeKey\n", encoding="utf-8")
        (self.root / "prompt_files").mkdir()
        (self.root / "prompt_files" / "default_prompt.txt").write_text("Prompt", encoding="utf-8")
        self.cfg = Config(project_root=self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("client_pool.genai.Client")
    def test_local_pdf_upload_and_cache(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        mock_client = MagicMock()
        mock_upload = MagicMock(uri="https://gemini/file/pdf123")
        mock_client.files.upload.return_value = mock_upload

        fake_pdf = self.root / "doc.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 test content")

        # First call uploads
        part1 = service._get_or_upload_pdf(mock_client, str(fake_pdf))
        self.assertEqual(part1, mock_upload)
        self.assertEqual(mock_client.files.upload.call_count, 1)

        # Second call returns from cache
        part2 = service._get_or_upload_pdf(mock_client, str(fake_pdf))
        self.assertEqual(part2, mock_upload)
        self.assertEqual(mock_client.files.upload.call_count, 1)

        # Invalidate cache -> re-uploads
        service.invalidate_pdf_cache()
        part3 = service._get_or_upload_pdf(mock_client, str(fake_pdf))
        self.assertEqual(mock_client.files.upload.call_count, 2)

    @patch("requests.get")
    @patch("client_pool.genai.Client")
    def test_remote_url_pdf_download_and_upload(self, mock_client_cls, mock_get):
        service = GeminiService(config=self.cfg)
        mock_client = MagicMock()
        mock_upload = MagicMock(uri="https://gemini/file/remote")
        mock_client.files.upload.return_value = mock_upload

        mock_resp = MagicMock()
        mock_resp.iter_content = lambda chunk_size: [b"%PDF-1.4 remote data"]
        mock_resp.raw = io.BytesIO(b"%PDF-1.4 remote data")
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        part = service._get_or_upload_pdf(mock_client, "https://example.com/sample.pdf")
        self.assertEqual(part, mock_upload)
        mock_get.assert_called_once_with("https://example.com/sample.pdf", stream=True, timeout=30)
        mock_client.files.upload.assert_called_once()


# =====================================================================
# 10. Gemini Streaming & Resilient Failover Tests
# =====================================================================

class TestGeminiStreamingAndResilience(unittest.TestCase):
    """Test resilient API calling, retries, and streaming callbacks."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.key_file = self.root / "apikeys.txt"
        self.key_file.write_text("AIzaSyFakeKey\n", encoding="utf-8")
        (self.root / "prompt_files").mkdir()
        (self.root / "prompt_files" / "default_prompt.txt").write_text("Prompt", encoding="utf-8")
        self.cfg = Config(project_root=self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("client_pool.genai.Client")
    def test_streaming_success_first_attempt(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        mock_client = MagicMock()
        chunk1 = MagicMock(text="Alpha ", usage_metadata=None)
        chunk2 = MagicMock(text="Beta", usage_metadata=MagicMock(total_token_count=150))
        mock_client.models.generate_content_stream.return_value = [chunk1, chunk2]

        received = []
        ans, tokens, err_c, err_m, model_used = service._call_api_resilient(
            client=mock_client,
            contents=["test"],
            model="models/gemini-2.5-flash",
            on_stream_chunk=lambda t: received.append(t),
        )
        self.assertEqual(ans, "Alpha Beta")
        self.assertEqual(tokens, 150)
        self.assertIsNone(err_c)
        self.assertEqual(model_used, "models/gemini-2.5-flash")
        self.assertEqual(received, ["Alpha", "Alpha Beta"])

    @patch("client_pool.genai.Client")
    def test_streaming_fallback_failover_on_503(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        mock_client = MagicMock()
        err_503 = genai_errors.APIError(503, {"error": {"message": "High demand"}})
        fallback_chunk = MagicMock(text="Fallback Answer", usage_metadata=MagicMock(total_token_count=90))

        # Model 3.8 attempts 1 and 2 fail, fallback model 2.5 succeeds
        mock_client.models.generate_content_stream.side_effect = [
            err_503,
            err_503,
            [fallback_chunk],
        ]

        ans, tokens, err_c, err_m, model_used = service._call_api_resilient(
            client=mock_client,
            contents=["test"],
            model="models/gemini-3.8-flash",
        )
        self.assertEqual(ans, "Fallback Answer")
        self.assertEqual(tokens, 90)
        self.assertIsNone(err_c)
        self.assertEqual(model_used, "models/gemini-2.5-flash")

    @patch("client_pool.genai.Client")
    def test_stream_cancellation_midway(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        mock_client = MagicMock()

        chunk1 = MagicMock(text="Hello", usage_metadata=None)
        chunk2 = MagicMock(text=" World", usage_metadata=None)

        cancelled = [False]

        def chunk_generator():
            yield chunk1
            cancelled[0] = True  # Cancel after chunk 1
            yield chunk2

        mock_client.models.generate_content_stream.return_value = chunk_generator()

        ans, tokens, err_c, err_m, model_used = service._call_api_resilient(
            client=mock_client,
            contents=["test"],
            model="models/gemini-2.5-flash",
            is_cancelled=lambda: cancelled[0],
        )

        self.assertEqual(err_c, "CNCL")
        self.assertIn("cancelled", err_m.lower())

    @patch("client_pool.genai.Client")
    def test_process_question_cancelled_before_call(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        result = service.process_question(
            image_bytes=b"fake_jpeg",
            mime_type="image/jpeg",
            pdf_sources=[],
            model="models/gemini-2.5-flash",
            prompt_file_name="default_prompt.txt",
            is_cancelled=lambda: True,
        )
        self.assertFalse(result.is_success)
        self.assertEqual(result.error_code, "CNCL")


# =====================================================================
# 11. System Tray Icon Tests
# =====================================================================

class TestTrayIcon(unittest.TestCase):
    """Test TrayIcon badge rendering and text shortening."""

    @patch("trayicon.pystray.Icon")
    @patch("trayicon.threading.Thread")
    def test_tray_icon_badge_formatting(self, mock_thread, mock_icon_cls):
        quit_cb = MagicMock()
        gui_cb = MagicMock()
        tray = TrayIcon(quit_callback=quit_cb, show_gui_callback=gui_cb)

        # 1. Short badge under 6 chars
        tray.display_answer("503")
        self.assertEqual(tray._icon.title, "503")

        # 2. Single word under 6 chars
        tray.display_answer("True")
        self.assertEqual(tray._icon.title, "True")

        # 3. Long title truncated to 120 chars
        long_text = "A" * 150
        tray.display_answer("Answer", title=long_text)
        self.assertEqual(len(tray._icon.title), 123)  # 120 + "..."

        # 4. Image creation produces 64x64 PIL Image
        img = tray._create_image("TEST", color="blue")
        self.assertEqual(img.size, (64, 64))

        tray.stop()


# =====================================================================
# 12. UI Bridge & JSBridge Protocol Tests
# =====================================================================

class TestUIBridge(unittest.TestCase):
    """Test UI Controller bridge contracts and JS exposed functions."""

    def test_app_callbacks_contract(self):
        cb = AppCallbacks(
            start_listening=lambda: True,
            stop_listening=lambda: None,
            get_pdf_sources=lambda: ["a.pdf"],
            set_pdf_sources=lambda s: None,
            get_available_models=lambda: ["m1"],
            get_selected_model=lambda: "m1",
            set_selected_model=lambda m: None,
            get_token_usage=lambda: {"total": 50},
            quit_app=lambda: None,
            toggle_ui_visibility=lambda: None,
            get_available_prompts=lambda: ["default_prompt.txt"],
            get_selected_prompt=lambda: "default_prompt.txt",
            set_selected_prompt=lambda p: None,
            get_prompt_content=lambda p: "Text",
            save_prompt=lambda p, c: True,
            get_capture_mode=lambda: "cursor_monitor",
            set_capture_mode=lambda m: True,
            get_hotkey=lambda: "ctrl+shift+q",
            set_hotkey=lambda k: True,
            get_history=lambda: [],
            clear_history=lambda: None,
            delete_history_item=lambda i: True,
            open_history_image=lambda p: True,
            get_history_image_base64=lambda p: None,
            get_api_keys_info=lambda: [],
            add_api_key=lambda k: True,
            remove_api_key=lambda k: True,
            get_log_level=lambda: "INFO",
            set_log_level=lambda l: True,
            get_response_time_stats=lambda: {"today": {}},
            get_initial_state=lambda: {"test": 1},
        )
        self.assertEqual(cb.get_pdf_sources(), ["a.pdf"])
        self.assertEqual(cb.get_token_usage(), {"total": 50})
        self.assertEqual(cb.get_response_time_stats(), {"today": {}})
        self.assertEqual(cb.get_initial_state(), {"test": 1})

    @patch("ui.webview.create_window")
    def test_ui_initialization_and_exposed_functions(self, mock_create_window):
        mock_win = MagicMock()
        mock_create_window.return_value = mock_win

        mock_controller = MagicMock(spec=AppControllerProtocol)
        mock_controller.get_initial_state.return_value = {"status": "ok"}
        mock_controller.start_listening.return_value = True

        log_q = queue.Queue()
        ui = UI(callbacks=mock_controller, log_queue=log_q, hidden=True, listening=False)

        mock_create_window.assert_called_once()
        exposed_calls = mock_win.expose.call_args[0]
        exposed_names = {f.__name__ for f in exposed_calls}

        self.assertIn("start_listening", exposed_names)
        self.assertIn("stop_listening", exposed_names)
        self.assertIn("set_selected_model", exposed_names)
        self.assertIn("get_initial_state", exposed_names)
        self.assertIn("save_prompt", exposed_names)
        self.assertIn("set_hotkey", exposed_names)
        self.assertIn("delete_history_item", exposed_names)
        self.assertIn("get_response_time_stats", exposed_names)


# =====================================================================
# 13. Application Facade & Worker Pipeline Tests
# =====================================================================

class TestApplicationComplete(unittest.TestCase):
    """Test Application facade coordinator, state changes, and question pipeline."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.key_file = self.root / "apikeys.txt"
        self.key_file.write_text("AIzaSyFakeKey1\nAIzaSyFakeKey2\n", encoding="utf-8")
        (self.root / "prompt_files").mkdir()
        (self.root / "prompt_files" / "default_prompt.txt").write_text("Default instruction", encoding="utf-8")
        self.cfg = Config(project_root=self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("client_pool.genai.Client")
    def test_application_protocol_conformance(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)
        self.assertTrue(isinstance(app, AppControllerProtocol))

    @patch("client_pool.genai.Client")
    def test_initial_state_complete_schema(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)
        state = app.get_initial_state()

        required_keys = [
            "pdfSources",
            "availableModels",
            "selectedModel",
            "tokenUsage",
            "isListening",
            "availablePrompts",
            "selectedPrompt",
            "promptContent",
            "captureMode",
            "currentHotkey",
            "history",
            "apiKeys",
            "logLevel",
        ]
        for key in required_keys:
            self.assertIn(key, state)

    @patch("client_pool.genai.Client")
    def test_hotkey_listener_transitions(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)

        with patch("keyboard.add_hotkey") as mock_add_hotkey, patch("keyboard.unhook_all_hotkeys") as mock_unhook:
            # Start listening
            self.assertTrue(app.start_listening())
            self.assertTrue(app._is_listening)
            mock_add_hotkey.assert_called()

            # Second start does nothing
            self.assertTrue(app.start_listening())

            # Change hotkey while listening -> restarts
            mock_add_hotkey.reset_mock()
            mock_unhook.reset_mock()
            self.assertTrue(app.set_hotkey("ctrl+shift+a"))
            self.assertEqual(app.get_hotkey(), "ctrl+shift+a")
            mock_unhook.assert_called_once()
            mock_add_hotkey.assert_called()

            # Stop listening
            app.stop_listening()
            self.assertFalse(app._is_listening)

    @patch("client_pool.genai.Client")
    def test_setting_pdf_or_model_stops_listening(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)

        with patch("keyboard.add_hotkey"), patch("keyboard.unhook_all_hotkeys"):
            app.start_listening()
            self.assertTrue(app._is_listening)

            # Changing PDF sources stops listening
            app.set_pdf_sources(["test.pdf"])
            self.assertFalse(app._is_listening)

            # Changing model stops listening
            app.start_listening()
            self.assertTrue(app._is_listening)
            app.set_selected_model("models/gemini-2.5-flash")
            self.assertFalse(app._is_listening)

    @patch("client_pool.genai.Client")
    def test_worker_pipeline_success_and_history_persistence(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)

        # Mock ScreenCaptureService.capture
        from PIL import Image
        fake_img = Image.new("RGB", (100, 100), color="blue")
        buf = io.BytesIO()
        fake_img.save(buf, format="JPEG")
        fake_jpeg = buf.getvalue()

        # Mock GeminiService.process_question
        mock_result = QuestionResult(
            answer="B) 42",
            tokens_used=180,
            model_used="models/gemini-2.5-flash",
            api_key_used="AIzaSyFakeKey1",
        )
        app._gemini.process_question = MagicMock(return_value=mock_result)

        app._active_request_id = 1
        app._process_question_worker(
            request_id=1,
            image_bytes=fake_jpeg,
            mime_type="image/jpeg",
            pdf_sources=[],
            model="models/gemini-2.5-flash",
            prompt_file="default_prompt.txt",
            capture_mode="cursor_monitor",
            hotkey_start_time=time.perf_counter() - 0.2,
            image_path=str(self.root / "shot.jpg"),
            thumbnail_b64="data:image/jpeg;base64,fake",
        )

        # Token usage updated
        self.assertEqual(app._token_repo.get_total(), 180)
        self.assertEqual(app._token_repo.get_key_stats()["AIzaSyFakeKey1"]["tokens"], 180)

        # History entry added
        history = app.get_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["answer"], "B) 42")
        self.assertEqual(history[0]["tokens"], 180)
        self.assertEqual(history[0]["status"], "success")
        self.assertIsNotNone(history[0]["thumbnail"])

    @patch("client_pool.genai.Client")
    def test_worker_pipeline_superseded_request_discarded(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)

        mock_result = QuestionResult(
            answer="Older Slow Answer",
            tokens_used=100,
            model_used="models/gemini-2.5-flash",
        )
        app._gemini.process_question = MagicMock(return_value=mock_result)

        from PIL import Image
        fake_img = Image.new("RGB", (20, 20), color="red")
        buf = io.BytesIO()
        fake_img.save(buf, format="JPEG")

        # Simulate a newer hotkey pressed while request 1 is processing
        app._active_request_id = 2

        app._process_question_worker(
            request_id=1,
            image_bytes=buf.getvalue(),
            mime_type="image/jpeg",
            pdf_sources=[],
            model="models/gemini-2.5-flash",
            prompt_file="default_prompt.txt",
            capture_mode="cursor_monitor",
            hotkey_start_time=time.perf_counter(),
            image_path=str(self.root / "shot.jpg"),
            thumbnail_b64="data:image/jpeg;base64,fake",
        )

        # Answer was discarded! History should remain empty
        self.assertEqual(len(app.get_history()), 0)

    @patch("client_pool.genai.Client")
    def test_application_api_key_management_callbacks(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)

        keys_info = app.get_api_keys_info()
        self.assertEqual(len(keys_info), 2)
        self.assertEqual(keys_info[0]["index"], 1)
        self.assertTrue("..." in keys_info[0]["key_masked"])

        # Add key
        self.assertTrue(app.add_api_key("AIzaSyNewKey333"))
        self.assertEqual(len(app.get_api_keys_info()), 3)

        # Remove key
        self.assertTrue(app.remove_api_key("AIzaSyNewKey333"))
        self.assertEqual(len(app.get_api_keys_info()), 2)

    @patch("client_pool.genai.Client")
    def test_application_history_deletion_and_clearing(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)
        app._history_repo.add({"id": 1, "answer": "Ans 1", "image_path": None})
        app._history_repo.add({"id": 2, "answer": "Ans 2", "image_path": None})

        self.assertEqual(len(app.get_history()), 2)
        self.assertTrue(app.delete_history_item(1))
        self.assertEqual(len(app.get_history()), 1)
        self.assertFalse(app.delete_history_item(999))

        app.clear_history()
        self.assertEqual(len(app.get_history()), 0)

    @patch("client_pool.genai.Client")
    def test_application_prompt_preset_operations(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)

        prompts = app.get_available_prompts()
        self.assertIn("default_prompt.txt", prompts)

        # Save prompt
        self.assertTrue(app.save_prompt("exam_prompt", "Answer with concise letters."))
        self.assertIn("exam_prompt.txt", app.get_available_prompts())
        self.assertEqual(app.get_selected_prompt(), "exam_prompt.txt")
        self.assertEqual(app.get_prompt_content("exam_prompt.txt"), "Answer with concise letters.")

    @patch("client_pool.genai.Client")
    def test_application_log_level_switching(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)

        self.assertEqual(app.get_log_level(), "INFO")
        self.assertTrue(app.set_log_level("DEBUG"))
        self.assertEqual(app.get_log_level(), "DEBUG")
        self.assertFalse(app.set_log_level("INVALID_LEVEL"))

    @patch("client_pool.genai.Client")
    def test_application_get_response_time_stats(self, mock_client_cls):
        from main import Application
        app = Application(config=self.cfg, prompt_file="default_prompt.txt", invisible=True)
        stats = app.get_response_time_stats()
        self.assertIn("today", stats)
        self.assertIn("week", stats)
        self.assertIn("month", stats)
        self.assertIn("all", stats)
        self.assertIn("generated_at", stats)


# =====================================================================
# Main runner
# =====================================================================

if __name__ == "__main__":
    unittest.main()
