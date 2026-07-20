"""Root pytest conftest.

Registers fake hardware modules (luma/gpiozero/smbus2/spidev/pixelblaze) in
sys.modules BEFORE any test imports a project module, and puts the repo root
on sys.path so `import config`, `from hardware.lcd import LCD`, etc. resolve.

Also provides shared fixtures. Kept at the repo root so pytest inserts the
root into sys.path automatically; it is NOT deployed to the Pi (deploy.bat
ships an explicit file list that excludes tests).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests import _fakes  # noqa: E402

_fakes.install()

import pytest  # noqa: E402
import config  # noqa: E402
import log  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_fakes():
    """Reset all fake-module class state between tests so instance lists and
    register maps don't leak across cases."""
    _fakes.FakeButton.instances = []
    _fakes.FakeRotaryEncoder.instances = []
    _fakes.FakeDigitalOutputDevice.instances = []
    _fakes.FakeSpiDev.instances = []
    _fakes.FakeSMBus.reset()
    _fakes.FakePixelblaze.reset()
    log._level = 0  # silence logging (no file writes) unless a test opts in
    yield


@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    """Point store at a fresh temp file and clear its module-level cache."""
    path = tmp_path / "pbpad.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", str(path))
    monkeypatch.setattr(store, "_data", None)
    return path


@pytest.fixture
def lcd():
    """A real LCD driving a fake OLED — renders true pixels into PIL."""
    from hardware.lcd import LCD
    return LCD()


class ImmediateLoop:
    """Stand-in event loop: runs call_soon_threadsafe callbacks synchronously
    so gpiozero-thread callbacks can be tested without a real loop."""
    def __init__(self):
        self.scheduled = []

    def call_soon_threadsafe(self, fn, *args):
        self.scheduled.append((fn, args))
        fn(*args)


class ListQueue:
    """Minimal asyncio.Queue stand-in that records put_nowait items."""
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


@pytest.fixture
def fake_loop():
    return ImmediateLoop()


@pytest.fixture
def fake_queue():
    return ListQueue()

