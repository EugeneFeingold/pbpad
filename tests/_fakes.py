"""Fake hardware modules, injected into sys.modules by conftest.py before any
project module imports them. Lets the whole app be imported and exercised on a
dev machine with no Pi hardware (no luma/gpiozero/smbus2/spidev/pixelblaze).

PIL stays real — LCD rendering is tested against actual pixel output.
"""
import types


# --- CRC-8 (matches hardware.battery._crc8) for the smbus2 fake ------------
def crc8(data) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


# --- luma (OLED) -----------------------------------------------------------
class FakeSerial:
    def __init__(self, *a, **k):
        self.args, self.kwargs = a, k


class FakeOLED:
    def __init__(self, *a, **k):
        self.mode = "1"
        self.size = (128, 64)
        self.width, self.height = 128, 64
        self.last_image = None
        self.contrast_val = None
        self.hidden = False
        self.cleaned = False

    def display(self, img):
        self.last_image = img.copy()

    def contrast(self, v):
        self.contrast_val = v

    def hide(self):
        self.hidden = True

    def show(self):
        self.hidden = False

    def cleanup(self):
        self.cleaned = True


# --- gpiozero --------------------------------------------------------------
class FakeButton:
    instances = []

    def __init__(self, pin, pull_up=True, bounce_time=None, hold_time=None):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.hold_time = hold_time
        self.when_pressed = None
        self.when_held = None
        self.when_released = None
        self.is_pressed = False
        self.closed = False
        FakeButton.instances.append(self)


    def close(self):
        self.closed = True


class FakeRotaryEncoder:
    instances = []

    def __init__(self, a, b, max_steps=16, wrap=False):
        self.a, self.b = a, b
        self.when_rotated_clockwise = None
        self.when_rotated_counter_clockwise = None
        self.closed = False
        FakeRotaryEncoder.instances.append(self)

    def close(self):
        self.closed = True


class FakeDigitalOutputDevice:
    instances = []

    def __init__(self, pin, initial_value=False):
        self.pin = pin
        self.value = initial_value
        self.closed = False
        FakeDigitalOutputDevice.instances.append(self)

    def close(self):
        self.closed = True


# --- smbus2 ----------------------------------------------------------------
class FakeI2CMsg:
    def __init__(self, addr, data=None, length=None, is_read=False):
        self.addr = addr
        self.data = list(data) if data else []
        self.length = length
        self.is_read = is_read

    def __iter__(self):
        return iter(self.data)


class _i2c_msg:
    @staticmethod
    def write(addr, data):
        return FakeI2CMsg(addr, data=data, is_read=False)

    @staticmethod
    def read(addr, length):
        return FakeI2CMsg(addr, length=length, is_read=True)


class FakeSMBus:
    # Tests set FakeSMBus.registers[reg] = 16-bit value; a register read
    # returns [low, high, crc] with a valid CRC so Battery accepts it.
    registers = {}
    raise_on_open = False
    instances = []

    def __init__(self, bus):
        if FakeSMBus.raise_on_open:
            raise FileNotFoundError(f"[Errno 2] No such file: /dev/i2c-{bus}")
        self.bus = bus
        self.closed = False
        FakeSMBus.instances.append(self)

    def i2c_rdwr(self, *msgs):
        wr = msgs[0]
        reg = wr.data[0]
        if len(msgs) == 2 and msgs[1].is_read:
            val = FakeSMBus.registers.get(reg, 0)
            low, high = val & 0xFF, (val >> 8) & 0xFF
            crc = crc8([wr.addr << 1, reg, (wr.addr << 1) | 1, low, high])
            msgs[1].data = [low, high, crc]
        else:
            low, high = wr.data[1], wr.data[2]
            FakeSMBus.registers[reg] = low | (high << 8)

    def close(self):
        self.closed = True

    @classmethod
    def reset(cls):
        cls.registers = {}
        cls.raise_on_open = False
        cls.instances = []


# --- spidev ----------------------------------------------------------------
class FakeSpiDev:
    instances = []

    def __init__(self):
        self.opened = None
        self.max_speed_hz = 0
        self.mode = 0
        self.written = []
        self.closed = False
        FakeSpiDev.instances.append(self)

    def open(self, bus, device):
        self.opened = (bus, device)

    def writebytes2(self, data):
        self.written.append(bytes(data))

    def close(self):
        self.closed = True


# --- pixelblaze ------------------------------------------------------------
class FakePixelblaze:
    """Per-test configurable. Tests set class attrs or monkeypatch instances."""
    instances = []
    enumerate_result = []

    def __init__(self, ip):
        self.ip = ip
        self.closed = False
        self.preview_frames_enabled = False
        self.latestSequencer = None
        FakePixelblaze.instances.append(self)

    @classmethod
    def EnumerateDevices(cls, timeout=0):
        return list(cls.enumerate_result)

    def getPatternList(self):
        return {}

    def getConfigSettings(self):
        return {"brightness": 1.0}

    def getDeviceName(self):
        return "FakePB"

    def setSendPreviewFrames(self, on):
        self.preview_frames_enabled = on

    def getPreviewFrame(self):
        return b""

    def _close(self):
        self.closed = True

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.enumerate_result = []


def install(monkeypatch=None):
    """Register all fake modules in sys.modules. Called once from conftest at
    import time (monkeypatch=None) so it's in place before project imports."""
    import sys

    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    mods = {}
    mods["luma"] = mod("luma")
    mods["luma.core"] = mod("luma.core")
    mods["luma.core.interface"] = mod("luma.core.interface")
    mods["luma.core.interface.serial"] = mod("luma.core.interface.serial", i2c=FakeSerial)
    mods["luma.oled"] = mod("luma.oled")
    mods["luma.oled.device"] = mod("luma.oled.device", sh1106=FakeOLED)
    mods["gpiozero"] = mod(
        "gpiozero",
        Button=FakeButton,
        RotaryEncoder=FakeRotaryEncoder,
        DigitalOutputDevice=FakeDigitalOutputDevice,
    )
    mods["smbus2"] = mod("smbus2", SMBus=FakeSMBus, i2c_msg=_i2c_msg)
    mods["spidev"] = mod("spidev", SpiDev=FakeSpiDev)
    mods["pixelblaze"] = mod("pixelblaze", Pixelblaze=FakePixelblaze)

    for name, m in mods.items():
        sys.modules[name] = m
