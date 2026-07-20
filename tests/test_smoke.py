"""Smoke test: every project module imports cleanly against the fakes."""
import importlib

import pytest

MODULES = [
    "conf.config", "log", "store",
    "hardware.lcd", "hardware.lcd_text", "hardware.lcd_widgets", "hardware.lcd_layout",
    "hardware.leds", "hardware.battery",
    "hardware.encoders", "hardware.power",
    "pb.client", "pb.preview", "pb.discovery", "pb.preferred",
    "ui.screens",
    "wifi.scanner", "wifi.manager",
    "app.events", "app.flows", "app.connection", "app.preview",
    "app.battery", "app.power", "app.util",
    "main",
]


@pytest.mark.parametrize("name", MODULES)
def test_import(name):
    assert importlib.import_module(name) is not None
