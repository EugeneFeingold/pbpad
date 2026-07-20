"""Tests for hardware/power.py — power button + MOSFET wake-gate."""
import pytest

from conf import config
from hardware.power import PowerButton


def make_power(fake_queue, fake_loop, on_shutdown=None):
    return PowerButton(fake_queue, fake_loop, on_shutdown or (lambda: None))


def test_gate_driven_high_at_init(fake_queue, fake_loop):
    pb = make_power(fake_queue, fake_loop)
    # Gate must be HIGH so the MOSFET opens the button->GPIO3 path during run.
    assert pb._gate.value is True
    assert pb._gate.pin == config.POWER_GATE


def test_button_hold_time_from_config(fake_queue, fake_loop):
    pb = make_power(fake_queue, fake_loop)
    assert pb._btn.hold_time == config.POWER_OFF_HOLD_SEC
    assert pb._btn.pin == config.POWER_BTN


def test_press_emits_hold_start(fake_queue, fake_loop):
    pb = make_power(fake_queue, fake_loop)
    pb._on_pressed()
    assert ("power", "hold_start") in fake_queue.items


def test_early_release_emits_cancel(fake_queue, fake_loop):
    pb = make_power(fake_queue, fake_loop)
    pb._on_pressed()
    pb._on_released()
    assert ("power", "hold_cancel") in fake_queue.items


def test_hold_calls_shutdown(fake_queue, fake_loop):
    fired = []
    pb = make_power(fake_queue, fake_loop, on_shutdown=lambda: fired.append(True))
    pb._on_pressed()
    pb._on_held()
    assert fired == [True]


def test_hold_then_release_no_cancel(fake_queue, fake_loop):
    pb = make_power(fake_queue, fake_loop, on_shutdown=lambda: None)
    pb._on_pressed()
    pb._on_held()
    fake_queue.items.clear()
    pb._on_released()
    assert ("power", "hold_cancel") not in fake_queue.items


def test_close_releases_button_and_gate(fake_queue, fake_loop):
    pb = make_power(fake_queue, fake_loop)
    btn, gate = pb._btn, pb._gate
    pb.close()
    assert btn.closed
    assert gate.closed
