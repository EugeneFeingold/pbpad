"""The home screen and its drill-ins: live pattern + control editing.

MainScreen shows the running pattern and one row per control the pattern
exposes (sliders adjust in place; color controls open ColorEditorScreen).
"""
from __future__ import annotations

import log
from .base import ListScreen, Row
from .widgets import _VelocityStep, _Scroller


class MainScreen(ListScreen):
    # Fallback when no LCD was supplied (no scrolling in that case anyway);
    # the real budget comes from lcd.row_value_budget() so this can never
    # drift out of sync with the renderer's layout.
    _NAME_SLOT_FALLBACK_PX = 65

    def __init__(self, client, lcd=None):
        super().__init__()
        self._client = client
        # lcd is used only to measure text pixel widths for the scroller;
        # rows() falls back to a no-scroll behavior if lcd isn't provided.
        self._lcd = lcd
        self._vel = _VelocityStep()
        self._name_scroll = _Scroller()

    def title(self) -> str:
        c = self._client
        return ("\xd0 " if c.sequencer_running else "") + c.device_name

    def _on_back(self):
        return None  # top level: nothing to go back to

    @staticmethod
    def _label(name: str) -> str:
        low = name.lower()
        if low.startswith("hsvpicker") or low.startswith("rgbpicker"):
            return name[9:] or name
        if low.startswith("slider"):
            return name[6:] or name
        return name

    def rows(self):
        c = self._client
        pats = c.patterns
        name = pats.get(c.active_pattern_id, "...") if pats else "Loading..."
        if self._lcd is not None:
            # Pixel-based: scroll only when actual ink overflows the slot. Ask
            # the LCD for the real budget so this tracks the row layout.
            slot = self._lcd.row_value_budget("Pattern", arrows=True,
                                              fixed_arrows=True)
            name = self._name_scroll.get(name, slot, self._lcd._ink_right)
        # In playlist mode _cycle_pattern only honors forward direction, so only
        # advertise the right arrow. fixed_arrows because the scrolling name
        # changes width — the left arrow needs to stay put.
        playlist_mode = c.sequencer_running and bool(c.playlist)
        out = [Row("Pattern", name, on_turn=self._cycle_pattern,
                   turn_dir="right" if playlist_mode else None,
                   fixed_arrows=True)]
        for cname, val in c.controls.items():
            if isinstance(val, list):
                out.append(Row(self._label(cname), on_open=self._open_color(cname)))
            else:
                out.append(Row(self._label(cname), f"{int(val * 100)}%",
                               on_turn=self._slider_turn(cname)))
        out.append(Row("Settings", on_open=lambda: "settings"))
        return out

    def _cycle_pattern(self, direction: int):
        c = self._client
        pats = c.patterns
        if not pats:
            return
        if c.sequencer_running and c.playlist:
            ids = [p for p in c.playlist if p in pats]
        else:
            ids = sorted(pats, key=lambda k: pats[k].lower())
        if not ids:
            return
        try:
            idx = ids.index(c.active_pattern_id)
        except ValueError:
            idx = 0
        if c.sequencer_running and c.playlist and direction < 0:
            return
        new_id = ids[(idx + direction) % len(ids)]
        log.log(log.CHANGE, f"pattern -> {pats.get(new_id, new_id)}")
        c.set_pattern_local(new_id)  # immediate local update
        # network send is debounced inside the client (250ms after the knob stops)
        if c.sequencer_running and c.playlist:
            c.advance_playlist(1)
        else:
            c.commit_pattern(new_id)

    def _slider_turn(self, name: str):
        def turn(direction):
            val = self._client.controls.get(name)
            if not isinstance(val, (int, float)):
                return None
            return self._client.set_control(name, self._vel.apply(float(val), direction))
        return turn

    def _open_color(self, name: str):
        return lambda: ColorEditorScreen(self._client, name)


class ColorEditorScreen(ListScreen):
    """Drill-in editor for a color control: one row per component."""

    def __init__(self, client, name: str):
        super().__init__()
        self._client = client
        self._name = name
        self._vel = _VelocityStep(scale=255, slow=1, fast=16)  # 0-255; fast step = 16
        low = name.lower()
        self._labels = ["R", "G", "B"] if low.startswith("rgb") else ["H", "S", "V"]

    def title(self):
        return MainScreen._label(self._name)

    def rows(self):
        val = self._client.controls.get(self._name)
        if not isinstance(val, list):
            return [Row("Back", on_open=lambda: "__back__")]
        return [Row(self._labels[i] if i < len(self._labels) else str(i),
                    str(int(val[i] * 255)), on_turn=self._comp_turn(i))
                for i in range(len(val))]

    def _comp_turn(self, i: int):
        def turn(direction):
            val = list(self._client.controls.get(self._name) or [])
            if i >= len(val):
                return None
            val[i] = self._vel.apply(float(val[i]), direction)
            return self._client.set_control(self._name, val)
        return turn
