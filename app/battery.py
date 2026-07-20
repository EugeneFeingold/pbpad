"""Fuel-gauge polling, low-battery conservation mode, and the Info page.

Below LOW_BATTERY_PCT the app suspends the PB poll and the preview stream and
hands the LED strip to the writer thread's red-flash gauge (see preview.py).
"""
import asyncio

from conf import config
import log
from wifi import manager as wifi_manager

from app.util import _local_ip, _fmt_uptime


class BatteryMixin:
    async def _battery_loop(self):
        while True:
            if not self._shutting_down:
                raw_pct = await self._battery.read_percent()
                mv = await self._battery.read_millivolts()
                if raw_pct is not None:
                    self._gauge.feed(raw_pct, mv)
                    scaled = self._gauge.scale(raw_pct)
                    self._lcd.set_battery(str(scaled))
                    self._check_low_battery(scaled)
            await asyncio.sleep(config.BATTERY_POLL_SEC)

    def _check_low_battery(self, pct: int):
        """Enter/refresh/leave low-battery mode as the reading crosses 10%."""
        if pct < config.LOW_BATTERY_PCT:
            count = max(0, pct - config.LOW_BATTERY_FLOOR_PCT)
            if not self._low_battery:
                self._enter_low_battery(count)
            else:
                self._low_battery_count = count
        elif self._low_battery:
            self._exit_low_battery()

    def _enter_low_battery(self, count: int):
        self._low_battery = True
        self._low_battery_count = count
        log.log(log.INFO, "low battery: entering conservation mode")
        # Suspend the PB poll (manager checks _low_battery on next iteration)
        # and drop the preview stream. The LED writer thread sees the flag
        # flip on its next tick and switches to red-flash mode.
        self._stop_preview_client()

    def _exit_low_battery(self):
        self._low_battery = False
        log.log(log.INFO, "low battery: exiting conservation mode")
        # Writer thread sees the flag flip and stops flashing; it'll blank
        # the strip on the next tick if no preview frames are buffered yet.
        # Resume PB poll + preview stream (if we're still connected).
        self._wake_manager()
        if self._pb_client is not None and self._led_brightness > 0:
            self._start_preview_client(self._pb_client.ip)

    async def _collect_info(self) -> dict:
        mv = await self._battery.read_millivolts()
        raw_pct = await self._battery.read_percent()
        scaled = self._gauge.scale(raw_pct) if raw_pct is not None else None
        stored_max = self._gauge.stored_max
        dbm = wifi_manager.signal_dbm()
        return {
            "Voltage": f"{mv/1000:.3f}V" if mv is not None else "n/a",
            "Raw %":   f"{raw_pct}%" if raw_pct is not None else "n/a",
            "Scaled":  f"{scaled}%" if scaled is not None else "n/a",
            "Max":     f"{stored_max}%" if stored_max > 0 else "unset",
            "Uptime":  _fmt_uptime(),
            "SSID":    self._ssid or "n/a",
            "Signal":  f"{dbm} dBm" if dbm is not None else "n/a",
            "IP":      _local_ip() or "n/a",
            "Device FPS":        self._device_fps_text(),
            "Preview Reqs/s":    str(self._recv_shown),
            "Preview Renders/s": str(self._write_shown),
        }

    def _device_fps_text(self) -> str:
        """The PixelBlaze's own render rate. Prefer the preview connection —
        the PB interleaves stats into that stream every second, so it's the
        freshest — and fall back to the interactive client's last poll when
        the preview stream isn't running (LED brightness 0, low battery)."""
        for source in (self._preview_client, self._pb_client):
            fps = getattr(source, "device_fps", None) if source else None
            if fps is not None:
                return f"{float(fps):.0f}"
        return "n/a"
