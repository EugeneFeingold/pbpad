import asyncio
from typing import Optional


async def current_ssid() -> Optional[str]:
    proc = await asyncio.create_subprocess_exec(
        "iwgetid", "-r", "wlan0",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    ssid = stdout.decode().strip()
    return ssid if ssid else None


async def is_connected() -> bool:
    return await current_ssid() is not None


def signal_dbm() -> Optional[int]:
    """Current WiFi RSSI in dBm from /proc/net/wireless (wlan0), or None."""
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if not line.lstrip().startswith("wlan0"):
                    continue
                parts = line.split()
                # parts: iface: status  link. level. noise. ... — level is dBm
                return int(float(parts[3].rstrip(".")))
    except (OSError, ValueError, IndexError):
        pass
    return None


async def reset() -> bool:
    """Bounce the WiFi interface: disconnect, then reconnect via NetworkManager
    (which reassociates using the highest-priority autoconnect profile).

    For a device that moves around outdoors, the Pi's own WiFi association is a
    common failure point — it can stay nominally "connected" to a dead AP. This
    gives the user a way to force a fresh association without a reboot.
    Returns True if we're connected afterwards.
    """
    for args in (["nmcli", "device", "disconnect", "wlan0"],
                 ["nmcli", "device", "connect", "wlan0"]):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except (FileNotFoundError, OSError):
            return False
        await asyncio.sleep(1)
    # Give the association + DHCP a moment to settle before reporting.
    for _ in range(10):
        if await is_connected():
            return True
        await asyncio.sleep(1)
    return False


async def reconnect(ssid: str) -> bool:
    """Drop the WiFi link and bring it back up on `ssid` specifically.

    Unlike reset() (which bounces the interface and lets NM pick whatever's
    highest priority), this targets one network: it disconnects, then joins
    `ssid` by name. Used when the user re-selects the network they're already on
    to force a clean re-association. Returns True if connected afterwards.
    """
    for args in (["nmcli", "device", "disconnect", "wlan0"],
                 ["nmcli", "dev", "wifi", "connect", ssid]):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except (FileNotFoundError, OSError):
            return False
        await asyncio.sleep(1)
    for _ in range(10):
        if await is_connected():
            return True
        await asyncio.sleep(1)
    return False


async def known_ssids() -> set:
    """SSIDs we have a saved NetworkManager profile for."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "NAME,TYPE", "connection", "show",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return set()
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return set()
    ssids = set()
    for line in stdout.decode().splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and "wireless" in parts[1]:
            ssids.add(parts[0])
    return ssids


async def prefer(ssid: str) -> None:
    """Make `ssid` NetworkManager's top autoconnect choice.

    Mirrors the preferred-PixelBlaze behavior for WiFi: when the user explicitly
    picks a network, it should stay picked. Without this, if two known networks
    are in range NM can reconnect (after a reboot / WiFi reset / signal drop) to
    whichever it likes, so the device bounced between networks the user never
    chose. Bumping the chosen profile's autoconnect-priority above the others
    pins the user's choice. Best-effort — ignores errors (e.g. no nmcli)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "connection", "modify", ssid,
            "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "999",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    except (FileNotFoundError, OSError):
        pass


async def connect(ssid: str, password: Optional[str] = None) -> bool:
    """Connect via NetworkManager. With no password NM uses the saved profile
    (or joins an open network); with one it creates/updates the profile.

    NM-only on purpose. The old wpa_supplicant fallback appended to
    wpa_supplicant.conf and then reported success from is_connected() — which,
    still being on the previous network, returned True even though nothing new
    connected. That false success is what made the UI jump to "Connected!" and
    reconnect the PixelBlaze after a connect that actually failed.
    """
    if password:
        cmd = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
    else:
        cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return False
    await proc.communicate()
    return proc.returncode == 0
