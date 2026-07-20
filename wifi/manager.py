import asyncio
import os
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


async def known_ssids() -> set:
    # Try NetworkManager first, fall back to wpa_supplicant config
    proc = await asyncio.create_subprocess_exec(
        "nmcli", "-t", "-f", "NAME,TYPE", "connection", "show",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0:
        ssids = set()
        for line in stdout.decode().splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and "wireless" in parts[1]:
                ssids.add(parts[0])
        return ssids

    # wpa_supplicant fallback
    try:
        with open("/etc/wpa_supplicant/wpa_supplicant.conf") as f:
            return {
                line.split('"')[1]
                for line in f
                if line.strip().startswith("ssid=")
            }
    except OSError:
        return set()


async def connect(ssid: str, password: Optional[str] = None) -> bool:
    # Try NetworkManager first
    if password:
        cmd = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
    else:
        cmd = ["nmcli", "dev", "wifi", "connect", ssid]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode == 0:
        return True

    # wpa_supplicant fallback: append to config and reconfigure
    if password:
        wpa_entry = f'\nnetwork={{\n    ssid="{ssid}"\n    psk="{password}"\n}}\n'
        try:
            with open("/etc/wpa_supplicant/wpa_supplicant.conf", "a") as f:
                f.write(wpa_entry)
            reconfigure = await asyncio.create_subprocess_exec(
                "wpa_cli", "-i", "wlan0", "reconfigure",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await reconfigure.communicate()
            await asyncio.sleep(5)
            return await is_connected()
        except OSError:
            return False

    return False
