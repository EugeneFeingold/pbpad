"""Text measurement, fitting, wrapping, and THE single text-drawing primitive.

Measurement means rendering a scratch image and scanning it in Python, so it's
expensive on a Pi Zero; everything here is memoised aggressively. `_draw_text`
is the only place in the whole LCD stack that issues a PIL text draw — every
string on the OLED is truncated to a hard pixel budget before it is drawn,
which is what makes overflow into a neighbouring element structurally
impossible.
"""
from PIL import Image, ImageDraw

from hardware.lcd_layout import _PAD_L, _MEASURE_CACHE_MAX


class LCDTextMixin:
    @property
    def body_width(self) -> int:
        """Pixel budget for a centered message body line (matches the width
        render_message lays text into). For screens that pre-size a field."""
        return self._w - 2 * _PAD_L

    def fit_tail(self, text, max_px):
        """Return the RIGHTMOST run of `text` that fits `max_px`.

        Entry fields scroll to keep the caret in view as you type past the
        edge, so they need to keep the tail — _fit_text keeps the head."""
        if max_px <= 0 or not text:
            return ""
        if self._ink_right(text) <= max_px:
            return text
        lo, hi = 0, len(text)          # smallest start whose tail fits
        while lo < hi:
            mid = (lo + hi) // 2
            if self._ink_right(text[mid:]) <= max_px:
                hi = mid
            else:
                lo = mid + 1
        return text[lo:]

    def _tw(self, font, text: str) -> int:
        try:
            return int(font.getlength(text))
        except AttributeError:
            return font.getsize(text)[0]

    def _measure_cached(self, text: str) -> int:
        """Memoised _ink_right for the default font.

        Measuring means rendering a scratch image and scanning it in Python —
        ~0.6ms per call on a desktop, tens of ms on a Pi Zero. The UI measures
        the same handful of strings every frame, so without this the OLED
        render alone saturated the CPU (measured at ~250% of a Zero core) and
        starved input handling. Cache is cleared wholesale when it grows past
        a sane bound; the working set is a few dozen strings."""
        hit = self._ink_cache.get(text)
        if hit is None:
            hit = self._ink_right_uncached(text, self._font)
            if len(self._ink_cache) > _MEASURE_CACHE_MAX:
                self._ink_cache.clear()
            self._ink_cache[text] = hit
        return hit

    def _ink_right(self, text: str, font=None) -> int:
        if not text:
            return 0
        if font is None or font is self._font:
            return self._measure_cached(text)
        return self._ink_right_uncached(text, font)

    def _ink_right_uncached(self, text: str, font=None) -> int:
        """One past the rightmost ink column when `text` is drawn at origin.

        Used for right-alignment against a hard boundary (drill glyph, arrow).
        Measured by actually rendering into a scratch image and scanning —
        both getlength() (advance width) and getbbox()[2] under-report for
        Pillow's default bitmap font, so callers that trusted them would
        overshoot the boundary. Rendering is cheap: ~1 mono allocation per
        distinct value per frame.

        Semantic: if `_ink_right(text)` returns N, drawing at x=X places the
        rightmost lit pixel at column X + N - 1."""
        if not text:
            return 0
        if font is None:
            font = self._font
        est = self._tw(font, text) + 16    # generous margin for overhang
        img = Image.new("1", (max(est, 8), self._font_h + 4))
        d = ImageDraw.Draw(img)
        d.text((0, 0), text, font=font, fill=1)
        px = img.load()
        w, h = img.size
        for x in range(w - 1, -1, -1):
            for y in range(h):
                if px[x, y]:
                    return x + 1
        return 0

    def _fit_text(self, val, maxw):
        """Truncate val so its ink width is <= maxw px. Uses ink width (not
        advance) because that's what determines whether the last glyph's
        pixels stay inside the boundary — see _ink_right for why.

        Memoised on (text, budget): the UI re-fits the same strings every
        frame, and the search below is the single most expensive thing the
        renderer does."""
        if maxw <= 0 or not val:
            return ""
        key = (val, maxw)
        hit = self._fit_cache.get(key)
        if hit is None:
            hit = self._fit_text_uncached(val, maxw)
            if len(self._fit_cache) > _MEASURE_CACHE_MAX:
                self._fit_cache.clear()
            self._fit_cache[key] = hit
        return hit

    def _fit_text_uncached(self, val, maxw):
        if self._ink_right(val) <= maxw:
            return val
        prefix = suffix = ""
        core = val
        if core.startswith("< "):
            prefix, core = "< ", core[2:]
        for s in (" ->", " >"):
            if core.endswith(s):
                suffix, core = s, core[:-len(s)]
                break
        # Binary search the cut point: O(log n) measurements instead of one
        # per character. Trimming a long name was ~25 renders (14ms) before.
        lo, hi, best = 0, len(core), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._ink_right(prefix + core[:mid] + "." + suffix) <= maxw:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        core = core[:best]
        return (prefix + core + "." + suffix) if core else (prefix.strip() + suffix.strip())

    def _wrap_text(self, text, max_px, max_lines=3):
        """Split `text` into at most `max_lines` lines that each fit `max_px`.

        Message screens used to draw a single line and let anything too long
        run off the display ("hold both knobs to unlock" lost the last word).
        Wrapping at word boundaries keeps the whole message readable; a single
        word too wide to fit is still hard-truncated by _fit_text."""
        if not text:
            return []
        key = ("wrap", text, max_px, max_lines)
        hit = self._fit_cache.get(key)
        if hit is not None:
            return hit
        if self._ink_right(text) <= max_px:
            lines = [text]
        else:
            lines, cur = [], ""
            for word in text.split():
                trial = f"{cur} {word}".strip()
                if not cur or self._ink_right(trial) <= max_px:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = word
                    if len(lines) >= max_lines:
                        break
            if cur and len(lines) < max_lines:
                lines.append(cur)
            lines = [self._fit_text(ln, max_px) for ln in lines[:max_lines]]
        if len(self._fit_cache) > _MEASURE_CACHE_MAX:
            self._fit_cache.clear()
        self._fit_cache[key] = lines
        return lines

    def _draw_text(self, draw, text, y, *, max_px, fg="white", bold=False,
                   left=None, right=None, center=None) -> int:
        """THE single text-drawing primitive — the only place in this module
        that calls draw.text().

        Every caller MUST supply `max_px`, a hard pixel budget; the string is
        truncated to fit before it is drawn. That is what makes overflow into
        a neighbouring element (the >> glyph, an arrow, the battery badge)
        structurally impossible rather than something each call site has to
        remember. test_lcd.py enforces that no other draw.text() exists.

        Exactly one of left / right / center positions the text. Returns the
        ink width actually drawn, so callers can lay out what follows."""
        text = self._fit_text(text or "", max_px)
        if not text:
            return 0
        w = self._ink_right(text) + (1 if bold else 0)
        if left is not None:
            x = left
        elif right is not None:
            x = right - w
        else:
            x = max(_PAD_L, center - w // 2)
        draw.text((x, y), text, font=self._font, fill=fg)
        if bold:  # faux-bold: overprint 1px to the right
            draw.text((x + 1, y), text, font=self._font, fill=fg)
        return w
