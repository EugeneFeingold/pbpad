from __future__ import annotations
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import log
from pb.discovery import PixelblazeDevice

DEBOUNCE_SEC = 0.25  # wait this long after the knob stops before sending


def _parse_config(pb) -> dict:
    """Call getConfigSettings once and extract everything from the result.

    getConfigSettings sends one {"getConfig": true} request and receives 3
    WebSocket messages. As a side effect it populates pb.latestSequencer.
    This avoids calling getConfigSettings 4+ times per poll.
    """
    settings = pb.getConfigSettings()
    seq = json.loads(pb.latestSequencer) if pb.latestSequencer else {}
    active = seq.get("activeProgram", {})
    return {
        "pattern_id": active.get("activeProgramId") or None,
        "controls": active.get("controls") or {},
        "brightness": settings.get("brightness"),
        "seq_running": bool(seq.get("runSequencer", False)),
        "seq_mode": seq.get("sequencerMode", 1),
        "fps": _latest_fps(pb),
    }


def _latest_fps(pb):
    """The PB's own render rate, from the stats packet it broadcasts each second.

    Read from the cached `latestStats` that wsReceive stores opportunistically
    — deliberately NOT via getFPS(), which calls getStatistics(), which calls
    setSendPreviewFrames(True). That would start streaming preview frames down
    the interactive socket and rebuild the backlog we removed."""
    raw = getattr(pb, "latestStats", None)
    if not raw:
        return None
    try:
        return json.loads(raw).get("fps")
    except Exception:
        return None


class PixelblazeClient:
    def __init__(self, device: PixelblazeDevice):
        self._device = device
        self._pb = None
        self._patterns: dict = {}
        self._active_pattern_id: Optional[str] = None
        self._brightness: float = 1.0
        self._sequencer_running: bool = False
        self._sequencer_shuffle: bool = False
        self._controls: dict = {}  # {name: float 0-1}
        self._playlist: list = []  # pattern IDs in playlist order
        self._pending_control: Optional[tuple] = None  # (name, value)
        self._last_control_sent_at: float = 0.0
        self._debounce_tasks: dict = {}  # channel -> pending send task
        self._pending_advance: int = 0   # accumulated playlist steps
        self._device_fps = None          # PB's own render rate (from its stats)
        # One dedicated thread so all WebSocket I/O (poll + user actions) runs
        # serially on a single socket — no concurrent-access corruption and no
        # pile-up on the shared pool that could starve the poll.
        self._executor = ThreadPoolExecutor(max_workers=1)

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        result = await loop.run_in_executor(self._executor, fn, *args)
        log.log(log.NETWORK, f"{getattr(fn, '__name__', fn)} {(time.monotonic() - t0) * 1000:.0f}ms")
        return result

    @staticmethod
    def _norm_controls(raw: dict) -> dict:
        result = {}
        for k, v in raw.items():
            if isinstance(v, (int, float)):
                result[k] = max(0.0, min(1.0, float(v)))
            elif isinstance(v, list) and len(v) >= 3:
                result[k] = [max(0.0, min(1.0, float(c))) for c in v[:3]]
        return result

    async def connect(self):
        from pixelblaze import Pixelblaze
        loop = asyncio.get_running_loop()
        self._pb = await loop.run_in_executor(self._executor, Pixelblaze, self._device.ip)

        def _init():
            patterns = self._pb.getPatternList() or {}
            cfg = _parse_config(self._pb)  # one getConfigSettings call
            return patterns, cfg

        patterns, cfg = await loop.run_in_executor(self._executor, _init)
        self._patterns = patterns
        self._active_pattern_id = cfg["pattern_id"]
        self._controls = self._norm_controls(cfg["controls"])
        brightness = cfg["brightness"]
        self._brightness = float(brightness) if brightness is not None else 1.0
        self._sequencer_running = cfg["seq_running"]
        self._sequencer_shuffle = cfg["seq_mode"] == 2
        self._device_fps = cfg["fps"]
        await self.load_playlist()

    async def load_playlist(self):
        try:
            pl = await self._run(self._pb.getSequencerPlaylist)
            items = pl.get("playlist", {}).get("items", [])
            self._playlist = [item["id"] for item in items if "id" in item]
        except Exception:
            self._playlist = []

    async def poll(self) -> bool:
        """Re-read live state with a single getConfigSettings call.

        Returns True on success, False if the read failed — e.g. the WebSocket
        to the PixelBlaze has dropped. Callers use repeated failures to detect
        a lost connection.
        """
        poll_start = time.monotonic()
        try:
            cfg = await self._run(_parse_config, self._pb)
        except Exception:
            return False

        # Atomic assignment — no awaits below this line
        if cfg["pattern_id"]:
            self._active_pattern_id = cfg["pattern_id"]
        if not self._pending_control and self._last_control_sent_at <= poll_start:
            self._controls = self._norm_controls(cfg["controls"])
        if cfg["brightness"] is not None:
            self._brightness = float(cfg["brightness"])
        if cfg["fps"] is not None:
            self._device_fps = cfg["fps"]
        self._sequencer_running = cfg["seq_running"]
        return True

    async def load_controls(self):
        try:
            cfg = await self._run(_parse_config, self._pb)
            self._controls = self._norm_controls(cfg["controls"])
        except Exception:
            self._controls = {}

    # --- debounce: local state updates immediately, the network send fires
    #     DEBOUNCE_SEC after the knob stops (only the final value is sent). ---
    def _debounce(self, key: str, factory):
        old = self._debounce_tasks.get(key)
        if old and not old.done():
            old.cancel()
        self._debounce_tasks[key] = asyncio.ensure_future(self._debounced_run(key, factory))

    async def _debounced_run(self, key: str, factory):
        try:
            await asyncio.sleep(DEBOUNCE_SEC)
        except asyncio.CancelledError:
            return  # superseded by a newer value; never sent
        try:
            await factory()
        except Exception as e:
            log.log(log.ERROR, f"send [{key}] failed: {e}")

    def _cancel_debounce(self, prefix: str = ""):
        for key in list(self._debounce_tasks):
            if key.startswith(prefix):
                task = self._debounce_tasks.pop(key)
                if task and not task.done():
                    task.cancel()

    def set_pattern_local(self, pattern_id: str):
        """Update local state immediately, no I/O."""
        self._active_pattern_id = pattern_id
        self._controls = {}
        self._pending_control = None
        self._cancel_debounce("control")  # controls belong to the old pattern

    def commit_pattern(self, pattern_id: str):
        """Debounced: switch the PixelBlaze to `pattern_id` once the knob stops."""
        self._debounce("pattern", lambda: self._do_commit_pattern(pattern_id))

    async def _do_commit_pattern(self, pattern_id: str):
        await self._run(self._pb.setActivePattern, pattern_id)
        await self.load_controls()

    def advance_playlist(self, steps: int):
        """Debounced: accumulate steps, advance the sequencer by the total."""
        self._pending_advance += steps
        self._debounce("pattern", self._do_advance_playlist)

    async def _do_advance_playlist(self):
        steps, self._pending_advance = self._pending_advance, 0
        for _ in range(max(1, steps)):
            await self._run(self._pb.nextSequencer)
        await self.load_controls()

    def set_control(self, name: str, value):
        if isinstance(value, list):
            value = [max(0.0, min(1.0, float(c))) for c in value]
        else:
            value = max(0.0, min(1.0, float(value)))
        self._controls[name] = value            # immediate local
        self._pending_control = (name, value)   # keep poll from clobbering it
        self._debounce("control:" + name, lambda: self._send_control(name, value))

    async def _send_control(self, name, value):
        self._pending_control = None
        self._last_control_sent_at = time.monotonic()
        await self._run(self._pb.setActiveControls, {name: value})
        if isinstance(value, list):
            log.log(log.CHANGE, f"color {name} = {[round(v * 255) for v in value]}")
        else:
            log.log(log.CHANGE, f"slider {name} = {value:.2f}")

    def set_brightness(self, value: float):
        value = max(0.0, min(1.0, value))
        self._brightness = value                # immediate local
        self._debounce("brightness", lambda: self._run(self._pb.setBrightnessSlider, value))

    def set_sequencer_running(self, running: bool):
        self._sequencer_running = running       # immediate local
        self._debounce("seq_run", lambda: self._do_seq_running(running))

    async def _do_seq_running(self, running: bool):
        if running:
            await self._run(self._pb.playSequencer)
            try:
                cfg = await self._run(_parse_config, self._pb)
                if cfg["pattern_id"]:
                    self._active_pattern_id = cfg["pattern_id"]
                self._controls = self._norm_controls(cfg["controls"])
            except Exception:
                self._controls = {}
        else:
            await self._run(self._pb.pauseSequencer)

    def set_sequencer_shuffle(self, shuffle: bool):
        self._sequencer_shuffle = shuffle       # immediate local
        if self._sequencer_running:
            self._debounce("seq_shuffle",
                           lambda: self._run(self._pb.setSequencerMode, 2 if shuffle else 1))

    @property
    def patterns(self) -> dict:
        return self._patterns

    @property
    def active_pattern_id(self) -> Optional[str]:
        return self._active_pattern_id

    @property
    def brightness(self) -> float:
        return self._brightness

    @property
    def sequencer_running(self) -> bool:
        return self._sequencer_running

    @property
    def sequencer_shuffle(self) -> bool:
        return self._sequencer_shuffle

    @property
    def controls(self) -> dict:
        return self._controls

    @property
    def playlist(self) -> list:
        return self._playlist

    def cancel_pending(self):
        """Cancel all pending debounced sends. Touches asyncio state, so it MUST
        be called on the event loop thread (not from an executor)."""
        self._cancel_debounce()
        self._pending_control = None

    def close_socket(self):
        """Blocking WebSocket/socket teardown. Safe to run in an executor thread;
        closing also unblocks any read stranded on a dead connection."""
        pb = self._pb
        self._pb = None
        if pb is not None:
            try:
                pb._close()
            except Exception:
                pass
        # Don't wait — a stuck I/O thread finishes once the socket is closed.
        self._executor.shutdown(wait=False)

    @property
    def device_name(self) -> str:
        return self._device.name

    @property
    def ip(self) -> str:
        return self._device.ip

    @property
    def device_fps(self):
        """The PixelBlaze's own pattern render rate, or None if it hasn't
        reported one yet. Refreshed on each poll from its stats packet."""
        return self._device_fps
