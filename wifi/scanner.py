import asyncio
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class WifiNetwork:
    ssid: str
    signal: int   # dBm
    secured: bool

    def __str__(self):
        lock = "*" if self.secured else " "
        return f"{lock}{self.ssid}"


async def scan() -> list[WifiNetwork]:
    networks = await _scan_nmcli()
    if networks is not None:
        return networks
    return await _scan_iwlist()


async def _scan_nmcli() -> Optional[list]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
            "dev", "wifi", "list", "--rescan", "yes",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
    except FileNotFoundError:
        return None

    networks: list[WifiNetwork] = []
    seen: set[str] = set()
    for line in stdout.decode().splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        ssid, signal_str, security = parts[0], parts[1], ":".join(parts[2:])
        if not ssid or ssid in seen:
            continue
        try:
            signal = int(signal_str)
        except ValueError:
            signal = 0
        seen.add(ssid)
        networks.append(WifiNetwork(ssid=ssid, signal=signal, secured=bool(security.strip())))

    networks.sort(key=lambda n: n.signal, reverse=True)
    return networks


async def _scan_iwlist() -> list[WifiNetwork]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "iwlist", "wlan0", "scan",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
    except FileNotFoundError:
        return []

    networks: list[WifiNetwork] = []
    seen: set[str] = set()
    current_ssid = None
    current_signal = 0
    current_secured = False

    for line in stdout.decode().splitlines():
        line = line.strip()
        if line.startswith("Cell "):
            if current_ssid and current_ssid not in seen:
                seen.add(current_ssid)
                networks.append(WifiNetwork(
                    ssid=current_ssid,
                    signal=current_signal,
                    secured=current_secured,
                ))
            current_ssid = None
            current_signal = 0
            current_secured = False
        m = re.search(r'ESSID:"([^"]*)"', line)
        if m:
            current_ssid = m.group(1) or None
        m = re.search(r'Signal level=(-?\d+)\s*dBm', line)
        if m:
            current_signal = int(m.group(1))
        if "Encryption key:on" in line:
            current_secured = True

    if current_ssid and current_ssid not in seen:
        networks.append(WifiNetwork(
            ssid=current_ssid,
            signal=current_signal,
            secured=current_secured,
        ))

    networks.sort(key=lambda n: n.signal, reverse=True)
    return networks
