"""Tests for hardware/encoders.py — rotation filter, switch handler, wiring."""
import pytest

from hardware import encoders
from hardware.encoders import _RotationFilter, _SwitchHandler, EncoderHandler


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(encoders.time, "monotonic", c)
    return c


# --- _RotationFilter -------------------------------------------------------
def test_filter_accepts_first(clock):
    assert _RotationFilter().accept(1)


def test_filter_accepts_same_direction_immediately(clock):
    f = _RotationFilter()
    assert f.accept(1)
    assert f.accept(1)   # same dir, no delay needed


def test_filter_rejects_quick_reversal(clock):
    f = _RotationFilter()
    assert f.accept(1)
    clock.t += 0.01      # within _BOUNCE_WINDOW (0.05)
    assert not f.accept(-1)


def test_filter_accepts_reversal_after_window(clock):
    f = _RotationFilter()
    assert f.accept(1)
    clock.t += 0.06      # past the window
    assert f.accept(-1)


# --- _SwitchHandler --------------------------------------------------------
def make_switch(fake_queue, fake_loop):
    return _SwitchHandler(pin=17, name="enc1", queue=fake_queue, loop=fake_loop,
                          short_event=("press", "enc1"), long_event=None)


def test_press_emits_button_down(fake_queue, fake_loop):
    sw = make_switch(fake_queue, fake_loop)
    sw._on_pressed()
    assert fake_queue.items == [("button_down", "enc1")]


def test_short_release_emits_press(fake_queue, fake_loop):
    sw = make_switch(fake_queue, fake_loop)
    sw._on_pressed()
    sw._on_released()
    assert ("button_up", "enc1") in fake_queue.items
    assert ("press", "enc1") in fake_queue.items


def test_held_suppresses_short_press(fake_queue, fake_loop):
    sw = _SwitchHandler(17, "enc1", fake_queue, fake_loop,
                        ("press", "enc1"), long_event=("long", "enc1"))
    sw._on_pressed()
    sw._on_held()
    sw._on_released()
    assert ("long", "enc1") in fake_queue.items
    assert ("press", "enc1") not in fake_queue.items


def test_is_pressed_reflects_pin(fake_queue, fake_loop):
    sw = make_switch(fake_queue, fake_loop)
    sw._btn.is_pressed = True
    assert sw.is_pressed


# --- EncoderHandler --------------------------------------------------------
def test_rotate_emits_encoder_event(fake_queue, fake_loop, clock):
    h = EncoderHandler(fake_queue, fake_loop)
    enc1_cw = h._encoders[0].when_rotated_clockwise
    enc1_cw()
    assert ("encoder", "enc1", -1) in fake_queue.items


def test_rotate_suppressed_while_pressed(fake_queue, fake_loop, clock):
    h = EncoderHandler(fake_queue, fake_loop)
    # Press enc1's switch, then rotate -> event suppressed.
    h._switches[0]._btn.is_pressed = True
    h._encoders[0].when_rotated_clockwise()
    assert fake_queue.items == []


def test_close_closes_everything(fake_queue, fake_loop):
    h = EncoderHandler(fake_queue, fake_loop)
    encs = list(h._encoders)
    sws = list(h._switches)
    h.close()
    assert all(e.closed for e in encs)
    assert all(s._btn.closed for s in sws)
