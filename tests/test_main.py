"""Tests for main.py — the App orchestration logic (all hardware faked)."""
import asyncio
from types import SimpleNamespace

import pytest

from conf import config
import main
from main import App
from app import connection as app_connection
from app import preview as app_preview
from app import util as app_util
from pb.discovery import PixelblazeDevice
from wifi import manager as wifi_manager
from ui.screens import SettingsScreen, InfoScreen, ReconnectScreen, MainScreen


@pytest.fixture
async def app(temp_settings):
    """A fully-constructed App with faked hardware, on the test's event loop."""
    a = App()
    yield a
    a._led_thread_stop.set()
    if a._led_thread is not None:
        a._led_thread.join(timeout=1)


# --- module-level helpers --------------------------------------------------
def test_fmt_uptime_no_proc(monkeypatch):
    def boom(*a, **k):
        raise OSError("no /proc")
    monkeypatch.setattr("builtins.open", boom)
    assert main._fmt_uptime() == "?"


def test_fmt_uptime_minutes(monkeypatch, tmp_path):
    f = tmp_path / "uptime"
    f.write_text("125.5 100.0")   # 2m 5s
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(f, *a, **k))
    assert main._fmt_uptime() == "2m 5s"


def test_fmt_uptime_hours(monkeypatch, tmp_path):
    f = tmp_path / "uptime"
    f.write_text("3700 0")        # 1h 1m
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(f, *a, **k))
    assert main._fmt_uptime() == "1h 1m"


def test_subnet_prefix(monkeypatch):
    monkeypatch.setattr(app_util, "_local_ip", lambda: "10.0.0.42")
    assert app_util._subnet_prefix() == "10.0.0."


def test_subnet_prefix_empty(monkeypatch):
    monkeypatch.setattr(app_util, "_local_ip", lambda: None)
    assert app_util._subnet_prefix() == ""


# --- _extract_picks (static) ----------------------------------------------
def test_extract_picks_averages(monkeypatch):
    monkeypatch.setattr(config, "LED_COUNT", 2)
    monkeypatch.setattr(config, "LED_STRIP_GROUPS", [[0, 1], [2, 3]])
    # pixels: 0=(0,0,0) 1=(10,10,10) 2=(20,20,20) 3=(40,40,40)
    frame = bytes([0, 0, 0, 10, 10, 10, 20, 20, 20, 40, 40, 40])
    picks = App._extract_picks(frame)
    assert picks == [(5, 5, 5), (30, 30, 30)]


def test_extract_picks_short_frame_pads(monkeypatch):
    monkeypatch.setattr(config, "LED_COUNT", 2)
    monkeypatch.setattr(config, "LED_STRIP_GROUPS", [[0], [14]])
    frame = bytes([100, 100, 100])   # only pixel 0 exists
    picks = App._extract_picks(frame)
    assert picks == [(100, 100, 100), (0, 0, 0)]


# --- _render_from_buffer ---------------------------------------------------
async def test_render_from_buffer_empty(app):
    app._frame_buffer = ()
    assert app._render_from_buffer() is None


async def test_render_from_buffer_interpolates(app, monkeypatch):
    monkeypatch.setattr(config, "LED_PLAYBACK_DELAY_SEC", 0.4)
    black = [(0, 0, 0)] * 8
    red = [(255, 0, 0)] * 8
    app._frame_buffer = ((100.0, black), (100.1, red))
    monkeypatch.setattr(main.time, "monotonic", lambda: 100.45)  # target=100.05 -> f=0.5
    picks = app._render_from_buffer()
    assert picks[0] == (127, 0, 0)


async def test_render_from_buffer_still_filling_plays_live(app, monkeypatch):
    # While the window is refilling (fresh connect, or a pattern change just
    # flushed it) we play the NEWEST frame rather than blanking the strip.
    monkeypatch.setattr(config, "LED_PLAYBACK_DELAY_SEC", 0.4)
    newest = [(9, 9, 9)] * 8
    app._frame_buffer = ((100.0, [(1, 1, 1)] * 8), (100.05, newest))
    monkeypatch.setattr(main.time, "monotonic", lambda: 100.1)  # target < first ts
    assert app._render_from_buffer() == newest


async def test_render_from_buffer_none_when_no_stream(app):
    app._frame_buffer = ()
    assert app._render_from_buffer() is None


async def test_render_from_buffer_past_last_holds(app, monkeypatch):
    monkeypatch.setattr(config, "LED_PLAYBACK_DELAY_SEC", 0.4)
    last = [(9, 9, 9)] * 8
    app._frame_buffer = ((100.0, [(0, 0, 0)] * 8), (100.1, last))
    monkeypatch.setattr(main.time, "monotonic", lambda: 101.0)  # target=100.6 > last
    assert app._render_from_buffer() == last


# --- _on_preview_frame -----------------------------------------------------
async def test_on_preview_frame_buffers(app, monkeypatch):
    monkeypatch.setattr(config, "LED_COUNT", 2)
    monkeypatch.setattr(config, "LED_STRIP_GROUPS", [[0], [1]])
    app._led_brightness = 5
    app._on_preview_frame(bytes([1, 2, 3, 4, 5, 6]))
    assert len(app._frame_buffer) == 1
    assert app._recv_count == 1


async def test_on_preview_frame_skipped_when_brightness_zero(app):
    app._led_brightness = 0
    app._on_preview_frame(bytes([1, 2, 3]))
    assert app._frame_buffer == ()


async def test_pattern_change_flushes_buffer(app, monkeypatch):
    # Buffered frames belong to the old pattern; the playback delay would keep
    # showing them after the change, so they must be dropped.
    monkeypatch.setattr(config, "LED_COUNT", 1)
    monkeypatch.setattr(config, "LED_STRIP_GROUPS", [[0]])
    app._led_brightness = 5
    app._pb_client = SimpleNamespace(active_pattern_id="pat1")
    for _ in range(3):
        app._on_preview_frame(bytes([1, 2, 3]))
    assert len(app._frame_buffer) == 3

    app._pb_client.active_pattern_id = "pat2"      # user turned the knob
    app._on_preview_frame(bytes([4, 5, 6]))
    assert len(app._frame_buffer) == 1             # flushed, then the new frame


async def test_same_pattern_does_not_flush(app, monkeypatch):
    monkeypatch.setattr(config, "LED_COUNT", 1)
    monkeypatch.setattr(config, "LED_STRIP_GROUPS", [[0]])
    app._led_brightness = 5
    app._pb_client = SimpleNamespace(active_pattern_id="pat1")
    for _ in range(3):
        app._on_preview_frame(bytes([1, 2, 3]))
    assert len(app._frame_buffer) == 3


# --- per-pattern frame cache ----------------------------------------------
def feed(app, monkeypatch, pattern_id, n, t, step=0.02):
    """Push n frames for `pattern_id`, advancing the fake clock."""
    app._pb_client.active_pattern_id = pattern_id
    for _ in range(n):
        t[0] += step
        app._on_preview_frame(bytes([1, 2, 3]))


@pytest.fixture
def cache_app(app, monkeypatch):
    monkeypatch.setattr(config, "LED_COUNT", 1)
    monkeypatch.setattr(config, "LED_STRIP_GROUPS", [[0]])
    monkeypatch.setattr(config, "LED_PLAYBACK_DELAY_SEC", 0.4)
    app._led_brightness = 5
    app._pb_client = SimpleNamespace(active_pattern_id="pat1")
    return app


async def test_leaving_pattern_stashes_buffer(cache_app, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])
    feed(cache_app, monkeypatch, "pat1", 5, t)
    feed(cache_app, monkeypatch, "pat2", 1, t)   # switch away
    assert "pat1" in cache_app._pattern_buffers
    assert len(cache_app._pattern_buffers["pat1"]) == 5


async def test_returning_to_pattern_restores_buffer(cache_app, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])
    feed(cache_app, monkeypatch, "pat1", 5, t)
    feed(cache_app, monkeypatch, "pat2", 1, t)
    feed(cache_app, monkeypatch, "pat1", 1, t)   # come back
    # Restored frames (5) + the one live frame just appended.
    assert len(cache_app._frame_buffer) == 6


async def test_restored_frames_are_rebased_to_now(cache_app, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])
    feed(cache_app, monkeypatch, "pat1", 5, t)
    feed(cache_app, monkeypatch, "pat2", 1, t)
    t[0] += 60.0                                  # a long time later
    feed(cache_app, monkeypatch, "pat1", 1, t)
    # Every restored timestamp must be recent, not from a minute ago —
    # otherwise the playhead treats them as ancient and the age prune drops
    # them on the very next frame.
    for ts, _ in cache_app._frame_buffer:
        assert t[0] - ts < 1.0


async def test_restored_buffer_is_immediately_playable(cache_app, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])
    feed(cache_app, monkeypatch, "pat1", 20, t)   # ~0.4s of history
    feed(cache_app, monkeypatch, "pat2", 1, t)
    t[0] += 30.0
    feed(cache_app, monkeypatch, "pat1", 1, t)
    # Delayed interpolation works right away — no live-only fallback.
    picks = cache_app._render_from_buffer()
    assert picks is not None


async def test_recall_trims_to_playback_window(cache_app, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])
    # 1s of frames, but only the last LED_PLAYBACK_DELAY_SEC should be recalled.
    feed(cache_app, monkeypatch, "pat1", 50, t)
    feed(cache_app, monkeypatch, "pat2", 1, t)
    restored = cache_app._recall_pattern_buffer("pat1", t[0])
    span = restored[-1][0] - restored[0][0]
    assert span <= config.LED_PLAYBACK_DELAY_SEC + 1e-6


async def test_unknown_pattern_recalls_nothing(cache_app):
    assert cache_app._recall_pattern_buffer("never-seen", 100.0) == ()


async def test_cache_evicts_past_cap(cache_app, monkeypatch):
    monkeypatch.setattr(config, "LED_PATTERN_CACHE_MAX", 3)
    t = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])
    for i in range(6):
        feed(cache_app, monkeypatch, f"pat{i}", 2, t)
    assert len(cache_app._pattern_buffers) <= 3


async def test_stop_preview_stashes_for_reconnect(cache_app, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])
    feed(cache_app, monkeypatch, "pat1", 5, t)
    cache_app._stop_preview_client()
    assert "pat1" in cache_app._pattern_buffers
    assert cache_app._frame_buffer == ()
    assert cache_app._last_preview_pattern is None   # next frame triggers recall


async def test_preview_client_started_with_frame_callback(app, monkeypatch):
    made = {}

    class FakePreview:
        def __init__(self, ip, on_frame):
            made["ip"] = ip
            made["on_frame"] = on_frame

        def start(self):
            made["started"] = True

    monkeypatch.setattr(app_preview, "PreviewClient", FakePreview)
    app._led_brightness = 5
    app._low_battery = False
    app._start_preview_client("1.2.3.4")
    assert made["ip"] == "1.2.3.4"
    assert made["on_frame"] == app._on_preview_frame
    assert made["started"] is True


async def test_on_preview_frame_caps_buffer(app, monkeypatch):
    monkeypatch.setattr(config, "LED_COUNT", 1)
    monkeypatch.setattr(config, "LED_STRIP_GROUPS", [[0]])
    monkeypatch.setattr(config, "LED_FRAME_BUFFER_MAX", 10)
    app._led_brightness = 5
    t = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])
    for _ in range(40):
        t[0] += 0.001
        app._on_preview_frame(bytes([1, 2, 3]))
    assert len(app._frame_buffer) <= 10


# --- low battery -----------------------------------------------------------
async def test_check_low_battery_enters(app, monkeypatch):
    stops = []
    monkeypatch.setattr(app, "_stop_preview_client", lambda: stops.append(True))
    app._check_low_battery(8)   # < 10
    assert app._low_battery is True
    assert app._low_battery_count == 3   # 8 - 5
    assert stops == [True]


async def test_check_low_battery_refreshes_count(app, monkeypatch):
    monkeypatch.setattr(app, "_stop_preview_client", lambda: None)
    app._check_low_battery(8)
    app._check_low_battery(6)   # still low, just update count
    assert app._low_battery_count == 1


async def test_check_low_battery_exits(app, monkeypatch):
    monkeypatch.setattr(app, "_stop_preview_client", lambda: None)
    starts = []
    monkeypatch.setattr(app, "_start_preview_client", lambda ip: starts.append(ip))
    app._check_low_battery(8)     # enter
    app._check_low_battery(50)    # recover
    assert app._low_battery is False


async def test_low_battery_no_op_when_healthy(app, monkeypatch):
    monkeypatch.setattr(app, "_stop_preview_client", lambda: None)
    app._check_low_battery(80)
    assert app._low_battery is False


# --- LED brightness change -------------------------------------------------
async def test_brightness_to_zero_stops_preview(app, monkeypatch):
    stops = []
    monkeypatch.setattr(app, "_stop_preview_client", lambda: stops.append(True))
    app._led_brightness = 5
    app._on_led_brightness_change(0)
    assert app._led_brightness == 0
    assert stops == [True]


async def test_brightness_from_zero_starts_preview(app, monkeypatch):
    starts = []
    monkeypatch.setattr(app, "_start_preview_client", lambda ip: starts.append(ip))
    app._pb_client = SimpleNamespace(ip="1.2.3.4")
    app._led_brightness = 0
    app._on_led_brightness_change(5)
    assert starts == ["1.2.3.4"]


async def test_brightness_change_persists(app, temp_settings, monkeypatch):
    monkeypatch.setattr(app, "_start_preview_client", lambda ip: None)
    app._pb_client = None
    app._on_led_brightness_change(7)
    import store
    monkeypatch.setattr(store, "_data", None)
    assert store.get("led_brightness") == 7


async def test_load_led_brightness_clamps(app, monkeypatch):
    import store
    monkeypatch.setattr(store, "get", lambda k, d=None: 99)
    assert app._load_led_brightness() == 25


async def test_load_backlight_clamps(app, monkeypatch):
    import store
    monkeypatch.setattr(store, "get", lambda k, d=None: 50)
    assert app._load_backlight() == 9


# --- navigation stack ------------------------------------------------------
def scr(name):
    return SimpleNamespace(name=name, render=lambda lcd: None)


async def test_push_and_pop(app):
    main_s, settings_s = scr("main"), scr("settings")
    app._screen = main_s
    app._push_screen(settings_s)
    assert app._screen is settings_s
    assert app._nav_stack == [main_s]
    assert app._in_menu is True
    app._pop_screen()
    assert app._screen is main_s
    assert app._nav_stack == []
    assert app._in_menu is False


async def test_pop_multi_level_returns_previous(app):
    # main -> settings -> devices -> ip; back should unwind one at a time.
    a, b, c, d = scr("main"), scr("settings"), scr("devices"), scr("ip")
    app._screen = a
    app._push_screen(b)
    app._push_screen(c)
    app._push_screen(d)
    app._pop_screen()
    assert app._screen is c     # devices, NOT settings (the reported bug)
    app._pop_screen()
    assert app._screen is b


async def test_notify_pop_calls_will_pop(app):
    popped = []
    s = SimpleNamespace(will_pop=lambda: popped.append(True),
                        render=lambda lcd: None)
    app._screen = s
    app._nav_stack = [scr("prev")]
    app._pop_screen()
    assert popped == [True]


async def test_replace_screen_notifies_old(app):
    popped = []
    old = SimpleNamespace(will_pop=lambda: popped.append(True))
    app._screen = old
    app._replace_screen(SimpleNamespace(render=lambda lcd: None))
    assert popped == [True]


async def test_refresh_settings_pushes_state(app):
    app._ssid = "MyNet"
    app._backlight_level = 4
    app._led_brightness = 12
    s = SettingsScreen(client=None)
    app._refresh_settings(s)
    assert s._ssid == "MyNet"
    assert s._backlight == 4
    assert s._led_brightness == 12


async def test_make_settings_wires_callbacks(app):
    s = app._make_settings()
    assert isinstance(s, SettingsScreen)
    assert s._on_led_brightness_change == app._on_led_brightness_change
    assert s._on_restart_software == app._restart_software


# --- Device FPS on the info page -------------------------------------------
async def test_device_fps_prefers_preview_client(app):
    app._preview_client = SimpleNamespace(device_fps=62.4)
    app._pb_client = SimpleNamespace(device_fps=10)
    assert app._device_fps_text() == "62"     # preview stream is freshest


async def test_device_fps_falls_back_to_pb_client(app):
    # No preview stream (LED brightness 0 / low battery) — use the last poll.
    app._preview_client = None
    app._pb_client = SimpleNamespace(device_fps=45.0)
    assert app._device_fps_text() == "45"


async def test_device_fps_na_when_unknown(app):
    app._preview_client = None
    app._pb_client = None
    assert app._device_fps_text() == "n/a"


async def test_device_fps_na_when_never_reported(app):
    app._preview_client = SimpleNamespace(device_fps=None)
    app._pb_client = SimpleNamespace(device_fps=None)
    assert app._device_fps_text() == "n/a"


async def test_collect_info_includes_device_fps(app):
    app._preview_client = SimpleNamespace(device_fps=61.0)
    info = await app._collect_info()
    assert info["Device FPS"] == "61"


# --- escalating recovery ---------------------------------------------------
async def test_recover_direct_success(app, monkeypatch):
    monkeypatch.setattr(app, "_start_preview_client", lambda ip: None)
    target = PixelblazeDevice(ip="1.2.3.4", name="Living Room", device_id=1)
    app._reconnect_target = target
    await app._recover_connection()
    # Fake Pixelblaze connects successfully on the first (direct-IP) attempt.
    assert app._reconnect_target is None
    assert app._connected_device is target
    assert app._reconnect_attempts == 0
    assert isinstance(app._screen, MainScreen)
    app._teardown_client()


class _FailClient:
    def __init__(self, device):
        self._device = device

    async def connect(self):
        raise RuntimeError("unreachable")

    def cancel_pending(self):
        pass

    def close_socket(self):
        pass


async def test_recover_failure_keeps_target(app, monkeypatch):
    monkeypatch.setattr(app_connection, "PixelblazeClient", _FailClient)
    target = PixelblazeDevice(ip="1.2.3.4", name="X", device_id=1)
    app._reconnect_target = target
    await app._recover_connection()
    assert app._reconnect_target is target   # kept, so the manager retries
    assert app._reconnect_attempts == 1
    assert app._pb_client is None


async def test_recover_shows_reconnect_screen(app, monkeypatch):
    monkeypatch.setattr(app_connection, "PixelblazeClient", _FailClient)
    app._reconnect_target = PixelblazeDevice(ip="1.2.3.4", name="Living Room", device_id=1)
    app._screen = None
    await app._recover_connection()
    assert isinstance(app._screen, ReconnectScreen)


async def test_recover_escalates_to_discovery(app, monkeypatch):
    # After DIRECT_ATTEMPTS, recovery uses discovery to find the same device
    # by name (catches a DHCP IP change) instead of the stale IP.
    monkeypatch.setattr(app, "_start_preview_client", lambda ip: None)

    async def fake_discover():
        # Same name, NEW ip.
        return [PixelblazeDevice(ip="5.5.5.5", name="Living Room", device_id=9)]
    monkeypatch.setattr(app_connection, "discover", fake_discover)

    target = PixelblazeDevice(ip="1.2.3.4", name="Living Room", device_id=1)
    app._reconnect_target = target
    app._reconnect_attempts = app_connection.DIRECT_ATTEMPTS   # next attempt escalates
    await app._recover_connection()
    # Reconnected to the same name at the new IP.
    assert app._connected_device.ip == "5.5.5.5"
    assert app._reconnect_target is None
    app._teardown_client()


async def test_recover_discovery_not_found_keeps_target(app, monkeypatch):
    async def fake_discover():
        return [PixelblazeDevice(ip="5.5.5.5", name="SomeOtherPB", device_id=9)]
    monkeypatch.setattr(app_connection, "discover", fake_discover)
    target = PixelblazeDevice(ip="1.2.3.4", name="Living Room", device_id=1)
    app._reconnect_target = target
    app._reconnect_attempts = app_connection.DIRECT_ATTEMPTS
    await app._recover_connection()
    assert app._reconnect_target is target   # not found, keep trying


async def test_recovery_watchdog_restarts_when_stuck(app, monkeypatch):
    # Unattended device: if recovery gets nowhere for RECOVERY_RESTART_SEC,
    # restart the process rather than stay wedged.
    restarted = []
    monkeypatch.setattr(app, "_restart_software", lambda: restarted.append(True))
    monkeypatch.setattr(app_connection, "PixelblazeClient", _FailClient)
    app._reconnect_target = PixelblazeDevice(ip="1.2.3.4", name="X", device_id=1)
    app._recovery_started_at = main.time.monotonic() - (app_connection.RECOVERY_RESTART_SEC + 1)
    await app._recover_connection()
    assert restarted == [True]


async def test_recovery_watchdog_quiet_before_threshold(app, monkeypatch):
    restarted = []
    monkeypatch.setattr(app, "_restart_software", lambda: restarted.append(True))
    monkeypatch.setattr(app_connection, "PixelblazeClient", _FailClient)
    app._reconnect_target = PixelblazeDevice(ip="1.2.3.4", name="X", device_id=1)
    app._recovery_started_at = main.time.monotonic()   # just started
    await app._recover_connection()
    assert restarted == []


async def test_successful_recovery_clears_watchdog(app, monkeypatch):
    monkeypatch.setattr(app, "_start_preview_client", lambda ip: None)
    app._reconnect_target = PixelblazeDevice(ip="1.2.3.4", name="Living Room", device_id=1)
    app._recovery_started_at = main.time.monotonic()
    await app._recover_connection()
    assert app._recovery_started_at == 0.0
    app._teardown_client()


async def test_reset_wifi_transition_spawns_flow(app, monkeypatch):
    ran = []

    async def fake_flow():
        ran.append(True)

    monkeypatch.setattr(app, "_reset_wifi_flow", fake_flow)
    await app._transition("reset_wifi")
    await asyncio.sleep(0)   # let the spawned task run
    assert ran == [True]


async def test_hard_reset_logs_and_tears_down(app, monkeypatch):
    torn = []
    monkeypatch.setattr(app, "_teardown_client", lambda: torn.append(True) or None)
    await app._hard_reset()
    assert torn == [True]


async def test_establish_routes_to_recovery(app, monkeypatch):
    called = []

    async def fake_recover():
        called.append(True)

    async def yes():
        return True

    async def ssid():
        return "Net"

    monkeypatch.setattr(app, "_recover_connection", fake_recover)
    monkeypatch.setattr(wifi_manager, "is_connected", yes)
    monkeypatch.setattr(wifi_manager, "current_ssid", ssid)
    app._reconnect_target = PixelblazeDevice(ip="1.2.3.4", name="X", device_id=1)
    app._selected_device = None
    app._screen = None
    await app._establish()
    assert called == [True]


async def test_cancel_reconnect_clears_target(app, monkeypatch):
    monkeypatch.setattr(app, "_teardown_client", lambda: None)
    app._reconnect_target = PixelblazeDevice(ip="1.2.3.4", name="X", device_id=1)
    app._reconnect_attempts = 5
    await app._transition("cancel_reconnect")
    assert app._reconnect_target is None
    assert app._reconnect_attempts == 0
    assert app._force_picker is True   # falls into the picker after search


async def test_selected_device_overrides_reconnect(app, monkeypatch):
    # An explicit user pick must clear any pending reconnect target.
    async def yes():
        return True

    async def ssid():
        return "Net"

    async def fake_connect(dev):
        return True

    monkeypatch.setattr(wifi_manager, "is_connected", yes)
    monkeypatch.setattr(wifi_manager, "current_ssid", ssid)
    monkeypatch.setattr(app, "_connect", fake_connect)
    app._reconnect_target = PixelblazeDevice(ip="9.9.9.9", name="Old", device_id=1)
    app._selected_device = PixelblazeDevice(ip="1.1.1.1", name="New", device_id=2)
    app._screen = None
    await app._establish()
    assert app._reconnect_target is None
