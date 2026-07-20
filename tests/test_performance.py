"""Performance guards for the OLED render path.

Context: a refactor routed every string through _ink_right, which renders a
scratch PIL image and scans it pixel-by-pixel in Python. _fit_text then called
it once per character while truncating. Measured cost: 14.5ms to fit one long
string, ~5ms for a full frame — roughly 250% of a Raspberry Pi Zero core just
to redraw the screen 12x/second. The device's input went sluggish and it
eventually hard-froze. Nothing in the suite caught it.

These tests mostly count EXPENSIVE OPERATIONS rather than measure wall time,
so they are deterministic and machine-independent. A couple of wall-clock
budgets are included as a backstop with large headroom; they exist to catch
order-of-magnitude regressions, not to police small changes.

If one of these fails, do not just raise the limit — find out what started
doing real work per frame.
"""
import time

import pytest

from PIL import Image, ImageDraw


ROWS = [("Brightness", "72%", False, True), ("Playlist", "On", False, True),
        ("Shuffle", "Off", False, True), ("Backlight", "9", False, True),
        ("LED Brightness", "5", False, True),
        ("Device", "Living Room", True, False),
        ("WiFi", "Home WiFi", True, False)]

LONG = "Gene's Extremely Long PixelBlaze Name"


def items(values=None):
    out = []
    for i, (label, value, drill, arrows) in enumerate(ROWS):
        if values is not None:
            value = values
        out.append({"label": label, "value": value, "drill": drill,
                    "mark": False, "arrows_left": arrows, "arrows_right": arrows,
                    "divider": False, "big": False, "fixed_arrows": False})
    return out


@pytest.fixture
def measured(lcd, monkeypatch):
    """Records every UNCACHED text measurement (the expensive primitive)."""
    calls = []
    orig = lcd._ink_right_uncached

    def counted(text, font=None):
        calls.append(text)
        return orig(text, font)

    monkeypatch.setattr(lcd, "_ink_right_uncached", counted)
    return calls


# --- measurement must be memoised -----------------------------------------
def test_ink_right_measures_each_string_once(lcd, measured):
    for _ in range(10):
        lcd._ink_right("Brightness")
    assert len(measured) == 1, "text measurement is not memoised"


def test_fit_text_is_memoised(lcd, measured):
    lcd._fit_text(LONG, 60)
    first = len(measured)
    for _ in range(10):
        lcd._fit_text(LONG, 60)
    assert len(measured) == first, "_fit_text re-searches on every call"


def test_fit_text_search_is_logarithmic(lcd, measured):
    """Truncation must binary-search the cut point. The original code removed
    one character at a time, each removal costing a full render+scan."""
    lcd._fit_text("x" * 256, 40)
    assert len(measured) <= 20, (
        f"{len(measured)} measurements to fit a 256-char string; "
        "expected a logarithmic search")


# --- unchanged frames must not be redrawn ----------------------------------
def test_identical_render_does_no_drawing(lcd, monkeypatch):
    lcd.set_battery("87")
    lcd.render_list(items(), cursor=0, title="MyPB")

    drew = []
    orig = lcd._draw_text
    monkeypatch.setattr(lcd, "_draw_text",
                        lambda *a, **k: (drew.append(1), orig(*a, **k))[1])
    lcd.render_list(items(), cursor=0, title="MyPB")   # same content
    assert drew == [], "unchanged frame was re-rendered"


def test_changed_content_does_redraw(lcd, monkeypatch):
    # The skip must be content-based, not a blanket "render once".
    lcd.render_list(items(), cursor=0, title="MyPB")
    drew = []
    orig = lcd._draw_text
    monkeypatch.setattr(lcd, "_draw_text",
                        lambda *a, **k: (drew.append(1), orig(*a, **k))[1])
    lcd.render_list(items(), cursor=1, title="MyPB")   # cursor moved
    assert drew, "a real content change was skipped"


def test_message_render_skips_when_unchanged(lcd, monkeypatch):
    lcd.render_message("Finding", "PixelBlaze...")
    drew = []
    orig = lcd._draw_text
    monkeypatch.setattr(lcd, "_draw_text",
                        lambda *a, **k: (drew.append(1), orig(*a, **k))[1])
    lcd.render_message("Finding", "PixelBlaze...")
    assert drew == []


# --- steady state must not re-measure --------------------------------------
def test_steady_state_render_makes_no_new_measurements(lcd, measured):
    """Once a string has been on screen, redrawing must never measure it
    again. Warm every cursor position first — rows scrolled out of view are
    legitimately measured the first time they appear."""
    lcd.set_battery("87")
    for cursor in range(len(ROWS)):
        lcd.render_list(items(), cursor=cursor, title="MyPB")
    measured.clear()
    for cursor in (0, 3, 6, 3, 0, 6, 1):     # every row already seen
        lcd.render_list(items(), cursor=cursor, title="MyPB")
    assert measured == [], f"re-measured on redraw: {measured[:5]}"


def test_scrolling_reuses_measurements_across_frames(lcd, measured):
    # A scroller emits a window of the same string; windows repeat every cycle,
    # so measurement count must stay bounded rather than grow per frame.
    for i in range(40):
        lcd._fit_text(LONG[i % 8:][:14], 60)
    first_pass = len(measured)
    measured.clear()
    for i in range(40):
        lcd._fit_text(LONG[i % 8:][:14], 60)
    assert measured == [], f"second pass re-measured {first_pass} strings"


# --- wall-clock backstops (generous; catch order-of-magnitude only) --------
def bench(fn, n):
    fn()                       # warm
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n * 1e6      # us per call


def test_idle_redraw_budget(lcd):
    """Idle redraws happen ~12x/second forever. Was 4989us before the fix,
    now ~3us. A 200us ceiling still leaves ~60x headroom."""
    lcd.set_battery("87")
    frame = items()
    per_us = bench(lambda: lcd.render_list(frame, cursor=0, title="MyPB"), 500)
    assert per_us < 200, f"idle redraw costs {per_us:.0f}us"


def test_full_redraw_budget(lcd):
    """A frame that genuinely changed. Was ~5000us before the fix, ~1300us
    now. 15000us catches a real regression without policing small changes."""
    lcd.set_battery("87")
    state = {"i": 0}

    def render():
        state["i"] += 1
        lcd.render_list(items(f"v{state['i']}"), cursor=0, title="MyPB")

    per_us = bench(render, 200)
    assert per_us < 15000, f"full redraw costs {per_us:.0f}us"


def test_fit_long_text_budget(lcd):
    """Was 14484us per call with the per-character search."""
    per_us = bench(lambda: lcd._fit_text(LONG, 60), 200)
    assert per_us < 500, f"_fit_text costs {per_us:.0f}us"
