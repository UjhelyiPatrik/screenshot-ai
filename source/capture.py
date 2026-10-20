"""Screen capture subsystem using the Strategy design pattern.

Provides modular, extensible display capture strategies for cursor monitor,
active window, and multi-monitor virtual desktop, with automated fallback
and high-performance JPEG compression.
"""

from __future__ import annotations

import io
import logging
import sys
import time
from typing import Protocol, runtime_checkable

import pyautogui
from PIL import Image

logger = logging.getLogger(__name__)


@runtime_checkable
class CaptureStrategy(Protocol):
    """Strategy interface for capturing a screen region as a PIL Image."""

    def capture(self) -> Image.Image:
        """Capture and return the target screen region as an RGB PIL Image."""
        ...


class CursorMonitorCaptureStrategy:
    """Capture the monitor display that currently contains the mouse cursor."""

    def capture(self) -> Image.Image:
        try:
            import mss

            with mss.MSS() as sct:
                cur_x, cur_y = pyautogui.position()
                monitors = sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors
                target_mon = None
                for m in monitors:
                    if (
                        m["left"] <= cur_x < m["left"] + m["width"]
                        and m["top"] <= cur_y < m["top"] + m["height"]
                    ):
                        target_mon = m
                        break

                if target_mon is None:
                    target_mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

                logger.debug(
                    "Cursor detected at (%d, %d) on monitor bounds (left=%d, top=%d, width=%d, height=%d)",
                    cur_x,
                    cur_y,
                    target_mon.get("left", 0),
                    target_mon.get("top", 0),
                    target_mon.get("width", 0),
                    target_mon.get("height", 0),
                )
                sct_img = sct.grab(target_mon)
                return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        except Exception as exc:
            logger.warning("mss cursor monitor capture failed, falling back to pyautogui: %s", exc)
            return pyautogui.screenshot()


class ActiveWindowCaptureStrategy:
    """Capture the foreground window bounding box, clamped to the containing monitor."""

    def capture(self) -> Image.Image:
        target_box = None
        if sys.platform == "win32":
            try:
                import win32gui
                import mss

                hwnd = win32gui.GetForegroundWindow()
                if hwnd:
                    rect = win32gui.GetWindowRect(hwnd)
                    w = rect[2] - rect[0]
                    h = rect[3] - rect[1]
                    if w > 50 and h > 50:
                        with mss.MSS() as sct:
                            center_x = rect[0] + w // 2
                            center_y = rect[1] + h // 2
                            monitors = sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors
                            matched_mon = None
                            for m in monitors:
                                if (
                                    m["left"] <= center_x < m["left"] + m["width"]
                                    and m["top"] <= center_y < m["top"] + m["height"]
                                ):
                                    matched_mon = m
                                    break
                            if matched_mon is None:
                                matched_mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

                            # Clamp window rect within matched monitor bounds (handling maximized -8px borders)
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
                            sct_img = sct.grab(target_box)
                            return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

            except Exception as exc:
                logger.debug("Active window capture failed, falling back to cursor monitor: %s", exc)

        # Fallback to cursor monitor capture if window detection failed or platform is non-Windows
        return CursorMonitorCaptureStrategy().capture()


class AllScreensCaptureStrategy:
    """Capture the entire virtual multi-monitor desktop."""

    def capture(self) -> Image.Image:
        try:
            import mss

            with mss.MSS() as sct:
                target_box = sct.monitors[0]  # Monitor 0 represents all monitors combined
                logger.debug("Capturing all virtual screens: %dx%d", target_box.get("width", 0), target_box.get("height", 0))
                sct_img = sct.grab(target_box)
                return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        except Exception as exc:
            logger.warning("mss all-screens capture failed, falling back to pyautogui: %s", exc)
            return pyautogui.screenshot()


class ScreenCaptureService:
    """Context for screen capture strategies that handles encoding and compression."""

    _STRATEGIES: dict[str, CaptureStrategy] = {
        "cursor_monitor": CursorMonitorCaptureStrategy(),
        "active_window": ActiveWindowCaptureStrategy(),
        "all_screens": AllScreensCaptureStrategy(),
    }

    @classmethod
    def register_strategy(cls, mode_name: str, strategy: CaptureStrategy) -> None:
        """Register a custom capture strategy, honoring the Open/Closed Principle."""
        cls._STRATEGIES[mode_name] = strategy
        logger.debug("Registered custom screen capture strategy: '%s'", mode_name)

    @classmethod
    def get_strategy(cls, mode: str) -> CaptureStrategy:
        """Resolve capture strategy by mode name, defaulting to CursorMonitorCaptureStrategy."""
        return cls._STRATEGIES.get(mode, cls._STRATEGIES["cursor_monitor"])

    @classmethod
    def capture(cls, mode: str = "cursor_monitor", quality: int = 85) -> tuple[bytes, str]:
        """Capture screen according to *mode* and return JPEG (bytes, mime_type)."""
        t_start = time.perf_counter()
        logger.debug("Starting screen capture (mode='%s') …", mode)

        strategy = cls.get_strategy(mode)
        img = strategy.capture()

        t_grab = (time.perf_counter() - t_start) * 1000
        logger.debug("Screen captured in %.1f ms: resolution=%dx%d", t_grab, img.width, img.height)

        t_enc_start = time.perf_counter()
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        compressed_bytes = buf.getvalue()
        t_enc = (time.perf_counter() - t_enc_start) * 1000

        ratio = (img.width * img.height * 3) / max(1, len(compressed_bytes))
        logger.debug(
            "Encoded JPEG (q=%d) in %.1f ms: %d bytes (%.1f KB, compression ratio: %.1fx)",
            quality,
            t_enc,
            len(compressed_bytes),
            len(compressed_bytes) / 1024,
            ratio,
        )

        return compressed_bytes, "image/jpeg"
