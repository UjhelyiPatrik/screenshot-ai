"""Unit tests for Screenshot AI modules."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure source/ is on sys.path
source_dir = Path(__file__).resolve().parent.parent / "source"
sys.path.insert(0, str(source_dir))

from config import Config
import token_db
from gemini import GeminiService, QuestionResult, _classify_error
from google.genai import errors as genai_errors


class TestConfig(unittest.TestCase):
    def test_config_paths(self):
        root = Path("/tmp/fake_project")
        cfg = Config(project_root=root)
        self.assertEqual(cfg.api_key_file, root / "apikeys.txt")
        self.assertEqual(cfg.prompt_dir, root / "prompt_files")
        self.assertEqual(cfg.token_db_file, root / "token_usage.json")
        self.assertEqual(cfg.settings_file, root / "settings.json")
        self.assertEqual(cfg.history_file, root / "history.json")
        self.assertEqual(cfg.history_dir, root / "history_images")


class TestTokenDB(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "token_usage.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_non_existent(self):
        data = token_db.load_token_data(self.db_path)
        self.assertEqual(data, {"total": 0, "daily": {}, "keys": {}})

    def test_save_and_load(self):
        sample = {"total": 150, "daily": {"2026-10-19": 150}, "keys": {}}
        token_db.save_token_data(self.db_path, sample)
        loaded = token_db.load_token_data(self.db_path)
        self.assertEqual(loaded, sample)

    def test_update_token_data(self):
        data = {"total": 100, "daily": {}, "keys": {}}
        updated = token_db.update_token_data(data, 50)
        self.assertEqual(updated["total"], 150)
        today = str(date.today())
        self.assertEqual(updated["daily"][today], 50)

    def test_update_token_data_with_key(self):
        data = {"total": 100, "daily": {}, "keys": {}}
        updated = token_db.update_token_data(data, 50, key="TEST_KEY")
        self.assertEqual(updated["total"], 150)
        self.assertEqual(updated["keys"]["TEST_KEY"]["tokens"], 50)
        self.assertEqual(updated["keys"]["TEST_KEY"]["requests"], 1)

    def test_pruning_old_dates(self):
        old_day = str(date.today() - timedelta(days=35))
        data = {"total": 100, "daily": {old_day: 100}, "keys": {}}
        updated = token_db.update_token_data(data, 20)
        self.assertNotIn(old_day, updated["daily"])
        today = str(date.today())
        self.assertEqual(updated["daily"][today], 20)


class TestErrorClassification(unittest.TestCase):
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

    def test_classify_network_error(self):
        err = ConnectionAbortedError("WinError 10053")
        code, msg = _classify_error(err)
        self.assertEqual(code, "NET")


class TestGeminiService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.key_file = self.root / "apikeys.txt"
        self.key_file.write_text("# last_index=0\nKEY1\nKEY2\nKEY3\n", encoding="utf-8")
        self.prompt_dir = self.root / "prompt_files"
        self.prompt_dir.mkdir()
        (self.prompt_dir / "default_prompt.txt").write_text("Test prompt instruction", encoding="utf-8")
        (self.prompt_dir / "math.txt").write_text("Math prompt", encoding="utf-8")
        self.cfg = Config(project_root=self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("gemini.genai.Client")
    def test_client_pool_and_rotation(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        self.assertEqual(len(service._clients), 3)

        client1, key1, idx1 = service._get_active_client()
        self.assertEqual(key1, "KEY2")
        self.assertEqual(idx1, 1)

        client2, key2, idx2 = service._get_active_client()
        self.assertEqual(key2, "KEY3")
        self.assertEqual(idx2, 2)

        client3, key3, idx3 = service._get_active_client()
        self.assertEqual(key3, "KEY1")
        self.assertEqual(idx3, 0)

    @patch("gemini.genai.Client")
    def test_list_prompts(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        prompts = service.list_prompts()
        self.assertIn("default_prompt.txt", prompts)
        self.assertIn("math.txt", prompts)

    @patch("gemini.genai.Client")
    def test_get_and_save_prompt(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        content = service.get_prompt_content("default_prompt.txt")
        self.assertEqual(content, "Test prompt instruction")

        success = service.save_prompt("coding_prompt.txt", "Solve the code problem concisely.")
        self.assertTrue(success)
        self.assertIn("coding_prompt.txt", service.list_prompts())
        self.assertEqual(service.get_prompt_content("coding_prompt"), "Solve the code problem concisely.")

    @patch("gemini.genai.Client")
    def test_add_and_remove_api_key(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        self.assertEqual(len(service.get_api_keys()), 3)
        self.assertTrue(service.add_api_key("NEW_KEY_999"))
        self.assertIn("NEW_KEY_999", service.get_api_keys())
        # Duplicate rejected
        self.assertFalse(service.add_api_key("NEW_KEY_999"))
        # Remove key
        self.assertTrue(service.remove_api_key("NEW_KEY_999"))
        self.assertNotIn("NEW_KEY_999", service.get_api_keys())

    @patch("gemini.genai.Client")
    def test_pdf_cache_invalidation(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        mock_client = MagicMock()
        mock_upload = MagicMock(uri="https://gemini/file/123")
        mock_client.files.upload.return_value = mock_upload

        fake_pdf = self.root / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 test")

        part1 = service._get_or_upload_pdf(mock_client, str(fake_pdf))
        self.assertEqual(part1, mock_upload)
        self.assertEqual(mock_client.files.upload.call_count, 1)

        part2 = service._get_or_upload_pdf(mock_client, str(fake_pdf))
        self.assertEqual(part2, mock_upload)
        self.assertEqual(mock_client.files.upload.call_count, 1)

        service.invalidate_pdf_cache()
        part3 = service._get_or_upload_pdf(mock_client, str(fake_pdf))
        self.assertEqual(mock_client.files.upload.call_count, 2)

    @patch("gemini.genai.Client")
    def test_streaming_and_fallback(self, mock_client_cls):
        service = GeminiService(config=self.cfg)
        mock_client = MagicMock()

        # Primary model (gemini-3.8-flash) raises 503
        err_503 = genai_errors.APIError(503, {"error": {"message": "High demand"}})

        # Fallback model streams chunks
        chunk1 = MagicMock(text="Part 1 ", usage_metadata=None)
        chunk2 = MagicMock(text="Part 2", usage_metadata=MagicMock(total_token_count=100))

        mock_client.models.generate_content_stream.side_effect = [
            err_503,
            err_503,
            [chunk1, chunk2],
        ]

        received_chunks = []

        ans, tokens, err_c, err_m, model_used = service._call_api_resilient(
            client=mock_client,
            contents=["test"],
            model="models/gemini-3.8-flash",
            on_stream_chunk=lambda text: received_chunks.append(text),
        )

        self.assertEqual(ans, "Part 1 Part 2")
        self.assertEqual(tokens, 100)
        self.assertIsNone(err_c)
        self.assertIsNone(err_m)
        self.assertEqual(received_chunks, ["Part 1", "Part 1 Part 2"])
        self.assertEqual(model_used, "models/gemini-2.5-flash")

    @patch("gemini.pyautogui.screenshot")
    def test_take_screenshot_jpeg_bytes(self, mock_screenshot):
        from PIL import Image
        fake_img = Image.new("RGB", (10, 10), color="blue")
        mock_screenshot.return_value = fake_img

        raw_bytes, mime = GeminiService.take_screenshot(mode="cursor_monitor")
        self.assertIsInstance(raw_bytes, bytes)
        self.assertEqual(mime, "image/jpeg")
        self.assertGreater(len(raw_bytes), 0)


class TestPreemptionLogic(unittest.TestCase):
    """Test preemptive request execution logic."""

    def test_preemption_simulation(self):
        active_request_id = 0
        results_displayed = []

        active_request_id += 1
        req1_id = active_request_id

        active_request_id += 1
        req2_id = active_request_id

        def on_worker_finish(req_id, result: QuestionResult):
            if req_id != active_request_id:
                return False
            results_displayed.append((req_id, result.answer))
            return True

        res1 = QuestionResult(answer="Old Answer")
        kept1 = on_worker_finish(req1_id, res1)
        self.assertFalse(kept1)
        self.assertEqual(len(results_displayed), 0)

        res2 = QuestionResult(answer="New Fresh Answer")
        kept2 = on_worker_finish(req2_id, res2)
        self.assertTrue(kept2)
        self.assertEqual(results_displayed, [(2, "New Fresh Answer")])


class TestAppCallbacks(unittest.TestCase):
    """Ensure AppCallbacks dataclass contracts are satisfied."""

    def test_callbacks_instantiation(self):
        from ui import AppCallbacks

        cb = AppCallbacks(
            start_listening=lambda: True,
            stop_listening=lambda: None,
            get_pdf_sources=lambda: [],
            set_pdf_sources=lambda s: None,
            get_available_models=lambda: ["m1"],
            get_selected_model=lambda: "m1",
            set_selected_model=lambda m: None,
            get_token_usage=lambda: {"total": 0, "daily": {}},
            quit_app=lambda: None,
            toggle_ui_visibility=lambda: None,
            get_available_prompts=lambda: ["default_prompt.txt"],
            get_selected_prompt=lambda: "default_prompt.txt",
            set_selected_prompt=lambda p: None,
            get_prompt_content=lambda p: "content",
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
            get_initial_state=lambda: {},
        )
        self.assertEqual(cb.get_selected_prompt(), "default_prompt.txt")
        self.assertEqual(cb.get_prompt_content("p"), "content")
        self.assertTrue(cb.save_prompt("p", "c"))
        self.assertTrue(cb.set_capture_mode("active_window"))
        self.assertTrue(cb.delete_history_item(1))
        self.assertTrue(cb.open_history_image("path.jpg"))
        self.assertIsNone(cb.get_history_image_base64("path.jpg"))
        self.assertTrue(cb.add_api_key("KEY"))
        self.assertTrue(cb.remove_api_key("KEY"))
        self.assertEqual(cb.get_log_level(), "INFO")
        self.assertTrue(cb.set_log_level("DEBUG"))
        self.assertEqual(cb.get_initial_state(), {})


class TestApplicationInitialState(unittest.TestCase):
    """Ensure Application.get_initial_state executes without deadlocks."""

    @patch("gemini.genai.Client")
    def test_application_initial_state(self, mock_client_cls):
        from main import Application
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            key_file = root / "apikeys.txt"
            key_file.write_text("AIzaSyFakeKey1\nAIzaSyFakeKey2\n", encoding="utf-8")
            (root / "prompt_files").mkdir()
            (root / "prompt_files" / "default_prompt.txt").write_text("test", encoding="utf-8")

            cfg = Config(project_root=root)
            app = Application(config=cfg, prompt_file="default_prompt.txt", invisible=True)
            state = app.get_initial_state()

            self.assertIn("availableModels", state)
            self.assertIn("apiKeys", state)
            self.assertEqual(len(state["apiKeys"]), 2)
            self.assertEqual(state["selectedPrompt"], "default_prompt.txt")
            self.assertEqual(state["logLevel"], "INFO")

    @patch("gemini.genai.Client")
    def test_application_capture_mode_persistence(self, mock_client_cls):
        from main import Application
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            key_file = root / "apikeys.txt"
            key_file.write_text("AIzaSyFakeKey1\n", encoding="utf-8")
            (root / "prompt_files").mkdir()
            (root / "prompt_files" / "default_prompt.txt").write_text("test", encoding="utf-8")

            cfg = Config(project_root=root)
            app1 = Application(config=cfg, prompt_file="default_prompt.txt", invisible=True)
            self.assertEqual(app1.get_capture_mode(), "cursor_monitor")

            # Change to active_window
            success = app1.set_capture_mode("active_window")
            self.assertTrue(success)
            self.assertEqual(app1.get_capture_mode(), "active_window")

            # Verify settings.json exists and has active_window
            self.assertTrue(cfg.settings_file.exists())
            saved = json.loads(cfg.settings_file.read_text(encoding="utf-8"))
            self.assertEqual(saved.get("capture_mode"), "active_window")

            # Launch a new Application instance and verify persistence
            app2 = Application(config=cfg, prompt_file="default_prompt.txt", invisible=True)
            self.assertEqual(app2.get_capture_mode(), "active_window")
            state = app2.get_initial_state()
            self.assertEqual(state.get("captureMode"), "active_window")

    @patch("gemini.genai.Client")
    def test_application_delete_history_item(self, mock_client_cls):
        from main import Application
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            key_file = root / "apikeys.txt"
            key_file.write_text("AIzaSyFakeKey1\n", encoding="utf-8")
            (root / "prompt_files").mkdir()
            (root / "prompt_files" / "default_prompt.txt").write_text("test", encoding="utf-8")

            dummy_img = root / "dummy.jpg"
            dummy_img.write_bytes(b"fake image data")

            cfg = Config(project_root=root)
            app = Application(config=cfg, prompt_file="default_prompt.txt", invisible=True)

            app._history = [
                {"id": 1, "time": "12:00:00", "question": "Q1", "answer": "A1", "image_path": str(dummy_img)},
                {"id": 2, "time": "12:01:00", "question": "Q2", "answer": "A2", "image_path": None},
            ]
            app._save_history()

            self.assertEqual(len(app.get_history()), 2)
            self.assertTrue(dummy_img.exists())

            deleted = app.delete_history_item(1)
            self.assertTrue(deleted)
            self.assertEqual(len(app.get_history()), 1)
            self.assertEqual(app.get_history()[0]["id"], 2)
            self.assertFalse(dummy_img.exists())

            self.assertFalse(app.delete_history_item(999))

    @patch("gemini.genai.Client")
    def test_application_log_level_persistence(self, mock_client_cls):
        from main import Application
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            key_file = root / "apikeys.txt"
            key_file.write_text("AIzaSyFakeKey1\n", encoding="utf-8")
            (root / "prompt_files").mkdir()
            (root / "prompt_files" / "default_prompt.txt").write_text("test", encoding="utf-8")

            cfg = Config(project_root=root)
            app1 = Application(config=cfg, prompt_file="default_prompt.txt", invisible=True)
            self.assertEqual(app1.get_log_level(), "INFO")

            # Change log level to DEBUG
            self.assertTrue(app1.set_log_level("DEBUG"))
            self.assertEqual(app1.get_log_level(), "DEBUG")

            # Verify persisted in settings.json
            self.assertTrue(cfg.settings_file.exists())
            saved = json.loads(cfg.settings_file.read_text(encoding="utf-8"))
            self.assertEqual(saved.get("log_level"), "DEBUG")

            # Launch new app and verify restored
            app2 = Application(config=cfg, prompt_file="default_prompt.txt", invisible=True)
            self.assertEqual(app2.get_log_level(), "DEBUG")
            self.assertEqual(app2.get_initial_state().get("logLevel"), "DEBUG")

            # Invalid log level
            self.assertFalse(app2.set_log_level("INVALID_LEVEL"))

    @patch("gemini.genai.Client")
    def test_application_get_history_image_base64(self, mock_client_cls):
        from main import Application
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            key_file = root / "apikeys.txt"
            key_file.write_text("AIzaSyFakeKey1\n", encoding="utf-8")
            (root / "prompt_files").mkdir()
            (root / "prompt_files" / "default_prompt.txt").write_text("test", encoding="utf-8")

            dummy_img = root / "screenshot.jpg"
            dummy_img.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIFfakejpegdata")

            cfg = Config(project_root=root)
            app = Application(config=cfg, prompt_file="default_prompt.txt", invisible=True)

            # Test valid image path
            b64_data = app.get_history_image_base64(str(dummy_img))
            self.assertIsNotNone(b64_data)
            self.assertTrue(b64_data.startswith("data:image/jpeg;base64,"))

            # Test non-existent path
            self.assertIsNone(app.get_history_image_base64(str(root / "nonexistent.jpg")))

            # Test empty path
            self.assertIsNone(app.get_history_image_base64(""))


if __name__ == "__main__":
    unittest.main()


