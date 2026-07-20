"""Tests for hardware/battery.py — LC709203F over the (faked) smbus2 bus."""
import pytest

import hardware.battery as battery_mod
from hardware.battery import Battery, _crc8, _REG_RSOC, _REG_CELL_VOLTAGE
from tests._fakes import FakeSMBus


def test_crc8_known_vector():
    # CRC-8, poly 0x07, init 0x00. Standard check value for "123456789" is 0xF4.
    assert _crc8(b"123456789") == 0xF4


def test_crc8_empty():
    assert _crc8([]) == 0


def test_available_when_bus_ok():
    bat = Battery()
    assert bat.available


def test_unavailable_when_open_raises():
    FakeSMBus.raise_on_open = True
    bat = Battery()
    assert not bat.available


def test_unavailable_when_smbus_missing(monkeypatch):
    monkeypatch.setattr(battery_mod, "_HAVE_SMBUS", False)
    bat = Battery()
    assert not bat.available


def test_read_percent_returns_register():
    FakeSMBus.registers[_REG_RSOC] = 73
    bat = Battery()
    assert bat._read_percent_sync() == 73


def test_read_percent_clamps_high():
    FakeSMBus.registers[_REG_RSOC] = 250
    bat = Battery()
    assert bat._read_percent_sync() == 100


def test_read_millivolts_returns_register():
    FakeSMBus.registers[_REG_CELL_VOLTAGE] = 3854
    bat = Battery()
    assert bat._read_millivolts_sync() == 3854


def test_read_returns_none_when_unavailable():
    FakeSMBus.raise_on_open = True
    bat = Battery()
    assert bat._read_percent_sync() is None
    assert bat._read_millivolts_sync() is None


def test_crc_mismatch_raises_then_read_returns_none(monkeypatch):
    bat = Battery()

    # Corrupt the CRC coming back so _read_reg raises IOError internally.
    def bad_rdwr(*msgs):
        if len(msgs) == 2 and msgs[1].is_read:
            msgs[1].data = [0x00, 0x00, 0xFF]  # wrong crc
    monkeypatch.setattr(bat._bus, "i2c_rdwr", bad_rdwr)
    assert bat._read_percent_sync() is None  # error swallowed -> None


async def test_async_read_percent():
    FakeSMBus.registers[_REG_RSOC] = 42
    bat = Battery()
    assert await bat.read_percent() == 42


def test_close():
    bat = Battery()
    bus = bat._bus
    bat.close()
    assert bus.closed
    assert bat._bus is None
