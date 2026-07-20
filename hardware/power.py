import asyncio
import time
from typing import Callable

from gpiozero import Button, DigitalOutputDevice

import config
import log


class PowerButton:
    """Physical power button (software half).

    On press it enqueues ("power", "hold_start") so the app can prompt
    "Hold to shut down", and on early release ("power", "hold_cancel") to
    dismiss it. Those are best-effort UI hints routed through the event loop.

    The shutdown itself is NOT routed through the event loop: when the button
    is held for config.POWER_OFF_HOLD_SEC, on_shutdown() is invoked directly on
    gpiozero's callback thread, so it fires at all times — even while the loop
    is busy or blocked in discovery/connect and queued events would never be
    processed. on_shutdown must therefore be synchronous and thread-safe.

    Power-ON is a hardware feature of the Pi's GPIO3 wake pin: wire the same
    button to GPIO3 (physical pin 5) so pressing it while halted boots the board.
    A P-MOSFET (gate on POWER_GATE) sits between the button and GPIO3 to
    isolate the wake path while pbpad is running — otherwise holding the
    button would pull SCL low and freeze the OLED. The gate defaults LOW
    (external pull-down) so the MOSFET conducts when halted; we drive it
    HIGH here at startup to open the path.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        on_shutdown: Callable[[], None],
    ):
        self._queue = queue
        self._loop = loop
        self._on_shutdown = on_shutdown
        self._held = False
        self._pressed_at = 0.0
        # Drive the MOSFET gate HIGH immediately so the button-to-GPIO3 path
        # is open by the time anyone touches the display. On process exit the
        # pin releases and the external pull-down re-enables wake.
        self._gate = DigitalOutputDevice(config.POWER_GATE, initial_value=True)
        self._btn = Button(
            config.POWER_BTN,
            pull_up=True,
            bounce_time=0.05,
            hold_time=config.POWER_OFF_HOLD_SEC,
        )
        self._btn.when_pressed = self._on_pressed
        self._btn.when_held = self._on_held
        self._btn.when_released = self._on_released

    def _emit(self, event: tuple):
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _on_pressed(self):
        self._pressed_at = time.monotonic()
        self._held = False
        log.log(log.NETWORK, "power button: pressed")
        self._emit(("power", "hold_start"))

    def _on_held(self):
        self._held = True
        held_for = time.monotonic() - self._pressed_at
        log.log(log.NETWORK, f"power button: held ({held_for:.2f}s) -> shutdown")
        # Runs on gpiozero's thread — deliberately bypasses the event loop.
        self._on_shutdown()

    def _on_released(self):
        held_for = time.monotonic() - self._pressed_at
        log.log(log.NETWORK,
                f"power button: released after {held_for:.2f}s (held_flag={self._held})")
        if not self._held:
            self._emit(("power", "hold_cancel"))
        self._held = False

    def close(self):
        self._btn.close()
        # Release the gate pin — the external pull-down brings the MOSFET
        # back on, so a subsequent button press can wake the halted Pi.
        self._gate.close()
