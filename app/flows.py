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
from ui.screens import WifiScanScreen, PasswordEntryScreen, SettingsScreen, ReconnectScreen


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
        """Scan for networks and show the picker. Deliberately does NOT
        auto-connect: NetworkManager already auto-associates to known networks
        on its own, and having pbpad also connect to the strongest known network
        on every scan made the device bounce between networks the user never
        picked. Scanning is now purely "show me what's around so I can choose."
        """
        # One scan at a time. A second trigger (Rescan double-fire, or entering
        # the menu while a scan is mid-flight) would spawn a second flow whose
        # spinner races this one's — two "Please wait…" frames alternating on
        # screen. Ignore overlapping triggers.
        if self._wifi_scanning:
            return
        self._wifi_scanning = True
        try:
            log.log(log.CHANGE, "WiFi scan started")
            networks = await self._run_with_spinner("Scanning WiFi", "Please wait...", wifi_scanner.scan())
            log.log(log.CHANGE, f"WiFi scan found {len(networks)} network(s)")
            self._ssid = await wifi_manager.current_ssid() or self._ssid
            self._screen = WifiScanScreen(networks, on_select=self._on_network_select,
                                          current_ssid=self._ssid)
            self._screen.render(self._lcd)
        finally:
            self._wifi_scanning = False

    def _on_network_select(self, network):
        self._selected_network = network

    async def _wifi_join(self, network, password=None):
        """Join `network`. If it's already known (a stored NM profile) or open,
        connect with the stored/absent credentials and no prompt; ask for a
        password only when we have none, or when a stored/typed one is rejected."""
        if network is None:
            return
        ssid = network.ssid

        # Re-selecting the network we're already on is a deliberate refresh:
        # drop the link and bring it back up on that same network, then rebuild
        # the PixelBlaze link from scratch (shared success path below).
        refresh = (password is None and ssid == self._ssid
                   and await wifi_manager.is_connected())

        if not refresh and password is None and network.secured:
            known = await wifi_manager.known_ssids()
            if ssid not in known:
                self._prompt_password(network)     # unknown secured net -> ask
                return

        if refresh:
            await wifi_manager.prefer(ssid)
            ok = await self._run_with_spinner("Reconnecting", ssid,
                                              wifi_manager.reconnect(ssid))
        else:
            # "Joining WiFi" (not "Connecting to") so it reads as the WiFi step,
            # distinct from the PixelBlaze "Connecting to <device>" that follows.
            ok = await self._run_with_spinner("Joining WiFi", ssid,
                                              wifi_manager.connect(ssid, password))

        if ok:
            log.log(log.CHANGE, f"WiFi connected to {ssid}")
            self._ssid = ssid
            if not refresh:
                await wifi_manager.prefer(ssid)    # keep the user's choice sticky
            self._lcd.render_message("Connected!", ssid[:16])
            await asyncio.sleep(1)
            # (Re)joining drops any existing PixelBlaze link, so rebuild it from
            # scratch: tear down and wipe the recovery target so the manager
            # does a FRESH discovery here instead of hammering a stale IP. This
            # is also why coming back to a network re-finds the PixelBlaze.
            self._teardown_client()
            self._connected_device = None
            self._reconnect_target = None
            self._reconnect_attempts = 0
            self._recovery_started_at = 0.0
            self._force_picker = False
            self._in_menu = False
            self._wake_manager()                   # -> fresh discovery + connect
        else:
            log.log(log.ERROR, f"WiFi connect failed for {ssid}")
            if not refresh and network.secured:
                # A stored password can be stale, or a typed one wrong — re-ask.
                self._lcd.render_message("Wrong password?", "try again")
                await asyncio.sleep(1.5)
                self._prompt_password(network)
            else:
                self._lcd.render_message("Connect failed", "try again")
                await asyncio.sleep(2)
                await self._wifi_scan_flow()

    def _prompt_password(self, network):
        self._selected_network = network
        self._screen = PasswordEntryScreen(
            ssid=network.ssid, on_submit=self._on_password_submit, on_cancel=lambda: None)
        self._screen.render(self._lcd)

    def _on_password_submit(self, password: str):
        asyncio.ensure_future(self._wifi_join(self._selected_network, password))

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
