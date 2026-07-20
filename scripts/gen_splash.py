#!/usr/bin/env python3
"""Bake a text message into a raw SH1106 framebuffer for the boot splash.

The boot splash (splash.py) must paint the OLED as early as possible in boot,
so it can't afford to import Pillow/luma (~8s on a Pi Zero). Instead we
pre-render the message here, on a dev machine, into the exact 1-bit page-format
bytes the SH1106 expects (128x64 -> 1024 bytes), and splash.py blits those
bytes over raw i2c with no heavy imports.

Byte layout matches luma.oled's sh1106 driver: 8 pages of 128 columns; within a
page column, bit i is the pixel one row down (LSB = top), so the app and the
splash render identically.

Usage:
    python scripts/gen_splash.py                      # default "Starting..." -> splash.fb
    python scripts/gen_splash.py "Booting"            # custom text
    python scripts/gen_splash.py "Top\nBottom"        # \n stacks lines, centered
    python scripts/gen_splash.py "Hi" -o other.fb     # custom output file

Run it whenever you want to change the splash text, then deploy splash.fb.
"""
import argparse
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 128, 64          # SH1106 visible area
PAGES = HEIGHT // 8


def _tw(font, s: str) -> int:
    try:
        return int(font.getlength(s))
    except AttributeError:
        return font.getsize(s)[0]


def render(text: str) -> Image.Image:
    img = Image.new("1", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()   # same 6px font the app UI uses
    lines = text.split("\n")
    try:
        line_h = font.getbbox("Xy")[3]
    except AttributeError:
        line_h = font.getsize("Xy")[1]
    y = max(0, (HEIGHT - line_h * len(lines)) // 2)
    for line in lines:
        x = max(0, (WIDTH - _tw(font, line)) // 2)
        draw.text((x, y), line, font=font, fill=1)
        y += line_h
    return img


def pack(img: Image.Image) -> bytes:
    """Pack a 1-bit 128x64 image into SH1106 page format (matches luma)."""
    px = img.load()
    fb = bytearray(WIDTH * PAGES)
    for page in range(PAGES):
        for x in range(WIDTH):
            byte = 0
            for bit in range(8):
                if px[x, page * 8 + bit]:
                    byte |= 1 << bit
            fb[page * WIDTH + x] = byte
    return bytes(fb)


def main():
    ap = argparse.ArgumentParser(description="Bake splash text to a raw SH1106 framebuffer.")
    ap.add_argument("text", nargs="?", default="Starting...",
                    help=r'message to render (use \n to stack lines); default "Starting..."')
    ap.add_argument("-o", "--out", default="splash.fb",
                    help="output framebuffer file (default splash.fb)")
    args = ap.parse_args()
    text = args.text.replace("\\n", "\n")
    fb = pack(render(text))
    with open(args.out, "wb") as f:
        f.write(fb)
    print(f"wrote {len(fb)} bytes to {args.out}  ({args.text!r})")


if __name__ == "__main__":
    main()
