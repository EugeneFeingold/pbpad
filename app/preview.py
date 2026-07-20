"""Preview-stream -> LED-strip rendering.

The PixelBlaze pushes preview frames over a second WebSocket; those frames are
buffered and interpolated, then written to the onboard WS2812 strip on a
dedicated thread so SPI writes never wait behind the asyncio loop.
"""
import asyncio
import threading
import time
from typing import Optional

from conf import config
from pb.preview import PreviewClient


class PreviewMixin:
    async def _fps_loop(self):
        """Once per second, snapshot the preview-frame receive and LED-write
        counts for the Info page. Receives = preview frames PB is sending us;
        renders = SPI writes the LED thread actually completed. Comparing
        them tells you where a bottleneck lives."""
        while True:
            await asyncio.sleep(1.0)
            self._recv_shown, self._recv_count = self._recv_count, 0
            self._write_shown, self._write_count = self._write_count, 0

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
