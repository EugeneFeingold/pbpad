"""Non-interactive status screens and overlays: connection status, the
connecting spinner, sleep, the live Info page, and the lock screen.
"""
from __future__ import annotations
import asyncio
from typing import Callable, Optional

import log
from .base import Screen, ListScreen, Row


class StatusScreen(Screen):
    """Non-blocking status (No WiFi / Finding / No PixelBlaze). Left push opens
    Settings so the user can switch network/device without a connection."""

    def __init__(self, line1: str, line2: str = ""):
        self._l1 = line1
        self._l2 = line2

    def matches(self, line1: str, line2: str) -> bool:
        return self._l1 == line1 and self._l2 == line2

    def render(self, lcd):
        lcd.render_message(self._l1, self._l2, hint="[Enter] Settings")

    async def handle(self, event: tuple) -> str | None:
        if event == ("press", "enc1"):
            return "settings"
        return None


class ConnectingScreen(Screen):
    def render(self, lcd):
        lcd.render_message("Connecting...", "please wait")


class SleepScreen(Screen):
    def render(self, lcd):
        lcd.render_message("Touch any control", "to wake")


class InfoScreen(ListScreen):
    """Live-updating device info page. A background task calls `collect` once
    per second; rows are display-only (no navigation or edits)."""

    def __init__(self, collect: Callable):
        super().__init__()
        self._collect = collect
        self._stats: dict = {}
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Prime with an initial reading, then begin the periodic refresh."""
        try:
            self._stats = await self._collect()
        except Exception as e:
            log.log(log.ERROR, f"info refresh failed: {e}")
        self._task = asyncio.ensure_future(self._refresh_loop())

    async def _refresh_loop(self):
        try:
            while True:
                await asyncio.sleep(1.0)
                try:
                    self._stats = await self._collect()
                except Exception as e:
                    log.log(log.ERROR, f"info refresh failed: {e}")
        except asyncio.CancelledError:
            pass

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def title(self):
        return "Info"

    def rows(self):
        if not self._stats:
            return [Row("Loading...")]
        return [Row(label, value) for label, value in self._stats.items()]

    def _on_back(self):
        return "back_from_info"

    def will_pop(self):
        # Cancel the refresh task when the App dismisses this screen. NOT in
        # _on_back — that method fires per-render as an availability check.
        self.stop()


class LockScreen(Screen):
    def __init__(self):
        self.hint = False

    def render(self, lcd):
        lcd.render_message("Locked", "hold both knobs to unlock" if self.hint else "")
