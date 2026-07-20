"""Small stateless helpers shared across the App mixins.

Kept dependency-free (no imports from the app package) so any mixin can pull
these in without risking an import cycle.
"""
import os
import socket
from typing import Optional


def _fd_count() -> int:
    """Open file-descriptor count for this process (Linux), or -1 elsewhere.
    A steady climb across reconnects is the fingerprint of a socket/thread
    leak — the suspected cause of 'can't reconnect until I restart'."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _local_ip() -> Optional[str]:
    """Return the Pi's outbound IP (the interface used to reach the LAN)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _subnet_prefix() -> str:
    """Return the Pi's IP with the last octet stripped, e.g. '10.0.0.'"""
    ip = _local_ip()
    return ".".join(ip.split(".")[:3]) + "." if ip else ""


def _fmt_uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
    except OSError:
        return "?"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"
