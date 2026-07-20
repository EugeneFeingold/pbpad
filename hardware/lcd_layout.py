"""Pixel-layout constants and the 4x6 bitmap digit font for the OLED.

Kept in one place so the measurement code (lcd_text), the row/footer drawing
(lcd_widgets), and the layout-budget helpers can never disagree about how much
space an element occupies.
"""

# Row layout, in pixels.
_PAD_L = 2        # left margin for row text
_PAD_R = 5        # right margin on rows with no trailing glyph
_GAP = 3          # gap between label and value
_GLYPH_W = 8      # width of the trailing >> / triangle zone
_GLYPH_GAP = 4    # gap between text and that glyph
_ARROW_W = 5      # width of a nav triangle
_ARROW_PAD = 8    # space between a label and a pinned (fixed_arrows) triangle,
                  # so the arrow doesn't read as touching the label text

# Text measurement is expensive (renders + scans a scratch image), so results
# are memoised. The UI's working set is a few dozen strings; the bound just
# stops unbounded growth from e.g. a long list of SSIDs.
_MEASURE_CACHE_MAX = 512

# 4x6 pixel digit font — crisp on the 1-bit OLED, where small TrueType is mushy.
_DIGIT_W, _DIGIT_H, _DIGIT_GAP = 4, 6, 1
_DIGITS = {
    "0": (".11.", "1..1", "1..1", "1..1", "1..1", ".11."),
    "1": (".1..", "11..", ".1..", ".1..", ".1..", "111."),
    "2": (".11.", "1..1", "..1.", ".1..", "1...", "1111"),
    "3": ("111.", "...1", ".11.", "...1", "1..1", ".11."),
    "4": ("..1.", ".11.", "1.1.", "1111", "..1.", "..1."),
    "5": ("1111", "1...", "111.", "...1", "1..1", ".11."),
    "6": (".11.", "1...", "111.", "1..1", "1..1", ".11."),
    "7": ("1111", "...1", "..1.", "..1.", ".1..", ".1.."),
    "8": (".11.", "1..1", ".11.", "1..1", "1..1", ".11."),
    "9": (".11.", "1..1", "1..1", ".111", "...1", ".11."),
    "/": ("...1", "...1", "..1.", "..1.", ".1..", "1..."),
}
