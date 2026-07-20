"""Tests for pb/discovery.py — UDP beacon listening, naming, and probing."""
import socket
import struct

import pytest

import pb.discovery as disc
from pb.discovery import (
    PixelblazeDevice, discover, probe, _listen_for_beacons,
    _close_stale_discovery_sockets,
)

BEACON = struct.pack("<LLL", 42, 1234, 5678)
NOT_BEACON = struct.pack("<LLL", 7, 0, 0)


class FakeSock:
    """Yields queued (data, addr) tuples, then raises socket.timeout forever."""

    def __init__(self, packets):
        self.packets = list(packets)
        self.closed = False
        self.bound = None
        self.opts = []

    def setsockopt(self, *a):
        self.opts.append(a)

    def settimeout(self, t):
        self.timeout = t

    def bind(self, addr):
        self.bound = addr

    def recvfrom(self, n):
        if self.packets:
            return self.packets.pop(0)
        raise socket.timeout()

    def close(self):
        self.closed = True


def patch_sock(monkeypatch, packets):
    fake = FakeSock(packets)
    monkeypatch.setattr(disc.socket, "socket", lambda *a, **k: fake)
    return fake


# --- _listen_for_beacons ---------------------------------------------------
def test_listen_collects_beacon_ips(monkeypatch):
    fake = patch_sock(monkeypatch, [
        (BEACON, ("10.0.0.5", 1889)),
        (BEACON, ("10.0.0.6", 1889)),
    ])
    ips = _listen_for_beacons(0.2)
    assert ips == {"10.0.0.5", "10.0.0.6"}
    assert fake.closed                    # socket always released
    assert fake.bound == ("0.0.0.0", 1889)


def test_listen_dedupes_same_ip(monkeypatch):
    patch_sock(monkeypatch, [
        (BEACON, ("10.0.0.5", 1889)),
        (BEACON, ("10.0.0.5", 1889)),
    ])
    assert _listen_for_beacons(0.2) == {"10.0.0.5"}


def test_listen_ignores_non_beacon(monkeypatch):
    patch_sock(monkeypatch, [(NOT_BEACON, ("10.0.0.9", 1889))])
    assert _listen_for_beacons(0.2) == set()


def test_listen_ignores_short_packet(monkeypatch):
    patch_sock(monkeypatch, [(b"\x01\x02", ("10.0.0.9", 1889))])
    assert _listen_for_beacons(0.2) == set()


def test_listen_closes_socket_on_error(monkeypatch):
    fake = FakeSock([])

    def boom(addr):
        raise OSError("address in use")
    fake.bind = boom
    monkeypatch.setattr(disc.socket, "socket", lambda *a, **k: fake)
    with pytest.raises(OSError):
        _listen_for_beacons(0.05)
    assert fake.closed


# --- discover --------------------------------------------------------------
async def test_discover_builds_devices(monkeypatch):
    monkeypatch.setattr(disc, "_listen_for_beacons", lambda t: {"10.0.0.5"})
    monkeypatch.setattr(disc, "_device_name", lambda ip: "Living Room")
    devices = await discover(0.01)
    assert len(devices) == 1
    assert devices[0].ip == "10.0.0.5"
    assert devices[0].name == "Living Room"


async def test_discover_falls_back_to_ip_as_name(monkeypatch):
    monkeypatch.setattr(disc, "_listen_for_beacons", lambda t: {"10.0.0.7"})
    monkeypatch.setattr(disc, "_device_name", lambda ip: None)
    devices = await discover(0.01)
    assert devices[0].name == "10.0.0.7"


async def test_discover_empty(monkeypatch):
    monkeypatch.setattr(disc, "_listen_for_beacons", lambda t: set())
    assert await discover(0.01) == []


async def test_discover_swallows_listen_error(monkeypatch):
    def boom(t):
        raise RuntimeError("socket exploded")
    monkeypatch.setattr(disc, "_listen_for_beacons", boom)
    assert await discover(0.01) == []


async def test_discover_is_repeatable(monkeypatch):
    """Regression: pixelblaze-client's LightweightEnumerator dedupes against a
    CLASS-level `seenPixelblazes` list that is never cleared, so it reported a
    device only on the FIRST scan of a process and nothing afterwards — the
    "can't rediscover until I restart the software" bug. Our discovery must
    return the device on every call."""
    monkeypatch.setattr(disc, "_listen_for_beacons", lambda t: {"10.0.0.5"})
    monkeypatch.setattr(disc, "_device_name", lambda ip: "Living Room")
    for _ in range(5):
        devices = await discover(0.01)
        assert [d.name for d in devices] == ["Living Room"]


async def test_discover_sorted_by_ip(monkeypatch):
    monkeypatch.setattr(disc, "_listen_for_beacons",
                        lambda t: {"10.0.0.9", "10.0.0.2"})
    monkeypatch.setattr(disc, "_device_name", lambda ip: ip)
    devices = await discover(0.01)
    assert [d.ip for d in devices] == ["10.0.0.2", "10.0.0.9"]


# --- probe / misc ----------------------------------------------------------
async def test_probe_returns_device():
    dev = await probe("9.9.9.9")
    assert isinstance(dev, PixelblazeDevice)
    assert dev.ip == "9.9.9.9"
    assert dev.name == "FakePB"


def test_close_stale_sockets_safe_without_proc():
    # On a non-Linux dev box there's no /proc/net/udp; must be a silent no-op.
    _close_stale_discovery_sockets()
