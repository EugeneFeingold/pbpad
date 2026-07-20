"""Reusable UI widgets shared by several screens: the velocity-sensitive
encoder stepper and the horizontal text scroller.
"""
from __future__ import annotations
import time

from conf import config


class _VelocityStep:
    """Velocity-sensitive step on a 0-1 float, expressed in `scale` units.

    Slow rotation moves by `slow` units; fast rotation snaps to the next
    multiple of `fast` units. Defaults are percentage-style (0-100, snap-5).
    """
    def __init__(self, scale: int = 100, slow: int = 1, fast: int = 5,
                 threshold: float = config.ENCODER_VELOCITY_THRESHOLD):
        self._scale = scale
        self._slow = slow
        self._fast = fast
        self._threshold = threshold
        self._last = 0.0

    def apply(self, value: float, direction: int) -> float:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if dt < self._threshold:
            u = round(value * self._scale)
            if direction > 0:
                snapped = (u // self._fast + 1) * self._fast
            else:
                snapped = (u // self._fast) * self._fast
                if snapped == u:
                    snapped -= self._fast
            return max(0, min(self._scale, snapped)) / self._scale
        return max(0.0, min(1.0, value + direction * (self._slow / self._scale)))


class _Scroller:
    """Returns the visible slice of a string too wide to fit `max_px` pixels
    at once, animating a scroll right then left with pauses at each end.

    `measure(s)` returns the pixel width of `s` in the target font. Pixel-based
    (not char-based) because character count is a poor proxy: "Rainbow Melt"
    is 12 chars but overflows a slot that comfortably fits 12 "iiiiiiiiiii"s."""

    def __init__(self, pause_start: float = 2.0, pause_end: float = 1.0, speed: float = 0.3):
        self._pause_start = pause_start
        self._pause_end = pause_end
        self._speed = speed  # seconds per character
        self._text = ""
        self._t0 = 0.0

    @staticmethod
    def _longest_prefix_that_fits(text: str, start: int, max_px: int, measure) -> str:
        """Longest text[start:start+n] whose ink fits in max_px."""
        n = len(text) - start
        while n > 0 and measure(text[start:start + n]) > max_px:
            n -= 1
        return text[start:start + n]

    def get(self, text: str, max_px: int, measure) -> str:
        if text != self._text:
            self._text = text
            self._t0 = time.monotonic()
        if measure(text) <= max_px:
            return text
        # Doesn't fit — animate a scroll by character offset. max_offset is
        # the smallest offset at which the entire remaining tail fits (so
        # we stop scrolling once the end is visible, rather than continuing
        # to slide off into just the last character).
        max_offset = len(text) - 1
        for offset in range(len(text)):
            if measure(text[offset:]) <= max_px:
                max_offset = offset
                break
        scroll_time = max_offset * self._speed
        cycle = self._pause_start + scroll_time + self._pause_end + scroll_time
        t = (time.monotonic() - self._t0) % cycle
        if t < self._pause_start:
            offset = 0
        elif t < self._pause_start + scroll_time:
            offset = min(int((t - self._pause_start) / self._speed), max_offset)
        elif t < self._pause_start + scroll_time + self._pause_end:
            offset = max_offset
        else:
            t_back = t - self._pause_start - scroll_time - self._pause_end
            offset = max(0, max_offset - int(t_back / self._speed))
        return self._longest_prefix_that_fits(text, offset, max_px, measure)
