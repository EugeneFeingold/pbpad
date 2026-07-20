import argparse
import asyncio
import os
import signal
import socket
import subprocess
import threading
import time
from typing import Optional
import config
import log
import store
from hardware.lcd import LCD
from hardware.encoders import EncoderHandler
from hardware.power import PowerButton
from hardware.battery import Battery
from hardware.gauge import Gauge
from hardware.leds import LEDStrip
from wifi import scanner as wifi_scanner
from wifi import manager as wifi_manager
from pb.discovery import discover, _close_stale_discovery_sockets
from pb.client import PixelblazeClient
from pb.preview import PreviewClient
from pb import preferred
from ui.screens import (
    WifiScanScreen,
    PasswordEntryScreen,
    ConnectingScreen,
    DeviceSelectScreen,
    DiscoveringScreen,
    ReconnectScreen,
    IPEntryScreen,
    InfoScreen,
    SettingsScreen,
    MainScreen,
    LockScreen,
    SleepScreen,
    StatusScreen,
)

POLL_INTERVAL = 5
POLL_TIMEOUT = 4         # a poll that doesn't answer in this long = dead connection
POLL_FAIL_LIMIT = 3      # consecutive failed polls before the PB is treated as lost
RECONNECT_INTERVAL = 3   # seconds between reconnect attempts
CONNECT_TIMEOUT = 8      # cap a reconnect attempt so Cancel stays responsive
DIRECT_ATTEMPTS = 3      # try the last-known IP this many times before falling
                         # back to discovery (which catches a DHCP IP change)
RECOVERY_RESTART_SEC = 300  # if recovery gets nowhere this long, restart the
                            # process (systemd Restart=always brings us back).
                            # Last-resort self-heal for an unattended device.


def _fd_count() -> int:
    """Open file-descriptor count for this process (Linux), or -1 elsewhere.
    A steady climb across reconnects is the fingerprint of a socket/thread
    leak — the suspected cause of 'can't reconnect until I restart'."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _local_ip() -> Optional[str]:
    """Return the Pi's outbound IP (the interface used to reach the LAN)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _subnet_prefix() -> str:
    """Return the Pi's IP with the last octet stripped, e.g. '10.0.0.'"""
    ip = _local_ip()
    return ".".join(ip.split(".")[:3]) + "." if ip else ""


def _fmt_uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
    except OSError:
        return "?"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


class App:
    def __init__(self):
        self._lcd = LCD()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()
        self._encoders = EncoderHandler(self._queue, self._loop)
        self._power = PowerButton(self._queue, self._loop, self._do_shutdown)
        self._battery = Battery()
        self._gauge = Gauge()
        self._leds = LEDStrip(config.LED_COUNT)
        self._preview_client: Optional[PreviewClient] = None
        self._led_brightness: int = self._load_led_brightness()
        self._screen = None
        # Navigation stack: every "forward" transition pushes the current
        # screen; every "back" pops it. Replaces the older _pre_settings_screen
        # / _settings_screen 1-slot design so Back returns to the actual
        # previous screen, not always to Settings.
        self._nav_stack: list = []
        self._pb_client: Optional[PixelblazeClient] = None
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

    async def _fps_loop(self):
        """Once per second, snapshot the preview-frame receive and LED-write
        counts for the Info page. Receives = preview frames PB is sending us;
        renders = SPI writes the LED thread actually completed. Comparing
        them tells you where a bottleneck lives."""
        while True:
            await asyncio.sleep(1.0)
            self._recv_shown, self._recv_count = self._recv_count, 0
            self._write_shown, self._write_count = self._write_count, 0

    async def _scroll_loop(self):
        while True:
            await asyncio.sleep(0.08)  # ~12fps; the LCD only flushes changed frames
            self._check_idle()
            # Don't clobber the power prompt / shutdown message.
            if self._screen and not self._shutting_down and not self._power_prompt:
                self._screen.render(self._lcd)

    def _check_idle(self):
        """Dim, then turn the screen off, after the configured idle timeouts."""
        if self._locked or self._shutting_down or self._power_prompt:
            return
        idle = time.monotonic() - self._last_activity
        if not self._sleeping and self._off_timeout is not None and idle >= self._off_timeout:
            self._enter_sleep()  # also pauses the connection manager
        elif not self._dimmed and not self._sleeping and self._dim_timeout is not None \
                and idle >= self._dim_timeout:
            self._enter_dim()

    async def _event_loop(self):
        asyncio.ensure_future(self._scroll_loop())
        while True:
            event = await self._queue.get()
            self._last_activity = time.monotonic()  # any input counts as activity

            # Power button: prompt on press, act on hold, dismiss on early release
            if event == ("power", "hold_start"):
                self._power_prompt = True
                self._lcd.set_backlight(self._backlight_level or 9)
                self._lcd.render_message("Hold to", "shut down")
                continue
            if event == ("power", "hold_cancel"):
                if self._power_prompt:
                    self._power_prompt = False
                    self._restore_display()
                continue

            if self._sleeping:
                self._exit_sleep()
                continue

            if self._dimmed:
                # Half-dim is not real sleep: wake, but still act on this input.
                self._exit_dim()

            # Track raw button state for combined-hold lock detection (always runs)
            if event[0] == "button_down":
                if event[1] == "enc1":
                    self._enc1_down = True
                else:
                    self._enc2_down = True
                if self._locked:
                    self._show_lock_hint()
                if self._enc1_down and self._enc2_down and self._lock_task is None:
                    self._lock_task = asyncio.ensure_future(self._lock_countdown())
                continue
            if event[0] == "button_up":
                if event[1] == "enc1":
                    self._enc1_down = False
                else:
                    self._enc2_down = False
                if self._lock_task:
                    self._lock_task.cancel()
                    self._lock_task = None
                continue

            if self._locked:
                self._show_lock_hint()
                continue

            log.log(log.ENCODER, str(event))

            # Settings is reached in-list now (the "Settings ->" row), not by a
            # long-press. The screen returns transition names handled below.
            if self._screen:
                try:
                    next_screen = await self._screen.handle(event)
                    if next_screen:
                        await self._transition(next_screen)
                    elif (self._queue.empty()
                          and not self._shutting_down
                          and not self._power_prompt):
                        # Coalesce: during a fast knob spin, skip intermediate
                        # repaints and draw once the burst is handled.
                        # Suppressed while shutting down / restarting so the
                        # confirm-then-back pop doesn't flash the parent menu
                        # on top of the "Shutting down" / "Restarting" text.
                        self._screen.render(self._lcd)
                except Exception:
                    import traceback
                    log.log(log.ERROR, "unhandled exception in event loop")
                    traceback.print_exc()

    async def _transition(self, name: str):
        # Fast, non-blocking transitions only: never await network I/O here, so
        # the input loop stays responsive and inputs can't queue up and replay.
        log.log(log.TRANSITION, f"-> {name}")
        if name == "settings":
            self._push_screen(self._make_settings())
        elif name == "lock":
            self._in_menu = False
            await self._enter_lock()
        elif name in ("back_from_settings", "back_from_wifi",
                      "back_from_device_select", "back_from_ip",
                      "back_from_info"):
            self._pop_screen()
        elif name == "wifi_scan":
            asyncio.ensure_future(self._wifi_scan_flow())
        elif name == "password_entry":
            self._replace_screen(PasswordEntryScreen(
                ssid=self._selected_network.ssid,
                on_submit=self._on_password_submit,
                on_cancel=lambda: None,
            ))
        elif name == "connecting":
            self._replace_screen(ConnectingScreen())
        elif name == "ip_entry":
            self._push_screen(IPEntryScreen(
                on_submit=self._on_ip_submit,
                on_cancel=lambda: None,
                default=_subnet_prefix(),
            ))
        elif name == "connecting_ip":
            self._replace_screen(ConnectingScreen())
        elif name == "info":
            info = InfoScreen(collect=self._collect_info)
            await info.start()
            self._push_screen(info)
        elif name == "reset_wifi":
            asyncio.ensure_future(self._reset_wifi_flow())
        elif name == "cancel_reconnect":
            # User gave up on reconnecting to the same PB: abandon the target,
            # unblock any in-flight attempt, and fall into the full search so
            # they can pick a different device.
            self._reconnect_target = None
            self._reconnect_attempts = 0
            self._recovery_started_at = 0.0
            self._force_picker = True
            self._teardown_client()
            self._status("Finding", "PixelBlaze...")
            self._wake_manager()
        elif name == "discovery" or name == "device_select":
            # Switch / rescan devices: show the picker without dropping the
            # current connection (only picking a new one reconnects).
            asyncio.ensure_future(self._open_device_picker())
        elif name == "main":
            # A device was chosen (see _on_device_select); hand off to manager.
            self._in_menu = False
            self._notify_pop(self._screen)
            for s in self._nav_stack:
                self._notify_pop(s)
            self._nav_stack.clear()
            self._wake_manager()

    def _push_screen(self, new_screen):
        """Navigate to a new screen, remembering the current one so Back
        pops back to it."""
        if self._screen is not None:
            self._nav_stack.append(self._screen)
        self._screen = new_screen
        self._in_menu = True
        self._screen.render(self._lcd)

    def _replace_screen(self, new_screen):
        """Swap in a new screen without changing stack depth. For transient
        screens (password entry, connecting spinner) chained within a flow."""
        self._notify_pop(self._screen)
        self._screen = new_screen
        self._screen.render(self._lcd)

    def _pop_screen(self):
        """Go back one step. If we land on a SettingsScreen, refresh its
        live-ish fields (ssid, device name, etc. can change while the user
        was drilled elsewhere). If the stack is now empty we've exited the
        menu tree — release _in_menu and nudge the manager."""
        if not self._nav_stack:
            self._in_menu = False
            self._wake_manager()
            return
        self._notify_pop(self._screen)     # cancel refresh tasks etc.
        self._screen = self._nav_stack.pop()
        if isinstance(self._screen, SettingsScreen):
            self._refresh_settings(self._screen)
        self._screen.render(self._lcd)
        if not self._nav_stack:
            self._in_menu = False
            self._wake_manager()

    @staticmethod
    def _notify_pop(screen):
        """Give a screen a chance to release resources when it's being
        dismissed (popped or replaced). Screens with periodic tasks —
        InfoScreen, etc. — override will_pop to cancel them."""
        if screen is not None and hasattr(screen, "will_pop"):
            try:
                screen.will_pop()
            except Exception as e:
                log.log(log.ERROR, f"will_pop failed on {type(screen).__name__}: {e}")

    def _refresh_settings(self, s: SettingsScreen):
        """Push current App state into a Settings screen we're returning to,
        so a WiFi change (etc.) made in a sub-menu is reflected on Back."""
        s._ssid = self._ssid
        s._device_name = self._pb_client.device_name if self._pb_client else ""
        s._backlight = self._backlight_level
        s._dim_secs = self._dim_timeout
        s._off_secs = self._off_timeout
        s._led_brightness = self._led_brightness

    async def _run_with_spinner(self, line1: str, line2: str, coro):
        # The spinner owns the display while it runs; drop any interactive
        # screen so the scroll loop doesn't redraw it over the spinner.
        self._screen = None
        chars = '|/-\\'
        idx = 0
        task = asyncio.ensure_future(coro)
        while not task.done():
            if not self._shutting_down:
                self._lcd.render_message(line1, f"{line2}  {chars[idx % 4]}")
                idx += 1
            await asyncio.sleep(0.2)
        return await task

    async def _reset_wifi_flow(self):
        """Bounce the Pi's WiFi association, then resume whatever we were doing.
        Triggered by the left knob on the reconnect screen — the Pi staying
        associated to a dead AP is a common failure for a device that moves."""
        log.log(log.CHANGE, "WiFi reset requested")
        # Drop the PB connection first: it's dead anyway once WiFi bounces, and
        # this keeps us from holding a socket across the interface going down.
        self._teardown_client()
        prev_screen = self._screen
        self._screen = None            # let the spinner own the display
        ok = await self._run_with_spinner("Resetting WiFi", "please wait...",
                                          wifi_manager.reset())
        log.log(log.CHANGE, f"WiFi reset {'ok' if ok else 'failed'}")
        self._ssid = await wifi_manager.current_ssid() or ""
        self._lcd.render_message("WiFi reset", "done" if ok else "failed")
        await asyncio.sleep(1.5)
        # Restart recovery from scratch so the fresh association gets a full
        # set of attempts (direct IP first, then discovery).
        self._reconnect_attempts = 0
        self._recovery_started_at = time.monotonic()
        self._screen = prev_screen if isinstance(prev_screen, ReconnectScreen) else None
        if self._screen is not None:
            self._screen.render(self._lcd)
        self._wake_manager()

    async def _wifi_scan_flow(self):
        """Background WiFi scan/connect flow (runs off the input loop)."""
        log.log(log.CHANGE, "WiFi scan started")
        networks = await self._run_with_spinner("Scanning WiFi", "Please wait...", wifi_scanner.scan())
        log.log(log.CHANGE, f"WiFi scan found {len(networks)} network(s)")
        known = await wifi_manager.known_ssids()

        # Auto-connect if a known network is in range
        for net in networks:
            if net.ssid in known:
                log.log(log.CHANGE, f"auto-connecting to {net.ssid}")
                ok = await self._run_with_spinner("Connecting to", net.ssid, wifi_manager.connect(net.ssid))
                if ok:
                    log.log(log.CHANGE, f"WiFi auto-connected to {net.ssid}")
                    await asyncio.sleep(1)
                    self._in_menu = False
                    self._wake_manager()  # let the manager discover + connect
                    return

        log.log(log.CHANGE, "no known networks found, showing scan results")
        self._screen = WifiScanScreen(networks, on_select=self._on_network_select)
        self._screen.render(self._lcd)

    def _on_ip_submit(self, ip: str):
        asyncio.ensure_future(self._connect_by_ip(ip))

    async def _connect_by_ip(self, ip: str):
        from pb.discovery import probe
        log.log(log.CHANGE, f"connecting by IP: {ip}")
        try:
            device = await self._run_with_spinner("Connecting to", ip, probe(ip))
        except Exception as e:
            log.log(log.ERROR, f"probe {ip} failed: {e}")
            self._lcd.render_message("Not found at", ip[:16])
            await asyncio.sleep(2)
            self._screen = IPEntryScreen(
                on_submit=self._on_ip_submit,
                on_cancel=lambda: None,
            )
            self._screen.render(self._lcd)
            return
        self._selected_device = device
        self._in_menu = False
        self._wake_manager()  # manager connects to the chosen device

    def _on_network_select(self, network):
        self._selected_network = network

    def _on_password_submit(self, password: str):
        asyncio.ensure_future(self._do_connect(password))

    async def _do_connect(self, password: str):
        ok = await self._run_with_spinner(
            "Connecting to", self._selected_network.ssid,
            wifi_manager.connect(self._selected_network.ssid, password)
        )
        if ok:
            log.log(log.CHANGE, f"WiFi connected to {self._selected_network.ssid}")
            self._lcd.render_message("Connected!", self._selected_network.ssid[:16])
            await asyncio.sleep(1)
            self._in_menu = False
            self._wake_manager()  # let the manager discover + connect
        else:
            log.log(log.ERROR, f"WiFi connect failed for {self._selected_network.ssid}")
            self._lcd.render_message("Connect failed", "Try again?")
            await asyncio.sleep(2)
            await self._wifi_scan_flow()

    def _on_device_select(self, device):
        # User picked a PB from the list: it becomes the most-recent preferred.
        # Tear down any current connection so the manager connects to this one.
        self._selected_device = device
        self._in_menu = False
        self._teardown_client()
        self._wake_manager()

    async def _open_device_picker(self):
        """Show the device picker from Settings without dropping the current
        connection; only picking a different PB reconnects (see _on_device_select).

        Uses a cancellable DiscoveringScreen (right knob = Back) instead of the
        old blocking spinner, and always transitions to the list — even an
        empty list is useful because it still exposes "Scan Again" and
        "Connect by IP" as ways forward."""
        task = asyncio.ensure_future(discover())
        # "Scan Again" comes from an existing DeviceSelectScreen; treat that
        # as a refresh (no stack push) rather than a new navigation step.
        # Regular entry from Settings does push, so Back returns to Settings.
        if isinstance(self._screen, DeviceSelectScreen):
            self._replace_screen(DiscoveringScreen(on_cancel=task.cancel))
        else:
            self._push_screen(DiscoveringScreen(on_cancel=task.cancel))
        try:
            devices = await task
        except asyncio.CancelledError:
            return  # user pressed Back; the transition already ran
        # If the user navigated away while discover was running (e.g. tapped
        # Back at the exact moment it returned), don't clobber the new screen.
        if not isinstance(self._screen, DiscoveringScreen):
            return
        # Make sure the currently connected PB shows in the list even if
        # discovery missed it, and mark it so the user sees which is active.
        current = self._pb_client
        current_name = current.device_name if current else None
        if current is not None and not any(d.ip == current.ip for d in devices):
            from pb.discovery import PixelblazeDevice
            devices = [PixelblazeDevice(
                ip=current.ip, name=current.device_name, device_id=hash(current.ip),
            )] + list(devices)
        self._replace_screen(DeviceSelectScreen(
            devices, on_select=self._on_device_select, current_name=current_name,
        ))

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

    def _wake_manager(self):
        self._wake.set()

    async def _wait_wake(self, timeout: float):
        """Sleep up to `timeout`, but return early if the manager is nudged."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    def _teardown_client(self):
        """Tear down the PB client + preview client. Returns the executor
        future for the (blocking) socket close so callers that need the socket
        actually gone — e.g. a hard reset before reconnecting — can await it;
        callers that don't care can ignore it (fire-and-forget)."""
        client = self._pb_client
        self._pb_client = None
        fut = None
        if client is not None:
            # Cancel the pending control task here (asyncio state, must be on the
            # loop), then offload only the blocking socket close — running that
            # inline would freeze the UI on a dead PB. Closing also unblocks any
            # read stranded on the dead connection.
            client.cancel_pending()
            fut = self._loop.run_in_executor(None, client.close_socket)
        self._stop_preview_client()
        return fut

    async def _hard_reset(self):
        """Destroy every connection object and its OS resources — the in-process
        equivalent of a program restart. Awaits the PB socket close (so we don't
        pile up connections on the PixelBlaze, which has limited slots), sweeps
        leaked discovery sockets, and logs fd/thread counts so a resource leak
        is visible in the journal."""
        fut = self._teardown_client()
        if fut is not None:
            try:
                await asyncio.wait_for(fut, 5)
            except Exception:
                pass  # a stuck close must not hang recovery
        _close_stale_discovery_sockets()
        log.log(log.CHANGE,
                f"hard reset: fds={_fd_count()} threads={threading.active_count()}")

    def _start_preview_client(self, ip: str):
        """No-op if the preview stream wouldn't be used — either the user has
        LED brightness at 0 or we're in low-battery mode (LEDs owned by the
        flash loop). Callers don't need to know these states; the check lives
        here so every entry point stays simple."""
        self._stop_preview_client()
        if self._led_brightness == 0 or self._low_battery:
            return
        self._preview_client = PreviewClient(ip, self._on_preview_frame)
        self._preview_client.start()

    def _stop_preview_client(self):
        pc = self._preview_client
        self._preview_client = None
        if pc is not None:
            pc.stop_nowait()
        # Stash what we have before dropping it, so reconnecting on the same
        # pattern can replay it. Clearing _last_preview_pattern makes the next
        # frame look like a pattern change, which is what triggers the recall.
        self._remember_pattern_buffer(self._last_preview_pattern, self._frame_buffer)
        self._last_preview_pattern = None
        # Drop the live buffer; otherwise a later restart would interpolate
        # from ancient frames and glitch on the first tick.
        self._frame_buffer = ()

    def _remember_pattern_buffer(self, pattern_id, buf):
        """Stash `buf` as the remembered frames for `pattern_id`.

        Picks lists are stored by reference, not copied — nothing mutates them
        in place (both _extract_picks and _render_from_buffer build new lists),
        so the cache costs only the tuple of references."""
        if pattern_id is None or not buf:
            return
        cache = self._pattern_buffers
        cache.pop(pattern_id, None)   # re-insert so recently used sorts last
        cache[pattern_id] = buf
        while len(cache) > config.LED_PATTERN_CACHE_MAX:
            cache.pop(next(iter(cache)))   # evict least recently used

    def _recall_pattern_buffer(self, pattern_id, now: float) -> tuple:
        """Return remembered frames for `pattern_id`, re-based to end at `now`.

        Cached entries carry absolute monotonic timestamps from when they were
        captured, so they must be shifted forward or the playhead treats them
        as ancient (and the age prune drops them on the next frame). We keep
        only the last LED_PLAYBACK_DELAY_SEC of them: that's exactly enough for
        _render_from_buffer to interpolate immediately, and it leaves the
        re-based frames comfortably inside the 1s prune horizon so they survive
        until live frames have accumulated a full window."""
        buf = self._pattern_buffers.get(pattern_id)
        if not buf:
            return ()
        newest = buf[-1][0]
        recent = tuple(e for e in buf
                       if newest - e[0] <= config.LED_PLAYBACK_DELAY_SEC)
        if not recent:
            return ()
        # Land the newest remembered frame one nominal frame before now, so the
        # live frame appended by this same call follows it cleanly.
        gap = 1.0 / config.PB_PREVIEW_MAX_HZ
        shift = (now - gap) - recent[-1][0]
        return tuple((ts + shift, picks) for ts, picks in recent)

    def _on_preview_frame(self, frame: bytes):
        """Called ON THE PREVIEW THREAD (not the event loop) for each frame we
        choose to process. Doing this work on the loop made the loop the
        bottleneck and starved input handling, so it lives here instead.

        Thread-safety: it only reads scalars owned by the loop (flags, the
        client's pattern id) and publishes `_frame_buffer` by whole-tuple
        assignment, which the LED thread already snapshots atomically. The
        `_pattern_buffers` dict is also touched by _stop_preview_client on the
        loop; the worst case of that race is one lost cache entry.
        Appends the extracted picks to the ring buffer (with arrival timestamp);
        the LED writer thread reads from the buffer at a fixed delay behind now,
        so variable inter-frame gaps get absorbed instead of causing stutter."""
        if (self._shutting_down or not self._leds.ok
                or self._low_battery or self._led_brightness == 0):
            return
        now = time.monotonic()
        # Pattern changed? The buffered frames belong to the old pattern and
        # the playback delay would keep showing them after the switch. Stash
        # them under the outgoing pattern and swap in whatever we remember for
        # the incoming one — returning to a pattern is frequent, and replaying
        # its remembered frames beats running unsmoothed while the window
        # refills. Catches local changes (set_pattern_local updates the id
        # immediately) and ones made elsewhere (the poll picks those up).
        pattern_id = self._pb_client.active_pattern_id if self._pb_client else None
        if pattern_id != self._last_preview_pattern:
            self._remember_pattern_buffer(self._last_preview_pattern,
                                          self._frame_buffer)
            self._last_preview_pattern = pattern_id
            self._frame_buffer = self._recall_pattern_buffer(pattern_id, now)
        picks = self._extract_picks(frame)
        # Drop anything older than 1s (well past the playback window) so a
        # long PB stall doesn't grow the buffer unboundedly; then cap size.
        keep = tuple(f for f in self._frame_buffer if now - f[0] < 1.0)
        keep = keep + ((now, picks),)
        if len(keep) > config.LED_FRAME_BUFFER_MAX:
            keep = keep[-config.LED_FRAME_BUFFER_MAX:]
        self._frame_buffer = keep       # single-attr assign is atomic
        self._recv_count += 1

    @staticmethod
    def _extract_picks(frame: bytes) -> list:
        """Average each LED_STRIP_GROUPS bucket into a single (R,G,B) tuple.
        Runs once per received frame; small (LED_COUNT groups of ~2 pixels)."""
        out = []
        for group in config.LED_STRIP_GROUPS[:config.LED_COUNT]:
            r = g = b = n = 0
            for idx in group:
                base = idx * 3
                if base + 3 <= len(frame):
                    r += frame[base]
                    g += frame[base + 1]
                    b += frame[base + 2]
                    n += 1
            out.append((r // n, g // n, b // n) if n else (0, 0, 0))
        return out

    def _render_from_buffer(self) -> Optional[list]:
        """Sample the frame buffer at (now - LED_PLAYBACK_DELAY_SEC): find the
        two frames whose timestamps bracket the target playback time and
        interpolate between them. Because the playhead advances at real time
        (independent of when frames arrive), variable inter-frame gaps become
        different interpolation weights instead of playback stutter.

        Returns None only when there is nothing buffered at all (no preview
        stream). While the buffer is still filling — at connect, or right
        after a pattern change flushed it — we render the NEWEST frame live
        rather than returning None, so the strip doesn't blank for
        LED_PLAYBACK_DELAY_SEC on every pattern change. Playback settles back
        into delayed interpolation once the window has filled."""
        buf = self._frame_buffer            # atomic snapshot
        if not buf:
            return None
        target = time.monotonic() - config.LED_PLAYBACK_DELAY_SEC
        # Still filling — play live off the newest frame instead of blanking.
        if target < buf[0][0]:
            return buf[-1][1]
        # Find the two frames straddling `target`; if target is past the last
        # frame (PB stalled), hold the last picks as-is.
        prev = buf[0]
        for entry in buf[1:]:
            if entry[0] > target:
                dt = entry[0] - prev[0]
                if dt <= 0:
                    return prev[1]
                f = (target - prev[0]) / dt
                return [
                    (int(r0 + (r1 - r0) * f),
                     int(g0 + (g1 - g0) * f),
                     int(b0 + (b1 - b0) * f))
                    for (r0, g0, b0), (r1, g1, b1) in zip(prev[1], entry[1])
                ]
            prev = entry
        return buf[-1][1]

    def _led_writer_loop(self):
        """Owns the LED strip. Runs in a dedicated thread so SPI writes never
        wait behind the asyncio loop (PIL frame builds, I2C flushes, other
        coroutines), and so all SPI access is serialized to one thread (no
        concurrent show()/off() races on the bus).

        Modes, checked in priority order each iteration:
          - shutting down / no strip: sleep
          - low battery: 500ms red-count flash
          - brightness 0: blank once, sleep
          - no buffered picks (never connected / connection lost): blank once, sleep
          - normal: LED_MAX_FPS interpolated preview writes, wall-clock paced

        `blanked` tracks whether we've most recently sent an all-off frame,
        so idle branches don't re-issue the SPI write every 100ms.
        """
        interval = 1.0 / config.LED_MAX_FPS
        last_flash = 0.0
        flash_on = True
        stop = self._led_thread_stop
        next_tick: Optional[float] = None
        blanked = False

        def blank():
            nonlocal blanked
            if not blanked and self._leds.ok:
                self._leds.off()
                blanked = True

        while not stop.is_set():
            if self._shutting_down or not self._leds.ok:
                stop.wait(0.1); next_tick = None; continue

            if self._low_battery:
                now = time.monotonic()
                if now - last_flash >= 0.5:
                    last_flash = now
                    flash_on = not flash_on
                    if flash_on:
                        frame = bytearray(config.LED_COUNT * 3)
                        for i in range(min(self._low_battery_count, config.LED_COUNT)):
                            frame[i * 3] = 255      # red only
                        self._leds.show(bytes(frame), 0.05)
                        blanked = False
                    else:
                        self._leds.off()
                        blanked = True
                stop.wait(0.05); next_tick = None; continue

            if self._led_brightness == 0:
                blank(); stop.wait(0.1); next_tick = None; continue

            picks = self._render_from_buffer()
            if picks is None:
                # No data — first tick after startup, buffer draining while
                # preview stream is paused, or connection lost. Blank so LEDs
                # don't hold whatever frame we last showed.
                blank(); stop.wait(0.05); next_tick = None; continue

            # Normal render: fixed-clock pacing.
            if next_tick is None:
                next_tick = time.monotonic()
            next_tick += interval

            buf = bytearray()
            for r, g, b in picks:
                buf += bytes([r, g, b])
            self._leds.show(bytes(buf), self._led_brightness / 100.0)
            blanked = False
            self._write_count += 1

            delay = next_tick - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            elif delay < -interval:
                # Fell more than a full frame behind (long stall); resync so
                # we don't burst-fire trying to catch up.
                next_tick = time.monotonic()

    def _status(self, line1: str, line2: str):
        """Show a manager status message. Interactive: a left push opens Settings
        (so the user can change WiFi/device while stuck searching).

        Idempotent: repeat calls with the same text are no-ops. This matters
        because the connection manager loops rapidly through _establish and
        would otherwise churn self._screen — creating a race where a queued
        Enter is dispatched to a fresh StatusScreen instance that never got
        rendered, or gets clobbered mid-dispatch."""
        if isinstance(self._screen, StatusScreen) and self._screen.matches(line1, line2):
            return
        self._screen = StatusScreen(line1, line2)
        self._screen.render(self._lcd)

    async def _connection_manager(self):
        """Single owner of the WiFi -> discover -> connect -> poll lifecycle.

        Used for both initial connection and recovery, so the behavior is
        identical either way. Runs in the background so the input loop never
        blocks. Pauses while asleep/locked (to save power) or while the user is
        in a menu (so it never yanks them out).
        """
        fails = 0
        while True:
            try:
                if self._sleeping or self._locked or self._in_menu or self._low_battery:
                    await self._wait_wake(1.0)
                    continue

                if self._pb_client is not None:
                    try:
                        ok = await asyncio.wait_for(self._pb_client.poll(), POLL_TIMEOUT)
                    except asyncio.TimeoutError:
                        # A slow poll (e.g. contention while the user hammers
                        # controls) is only a *soft* failure — count it, don't
                        # instantly declare the connection lost.
                        ok = False
                    if ok:
                        fails = 0
                        await self._wait_wake(POLL_INTERVAL)
                    else:
                        fails += 1
                        log.log(log.ERROR, f"poll failed ({fails}/{POLL_FAIL_LIMIT})")
                        if fails >= POLL_FAIL_LIMIT:
                            log.log(log.CHANGE, "connection lost")
                            # Reconnect to the SAME device by IP, not whatever
                            # discovery turns up (which may find nothing, or a
                            # different PB). _establish sees the target and
                            # routes to the direct-IP reconnect flow.
                            self._reconnect_target = self._connected_device
                            self._reconnect_attempts = 0
                            self._recovery_started_at = time.monotonic()
                            self._teardown_client()
                            fails = 0
                        else:
                            await self._wait_wake(1.0)  # quick retry after a soft failure
                    continue

                fails = 0
                await self._establish()
                if self._pb_client is None:
                    await self._wait_wake(RECONNECT_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception:
                import traceback
                log.log(log.ERROR, "connection manager error")
                traceback.print_exc()
                self._teardown_client()
                fails = 0
                await asyncio.sleep(RECONNECT_INTERVAL)

    async def _establish(self):
        """One attempt at getting connected. Sets _pb_client + MainScreen on success."""
        # While the device picker is up, wait for the user's choice (below).
        if isinstance(self._screen, DeviceSelectScreen) and self._selected_device is None:
            return
        if not await wifi_manager.is_connected():
            self._status("No WiFi", "connecting...")
            return
        self._ssid = await wifi_manager.current_ssid() or ""
        # A device the user chose explicitly (picker / IP entry) takes priority
        # and overrides any pending reconnect.
        if self._selected_device is not None:
            dev, self._selected_device = self._selected_device, None
            self._reconnect_target = None
            if await self._connect(dev):
                preferred.remember(dev.name)
            return
        # Recovery: reconnect straight to the device we lost, by IP. This skips
        # discovery entirely (a fresh WebSocket to the known IP), which is the
        # same reset a program restart performs — and it works even when UDP
        # discovery has stopped seeing the PB.
        if self._reconnect_target is not None:
            await self._recover_connection()
            return
        self._status("Finding", "PixelBlaze...")
        devices = await discover()
        if self._in_menu:
            return  # user opened Settings during the search; don't stomp it
        if not devices:
            return  # keep "Finding" up; manager loops and tries again
        # Auto-connect to the highest-ranked known PB (unless the user asked to
        # pick a different one from Settings).
        if not self._force_picker:
            dev = preferred.pick(devices)
            if dev is not None:
                if await self._connect(dev):
                    preferred.remember(dev.name)
                return
        # Let the user pick (their choice becomes the most-recent preferred).
        self._force_picker = False
        log.log(log.CHANGE, f"device picker: {', '.join(d.name for d in devices)}")
        self._screen = DeviceSelectScreen(devices, on_select=self._on_device_select)
        self._screen.render(self._lcd)

    async def _connect(self, device) -> bool:
        self._teardown_client()
        self._pb_client = PixelblazeClient(device)
        try:
            await self._run_with_spinner("Connecting to", device.name, self._pb_client.connect())
        except Exception as e:
            import traceback
            log.log(log.ERROR, f"PB connect failed: {e}")
            traceback.print_exc()
            self._teardown_client()
            self._status("PB connect", "failed")
            await asyncio.sleep(2)
            return False
        log.log(log.CHANGE, f"Connected to {device.name} ({device.ip})")
        self._connected_device = device      # reconnect target if we drop
        self._lcd.render_message("Connected!", device.name[:16])
        await asyncio.sleep(1.5)
        self._screen = MainScreen(self._pb_client, self._lcd)
        self._screen.render(self._lcd)
        # Second WebSocket for preview frames -> local WS2812 strip.
        self._start_preview_client(device.ip)
        return True

    async def _recover_connection(self):
        """One recovery attempt for the device we lost (self._reconnect_target).

        Every attempt starts with a hard reset (destroy + recreate all
        connection objects, close their sockets) — the in-process equivalent of
        a restart, since a stale/leaked socket is the suspected reason a plain
        reconnect fails until the program is restarted.

        Escalates: the first DIRECT_ATTEMPTS go straight to the last-known IP
        (fast, no discovery); after that it falls back to discovery to catch a
        DHCP IP change, but still only reconnects to the SAME device by name —
        never auto-jumping to a different PB. Right knob cancels to manual search.
        The interactive ReconnectScreen stays up across attempts."""
        target = self._reconnect_target
        if target is None:
            return
        if not isinstance(self._screen, ReconnectScreen):
            self._screen = ReconnectScreen(target.name)
            self._screen.render(self._lcd)

        # Last-resort self-heal: if we've been stuck in recovery this long, the
        # failure is something we don't understand, so restart the process —
        # systemd (Restart=always) brings us straight back with a clean slate.
        # This device runs unattended outdoors; it must not stay wedged.
        if (self._recovery_started_at
                and time.monotonic() - self._recovery_started_at > RECOVERY_RESTART_SEC):
            log.log(log.ERROR,
                    f"recovery stuck for {RECOVERY_RESTART_SEC}s — restarting software")
            self._restart_software()
            return

        self._reconnect_attempts += 1
        await self._hard_reset()

        if self._reconnect_attempts <= DIRECT_ATTEMPTS:
            dev = target                       # direct to the known IP
        else:
            # Discovery fallback: find the same device (possibly at a new IP).
            devices = await discover()
            if self._reconnect_target is not target:
                return                          # cancelled during discovery
            dev = next((d for d in devices if d.name == target.name), None)
            if dev is None:
                return  # not found this round; keep retrying (or user cancels)

        client = PixelblazeClient(dev)
        self._pb_client = client
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
        except Exception as e:
            log.log(log.ERROR, f"reconnect to {target.name} failed "
                               f"(attempt {self._reconnect_attempts}): {e}")
            self._teardown_client()
            return  # keep the target; the manager retries on its next loop

        # Cancelled (or superseded) while the attempt was in flight?
        if self._reconnect_target is not target:
            self._teardown_client()
            return

        self._reconnect_target = None
        self._reconnect_attempts = 0
        self._recovery_started_at = 0.0
        self._connected_device = dev
        preferred.remember(dev.name)
        log.log(log.CHANGE, f"reconnected to {dev.name} ({dev.ip})")
        self._screen = MainScreen(self._pb_client, self._lcd)
        self._screen.render(self._lcd)
        self._start_preview_client(dev.ip)
        return True

    def _enter_dim(self):
        self._dimmed = True
        dim_level = max(1, self._backlight_level // 2)
        self._lcd.set_backlight(dim_level)
        log.log(log.TRANSITION, f"-> Dim ({dim_level})")

    def _exit_dim(self):
        self._dimmed = False
        self._lcd.set_backlight(self._backlight_level)
        log.log(log.TRANSITION, "-> Wake from dim")

    def _enter_sleep(self):
        # Sleep pauses the connection manager (it checks _sleeping), so we stop
        # polling and reconnecting — no unnecessary work while nobody's looking.
        self._sleeping = True
        self._dimmed = False
        self._pre_sleep_screen = self._screen
        self._lcd.set_backlight(0)
        self._screen = SleepScreen()
        self._screen.render(self._lcd)
        log.log(log.TRANSITION, "-> Sleep")

    def _exit_sleep(self):
        self._sleeping = False
        self._dimmed = False
        self._pre_sleep_screen = None
        log.log(log.TRANSITION, "-> Wake")
        self._restore_main()

    def _restore_main(self):
        """Restore the display after sleep/lock and let the manager resume.

        If still connected, the manager validates via its next poll; if the
        connection died while we slept, that poll fails and it reconnects.
        """
        self._lcd.set_backlight(self._backlight_level)
        if self._pb_client is not None:
            self._screen = MainScreen(self._pb_client, self._lcd)
            self._screen.render(self._lcd)
        else:
            self._screen = None
        self._wake_manager()

    def _show_lock_hint(self):
        if self._lock_hint_task:
            self._lock_hint_task.cancel()
        if isinstance(self._screen, LockScreen):
            self._screen.hint = True
            self._lcd.set_backlight(self._backlight_level)  # light up so it's visible
            self._screen.render(self._lcd)
        self._lock_hint_task = asyncio.ensure_future(self._clear_lock_hint())

    async def _clear_lock_hint(self):
        await asyncio.sleep(3.0)
        self._lock_hint_task = None
        if isinstance(self._screen, LockScreen):
            self._screen.hint = False
            self._lcd.set_backlight(0)  # back to dark while locked
            self._screen.render(self._lcd)

    async def _lock_countdown(self):
        await asyncio.sleep(1.0)
        self._lock_task = None
        if self._enc1_down and self._enc2_down:
            if self._locked:
                await self._exit_lock()
            else:
                await self._enter_lock()

    async def _enter_lock(self):
        # Locking pauses the manager too (it checks _locked).
        self._locked = True
        self._lcd.set_backlight(0)
        self._screen = LockScreen()
        self._screen.render(self._lcd)
        log.log(log.TRANSITION, "-> Locked")

    async def _exit_lock(self):
        self._locked = False
        if self._lock_hint_task:
            self._lock_hint_task.cancel()
            self._lock_hint_task = None
        log.log(log.TRANSITION, "-> Unlocked")
        self._restore_main()

    def _make_settings(self) -> SettingsScreen:
        return SettingsScreen(
            client=self._pb_client,
            backlight_level=self._backlight_level,
            on_backlight_change=self._on_backlight_change,
            device_name=self._pb_client.device_name if self._pb_client else "",
            ssid=self._ssid,
            on_power_off=self._do_shutdown,
            on_restart_software=self._restart_software,
            on_restart_device=self._restart_device,
            dim_secs=self._dim_timeout,
            off_secs=self._off_timeout,
            on_dim_change=self._on_dim_change,
            on_off_change=self._on_off_change,
            led_brightness=self._led_brightness,
            on_led_brightness_change=self._on_led_brightness_change,
        )

    def _on_dim_change(self, secs):
        self._dim_timeout = secs
        store.set("dim_timeout", secs)

    def _on_off_change(self, secs):
        self._off_timeout = secs
        store.set("off_timeout", secs)

    def _on_backlight_change(self, level: int):
        self._backlight_level = level
        self._lcd.set_backlight(level)
        store.set("backlight", level)

    @staticmethod
    def _load_backlight() -> int:
        try:
            return max(1, min(9, int(store.get("backlight", config.BACKLIGHT_LEVEL))))
        except (TypeError, ValueError):
            return config.BACKLIGHT_LEVEL

    def _on_led_brightness_change(self, level: int):
        prev = self._led_brightness
        self._led_brightness = level
        store.set("led_brightness", level)
        # Turning brightness to 0 drops the preview stream (no point paying
        # WebSocket + CPU cost if nothing will render). Turning it back on
        # restarts the stream. LED blanking is handled by the writer thread —
        # never call show()/off() from here or we'd race with it on the SPI
        # bus. Low-battery mode owns both the LEDs and the stream itself.
        if self._low_battery:
            return
        if level == 0:
            self._stop_preview_client()
        elif prev == 0 and self._pb_client is not None:
            self._start_preview_client(self._pb_client.ip)

    @staticmethod
    def _load_led_brightness() -> int:
        try:
            return max(0, min(25, int(store.get("led_brightness", config.LED_BRIGHTNESS_DEFAULT))))
        except (TypeError, ValueError):
            return config.LED_BRIGHTNESS_DEFAULT

    def _restore_display(self):
        """Reapply backlight and re-render after dismissing the power prompt."""
        if self._sleeping or self._locked:
            self._lcd.set_backlight(0)
        elif self._dimmed:
            self._lcd.set_backlight(max(1, self._backlight_level // 2))
        else:
            self._lcd.set_backlight(self._backlight_level)
        if self._screen:
            self._screen.render(self._lcd)

    def _restart_software(self):
        """Exit cleanly; systemd (Restart=always) brings us back up. Send
        SIGTERM to ourselves so the same signal handler that services
        `systemctl restart` runs — that path is tested and does full cleanup
        (GPIO release, socket close, LED blank)."""
        if self._shutting_down:
            return
        self._shutting_down = True    # suppresses further screen re-renders
        log.log(log.INFO, "software restart requested via Settings")
        try:
            self._lcd.set_backlight(self._backlight_level or 9)
            self._lcd.render_message("Restarting...", "please wait")
        except Exception as e:
            log.log(log.ERROR, f"restart display failed: {e}")
        import os
        os.kill(os.getpid(), signal.SIGTERM)

    def _restart_device(self):
        """Reboot the Pi. Requires the pi user to have NOPASSWD sudo for
        /sbin/reboot (setup.sh grants this alongside /sbin/poweroff)."""
        if self._shutting_down:
            return
        self._shutting_down = True
        log.log(log.INFO, "device reboot requested via Settings")
        try:
            self._lcd.set_backlight(self._backlight_level or 9)
            self._lcd.render_message("Rebooting...", "please wait")
        except Exception as e:
            log.log(log.ERROR, f"reboot display failed: {e}")
        try:
            subprocess.Popen(["/usr/bin/sudo", "/sbin/reboot"])
        except Exception as e:
            log.log(log.ERROR, f"reboot failed: {e}")

    def _do_shutdown(self):
        """Shut the Pi down. Called directly from the power button's thread so
        it works at all times — even while the event loop is busy or blocked in
        discovery/connect, when queued events would never be processed. Must be
        synchronous and touch only thread-safe things (the LCD is locked).
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        log.log(log.INFO, "power button held: shutting down")
        try:
            # Make sure the message is visible even if dimmed/asleep/locked.
            self._lcd.set_backlight(self._backlight_level or 9)
            self._lcd.render_message("Shutting down", "Bye!")
        except Exception as e:
            log.log(log.ERROR, f"shutdown display failed: {e}")
        try:
            subprocess.Popen(["/usr/bin/sudo", "/sbin/poweroff"])
        except Exception as e:
            log.log(log.ERROR, f"poweroff failed: {e}")

    def _cleanup(self):
        if self._conn_task:
            self._conn_task.cancel()
        if self._fps_task:
            self._fps_task.cancel()
        # Stop the LED writer thread first, THEN close the strip — otherwise a
        # racing show() call would hit a torn-down SpiDev handle.
        self._led_thread_stop.set()
        if self._led_thread is not None:
            self._led_thread.join(timeout=1.0)
        self._teardown_client()  # also stops the preview client
        self._encoders.close()
        self._power.close()
        self._battery.close()
        self._leds.close()
        self._lcd.close()


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
