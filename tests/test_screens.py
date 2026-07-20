"""Tests for ui/screens.py — velocity stepper, scroller, Row, and every screen."""
from types import SimpleNamespace

import pytest

import ui.screens as screens
from ui.screens import (
    _VelocityStep, _Scroller, Row, ListScreen, MainScreen, SettingsScreen,
    ConfirmScreen, ChoiceScreen, DeviceSelectScreen, DiscoveringScreen,
    ReconnectScreen, StatusScreen, InfoScreen,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(screens.time, "monotonic", c)
    return c


# --- _VelocityStep ---------------------------------------------------------
def test_velocity_slow_step(clock):
    v = _VelocityStep(scale=100, slow=1, fast=5)
    # First call: _last=0 so dt is huge -> slow path, +1/100.
    assert v.apply(0.5, 1) == pytest.approx(0.51)


def test_velocity_slow_after_pause(clock):
    v = _VelocityStep(scale=100, slow=1, fast=5)
    v.apply(0.5, 1)          # prime
    clock.t += 1.0           # well past threshold -> slow again
    assert v.apply(0.5, 1) == pytest.approx(0.51)


def test_velocity_clamps_high(clock):
    v = _VelocityStep()
    assert v.apply(1.0, 1) == 1.0


def test_velocity_clamps_low(clock):
    v = _VelocityStep()
    assert v.apply(0.0, -1) == 0.0


def test_velocity_fast_snaps_to_multiple(clock):
    v = _VelocityStep(scale=100, slow=1, fast=5)
    v.apply(0.5, 1)          # prime
    clock.t += 0.01          # within threshold -> fast path
    result = v.apply(0.52, 1)   # u=52 -> snap up to 55
    assert result == pytest.approx(0.55)


# --- _Scroller -------------------------------------------------------------
def measure_len(s):
    return len(s)


def test_scroller_short_text_unchanged(clock):
    s = _Scroller()
    assert s.get("hi", 10, measure_len) == "hi"


def test_scroller_long_text_starts_at_head(clock):
    s = _Scroller()
    # pause_start window: shows the head prefix that fits.
    assert s.get("hello world", 5, measure_len) == "hello"


def test_scroller_advances_over_time(clock):
    s = _Scroller()
    s.get("hello world", 5, measure_len)      # t0
    clock.t += 10                              # well into the cycle
    later = s.get("hello world", 5, measure_len)
    assert later != "hello"                    # it scrolled


def test_scroller_resets_on_new_text(clock):
    s = _Scroller()
    s.get("aaaaaaa", 3, measure_len)
    t_before = s._t0
    clock.t += 5
    s.get("different", 3, measure_len)         # new text resets timer
    assert s._t0 == clock.t and s._t0 != t_before


# --- Row -------------------------------------------------------------------
def test_row_both_arrows_by_default():
    r = Row("L", "V", on_turn=lambda d: None)
    assert r.can_turn_left and r.can_turn_right


def test_row_turn_dir_left_only():
    r = Row("On", on_turn=lambda d: None, turn_dir="left")
    assert r.can_turn_left and not r.can_turn_right


def test_row_turn_dir_right_only():
    r = Row("Off", on_turn=lambda d: None, turn_dir="right")
    assert r.can_turn_right and not r.can_turn_left


def test_row_no_turn_no_arrows():
    r = Row("Label", on_open=lambda: "x")
    assert not r.can_turn_left and not r.can_turn_right


def test_row_display_dict():
    r = Row("L", "V", on_open=lambda: "x", mark=True, fixed_arrows=True)
    d = r.display(active=True)
    assert d["drill"] is True      # on_open and not on_turn
    assert d["mark"] is True
    assert d["fixed_arrows"] is True


# --- ListScreen ------------------------------------------------------------
class _DummyList(ListScreen):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    def rows(self):
        return self._rows


def test_selectable_skips_dividers():
    rows = [Row("a"), Row("div", divider=True), Row("b")]
    assert ListScreen._selectable(rows) == [0, 2]


async def test_enc1_rotate_moves_cursor():
    opened = []
    s = _DummyList([Row("a", on_open=lambda: "A"),
                    Row("b", on_open=lambda: "B")])
    await s.handle(("encoder", "enc1", 1))
    assert s._cursor == 1


async def test_enc2_rotate_calls_on_turn():
    turned = []
    s = _DummyList([Row("a", on_turn=lambda d: turned.append(d))])
    await s.handle(("encoder", "enc2", 1))
    assert turned == [1]


async def test_enc1_press_opens_transition():
    s = _DummyList([Row("a", on_open=lambda: "target")])
    assert await s.handle(("press", "enc1")) == "target"


async def test_enc2_press_backs_out():
    s = _DummyList([Row("a")])
    # top-level default _on_back returns "__back__"
    assert await s.handle(("press", "enc2")) == "__back__"


# --- MainScreen ------------------------------------------------------------
def make_client(**kw):
    defaults = dict(
        patterns={"p1": "Sparkle", "p2": "Rainbow Melt"},
        active_pattern_id="p1",
        controls={},
        sequencer_running=False,
        sequencer_shuffle=False,
        playlist=[],
        brightness=1.0,
        device_name="MyPB",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_mainscreen_label_strips_prefixes():
    assert MainScreen._label("sliderSpeed") == "Speed"
    assert MainScreen._label("hsvpickerColor") == "Color"
    assert MainScreen._label("Plain") == "Plain"


def test_mainscreen_rows_has_pattern_and_settings():
    c = make_client(controls={"sliderSpeed": 0.5})
    s = MainScreen(c)
    labels = [r.label for r in s.rows()]
    assert labels[0] == "Pattern"
    assert "Speed" in labels
    assert labels[-1] == "Settings"


def test_mainscreen_pattern_fixed_arrows():
    s = MainScreen(make_client())
    pattern_row = s.rows()[0]
    assert pattern_row.fixed_arrows is True


def test_mainscreen_playlist_mode_right_only():
    c = make_client(sequencer_running=True, playlist=["p1", "p2"])
    s = MainScreen(c)
    pattern_row = s.rows()[0]
    assert pattern_row.turn_dir == "right"


def test_cycle_pattern_commits(monkeypatch):
    committed = []
    c = make_client()
    c.set_pattern_local = lambda pid: None
    c.commit_pattern = lambda pid: committed.append(pid)
    c.advance_playlist = lambda n: None
    s = MainScreen(c)
    s._cycle_pattern(1)
    assert len(committed) == 1


# --- SettingsScreen --------------------------------------------------------
def test_settings_device_rows_without_client():
    s = SettingsScreen(client=None)
    labels = [r.label for r in s.rows()]
    assert "Backlight" in labels
    assert "LED Brightness" in labels
    assert "Restart" in labels
    assert "Power off" in labels
    assert "Brightness" not in labels   # PB-only row hidden without a client


def test_settings_toggle_turn_dir():
    c = SimpleNamespace(brightness=1.0, sequencer_running=True, sequencer_shuffle=False)
    s = SettingsScreen(client=c)
    rows = {r.label: r for r in s.rows()}
    assert rows["Playlist"].turn_dir == "left"    # On -> can only go Off
    assert rows["Shuffle"].turn_dir == "right"    # Off -> can only go On


def test_led_brightness_turn_clamps():
    seen = []
    s = SettingsScreen(client=None, led_brightness=24,
                       on_led_brightness_change=seen.append)
    s._led_brightness_turn(1)   # 24 -> 25
    s._led_brightness_turn(1)   # clamp at 25
    assert s._led_brightness == 25
    assert seen == [25]         # only fired on actual change


def test_led_brightness_turn_floor_zero():
    s = SettingsScreen(client=None, led_brightness=1)
    s._led_brightness_turn(-1)
    s._led_brightness_turn(-1)
    assert s._led_brightness == 0   # 0 allowed


def test_backlight_turn_never_zero():
    s = SettingsScreen(client=None, backlight_level=1)
    s._backlight_turn(-1)
    assert s._backlight == 1     # never fully off


def test_timeout_label_and_index():
    assert SettingsScreen._timeout_label(60) == "1m"
    assert SettingsScreen._timeout_label(None) == "Never"
    idx = SettingsScreen._timeout_index(60)
    assert SettingsScreen._TIMEOUTS[idx] == ("1m", 60)


def test_open_restart_is_choice_screen():
    s = SettingsScreen(client=None)
    result = s._open_restart()
    assert isinstance(result, ChoiceScreen)
    labels = [r.label for r in result.rows()]
    assert labels == ["Restart software", "Restart device", "Cancel"]


# --- ConfirmScreen / ChoiceScreen ------------------------------------------
def test_confirm_yes_calls_and_backs():
    fired = []
    s = ConfirmScreen("Power off?", on_yes=lambda: fired.append(True))
    yes_row = [r for r in s.rows() if r.label == "Yes"][0]
    assert yes_row.on_open() == "__back__"
    assert fired == [True]


def test_choice_cancel_is_noop():
    s = ChoiceScreen("Restart?", [("Go", lambda: None), ("Cancel", None)])
    cancel_row = [r for r in s.rows() if r.label == "Cancel"][0]
    assert cancel_row.on_open() == "__back__"   # None callback, just dismiss


def test_choice_runs_callback():
    ran = []
    s = ChoiceScreen("Q", [("Do it", lambda: ran.append(True))])
    s.rows()[0].on_open()
    assert ran == [True]


# --- DeviceSelectScreen ----------------------------------------------------
def test_device_select_marks_current():
    devs = [SimpleNamespace(name="A"), SimpleNamespace(name="B")]
    s = DeviceSelectScreen(devs, on_select=lambda d: None, current_name="B")
    rows = {r.label: r for r in s.rows()}
    assert rows["A"].mark is False
    assert rows["B"].mark is True


def test_device_select_action_rows():
    s = DeviceSelectScreen([], on_select=lambda d: None)
    labels = [r.label for r in s.rows()]
    assert labels == ["Scan Again", "Connect by IP"]


def test_device_select_pick_selects_and_transitions():
    picked = []
    dev = SimpleNamespace(name="A")
    s = DeviceSelectScreen([dev], on_select=picked.append)
    result = s.rows()[0].on_open()
    assert result == "main"
    assert picked == [dev]


# --- DiscoveringScreen -----------------------------------------------------
async def test_discovering_cancel_on_enc2():
    cancelled = []
    s = DiscoveringScreen(on_cancel=lambda: cancelled.append(True))
    result = await s.handle(("press", "enc2"))
    assert result == "back_from_device_select"
    assert cancelled == [True]


async def test_discovering_ignores_enc1():
    s = DiscoveringScreen(on_cancel=lambda: None)
    assert await s.handle(("press", "enc1")) is None


# --- ReconnectScreen -------------------------------------------------------
async def test_reconnect_cancel_on_enc2():
    s = ReconnectScreen("Living Room")
    assert await s.handle(("press", "enc2")) == "cancel_reconnect"


async def test_reconnect_enc1_resets_wifi():
    s = ReconnectScreen("Living Room")
    assert await s.handle(("press", "enc1")) == "reset_wifi"


async def test_reconnect_ignores_rotation():
    s = ReconnectScreen("Living Room")
    assert await s.handle(("encoder", "enc1", 1)) is None


def test_reconnect_renders(lcd):
    ReconnectScreen("Living Room").render(lcd)
    assert lcd._device.last_image is not None


# --- StatusScreen ----------------------------------------------------------
def test_status_matches():
    s = StatusScreen("Finding", "PixelBlaze...")
    assert s.matches("Finding", "PixelBlaze...")
    assert not s.matches("Finding", "other")


async def test_status_enter_opens_settings():
    s = StatusScreen("No WiFi", "connecting...")
    assert await s.handle(("press", "enc1")) == "settings"


# --- InfoScreen (the refresh-task lifecycle bug) ---------------------------
async def test_info_start_primes_and_runs():
    async def collect():
        return {"Uptime": "1s"}
    info = InfoScreen(collect=collect)
    await info.start()
    assert info._stats == {"Uptime": "1s"}
    assert info._task is not None and not info._task.done()
    info.will_pop()


async def test_info_on_back_does_not_cancel_task():
    async def collect():
        return {"Uptime": "1s"}
    info = InfoScreen(collect=collect)
    await info.start()
    # render calls _on_back() every frame as an availability check; it MUST
    # NOT cancel the refresh task (the reported "frozen readings" bug).
    for _ in range(10):
        assert info._on_back() == "back_from_info"
    assert not info._task.done()
    info.will_pop()


async def test_info_will_pop_cancels_task():
    import asyncio
    async def collect():
        return {"Uptime": "1s"}
    info = InfoScreen(collect=collect)
    await info.start()
    info.will_pop()
    await asyncio.sleep(0.01)
    assert info._task is None or info._task.cancelled() or info._task.done()


def test_info_rows_reflect_stats():
    info = InfoScreen(collect=None)
    info._stats = {"Voltage": "3.8V", "Uptime": "2m"}
    labels = [(r.label, r.value) for r in info.rows()]
    assert ("Voltage", "3.8V") in labels
    assert ("Uptime", "2m") in labels


# --- WifiScanScreen: checkmark marks the CONNECTED network, not secured ------
def test_wifi_scan_marks_current_network():
    from ui.screens import WifiScanScreen
    nets = [SimpleNamespace(ssid="Home", secured=True),
            SimpleNamespace(ssid="Cafe", secured=False)]
    s = WifiScanScreen(nets, on_select=lambda n: None, current_ssid="Cafe")
    rows = {r.label: r for r in s.rows()}
    assert rows["Cafe"].mark is True       # the one we're connected to
    assert rows["Home"].mark is False
    # The old secured "*" indicator is gone — no stray value on either row.
    assert rows["Home"].value == ""
    assert rows["Cafe"].value == ""


def test_wifi_scan_select_routes_to_join():
    from ui.screens import WifiScanScreen
    picked = []
    net = SimpleNamespace(ssid="Home", secured=True)
    s = WifiScanScreen([net], on_select=lambda n: picked.append(n))
    row = next(r for r in s.rows() if r.label == "Home")
    assert row.on_open() == "wifi_join"    # app decides on password, not the screen
    assert picked == [net]
