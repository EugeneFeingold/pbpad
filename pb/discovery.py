from __future__ import annotations
import asyncio
import socket
import struct
import time
from dataclasses import dataclass

from conf import config
from pb import pools
import log

_BEACON_MAGIC = 42        # first word of a PixelBlaze beacon packet
_BEACON_STRUCT = "<LLL"   # magic, id, uptime — 12 bytes
_WS_TIMEOUT = 5.0         # bound the name/probe socket reads


@dataclass
class PixelblazeDevice:
    ip: str
    name: str
    device_id: int


def _listen_for_beacons(timeout_sec: float) -> set:
    """Listen for PixelBlaze UDP beacons and return the set of source IPs.

    We do our own listening instead of using pixelblaze-client's
    Pixelblaze.EnumerateDevices() because that class dedupes against
    `LightweightEnumerator.seenPixelblazes`, a CLASS-level list that is never
    cleared. It therefore reports each device only once per *process*: the
    first scan finds the PB, and every scan after that returns nothing until
    the program is restarted. That was the "can't rediscover until I restart
    the software" bug — confirmed from a log showing repeated `found 0`
    followed by an immediate `found 1` on a fresh process.

    This socket is created and closed per call, so there is no shared state
    and nothing to go stale as the device moves between networks.
    """
    ips = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)   # short, so we re-check the deadline promptly
        sock.bind(("0.0.0.0", config.PIXELBLAZE_DISCOVERY_PORT))
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) >= 12:
                try:
                    magic, _id, _uptime = struct.unpack(_BEACON_STRUCT, data[:12])
                except struct.error:
                    continue
                if magic == _BEACON_MAGIC:
                    ips.add(addr[0])
    finally:
        sock.close()
    return ips


def _device_name(ip: str) -> str | None:
    """Ask the PB at `ip` for its name. Returns None if it can't be reached."""
    from pixelblaze import Pixelblaze
    try:
        pb = Pixelblaze(ip)
    except Exception:
        return None
    try:
        pb.ws.settimeout(_WS_TIMEOUT)
    except Exception:
        pass
    try:
        return pb.getDeviceName() or None
    except Exception:
        return None
    finally:
        try:
            pb._close()
        except Exception:
            pass


def _close_stale_discovery_sockets():
    """Close any UDP socket owned by this process that's bound to the PB
    discovery port. Guards against a pixelblaze-client leak: EnumerateDevices
    opens a UDP socket for the duration of its generator and relies on GC to
    close it. If GC is delayed (dangling reference, interrupted iteration),
    the socket stays bound and slowly fills its receive buffer with broadcasts
    — once full, the kernel drops packets, and subsequent discover() calls
    quietly return no devices even though everything else is fine.

    Diagnosed via `ss -uap sport = :1889` showing Recv-Q at ~100KB.

    Linux-only (walks /proc); no-op on other platforms.
    """
    import os
    try:
        with open("/proc/net/udp") as f:
            next(f)   # skip header
            inodes = set()
            for line in f:
                parts = line.split()
                try:
                    _, port_hex = parts[1].split(":")
                    if int(port_hex, 16) == 1889:
                        inodes.add(int(parts[9]))
                except (ValueError, IndexError):
                    continue
    except OSError:
        return
    if not inodes:
        return
    import log
    for fd_name in os.listdir("/proc/self/fd"):
        try:
            fd = int(fd_name)
            st = os.stat(f"/proc/self/fd/{fd_name}")
        except (OSError, ValueError):
            continue
        if st.st_ino in inodes:
            try:
                os.close(fd)
                log.log(log.CHANGE, f"closed stale UDP :1889 socket (fd={fd})")
            except OSError:
                pass


async def discover(timeout_sec: float = 5) -> list:
    """Find PixelBlazes by listening for their UDP beacons.

    Uses our own listener (_listen_for_beacons) rather than pixelblaze-client's
    EnumerateDevices, which only reports each device once per process — see
    _listen_for_beacons for the details. Every call binds and closes a fresh
    socket, so repeated scans keep working for the life of the program.

    Logs the outcome at CHANGE level with count and elapsed time.
    """
    loop = asyncio.get_running_loop()

    def _run():
        log.log(log.CHANGE, f"discover: starting (timeout={timeout_sec}s)")
        # Safety net: close anything else of ours still holding the port.
        _close_stale_discovery_sockets()
        t0 = time.monotonic()
        try:
            ips = _listen_for_beacons(timeout_sec)
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.log(log.ERROR, f"discover: listen failed after {elapsed_ms}ms: {e!r}")
            return []
        devices = []
        for ip in sorted(ips):
            devices.append(PixelblazeDevice(
                ip=ip, name=_device_name(ip) or ip, device_id=hash(ip),
            ))
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        names = ", ".join(d.name for d in devices) or "<none>"
        log.log(log.CHANGE, f"discover: found {len(devices)} in {elapsed_ms}ms ({names})")
        return devices

    return await loop.run_in_executor(pools.DISCOVERY, _run)


async def probe(ip: str) -> PixelblazeDevice:
    """Verify a PixelBlaze at a known IP and return its device info."""
    loop = asyncio.get_running_loop()

    def _probe():
        from pixelblaze import Pixelblaze
        pb = Pixelblaze(ip)
        try:
            pb.ws.settimeout(_WS_TIMEOUT)
        except Exception:
            pass
        try:
            name = pb.getDeviceName() or ip
        except Exception:
            name = ip
        pb._close()
        return name

    name = await loop.run_in_executor(pools.DISCOVERY, _probe)
    return PixelblazeDevice(ip=ip, name=name, device_id=hash(ip))
