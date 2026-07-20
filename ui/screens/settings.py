"""The Settings screen and its confirmation modals.

SettingsScreen combines the light controls (brightness, playlist, shuffle) and
this-device settings (backlight, timeouts, WiFi, restart, power off), split by
a divider. Power off and Restart open ConfirmScreen / ChoiceScreen.
"""
from __future__ import annotations
from typing import Callable

from .base import ListScreen, Row
from .widgets import _VelocityStep


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
