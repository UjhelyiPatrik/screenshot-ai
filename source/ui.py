"""Desktop UI backed by pywebview (no Flask dependency)."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

import webview

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# UI Bridge Protocol & Dataclass (Bridge Pattern)
# ------------------------------------------------------------------

@runtime_checkable
class AppControllerProtocol(Protocol):
    """Protocol defining the interface required by the desktop webview UI."""

    def start_listening(self) -> bool: ...
    def stop_listening(self) -> None: ...
    def get_pdf_sources(self) -> list[str]: ...
    def set_pdf_sources(self, sources: list[str]) -> None: ...
    def get_available_models(self) -> list[str]: ...
    def get_selected_model(self) -> str | None: ...
    def set_selected_model(self, model: str) -> None: ...
    def get_token_usage(self) -> dict: ...
    def quit_app(self) -> None: ...
    def toggle_ui_visibility(self) -> None: ...
    def get_available_prompts(self) -> list[str]: ...
    def get_selected_prompt(self) -> str: ...
    def set_selected_prompt(self, prompt_name: str) -> None: ...
    def get_prompt_content(self, prompt_name: str) -> str: ...
    def save_prompt(self, prompt_name: str, content: str) -> bool: ...
    def get_capture_mode(self) -> str: ...
    def set_capture_mode(self, mode: str) -> bool: ...
    def get_hotkey(self) -> str: ...
    def set_hotkey(self, hotkey_str: str) -> bool: ...
    def get_history(self) -> list[dict]: ...
    def clear_history(self) -> None: ...
    def delete_history_item(self, item_id: Any) -> bool: ...
    def open_history_image(self, image_path: str) -> bool: ...
    def get_history_image_base64(self, image_path: str) -> str | None: ...
    def get_api_keys_info(self) -> list[dict]: ...
    def add_api_key(self, key: str) -> bool: ...
    def remove_api_key(self, key: str) -> bool: ...
    def get_log_level(self) -> str: ...
    def set_log_level(self, level: str) -> bool: ...
    def get_response_time_stats(self) -> dict: ...
    def get_initial_state(self) -> dict: ...


@dataclass
class AppCallbacks:
    """Strongly-typed callbacks the UI uses to communicate with the application."""

    start_listening: Callable[[], bool]
    stop_listening: Callable[[], None]
    get_pdf_sources: Callable[[], list[str]]
    set_pdf_sources: Callable[[list[str]], None]
    get_available_models: Callable[[], list[str]]
    get_selected_model: Callable[[], str | None]
    set_selected_model: Callable[[str], None]
    get_token_usage: Callable[[], dict]
    quit_app: Callable[[], None]
    toggle_ui_visibility: Callable[[], None]
    get_available_prompts: Callable[[], list[str]]
    get_selected_prompt: Callable[[], str]
    set_selected_prompt: Callable[[str], None]
    get_prompt_content: Callable[[str], str]
    save_prompt: Callable[[str, str], bool]
    get_capture_mode: Callable[[], str]
    set_capture_mode: Callable[[str], bool]
    get_hotkey: Callable[[], str]
    set_hotkey: Callable[[str], bool]
    get_history: Callable[[], list[dict]]
    clear_history: Callable[[], None]
    delete_history_item: Callable[[Any], bool]
    open_history_image: Callable[[str], bool]
    get_history_image_base64: Callable[[str], str | None]
    get_api_keys_info: Callable[[], list[dict]]
    add_api_key: Callable[[str], bool]
    remove_api_key: Callable[[str], bool]
    get_log_level: Callable[[], str]
    set_log_level: Callable[[str], bool]
    get_response_time_stats: Callable[[], dict]
    get_initial_state: Callable[[], dict]


# ------------------------------------------------------------------
# UI class
# ------------------------------------------------------------------

class UI:
    """Pywebview-based desktop window that serves ``ui.html`` directly from disk.

    Receives log messages via *log_queue* and forwards them to the JS
    ``appendLog()`` function, preserving ANSI colour codes for rendering.
    """

    def __init__(
        self,
        callbacks: AppCallbacks | AppControllerProtocol,
        log_queue: queue.Queue[str | None],
        hidden: bool = False,
        listening: bool = False,
    ) -> None:
        self.window: webview.Window | None = None
        self._callbacks = callbacks
        self._log_queue = log_queue
        self._log_thread: threading.Thread | None = None
        self._log_thread_running = False
        self._initial_listening = listening

        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")

        self.window = webview.create_window(
            "Screenshot AI",
            url=html_path,
            width=940,
            height=740,
            resizable=True,
            hidden=hidden,
        )

        # ----- Expose to JS --------------------------------------------------
        # Named wrappers guarantee that func.__name__ matches what JS expects.

        def start_listening() -> bool:
            return callbacks.start_listening()

        def stop_listening() -> None:
            callbacks.stop_listening()

        def set_pdf_sources(sources: list[str]) -> None:
            callbacks.set_pdf_sources(sources)

        def set_selected_model(model: str) -> None:
            callbacks.set_selected_model(model)

        def set_selected_prompt(prompt_name: str) -> None:
            callbacks.set_selected_prompt(prompt_name)

        def get_prompt_content(prompt_name: str) -> str:
            return callbacks.get_prompt_content(prompt_name)

        def save_prompt(prompt_name: str, content: str) -> bool:
            return callbacks.save_prompt(prompt_name, content)

        def set_capture_mode(mode: str) -> bool:
            return callbacks.set_capture_mode(mode)

        def set_hotkey(hotkey_str: str) -> bool:
            return callbacks.set_hotkey(hotkey_str)

        def clear_history() -> None:
            callbacks.clear_history()

        def delete_history_item(item_id: Any) -> bool:
            return callbacks.delete_history_item(item_id)

        def open_history_image(image_path: str) -> bool:
            return callbacks.open_history_image(image_path)

        def get_history_image_base64(image_path: str) -> str | None:
            return callbacks.get_history_image_base64(image_path)

        def get_api_keys_info() -> list[dict]:
            return callbacks.get_api_keys_info()

        def add_api_key(key: str) -> bool:
            return callbacks.add_api_key(key)

        def remove_api_key(key: str) -> bool:
            return callbacks.remove_api_key(key)

        def get_log_level() -> str:
            return callbacks.get_log_level()

        def set_log_level(level: str) -> bool:
            return callbacks.set_log_level(level)

        def get_response_time_stats() -> dict:
            return callbacks.get_response_time_stats()

        def toggle_ui_visibility() -> None:
            callbacks.toggle_ui_visibility()

        def get_initial_state() -> dict:
            return callbacks.get_initial_state()

        self.window.expose(
            self.browse_pdf,
            start_listening,
            stop_listening,
            set_pdf_sources,
            set_selected_model,
            set_selected_prompt,
            get_prompt_content,
            save_prompt,
            set_capture_mode,
            set_hotkey,
            clear_history,
            delete_history_item,
            open_history_image,
            get_history_image_base64,
            get_api_keys_info,
            add_api_key,
            remove_api_key,
            get_log_level,
            set_log_level,
            get_response_time_stats,
            toggle_ui_visibility,
            get_initial_state,
        )

    # ------------------------------------------------------------------
    # File dialog
    # ------------------------------------------------------------------

    def browse_pdf(self) -> str | None:
        """Open a native file dialog and return the selected PDF path."""
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("PDF Files (*.pdf)",),
                allow_multiple=False,
            )
            if result:
                return result[0]
            logger.info("No PDF file selected.")
            return None
        except Exception as exc:
            logger.error("File dialog error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_ui(self) -> None:
        """Start the webview event loop (blocks until the window is closed)."""
        logger.info("Starting UI …")

        self._log_thread_running = True
        self._log_thread = threading.Thread(target=self._pump_logs, daemon=True)
        self._log_thread.start()

        self.window.events.closed += self._on_closed
        self.window.events.shown += self._on_shown

        logger.info("Entering webview event loop …")
        webview.start()

        # Cleanup after the window closes
        logger.info("Webview loop ended.")
        self._log_thread_running = False
        if self._log_thread and self._log_thread.is_alive():
            self._log_queue.put(None)
            self._log_thread.join(timeout=2)

    # ------------------------------------------------------------------
    # Webview event handlers
    # ------------------------------------------------------------------

    def _on_shown(self) -> None:
        logger.info("Window shown — syncing initial state.")
        try:
            state = self._callbacks.get_initial_state()
            state["isListening"] = self._initial_listening
            self.window.evaluate_js(
                f"if (typeof setInitialState === 'function') setInitialState({json.dumps(state)});"
            )
            if self._initial_listening:
                logger.info("Auto-starting listening mode.")
                self._callbacks.start_listening()
        except Exception as exc:
            logger.error("Error sending initial state to JS: %s", exc)

    def _on_closed(self) -> None:
        logger.info("Window closed.")
        self._callbacks.quit_app()

    # ------------------------------------------------------------------
    # Log forwarding
    # ------------------------------------------------------------------

    def _pump_logs(self) -> None:
        """Read log lines from the queue and push them to the JS log pane."""
        while self._log_thread_running:
            try:
                line = self._log_queue.get(timeout=0.5)
                if line is None:
                    break
                if self.window:
                    self.window.evaluate_js(f"appendLog({json.dumps(line)})")
            except queue.Empty:
                continue
            except Exception:
                pass  # window may be closing

    def _eval_js_async(self, script: str) -> None:
        """Execute JavaScript asynchronously without blocking caller thread."""
        if not self.window:
            return

        def _runner():
            try:
                if self.window:
                    self.window.evaluate_js(script)
            except Exception as exc:
                logger.debug("Async evaluate_js error: %s", exc)

        threading.Thread(target=_runner, daemon=True).start()

    def update_token_usage(self, total: int, today: int) -> None:
        self._eval_js_async(f"updateTokenDisplay({total}, {today})")

    def update_ui_state(self, state: str) -> None:
        self._eval_js_async(f"setUIState({json.dumps(state)})")

    def add_history_entry(self, entry: dict) -> None:
        """Push a newly finished query record into the UI history list asynchronously."""
        self._eval_js_async(f"addHistoryItem({json.dumps(entry)})")

    def show(self) -> None:
        if self.window:
            self.window.show()
            logger.info("Window shown.")

    def hide(self) -> None:
        if self.window:
            self.window.hide()
            logger.info("Window hidden.")

    def destroy(self) -> None:
        if self.window:
            try:
                self.window.destroy()
                logger.info("Window destroyed.")
            except Exception as exc:
                logger.error("Error destroying window: %s", exc)
            self.window = None