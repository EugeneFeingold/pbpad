"""Smoke test: every project module imports cleanly against the fakes."""
import importlib

import pytest

MODULES = [
    "config", "log", "store",
    "hardware.lcd", "hardware.leds", "hardware.battery",
    "hardware.encoders", "hardware.power",
    "pb.client", "pb.preview", "pb.discovery", "pb.preferred",
    "ui.screens",
    "wifi.scanner", "wifi.manager",
    "main",
]


@pytest.mark.parametrize("name", MODULES)
def test_import(name):
    assert importlib.import_module(name) is not None
