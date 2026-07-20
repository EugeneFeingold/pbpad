import asyncio
import time
from typing import Optional
from gpiozero import RotaryEncoder, Button
from conf import config

_LONG_PRESS_TIME = 1.0
_BOUNCE_WINDOW = 0.05


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
    """Distinguishes short press (released before hold_time) from long press.

    Also emits button_down on press and button_up on release so the app can
    track simultaneous holds without interfering with normal press logic.
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
        self._held = False

        self._btn = Button(pin, pull_up=True, bounce_time=0.05, hold_time=_LONG_PRESS_TIME)
        if long_event:
            self._btn.when_held = self._on_held
        self._btn.when_pressed = self._on_pressed
        self._btn.when_released = self._on_released

    def _on_pressed(self):
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, ("button_down", self._name)
        )

    def _on_held(self):
        self._held = True
        self._loop.call_soon_threadsafe(self._queue.put_nowait, self._long_event)

    def _on_released(self):
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, ("button_up", self._name)
        )
        if not self._held:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, self._short_event)
        self._held = False

    @property
    def is_pressed(self) -> bool:
        """Raw current state of the switch pin — reflects the button right now,
        not the debounced state. Used to suppress accidental rotation while
        the user is physically pressing the knob."""
        return self._btn.is_pressed

    def close(self):
        self._btn.close()


class EncoderHandler:
    def __init__(self, event_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._queue = event_queue
        self._loop = loop
        self._encoders = []
        self._switches = []

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
        for enc in self._encoders:
            enc.close()
        for sw in self._switches:
            sw.close()
