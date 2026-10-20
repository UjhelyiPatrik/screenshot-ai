"""Gemini API service — handles screenshots, PDF uploads, streaming, and resilient API calls."""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pyautogui
from PIL import Image
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import Config

logger = logging.getLogger(__name__)

# Fallback models ordered by stability and availability
FALLBACK_MODELS = [
    "models/gemini-2.5-flash",
    "models/gemini-2.0-flash",
    "models/gemini-1.5-flash",
]


@dataclass
class QuestionResult:
    """Outcome of processing a question with Gemini."""

    answer: str | None = None
    tokens_used: int = 0
    error_code: str | None = None  # Short 3-4 char badge for tray icon (e.g. 503, 429, NET, ERR)
    error_message: str | None = None  # Human-readable explanation for tooltip & logs
    model_used: str | None = None
    api_key_used: str | None = None

    @property
    def is_success(self) -> bool:
        return self.answer is not None


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Extract a concise badge code (3-4 chars) and human-readable message from an exception."""
    if isinstance(exc, genai_errors.APIError):
        code = exc.code
        if code == 503:
            return "503", "Model busy / high demand"
        if code == 429:
            return "429", "Rate limit / quota exceeded"
        if code in (401, 403):
            return "AUTH", "Invalid API key or unauthorized"
        if code == 404:
            return "404", "Model or resource not found"
        if code == 400:
            return "400", "Bad request / invalid argument"
        return str(code) if code else "API", (exc.message or str(exc))[:80]

    name = exc.__class__.__name__
    msg = str(exc)

    if name in (
        "ConnectionAbortedError",
        "ConnectionResetError",
        "ConnectError",
        "ReadError",
        "ConnectTimeout",
    ):
        return "NET", "Network connection error"
    if "10053" in msg or "connection" in msg.lower() or "aborted" in msg.lower():
        return "NET", "Network connection error"
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return "429", "Rate limit / quota exceeded"
    if "503" in msg or "UNAVAILABLE" in msg:
        return "503", "Model busy / high demand"

    return "ERR", msg[:80] if msg else "Unknown error"


class GeminiService:
    """Encapsulates all interactions with the Gemini generative-AI API.

    Features:
    * High-speed screen capture via mss with JPEG compression.
    * Multiple capture modes: cursor monitor, active window, all screens.
    * Zero-roundtrip inline image transmission (types.Part.from_bytes).
    * Streaming responses (generate_content_stream) for instant first-token display.
    * Thread-safe client pool per API key.
    * Automatic retry and transparent fallback model failover on 503/429.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._api_keys: list[str] = []
        self._last_index: int = -1
        self._uploaded_pdf_cache: dict[str, Any] = {}
        self._clients: dict[str, genai.Client] = {}
        self._cached_models: list[str] | None = None
        self._lock = threading.RLock()

        self._load_api_keys()
        self._init_clients()

    # ------------------------------------------------------------------
    # API-key & Client pool management
    # ------------------------------------------------------------------

    def _load_api_keys(self) -> None:
        path = self._config.api_key_file
        if not path.exists():
            raise FileNotFoundError(f"API key file not found: {path}")

        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            raise ValueError(f"API key file is empty: {path}")

        first = lines[0]
        if first.startswith("#") and "last_index=" in first:
            try:
                self._last_index = int(first.split("last_index=")[1].strip())
            except (ValueError, IndexError):
                self._last_index = -1

        self._api_keys = [ln for ln in lines if not ln.startswith("#")]
        if not self._api_keys:
            raise ValueError(f"No valid API keys in: {path}")

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
            self._config.api_key_file.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to persist last_index: %s", exc)

    def _get_active_client(self) -> tuple[genai.Client, str, int]:
        """Rotate to next API key thread-safely and return its client."""
        with self._lock:
            if not self._api_keys:
                raise ValueError("No API keys available.")
            self._last_index = (self._last_index + 1) % len(self._api_keys)
            key = self._api_keys[self._last_index]
            self._persist_last_index()
            client = self._clients[key]
            logger.info("Using API key #%d.", self._last_index + 1)
            return client, key, self._last_index

    def get_api_keys(self) -> list[str]:
        """Return list of loaded API keys."""
        with self._lock:
            return list(self._api_keys)

    def get_last_key_index(self) -> int:
        """Return index of the most recently used API key."""
        with self._lock:
            return self._last_index

    def add_api_key(self, new_key: str) -> bool:
        """Add a new Gemini API key, initialize its client, and persist to apikeys.txt."""
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

    def remove_api_key(self, key_to_remove: str) -> bool:
        """Remove an API key and update client pool and apikeys.txt."""
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

    # ------------------------------------------------------------------
    # High-speed Screenshot capture (mss + JPEG)
    # ------------------------------------------------------------------

    @staticmethod
    def take_screenshot(mode: str = "cursor_monitor") -> tuple[bytes, str]:
        """Capture screen using mss (or pyautogui fallback) with JPEG compression.

        Modes:
        * 'cursor_monitor': grabs the display containing the mouse cursor.
        * 'active_window': grabs the foreground window's bounding box.
        * 'all_screens': grabs the entire virtual desktop.

        Returns (image_bytes, mime_type).
        """
        t_capture_start = time.perf_counter()
        logger.debug("Starting screen capture (mode='%s') …", mode)

        try:
            import mss

            with mss.MSS() as sct:
                target_box = None

                if mode == "active_window":
                    try:
                        import win32gui

                        hwnd = win32gui.GetForegroundWindow()
                        if hwnd:
                            rect = win32gui.GetWindowRect(hwnd)
                            w = rect[2] - rect[0]
                            h = rect[3] - rect[1]
                            if w > 50 and h > 50:
                                # Find the monitor that contains the center of this window
                                center_x = rect[0] + w // 2
                                center_y = rect[1] + h // 2
                                matched_mon = None
                                monitors = sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors
                                for m in monitors:
                                    if (
                                        m["left"] <= center_x < m["left"] + m["width"]
                                        and m["top"] <= center_y < m["top"] + m["height"]
                                    ):
                                        matched_mon = m
                                        break
                                if matched_mon is None:
                                    matched_mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

                                # Clamp window rect within the matched monitor bounds (handling maximized -8px borders)
                                c_left = max(matched_mon["left"], rect[0])
                                c_top = max(matched_mon["top"], rect[1])
                                c_right = min(matched_mon["left"] + matched_mon["width"], rect[2])
                                c_bottom = min(matched_mon["top"] + matched_mon["height"], rect[3])
                                c_w = max(50, c_right - c_left)
                                c_h = max(50, c_bottom - c_top)

                                target_box = {
                                    "left": c_left,
                                    "top": c_top,
                                    "width": c_w,
                                    "height": c_h,
                                }
                                win_title = win32gui.GetWindowText(hwnd)
                                logger.debug(
                                    "Active window detected: hwnd=%s, title='%s', bounds=(%d,%d,%d,%d), size=%dx%d",
                                    hwnd,
                                    win_title[:40] if win_title else "Untitled",
                                    c_left,
                                    c_top,
                                    c_right,
                                    c_bottom,
                                    c_w,
                                    c_h,
                                )
                    except Exception as exc:
                        logger.debug("Active window detection failed, falling back to cursor monitor: %s", exc)

                if target_box is None and mode in ("cursor_monitor", "active_window"):
                    # Find monitor containing cursor
                    try:
                        cur_x, cur_y = pyautogui.position()
                        monitors = sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors
                        for m in monitors:
                            if (
                                m["left"] <= cur_x < m["left"] + m["width"]
                                and m["top"] <= cur_y < m["top"] + m["height"]
                            ):
                                target_box = m
                                break
                        if target_box:
                            logger.debug(
                                "Cursor detected at (%d, %d) on monitor bounds (left=%d, top=%d, width=%d, height=%d)",
                                cur_x,
                                cur_y,
                                target_box.get("left", 0),
                                target_box.get("top", 0),
                                target_box.get("width", 0),
                                target_box.get("height", 0),
                            )
                    except Exception as exc:
                        logger.debug("Cursor monitor detection failed: %s", exc)

                # Default fallback: entire virtual screen or primary monitor
                if target_box is None:
                    target_box = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                    logger.debug("Using virtual display bounds: %dx%d", target_box.get("width", 0), target_box.get("height", 0))

                sct_img = sct.grab(target_box)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                t_grab = (time.perf_counter() - t_capture_start) * 1000
                logger.debug("Screen grabbed via mss in %.1f ms: resolution=%dx%d", t_grab, img.width, img.height)

        except Exception as exc:
            logger.warning("mss capture failed, falling back to pyautogui: %s", exc)
            img = pyautogui.screenshot()
            logger.debug("Screen grabbed via pyautogui fallback: resolution=%dx%d", img.width, img.height)

        # Encode to JPEG with quality 85 for fast network transmission and small payload
        t_enc_start = time.perf_counter()
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        compressed_bytes = buf.getvalue()
        t_enc = (time.perf_counter() - t_enc_start) * 1000
        ratio = (img.width * img.height * 3) / max(1, len(compressed_bytes))
        logger.debug(
            "Encoded JPEG (q=85) in %.1f ms: %d bytes (%.1f KB, compression ratio: %.1fx)",
            t_enc,
            len(compressed_bytes),
            len(compressed_bytes) / 1024,
            ratio,
        )
        return compressed_bytes, "image/jpeg"

    # ------------------------------------------------------------------
    # PDF upload (with caching)
    # ------------------------------------------------------------------

    def _get_or_upload_pdf(self, client: genai.Client, source: str) -> Any | None:
        with self._lock:
            if source in self._uploaded_pdf_cache:
                logger.debug("Using cached PDF for: %s", source)
                return self._uploaded_pdf_cache[source]

        uploaded = self._upload_pdf(client, source)
        if uploaded is not None:
            with self._lock:
                self._uploaded_pdf_cache[source] = uploaded
        return uploaded

    @staticmethod
    def _upload_pdf(client: genai.Client, source: str) -> Any | None:
        is_url = source.lower().startswith(("http://", "https://"))

        try:
            if is_url:
                import requests

                logger.info("Downloading PDF from %s …", source)
                resp = requests.get(source, stream=True, timeout=30)
                resp.raise_for_status()
                logger.info("Downloaded %d bytes.", len(resp.content))

                uploaded = client.files.upload(
                    file=io.BytesIO(resp.content),
                    config=dict(
                        mime_type="application/pdf",
                        display_name=Path(source).name or "downloaded.pdf",
                    ),
                )
            else:
                local = Path(source)
                if not local.exists():
                    logger.error("Local PDF not found: %s", local)
                    return None
                logger.info("Uploading local PDF: %s", local.name)
                uploaded = client.files.upload(file=local)

            logger.info("PDF uploaded — URI: %s", uploaded.uri)
            return uploaded

        except Exception as exc:
            logger.error("Error uploading PDF %s: %s", source, exc)
            return None

    def invalidate_pdf_cache(self) -> None:
        """Clear the upload cache (call when the PDF source list changes)."""
        with self._lock:
            self._uploaded_pdf_cache.clear()
        logger.debug("PDF upload cache cleared.")

    # ------------------------------------------------------------------
    # Prompt loading
    # ------------------------------------------------------------------

    def _load_prompt(self, prompt_file_name: str) -> str:
        if not prompt_file_name:
            prompt_file_name = "default_prompt.txt"
        if not prompt_file_name.endswith(".txt"):
            prompt_file_name += ".txt"

        path = self._config.prompt_dir / prompt_file_name
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")

        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"Prompt file is empty: {path}")

        logger.info("Loaded prompt: %s", path.name)
        return text

    def list_prompts(self) -> list[str]:
        """Return list of available prompt file names in prompt_files directory."""
        if not self._config.prompt_dir.exists():
            return ["default_prompt.txt"]
        return sorted([p.name for p in self._config.prompt_dir.glob("*.txt")])

    def get_prompt_content(self, prompt_file_name: str) -> str:
        """Read and return content of prompt file."""
        if not prompt_file_name:
            prompt_file_name = "default_prompt.txt"
        if not prompt_file_name.endswith(".txt"):
            prompt_file_name += ".txt"
        path = self._config.prompt_dir / prompt_file_name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def save_prompt(self, prompt_file_name: str, content: str) -> bool:
        """Save new or updated prompt file to prompt_files directory."""
        if not prompt_file_name:
            return False
        clean_name = prompt_file_name.strip()
        if not clean_name.endswith(".txt"):
            clean_name += ".txt"
        path = self._config.prompt_dir / clean_name
        try:
            self._config.prompt_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content.strip(), encoding="utf-8")
            logger.info("Saved prompt preset: %s", path.name)
            return True
        except OSError as exc:
            logger.error("Failed to save prompt %s: %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # Streaming API call with retry and model fallback
    # ------------------------------------------------------------------

    def _call_api_resilient(
        self,
        client: genai.Client,
        contents: list[Any],
        model: str,
        is_cancelled: Callable[[], bool] | None = None,
        on_stream_chunk: Callable[[str], None] | None = None,
    ) -> tuple[str | None, int, str | None, str | None, str]:
        """Call Gemini generate_content_stream with automatic retry and model fallback.

        Returns: (answer, tokens_used, error_code, error_message, final_model_used).
        """
        candidate_models = [model]
        for fallback in FALLBACK_MODELS:
            if fallback != model and fallback not in candidate_models:
                candidate_models.append(fallback)

        last_error_code = "ERR"
        last_error_msg = "Generation failed"
        total_tokens = 0
        final_model = model

        for m_idx, current_model in enumerate(candidate_models):
            if is_cancelled and is_cancelled():
                logger.info("Request cancelled — aborting API calls.")
                return None, total_tokens, "CNCL", "Request cancelled", current_model

            max_attempts = 2 if m_idx == 0 else 1
            for attempt in range(max_attempts):
                if is_cancelled and is_cancelled():
                    return None, total_tokens, "CNCL", "Request cancelled", current_model

                try:
                    logger.info("Calling Gemini API stream (model=%s, attempt=%d) …", current_model, attempt + 1)
                    final_model = current_model

                    response_stream = client.models.generate_content_stream(
                        model=current_model,
                        contents=contents,
                    )

                    t_api_start = time.perf_counter()
                    chunks: list[str] = []
                    chunk_idx = 0
                    first_token_logged = False
                    for chunk in response_stream:
                        if is_cancelled and is_cancelled():
                            return None, total_tokens, "CNCL", "Request cancelled", current_model

                        if hasattr(chunk, "usage_metadata") and hasattr(chunk.usage_metadata, "total_token_count"):
                            total_tokens = chunk.usage_metadata.total_token_count

                        if hasattr(chunk, "text") and chunk.text:
                            chunk_idx += 1
                            if not first_token_logged:
                                ttft = (time.perf_counter() - t_api_start) * 1000
                                logger.debug("First stream token received in %.1f ms (TTFT): '%.30s'", ttft, chunk.text.replace("\n", " "))
                                first_token_logged = True
                            chunks.append(chunk.text)
                            logger.debug("Stream chunk #%d (+%d chars): '%.40s'", chunk_idx, len(chunk.text), chunk.text.replace("\n", " "))
                            if on_stream_chunk:
                                on_stream_chunk("".join(chunks).strip())

                    answer = "".join(chunks).strip()
                    t_stream = (time.perf_counter() - t_api_start) * 1000
                    logger.debug(
                        "Stream completed in %.1f ms (%d chunks, %d chars, %d tokens). Model: %s",
                        t_stream,
                        chunk_idx,
                        len(answer),
                        total_tokens,
                        current_model,
                    )
                    if not answer:
                        logger.warning("Stream ended with empty response text.")
                        last_error_code, last_error_msg = "EMPTY", "API returned empty text"
                        continue

                    if m_idx > 0:
                        logger.info("Fallback model %s succeeded!", current_model)
                    return answer, total_tokens, None, None, current_model

                except Exception as exc:
                    err_code, err_msg = _classify_error(exc)
                    last_error_code, last_error_msg = err_code, err_msg
                    logger.warning(
                        "Model %s attempt %d failed [%s]: %s",
                        current_model,
                        attempt + 1,
                        err_code,
                        err_msg,
                    )

                    if err_code in ("503", "429", "NET") and attempt < max_attempts - 1:
                        time.sleep(0.5)
                        continue
                    break

            if m_idx < len(candidate_models) - 1:
                next_model = candidate_models[m_idx + 1]
                logger.info("Failing over to fallback model: %s …", next_model)

        return None, total_tokens, last_error_code, last_error_msg, final_model

    # ------------------------------------------------------------------
    # High-level: process a question
    # ------------------------------------------------------------------

    def process_question(
        self,
        image_bytes: bytes,
        mime_type: str,
        pdf_sources: list[str],
        model: str,
        prompt_file_name: str,
        is_cancelled: Callable[[], bool] | None = None,
        on_stream_chunk: Callable[[str], None] | None = None,
    ) -> QuestionResult:
        """Process question using inline image bytes (zero File API roundtrips) and streaming."""
        try:
            client, current_key, _idx = self._get_active_client()
        except Exception as exc:
            logger.error("Failed to obtain active client: %s", exc)
            return QuestionResult(error_code="AUTH", error_message="No valid API key available")

        # Zero-roundtrip inline image part
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        contents: list[Any] = [image_part]

        for source in pdf_sources:
            if is_cancelled and is_cancelled():
                return QuestionResult(error_code="CNCL", error_message="Request cancelled", api_key_used=current_key)
            pdf_part = self._get_or_upload_pdf(client, source)
            if pdf_part is not None:
                contents.append(pdf_part)

        try:
            prompt = self._load_prompt(prompt_file_name)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("Prompt error: %s", exc)
            return QuestionResult(error_code="PRMT", error_message=str(exc), api_key_used=current_key)

        contents.append(prompt)

        logger.debug(
            "Dispatching question: image=%d bytes (%s), model=%s, prompt='%s' (%d chars), pdfs=%d, key=#%d",
            len(image_bytes),
            mime_type,
            model,
            prompt_file_name,
            len(prompt),
            len(pdf_sources),
            _idx + 1,
        )

        answer, tokens, err_code, err_msg, final_model = self._call_api_resilient(
            client=client,
            contents=contents,
            model=model,
            is_cancelled=is_cancelled,
            on_stream_chunk=on_stream_chunk,
        )

        return QuestionResult(
            answer=answer,
            tokens_used=tokens,
            error_code=err_code,
            error_message=err_msg,
            model_used=final_model,
            api_key_used=current_key,
        )

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    def list_models(self) -> list[str]:
        """Return names of models that support generateContent (cached after first query)."""
        with self._lock:
            if self._cached_models is not None:
                logger.debug("Returning cached models list (%d models).", len(self._cached_models))
                return list(self._cached_models)

        try:
            client, _key, _idx = self._get_active_client()
            logger.info("Fetching available models …")
            logger.debug("Querying Gemini client API (key #%d) for models list …", _idx + 1)
            models = [
                m.name
                for m in client.models.list()
                if "generateContent" in m.supported_actions
            ]
            logger.info("Found %d model(s).", len(models))
            logger.debug("Discovered generateContent models sample: %s", [m.split("/")[-1] for m in models[:5]])
            with self._lock:
                self._cached_models = models
            return models
        except Exception as exc:
            logger.error("Failed to fetch models: %s", exc)
            return []

    def select_newest_flash_model(self) -> str:
        """Return the name of the newest gemini-*-flash model, or a fallback."""
        fallback = "models/gemini-2.5-flash"
        try:
            models = self.list_models()
            pattern = r"^models/gemini-\d+\.\d+-flash$"
            flash = [m for m in models if re.match(pattern, m)]
            if not flash:
                logger.warning("No flash models found — using fallback: %s", fallback)
                return fallback
            flash.sort(reverse=True)
            name = flash[0]
            logger.info("Selected newest flash model: %s", name)
            return name
        except Exception as exc:
            logger.error("Failed to select flash model: %s", exc)
            return fallback
