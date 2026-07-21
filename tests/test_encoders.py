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


# --- _SwitchHandler (polled) -----------------------------------------------
def make_switch(fake_queue, fake_loop):
    return _SwitchHandler(pin=17, name="enc1", queue=fake_queue, loop=fake_loop,
                          short_event=("press", "enc1"), long_event=None)


def settle(sw, clock, pressed):
    """Drive the raw pin to `pressed` and poll until it registers as stable."""
    sw._btn.is_pressed = pressed
    sw.poll()              # notices the change, starts the settle timer
    clock.t += 0.05        # past _SW_DEBOUNCE
    sw.poll()              # stable now -> emits the transition


def test_press_fires_immediately_without_long_event(fake_queue, fake_loop, clock):
    # No long-press to disambiguate -> the action fires on the PRESS transition.
    sw = make_switch(fake_queue, fake_loop)
    settle(sw, clock, True)
    assert fake_queue.items == [("button_down", "enc1"), ("press", "enc1")]


def test_release_only_reports_button_up_without_long_event(fake_queue, fake_loop, clock):
    sw = make_switch(fake_queue, fake_loop)
    settle(sw, clock, True)
    fake_queue.items.clear()
    settle(sw, clock, False)
    assert fake_queue.items == [("button_up", "enc1")]   # no duplicate press


def test_poll_recovers_from_a_missed_edge(fake_queue, fake_loop, clock):
    # The whole point of polling: whatever state we think we're in, a sample
    # that sees the pin released emits button_up and clears — no stuck 'pressed'
    # that would kill the next press or spuriously arm the two-knob lock.
    sw = make_switch(fake_queue, fake_loop)
    settle(sw, clock, True)
    assert sw.is_pressed is True
    fake_queue.items.clear()
    settle(sw, clock, False)
    assert ("button_up", "enc1") in fake_queue.items
    assert sw.is_pressed is False


def test_debounce_ignores_unsettled_change(fake_queue, fake_loop, clock):
    sw = make_switch(fake_queue, fake_loop)
    sw._btn.is_pressed = True
    sw.poll()
    clock.t += 0.005       # shorter than _SW_DEBOUNCE
    sw.poll()
    assert fake_queue.items == []   # not stable long enough -> nothing yet


def test_long_event_mode_defers_press_to_release(fake_queue, fake_loop, clock):
    sw = _SwitchHandler(17, "enc1", fake_queue, fake_loop,
                        ("press", "enc1"), long_event=("long", "enc1"))
    settle(sw, clock, True)
    assert ("press", "enc1") not in fake_queue.items    # not on press
    settle(sw, clock, False)
    assert ("press", "enc1") in fake_queue.items         # on release (not held)


def test_held_emits_long_and_suppresses_short(fake_queue, fake_loop, clock):
    sw = _SwitchHandler(17, "enc1", fake_queue, fake_loop,
                        ("press", "enc1"), long_event=("long", "enc1"))
    settle(sw, clock, True)                    # press -> button_down
    clock.t += encoders._LONG_PRESS_TIME + 0.1
    sw.poll()                                  # still held -> long fires
    settle(sw, clock, False)                   # release
    assert ("long", "enc1") in fake_queue.items
    assert ("press", "enc1") not in fake_queue.items


def test_is_pressed_reflects_pin(fake_queue, fake_loop):
    sw = make_switch(fake_queue, fake_loop)
    sw._btn.is_pressed = True
    assert sw.is_pressed


# --- EncoderHandler --------------------------------------------------------
def test_rotate_emits_encoder_event(fake_queue, fake_loop, clock):
    h = EncoderHandler(fake_queue, fake_loop)
    try:
        enc1_cw = h._encoders[0].when_rotated_clockwise
        enc1_cw()
        assert ("encoder", "enc1", -1) in fake_queue.items
    finally:
        h.close()


def test_rotate_suppressed_while_pressed(fake_queue, fake_loop, clock):
    h = EncoderHandler(fake_queue, fake_loop)
    try:
        # Press enc1's switch, then rotate -> event suppressed.
        h._switches[0]._btn.is_pressed = True
        h._encoders[0].when_rotated_clockwise()
        assert fake_queue.items == []
    finally:
        h.close()


def test_close_closes_everything(fake_queue, fake_loop):
    h = EncoderHandler(fake_queue, fake_loop)
    encs = list(h._encoders)
    sws = list(h._switches)
    h.close()
    assert all(e.closed for e in encs)
    assert all(s._btn.closed for s in sws)
