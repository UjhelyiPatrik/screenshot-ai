"""Main application controller and orchestrator for Screenshot AI.

Utilizes the Facade pattern to mediate cleanly between user inputs (keyboard hotkeys, UI),
screen capture strategies, Gemini AI services, system tray notifications, and persistence repositories.
"""

from __future__ import annotations

import argparse
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

import token_db
from capture import ScreenCaptureService
from config import Config, set_console_log_level, setup_logging
from gemini import GeminiService, QuestionResult
from history import HistoryRepository
from prompt_manager import PromptManager
from settings import SettingsManager
from trayicon import TrayIcon
from ui import AppCallbacks, UI

logger = logging.getLogger("screenshot_ai")


class Application:
    """Facade orchestrator coordinating all subsystems."""

    def __init__(self, config: Config, prompt_file: str, invisible: bool = False) -> None:
        self._config = config
        self._is_hidden = invisible
        self._should_start_listening = invisible

        self._lock = threading.RLock()
        self._quitting = False
        self._is_listening = False
        self._active_request_id = 0

        # Subsystems & Repositories
        self._settings_mgr = SettingsManager(config.settings_file)
        self._history_repo = HistoryRepository(config.history_file, config.history_dir)
        self._prompt_mgr = PromptManager(config.prompt_dir)

        # Logging queue shared with the UI
        self._log_queue: queue.Queue[str | None] = queue.Queue()
        setup_logging(self._log_queue, level=self.get_log_level())
        logging.getLogger("screenshot_ai").setLevel(logging.DEBUG)

        logger.debug(
            "Application initializing with settings: model='%s', hotkey='%s', capture_mode='%s', prompt='%s', log_level='%s'",
            self._settings_mgr.settings.selected_model,
            self._settings_mgr.settings.hotkey,
            self._settings_mgr.settings.capture_mode,
            self._settings_mgr.settings.prompt_file,
            self._settings_mgr.settings.log_level,
        )

        # Core AI service
        logger.info("Initializing Gemini service …")
        self._gemini = GeminiService(config=self._config)

        # Resolve selected model
        saved_model = self._settings_mgr.settings.selected_model
        if saved_model:
            self._selected_model = saved_model
            logger.info("Restored model from settings: %s", saved_model)
        else:
            self._selected_model = self._gemini.select_newest_flash_model()
            self._settings_mgr.update(selected_model=self._selected_model)

        # Resolve prompt file preset
        if prompt_file != "default_prompt.txt":
            self._prompt_file_name = prompt_file
        else:
            saved_prompt = self._settings_mgr.settings.prompt_file
            self._prompt_file_name = self._prompt_mgr.resolve_default_prompt(saved_prompt)

        # Token usage repository
        self._token_repo = token_db.TokenRepository(self._config.token_db_file)
        logger.info(
            "Loaded token data — Total: %d, Today: %d",
            self._token_repo.get_total(),
            self._token_repo.get_today(),
        )

        # System tray icon
        self._trayicon = TrayIcon(
            quit_callback=self.shutdown,
            show_gui_callback=self.toggle_ui_visibility,
        )

        self._ui: UI | None = None

    # ------------------------------------------------------------------
    # Backwards-compatible properties
    # ------------------------------------------------------------------

    @property
    def _pdf_sources(self) -> list[str]:
        return self._settings_mgr.settings.pdf_sources

    @_pdf_sources.setter
    def _pdf_sources(self, val: list[str]) -> None:
        self._settings_mgr.update(pdf_sources=list(val))

    @property
    def _capture_mode(self) -> str:
        return self._settings_mgr.settings.capture_mode

    @_capture_mode.setter
    def _capture_mode(self, val: str) -> None:
        self._settings_mgr.update(capture_mode=val)

    @property
    def _hotkey(self) -> str:
        return self._settings_mgr.settings.hotkey

    @_hotkey.setter
    def _hotkey(self, val: str) -> None:
        self._settings_mgr.update(hotkey=val)

    @property
    def _log_level(self) -> str:
        return self._settings_mgr.settings.log_level

    @_log_level.setter
    def _log_level(self, val: str) -> None:
        self._settings_mgr.update(log_level=val)

    @property
    def _token_data(self) -> dict:
        return self._token_repo.get_raw_data()

    @_token_data.setter
    def _token_data(self, val: dict) -> None:
        with self._token_repo._lock:
            self._token_repo._data = dict(val)

    @property
    def _history(self) -> list[dict]:
        return self._history_repo.get_all()

    @_history.setter
    def _history(self, entries: list[dict]) -> None:
        with self._history_repo._lock:
            self._history_repo._entries = list(entries)

    def _save_history(self) -> None:
        self._history_repo._save()

    def _save_settings(self) -> None:
        self._settings_mgr.save()

    def quit_app(self) -> None:
        """Alias satisfying AppControllerProtocol."""
        self.shutdown()

    # ------------------------------------------------------------------
    # Application lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Initialize UI, start hotkeys if requested, and run the main event loop."""
        try:
            logger.info("Initializing UI …")
            self._ui = UI(
                callbacks=self,
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
        try:
            keyboard.unhook_all_hotkeys()
            logger.info("Keyboard hotkeys unhooked.")
        except Exception as exc:
            logger.error("Error unhooking keyboard hotkeys: %s", exc)

    def _hide_console_window(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import win32console
            import win32gui
            hwnd = win32console.GetConsoleWindow()
            if hwnd:
                win32gui.ShowWindow(hwnd, 0)
                logger.info("Console window hidden.")
        except Exception as exc:
            logger.debug("Could not hide console window: %s", exc)

    def _run_fallback_loop(self) -> None:
        logger.info("Entering console fallback event loop (Ctrl+C to quit).")
        try:
            while not self._quitting:
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("Interrupted by user.")

    def toggle_ui_visibility(self) -> None:
        if self._ui:
            self._is_hidden = not self._is_hidden
            if self._is_hidden:
                self._ui.hide()
            else:
                self._ui.show()

    # ------------------------------------------------------------------
    # Hotkey & Listening Mode
    # ------------------------------------------------------------------

    def start_listening(self) -> bool:
        """Register keyboard hotkeys and transition to listening state."""
        with self._lock:
            if self._is_listening:
                logger.info("Already in listening state.")
                return True

            if not self._selected_model:
                logger.error("No AI model selected. Cannot start listening.")
                return False

            if not self._settings_mgr.settings.pdf_sources:
                logger.warning("No PDF sources added. Will only use screenshot for context.")

            logger.info("Registering keyboard hotkeys (Question: %s) …", self._settings_mgr.settings.hotkey)
            try:
                keyboard.add_hotkey(self._settings_mgr.settings.hotkey, self._on_question_hotkey)
                keyboard.add_hotkey("ctrl+alt+shift+c", self.shutdown)
                self._is_listening = True
                logger.info("Hotkeys registered: %s (Question), Ctrl+Alt+Shift+C (Quit).", self._settings_mgr.settings.hotkey.upper())
            except Exception as exc:
                logger.error("Failed to register keyboard hotkeys: %s", exc)
                return False

        if self._ui:
            self._ui.update_ui_state("listening")
        return True

    def stop_listening(self) -> None:
        """Unhook keyboard hotkeys and transition back to configuring state."""
        with self._lock:
            if not self._is_listening:
                return
            try:
                keyboard.unhook_all_hotkeys()
                self._is_listening = False
                logger.info("Keyboard hotkeys unhooked.")
            except Exception as exc:
                logger.error("Error unhooking hotkeys: %s", exc)

        if self._ui:
            self._ui.update_ui_state("configuring")
        logger.info("Listening state disabled.")

    def set_hotkey(self, hotkey_str: str) -> bool:
        """Update question hotkey dynamically."""
        clean_key = hotkey_str.strip().lower()
        if not clean_key:
            return False

        with self._lock:
            was_listening = self._is_listening

        if was_listening:
            self.stop_listening()

        self._settings_mgr.update(hotkey=clean_key)
        logger.info("Question hotkey changed to: %s", clean_key.upper())

        if was_listening:
            return self.start_listening()
        return True

    def get_hotkey(self) -> str:
        return self._settings_mgr.settings.hotkey

    # ------------------------------------------------------------------
    # Question Processing Pipeline
    # ------------------------------------------------------------------

    def _on_question_hotkey(self) -> None:
        """Immediate hotkey callback: returns sub-millisecond to avoid Windows hook drop."""
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
        """Worker executing screen capture, disk persistence, and model query."""
        with self._lock:
            if not self._is_listening or self._quitting:
                return
            capture_mode = self._settings_mgr.settings.capture_mode

        # 1. High-speed screen capture via Strategy pattern
        try:
            image_bytes, mime_type = ScreenCaptureService.capture(mode=capture_mode)
        except Exception as exc:
            logger.error("Failed to take screenshot: %s", exc)
            if self._trayicon:
                self._trayicon.display_answer("ERR", color="red", title="Screenshot capture failed")
            return

        # 2. Advance active request ID and snapshot request parameters
        with self._lock:
            self._active_request_id += 1
            request_id = self._active_request_id
            pdf_sources = list(self._settings_mgr.settings.pdf_sources)
            model = self._selected_model or "models/gemini-2.5-flash"
            prompt_file = self._prompt_file_name

        # 3. Persist screenshot to history repository
        image_path, thumbnail_b64 = self._history_repo.persist_screenshot(request_id, image_bytes)

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

        elapsed_sec = time.perf_counter() - hotkey_start_time
        duration_formatted = f"{elapsed_sec:.2f}s" if elapsed_sec >= 1.0 else f"{int(elapsed_sec * 1000)}ms"

        # Update token usage
        if result.tokens_used > 0:
            logger.info("Request #%d consumed %d tokens.", request_id, result.tokens_used)
            total, today = self._token_repo.record_usage(result.tokens_used, key=result.api_key_used)
            if self._ui:
                self._ui.update_token_usage(total, today)

        # Discard older answer if superseded by newer hotkey trigger
        if self._is_request_cancelled(request_id):
            logger.info("Request #%d finished, but superseded by request #%d. Discarding older answer.", request_id, self._active_request_id)
            return

        # Add to history repository
        history_entry = {
            "id": request_id,
            "timestamp": time.time(),
            "date": str(date.today()),
            "time": time.strftime("%H:%M:%S"),
            "duration": duration_formatted,
            "duration_ms": round(elapsed_sec * 1000.0, 1),
            "model": result.model_used or model,
            "capture_mode": capture_mode,
            "tokens": result.tokens_used,
            "status": "success" if result.is_success else "error",
            "answer": result.answer if result.is_success else (result.error_message or "Error"),
            "image_path": image_path,
            "thumbnail": thumbnail_b64,
        }
        self._history_repo.add(history_entry)

        if self._ui:
            self._ui.add_history_entry(history_entry)

        # Update tray icon
        if result.is_success:
            logger.info("Gemini answer [request #%d]: %s", request_id, result.answer)
            if self._trayicon:
                self._trayicon.display_answer(result.answer)
        else:
            badge = result.error_code or "ERR"
            detail = result.error_message or "API call failed"
            logger.error("Request #%d failed [%s]: %s", request_id, badge, detail)
            if self._trayicon:
                self._trayicon.display_answer(badge, color="red", title=detail)

        logger.debug(
            "Request #%d finished in %s (success=%s, tokens=%d, model=%s)",
            request_id,
            duration_formatted,
            result.is_success,
            result.tokens_used,
            result.model_used or model,
        )

    # ------------------------------------------------------------------
    # Callbacks and State Providers for UI
    # ------------------------------------------------------------------

    def get_pdf_sources(self) -> list[str]:
        return list(self._settings_mgr.settings.pdf_sources)

    def set_pdf_sources(self, sources: list[str]) -> None:
        self._settings_mgr.update(pdf_sources=list(sources))
        self._gemini.invalidate_pdf_cache()
        logger.info("PDF sources updated. Total sources: %d", len(sources))
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
        self._settings_mgr.update(selected_model=model)
        logger.info("Selected model: %s", model)
        if self._is_listening:
            logger.warning("Model changed while listening. Stopping listening mode.")
            self.stop_listening()

    def get_token_usage(self) -> dict:
        with self._lock:
            return dict(self._token_data)

    def get_available_prompts(self) -> list[str]:
        return self._prompt_mgr.list_prompts()

    def get_selected_prompt(self) -> str:
        with self._lock:
            return self._prompt_file_name

    def set_selected_prompt(self, prompt_name: str) -> None:
        with self._lock:
            self._prompt_file_name = prompt_name
        self._settings_mgr.update(prompt_file=prompt_name)
        logger.info("Selected prompt preset: %s", prompt_name)

    def get_prompt_content(self, prompt_name: str) -> str:
        return self._prompt_mgr.get_content(prompt_name)

    def save_prompt(self, prompt_name: str, content: str) -> bool:
        success = self._prompt_mgr.save_prompt(prompt_name, content)
        if success:
            clean = prompt_name.strip()
            if not clean.endswith(".txt"):
                clean += ".txt"
            self.set_selected_prompt(clean)
        return success

    def open_history_image(self, image_path: str) -> bool:
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
        return self._history_repo.get_image_base64(image_path)

    def get_api_keys_info(self) -> list[dict]:
        with self._lock:
            keys = self._gemini.get_api_keys()
            active_idx = self._gemini.get_last_key_index()
            usage_dict = self._token_repo.get_key_stats()

        result = []
        for idx, k in enumerate(keys):
            masked = k[:6] + "..." + k[-4:] if len(k) > 12 else k
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
        return self._gemini.add_api_key(key)

    def remove_api_key(self, key: str) -> bool:
        return self._gemini.remove_api_key(key)

    def get_initial_state(self) -> dict:
        with self._lock:
            pdf_sources = list(self._settings_mgr.settings.pdf_sources)
            selected_model = self._selected_model
            token_data = dict(self._token_data)
            is_listening = self._is_listening
            prompt_file_name = self._prompt_file_name
            capture_mode = self._settings_mgr.settings.capture_mode
            hotkey = self._settings_mgr.settings.hotkey
            history = self._history_repo.get_all()
            log_level = self._settings_mgr.settings.log_level

        return {
            "pdfSources": pdf_sources,
            "availableModels": self.get_available_models(),
            "selectedModel": selected_model,
            "tokenUsage": token_data,
            "isListening": is_listening,
            "availablePrompts": self._prompt_mgr.list_prompts(),
            "selectedPrompt": prompt_file_name,
            "promptContent": self._prompt_mgr.get_content(prompt_file_name),
            "captureMode": capture_mode,
            "currentHotkey": hotkey,
            "history": history,
            "apiKeys": self.get_api_keys_info(),
            "logLevel": log_level,
        }

    def get_capture_mode(self) -> str:
        return self._settings_mgr.settings.capture_mode

    def set_capture_mode(self, mode: str) -> bool:
        self._settings_mgr.update(capture_mode=mode)
        logger.info("Selected capture mode: %s", mode)
        return True

    def get_history(self) -> list[dict]:
        return self._history_repo.get_all()

    def get_response_time_stats(self) -> dict[str, Any]:
        """Return lazy-calculated average and median response times broken down by model."""
        return self._history_repo.get_response_time_stats()

    def clear_history(self) -> None:
        self._history_repo.clear()

    def delete_history_item(self, item_id: Any) -> bool:
        return self._history_repo.delete_item(item_id)

    def get_log_level(self) -> str:
        return self._settings_mgr.settings.log_level

    def set_log_level(self, level: str) -> bool:
        lvl_upper = level.strip().upper()
        if lvl_upper not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            return False
        self._settings_mgr.update(log_level=lvl_upper)
        set_console_log_level(lvl_upper)
        logger.info("Log level set to %s.", lvl_upper)
        return True


# ------------------------------------------------------------------
# CLI Entry Point
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Screenshot AI — Context-aware Gemini desktop assistant.")
    parser.add_argument("--prompt-file", default="default_prompt.txt", help="Prompt template filename inside prompt_files/.")
    parser.add_argument("--invisible", action="store_true", help="Start directly in listening mode with the GUI window hidden.")
    return parser.parse_args()


def main() -> NoReturn:
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    config = Config(project_root=project_root)
    app = Application(config=config, prompt_file=args.prompt_file, invisible=args.invisible)
    app.run()


if __name__ == "__main__":
    main()
