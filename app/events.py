"""The input/event loop, the navigation stack, and screen transitions.

Every hardware event (encoder turn, button, power) lands on a single asyncio
queue; `_event_loop` drains it. Navigation is a plain stack: a "forward"
transition pushes the current screen, "back" pops it.
"""
import asyncio
import time

import log
from ui.screens import (
    ConnectingScreen,
    IPEntryScreen,
    InfoScreen,
    SettingsScreen,
)

from app.util import _subnet_prefix


class EventsMixin:
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

            # A press while BOTH knobs are down is the lock gesture forming, not
            # a navigation press. Presses now fire on the press edge, so without
            # this a knob in a lock-combo would also trigger its screen action.
            if event[0] == "press" and self._enc1_down and self._enc2_down:
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
        elif name == "wifi_join":
            # A network was picked in the list; the flow decides whether it can
            # use stored credentials or needs to prompt for a password.
            asyncio.ensure_future(self._wifi_join(self._selected_network))
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

    def _leave_menu_for_recovery(self):
        """A connection drop was detected while the user was in a menu. Tear the
        menu tree down (running each screen's will_pop cleanup) and release
        _in_menu, so the ReconnectScreen recovery is about to show isn't
        stranded on top of a stale nav stack — Back/Cancel from it would
        otherwise walk back into dead Settings sub-screens. Recovery replaces
        self._screen with the ReconnectScreen on its next loop."""
        self._notify_pop(self._screen)
        for s in self._nav_stack:
            self._notify_pop(s)
        self._nav_stack.clear()
        self._in_menu = False

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
