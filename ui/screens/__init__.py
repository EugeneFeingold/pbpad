"""Screen classes for the OLED UI.

Formerly a single ui/screens.py; split into submodules by role. Everything is
re-exported here so `from ui.screens import MainScreen` (etc.) keeps working.

`time` is imported so tests can patch `ui.screens.time.monotonic` — it is the
shared stdlib module object, so the patch reaches every submodule that reads
`time.monotonic()`.
"""
import time  # noqa: F401  (patched by tests via ui.screens.time)

from .base import Screen, Row, ListScreen
from .widgets import _VelocityStep, _Scroller
from .controls import MainScreen, ColorEditorScreen
from .settings import SettingsScreen, ConfirmScreen, ChoiceScreen
from .connect import (
    DeviceSelectScreen,
    DiscoveringScreen,
    ReconnectScreen,
    WifiScanScreen,
    PasswordEntryScreen,
    IPEntryScreen,
    CHARSET,
    _SEND,
    _IP_SEND,
)
from .status import (
    StatusScreen,
    ConnectingScreen,
    SleepScreen,
    InfoScreen,
    LockScreen,
)

__all__ = [
    "Screen", "Row", "ListScreen",
    "MainScreen", "ColorEditorScreen",
    "SettingsScreen", "ConfirmScreen", "ChoiceScreen",
    "DeviceSelectScreen", "DiscoveringScreen", "ReconnectScreen",
    "WifiScanScreen", "PasswordEntryScreen", "IPEntryScreen",
    "StatusScreen", "ConnectingScreen", "SleepScreen", "InfoScreen", "LockScreen",
    "CHARSET",
]
