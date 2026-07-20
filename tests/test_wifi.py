"""Tests for wifi/scanner.py and wifi/manager.py — output parsing (subprocess
and /proc are faked)."""
import asyncio

import pytest

from wifi import scanner
from wifi import manager
from wifi.scanner import WifiNetwork


class FakeProc:
    def __init__(self, stdout=b"", returncode=0):
        self._out = stdout
        self.returncode = returncode

    async def communicate(self):
        return (self._out, b"")


def fake_exec(stdout, rc=0):
    async def _exec(*args, **kwargs):
        return FakeProc(stdout, rc)
    return _exec


# --- WifiNetwork -----------------------------------------------------------
def test_wifinetwork_str_secured():
    assert str(WifiNetwork("Net", -50, True)) == "*Net"


def test_wifinetwork_str_open():
    assert str(WifiNetwork("Open", -50, False)) == " Open"


# --- scanner._scan_nmcli ---------------------------------------------------
async def test_scan_nmcli_parses_and_sorts(monkeypatch):
    out = b"HomeNet:80:WPA2\nWeakNet:30:WPA2\nOpenNet:60:\n"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(out))
    nets = await scanner._scan_nmcli()
    assert [n.ssid for n in nets] == ["HomeNet", "OpenNet", "WeakNet"]  # by signal desc
    assert nets[0].secured is True
    assert nets[1].secured is False   # empty security field


async def test_scan_nmcli_dedupes(monkeypatch):
    out = b"Net:80:WPA2\nNet:70:WPA2\n"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(out))
    nets = await scanner._scan_nmcli()
    assert len(nets) == 1


async def test_scan_nmcli_returns_none_on_error(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(b"", rc=1))
    assert await scanner._scan_nmcli() is None


async def test_scan_nmcli_skips_blank_ssid(monkeypatch):
    out = b":80:WPA2\nReal:70:WPA2\n"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(out))
    nets = await scanner._scan_nmcli()
    assert [n.ssid for n in nets] == ["Real"]


async def test_scan_nmcli_falls_back_to_cached_when_rescan_refused(monkeypatch):
    # NM rate-limits rescans; "--rescan yes" can be refused (non-zero). We must
    # retry with "--rescan no" (cached) rather than dropping to iwlist and
    # losing networks — that was why a just-seen network disappeared.
    seq = iter([FakeProc(b"", returncode=1),               # rescan yes -> refused
                FakeProc(b"Cached:70:WPA2\n", returncode=0)])  # rescan no  -> cached
    seen_args = []

    async def _exec(*args, **kwargs):
        seen_args.append(list(args))
        return next(seq)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    nets = await scanner._scan_nmcli()
    assert [n.ssid for n in nets] == ["Cached"]
    assert seen_args[0][-1] == "yes"   # first forces a fresh scan
    assert seen_args[1][-1] == "no"    # then falls back to the cached list


# --- manager.prefer --------------------------------------------------------
async def test_prefer_bumps_autoconnect_priority(monkeypatch):
    calls = []

    async def _exec(*args, **kwargs):
        calls.append(list(args))
        return FakeProc(b"", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    await manager.prefer("HomeNet")
    assert calls[0][:4] == ["nmcli", "connection", "modify", "HomeNet"]
    assert "connection.autoconnect-priority" in calls[0]
    assert "999" in calls[0]


async def test_prefer_survives_missing_nmcli(monkeypatch):
    async def _exec(*a, **k):
        raise FileNotFoundError("no nmcli")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    await manager.prefer("X")   # best-effort: must not raise


# --- manager.connect (NM-only) ---------------------------------------------
async def test_connect_with_password(monkeypatch):
    calls = []

    async def _exec(*args, **kwargs):
        calls.append(list(args))
        return FakeProc(b"", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    assert await manager.connect("Net", "pw") is True
    assert calls[0] == ["nmcli", "dev", "wifi", "connect", "Net", "password", "pw"]


async def test_connect_without_password_uses_saved_profile(monkeypatch):
    calls = []

    async def _exec(*args, **kwargs):
        calls.append(list(args))
        return FakeProc(b"", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    assert await manager.connect("Net") is True
    assert calls[0] == ["nmcli", "dev", "wifi", "connect", "Net"]   # no password arg


async def test_connect_failure_does_not_touch_wpa_supplicant(monkeypatch):
    # Post-migration we're NM-only: a failed connect must report False, NOT fall
    # back to editing wpa_supplicant.conf (which then falsely read as success).
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(b"", rc=1))

    def no_open(*a, **k):
        raise AssertionError("connect must not open wpa_supplicant.conf")

    monkeypatch.setattr("builtins.open", no_open)
    assert await manager.connect("Net", "pw") is False


# --- manager.known_ssids ---------------------------------------------------
async def test_known_ssids_parses_wireless_only(monkeypatch):
    out = b"HomeWifi:802-11-wireless\nEthernet:802-3-ethernet\nCafe:802-11-wireless\n"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(out))
    ssids = await manager.known_ssids()
    assert ssids == {"HomeWifi", "Cafe"}


# --- manager.current_ssid / is_connected -----------------------------------
async def test_current_ssid(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(b"MyNetwork\n"))
    assert await manager.current_ssid() == "MyNetwork"


async def test_current_ssid_empty_is_none(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(b"\n"))
    assert await manager.current_ssid() is None


async def test_is_connected(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(b"Net\n"))
    assert await manager.is_connected() is True


# --- manager.reset ---------------------------------------------------------
async def test_reset_bounces_interface(monkeypatch):
    calls = []

    async def _exec(*args, **kwargs):
        calls.append(list(args))
        return FakeProc(b"MyNet\n", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    assert await manager.reset() is True
    # disconnect then connect on wlan0
    assert calls[0][:4] == ["nmcli", "device", "disconnect", "wlan0"]
    assert calls[1][:4] == ["nmcli", "device", "connect", "wlan0"]


async def test_reconnect_drops_then_joins_specific_ssid(monkeypatch):
    calls = []

    async def _exec(*args, **kwargs):
        calls.append(list(args))
        return FakeProc(b"MyNet\n", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    assert await manager.reconnect("MyNet") is True
    assert calls[0][:4] == ["nmcli", "device", "disconnect", "wlan0"]   # drop
    assert calls[1] == ["nmcli", "dev", "wifi", "connect", "MyNet"]     # rejoin


async def test_reset_returns_false_without_nmcli(monkeypatch):
    async def _exec(*args, **kwargs):
        raise FileNotFoundError("no nmcli")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    assert await manager.reset() is False


# --- manager.signal_dbm ----------------------------------------------------
def test_signal_dbm_parses(monkeypatch, tmp_path):
    proc = tmp_path / "wireless"
    proc.write_text(
        "Inter-| sta-|   Quality        |   Discarded packets\n"
        " face | tus | link level noise |  nwid  crypt   frag\n"
        " wlan0: 0000   70.  -55.  -256        0      0      0\n"
    )
    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: real_open(proc, *a, **k))
    assert manager.signal_dbm() == -55


def test_signal_dbm_none_when_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr("builtins.open", boom)
    assert manager.signal_dbm() is None
