import argparse
import asyncio
import os
import signal
import threading
import time
from typing import Optional

from conf import config
import log
import store
from hardware.lcd import LCD
from hardware.encoders import EncoderHandler
from hardware.power import PowerButton
from hardware.battery import Battery
from hardware.gauge import Gauge
from hardware.leds import LEDStrip

from app.events import EventsMixin
from app.flows import FlowsMixin
from app.connection import ConnectionMixin
from app.preview import PreviewMixin
from app.battery import BatteryMixin
from app.power import PowerMixin
# Re-exported so `main._fmt_uptime` etc. stay importable (and so `main.time`
# remains the module the tests patch). The behavior lives in app/.
from app.util import _fd_count, _local_ip, _subnet_prefix, _fmt_uptime  # noqa: F401


class App(EventsMixin, FlowsMixin, ConnectionMixin, PreviewMixin,
          BatteryMixin, PowerMixin):
    """The whole device. Its behavior is split across the app.* mixins by
    responsibility (events, flows, connection, preview, battery, power); this
    class owns construction, the top-level run/cleanup lifecycle, and all the
    shared state the mixins operate on."""

    def __init__(self):
        self._lcd = LCD()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()
        self._encoders = EncoderHandler(self._queue, self._loop)
        self._power = PowerButton(self._queue, self._loop, self._do_shutdown)
        self._battery = Battery()
        self._gauge = Gauge()
        self._leds = LEDStrip(config.LED_COUNT)
        self._preview_client = None
        self._led_brightness: int = self._load_led_brightness()
        self._screen = None
        # Navigation stack: every "forward" transition pushes the current
        # screen; every "back" pops it. Replaces the older _pre_settings_screen
        # / _settings_screen 1-slot design so Back returns to the actual
        # previous screen, not always to Settings.
        self._nav_stack: list = []
        self._pb_client = None
        self._conn_task = None            # the connection-manager task
        # The device we're currently/last connected to, and (when set) the
        # device to reconnect DIRECTLY to by IP after a connection drop —
        # bypassing UDP discovery (which is the flaky part) and never jumping
        # to a different PB without the user's say-so. Cleared by Cancel.
        self._connected_device = None
        self._reconnect_target = None
        self._reconnect_attempts = 0
        self._recovery_started_at = 0.0
        self._in_menu: bool = False       # user is in settings/wifi flow
        self._wake = asyncio.Event()      # nudge the manager to act immediately
        self._force_picker: bool = False  # show the device picker even if one is known
        self._ssid: str = ""              # last-known WiFi network name (for Settings)
        self._selected_network = None
        self._selected_device = None
        self._wifi_scanning: bool = False  # guard: one scan flow at a time
        self._sleeping: bool = False
        self._dimmed: bool = False
        self._locked: bool = False
        self._pre_sleep_screen = None
        self._backlight_level: int = self._load_backlight()
        self._dim_timeout = store.get("dim_timeout", config.DIM_TIMEOUT_DEFAULT)
        self._off_timeout = store.get("off_timeout", config.OFF_TIMEOUT_DEFAULT)
        self._last_activity = time.monotonic()
        self._enc1_down: bool = False
        self._enc2_down: bool = False
        self._power_prompt: bool = False
        self._shutting_down: bool = False
        self._lock_task = None
        self._lock_hint_task = None
        # Low-battery mode: below LOW_BATTERY_PCT we suspend the PB poll and
        # the preview stream, and take direct control of the LED strip to
        # blink a gauge of remaining charge.
        self._low_battery: bool = False
        self._low_battery_count: int = 0
        # Diagnostic: preview frames received from PB per second, and strip
        # writes actually performed. Shown on the Info page. _fps_loop resets
        # the running counters + snapshots into the "_shown" values every 1s.
        self._recv_count: int = 0
        self._write_count: int = 0
        self._recv_shown: int = 0
        self._write_shown: int = 0
        self._last_preview_at: float = 0.0   # monotonic time of last preview frame
        self._fps_task: Optional[asyncio.Task] = None
        # Ring of recent picks: tuple of (timestamp, picks_list). Stored as an
        # immutable tuple so the LED thread can snapshot it in one attribute
        # read (GIL guarantees the reference read is atomic), while the
        # receive callback replaces it wholesale on each new frame.
        self._frame_buffer: tuple = ()
        # Active pattern the buffered frames belong to; a change swaps them.
        self._last_preview_pattern = None
        # pattern_id -> remembered frame buffer, so returning to a pattern can
        # replay it instantly instead of running unsmoothed until the playback
        # window refills. Insertion-ordered; oldest evicted past the cap.
        self._pattern_buffers: dict = {}
        # LED writes run on their own thread so they never compete with the
        # asyncio loop (which spends real time on PIL frame builds + I2C).
        self._led_thread: Optional[threading.Thread] = None
        self._led_thread_stop = threading.Event()

    async def run(self):
        try:
            self._lcd.set_backlight(self._backlight_level)
            self._lcd.render_message("Starting...", "please wait")
            asyncio.ensure_future(self._battery_loop())
            self._fps_task = asyncio.ensure_future(self._fps_loop())
            self._led_thread = threading.Thread(
                target=self._led_writer_loop, daemon=True, name="led-writer",
            )
            self._led_thread.start()
            # The connection manager owns WiFi -> discover -> connect -> poll,
            # for both initial connection and recovery. It runs in the
            # background so the input loop never blocks on network I/O.
            self._conn_task = asyncio.ensure_future(self._connection_manager())
            await self._event_loop()
        finally:
            log.log(log.INFO, "pbpad stopped")
            self._cleanup()

    def _cleanup(self):
        # Every step is isolated: a failure closing one resource must not skip
        # the others, or a restart leaves GPIO/sockets/LEDs half-torn-down.
        def _try(fn, *a):
            try:
                fn(*a)
            except Exception as e:
                log.log(log.ERROR, f"cleanup: {getattr(fn, '__name__', fn)} failed: {e}")

        if self._conn_task:
            self._conn_task.cancel()
        if self._fps_task:
            self._fps_task.cancel()
        # Stop the LED writer thread first, THEN close the strip — otherwise a
        # racing show() call would hit a torn-down SpiDev handle.
        self._led_thread_stop.set()
        if self._led_thread is not None:
            _try(self._led_thread.join, 1.0)
        _try(self._teardown_client)  # also stops the preview client
        _try(self._encoders.close)
        _try(self._power.close)
        _try(self._battery.close)
        _try(self._leds.close)
        _try(self._lcd.close)


async def main():
    app = App()
    task = asyncio.ensure_future(app.run())
    loop = asyncio.get_running_loop()
    # Cancel the task on SIGTERM/SIGINT so app.run()'s `finally: _cleanup()`
    # gets to release GPIOs, close sockets, and blank the LEDs. The previous
    # signal.signal(SIGTERM -> raise SIGINT) trick relied on Python's default
    # SIGINT handler and could skip the finally chain — that's what left enc1
    # rotation dead after `systemctl restart` (stuck kernel-side handler).
    def _request_shutdown():
        log.log(log.INFO, "shutdown signal received")
        task.cancel()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            pass  # e.g. running on Windows during dev
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-level", type=int, default=None)
    parser.add_argument("--reset-battery-gauge", action="store_true",
                        help="Clear the learned battery-max and start relearning")
    args = parser.parse_args()
    level = args.log_level if args.log_level is not None else config.LOG_LEVEL
    log.init(level)
    if args.reset_battery_gauge:
        Gauge.reset()
        log.log(log.INFO, "battery gauge: stored max reset")
    log.log(log.INFO, "pbpad started")
    asyncio.run(main())
    # Force-exit after clean shutdown. A PixelBlaze WebSocket read can strand a
    # non-daemon worker thread on a dead socket; that thread would keep the
    # process — and its bound UDP discovery port (:1889) — alive after SIGTERM,
    # so the systemd-restarted instance couldn't rediscover anything until a
    # full reboot freed the port. os._exit skips the atexit thread-join and lets
    # the kernel reclaim every fd immediately, so a software restart is clean.
    os._exit(0)
