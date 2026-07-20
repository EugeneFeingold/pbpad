"""Row, footer, and glyph drawing, plus the layout-budget math that keeps every
row type honest.

All text here goes through self._draw_text (from LCDTextMixin); the only raw
drawing is lines/rectangles/polygons/points for the battery badge, nav
triangles, drill chevron, and checkmark.
"""
from hardware.lcd_layout import (
    _PAD_L, _PAD_R, _GAP, _GLYPH_W, _GLYPH_GAP, _ARROW_W, _ARROW_PAD,
    _DIGIT_W, _DIGIT_H, _DIGIT_GAP, _DIGITS,
)


class LCDWidgetsMixin:
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
