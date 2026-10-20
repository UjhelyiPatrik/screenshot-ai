"""System-tray icon for displaying short answers and status."""

from __future__ import annotations

import logging
import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw, ImageFont
from pystray import MenuItem

logger = logging.getLogger(__name__)


class TrayIcon:
    """Persistent system-tray icon that is created once and updated in-place."""

    _ICON_SIZE = 64

    def __init__(
        self,
        quit_callback: Callable[[], None],
        show_gui_callback: Callable[[], None],
    ) -> None:
        self._quit_callback = quit_callback
        self._show_gui_callback = show_gui_callback

        self._icon = pystray.Icon(
            "Screenshot AI",
            icon=self._create_image("RDY", color="green"),
            title="Ready",
            menu=pystray.Menu(
                MenuItem("Toggle GUI", lambda _icon, _item: self._show_gui_callback()),
                MenuItem("Quit", lambda _icon, _item: self._quit_callback()),
            ),
        )

        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
        logger.info("Tray icon started.")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def display_answer(
        self,
        answer: str,
        color: str = "black",
        title: str | None = None,
    ) -> None:
        """Update the tray icon with *answer* badge and optional hover tooltip *title*."""
        with self._lock:
            display_title = title or answer
            if len(display_title) > 120:
                display_title = display_title[:120] + "..."

            # For badge text: ensure it fits cleanly inside the 64x64 icon
            badge = answer.strip()
            if len(badge) > 6:
                words = badge.split()
                if words and len(words[0]) <= 6:
                    badge = words[0]
                else:
                    badge = badge[:5] + "…"

            try:
                self._icon.icon = self._create_image(badge, color=color)
                self._icon.title = display_title
            except Exception as exc:
                logger.warning("Error updating tray icon: %s", exc)

    def set_loading(self) -> None:
        """Show a "loading" indicator in the tray."""
        with self._lock:
            try:
                self._icon.icon = self._create_image("...", color="navy")
                self._icon.title = "Processing question …"
            except Exception as exc:
                logger.warning("Error setting tray loading: %s", exc)

    def stop(self) -> None:
        """Stop the tray icon and its thread."""
        try:
            self._icon.stop()
            logger.info("Tray icon stopped.")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _create_image(self, text: str, color: str = "black") -> Image.Image:
        """Render a small square icon with centered *text* on a *color* background.

        Dynamically adjusts font size so short answers and 3-4 character error
        badges (e.g. 503, 429, NET, ERR) fit neatly inside the icon.
        """
        size = self._ICON_SIZE
        image = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, size, size), fill=color)

        # Scale font down from 32 until text fits with padding
        font_size = 32
        font = None
        text_w, text_h = 0, 0

        while font_size >= 12:
            try:
                candidate_font = ImageFont.truetype("arial.ttf", font_size)
            except IOError:
                candidate_font = ImageFont.load_default()
                font = candidate_font
                bbox = draw.textbbox((0, 0), text, font=font)
                text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
                break

            bbox = draw.textbbox((0, 0), text, font=candidate_font)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if w <= size - 6 and h <= size - 6:
                font = candidate_font
                text_w, text_h = w, h
                break
            font_size -= 2

        if font is None:
            font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

        draw.text(
            ((size - text_w) / 2, (size - text_h) / 2),
            text,
            fill="white",
            font=font,
        )
        return image