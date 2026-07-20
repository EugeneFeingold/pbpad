from __future__ import annotations
import asyncio
import time
from typing import Callable, Optional
import config
import log


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


# Special sentinel shown in the character picker to submit the password
_SEND = "\x00"

CHARSET = (
    _SEND
    + "abcdefghijklmnopqrstuvwxyz"
    + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    + "0123456789"
    + " !@#$%^&*()-_=+[]{}|;:',.<>?/\\"
)


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


# ---------------------------------------------------------------------------
# List model — the whole UI is a cursored list.
#   left knob:  turn = move cursor, push = back
#   right knob: turn = change this row, push = open a "->" row
# A row is turnable (arrows), a drill (opens something), or a divider (skipped).
# ---------------------------------------------------------------------------

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


class SettingsScreen(ListScreen):
    """Combined Settings + PB Setup, split by a divider: the lights vs this device."""

    # (label, seconds) — None = Never
    _TIMEOUTS = [
        ("1s", 1), ("2s", 2), ("3s", 3), ("4s", 4), ("5s", 5), ("6s", 6), ("7s", 7),
        ("8s", 8), ("9s", 9), ("10s", 10), ("15s", 15), ("20s", 20), ("25s", 25),
        ("30s", 30), ("45s", 45), ("1m", 60), ("1.5m", 90), ("2m", 120), ("3m", 180),
        ("4m", 240), ("5m", 300), ("10m", 600), ("15m", 900), ("20m", 1200),
        ("30m", 1800), ("Never", None),
    ]

    def __init__(self, client=None, backlight_level: int = 9,
                 on_backlight_change: Callable[[int], None] = None,
                 device_name: str = "", ssid: str = "",
                 on_power_off: Callable = None,
                 on_restart_software: Callable = None,
                 on_restart_device: Callable = None,
                 dim_secs=None, off_secs=None,
                 on_dim_change: Callable = None, on_off_change: Callable = None,
                 led_brightness: int = 2,
                 on_led_brightness_change: Callable[[int], None] = None):
        super().__init__()
        self._client = client
        self._backlight = backlight_level
        self._on_backlight_change = on_backlight_change
        self._device_name = device_name
        self._ssid = ssid
        self._on_power_off = on_power_off
        self._on_restart_software = on_restart_software
        self._on_restart_device = on_restart_device
        self._dim_secs = dim_secs
        self._off_secs = off_secs
        self._on_dim_change = on_dim_change
        self._on_off_change = on_off_change
        self._led_brightness = led_brightness
        self._on_led_brightness_change = on_led_brightness_change
        self._vel = _VelocityStep()

    def title(self):
        return "Settings"

    def _on_back(self):
        return "back_from_settings"

    def rows(self):
        out = []
        c = self._client
        if c is not None:
            out.append(Row("Brightness", f"{int(c.brightness * 100)}%", on_turn=self._bright_turn))
            # On/Off toggles: from "On" you can only go left (to Off), and vice
            # versa. Show only the arrow that maps to a real state change.
            out.append(Row("Playlist", "On" if c.sequencer_running else "Off",
                           on_turn=self._playlist_turn,
                           turn_dir="left" if c.sequencer_running else "right"))
            out.append(Row("Shuffle", "On" if c.sequencer_shuffle else "Off",
                           on_turn=self._shuffle_turn,
                           turn_dir="left" if c.sequencer_shuffle else "right"))
        out.append(Row("this device", divider=True))
        out.append(Row("Backlight", str(self._backlight), on_turn=self._backlight_turn))
        out.append(Row("LED Brightness", str(self._led_brightness), on_turn=self._led_brightness_turn))
        out.append(Row("Screen Dim", self._timeout_label(self._dim_secs), on_turn=self._dim_turn))
        out.append(Row("Screen Off", self._timeout_label(self._off_secs), on_turn=self._off_turn))
        out.append(Row("Device", self._device_name, on_open=lambda: "device_select"))
        out.append(Row("WiFi", self._ssid, on_open=lambda: "wifi_scan"))
        out.append(Row("Info", on_open=lambda: "info"))
        out.append(Row("Lock", on_open=lambda: "lock"))
        out.append(Row("Restart", on_open=self._open_restart))
        out.append(Row("Power off", on_open=self._open_power))
        return out

    @classmethod
    def _timeout_label(cls, secs):
        for label, s in cls._TIMEOUTS:
            if s == secs:
                return label
        return "?"

    @classmethod
    def _timeout_index(cls, secs):
        for i, (_, s) in enumerate(cls._TIMEOUTS):
            if s == secs:
                return i
        return 0

    def _dim_turn(self, direction):
        i = (self._timeout_index(self._dim_secs) + direction) % len(self._TIMEOUTS)
        self._dim_secs = self._TIMEOUTS[i][1]
        if self._on_dim_change:
            self._on_dim_change(self._dim_secs)
        return None

    def _off_turn(self, direction):
        i = (self._timeout_index(self._off_secs) + direction) % len(self._TIMEOUTS)
        self._off_secs = self._TIMEOUTS[i][1]
        if self._on_off_change:
            self._on_off_change(self._off_secs)
        return None

    def _bright_turn(self, direction):
        return self._client.set_brightness(self._vel.apply(self._client.brightness, direction))

    def _playlist_turn(self, direction):
        return self._client.set_sequencer_running(direction > 0)

    def _shuffle_turn(self, direction):
        return self._client.set_sequencer_shuffle(direction > 0)

    def _backlight_turn(self, direction):
        new = max(1, min(9, self._backlight + direction))  # never fully off
        if new != self._backlight:
            self._backlight = new
            if self._on_backlight_change:
                self._on_backlight_change(new)
        return None

    def _led_brightness_turn(self, direction):
        # 0..25 maps to 0%..25% actual output (clamped for battery + eye comfort).
        # 0 is allowed even though "always on for findability" is the goal —
        # policy, not a hard rule.
        new = max(0, min(25, self._led_brightness + direction))
        if new != self._led_brightness:
            self._led_brightness = new
            if self._on_led_brightness_change:
                self._on_led_brightness_change(new)
        return None

    def _open_power(self):
        return ConfirmScreen("Power off?", self._on_power_off)

    def _open_restart(self):
        return ChoiceScreen("Restart?", [
            ("Restart software", self._on_restart_software),
            ("Restart device", self._on_restart_device),
            ("Cancel", None),
        ])


class ConfirmScreen(ListScreen):
    def __init__(self, prompt: str, on_yes: Callable):
        super().__init__()
        self._prompt = prompt
        self._on_yes = on_yes

    def title(self):
        return self._prompt

    def rows(self):
        return [Row("Cancel", on_open=lambda: "__back__"),
                Row("Yes", on_open=self._confirm)]

    def _confirm(self):
        if self._on_yes:
            self._on_yes()
        return "__back__"


class ChoiceScreen(ListScreen):
    """N-way confirmation. `choices` is a list of (label, callback|None) pairs;
    a None callback simply dismisses the screen (Cancel-style entries)."""

    def __init__(self, prompt: str, choices: list):
        super().__init__()
        self._prompt = prompt
        self._choices = choices

    def title(self):
        return self._prompt

    def rows(self):
        return [Row(label, on_open=self._make(cb)) for label, cb in self._choices]

    def _make(self, cb):
        def action():
            if cb is not None:
                cb()
            return "__back__"
        return action


class DeviceSelectScreen(ListScreen):
    def __init__(self, devices, on_select: Callable, current_name: Optional[str] = None):
        super().__init__()
        self._devices = devices
        self._on_select = on_select
        self._current_name = current_name

    def title(self):
        return "PixelBlaze"

    def _on_back(self):
        return "back_from_device_select"

    def rows(self):
        out = []
        for d in self._devices:
            is_current = self._current_name is not None and d.name == self._current_name
            out.append(Row(d.name, on_open=self._pick(d), mark=is_current))
        out.append(Row("Scan Again", on_open=lambda: "discovery"))
        # "Connect by IP" moved off SettingsScreen: manually connecting is a
        # per-device action, so it lives with the device list where the user
        # is already thinking about which PB to talk to.
        out.append(Row("Connect by IP", on_open=lambda: "ip_entry"))
        return out

    def _pick(self, dev):
        def _open():
            self._on_select(dev)
            return "main"
        return _open


class DiscoveringScreen(Screen):
    """Shown while PB discovery is running. Right knob cancels — pushes the
    "back_from_device_select" transition and calls on_cancel() so the caller
    can tear down the background discover task."""

    def __init__(self, on_cancel: Callable):
        self._on_cancel = on_cancel
        self._t0 = time.monotonic()

    def render(self, lcd):
        chars = "|/-\\"
        idx = int((time.monotonic() - self._t0) * 5) % 4
        lcd.render_message(
            "Finding", f"PixelBlaze...  {chars[idx]}",
            footer=(False, False, False, False, True),  # only Back visible
        )

    async def handle(self, event: tuple) -> str | None:
        if event == ("press", "enc2"):
            self._on_cancel()
            return "back_from_device_select"
        return None


class ReconnectScreen(Screen):
    """Shown while trying to reconnect to the SAME device we lost.

    Left knob  = reset the Pi's WiFi (force a fresh association — the usual
                 culprit on a device that moves around outdoors).
    Right knob = cancel, falling back to the full device search so the user
                 can pick a different PB.
    """

    _NAME_COLS = 13

    def __init__(self, name: str):
        self._name = name
        self._t0 = time.monotonic()
        self._scroll = _Scroller()

    def render(self, lcd):
        chars = "|/-\\"
        idx = int((time.monotonic() - self._t0) * 5) % 4
        name = self._scroll.get(self._name, self._NAME_COLS * 6, lcd._ink_right)
        lcd.render_message(
            "Reconnecting to", f"{name}  {chars[idx]}",
            # Left knob resets WiFi, right knob cancels — label them so the
            # generic "[Enter]" doesn't hide what the button actually does.
            footer=(False, True, False, False, True),
            enter_label="[Reset WiFi]",
        )

    async def handle(self, event: tuple) -> str | None:
        if event == ("press", "enc1"):
            return "reset_wifi"
        if event == ("press", "enc2"):
            return "cancel_reconnect"
        return None


class WifiScanScreen(ListScreen):
    def __init__(self, networks, on_select: Callable):
        super().__init__()
        self._networks = networks
        self._on_select = on_select

    def title(self):
        return "WiFi"

    def _on_back(self):
        return "back_from_wifi"

    def rows(self):
        out = [Row(n.ssid, "*" if n.secured else "", on_open=self._pick(n))
               for n in self._networks]
        out.append(Row("Rescan", on_open=lambda: "wifi_scan"))
        return out

    def _pick(self, net):
        def _open():
            self._on_select(net)
            return "password_entry"
        return _open


class PasswordEntryScreen(Screen):
    """Left knob scrolls the character set, left push adds (or SEND submits),
    right push deletes / backs out."""

    def __init__(self, ssid: str, on_submit: Callable[[str], None], on_cancel: Callable):
        self._ssid = ssid
        self._on_submit = on_submit
        self._on_cancel = on_cancel
        self._chars = list(CHARSET)
        self._char_idx = 0
        self._password = ""

    def _current_char(self) -> str:
        return self._chars[self._char_idx]

    def render(self, lcd):
        c = self._current_char()
        sel = "[SEND]" if c == _SEND else f" {c} "
        lcd.render_message(
            line1=lcd.fit_tail(self._password + "_", lcd.body_width),
            line2=sel,
            title=self._ssid or "Wi-Fi",
            footer=(True, True, False, False, True),  # scroll chars, Enter=add, Back=delete
            line2_arrows=True,
        )

    async def handle(self, event: tuple) -> str | None:
        kind = event[0]
        if kind == "encoder" and event[1] == "enc1":
            self._char_idx = (self._char_idx + event[2]) % len(self._chars)
        elif event == ("press", "enc1"):
            c = self._current_char()
            if c == _SEND:
                self._on_submit(self._password)
                return "connecting"
            self._password += c
        elif event == ("press", "enc2"):
            if self._password:
                self._password = self._password[:-1]
            else:
                self._on_cancel()
                return "wifi_scan"
        return None


_IP_SEND = "\x01"


class IPEntryScreen(Screen):
    """Enter a device IP directly. Left knob scrolls digits/dots, left push
    adds (or [OK] submits), right push deletes / cancels."""

    _CHARS = [_IP_SEND] + list("0123456789.")

    def __init__(self, on_submit: Callable[[str], None], on_cancel: Callable, default: str = ""):
        self._on_submit = on_submit
        self._on_cancel = on_cancel
        self._char_idx = 1  # start on "0", not the send sentinel
        self._ip = default

    def render(self, lcd):
        c = self._CHARS[self._char_idx]
        sel = "[OK]" if c == _IP_SEND else f" {c} "
        lcd.render_message(
            line1=lcd.fit_tail(self._ip + "_", lcd.body_width),
            line2=sel,
            title="Connect by IP",
            footer=(True, True, False, False, True),  # scroll chars, Enter=add, Back=delete
            line2_arrows=True,
        )

    async def handle(self, event: tuple) -> str | None:
        kind = event[0]
        if kind == "encoder" and event[1] == "enc1":
            self._char_idx = (self._char_idx + event[2]) % len(self._CHARS)
        elif event == ("press", "enc1"):
            c = self._CHARS[self._char_idx]
            if c == _IP_SEND:
                self._on_submit(self._ip)
                return "connecting_ip"
            self._ip += c
        elif event == ("press", "enc2"):
            if self._ip:
                self._ip = self._ip[:-1]
            else:
                self._on_cancel()
                return "back_from_ip"
        return None


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
