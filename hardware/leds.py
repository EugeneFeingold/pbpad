"""WS2812/WS2812B driver over SPI (MOSI = GPIO10, physical pin 19).

Each WS2812 data bit is one SPI byte at ~6.4 MHz — one SPI bit ~156 ns, so:
    '0' = 0xC0  (2 high ~312 ns,  6 low ~938 ns) — matches T0H/T0L spec
    '1' = 0xF8  (5 high ~781 ns,  3 low ~469 ns) — matches T1H/T1L spec
A normal SPI byte stream produces valid WS2812 waveforms with no DMA, no
special peripheral, and no root — a `pi`-group SPI device is enough.

Wire order is GRB (standard WS2812); this module takes RGB in and swaps.

One-time Pi setup: `sudo raspi-config nonint do_spi 0` and reboot; the pi
user must be in the `spi` group (setup.sh handles both).
"""
import log

try:
    import spidev
except ImportError:
    spidev = None

_BIT0 = 0xC0
_BIT1 = 0xF8
_RESET_BYTES = 50            # >50 µs of low latches the frame; ~62 µs at 6.4 MHz
_SPI_HZ = 6_400_000
_SPI_MODE = 0

# Precomputed byte-to-SPI-pattern table: index `b` gives the 8 SPI bytes that
# encode the WS2812 waveform for the 8 bits of `b`. Built once at import so
# the hot path is 8 slice-assigns per pixel instead of 8 Python bit-shifts.
_ENC_TABLE = tuple(
    bytes(_BIT1 if (b >> bit) & 1 else _BIT0 for bit in range(7, -1, -1))
    for b in range(256)
)


class LEDStrip:
    """Small WS2812 driver: `show(rgb, brightness)`. If spidev is missing or
    the SPI device can't be opened the strip is silently disabled — safe on
    a dev machine or a Pi where SPI hasn't been enabled yet."""

    def __init__(self, count: int, bus: int = 0, device: int = 0):
        self._count = count
        self._spi = None
        self._write = None
        if spidev is None:
            log.log(log.ERROR, "spidev unavailable; LED strip disabled")
            return
        try:
            self._spi = spidev.SpiDev()
            self._spi.open(bus, device)
            self._spi.max_speed_hz = _SPI_HZ
            self._spi.mode = _SPI_MODE
        except Exception as e:
            log.log(log.ERROR, f"SPI open failed: {e}")
            self._spi = None
            return
        # Bind the fastest transfer method available: writebytes2 takes a
        # bytes-like directly (no per-byte Python conversion); xfer3/xfer2
        # still work but require a list of ints, which on Pi Zero adds real
        # allocation cost per frame.
        if hasattr(self._spi, "writebytes2"):
            self._write = self._spi.writebytes2
        elif hasattr(self._spi, "xfer3"):
            self._write = lambda d: self._spi.xfer3(list(d))
        else:
            self._write = lambda d: self._spi.xfer2(list(d))

    @property
    def count(self) -> int:
        return self._count

    @property
    def ok(self) -> bool:
        return self._spi is not None

    def show(self, rgb: bytes, brightness: float = 1.0):
        """Push `count` pixels to the strip. `rgb` is bytes of R,G,B triples
        (any length — pads with black, truncates the rest). `brightness`
        scales 0.0..1.0."""
        if self._spi is None:
            return
        buf = bytearray(self._count * 24 + _RESET_BYTES)
        n = min(self._count * 3, len(rgb))
        # Scale into a bytes value up front so the hot loop is just table
        # lookups + slice assigns. Pure Python but tight.
        scaled = bytes(int(rgb[i] * brightness) for i in range(n))
        pos = 0
        table = _ENC_TABLE
        for i in range(self._count):
            base = i * 3
            r = scaled[base]     if base     < n else 0
            g = scaled[base + 1] if base + 1 < n else 0
            b = scaled[base + 2] if base + 2 < n else 0
            # WS2812 wire order: G, R, B, MSB first.
            buf[pos:pos + 8] = table[g]; pos += 8
            buf[pos:pos + 8] = table[r]; pos += 8
            buf[pos:pos + 8] = table[b]; pos += 8
        try:
            self._write(bytes(buf))
        except Exception as e:
            log.log(log.ERROR, f"SPI write failed: {e}")

    def off(self):
        """Blank the strip (all pixels black)."""
        self.show(b"", 0.0)

    def close(self):
        if self._spi is not None:
            try:
                self.off()
            except Exception:
                pass
            self._spi.close()
            self._spi = None
