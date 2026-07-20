"""WiFi setup flows (scan / join / reset) and the Settings-value callbacks.

The flows run off the input loop (spawned as tasks) so network I/O never
blocks event handling; the callbacks persist their values via store.
"""
import asyncio
import time

from conf import config
import log
import store
from wifi import scanner as wifi_scanner
from wifi import manager as wifi_manager
from ui.screens import WifiScanScreen, SettingsScreen, ReconnectScreen


class FlowsMixin:
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
