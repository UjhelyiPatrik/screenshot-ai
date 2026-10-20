"""Main entry point for Screenshot AI.

Manages application lifecycle, hotkey registration, UI integration,
streaming responses, customizable presets, and concurrent preemptive request processing.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

import keyboard
from PIL import Image

import token_db
from config import Config, setup_logging, set_console_log_level
from gemini import GeminiService, QuestionResult
from trayicon import TrayIcon
from ui import AppCallbacks, UI

logger = logging.getLogger("screenshot_ai")


class Application:
    """Central application controller that mediates between the hotkey listener,

    Gemini AI service, system tray icon, and graphical user interface.
    """

    def __init__(self, config: Config, prompt_file: str, invisible: bool = False) -> None:
        self._config = config
        self._is_hidden = invisible
        self._should_start_listening = invisible

        # Thread synchronization (Re-entrant lock)
        self._lock = threading.RLock()
        self._quitting = False
        self._is_listening = False

        # Preemptive request tracking (request ID counter)
        self._active_request_id = 0

        # Load persistent settings
        saved_settings = self._load_settings()
        self._pdf_sources: list[str] = saved_settings.get("pdf_sources", [])
        self._capture_mode: str = saved_settings.get("capture_mode", "cursor_monitor")
        self._hotkey: str = saved_settings.get("hotkey", "ctrl+shift+q")
        self._log_level: str = saved_settings.get("log_level", "INFO").upper()

        # Resolve prompt file: prefer explicit CLI if not default, else settings, else ultra prompt if present
        ultra_prompt_exists = (self._config.prompt_dir / "default_prompt_ultra.txt").exists()
        fallback_prompt = "default_prompt_ultra.txt" if ultra_prompt_exists else "default_prompt.txt"
        if prompt_file != "default_prompt.txt":
            self._prompt_file_name = prompt_file
        else:
            self._prompt_file_name = saved_settings.get("prompt_file", fallback_prompt)

        # Load persistent history
        self._history: list[dict] = self._load_history()

        # Log queue shared with the UI
        self._log_queue: queue.Queue[str | None] = queue.Queue()
        setup_logging(self._log_queue, level=self._log_level)
        logging.getLogger("screenshot_ai").setLevel(logging.DEBUG)

        logger.debug(
            "Application initializing with settings: model='%s', hotkey='%s', capture_mode='%s', prompt='%s', log_level='%s'",
            saved_settings.get("selected_model", "auto"),
            self._hotkey,
            self._capture_mode,
            self._prompt_file_name,
            self._log_level,
        )

        # Core services
        logger.info("Initializing Gemini service …")
        self._gemini = GeminiService(config=self._config)

        # Pre-select model from settings if saved, else newest flash model
        saved_model = saved_settings.get("selected_model")
        if saved_model:
            self._selected_model = saved_model
            logger.info("Restored model from settings: %s", saved_model)
        else:
            self._selected_model = self._gemini.select_newest_flash_model()

        # Load token usage data
        self._token_data = token_db.load_token_data(self._config.token_db_file)
        today_str = str(date.today())
        logger.info(
            "Loaded token data — Total: %d, Today: %d",
            self._token_data.get("total", 0),
            self._token_data.get("daily", {}).get(today_str, 0),
        )

        # Always persist settings on launch if settings.json didn't exist yet
        if not self._config.settings_file.exists():
            self._save_settings()

        # System tray icon
        self._trayicon = TrayIcon(
            quit_callback=self.shutdown,
            show_gui_callback=self.toggle_ui_visibility,
        )

        # UI instance
        self._ui: UI | None = None

    def _load_settings(self) -> dict:
        """Load user settings from settings.json."""
        if self._config.settings_file.exists():
            try:
                data = json.loads(self._config.settings_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                logger.warning("Failed to load settings from %s: %s", self._config.settings_file, exc)
        return {}

    def _save_settings(self) -> None:
        """Persist current user settings to settings.json."""
        try:
            with self._lock:
                settings = {
                    "selected_model": self._selected_model,
                    "hotkey": self._hotkey,
                    "prompt_file": self._prompt_file_name,
                    "capture_mode": self._capture_mode,
                    "pdf_sources": list(self._pdf_sources),
                    "log_level": self._log_level,
                }
            self._config.settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save settings: %s", exc)

    def _load_history(self) -> list[dict]:
        """Load query history from history.json, ensuring thumbnails exist for previews."""
        if self._config.history_file.exists():
            try:
                data = json.loads(self._config.history_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    history = data[:100]
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
                            self._config.history_file.write_text(json.dumps(history, indent=2), encoding="utf-8")
                            logger.info("Regenerated missing history thumbnails in history.json.")
                        except Exception as exc:
                            logger.debug("Failed saving history after thumbnail regeneration: %s", exc)
                    return history
            except Exception as exc:
                logger.warning("Failed to load history from %s: %s", self._config.history_file, exc)
        return []

    def _save_history(self) -> None:
        """Persist query history to history.json."""
        try:
            with self._lock:
                to_save = list(self._history[:100])
            self._config.history_file.write_text(json.dumps(to_save, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save history: %s", exc)

    # ------------------------------------------------------------------
    # Application lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Initialize UI, start hotkeys if requested, and run the main event loop."""
        callbacks = AppCallbacks(
            start_listening=self.start_listening,
            stop_listening=self.stop_listening,
            get_pdf_sources=self.get_pdf_sources,
            set_pdf_sources=self.set_pdf_sources,
            get_available_models=self.get_available_models,
            get_selected_model=self.get_selected_model,
            set_selected_model=self.set_selected_model,
            get_token_usage=self.get_token_usage,
            quit_app=self.shutdown,
            toggle_ui_visibility=self.toggle_ui_visibility,
            get_available_prompts=self.get_available_prompts,
            get_selected_prompt=self.get_selected_prompt,
            set_selected_prompt=self.set_selected_prompt,
            get_prompt_content=self.get_prompt_content,
            save_prompt=self.save_prompt,
            get_capture_mode=self.get_capture_mode,
            set_capture_mode=self.set_capture_mode,
            get_hotkey=self.get_hotkey,
            set_hotkey=self.set_hotkey,
            get_history=self.get_history,
            clear_history=self.clear_history,
            delete_history_item=self.delete_history_item,
            open_history_image=self.open_history_image,
            get_history_image_base64=self.get_history_image_base64,
            get_api_keys_info=self.get_api_keys_info,
            add_api_key=self.add_api_key,
            remove_api_key=self.remove_api_key,
            get_log_level=self.get_log_level,
            set_log_level=self.set_log_level,
            get_initial_state=self.get_initial_state,
        )

        try:
            logger.info("Initializing UI …")
            self._ui = UI(
                callbacks=callbacks,
                log_queue=self._log_queue,
                hidden=self._is_hidden,
                listening=self._should_start_listening,
            )
            logger.info("UI initialized successfully.")
        except Exception as exc:
            logger.error("Failed to initialize UI: %s", exc)
            logger.warning("Continuing without GUI. Console will be the primary interface.")
            self._ui = None

        self._hide_console_window()

        if self._ui is not None:
            logger.info("Starting UI event loop …")
            self._ui.start_ui()  # Blocks until UI window is closed
        else:
            self._run_fallback_loop()

        self._cleanup()
        logger.info("Program finished.")
        sys.exit(0)

    def shutdown(self) -> None:
        """Signal the application to shut down gracefully."""
        with self._lock:
            if self._quitting:
                return
            self._quitting = True

        logger.info("Shutdown requested.")

        self.stop_listening()

        if self._trayicon:
            self._trayicon.stop()

        if self._ui and self._ui.window:
            try:
                self._ui.destroy()
            except Exception as exc:
                logger.error("Error destroying UI window: %s", exc)

    def _cleanup(self) -> None:
        """Unhook hotkeys and finalize resources."""
        try:
            keyboard.unhook_all_hotkeys()
            logger.info("All keyboard hotkeys unhooked.")
        except Exception as exc:
            logger.warning("Error unhooking hotkeys during cleanup: %s", exc)

    def _run_fallback_loop(self) -> None:
        """Fallback polling loop used when GUI initialization fails."""
        logger.info("Entering console fallback loop. Press Ctrl+C to quit.")
        try:
            while not self._quitting:
                time.sleep(0.1)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
            self.shutdown()

    @staticmethod
    def _hide_console_window() -> None:
        """Hide the Windows console window."""
        try:
            import ctypes
            import win32con
            import win32gui

            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                win32gui.SetForegroundWindow(hwnd)
                hwnd = win32gui.GetForegroundWindow()
                if hwnd:
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                    logger.info("Console window hidden.")
        except Exception as exc:
            logger.debug("Could not hide console window: %s", exc)

    # ------------------------------------------------------------------
    # UI visibility
    # ------------------------------------------------------------------

    def toggle_ui_visibility(self) -> None:
        """Toggle GUI window between shown and hidden."""
        if not self._ui or not self._ui.window:
            logger.warning("UI window is not available.")
            return

        with self._lock:
            if self._is_hidden:
                self._ui.show()
                self._is_hidden = False
            else:
                self._ui.hide()
                self._is_hidden = True

    # ------------------------------------------------------------------
    # Listening / Hotkeys
    # ------------------------------------------------------------------

    def start_listening(self) -> bool:
        """Enable listening state and register global keyboard hotkeys."""
        with self._lock:
            if self._is_listening:
                logger.info("Already in listening state.")
                return True

            if not self._selected_model:
                logger.error("No AI model selected. Cannot start listening.")
                if self._ui:
                    self._ui.update_ui_state("configuring")
                return False

            if not self._pdf_sources:
                logger.warning("No PDF sources added. Will only use screenshot for context.")

            logger.info("Registering keyboard hotkeys (Question: %s) …", self._hotkey)
            try:
                keyboard.add_hotkey(self._hotkey, self._on_question_hotkey)
                keyboard.add_hotkey("ctrl+alt+shift+c", lambda: threading.Thread(target=self.shutdown, daemon=True).start())
                self._is_listening = True
                logger.info("Hotkeys registered: %s (Question), Ctrl+Alt+Shift+C (Quit).", self._hotkey.upper())
            except Exception as exc:
                logger.error("Failed to register hotkeys (%s): %s", self._hotkey, exc)
                if self._ui:
                    self._ui.update_ui_state("configuring")
                return False

        if self._ui:
            self._ui.update_ui_state("listening")
        return True

    def stop_listening(self) -> None:
        """Disable listening state and unhook global keyboard hotkeys."""
        with self._lock:
            if not self._is_listening:
                return

            try:
                keyboard.unhook_all_hotkeys()
                logger.info("Keyboard hotkeys unhooked.")
            except Exception as exc:
                logger.warning("Error unhooking hotkeys: %s", exc)

            self._is_listening = False

        if self._ui:
            self._ui.update_ui_state("configuring")
        logger.info("Listening state disabled.")

    def set_hotkey(self, hotkey_str: str) -> bool:
        """Update question hotkey dynamically."""
        clean_key = hotkey_str.strip().lower()
        if not clean_key:
            return False

        with self._lock:
            old_key = self._hotkey
            was_listening = self._is_listening

        if was_listening:
            self.stop_listening()

        with self._lock:
            self._hotkey = clean_key
        self._save_settings()
        logger.info("Question hotkey changed to: %s", clean_key.upper())

        if was_listening:
            return self.start_listening()
        return True

    def get_hotkey(self) -> str:
        with self._lock:
            return self._hotkey

    # ------------------------------------------------------------------
    # Question processing, Streaming & Preemption
    # ------------------------------------------------------------------

    def _on_question_hotkey(self) -> None:
        """Immediate hotkey callback: must return INSTANTLY (sub-millisecond)
        so the Windows low-level keyboard hook (WH_KEYBOARD_LL) is never dropped by the OS.
        """
        hotkey_start_time = time.perf_counter()
        with self._lock:
            if not self._is_listening or self._quitting:
                return

        logger.debug("Question hotkey trigger received. Spawning QuestionTriggerHandler thread.")

        threading.Thread(
            target=self._handle_question_trigger,
            args=(hotkey_start_time,),
            name="QuestionTriggerHandler",
            daemon=True,
        ).start()

    def _handle_question_trigger(self, hotkey_start_time: float) -> None:
        """Worker executing screenshot capture, disk persistence, and model query."""
        with self._lock:
            if not self._is_listening or self._quitting:
                return
            capture_mode = self._capture_mode

        # 1. Capture screenshot immediately at keypress time (mss + JPEG)
        try:
            image_bytes, mime_type = self._gemini.take_screenshot(mode=capture_mode)
        except Exception as exc:
            logger.error("Failed to take screenshot: %s", exc)
            if self._trayicon:
                self._trayicon.display_answer("ERR", color="red", title="Screenshot capture failed")
            return

        # 2. Advance the active request ID and snapshot parameters
        with self._lock:
            self._active_request_id += 1
            request_id = self._active_request_id
            pdf_sources = list(self._pdf_sources)
            model = self._selected_model or "models/gemini-2.5-flash"
            prompt_file = self._prompt_file_name

        # 3. Save captured screenshot and generate thumbnail for persistent history preview
        t_thumb_start = time.perf_counter()
        image_path: str | None = None
        thumbnail_b64: str | None = None
        try:
            self._config.history_dir.mkdir(parents=True, exist_ok=True)
            saved_file = self._config.history_dir / f"shot_{int(time.time())}_{request_id}.jpg"
            saved_file.write_bytes(image_bytes)
            image_path = saved_file.resolve().as_posix()

            with Image.open(io.BytesIO(image_bytes)) as pil_img:
                pil_img.thumbnail((360, 220))
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85)
                thumbnail_b64 = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            t_thumb = (time.perf_counter() - t_thumb_start) * 1000
            logger.debug(
                "Persisted history screenshot: %s (%.1f KB), thumbnail generated in %.1f ms",
                saved_file.name,
                len(image_bytes) / 1024,
                t_thumb,
            )
        except Exception as exc:
            logger.warning("Could not persist screenshot or thumbnail: %s", exc)

        # 4. Indicate loading state in tray icon
        if self._trayicon:
            self._trayicon.set_loading()

        # 5. Offload to dedicated worker thread
        self._process_question_worker(
            request_id, image_bytes, mime_type, pdf_sources, model, prompt_file,
            capture_mode, hotkey_start_time, image_path, thumbnail_b64
        )

    def _is_request_cancelled(self, request_id: int) -> bool:
        """Return True if this request has been preempted by a newer hotkey trigger."""
        with self._lock:
            return request_id != self._active_request_id or self._quitting

    def _process_question_worker(
        self,
        request_id: int,
        image_bytes: bytes,
        mime_type: str,
        pdf_sources: list[str],
        model: str,
        prompt_file: str,
        capture_mode: str,
        hotkey_start_time: float,
        image_path: str | None,
        thumbnail_b64: str | None = None,
    ) -> None:
        """Worker executing streaming Gemini API call and handling preemptive completion."""
        logger.info("Processing question [request #%d] …", request_id)
        logger.debug(
            "Worker started for request #%d (mode='%s', model='%s', prompt='%s', image=%d KB, thread='%s')",
            request_id,
            capture_mode,
            model,
            prompt_file,
            len(image_bytes) // 1024,
            threading.current_thread().name,
        )
        last_stream_time = [0.0]

        def on_stream_chunk(partial_text: str) -> None:
            """Stream tokens directly to tray icon as they arrive (throttled to max 1 update / 150ms)."""
            now = time.perf_counter()
            if now - last_stream_time[0] < 0.15:
                return
            last_stream_time[0] = now

            if not self._is_request_cancelled(request_id) and self._trayicon:
                badge = partial_text[:6] if len(partial_text) > 6 else partial_text
                self._trayicon.display_answer(badge, color="navy", title=f"Generating: {partial_text}")

        try:
            result = self._gemini.process_question(
                image_bytes=image_bytes,
                mime_type=mime_type,
                pdf_sources=pdf_sources,
                model=model,
                prompt_file_name=prompt_file,
                is_cancelled=lambda: self._is_request_cancelled(request_id),
                on_stream_chunk=on_stream_chunk,
            )
        except Exception as exc:
            logger.error("Unexpected error in request #%d: %s", request_id, exc)
            result = QuestionResult(error_code="ERR", error_message=str(exc))

        # Calculate exact duration between hotkey press and response completion
        elapsed_sec = time.perf_counter() - hotkey_start_time
        duration_formatted = f"{elapsed_sec:.2f}s" if elapsed_sec >= 1.0 else f"{int(elapsed_sec * 1000)}ms"

        # Always record token usage if tokens were consumed
        if result.tokens_used > 0:
            logger.info("Request #%d consumed %d tokens.", request_id, result.tokens_used)
            with self._lock:
                self._token_data = token_db.update_token_data(
                    self._token_data, result.tokens_used, key=result.api_key_used
                )
                token_db.save_token_data(self._config.token_db_file, self._token_data)
                total = self._token_data.get("total", 0)
                today = self._token_data.get("daily", {}).get(str(date.today()), 0)
            if self._ui:
                self._ui.update_token_usage(total, today)

        # Check if this request was preempted by a newer Ctrl+Shift+Q press
        if self._is_request_cancelled(request_id):
            logger.info(
                "Request #%d finished, but superseded by request #%d. Discarding older answer.",
                request_id,
                self._active_request_id,
            )
            return

        # Record in history
        history_entry = {
            "id": request_id,
            "time": time.strftime("%H:%M:%S"),
            "duration": duration_formatted,
            "model": result.model_used or model,
            "capture_mode": capture_mode,
            "tokens": result.tokens_used,
            "status": "success" if result.is_success else "error",
            "answer": result.answer if result.is_success else (result.error_message or "Error"),
            "image_path": image_path,
            "thumbnail": thumbnail_b64,
        }
        with self._lock:
            self._history.insert(0, history_entry)
            if len(self._history) > 100:
                self._history.pop()
        self._save_history()

        if self._ui:
            self._ui.add_history_entry(history_entry)

        # Request is still current — display final result in tray icon
        if result.is_success:
            logger.info("Gemini answer [request #%d]: %s", request_id, result.answer)
            if self._trayicon:
                self._trayicon.display_answer(result.answer)
        else:
            badge = result.error_code or "ERR"
            detail = result.error_message or "API call failed"
            logger.error(
                "Request #%d failed [%s]: %s (Next screenshot will retry automatically)",
                request_id,
                badge,
                detail,
            )
            if self._trayicon:
                self._trayicon.display_answer(badge, color="red", title=f"Error {badge}: {detail}")

    # ------------------------------------------------------------------
    # Callbacks exposed to UI
    # ------------------------------------------------------------------

    def get_pdf_sources(self) -> list[str]:
        with self._lock:
            return list(self._pdf_sources)

    def set_pdf_sources(self, sources: list[str]) -> None:
        with self._lock:
            self._pdf_sources = list(sources)
        self._save_settings()
        logger.info("Updated PDF sources: %s", sources)

        self._gemini.invalidate_pdf_cache()

        if self._is_listening:
            logger.warning("PDF sources changed while listening. Stopping listening mode.")
            self.stop_listening()

    def get_available_models(self) -> list[str]:
        models = self._gemini.list_models()
        if not models and self._selected_model:
            return [self._selected_model]
        return models

    def get_selected_model(self) -> str | None:
        with self._lock:
            return self._selected_model

    def set_selected_model(self, model: str) -> None:
        with self._lock:
            self._selected_model = model
        self._save_settings()
        logger.info("Selected model: %s", model)

        if self._is_listening:
            logger.warning("Model changed while listening. Stopping listening mode.")
            self.stop_listening()

    def get_token_usage(self) -> dict:
        with self._lock:
            return dict(self._token_data)

    def get_available_prompts(self) -> list[str]:
        return self._gemini.list_prompts()

    def get_selected_prompt(self) -> str:
        with self._lock:
            return self._prompt_file_name

    def set_selected_prompt(self, prompt_name: str) -> None:
        with self._lock:
            self._prompt_file_name = prompt_name
        self._save_settings()
        logger.info("Selected prompt preset: %s", prompt_name)

    def get_prompt_content(self, prompt_name: str) -> str:
        return self._gemini.get_prompt_content(prompt_name)

    def save_prompt(self, prompt_name: str, content: str) -> bool:
        success = self._gemini.save_prompt(prompt_name, content)
        if success:
            clean_name = prompt_name.strip()
            if not clean_name.endswith(".txt"):
                clean_name += ".txt"
            with self._lock:
                self._prompt_file_name = clean_name
            self._save_settings()
        return success

    def open_history_image(self, image_path: str) -> bool:
        """Open the captured screenshot with the default system image viewer."""
        if not image_path:
            return False
        clean_path = os.path.normpath(image_path)
        if not os.path.exists(clean_path):
            logger.warning("Image file does not exist: %s", clean_path)
            return False
        try:
            if sys.platform == "win32":
                os.startfile(clean_path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", clean_path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", clean_path])
            logger.info("Opened history image: %s", clean_path)
            return True
        except Exception as exc:
            logger.error("Failed to open image %s: %s", clean_path, exc)
            return False

    def get_history_image_base64(self, image_path: str) -> str | None:
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

    def get_api_keys_info(self) -> list[dict]:
        """Return list of API keys with per-key usage statistics."""
        with self._lock:
            keys = self._gemini.get_api_keys()
            active_idx = self._gemini.get_last_key_index()
            usage_dict = self._token_data.get("keys", {})

        result = []
        for idx, k in enumerate(keys):
            masked = f"{k[:6]}...{k[-4:]}" if len(k) > 12 else k
            stats = usage_dict.get(k, {})
            result.append({
                "index": idx + 1,
                "key_masked": masked,
                "key_full": k,
                "is_active": (idx == active_idx),
                "requests": stats.get("requests", 0),
                "tokens": stats.get("tokens", 0),
                "last_used": stats.get("last_used", "Never"),
            })
        return result

    def add_api_key(self, key: str) -> bool:
        """Add new API key and refresh keys."""
        return self._gemini.add_api_key(key)

    def remove_api_key(self, key: str) -> bool:
        """Remove API key and refresh keys."""
        return self._gemini.remove_api_key(key)

    def get_initial_state(self) -> dict:
        with self._lock:
            pdf_sources = list(self._pdf_sources)
            selected_model = self._selected_model
            token_data = dict(self._token_data)
            is_listening = self._is_listening
            prompt_file_name = self._prompt_file_name
            capture_mode = self._capture_mode
            hotkey = self._hotkey
            history = list(self._history)
            log_level = self._log_level

        return {
            "pdfSources": pdf_sources,
            "availableModels": self.get_available_models(),
            "selectedModel": selected_model,
            "tokenUsage": token_data,
            "isListening": is_listening,
            "availablePrompts": self._gemini.list_prompts(),
            "selectedPrompt": prompt_file_name,
            "promptContent": self._gemini.get_prompt_content(prompt_file_name),
            "captureMode": capture_mode,
            "currentHotkey": hotkey,
            "history": history,
            "apiKeys": self.get_api_keys_info(),
            "logLevel": log_level,
        }

    def get_log_level(self) -> str:
        with self._lock:
            return self._log_level

    def set_log_level(self, level: str) -> bool:
        lvl_upper = level.strip().upper()
        if lvl_upper not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            return False
        with self._lock:
            self._log_level = lvl_upper
        self._save_settings()
        set_console_log_level(lvl_upper)
        logger.info("Log level set to %s.", lvl_upper)
        return True

    def get_capture_mode(self) -> str:
        with self._lock:
            return self._capture_mode

    def set_capture_mode(self, mode: str) -> bool:
        with self._lock:
            self._capture_mode = mode
        self._save_settings()
        logger.info("Selected capture mode: %s", mode)
        return True

    def get_history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
        self._save_history()
        logger.info("Query history cleared.")

    def delete_history_item(self, item_id: Any) -> bool:
        """Delete a single query from history by its ID (or timestamp/image_path)."""
        with self._lock:
            found_idx = -1
            target_str = str(item_id).strip()
            for i, entry in enumerate(self._history):
                if (
                    str(entry.get("id")) == target_str
                    or str(entry.get("time")) == target_str
                    or str(entry.get("image_path")) == target_str
                ):
                    found_idx = i
                    break

            if found_idx != -1:
                removed = self._history.pop(found_idx)
                img_path = removed.get("image_path")
                if img_path:
                    try:
                        clean_path = os.path.normpath(img_path)
                        if os.path.exists(clean_path):
                            os.remove(clean_path)
                    except Exception as exc:
                        logger.debug("Could not delete image file %s: %s", img_path, exc)
                self._save_history()
                logger.info("Deleted history entry #%s (%s).", item_id, removed.get("time"))
                return True

        return False


# ------------------------------------------------------------------
# CLI Entry point
# ------------------------------------------------------------------

def parse_args() -> tuple[str, bool]:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Screenshot AI Question Answering")
    parser.add_argument(
        "prompt_file",
        nargs="?",
        default="default_prompt.txt",
        help="Prompt filename located in prompt_files/ (default: default_prompt.txt)",
    )
    parser.add_argument(
        "-i",
        "--invisible",
        action="store_true",
        help="Start in invisible mode with listening active",
    )
    args = parser.parse_args()
    return args.prompt_file, args.invisible


def main() -> NoReturn:
    prompt_file, invisible = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    config = Config(project_root=project_root)

    app = Application(config=config, prompt_file=prompt_file, invisible=invisible)
    app.run()


if __name__ == "__main__":
    main()
