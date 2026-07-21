"""Second WebSocket to the Pixelblaze, dedicated to preview frames.

A separate connection means the preview poll can never queue behind a control
send on the interactive client and add latency to user input — nothing is
shared: different WebSocket, different pixelblaze-client instance, different
thread. `getPreviewFrame()` is a blocking call that returns after the next
render cycle, so it lives in its own executor thread; each frame is handed
off to the asyncio loop via `call_soon_threadsafe`.

The session self-heals: if the WebSocket drops or the call raises, the loop
sleeps a beat and reconnects. `stop()` cleanly tears everything down (closes
the socket, which unblocks a stuck `getPreviewFrame`, then joins the task).
"""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from conf import config
from pb import pools
import log

# Socket timeout for the preview WebSocket. A dead/half-open connection would
# otherwise block getPreviewFrame() (a raw ws.recv) forever, so the session
# never exits and the supervisor never reconnects. Frames arrive many times a
# second, so this only ever fires on a genuinely dead link.
_WS_TIMEOUT = 5.0
# If we go this long with no usable frame, treat the stream as dead and let the
# supervisor reconnect — covers the case where the library returns empty rather
# than raising on a broken socket.
_STALL_SEC = 5.0


class PreviewClient:
    def __init__(self, ip: str, on_frame: Callable[[bytes], None]):
        self._ip = ip
        # NOTE: on_frame is invoked on the session thread, NOT the event loop.
        # It must be cheap and touch only state that is safe to mutate from a
        # worker (see App._on_preview_frame).
        self._on_frame = on_frame
        # The PB's own render rate, scraped from the stats packets that share
        # this stream. Written by the session thread, read by the event loop.
        self._device_fps = None
        self._stats_raw = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._pb = None

    def start(self):
        self._loop = asyncio.get_event_loop()
        self._stop = False
        self._task = asyncio.ensure_future(self._supervisor())

    async def _supervisor(self):
        while not self._stop:
            try:
                await self._loop.run_in_executor(self._executor, self._run_session)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.log(log.ERROR, f"preview session error: {e}")
            if not self._stop:
                await asyncio.sleep(2)

    def _run_session(self):
        """Blocking: open PB, enable preview frames, loop reading frames.
        Runs on the executor thread. Exits when self._stop is set or the
        socket dies (both surface as an exception on getPreviewFrame)."""
        from pixelblaze import Pixelblaze
        pb = Pixelblaze(self._ip)
        self._pb = pb
        # Bound every blocking socket op (recv/close) so a dead link surfaces
        # instead of wedging this thread forever. Defensive: no-op if the
        # library ever changes the attribute.
        try:
            pb.ws.settimeout(_WS_TIMEOUT)
        except Exception:
            pass
        try:
            try:
                pb.setSendPreviewFrames(True)
            except Exception as e:
                log.log(log.ERROR, f"setSendPreviewFrames failed: {e}")
                return
            # ALWAYS drain; process at most PB_PREVIEW_MAX_HZ of what we drain.
            #
            # getPreviewFrame() is a plain ws.recv() — it returns the OLDEST
            # unread message, not the newest, and sends NOTHING to the PB
            # (frames are pushed unsolicited once sendUpdates is on). So
            # reading slower than the PB sends does not slow the PB down; it
            # just queues frames in the socket, and every frame we pull gets
            # staler until the preview runs seconds behind. That was a real
            # bug. Never sleep here — always take every frame off the wire and
            # rate-limit by DROPPING, which costs a `continue`.
            #
            # on_frame runs on THIS thread, not the event loop. Handing every
            # frame to the loop made the loop the bottleneck and starved input
            # handling (sluggish knobs, and on a 680-pixel PB a hard freeze).
            # Keeping the work here leaves the loop free for the UI, and the
            # GIL is released for the whole of the blocking recv above.
            min_interval = 1.0 / config.PB_PREVIEW_MAX_HZ
            next_time = 0.0
            last_frame_at = time.monotonic()
            while not self._stop:
                frame = pb.getPreviewFrame()
                now = time.monotonic()
                if not frame:
                    # A broken socket can return empty instead of raising; bail
                    # after a stall so the supervisor reconnects rather than
                    # spinning here (and holding the stream dead) forever.
                    if now - last_frame_at > _STALL_SEC:
                        log.log(log.ERROR, "preview stream stalled — reconnecting")
                        return
                    continue
                last_frame_at = now
                if now < next_time:
                    continue      # over budget: drop it, but stay drained
                next_time = now + min_interval
                try:
                    self._on_frame(bytes(frame))
                except Exception as e:
                    log.log(log.ERROR, f"preview frame handler failed: {e}")
                # The PB interleaves a stats packet (~1/s) into this same
                # stream; wsReceive caches it for free. Parse only when it
                # actually changes so this costs nothing per frame.
                raw = getattr(pb, "latestStats", None)
                if raw and raw != self._stats_raw:
                    self._stats_raw = raw
                    try:
                        self._device_fps = json.loads(raw).get("fps")
                    except Exception:
                        pass
        finally:
            try:
                pb.setSendPreviewFrames(False)
            except Exception:
                pass
            try:
                pb._close()
            except Exception:
                pass
            self._pb = None

    @property
    def device_fps(self):
        return self._device_fps

    def stop_nowait(self):
        """Signal shutdown from the event loop; don't wait for it.

        The blocking socket close is offloaded to the dedicated TEARDOWN pool
        (not our own executor, which is busy in getPreviewFrame, and not the
        shared default one, where a hung close could starve discovery). Closing
        the socket unblocks getPreviewFrame so the session-runner exits and the
        supervisor task ends."""
        self._stop = True
        pb = self._pb
        if pb is not None:
            loop = self._loop or asyncio.get_event_loop()
            loop.run_in_executor(pools.TEARDOWN, self._safe_close, pb)
        if self._task is not None:
            self._task.cancel()
        self._executor.shutdown(wait=False)

    @staticmethod
    def _safe_close(pb):
        try:
            pb._close()
        except Exception:
            pass
