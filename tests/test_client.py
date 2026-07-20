"""Tests for pb/client.py — config parsing, control normalization, debounce."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import pb.client as client_mod
from pb.client import PixelblazeClient, _parse_config
from pb.discovery import PixelblazeDevice


@pytest.fixture
def client():
    c = PixelblazeClient(PixelblazeDevice(ip="1.2.3.4", name="MyPB", device_id=1))
    yield c
    c._cancel_debounce()
    c._executor.shutdown(wait=False)


# --- _norm_controls --------------------------------------------------------
def test_norm_controls_clamps_floats():
    out = PixelblazeClient._norm_controls({"a": 1.5, "b": -0.2, "c": 0.5})
    assert out == {"a": 1.0, "b": 0.0, "c": 0.5}


def test_norm_controls_rgb_list():
    out = PixelblazeClient._norm_controls({"col": [2.0, 0.5, -1.0, 9.9]})
    assert out == {"col": [1.0, 0.5, 0.0]}   # first 3, clamped


def test_norm_controls_drops_short_lists():
    out = PixelblazeClient._norm_controls({"bad": [0.1, 0.2]})
    assert out == {}


def test_norm_controls_ignores_non_numeric():
    out = PixelblazeClient._norm_controls({"s": "nope", "n": 0.3})
    assert out == {"n": 0.3}


# --- _parse_config ---------------------------------------------------------
def test_parse_config_extracts_fields():
    seq = {
        "activeProgram": {"activeProgramId": "pat1", "controls": {"x": 0.5}},
        "runSequencer": True,
        "sequencerMode": 2,
    }
    pb = SimpleNamespace(
        getConfigSettings=lambda: {"brightness": 0.8},
        latestSequencer=json.dumps(seq),
    )
    cfg = _parse_config(pb)
    assert cfg["pattern_id"] == "pat1"
    assert cfg["controls"] == {"x": 0.5}
    assert cfg["brightness"] == 0.8
    assert cfg["seq_running"] is True
    assert cfg["seq_mode"] == 2


def test_parse_config_reads_device_fps():
    pb = SimpleNamespace(
        getConfigSettings=lambda: {},
        latestSequencer=None,
        latestStats='{"fps":63.5,"vmerr":0}',
    )
    assert _parse_config(pb)["fps"] == 63.5


def test_parse_config_fps_none_without_stats():
    pb = SimpleNamespace(getConfigSettings=lambda: {}, latestSequencer=None,
                         latestStats=None)
    assert _parse_config(pb)["fps"] is None


def test_parse_config_survives_corrupt_stats():
    pb = SimpleNamespace(getConfigSettings=lambda: {}, latestSequencer=None,
                         latestStats="not json")
    assert _parse_config(pb)["fps"] is None


def test_parse_config_never_enables_preview_frames():
    """getFPS()/getStatistics() would call setSendPreviewFrames(True), which
    starts streaming preview frames down the INTERACTIVE socket and rebuilds
    the backlog we removed. The poll path must only read cached stats."""
    calls = []
    pb = SimpleNamespace(
        getConfigSettings=lambda: {},
        latestSequencer=None,
        latestStats='{"fps":30}',
        setSendPreviewFrames=lambda on: calls.append(on),
        getStatistics=lambda: calls.append("stats"),
        getFPS=lambda *a: calls.append("fps"),
    )
    _parse_config(pb)
    assert calls == []


def test_parse_config_empty_sequencer():
    pb = SimpleNamespace(getConfigSettings=lambda: {}, latestSequencer=None)
    cfg = _parse_config(pb)
    assert cfg["pattern_id"] is None
    assert cfg["controls"] == {}
    assert cfg["seq_running"] is False
    assert cfg["seq_mode"] == 1


# --- local state -----------------------------------------------------------
async def test_set_control_clamps_and_marks_pending(client):
    # async: set_control schedules a debounce task, which needs a running loop.
    client.set_control("slider", 2.0)
    assert client.controls["slider"] == 1.0
    assert client._pending_control == ("slider", 1.0)


def test_set_pattern_local_resets_controls(client):
    client._controls = {"old": 0.5}
    client.set_pattern_local("newpat")
    assert client.active_pattern_id == "newpat"
    assert client.controls == {}
    assert client._pending_control is None


async def test_cancel_pending_clears(client):
    client.set_control("s", 0.5)
    client.cancel_pending()
    assert client._pending_control is None


def test_ip_and_name_properties(client):
    assert client.ip == "1.2.3.4"
    assert client.device_name == "MyPB"


# --- debounce --------------------------------------------------------------
async def test_debounce_fires_factory(client, monkeypatch):
    monkeypatch.setattr(client_mod, "DEBOUNCE_SEC", 0.0)
    fired = []

    async def factory():
        fired.append(True)

    client._debounce("k", factory)
    await asyncio.sleep(0.02)
    assert fired == [True]


async def test_debounce_supersedes(client, monkeypatch):
    monkeypatch.setattr(client_mod, "DEBOUNCE_SEC", 0.05)
    calls = []

    def make(n):
        async def f():
            calls.append(n)
        return f

    client._debounce("k", make(1))
    client._debounce("k", make(2))   # cancels the first
    await asyncio.sleep(0.12)
    assert calls == [2]


async def test_cancel_debounce_prevents_fire(client, monkeypatch):
    monkeypatch.setattr(client_mod, "DEBOUNCE_SEC", 0.05)
    fired = []

    async def factory():
        fired.append(True)

    client._debounce("control:x", factory)
    client._cancel_debounce("control")
    await asyncio.sleep(0.12)
    assert fired == []
