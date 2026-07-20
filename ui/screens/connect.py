"""Screens for getting connected: the device picker, the discovery/reconnect
spinners, and the WiFi-scan / password / IP-entry flows.
"""
from __future__ import annotations
import time
from typing import Callable, Optional

from .base import Screen, ListScreen, Row
from .widgets import _Scroller

# Special sentinel shown in the character picker to submit the password
_SEND = "\x00"

CHARSET = (
    _SEND
    + "abcdefghijklmnopqrstuvwxyz"
    + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    + "0123456789"
    + " !@#$%^&*()-_=+[]{}|;:',.<>?/\\"
)

_IP_SEND = "\x01"


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
