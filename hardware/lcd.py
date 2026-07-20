import threading
from contextlib import contextmanager
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from PIL import Image, ImageDraw, ImageFont
import config

# Row layout, in pixels. Kept here so _split_row / row_value_budget and the
# drawing code can never disagree about how much space an element occupies.
_PAD_L = 2        # left margin for row text
_PAD_R = 5        # right margin on rows with no trailing glyph
_GAP = 3          # gap between label and value
_GLYPH_W = 8      # width of the trailing >> / triangle zone
_GLYPH_GAP = 4    # gap between text and that glyph
_ARROW_W = 5      # width of a nav triangle
_ARROW_PAD = 8    # space between a label and a pinned (fixed_arrows) triangle,
                  # so the arrow doesn't read as touching the label text

# Text measurement is expensive (renders + scans a scratch image), so results
# are memoised. The UI's working set is a few dozen strings; the bound just
# stops unbounded growth from e.g. a long list of SSIDs.
_MEASURE_CACHE_MAX = 512

# 4x6 pixel digit font — crisp on the 1-bit OLED, where small TrueType is mushy.
_DIGIT_W, _DIGIT_H, _DIGIT_GAP = 4, 6, 1
_DIGITS = {
    "0": (".11.", "1..1", "1..1", "1..1", "1..1", ".11."),
    "1": (".1..", "11..", ".1..", ".1..", ".1..", "111."),
    "2": (".11.", "1..1", "..1.", ".1..", "1...", "1111"),
    "3": ("111.", "...1", ".11.", "...1", "1..1", ".11."),
    "4": ("..1.", ".11.", "1.1.", "1111", "..1.", "..1."),
    "5": ("1111", "1...", "111.", "...1", "1..1", ".11."),
    "6": (".11.", "1...", "111.", "1..1", "1..1", ".11."),
    "7": ("1111", "...1", "..1.", "..1.", ".1..", ".1.."),
    "8": (".11.", "1..1", ".11.", "1..1", "1..1", ".11."),
    "9": (".11.", "1..1", "1..1", ".111", "...1", ".11."),
    "/": ("...1", "...1", "..1.", "..1.", ".1..", "1..."),
}


class LCD:
    def __init__(self):
        serial = i2c(port=1, address=config.OLED_I2C_ADDRESS)
        self._device = sh1106(serial)
        self._font = ImageFont.load_default()

        try:
            font_h = self._font.getbbox("Xy")[3]
        except AttributeError:
            font_h = self._font.getsize("X")[1]

        # SH1106 controller has 132 physical columns; only 128 are visible
        self._w = 128
        self._font_h = font_h

        self._battery = ""  # small SoC number drawn in the top-right corner
        self._ink_cache: dict = {}   # text -> ink width (see _measure_cached)
        self._fit_cache: dict = {}   # (text, budget) -> truncated text
        self._render_key = None      # content of the last frame we drew
        self._hidden = False
        self._last_frame = None  # bytes of the last pushed frame (skip redundant I2C)
        self._lock = threading.Lock()  # LCD is written from the button thread too

    @contextmanager
    def _frame(self):
        """Draw into an off-screen buffer and push to the OLED only if it
        actually changed — a full I2C flush is ~60ms, so skipping unchanged
        frames keeps the UI responsive."""
        with self._lock:
            img = Image.new(self._device.mode, self._device.size)
            draw = ImageDraw.Draw(img)
            yield draw
            data = img.tobytes()
            if data != self._last_frame:
                self._last_frame = data
                self._device.display(img)

    def _unchanged(self, key) -> bool:
        """True if `key` describes the frame already on screen.

        The scroll loop calls render ~12x/s regardless of whether anything
        moved. Drawing a frame costs real time (PIL text rendering dominates),
        so comparing a cheap content key first turns an idle redraw into a
        tuple compare. _frame() also skips the I2C flush for identical pixels,
        but by then the expensive drawing has already happened."""
        if key == self._render_key:
            return True
        self._render_key = key
        return False

    def _battery_left_edge(self, text: str) -> int:
        """x of the battery badge's left edge (or the display edge when there's
        no badge). Lets the header title be budgeted against the real glyph
        width instead of a character count."""
        digits = [c for c in text if c in _DIGITS]
        if not digits:
            return self._w
        tw = len(digits) * (_DIGIT_W + _DIGIT_GAP) - _DIGIT_GAP
        return (self._w - 1) - 2 - (tw + 2 * 2)   # nub + body padding

    def _draw_battery(self, draw, text: str, right_x: int = None, top_y: int = 0):
        """Draw the SoC number as a crisp 3x5 pixel font, black on a white
        battery-shaped badge (body + nub). Right edge at `right_x` (default the
        display's right edge), top at `top_y`."""
        digits = [c for c in text if c in _DIGITS]
        if not digits:
            return
        if right_x is None:
            right_x = self._w - 1
        tw = len(digits) * (_DIGIT_W + _DIGIT_GAP) - _DIGIT_GAP
        pad_x, pad_y, nub_w = 2, 1, 2
        body_w, body_h = tw + 2 * pad_x, _DIGIT_H + pad_y
        body_x1 = right_x - nub_w
        body_x0 = body_x1 - body_w
        y0 = top_y
        draw.rectangle([body_x0, y0, body_x1, y0 + body_h], fill="white")
        nub_h = max(2, body_h - 2)
        nub_y0 = y0 + (body_h - nub_h) // 2
        draw.rectangle([body_x1, nub_y0, body_x1 + nub_w, nub_y0 + nub_h], fill="white")
        x = body_x0 + pad_x
        for ch in digits:
            for ry, row in enumerate(_DIGITS[ch]):
                for rx, on in enumerate(row):
                    if on == "1":
                        draw.point((x + rx, y0 + pad_y + ry), fill="black")
            x += _DIGIT_W + _DIGIT_GAP

    def set_backlight(self, level: int):
        with self._lock:
            if level == 0:
                self._device.hide()
                self._hidden = True
            else:
                if self._hidden:
                    self._device.show()
                    self._hidden = False
                self._device.contrast(int(level / 9 * 255))

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

    def set_battery(self, text: str):
        """Set the small number shown in the top-right corner ('' to hide).

        Only stores the value; the current screen redraws it on its next
        render (the scroll loop repaints periodically), so callers don't force
        a specific render path.
        """
        self._battery = (text or "")[:3]

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

    def _split_row(self, label, value, right_reserved, mid_gap, bold):
        """Divide a list row's width between the left label and right value.

        Single source of truth for row text layout, so every row type gets the
        same guarantees: the label is bounded (it used to be drawn unclipped,
        which is why long device names ran through the >> glyph), the value is
        bounded, and neither can reach the trailing glyph zone.

        `right_reserved` is the px consumed by the trailing glyph/arrow and its
        gap; `mid_gap` is the space between label and value (wider on rows that
        put an arrow there). Returns (label, label_width, value, value_budget)."""
        avail = (self._w - right_reserved) - _PAD_L
        # Let the value claim what it needs, but never more than half, so a
        # long value can't squeeze the label out entirely (and vice versa).
        v_reserved = min(self._ink_right(value), avail // 2) if value else 0
        label_budget = avail - (v_reserved + mid_gap if v_reserved else 0)
        label = self._fit_text(label, label_budget)
        lw = self._ink_right(label) + (1 if bold else 0)
        value_budget = max(0, avail - lw - mid_gap) if value else 0
        return label, lw, value, value_budget

    @staticmethod
    def _mid_gap(arrows, fixed_arrows):
        """Space reserved between a row's label and its value.

        On a turnable row the left-hand triangle lives in this gap, so it must
        cover pad + arrow + gap — otherwise the right-aligned value is allowed
        to start on top of the arrow (which is exactly what clipped the first
        few pixels of long pattern names)."""
        if not arrows:
            return _GAP
        if fixed_arrows:
            return _ARROW_PAD + _ARROW_W + _GAP
        return _GAP + _ARROW_W + _GAP

    def row_value_budget(self, label, *, drill=False, arrows=False,
                         fixed_arrows=False, bold=False):
        """Px available for a row's value, given its label and row type.

        Exposed so screens that pre-format a value (e.g. MainScreen scrolling a
        long pattern name) size it against the real layout instead of a
        hard-coded guess that can drift out of sync with the renderer."""
        right_reserved = _GLYPH_W + _GLYPH_GAP if (drill or arrows) else _PAD_R
        mid_gap = self._mid_gap(arrows, fixed_arrows)
        _, _, _, budget = self._split_row(label, "x", right_reserved, mid_gap, bold)
        return budget

    def _draw_arrow(self, draw, x, y, direction, size=5, fill="white"):
        s = size
        if direction == "up":
            draw.polygon([(x, y + s), (x + s, y + s), (x + s // 2, y)], fill=fill)
        elif direction == "down":
            draw.polygon([(x, y), (x + s, y), (x + s // 2, y + s)], fill=fill)
        elif direction == "left":
            draw.polygon([(x + s, y), (x + s, y + s), (x, y + s // 2)], fill=fill)
        else:  # right
            draw.polygon([(x, y), (x, y + s), (x + s, y + s // 2)], fill=fill)

    def _draw_drill_arrow(self, draw, x, y, fill="white"):
        # Open double-chevron (»), deliberately different from the solid nav
        # triangles, to mark a row you can Enter into.
        for dx in (0, 3):
            draw.line([(x + dx, y), (x + dx + 3, y + 3)], fill=fill)
            draw.line([(x + dx + 3, y + 3), (x + dx, y + 6)], fill=fill)

    def _draw_checkmark(self, draw, x, y, fill="white"):
        """Small check ✓, 7 wide × 5 tall, drawn from top-left (x, y). Two
        strokes: a short down-right stroke, then a long up-right stroke.
        Bitmap because PIL's default font can't render Unicode reliably."""
        draw.line([(x, y + 2), (x + 2, y + 4)], fill=fill)
        draw.line([(x + 2, y + 4), (x + 6, y)], fill=fill)

    def _draw_list_row(self, draw, item, active: bool, y: int, bold: bool = False):
        W = self._w
        h = self._font_h + 1
        if item.get("divider"):
            ly = y + h // 2
            label = item.get("label", "")
            if label:
                # Centre the caption with rules either side; budget leaves room
                # for a minimum rule on each side.
                lw = self._draw_text(draw, label, y, max_px=W - 4 * _PAD_L,
                                     center=W // 2)
                if lw:
                    lx = (W - lw) // 2
                    draw.line((_PAD_L, ly, lx - _GAP, ly), fill="white")
                    draw.line((lx + lw + _PAD_L, ly, W - 3, ly), fill="white")
                else:
                    draw.line((_PAD_L, ly, W - 3, ly), fill="white")
            else:
                draw.line((_PAD_L, ly, W - 3, ly), fill="white")
            return
        if active:
            draw.rectangle((0, y, W - 1, y + self._font_h - 1), fill="white")
            fg = "black"
        else:
            fg = "white"

        drill = item.get("drill")
        arrows = item.get("arrows_left") or item.get("arrows_right")
        v = item.get("value", "")
        # A marked row shows a checkmark in the value slot instead of text.
        marked = bool(item.get("mark"))
        if marked:
            v = ""

        # One layout decision for every row type, so the label is always
        # bounded — an unbounded label is what let long device names run
        # through the >> glyph.
        right_reserved = _GLYPH_W + _GLYPH_GAP if (drill or arrows) else _PAD_R
        mid_gap = self._mid_gap(arrows, item.get("fixed_arrows"))
        label, lw, v, v_budget = self._split_row(
            item.get("label", ""), v, right_reserved, mid_gap, bold)
        self._draw_text(draw, label, y, max_px=self._w, left=_PAD_L,
                        fg=fg, bold=bold)

        ay = y + (self._font_h - 5) // 2
        text_right = W - right_reserved     # hard right edge for row text

        if drill:
            chev_x = W - _GLYPH_W
            self._draw_drill_arrow(draw, chev_x, y + (self._font_h - 6) // 2, fill=fg)
            if marked:
                # Checkmark in the value slot, right-aligned before the drill.
                self._draw_checkmark(draw, text_right - 7,
                                     y + (self._font_h - 5) // 2, fill=fg)
            elif v:
                self._draw_text(draw, v, y, max_px=v_budget, right=text_right,
                                fg=fg, bold=bold)
        elif arrows:
            # Layout is stable regardless of which arrows show (value stays put)
            # so a state change that flips a toggle doesn't shift text sideways.
            r_ax = W - _GLYPH_W
            vw = self._draw_text(draw, v, y, max_px=v_budget, right=text_right,
                                 fg=fg, bold=bold)
            if active:
                if item.get("arrows_right"):
                    self._draw_arrow(draw, r_ax, ay, "right", fill=fg)
                if item.get("arrows_left"):
                    # fixed_arrows: pin to just past the label so a value that
                    # changes width (a scrolling name) doesn't jitter the arrow.
                    # Otherwise sit right next to the value.
                    lax = (_PAD_L + lw + _ARROW_PAD if item.get("fixed_arrows")
                           else text_right - vw - _GAP - _ARROW_W)
                    self._draw_arrow(draw, lax, ay, "left", fill=fg)
        elif v:
            self._draw_text(draw, v, y, max_px=v_budget, right=text_right,
                            fg=fg, bold=bold)

    def _draw_footer(self, draw, y, can_scroll=True, can_enter=True,
                     can_adjust_left=True, can_adjust_right=True, can_back=True,
                     enter_label="[Enter]", back_label="[Back]"):
        """Knob legend; each element blanks (space kept) when it does nothing.
        Left/right adjust are independent so a row that only turns one way
        (On/Off toggle, playlist advance) shows just the one meaningful arrow.

        The two button captions are overridable so a screen whose knobs do
        something specific can say so — e.g. the reconnect screen labels the
        left knob "[Reset WiFi]" instead of the generic "[Enter]"."""
        W = self._w
        draw.line((0, y, W - 1, y), fill="white")
        ty = y + 1  # hug the bottom edge to give the list more room
        ay = ty + (self._font_h - 5) // 2

        # Space for BOTH captions is reserved from the layout whether or not
        # they are actually drawn. A screen toggles can_enter / can_back as the
        # cursor moves between rows, and if the reservation collapsed with the
        # caption the arrows would jump sideways every time ("<  >" one moment,
        # "< [Back] >" the next). Widths are computed unconditionally; only the
        # drawing is conditional.
        rx = W - 6
        bw = self._ink_right(back_label)
        left_arrow_x = rx - _GAP - bw - _GLYPH_W
        if can_adjust_right:
            self._draw_arrow(draw, rx, ay, "right")
        if can_back:
            self._draw_text(draw, back_label, ty, max_px=W // 2, right=rx - _GAP)
        if can_adjust_left:
            self._draw_arrow(draw, left_arrow_x, ay, "left")

        # left group:  ^ [Enter] v   (scroll cursor + Enter)
        x = 1
        if can_scroll:
            self._draw_arrow(draw, x, ay, "up")
        x += _GLYPH_W
        # Budget against the reserved right group so a long custom caption
        # ("[Reset WiFi]") can never collide with "[Back]".
        budget = max(0, left_arrow_x - x - _GAP - _GLYPH_W)
        ew = self._ink_right(self._fit_text(enter_label, budget))
        if can_enter:
            self._draw_text(draw, enter_label, ty, max_px=budget, left=x)
        if can_scroll:
            self._draw_arrow(draw, x + ew + _GAP, ay, "down")

    def render_list(self, items, cursor: int, title: str = "", *,
                    can_scroll=True, can_enter=True,
                    can_adjust_left=True, can_adjust_right=True, can_back=True):
        """Pinned header (device name + battery), scrolling list, knob legend."""
        if self._unchanged((
                "list", cursor, title, self._battery,
                can_scroll, can_enter, can_adjust_left, can_adjust_right, can_back,
                tuple((i.get("label"), i.get("value"), i.get("drill"),
                       i.get("mark"), i.get("arrows_left"), i.get("arrows_right"),
                       i.get("divider"), i.get("big"), i.get("fixed_arrows"))
                      for i in items))):
            return
        with self._frame() as draw:
            H = self._device.height
            row_h = self._font_h
            head_h = self._font_h + 1
            foot_top = H - self._font_h - 1
            # Budget the title against the battery badge's actual left edge —
            # a character count can't know how wide the glyphs are.
            self._draw_text(draw, title, 0, left=1,
                            max_px=self._battery_left_edge(self._battery) - 1 - _GAP)
            if self._battery:
                self._draw_battery(draw, self._battery)
            n = len(items)
            vis = max(1, (foot_top - head_h) // row_h)
            top = 0
            if n > vis:
                top = max(0, min(cursor - vis // 2, n - vis))
            y = head_h
            for i in range(top, min(top + vis, n)):
                self._draw_list_row(draw, items[i], cursor == i, y,
                                    bold=items[i].get("big", False))
                y += row_h
            self._draw_footer(draw, foot_top, can_scroll, can_enter,
                              can_adjust_left, can_adjust_right, can_back)

    def render_message(self, line1: str, line2: str = "", title: str = "",
                       hint: str = "", footer=None, line2_arrows: bool = False,
                       enter_label: str = "[Enter]", back_label: str = "[Back]"):
        """Status/message screen: header strip, centered text, and either a text
        hint or the graphical knob legend (`footer` = (scroll, enter, adjust, back)).
        `line2_arrows` flanks line2 with filled triangles (the char picker).
        `enter_label`/`back_label` override the footer button captions."""
        if self._unchanged(("msg", line1, line2, title, hint, footer,
                            line2_arrows, enter_label, back_label, self._battery)):
            return
        with self._frame() as draw:
            W, H = self._w, self._device.height
            # Budget the title against the battery badge's actual left edge —
            # a character count can't know how wide the glyphs are.
            self._draw_text(draw, title, 0, left=1,
                            max_px=self._battery_left_edge(self._battery) - 1 - _GAP)
            if self._battery:
                self._draw_battery(draw, self._battery)
            has_foot = footer is not None or bool(hint)
            body_top = self._font_h + 3
            body_bot = H - (self._font_h + 2 if has_foot else 2)
            cy = (body_top + body_bot) // 2
            body_px = W - 2 * _PAD_L          # body text budget
            if line2 and line2_arrows:
                # Char picker: line2 is a single glyph flanked by triangles,
                # so it never wraps and keeps its own layout.
                self._draw_text(draw, line1, cy - self._font_h,
                                max_px=body_px, center=W // 2)
                lw = self._ink_right(self._fit_text(line2, body_px - 2 * _GLYPH_W))
                gx = max(_PAD_L, (W - (lw + 2 * _GLYPH_W)) // 2)
                ay2 = (cy + 1) + (self._font_h - 5) // 2
                self._draw_arrow(draw, gx, ay2, "left")
                self._draw_text(draw, line2, cy + 1,
                                max_px=body_px - 2 * _GLYPH_W, left=gx + 8)
                self._draw_arrow(draw, gx + 8 + lw + _GAP, ay2, "right")
            else:
                # Wrap each line, then vertically centre the resulting block.
                # For the common 1- and 2-line cases this reproduces the old
                # fixed positions exactly; longer text now flows onto extra
                # lines instead of running off the display.
                line_h = self._font_h + 1
                room = max(1, (body_bot - body_top) // line_h)
                lines = self._wrap_text(line1, body_px, max_lines=room)
                if line2:
                    lines += self._wrap_text(line2, body_px,
                                             max_lines=max(1, room - len(lines)))
                lines = lines[:room]
                total = len(lines) * self._font_h + max(0, len(lines) - 1)
                top = cy - total // 2
                for i, ln in enumerate(lines):
                    self._draw_text(draw, ln, top + i * line_h,
                                    max_px=body_px, center=W // 2)
            if footer is not None:
                self._draw_footer(draw, H - self._font_h - 1, *footer,
                                  enter_label=enter_label, back_label=back_label)
            elif hint:
                draw.line((0, H - self._font_h - 2, W - 1, H - self._font_h - 2), fill="white")
                self._draw_text(draw, hint, H - self._font_h,
                                max_px=W - _PAD_L, left=1)

    def close(self):
        self._device.cleanup()
