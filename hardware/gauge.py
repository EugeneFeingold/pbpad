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
import store

_STORE_KEY = "battery_max_rsoc"
_WINDOW_MINUTES = 60
_POLL_SEC = 30                                          # matches config.BATTERY_POLL_SEC
_WINDOW_SIZE = (_WINDOW_MINUTES * 60) // _POLL_SEC      # 120 samples
_STABLE_TOLERANCE = 1                                    # max-min raw RSOC across window
_FULL_VOLTAGE_MV = 4000                                  # cell voltage floor to accept as full


class Gauge:
    def __init__(self):
        stored = store.get(_STORE_KEY, 0)
        self._max: int = int(stored) if stored else 0
        self._samples: deque = deque(maxlen=_WINDOW_SIZE)

    @property
    def stored_max(self) -> int:
        return self._max

    def feed(self, raw_pct: int, mv: Optional[int]):
        """Record a fresh sample. If the window is full, stable, and above the
        voltage floor, update the stored max (persisted only on change)."""
        self._samples.append(raw_pct)
        if len(self._samples) < _WINDOW_SIZE:
            return
        if mv is None or mv < _FULL_VOLTAGE_MV:
            return
        lo, hi = min(self._samples), max(self._samples)
        if hi - lo > _STABLE_TOLERANCE:
            return
        if hi != self._max:
            self._max = hi
            store.set(_STORE_KEY, hi)

    def scale(self, raw_pct: int) -> int:
        if self._max <= 0:
            return raw_pct
        return min(100, round(100 * raw_pct / self._max))

    @staticmethod
    def reset():
        """Clear the stored max. Called by main.py's --reset-battery-gauge
        flag before the Gauge is instantiated."""
        store.set(_STORE_KEY, 0)
