"""SH1106 OLED driver and the two high-level render entry points.

The text-measurement/primitive layer lives in lcd_text (LCDTextMixin) and the
row/footer/glyph drawing in lcd_widgets (LCDWidgetsMixin); this module owns the
device, the change-skipping frame buffer, and render_list / render_message.
The layout constants are re-exported here so callers and tests can keep doing
`from hardware.lcd import _PAD_L` etc.
"""
import threading
from contextlib import contextmanager
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from PIL import Image, ImageDraw, ImageFont
from conf import config

from hardware.lcd_layout import (  # noqa: F401  (re-exported for callers/tests)
    _PAD_L, _PAD_R, _GAP, _GLYPH_W, _GLYPH_GAP, _ARROW_W, _ARROW_PAD,
    _MEASURE_CACHE_MAX, _DIGIT_W, _DIGIT_H, _DIGIT_GAP, _DIGITS,
)
from hardware.lcd_text import LCDTextMixin
from hardware.lcd_widgets import LCDWidgetsMixin


class LCD(LCDTextMixin, LCDWidgetsMixin):
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

    def set_battery(self, text: str):
        """Set the small number shown in the top-right corner ('' to hide).

        Only stores the value; the current screen redraws it on its next
        render (the scroll loop repaints periodically), so callers don't force
        a specific render path.
        """
        self._battery = (text or "")[:3]

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
                # list(...) COPIES the memoised wrap result. Without the copy,
                # `lines += ...` would extend _wrap_text's cached list in place,
                # so line1's cache would grow by line2 every call — stacking a
                # stale body line onto every subsequent frame (the doubled
                # "Please wait…" / a previous network's name bleeding through).
                lines = list(self._wrap_text(line1, body_px, max_lines=room))
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
