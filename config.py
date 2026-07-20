# SH1106 OLED over I2C (SDA=GPIO2, SCL=GPIO3)
OLED_I2C_ADDRESS = 0x3C

# Rotary encoder pins (BCM) — assign to your wiring
# Encoder A: OK on press, primary navigation on rotate
ENC1_A  = 20
ENC1_B  = 21
ENC1_SW = 16

# Encoder B: Back on press, mode/secondary navigation on rotate
ENC2_A  = 19
ENC2_B  = 13
ENC2_SW = 26

# PixelBlaze discovery
PIXELBLAZE_DISCOVERY_PORT = 1889
PIXELBLAZE_WS_PORT = 81
DISCOVERY_TIMEOUT_SEC = 5

# All persisted settings live in one JSON file (see store.py): backlight, idle
# timeouts, the preferred-PixelBlaze list, etc.
SETTINGS_FILE = "/home/pi/.pbpad.json"

# Velocity-sensitive encoder threshold (seconds); events faster than this trigger the fast-step path
ENCODER_VELOCITY_THRESHOLD = 0.25

# Battery gauge: Adafruit LC709203F (product 4712), read over I2C.
# It requires I2C clock stretching, which the Pi's hardware I2C can't do
# reliably, so it lives on a *software* (bit-banged) i2c-gpio bus, separate
# from the OLED's hardware bus. Add this to /boot/config.txt (or
# /boot/firmware/config.txt on newer Raspberry Pi OS) and reboot
# (SDA=GPIO23 pin 16, SCL=GPIO24 pin 18):
#   dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24,i2c_gpio_delay_us=2
BATTERY_I2C_BUS = 3
BATTERY_I2C_ADDRESS = 0x0B
# Adjustment Pack Application: set per the LC709203F datasheet APA table for
# your cell. 0x44 is the ~5000mAh ballpark; verify/tune against the datasheet
# if the percentage looks off (APA affects gauge accuracy, not whether it reads).
BATTERY_APA = 0x44
BATTERY_POLL_SEC = 30

# Low-battery conservation mode: below LOW_BATTERY_PCT the app suspends the PB
# poll and the preview stream, and blinks a gauge on the LED strip showing
# (pct - LOW_BATTERY_FLOOR_PCT) red pixels every 500ms.
LOW_BATTERY_PCT = 10
LOW_BATTERY_FLOOR_PCT = 5

# Power button (BCM). Long-press triggers a clean OS shutdown.
# Power-ON is handled in hardware: wire the same button also to GPIO3
# (physical pin 5), the Pi's wake pin, which boots the board from halt.
POWER_BTN = 17
POWER_OFF_HOLD_SEC = 5.0
# Gate pin for the P-MOSFET that isolates the button-to-GPIO3 wake path while
# pbpad is running (so a held button doesn't pull SCL low and freeze the
# OLED during the shutdown prompt). Default LOW via external 100kΩ pull-down
# → MOSFET conducts → wake works when the Pi is halted; pbpad drives this
# HIGH at startup → MOSFET opens → button only reaches GPIO17.
POWER_GATE = 27

# Logging (0=off, 1=errors, 2=connections+changes, 3=transitions, 4=encoder, 5=network)
LOG_LEVEL = 2

# Backlight (1-9) and idle timeouts (seconds, None = never). Defaults; the live
# values are persisted in SETTINGS_FILE via store.py.
BACKLIGHT_LEVEL = 9
DIM_TIMEOUT_DEFAULT = 30
OFF_TIMEOUT_DEFAULT = 60

# Onboard WS2812 strip driven over SPI0 MOSI (GPIO10 / physical pin 19).
# Enable SPI on the Pi with `sudo raspi-config nonint do_spi 0` (and reboot);
# the `pi` user needs to be in the `spi` group (setup.sh handles both).
LED_COUNT = 8
# Which PB pixels feed each of our LED_COUNT physical LEDs. Each entry lists
# the PB pixel indices that get averaged into that output pixel.
# Current: 2-pixel averages covering the first 16 PB pixels.
# To try wider groups later:
#   [list(range(i, i+4)) for i in range(0, 32, 4)]   # 4-pixel avg over first 32
#   [list(range(i, i+8)) for i in range(0, 64, 8)]   # 8-pixel avg over first 64
# Or single-pixel sampling: [[i] for i in range(8)] or [[i*2] for i in range(8)]
LED_STRIP_GROUPS = [[i, i + 1] for i in range(0, 16, 2)]
# LED brightness setting (0-25); the value is the actual output percentage,
# so 25 means 25% (capped there for battery + eye comfort). Default persists
# in SETTINGS_FILE.
LED_BRIGHTNESS_DEFAULT = 5
# How many preview frames per second we PROCESS. Not a limit on what the PB
# sends — frames are pushed unsolicited, and reading them slower just queues
# them in the socket (which once put the preview seconds behind), so
# PreviewClient always drains every frame and simply drops the ones over this
# rate without processing them. This is a CPU budget for an unattended device:
# an uncapped preview saturated the Pi Zero and made input sluggish. Also used
# as the nominal inter-frame gap when re-basing a cached pattern buffer.
PB_PREVIEW_MAX_HZ = 15

# Cap how often the render loop pushes a frame to the strip. Higher = smoother
# interpolation between PB preview frames, at some CPU cost.
LED_MAX_FPS = 60

# Playback lag applied to LEDs — we buffer incoming preview frames and always
# render from (now - this) so arrival-time jitter doesn't cause playback
# stutter. At ~25 fps preview from PB, 0.4s ≈ 10 frames of buffer.
LED_PLAYBACK_DELAY_SEC = 0.4

# Max entries in the live frame buffer. The 1s age prune is the real bound;
# this is a safety cap. Sized to hold a burst-rate second (~60) plus a
# restored per-pattern buffer without evicting the restored frames early.
LED_FRAME_BUFFER_MAX = 120

# Remember recent preview frames per pattern, so switching back to a pattern
# we've already shown can replay it immediately while fresh frames stream in.
# A device typically has a few dozen patterns; each cached entry is at most
# LED_PLAYBACK_DELAY_SEC of picks (a few KB), so this cap is generous.
LED_PATTERN_CACHE_MAX = 64

