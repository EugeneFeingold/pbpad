"""Base screen types: the abstract Screen, the Row model, and ListScreen —
the cursored-list base that almost every screen builds on.

The whole UI is a cursored list:
  left knob:  turn = move cursor, push = back
  right knob: turn = change this row, push = open a "->" row
A row is turnable (arrows), a drill (opens something), or a divider (skipped).
"""
from __future__ import annotations
import asyncio
from typing import Callable, Optional


class Screen:
    async def handle(self, event: tuple) -> str | None:
        """Return next screen name or None to stay."""
        return None

    def render(self, lcd):
        pass

    def will_pop(self):
        """Called by App when this screen is about to be dismissed (popped
        from the nav stack or replaced). Override to release resources — e.g.
        cancel a periodic refresh task. Default: no-op.

        Important: do NOT put teardown logic in _on_back(): that method is
        called every render as `_on_back() is not None` to decide whether to
        show a Back label in the footer. Side effects there fire per-frame."""
        pass


class Row:
    def __init__(self, label: str, value: str = "", *, big: bool = False,
                 on_turn: Optional[Callable] = None, on_open: Optional[Callable] = None,
                 divider: bool = False, turn_dir: Optional[str] = None,
                 mark: bool = False, fixed_arrows: bool = False):
        self.label = label
        self.value = value
        self.big = big
        self.on_turn = on_turn
        self.on_open = on_open
        self.divider = divider
        # turn_dir: None = both directions; "left" or "right" = only that
        # direction is meaningful (draws only that arrow). Purely cosmetic —
        # on_turn is still invoked in either direction; the handler decides.
        self.turn_dir = turn_dir
        # mark: draw a checkmark on the row (used for "currently connected"
        # device in the picker). Only meaningful on drill rows.
        self.mark = mark
        # fixed_arrows: pin the left arrow to a fixed x (right after the
        # label) instead of hugging the value's left edge. Use when the
        # value's width is dynamic (e.g. a scrolling name) — otherwise the
        # arrow would bounce as the value changes width.
        self.fixed_arrows = fixed_arrows

    @property
    def can_turn_left(self) -> bool:
        return self.on_turn is not None and self.turn_dir != "right"

    @property
    def can_turn_right(self) -> bool:
        return self.on_turn is not None and self.turn_dir != "left"

    def display(self, active: bool) -> dict:
        return {
            "label": self.label,
            "value": self.value,
            "big": self.big,
            "arrows_left": self.can_turn_left,
            "arrows_right": self.can_turn_right,
            "drill": self.on_open is not None and self.on_turn is None,
            "divider": self.divider,
            "mark": self.mark,
            "fixed_arrows": self.fixed_arrows,
        }


class ListScreen(Screen):
    """Base for every cursored screen. Subclasses supply rows()."""

    def __init__(self):
        self._cursor = 0
        self._sub: Optional[Screen] = None  # in-place drill-down (color, confirm)

    def rows(self) -> list:
        return []

    def title(self) -> str:
        return ""

    def _on_back(self) -> str | None:
        return "__back__"  # sub-screens pop; top-level screens override

    @staticmethod
    def _selectable(rows) -> list:
        return [i for i, r in enumerate(rows) if not r.divider]

    def _fix_cursor(self, rows):
        sel = self._selectable(rows)
        if not sel:
            self._cursor = 0
        elif self._cursor not in sel:
            self._cursor = min(sel, key=lambda i: abs(i - self._cursor))

    def render(self, lcd):
        if self._sub is not None:
            self._sub.render(lcd)
            return
        rows = self.rows()
        self._fix_cursor(rows)
        sel = self._selectable(rows)
        row = rows[self._cursor] if 0 <= self._cursor < len(rows) else None
        lcd.render_list(
            [r.display(i == self._cursor) for i, r in enumerate(rows)],
            self._cursor, title=self.title(),
            can_scroll=len(sel) > 1,
            can_enter=bool(row and row.on_open),
            can_adjust_left=bool(row and row.can_turn_left),
            can_adjust_right=bool(row and row.can_turn_right),
            can_back=self._on_back() is not None,
        )

    async def handle(self, event: tuple) -> str | None:
        if self._sub is not None:
            result = await self._sub.handle(event)
            if result == "__back__":
                self._sub = None
                return None
            return result

        rows = self.rows()
        self._fix_cursor(rows)
        sel = self._selectable(rows)
        kind = event[0]

        if kind == "encoder":
            _, which, direction = event
            if which == "enc1" and sel:
                pos = sel.index(self._cursor)
                self._cursor = sel[(pos + direction) % len(sel)]
            elif which == "enc2":
                row = rows[self._cursor]
                if row.on_turn:
                    res = row.on_turn(direction)
                    if asyncio.iscoroutine(res):
                        await res
        elif event == ("press", "enc1"):  # left knob push = Enter
            row = rows[self._cursor]
            if row.on_open:
                res = row.on_open()
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, Screen):
                    self._sub = res
                elif isinstance(res, str):
                    return res
        elif event == ("press", "enc2"):  # right knob push = Back
            return self._on_back()
        return None
