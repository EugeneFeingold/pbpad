"""Tests for hardware/lcd.py — text measurement and pixel rendering.

The LCD drives a fake OLED but renders with real PIL, so these assert on
actual pixel output where it matters (e.g. the value/drill overlap bug).
"""
import pytest
from PIL import Image, ImageDraw


def rightmost_lit(img, x0=0, x1=128, y0=0, y1=64):
    px = img.load()
    r = -1
    for x in range(x0, x1):
        for y in range(y0, y1):
            if px[x, y]:
                r = x
                break
    return r


# --- text measurement ------------------------------------------------------
def test_ink_right_empty_is_zero(lcd):
    assert lcd._ink_right("") == 0


def test_ink_right_grows_with_length(lcd):
    assert lcd._ink_right("W") < lcd._ink_right("WW") < lcd._ink_right("WWW")


def test_ink_right_measures_actual_ink(lcd):
    # A 'w' overhangs its advance; _ink_right must catch that (the header
    # overlap bug). It should be >= the naive advance width for wide glyphs.
    assert lcd._ink_right("w") >= 1


def test_fit_text_keeps_short_unchanged(lcd):
    assert lcd._fit_text("Hi", 100) == "Hi"


def test_fit_text_zero_budget(lcd):
    assert lcd._fit_text("Hi", 0) == ""


def test_fit_tail_keeps_the_end(lcd):
    # Entry fields scroll to follow the caret, so they keep the RIGHT side.
    text = "10.0.0.123456789"
    tail = lcd.fit_tail(text, 40)
    assert text.endswith(tail)
    assert tail != text                    # something was dropped
    assert lcd._ink_right(tail) <= 40


def test_fit_tail_short_unchanged(lcd):
    assert lcd.fit_tail("10.0", 100) == "10.0"


def test_fit_tail_zero_budget(lcd):
    assert lcd.fit_tail("anything", 0) == ""


def test_fit_tail_caret_always_visible(lcd):
    # As the field grows past the edge, the trailing caret must stay on screen.
    field = "supersecretpassword" + "_"
    shown = lcd.fit_tail(field, lcd.body_width)
    assert shown.endswith("_")


def test_body_width_matches_message_budget(lcd):
    from hardware.lcd import _PAD_L
    assert lcd.body_width == 128 - 2 * _PAD_L


def test_fit_text_truncates_to_width(lcd):
    fit = lcd._fit_text("A very long value indeed", 40)
    assert lcd._ink_right(fit) <= 40
    assert fit.endswith(".")   # truncation indicator


# --- backlight -------------------------------------------------------------
def test_backlight_zero_hides(lcd):
    lcd.set_backlight(0)
    assert lcd._device.hidden


def test_backlight_nonzero_shows_and_sets_contrast(lcd):
    lcd.set_backlight(0)
    lcd.set_backlight(9)
    assert not lcd._device.hidden
    assert lcd._device.contrast_val == int(9 / 9 * 255)


# --- rendering doesn't crash + produces output -----------------------------
def test_render_message_paints(lcd):
    lcd.render_message("Hello", "World")
    assert lcd._device.last_image is not None
    assert rightmost_lit(lcd._device.last_image) > 0  # something drawn


def test_render_message_no_footer_by_default(lcd):
    # No separator line spanning the full width near the bottom.
    lcd.render_message("Starting...", "please wait")
    assert lcd._device.last_image is not None


def test_render_list_paints(lcd):
    items = [{"label": "Pattern", "value": "Sparkle", "arrows_left": True,
              "arrows_right": True, "drill": False, "divider": False,
              "mark": False, "fixed_arrows": True, "big": False}]
    lcd.render_list(items, cursor=0, title="PB")
    assert lcd._device.last_image is not None


def test_flush_skipped_when_unchanged(lcd):
    lcd.render_message("Same", "Frame")
    first = lcd._device.last_image
    lcd._device.last_image = None
    lcd.render_message("Same", "Frame")   # identical -> flush skipped
    assert lcd._device.last_image is None
    # sanity: a different frame does flush
    lcd.render_message("Different", "Frame")
    assert lcd._device.last_image is not None


# --- drill row value must not overlap the >> glyph (the reported bug) -------
@pytest.mark.parametrize("value", [
    "PixelBlaze-01", "MyLongPBName", "OurHouseNetwork", "wwwwwwwwwwww",
])
def test_drill_value_never_overlaps_glyph(lcd, value):
    # Render an INACTIVE drill row directly: ink is white(1) on black(0),
    # so any lit pixel in the gap between value and glyph is a real overlap.
    img = Image.new("1", (128, 64))
    draw = ImageDraw.Draw(img)
    item = {"label": "Device", "value": value, "drill": True, "mark": False,
            "arrows_left": False, "arrows_right": False,
            "divider": False, "big": False}
    lcd._draw_list_row(draw, item, active=False, y=0)
    px = img.load()
    band = lcd._font_h + 1
    # Value ink must stop before col 116; the drill glyph starts at col 120.
    # Columns 116-119 are the gap and must be clear of value ink.
    for x in range(116, 120):
        for y in range(band):
            assert not px[x, y], f"value {value!r} bled into drill gap at col {x}"


# --- footer button captions ------------------------------------------------
def footer_band(lcd):
    """(image, y-range) of the footer strip on the last rendered frame."""
    img = lcd._device.last_image
    top = 64 - lcd._font_h - 1
    return img, range(top, 64)


def lit_columns(img, rows):
    px = img.load()
    return {x for x in range(128) for y in rows if px[x, y]}


def test_footer_uses_custom_enter_label(lcd):
    lcd.render_message("A", "B", footer=(False, True, False, False, True),
                       enter_label="[Reset WiFi]")
    img, rows = footer_band(lcd)
    assert lit_columns(img, rows)          # something drawn


def test_custom_enter_label_does_not_collide_with_back(lcd):
    # "[Reset WiFi]" is much wider than "[Enter]" — make sure it still clears
    # the right-aligned "[Back]" caption on the 128px footer.
    lcd.render_message("A", "B", footer=(False, True, False, False, True),
                       enter_label="[Reset WiFi]")
    left_w = lcd._tw(lcd._font, "[Reset WiFi]")
    back_w = lcd._tw(lcd._font, "[Back]")
    left_end = 1 + 8 + left_w          # x start (1) + arrow slot (8) + label
    back_start = (128 - 6) - 3 - back_w
    assert left_end < back_start, (
        f"'[Reset WiFi]' ends at {left_end} but '[Back]' starts at {back_start}"
    )


def test_footer_defaults_unchanged(lcd):
    # Screens that don't pass labels still get the generic captions.
    lcd.render_message("A", "B", footer=(True, True, True, True, True))
    assert lcd._device.last_image is not None


def test_checkmark_and_arrows_render(lcd):
    # drill row with a mark (currently-connected device)
    items = [{"label": "MyPB", "value": "", "drill": True, "mark": True,
              "arrows_left": False, "arrows_right": False,
              "divider": False, "big": False}]
    lcd.render_list(items, cursor=0, title="PixelBlaze")
    assert lcd._device.last_image is not None


def test_close_cleans_device(lcd):
    dev = lcd._device
    lcd.close()
    assert dev.cleaned


# --- architectural guard ---------------------------------------------------
def test_all_text_goes_through_the_single_primitive():
    """Every string drawn on the OLED must go through LCD._draw_text, which
    requires a pixel budget and truncates to it.

    This is what makes overflow structurally impossible instead of something
    each call site has to remember. A long device name once ran through the
    >> glyph because _draw_list_row drew its label with a bare draw.text();
    the value on the same row had been fixed, the label had not. If you need
    to draw text, add a _draw_text call — do not reach for draw.text()."""
    import inspect
    from hardware import lcd as lcd_mod, lcd_text, lcd_widgets

    # The LCD is split across these modules; scan them all so the invariant
    # can't be dodged by adding a bare draw.text() in one of the split files.
    src = "\n".join(inspect.getsource(m) for m in (lcd_mod, lcd_text, lcd_widgets))
    allowed = inspect.getsource(lcd_mod.LCD._draw_text)
    outside = src.replace(allowed, "")
    # Strip comments/docstrings mentioning it, then look for real calls.
    code_lines = [ln for ln in outside.splitlines()
                  if "draw.text(" in ln and not ln.strip().startswith("#")]
    assert not code_lines, (
        "draw.text() called outside LCD._draw_text:\n  " + "\n  ".join(code_lines))


# --- overflow: nothing may reach the trailing glyph zone --------------------
LONG = "Gene's Extremely Long PixelBlaze Name"


def row_ink_columns(lcd, item, active=False):
    """Columns lit by a single rendered row (ink is white on black)."""
    img = Image.new("1", (128, 64))
    draw = ImageDraw.Draw(img)
    lcd._draw_list_row(draw, item, active=active, y=0)
    px = img.load()
    return {x for x in range(128) for y in range(lcd._font_h + 1) if px[x, y]}


def base_item(**kw):
    item = {"label": "", "value": "", "drill": False, "mark": False,
            "arrows_left": False, "arrows_right": False,
            "divider": False, "big": False, "fixed_arrows": False}
    item.update(kw)
    return item


@pytest.mark.parametrize("item", [
    # The reported bug: long LABEL on a drill row (device picker, wifi list).
    base_item(label=LONG, drill=True),
    # Long label AND a mark (currently-connected device).
    base_item(label=LONG, drill=True, mark=True),
    # Long label with a value too.
    base_item(label=LONG, value="Some Value", drill=True),
    # Long label on a turnable row.
    base_item(label=LONG, value="On", arrows_left=True, arrows_right=True),
    # Long label, no glyph at all.
    base_item(label=LONG),
    # Long value with a short label (the previously-fixed case).
    base_item(label="Device", value=LONG, drill=True),
    # Bold row (faux-bold overprints 1px wider).
    base_item(label=LONG, value="x", arrows_right=True),
])
def test_row_text_never_reaches_glyph_zone(lcd, item):
    lit = row_ink_columns(lcd, item)
    assert lit, "row drew nothing at all"
    has_glyph = item["drill"] or item["arrows_left"] or item["arrows_right"]
    if has_glyph:
        # The >> / triangle owns the last 8px; the 4px before it is the gap
        # that text must never enter. (The glyph itself lights 120+.)
        forbidden = range(128 - 8 - 4, 128 - 8)
    else:
        # No glyph: text still has to respect the right margin.
        forbidden = range(128 - 5, 128)
    intruders = sorted(x for x in lit if x in forbidden)
    assert not intruders, f"text reached columns {intruders} (forbidden {forbidden})"


def test_long_label_is_truncated_not_clipped(lcd):
    # Truncation should be visible (ends with '.') rather than silently cut.
    label, lw, value, vb = lcd._split_row(LONG, "", 8 + 4, 3, False)
    assert label.endswith(".")
    assert lcd._ink_right(label) <= 128 - 12 - 2


def test_label_and_value_share_space(lcd):
    # Neither element may starve the other completely.
    label, lw, value, vb = lcd._split_row(LONG, LONG, 8 + 4, 3, False)
    assert lw > 0 and vb > 0


@pytest.mark.parametrize("fixed", [False, True])
def test_row_value_budget_matches_renderer(lcd, fixed):
    # The budget screens use to pre-size a value must agree with what the
    # renderer actually allows, or a scrolling name drifts out of sync.
    budget = lcd.row_value_budget("Pattern", arrows=True, fixed_arrows=fixed)
    _, _, _, actual = lcd._split_row(
        "Pattern", "x", 8 + 4, lcd._mid_gap(arrows=True, fixed_arrows=fixed), False)
    assert budget == actual


def test_long_title_does_not_reach_battery(lcd):
    lcd.set_battery("100")
    lcd.render_message("body", "text", title="A Very Long Device Title Here")
    px = lcd._device.last_image.load()
    edge = lcd._battery_left_edge("100")
    # No title ink in the gap immediately left of the badge.
    for x in range(edge - 3, edge):
        assert not any(px[x, y] for y in range(lcd._font_h)), \
            f"title ink at column {x}, badge starts at {edge}"


def footer_columns(lcd, **flags):
    """Columns lit by the footer alone, drawn on a blank image."""
    img = Image.new("1", (128, 64))
    draw = ImageDraw.Draw(img)
    lcd._draw_footer(draw, 0, **flags)
    px = img.load()
    return {x for x in range(128) for y in range(lcd._font_h + 2) if px[x, y]}


@pytest.mark.parametrize("hidden", ["can_enter", "can_back", "both"])
def test_footer_arrows_do_not_move_when_captions_hide(lcd, hidden):
    """Screens toggle the captions as the cursor moves between rows. Space for
    them must stay reserved, or the arrows jump sideways ("<  >" one moment,
    "< [Back] >" the next). Arrow columns with captions hidden must be a
    subset of the columns lit when they're shown — if an arrow moved, it would
    light a column that the full footer doesn't."""
    full = dict(can_scroll=True, can_enter=True,
                can_adjust_left=True, can_adjust_right=True, can_back=True)
    partial = dict(full)
    if hidden == "both":
        partial["can_enter"] = partial["can_back"] = False
    else:
        partial[hidden] = False

    with_caps = footer_columns(lcd, **full)
    without = footer_columns(lcd, **partial)
    moved = sorted(without - with_caps)
    assert not moved, f"arrows shifted into columns {moved} when {hidden} hidden"


def test_footer_arrows_stable_with_custom_caption(lcd):
    # Same guarantee when a screen overrides the caption text.
    base = dict(can_scroll=False, can_enter=True, can_adjust_left=False,
                can_adjust_right=False, can_back=True,
                enter_label="[Reset WiFi]")
    shown = footer_columns(lcd, **base)
    hidden = footer_columns(lcd, **{**base, "can_enter": False})
    assert not (hidden - shown)


def test_pinned_arrow_does_not_overlap_value(lcd):
    """On a fixed_arrows row the left triangle sits in the gap between label
    and value. The gap must reserve pad + arrow + gap, or the right-aligned
    value is allowed to start on top of the arrow — which clipped the first
    few pixels of long pattern names."""
    from hardware.lcd import _PAD_L, _ARROW_PAD, _ARROW_W, _GLYPH_W, _GLYPH_GAP

    label = "Pattern"
    lw = lcd._ink_right(label)
    mid = lcd._mid_gap(arrows=True, fixed_arrows=True)
    _, _, _, budget = lcd._split_row(label, "x", _GLYPH_W + _GLYPH_GAP, mid, False)

    text_right = 128 - (_GLYPH_W + _GLYPH_GAP)
    earliest_value = text_right - budget            # value is right-aligned
    arrow_last_col = _PAD_L + lw + _ARROW_PAD + _ARROW_W - 1
    assert earliest_value > arrow_last_col, (
        f"value may start at {earliest_value} but the arrow ends at {arrow_last_col}")


def test_pinned_arrow_clears_the_label(lcd):
    # The other side of the same gap: the arrow must not touch the label.
    from hardware.lcd import _PAD_L, _ARROW_PAD
    lw = lcd._ink_right("Pattern")
    assert _PAD_L + lw + _ARROW_PAD > _PAD_L + lw + 2


def test_row_value_budget_accounts_for_pinned_arrow(lcd):
    # A screen pre-sizing a scrolling name must get the narrower budget, or it
    # scrolls against a slot wider than the renderer will actually allow.
    pinned = lcd.row_value_budget("Pattern", arrows=True, fixed_arrows=True)
    floating = lcd.row_value_budget("Pattern", arrows=True)
    assert pinned < floating


# --- long messages wrap instead of being cut off ---------------------------
def test_wrap_keeps_every_word(lcd):
    text = "hold both knobs to unlock"
    lines = lcd._wrap_text(text, 124)
    assert len(lines) > 1, "text that doesn't fit should wrap"
    assert " ".join(lines).split() == text.split(), "wrapping lost a word"


def test_wrap_lines_each_fit(lcd):
    for line in lcd._wrap_text("hold both knobs to unlock", 124):
        assert lcd._ink_right(line) <= 124


def test_wrap_single_line_when_it_fits(lcd):
    assert lcd._wrap_text("Locked", 124) == ["Locked"]


def test_wrap_hard_truncates_an_overlong_word(lcd):
    lines = lcd._wrap_text("Supercalifragilisticexpialidocious" * 2, 60)
    assert all(lcd._ink_right(ln) <= 60 for ln in lines)


def test_lock_screen_shows_the_whole_hint(lcd):
    from ui.screens import LockScreen
    screen = LockScreen()
    screen.hint = True
    screen.render(lcd)
    # "unlock" must survive — it used to be cut off the right edge.
    lines = lcd._wrap_text("hold both knobs to unlock", 128 - 4)
    assert "unlock" in " ".join(lines)


def test_message_layout_unchanged_for_short_text(lcd):
    # Wrapping must not shift the common 1- and 2-line cases.
    lcd.render_message("Locked", "")
    one = lcd._device.last_image.tobytes()
    lcd.render_message("Connected!", "MyPB")
    two = lcd._device.last_image.tobytes()
    assert one != two and one and two   # both rendered, no exception


def test_long_footer_captions_do_not_collide(lcd):
    lcd.render_message("a", "b", footer=(True, True, True, True, True),
                       enter_label="[An Absurdly Long Caption]",
                       back_label="[Back]")
    assert lcd._device.last_image is not None


def test_message_body_does_not_accumulate_across_renders(lcd, monkeypatch):
    """Re-rendering with the same line1 but a new line2 must show ONLY the new
    body — not stack it under the previous one.

    Regression: render_message did `lines = wrap(line1); lines += wrap(line2)`,
    and wrap() returns a MEMOISED list, so `+=` grew line1's cache by line2 on
    every call. A spinner (same line1, changing line2) then stacked every past
    frame's body — the doubled "Please wait…" and a prior network name bleeding
    onto the next "Joining WiFi" screen.
    """
    drawn = []
    orig = lcd._draw_text

    def rec(draw, text, *a, **k):
        drawn.append(text)
        return orig(draw, text, *a, **k)

    monkeypatch.setattr(lcd, "_draw_text", rec)
    lcd.render_message("Joining WiFi", "Network A")
    drawn.clear()
    lcd.render_message("Joining WiFi", "Network B")   # same line1, new line2
    assert "Network A" not in drawn                    # no stale body line
    assert drawn.count("Joining WiFi") == 1            # line1 drawn once, not stacked
    assert "Network B" in drawn
