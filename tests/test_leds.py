"""Tests for hardware/leds.py — the WS2812-over-SPI driver."""
import pytest

from hardware import leds
from hardware.leds import LEDStrip, _BIT0, _BIT1, _RESET_BYTES


def last_write(strip):
    return strip._spi.written[-1]


def test_ok_when_spidev_present():
    strip = LEDStrip(count=3)
    assert strip.ok
    assert strip.count == 3
    assert strip._spi.max_speed_hz == leds._SPI_HZ


def test_disabled_when_spidev_missing(monkeypatch):
    monkeypatch.setattr(leds, "spidev", None)
    strip = LEDStrip(count=3)
    assert not strip.ok
    strip.show(bytes([255, 0, 0]))  # no crash, no-op


def test_disabled_when_open_raises(monkeypatch):
    class Boom:
        class SpiDev:
            def open(self, *a):
                raise OSError("no spi")
    monkeypatch.setattr(leds, "spidev", Boom)
    strip = LEDStrip(count=1)
    assert not strip.ok


def test_frame_length():
    strip = LEDStrip(count=3)
    strip.show(bytes([1, 2, 3] * 3), 1.0)
    assert len(last_write(strip)) == 3 * 24 + _RESET_BYTES


def test_grb_encoding_full_brightness():
    strip = LEDStrip(count=3)
    # pixel0 red, pixel1 green, pixel2 blue
    strip.show(bytes([255, 0, 0, 0, 255, 0, 0, 0, 255]), 1.0)
    d = last_write(strip)
    # WS2812 wire order is G, R, B (8 SPI bytes each).
    assert d[0:8] == bytes([_BIT0] * 8)    # red: G=0
    assert d[8:16] == bytes([_BIT1] * 8)   # red: R=255
    assert d[16:24] == bytes([_BIT0] * 8)  # red: B=0
    assert d[24:32] == bytes([_BIT1] * 8)  # green: G=255
    assert d[48:72] == bytes([_BIT0] * 8 + [_BIT0] * 8 + [_BIT1] * 8)  # blue


def test_reset_trailer_is_zero():
    strip = LEDStrip(count=1)
    strip.show(bytes([255, 255, 255]), 1.0)
    assert last_write(strip)[24:] == bytes(_RESET_BYTES)


def test_brightness_scales():
    strip = LEDStrip(count=1)
    strip.show(bytes([255, 255, 255]), 0.5)
    d = last_write(strip)
    # 127 = 0b01111111 -> BIT0 then seven BIT1
    assert d[0:8] == bytes([_BIT0, _BIT1, _BIT1, _BIT1, _BIT1, _BIT1, _BIT1, _BIT1])


def test_short_rgb_pads_black():
    strip = LEDStrip(count=3)
    strip.show(bytes([255, 255, 255]), 1.0)  # only 1 pixel of data
    d = last_write(strip)
    # pixels 1 and 2 are black
    assert d[24:72] == bytes([_BIT0] * 48)


def test_off_blanks():
    strip = LEDStrip(count=2)
    strip.off()
    d = last_write(strip)
    assert d[:48] == bytes([_BIT0] * 48)


def test_close_blanks_and_closes():
    strip = LEDStrip(count=2)
    spi = strip._spi
    strip.close()
    assert spi.closed
    assert strip._spi is None
