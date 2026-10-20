"""Gemini API service — handles resilient streaming completions, fallback chains, and model discovery."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pyautogui
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import capture
from capture import ScreenCaptureService
from client_pool import ApiClientPool
from config import Config
from prompt_manager import PromptManager

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
    * Zero-roundtrip inline image transmission (types.Part.from_bytes).
    * Streaming responses (generate_content_stream) with TTFT metric recording.
    * Client pool rotation and key management via ApiClientPool.
    * Automatic retry and transparent fallback model failover on 503/429.
    * Screen capture delegation via ScreenCaptureService.
    * Prompt management delegation via PromptManager.
    """

    @staticmethod
    def take_screenshot(mode: str = "cursor_monitor") -> tuple[bytes, str]:
        """Backwards-compatible delegation to ScreenCaptureService with mock synchronization."""
        if getattr(pyautogui, "screenshot", None) != getattr(capture.pyautogui, "screenshot", None):
            capture.pyautogui.screenshot = pyautogui.screenshot
        return ScreenCaptureService.capture(mode=mode)

    def __init__(self, config: Config, client_pool: ApiClientPool | None = None) -> None:
        self._config = config
        self._prompt_manager = PromptManager(config.prompt_dir)
        self._client_pool = client_pool or ApiClientPool(config.api_key_file)
        self._uploaded_pdf_cache: dict[str, Any] = {}
        self._cached_models: list[str] | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Backwards-compatible properties and client pool delegation
    # ------------------------------------------------------------------

    @property
    def _clients(self) -> dict[str, genai.Client]:
        return self._client_pool.clients

    @property
    def _api_keys(self) -> list[str]:
        return self._client_pool._api_keys

    @property
    def _last_index(self) -> int:
        return self._client_pool._last_index

    @_last_index.setter
    def _last_index(self, val: int) -> None:
        self._client_pool._last_index = val

    def _get_active_client(self) -> tuple[genai.Client, str, int]:
        return self._client_pool.get_active_client()

    def get_api_keys(self) -> list[str]:
        return self._client_pool.get_keys()

    def get_last_key_index(self) -> int:
        return self._client_pool.get_last_index()

    def add_api_key(self, new_key: str) -> bool:
        return self._client_pool.add_key(new_key)

    def remove_api_key(self, key_to_remove: str) -> bool:
        return self._client_pool.remove_key(key_to_remove)

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
                logger.info("Uploading PDF to Gemini from stream …")
                uploaded = client.files.upload(
                    file=resp.raw,
                    config=types.UploadFileConfig(mime_type="application/pdf"),
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
    # Prompt delegation (Service Pattern)
    # ------------------------------------------------------------------

    def list_prompts(self) -> list[str]:
        return self._prompt_manager.list_prompts()

    def get_prompt_content(self, prompt_file_name: str) -> str:
        return self._prompt_manager.get_content(prompt_file_name)

    def save_prompt(self, prompt_file_name: str, content: str) -> bool:
        return self._prompt_manager.save_prompt(prompt_file_name, content)

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
            prompt = self._prompt_manager.load_prompt(prompt_file_name)
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
