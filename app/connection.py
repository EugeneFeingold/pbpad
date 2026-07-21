"""The connection lifecycle: WiFi -> discover -> connect -> poll -> recover.

A single background task (`_connection_manager`) owns this whole flow for both
the initial connection and recovery after a drop, so the behavior is identical
either way and the input loop never blocks on network I/O.
"""
import asyncio
import threading
import time

import log
from pb.discovery import (
    discover,
    probe,
    _close_stale_discovery_sockets,
    PixelblazeDevice,
)
from pb.client import PixelblazeClient
from pb import preferred
from pb import pools
from wifi import manager as wifi_manager
from ui.screens import (
    DeviceSelectScreen,
    DiscoveringScreen,
    ReconnectScreen,
    MainScreen,
    StatusScreen,
    IPEntryScreen,
)

from app.util import _fd_count

POLL_INTERVAL = 5
POLL_TIMEOUT = 4         # a poll that doesn't answer in this long = dead connection
POLL_FAIL_LIMIT = 3      # consecutive failed polls before the PB is treated as lost
RECONNECT_INTERVAL = 3   # seconds between reconnect attempts
CONNECT_TIMEOUT = 25     # backstop for a wedged reconnect attempt. Must exceed a
                         # FULL connect (WS handshake + getPatternList +
                         # getConfigSettings + getSequencerPlaylist) over a
                         # degraded post-drop link — that routinely takes >8s, and
                         # an 8s cap would cancel connects that would have
                         # succeeded, leaving the reconnect screen spinning while
                         # a manual discovery (which has no cap) connects fine.
                         # This is only a wedge backstop, NOT the Cancel path:
                         # Cancel is handled by the input task tearing the client
                         # down (closing the socket unblocks the connect at once).
DIRECT_ATTEMPTS = 3      # try the last-known IP this many times before falling
                         # back to discovery (which catches a DHCP IP change)
RECOVERY_RESTART_SEC = 300  # if recovery gets nowhere this long, restart the
                            # process (systemd Restart=always brings us back).
                            # Last-resort self-heal for an unattended device.


class ConnectionMixin:
    def _wake_manager(self):
        self._wake.set()

    async def _wait_wake(self, timeout: float):
        """Sleep up to `timeout`, but return early if the manager is nudged."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    def _teardown_client(self):
        """Tear down the PB client + preview client. Returns the executor
        future for the (blocking) socket close so callers that need the socket
        actually gone — e.g. a hard reset before reconnecting — can await it;
        callers that don't care can ignore it (fire-and-forget)."""
        client = self._pb_client
        self._pb_client = None
        fut = None
        if client is not None:
            # Cancel the pending control task here (asyncio state, must be on the
            # loop), then offload only the blocking socket close — running that
            # inline would freeze the UI on a dead PB. Closing also unblocks any
            # read stranded on the dead connection.
            client.cancel_pending()
            # Dedicated teardown pool — never the shared default one. A close
            # that blocks on a dead socket must only ever starve other closes,
            # never discovery or the next connection.
            fut = self._loop.run_in_executor(pools.TEARDOWN, client.close_socket)
        self._stop_preview_client()
        return fut

    async def _hard_reset(self):
        """Destroy every connection object and its OS resources — the in-process
        equivalent of a program restart. Awaits the PB socket close (so we don't
        pile up connections on the PixelBlaze, which has limited slots), sweeps
        leaked discovery sockets, and logs fd/thread counts so a resource leak
        is visible in the journal."""
        fut = self._teardown_client()
        if fut is not None:
            try:
                await asyncio.wait_for(fut, 5)
            except Exception:
                pass  # a stuck close must not hang recovery
        _close_stale_discovery_sockets()
        log.log(log.CHANGE,
                f"hard reset: fds={_fd_count()} threads={threading.active_count()}")

    def _status(self, line1: str, line2: str):
        """Show a manager status message. Interactive: a left push opens Settings
        (so the user can change WiFi/device while stuck searching).

        Idempotent: repeat calls with the same text are no-ops. This matters
        because the connection manager loops rapidly through _establish and
        would otherwise churn self._screen — creating a race where a queued
        Enter is dispatched to a fresh StatusScreen instance that never got
        rendered, or gets clobbered mid-dispatch."""
        if isinstance(self._screen, StatusScreen) and self._screen.matches(line1, line2):
            return
        self._screen = StatusScreen(line1, line2)
        self._screen.render(self._lcd)

    async def _connection_manager(self):
        """Single owner of the WiFi -> discover -> connect -> poll lifecycle.

        Used for both initial connection and recovery, so the behavior is
        identical either way. Runs in the background so the input loop never
        blocks. Pauses while asleep/locked (to save power) or while the user is
        in a menu (so it never yanks them out).
        """
        fails = 0
        while True:
            try:
                if self._sleeping or self._locked or self._in_menu or self._low_battery:
                    await self._wait_wake(1.0)
                    continue

                if self._pb_client is not None:
                    try:
                        ok = await asyncio.wait_for(self._pb_client.poll(), POLL_TIMEOUT)
                    except asyncio.TimeoutError:
                        # A slow poll (e.g. contention while the user hammers
                        # controls) is only a *soft* failure — count it, don't
                        # instantly declare the connection lost.
                        ok = False
                    if ok:
                        fails = 0
                        await self._wait_wake(POLL_INTERVAL)
                    else:
                        fails += 1
                        log.log(log.ERROR, f"poll failed ({fails}/{POLL_FAIL_LIMIT})")
                        if fails >= POLL_FAIL_LIMIT:
                            log.log(log.CHANGE, "connection lost")
                            # Reconnect to the SAME device by IP, not whatever
                            # discovery turns up (which may find nothing, or a
                            # different PB). _establish sees the target and
                            # routes to the direct-IP reconnect flow.
                            self._reconnect_target = self._connected_device
                            self._reconnect_attempts = 0
                            self._recovery_started_at = time.monotonic()
                            self._teardown_client()
                            fails = 0
                        else:
                            await self._wait_wake(1.0)  # quick retry after a soft failure
                    continue

                fails = 0
                await self._establish()
                if self._pb_client is None:
                    await self._wait_wake(RECONNECT_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception:
                import traceback
                log.log(log.ERROR, "connection manager error")
                traceback.print_exc()
                self._teardown_client()
                fails = 0
                await asyncio.sleep(RECONNECT_INTERVAL)

    async def _establish(self):
        """One attempt at getting connected. Sets _pb_client + MainScreen on success."""
        # While the device picker is up, wait for the user's choice (below).
        if isinstance(self._screen, DeviceSelectScreen) and self._selected_device is None:
            return
        if not await wifi_manager.is_connected():
            self._status("No WiFi", "connecting...")
            return
        self._ssid = await wifi_manager.current_ssid() or ""
        # A device the user chose explicitly (picker / IP entry) takes priority
        # and overrides any pending reconnect.
        if self._selected_device is not None:
            dev, self._selected_device = self._selected_device, None
            self._reconnect_target = None
            if await self._connect(dev):
                preferred.remember(dev.name)
            return
        # Recovery: reconnect straight to the device we lost, by IP. This skips
        # discovery entirely (a fresh WebSocket to the known IP), which is the
        # same reset a program restart performs — and it works even when UDP
        # discovery has stopped seeing the PB.
        if self._reconnect_target is not None:
            await self._recover_connection()
            return
        self._status("Finding", "PixelBlaze...")
        devices = await discover()
        if self._in_menu:
            return  # user opened Settings during the search; don't stomp it
        if not devices:
            return  # keep "Finding" up; manager loops and tries again
        # Auto-connect to the highest-ranked known PB (unless the user asked to
        # pick a different one from Settings).
        if not self._force_picker:
            dev = preferred.pick(devices)
            if dev is not None:
                if await self._connect(dev):
                    preferred.remember(dev.name)
                return
        # Let the user pick (their choice becomes the most-recent preferred).
        self._force_picker = False
        log.log(log.CHANGE, f"device picker: {', '.join(d.name for d in devices)}")
        self._screen = DeviceSelectScreen(devices, on_select=self._on_device_select)
        self._screen.render(self._lcd)

    async def _connect(self, device) -> bool:
        self._teardown_client()
        self._pb_client = PixelblazeClient(device)
        try:
            await self._run_with_spinner("Connecting to", device.name, self._pb_client.connect())
        except Exception as e:
            import traceback
            log.log(log.ERROR, f"PB connect failed: {e}")
            traceback.print_exc()
            self._teardown_client()
            self._status("PB connect", "failed")
            await asyncio.sleep(2)
            return False
        log.log(log.CHANGE, f"Connected to {device.name} ({device.ip})")
        self._connected_device = device      # reconnect target if we drop
        self._lcd.render_message("Connected!", device.name[:16])
        await asyncio.sleep(1.5)
        self._screen = MainScreen(self._pb_client, self._lcd)
        self._screen.render(self._lcd)
        # Second WebSocket for preview frames -> local WS2812 strip.
        self._start_preview_client(device.ip)
        return True

    async def _recover_connection(self):
        """One recovery attempt for the device we lost (self._reconnect_target).

        Every attempt starts with a hard reset (destroy + recreate all
        connection objects, close their sockets) — the in-process equivalent of
        a restart, since a stale/leaked socket is the suspected reason a plain
        reconnect fails until the program is restarted.

        Escalates: the first DIRECT_ATTEMPTS go straight to the last-known IP
        (fast, no discovery); after that it falls back to discovery to catch a
        DHCP IP change, but still only reconnects to the SAME device by name —
        never auto-jumping to a different PB. Right knob cancels to manual search.
        The interactive ReconnectScreen stays up across attempts."""
        target = self._reconnect_target
        if target is None:
            return
        if not isinstance(self._screen, ReconnectScreen):
            self._screen = ReconnectScreen(target.name)
            self._screen.render(self._lcd)

        # Last-resort self-heal: if we've been stuck in recovery this long, the
        # failure is something we don't understand, so restart the process —
        # systemd (Restart=always) brings us straight back with a clean slate.
        # This device runs unattended outdoors; it must not stay wedged.
        if (self._recovery_started_at
                and time.monotonic() - self._recovery_started_at > RECOVERY_RESTART_SEC):
            log.log(log.ERROR,
                    f"recovery stuck for {RECOVERY_RESTART_SEC}s — restarting software")
            self._restart_software()
            return

        self._reconnect_attempts += 1
        await self._hard_reset()

        if self._reconnect_attempts <= DIRECT_ATTEMPTS:
            dev = target                       # direct to the known IP
        else:
            # Discovery fallback: find the same device (possibly at a new IP).
            devices = await discover()
            if self._reconnect_target is not target:
                return                          # cancelled during discovery
            dev = next((d for d in devices if d.name == target.name), None)
            if dev is None:
                return  # not found this round; keep retrying (or user cancels)

        client = PixelblazeClient(dev)
        self._pb_client = client
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
        except Exception as e:
            log.log(log.ERROR, f"reconnect to {target.name} failed "
                               f"(attempt {self._reconnect_attempts}): {e}")
            self._teardown_client()
            return  # keep the target; the manager retries on its next loop

        # Cancelled (or superseded) while the attempt was in flight?
        if self._reconnect_target is not target:
            self._teardown_client()
            return

        self._reconnect_target = None
        self._reconnect_attempts = 0
        self._recovery_started_at = 0.0
        self._connected_device = dev
        preferred.remember(dev.name)
        log.log(log.CHANGE, f"reconnected to {dev.name} ({dev.ip})")
        self._screen = MainScreen(self._pb_client, self._lcd)
        self._screen.render(self._lcd)
        self._start_preview_client(dev.ip)
        return True

    def _on_device_select(self, device):
        # User picked a PB from the list: it becomes the most-recent preferred.
        # Tear down any current connection so the manager connects to this one.
        self._selected_device = device
        self._in_menu = False
        self._teardown_client()
        self._wake_manager()

    async def _open_device_picker(self):
        """Show the device picker from Settings without dropping the current
        connection; only picking a different PB reconnects (see _on_device_select).

        Uses a cancellable DiscoveringScreen (right knob = Back) instead of the
        old blocking spinner, and always transitions to the list — even an
        empty list is useful because it still exposes "Scan Again" and
        "Connect by IP" as ways forward."""
        task = asyncio.ensure_future(discover())
        # "Scan Again" comes from an existing DeviceSelectScreen; treat that
        # as a refresh (no stack push) rather than a new navigation step.
        # Regular entry from Settings does push, so Back returns to Settings.
        if isinstance(self._screen, DeviceSelectScreen):
            self._replace_screen(DiscoveringScreen(on_cancel=task.cancel))
        else:
            self._push_screen(DiscoveringScreen(on_cancel=task.cancel))
        try:
            devices = await task
        except asyncio.CancelledError:
            return  # user pressed Back; the transition already ran
        # If the user navigated away while discover was running (e.g. tapped
        # Back at the exact moment it returned), don't clobber the new screen.
        if not isinstance(self._screen, DiscoveringScreen):
            return
        # Make sure the currently connected PB shows in the list even if
        # discovery missed it, and mark it so the user sees which is active.
        current = self._pb_client
        current_name = current.device_name if current else None
        if current is not None and not any(d.ip == current.ip for d in devices):
            devices = [PixelblazeDevice(
                ip=current.ip, name=current.device_name, device_id=hash(current.ip),
            )] + list(devices)
        self._replace_screen(DeviceSelectScreen(
            devices, on_select=self._on_device_select, current_name=current_name,
        ))

    def _on_ip_submit(self, ip: str):
        asyncio.ensure_future(self._connect_by_ip(ip))

    async def _connect_by_ip(self, ip: str):
        log.log(log.CHANGE, f"connecting by IP: {ip}")
        try:
            device = await self._run_with_spinner("Connecting to", ip, probe(ip))
        except Exception as e:
            log.log(log.ERROR, f"probe {ip} failed: {e}")
            self._lcd.render_message("Not found at", ip[:16])
            await asyncio.sleep(2)
            self._screen = IPEntryScreen(
                on_submit=self._on_ip_submit,
                on_cancel=lambda: None,
            )
            self._screen.render(self._lcd)
            return
        self._selected_device = device
        self._in_menu = False
        self._wake_manager()  # manager connects to the chosen device
