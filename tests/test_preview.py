"""Tests for pb/preview.py — the second-WebSocket preview stream client."""
import pytest

from conf import config
import pixelblaze
import pb.preview as prev
from pb.preview import PreviewClient


def test_stop_nowait_no_task_no_pb():
    pc = PreviewClient("1.2.3.4", on_frame=lambda f: None)
    pc.stop_nowait()   # no task, no pb -> must not raise
    assert pc._stop is True


def test_run_session_streams_nonempty_frames(monkeypatch, fake_loop):
    monkeypatch.setattr(prev.time, "sleep", lambda s: None)   # no real pacing
    # Advance well past the rate limit each read so every frame is processed —
    # this test is about streaming and teardown, not rate limiting.
    clock = [1000.0]
    monkeypatch.setattr(prev.time, "monotonic",
                        lambda: clock.__setitem__(0, clock[0] + 1) or clock[0])

    got = []
    pc = PreviewClient("1.2.3.4", on_frame=got.append)
    pc._loop = fake_loop
    pc._stop = False

    frames = [b"\x01\x02\x03", b"", b"\x04\x05\x06"]
    state = {"i": 0}
    made = []

    class PB:
        def __init__(self, ip):
            self.ip = ip
            self.closed = False
            self.preview_calls = []
            made.append(self)

        def setSendPreviewFrames(self, on):
            self.preview_calls.append(on)

        def getPreviewFrame(self):
            i = state["i"]
            state["i"] += 1
            if i >= len(frames):
                pc._stop = True
                return b""
            return frames[i]

        def _close(self):
            self.closed = True

    monkeypatch.setattr(pixelblaze, "Pixelblaze", PB)

    pc._run_session()

    # Only non-empty frames are posted, as bytes, via the loop.
    assert got == [b"\x01\x02\x03", b"\x04\x05\x06"]
    pb = made[0]
    assert pb.preview_calls[0] is True     # enabled at start
    assert pb.preview_calls[-1] is False   # disabled on teardown
    assert pb.closed                       # socket closed on teardown


class SlowLoop:
    """Event loop stand-in whose callbacks only run when we say so — lets a
    test hold a frame 'in flight' and observe the backpressure."""

    def __init__(self):
        self.pending = []

    def call_soon_threadsafe(self, fn, *args):
        self.pending.append((fn, args))

    def drain(self):
        while self.pending:
            fn, args = self.pending.pop(0)
            fn(*args)


def _session_with(monkeypatch, loop, n_frames=10, clock_step=0.0):
    """Run one _run_session serving n_frames then stopping.

    Returns (reads, processed). `clock_step` advances the fake clock per read
    so the processing rate limit can be exercised deterministically."""
    t = [1000.0]
    monkeypatch.setattr(prev.time, "monotonic", lambda: t[0])
    processed = []
    reads = [0]

    pc = PreviewClient("1.2.3.4", on_frame=processed.append)
    pc._loop = loop
    pc._stop = False

    class PB:
        def __init__(self, ip):
            pass

        def setSendPreviewFrames(self, on):
            pass

        def getPreviewFrame(self):
            reads[0] += 1
            t[0] += clock_step
            if reads[0] > n_frames:
                pc._stop = True
                return b""
            return bytes([reads[0]])

        def _close(self):
            pass

    monkeypatch.setattr(pixelblaze, "Pixelblaze", PB)
    pc._run_session()
    return reads[0], processed


def test_never_sleeps(monkeypatch):
    """Regression: sleeping between reads does NOT slow the PixelBlaze down —
    frames are pushed unsolicited, so a slow reader just queues them in the
    socket and getPreviewFrame() returns the OLDEST one. That backlog put the
    preview ~5s behind. The loop must never sleep."""
    def boom(_s):
        raise AssertionError("preview loop must not sleep; it would build a backlog")
    monkeypatch.setattr(prev.time, "sleep", boom)
    _session_with(monkeypatch, SlowLoop())


def test_drains_every_frame_even_when_rate_limited(monkeypatch):
    # Clock never advances -> everything past the first is over budget, but
    # every frame must still be READ so the socket never backs up.
    reads, processed = _session_with(monkeypatch, SlowLoop(), n_frames=10)
    assert reads == 11               # 10 frames + the sentinel that stops us
    assert len(processed) == 1       # the rest dropped, not queued


def test_rate_limit_lets_frames_through_over_time(monkeypatch):
    # Advance a full interval per read -> every frame is within budget.
    step = 1.0 / config.PB_PREVIEW_MAX_HZ
    reads, processed = _session_with(monkeypatch, SlowLoop(), n_frames=10,
                                     clock_step=step)
    assert len(processed) == 10


def test_processing_runs_on_the_session_thread_not_the_loop(monkeypatch):
    """Handing every frame to the event loop made the loop the bottleneck and
    starved input (sluggish knobs, and a hard freeze on a 680-pixel PB). The
    work must happen on the session thread; the loop must not be involved."""
    loop = SlowLoop()
    _, delivered = _session_with(monkeypatch, loop, n_frames=3)
    assert len(delivered) >= 1
    assert loop.pending == []        # nothing was ever scheduled on the loop


def test_handler_exception_does_not_kill_the_session(monkeypatch):
    # A failing frame handler must not tear down the preview stream.
    calls = []

    def boom(_f):
        calls.append(1)
        raise RuntimeError("handler blew up")

    monkeypatch.setattr(prev.time, "monotonic", lambda: 1000.0 + len(calls))
    pc = PreviewClient("1.2.3.4", on_frame=boom)
    pc._loop = SlowLoop()
    pc._stop = False
    reads = [0]

    class PB:
        def __init__(self, ip):
            pass

        def setSendPreviewFrames(self, on):
            pass

        def getPreviewFrame(self):
            reads[0] += 1
            if reads[0] > 3:
                pc._stop = True
                return b""
            return b"\x01"

        def _close(self):
            pass

    monkeypatch.setattr(pixelblaze, "Pixelblaze", PB)
    pc._run_session()               # must not raise
    assert len(calls) >= 1


def test_run_session_bails_if_enable_fails(monkeypatch, fake_loop):
    monkeypatch.setattr(prev.time, "sleep", lambda s: None)
    got = []
    pc = PreviewClient("1.2.3.4", on_frame=lambda f: got.append(f))
    pc._loop = fake_loop
    pc._stop = False

    class PB:
        def __init__(self, ip):
            self.closed = False

        def setSendPreviewFrames(self, on):
            raise RuntimeError("ws refused")

        def getPreviewFrame(self):
            raise AssertionError("should not be reached")

        def _close(self):
            self.closed = True

    monkeypatch.setattr(pixelblaze, "Pixelblaze", PB)
    pc._run_session()   # must return cleanly, no frames
    assert got == []
