"""Tests for hardware/gauge.py — full-charge detection, max scaling, and the
'time since charged' timer (which resets on the same detection)."""
import pytest

import store
import hardware.gauge as gaugemod
from hardware.gauge import Gauge, _WINDOW_SIZE, _FULL_VOLTAGE_MV, _SINCE_KEY


@pytest.fixture(autouse=True)
def _isolate_store(temp_settings):
    # Fresh, empty store per test so a learned battery_max_rsoc can't leak in.
    pass


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(gaugemod.time, "monotonic", c)
    return c


def _fill_full(g, pct=95, mv=4100):
    """Feed a full, stable, at-voltage window — triggers full-charge detection."""
    for _ in range(_WINDOW_SIZE):
        g.feed(pct, mv)


# --- scaling ---------------------------------------------------------------
def test_scale_passthrough_without_max():
    assert Gauge().scale(80) == 80          # no learned max -> raw


def test_scale_anchors_to_detected_max(clock):
    g = Gauge()
    _fill_full(g, pct=90, mv=4100)
    assert g.stored_max == 90
    assert g.scale(90) == 100
    assert g.scale(45) == 50


# --- detection guards ------------------------------------------------------
def test_no_detection_below_voltage_floor(clock):
    g = Gauge()
    _fill_full(g, pct=90, mv=_FULL_VOLTAGE_MV - 1)
    assert g.stored_max == 0                # below floor -> not "full"


def test_no_detection_when_unstable(clock):
    g = Gauge()
    for i in range(_WINDOW_SIZE):
        g.feed(90 + (i % 3), 4100)          # varies by 2 > tolerance
    assert g.stored_max == 0


def test_partial_window_no_detection(clock):
    g = Gauge()
    for _ in range(_WINDOW_SIZE - 1):       # one short of a full window
        g.feed(95, 4100)
    assert g.stored_max == 0


# --- time since charged (run-time, persisted) ------------------------------
def test_minutes_since_charged_counts_run_time(clock):
    g = Gauge()                             # anchor at t=1000, since_min=0
    clock.t = 1000 + 90 * 60                # 90 minutes of run-time later
    assert g.minutes_since_charged() == 90


def test_detection_resets_the_timer(clock):
    g = Gauge()
    clock.t = 1000 + 120 * 60
    assert g.minutes_since_charged() == 120
    _fill_full(g, pct=95, mv=4100)          # full charge detected now
    assert g.minutes_since_charged() == 0
    clock.t += 45 * 60
    assert g.minutes_since_charged() == 45


def test_timer_resets_even_when_max_unchanged(clock):
    g = Gauge()
    _fill_full(g, pct=95, mv=4100)          # max=95, timer reset
    assert g.stored_max == 95
    clock.t += 30 * 60
    _fill_full(g, pct=95, mv=4100)          # same level: max unchanged...
    assert g.stored_max == 95
    assert g.minutes_since_charged() == 0   # ...but the timer still reset


def test_no_reset_without_detection(clock):
    g = Gauge()
    clock.t = 1000 + 20 * 60
    _fill_full(g, pct=90, mv=_FULL_VOLTAGE_MV - 1)   # below floor -> no detect
    assert g.minutes_since_charged() == 20           # still counting run-time


def test_persists_across_restart_excluding_off_time(clock, monkeypatch):
    # 40 min of run-time, persist, then "reboot": a fresh Gauge (monotonic reset
    # to an unrelated base, store re-read from disk) continues from 40 — the
    # time the device was OFF is never counted.
    g1 = Gauge()                            # anchor=1000
    clock.t = 1000 + 40 * 60
    g1.flush()
    monkeypatch.setattr(store, "_data", None)   # force re-read from file (reboot)
    clock.t = 5000                          # device was off; new monotonic base
    g2 = Gauge()                            # loads since_min=40, anchor=5000
    assert g2.minutes_since_charged() == 40      # off-time excluded
    clock.t = 5000 + 10 * 60
    assert g2.minutes_since_charged() == 50      # keeps accruing run-time


def test_flush_persists_current_total(clock):
    g = Gauge()
    clock.t = 1000 + 7 * 60
    g.flush()
    assert store.get(_SINCE_KEY) == 7


def test_accrual_persists_only_coarsely(clock, monkeypatch):
    # SD-wear: the timer is written at most every _FLUSH_EVERY_MIN minutes, even
    # though minutes_since_charged() stays exact.
    writes = []
    real_set = store.set
    monkeypatch.setattr(store, "set",
                        lambda k, v: writes.append((k, v)) or real_set(k, v))
    g = Gauge()
    clock.t = 1000 + 4 * 60
    g.feed(50, 3500)                        # 4 min accrued (< 5) -> not persisted
    assert not any(k == _SINCE_KEY for k, v in writes)
    clock.t = 1000 + 6 * 60
    g.feed(50, 3500)                        # crossed 5 -> persisted at 6
    assert (_SINCE_KEY, 6) in writes
