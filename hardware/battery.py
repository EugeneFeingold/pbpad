import asyncio
from typing import Optional

import config
import log

try:
    from smbus2 import SMBus, i2c_msg
    _HAVE_SMBUS = True
except ImportError:
    _HAVE_SMBUS = False

# LC709203F registers
_REG_INITIAL_RSOC = 0x07
_REG_CELL_VOLTAGE = 0x09
_REG_APA = 0x0B
_REG_RSOC = 0x0D
_REG_IC_VERSION = 0x11
_REG_POWER_MODE = 0x15

_POWER_MODE_OPERATIONAL = 0x0001
_INITIAL_RSOC_MAGIC = 0xAA55  # re-seeds RSOC from the current cell voltage


def _crc8(data) -> int:
    """CRC-8 (poly 0x07, init 0x00) as used by the LC709203F."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


class Battery:
    """Adafruit LC709203F fuel gauge on the bit-banged i2c-gpio bus.

    Degrades gracefully: if smbus2, the bus, or the chip is missing, the gauge
    is marked unavailable and read_percent() returns None, so the app runs fine
    without the sensor wired (or before the i2c-gpio overlay is added).
    """

    def __init__(self):
        self._bus = None
        self._addr = config.BATTERY_I2C_ADDRESS
        if not _HAVE_SMBUS:
            log.log(log.INFO, "battery: smbus2 not installed; gauge disabled")
            return
        try:
            self._bus = SMBus(config.BATTERY_I2C_BUS)
            self._read_reg(_REG_IC_VERSION)  # probe presence (raises if absent)
            self._init_chip()
            log.log(log.CHANGE, "battery: LC709203F ready")
        except Exception as e:
            log.log(log.INFO, f"battery: gauge unavailable ({e})")
            self._close_bus()

    @property
    def available(self) -> bool:
        return self._bus is not None

    def _close_bus(self):
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None

    def _write_reg(self, reg: int, value: int):
        low = value & 0xFF
        high = (value >> 8) & 0xFF
        crc = _crc8([self._addr << 1, reg, low, high])
        self._bus.i2c_rdwr(i2c_msg.write(self._addr, [reg, low, high, crc]))

    def _read_reg(self, reg: int) -> int:
        write = i2c_msg.write(self._addr, [reg])
        read = i2c_msg.read(self._addr, 3)
        self._bus.i2c_rdwr(write, read)  # repeated-start read
        low, high, crc = list(read)
        expected = _crc8([self._addr << 1, reg, (self._addr << 1) | 1, low, high])
        if crc != expected:
            raise IOError(f"CRC mismatch on reg {reg:#04x}")
        return low | (high << 8)

    def _init_chip(self):
        self._write_reg(_REG_POWER_MODE, _POWER_MODE_OPERATIONAL)
        self._write_reg(_REG_APA, config.BATTERY_APA)

    def _read_percent_sync(self) -> Optional[int]:
        if self._bus is None:
            return None
        try:
            return max(0, min(100, self._read_reg(_REG_RSOC)))
        except Exception as e:
            log.log(log.ERROR, f"battery read failed: {e}")
            return None

    def _read_millivolts_sync(self) -> Optional[int]:
        if self._bus is None:
            return None
        try:
            return self._read_reg(_REG_CELL_VOLTAGE)
        except Exception as e:
            log.log(log.ERROR, f"battery voltage read failed: {e}")
            return None

    async def read_percent(self) -> Optional[int]:
        """State of charge, 0-100, or None if the gauge is unavailable.

        The bit-banged read is blocking, so it runs in a worker thread to keep
        the event loop responsive.
        """
        if self._bus is None:
            return None
        return await asyncio.to_thread(self._read_percent_sync)

    async def read_millivolts(self) -> Optional[int]:
        """Cell voltage in mV, or None if the gauge is unavailable."""
        if self._bus is None:
            return None
        return await asyncio.to_thread(self._read_millivolts_sync)

    def close(self):
        self._close_bus()
