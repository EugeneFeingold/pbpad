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
