# Wiring

The hardware in this build, every pin on the Pi's 40-pin header, and the power
path. GPIO functions come from [`conf/config.py`](../conf/config.py); the power subsystem
from the build notes.

> The Pi is powered through the **5V header pins, not micro-USB**.

## Hardware components

| Component | Details |
|---|---|
| Raspberry Pi Zero W | Host · WiFi |
| SH1106 OLED | 1.3″ 128×64 · I²C (**not** SSD1306-compatible) |
| Rotary encoder ×2 | Push-rotary (EC11) |
| WS2812 LED strip | 8 pixels · data driven **directly** from GPIO10 (no level shifter) |
| LC709203F | LiPo fuel gauge (Adafruit 4712) |
| LiPo cell | 3.7 V · 5000 mAh |
| HW-775 board | IP5306 charger + 5V boost |
| Push button | Momentary · power |
| AO3407 | P-channel MOSFET |
| Schottky diode | BAT85 / 1N5817 |
| 100 kΩ resistor | MOSFET gate pull-down |

## GPIO header — pin functions

Physical pin order, as on the board (pin 1 at the corner). Left column = odd
pins, right column = even pins. Blank "Used for" = unused.

| Pin | Function | Used for | | Pin | Function | Used for |
|---:|---|---|---|---:|---|---|
| 1 | 3V3 | OLED VCC (3.3V) | | 2 | 5V | IP5306 5V in |
| 3 | GPIO2 / SDA1 | OLED SDA | | 4 | 5V | WS2812 5V |
| 5 | GPIO3 / SCL1 | OLED SCL **+ WAKE** | | 6 | GND | GND rail |
| 7 | GPIO4 | — | | 8 | GPIO14 / TXD | — |
| 9 | GND | OLED + button GND | | 10 | GPIO15 / RXD | — |
| 11 | GPIO17 | Button — shutdown detect | | 12 | GPIO18 | — |
| 13 | GPIO27 | MOSFET gate | | 14 | GND | Gauge GND |
| 15 | GPIO22 | — | | 16 | GPIO23 | Gauge SDA (sw i2c) |
| 17 | 3V3 | Gauge VCC | | 18 | GPIO24 | Gauge SCL (sw i2c) |
| 19 | GPIO10 / MOSI | WS2812 data | | 20 | GND | WS2812 GND |
| 21 | GPIO9 / MISO | — | | 22 | GPIO25 | — |
| 23 | GPIO11 / SCLK | — | | 24 | GPIO8 / CE0 | — |
| 25 | GND | GND rail | | 26 | GPIO7 / CE1 | — |
| 27 | GPIO0 / ID_SD | reserved | | 28 | GPIO1 / ID_SC | reserved |
| 29 | GPIO5 | — | | 30 | GND | GND rail |
| 31 | GPIO6 | — | | 32 | GPIO12 | — |
| 33 | GPIO13 | Enc 2 · B | | 34 | GND | Enc 1 GND |
| 35 | GPIO19 | Enc 2 · A | | 36 | GPIO16 | Enc 1 · SW |
| 37 | GPIO26 | Enc 2 · SW | | 38 | GPIO20 | Enc 1 · A |
| 39 | GND | Enc 2 GND | | 40 | GPIO21 | Enc 1 · B |

**Subsystems:** 3.3 V · 5 V · GND · OLED · Encoders · LEDs · Fuel gauge · Power button

## Power

### Power button — one button, four jobs

Press wakes / turns on; a 5-second hold triggers a clean shutdown. A P-MOSFET
(gated by GPIO27) isolates the wake tap from the OLED's SCL line while running.

```
button ──── GND (pin 9)
   │
   ├──── GPIO17 (pin 11)          shutdown detect
   │
   ├──── IP5306 KEY               toggles boost (full off/on)
   │
   └─[Schottky]─ D ─[AO3407]─ S ── GPIO3 (pin 5)   wake
                        G
                        ├─ 100 kΩ ─ GND       default: MOSFET ON
                        └─ GPIO27 (pin 13)    pbpad drives HIGH → OFF
```

**How it behaves.** *Halted:* GPIO27 floats, the pull-down holds the gate low,
the MOSFET conducts, and a press pulls GPIO3 low to wake. *Running:* pbpad
drives GPIO27 high, the MOSFET opens, and the button only reaches GPIO17 — so a
shutdown-hold no longer disturbs the I²C SCL.

### Power path — HW-775 · IP5306

| From | | To | Note |
|---|:-:|---|---|
| USB-C 5V | → | IP5306 IN | charge input |
| LiPo 3.7V | ⇄ | IP5306 BAT | 5000 mAh cell |
| IP5306 OUT | → | 5V · pin 2 | boost — powers Pi via header |
| IP5306 GND | → | GND | common ground |
| IP5306 KEY | ⇄ | power button | idle-shutoff after Pi halts |

> ⚠️ **Verify before soldering.** The IP5306 **KEY** terminal varies by HW-775
> clone — confirm with a meter.

### The fuel gauge needs a second, software I²C bus

The LC709203F requires I²C clock stretching, which the Pi's hardware I²C can't
do reliably, so it lives on a bit-banged `i2c-gpio` bus separate from the OLED.
Add this to `/boot/config.txt` (or `/boot/firmware/config.txt` on newer
Raspberry Pi OS) and reboot:

```
dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24,i2c_gpio_delay_us=2
```
