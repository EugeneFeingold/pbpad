import asyncio
import threading
import time
from typing import Optional
from gpiozero import RotaryEncoder, Button
from conf import config

_LONG_PRESS_TIME = 1.0
_BOUNCE_WINDOW = 0.05
_SW_DEBOUNCE = 0.015     # switch must read stable this long before a transition
_SW_POLL = 0.005         # raw-pin sample interval (200 Hz)


class _RotationFilter:
    """Drops rapid CW/CCW reversals caused by mechanical contact bounce."""

    def __init__(self):
        self._last_dir = 0
        self._last_time = 0.0

    def accept(self, direction: int) -> bool:
        now = time.monotonic()
        if direction == self._last_dir or (now - self._last_time) >= _BOUNCE_WINDOW:
            self._last_dir = direction
            self._last_time = now
            return True
        return False


class _SwitchHandler:
    """A polled push-switch. We sample the RAW pin ourselves (see poll()) and
    debounce in software instead of using gpiozero's when_pressed/when_released.

    Why: gpiozero's edge callbacks + bounce_time dropped release edges. A
    release that landed inside the press's debounce window was swallowed, so
    gpiozero's state machine stuck in 'pressed' — the next physical press then
    produced NO event (no transition from its point of view), and our down/up
    tracking stuck too, spuriously arming the two-knob lock. Polling reads the
    truth every cycle, so any missed edge self-heals on the very next sample.

    Emits button_down + the short event on a debounced press, button_up on
    release. With a long_event, the short event is deferred to release so a hold
    can be reclassified (no encoder uses long_event today).
    """

    def __init__(
        self,
        pin: int,
        name: str,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        short_event: tuple,
        long_event: Optional[tuple] = None,
    ):
        self._name = name
        self._queue = queue
        self._loop = loop
        self._short_event = short_event
        self._long_event = long_event

        # No callbacks, no bounce_time — we only ever read the pin.
        self._btn = Button(pin, pull_up=True)

        self._pressed = False        # debounced (reported) state
        self._pressed_at = 0.0
        self._long_fired = False
        self._raw = False            # last raw sample
        self._raw_since = time.monotonic()

    def poll(self):
        """Sample the raw pin once and emit any debounced transition. Called
        repeatedly from EncoderHandler's poll thread."""
        now = time.monotonic()
        raw = bool(self._btn.is_pressed)
        if raw != self._raw:
            # State just changed — start (or restart) the settle timer. Contact
            # bounce keeps resetting this until the pin holds steady.
            self._raw = raw
            self._raw_since = now
            return
        if (now - self._raw_since) < _SW_DEBOUNCE:
            return  # not stable long enough yet

        if raw and not self._pressed:
            self._pressed = True
            self._pressed_at = now
            self._long_fired = False
            self._emit(("button_down", self._name))
            if self._long_event is None:
                self._emit(self._short_event)
        elif not raw and self._pressed:
            self._pressed = False
            self._emit(("button_up", self._name))
            if self._long_event is not None and not self._long_fired:
                self._emit(self._short_event)
        elif (raw and self._pressed and self._long_event is not None
                and not self._long_fired
                and now - self._pressed_at >= _LONG_PRESS_TIME):
            self._long_fired = True
            self._emit(self._long_event)

    def _emit(self, event):
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    @property
    def is_pressed(self) -> bool:
        """Raw current state of the switch pin — a fresh read, so rotation
        suppression reflects the button right now."""
        return bool(self._btn.is_pressed)

    def close(self):
        self._btn.close()


class EncoderHandler:
    def __init__(self, event_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._queue = event_queue
        self._loop = loop
        self._encoders = []
        self._switches = []
        self._poll_stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # New control model: left knob navigates, right knob changes/opens.
        # Presses are encoder-relative ("press","enc1"/"enc2"); the screen
        # decides what they mean. No long-press (removed from the UI).
        encoder_cfg = [
            (config.ENC1_A, config.ENC1_B, config.ENC1_SW, "enc1",
             ("press", "enc1"), None),
            (config.ENC2_A, config.ENC2_B, config.ENC2_SW, "enc2",
             ("press", "enc2"), None),
        ]
        for a, b, sw, enc_name, short_event, long_event in encoder_cfg:
            # Build the switch first so the rotate handlers can consult its
            # raw pressed state — dropping accidental rotations that happen
            # while the user is physically pushing down the same knob.
            sw_handler = None
            if sw is not None:
                sw_handler = _SwitchHandler(
                    sw, enc_name, event_queue, loop, short_event, long_event,
                )
                self._switches.append(sw_handler)

            if a is not None and b is not None:
                filt = _RotationFilter()
                enc = RotaryEncoder(a, b, max_steps=100, wrap=True)
                enc.when_rotated_clockwise = self._make_rotate_handler(enc_name, -1, filt, sw_handler)
                enc.when_rotated_counter_clockwise = self._make_rotate_handler(enc_name, 1, filt, sw_handler)
                self._encoders.append(enc)

        # One background thread polls every switch's raw pin. Rotation stays on
        # gpiozero's callbacks (it works fine); only the buttons are polled.
        if self._switches:
            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="switch-poll",
            )
            self._poll_thread.start()

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            for sw in self._switches:
                try:
                    sw.poll()
                except Exception:
                    pass  # a transient GPIO read error must not kill input
            self._poll_stop.wait(_SW_POLL)

    def _make_rotate_handler(self, name: str, direction: int,
                             filt: _RotationFilter,
                             sw_handler: Optional["_SwitchHandler"]):
        def handler():
            # Suppress rotation while the same knob is physically pressed —
            # a small accidental turn during a push would otherwise move the
            # cursor off the intended row before the press event lands, and
            # the press would fire on the wrong row.
            if sw_handler is not None and sw_handler.is_pressed:
                return
            if filt.accept(direction):
                self._loop.call_soon_threadsafe(
                    self._queue.put_nowait, ("encoder", name, direction)
                )
        return handler

    def close(self):
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=1.0)
        for enc in self._encoders:
            enc.close()
        for sw in self._switches:
            sw.close()
