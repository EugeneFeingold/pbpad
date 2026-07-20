"""Power-related UI states and actions: dim, sleep, lock overlays, and the
shutdown / restart paths.
"""
import asyncio
import os
import signal
import subprocess

import log
from ui.screens import SleepScreen, LockScreen, MainScreen


class PowerMixin:
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
