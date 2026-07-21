"""Battery gauge scaler: anchors displayed 100% to the observed full-charge
maximum of the raw LC709203F RSOC.

The LC709203F's internal voltage-to-percent curve tops at 4.20V. Chargers that
terminate lower (like the IP5306 holding the cell at ~4.10V) leave the raw
RSOC stuck below 100% at full charge. Scaling maps the observed ceiling to
100% so the on-screen readout matches user intuition.

Fully-charged detection: a full hour of raw RSOC readings within a tight
tolerance, above a voltage floor. Each detected event overwrites the stored
max — so aging cells naturally re-anchor lower without any manual reset."""

from collections import deque
from typing import Optional
import time
import store

_STORE_KEY = "battery_max_rsoc"
_SINCE_KEY = "since_charged_min"                         # persisted run-minutes since charge
_WINDOW_MINUTES = 60
_POLL_SEC = 30                                          # matches config.BATTERY_POLL_SEC
_WINDOW_SIZE = (_WINDOW_MINUTES * 60) // _POLL_SEC      # 120 samples
_STABLE_TOLERANCE = 1                                    # max-min raw RSOC across window
_FULL_VOLTAGE_MV = 4000                                  # cell voltage floor to accept as full
_FLUSH_EVERY_MIN = 5                                     # persist the timer at most this often


class Gauge:
    def __init__(self):
        stored = store.get(_STORE_KEY, 0)
        self._max: int = int(stored) if stored else 0
        self._samples: deque = deque(maxlen=_WINDOW_SIZE)
        # "Time since charged" = accumulated RUN-time minutes since the last full
        # charge. Persisted, so it survives a shutdown — but only run-time counts
        # (the Pi has no RTC, so wall clock is unreliable across reboots, and
        # monotonic resets each boot). We keep a stored whole-minute total and
        # add the current session's elapsed run-time on top, live.
        self._since_min: int = int(store.get(_SINCE_KEY, 0) or 0)
        self._flushed: int = self._since_min   # last value written to the store
        self._anchor: float = time.monotonic()  # monotonic base for un-rolled minutes

    @property
    def stored_max(self) -> int:
        return self._max

    def _accrue(self):
        """Roll whole elapsed run-minutes into the persisted total, writing to
        the store only every _FLUSH_EVERY_MIN minutes (SD-wear friendly — the
        live value stays exact regardless of how often we persist)."""
        mins = int((time.monotonic() - self._anchor) // 60)
        if mins <= 0:
            return
        self._since_min += mins
        self._anchor += mins * 60
        if self._since_min - self._flushed >= _FLUSH_EVERY_MIN:
            store.set(_SINCE_KEY, self._since_min)
            self._flushed = self._since_min

    def feed(self, raw_pct: int, mv: Optional[int]):
        """Record a fresh sample. Accrues run-time each call; on a full-charge
        detection (stable, at-voltage window) resets the since-charged timer and
        re-anchors the max (persisted only on change)."""
        self._accrue()
        self._samples.append(raw_pct)
        if len(self._samples) < _WINDOW_SIZE:
            return
        if mv is None or mv < _FULL_VOLTAGE_MV:
            return
        lo, hi = min(self._samples), max(self._samples)
        if hi - lo > _STABLE_TOLERANCE:
            return
        # Full-charge detected — the same signal that re-anchors the max. Reset
        # the timer (even when the max value is unchanged). The anchor always
        # moves so the live count stays 0 while parked on the charger; only
        # WRITE when the persisted value actually changes, so sitting at full
        # doesn't hammer the store every poll.
        self._anchor = time.monotonic()
        if self._since_min != 0 or self._flushed != 0:
            self._since_min = 0
            self._flushed = 0
            store.set(_SINCE_KEY, 0)
        if hi != self._max:
            self._max = hi
            store.set(_STORE_KEY, hi)

    def minutes_since_charged(self) -> int:
        """Accumulated RUN-time minutes since the last full charge. Persisted
        across shutdowns; time spent powered off is not counted."""
        return self._since_min + int((time.monotonic() - self._anchor) // 60)

    def flush(self):
        """Persist the current total. Call on a clean shutdown so no run-time is
        lost between the coarse periodic writes."""
        self._accrue()
        if self._since_min != self._flushed:
            store.set(_SINCE_KEY, self._since_min)
            self._flushed = self._since_min

    def scale(self, raw_pct: int) -> int:
        if self._max <= 0:
            return raw_pct
        return min(100, round(100 * raw_pct / self._max))

    @staticmethod
    def reset():
        """Clear the stored max. Called by main.py's --reset-battery-gauge
        flag before the Gauge is instantiated."""
        store.set(_STORE_KEY, 0)
