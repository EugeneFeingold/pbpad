"""Boot splash: blit a pre-baked SH1106 framebuffer over raw i2c.

Runs from pbpad-splash.service as early as possible in boot. To stay fast it
imports no Pillow/luma (which cost ~8s on a Pi Zero) -- only smbus2. The image
is pre-rendered by gen_splash.py into splash.fb (128x64 -> 1024 bytes of
SH1106 page-format data); we send the panel's init sequence, blit those bytes,
then turn the display on so the image appears without a flash of garbage RAM.

The init sequence and per-page addressing below were captured from luma.oled's
sh1106 driver (the same one the app uses) so the panel is configured
identically. Regenerate the message with:  python gen_splash.py "text"
"""
import os
import sys
import time

I2C_BUS = 1
I2C_ADDR = 0x3C          # config.OLED_I2C_ADDRESS
WIDTH, HEIGHT = 128, 64
PAGES = HEIGHT // 8
COL_LOW, COL_HIGH = 0x02, 0x10   # SH1106's visible area starts at column 2

# SH1106 config, captured verbatim from luma.oled's sh1106 (128x64). Display is
# left OFF (0xAE) until after the framebuffer is written; ends at contrast.
INIT = [
    0xAE, 0x20, 0x10, 0xB0, 0xC8, 0x00, 0x10, 0x40, 0xA1, 0xA6,
    0xA8, 0x3F, 0xA4, 0xD3, 0x00, 0xD5, 0xF0, 0xD9, 0x22, 0xDA,
    0x12, 0xDB, 0x20, 0x8D, 0x14, 0x81, 0x7F,
]


def _log(msg):
    print(f"[splash] {msg}", file=sys.stderr, flush=True)


def _cmd(bus, *cmds):
    bus.write_i2c_block_data(I2C_ADDR, 0x00, list(cmds))  # control 0x00 = command


def _blit(bus, data):
    for i in range(0, len(data), 32):  # SMBus block writes cap at 32 bytes
        bus.write_i2c_block_data(I2C_ADDR, 0x40, list(data[i:i + 32]))  # 0x40 = data


def _main():
    from smbus2 import SMBus

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "splash.fb")
    with open(path, "rb") as f:
        fb = f.read()
    if len(fb) != WIDTH * PAGES:
        _log(f"unexpected framebuffer size {len(fb)} (want {WIDTH * PAGES}); skipping")
        return

    # Retry opening the bus: this early in boot /dev/i2c-1 may not exist yet
    # (the i2c-dev module and its udev node can lag the service start). Wait up
    # to ~15s, exiting the loop the instant the bus opens.
    bus = None
    waited = 0.0
    for _ in range(30):
        try:
            bus = SMBus(I2C_BUS)
            break
        except Exception:
            time.sleep(0.5)
            waited += 0.5
    if bus is None:
        _log(f"i2c bus never became available after {waited:.0f}s; skipping")
        return
    if waited:
        _log(f"i2c bus ready after {waited:.0f}s")

    _cmd(bus, *INIT)
    time.sleep(0.1)  # let the charge pump settle before writing
    for page in range(PAGES):
        _cmd(bus, 0xB0 + page, COL_LOW, COL_HIGH)
        _blit(bus, fb[page * WIDTH:(page + 1) * WIDTH])
    _cmd(bus, 0xAF)  # display on -> reveal the freshly written image
    _log("painted splash")


try:
    _main()
except Exception:
    import traceback
    _log("splash failed:\n" + traceback.format_exc())

# Leave the panel showing the splash; the app paints over it later.
os._exit(0)
