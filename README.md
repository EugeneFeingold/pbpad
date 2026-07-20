# pbpad

A portable, handheld remote for [PixelBlaze](https://electromage.com/) LED
controllers, built on a Raspberry Pi Zero W. It discovers PixelBlazes on your
Wi-Fi, lets you switch patterns and tweak their controls from a two-knob OLED
interface, and mirrors a live preview of the running pattern on an onboard
WS2812 strip — all from a battery-powered device that fits in one hand.

## Features

- **PixelBlaze discovery** — finds controllers on the local network (or connect
  by IP), with a preferred-device list so it reconnects to your usual one.
- **Pattern control** — browse and switch patterns, drive their sliders and
  color pickers, and toggle the sequencer's playlist and shuffle modes.
- **Live on-device preview** — a second WebSocket streams preview frames from
  the PixelBlaze and renders them on the local 8-pixel WS2812 strip, with
  frame buffering and playback smoothing tuned for the Pi Zero's CPU budget.
- **Two-encoder UI** on a 128×64 SH1106 OLED — one knob navigates and confirms,
  the other goes back; both are velocity-sensitive for fast scrolling.
- **Battery monitoring** via an LC709203F fuel gauge, with a low-battery
  conservation mode that suspends streaming and shows a charge gauge on the LEDs.
- **Single-button power** — press to wake from halt, long-press for a clean
  OS shutdown (see [docs/wiring.md](docs/wiring.md)).
- **Wi-Fi management** — scan and join networks from the device, or preload
  known networks at setup time.

## Documentation

- **[Wiring diagram](docs/wiring.md)** — full GPIO pinout, power path, and
  the single-button power circuit.
- **[Menu navigation tree](docs/nav_tree.md)** — every screen and how the
  two encoders move between them.

## Hardware

| Part | Role |
|------|------|
| Raspberry Pi Zero W | Host (powered via the GPIO header, not micro-USB) |
| SH1106 1.3" OLED, I²C | Display — **not** SSD1306-compatible |
| 2 × push rotary encoders | Navigation / OK / Back |
| WS2812 8-pixel strip | On-device preview (driven directly from GPIO10, no level shifter) |
| LC709203F fuel gauge | Battery percentage (on a software I²C bus) |
| IP5306 charger + boost board | LiPo charge + 5V boost |
| 3.7V LiPo cell | Power |
| Momentary push button | Power on / off |

Pin assignments live in [config.py](config.py); the exact wiring, including the
power circuit and the second (software) I²C bus for the fuel gauge, is in
[docs/wiring.md](docs/wiring.md).

## Setup (on the Pi)

Clone onto the Pi and run the setup script. It installs system and Python
packages, enables I²C and SPI, installs the systemd services, and configures
single-button power-off:

```bash
git clone https://github.com/EugeneFeingold/pbpad.git ~/dev/pbpad
cd ~/dev/pbpad
bash setup.sh
sudo reboot          # SPI group membership + boot config take effect on reboot
```

After reboot the `pbpad` service starts automatically. To run it by hand
instead:

```bash
python3 main.py
```

### Wi-Fi credentials

Real Wi-Fi credentials are **not** stored in the repo. To preload networks at
setup time, copy the example file to `~/.pbpad-wifi.conf` on the Pi and fill in
your own values:

```bash
cp pbpad-wifi.conf.example ~/.pbpad-wifi.conf
chmod 600 ~/.pbpad-wifi.conf
# edit ~/.pbpad-wifi.conf — one network per line: SSID|password|priority
```

`setup.sh` reads that file and preloads the networks into NetworkManager. The
file is git-ignored. You can also join networks interactively from the device's
Wi-Fi menu at any time.

## Deploying changes

The deploy scripts tarball the app, copy it to the Pi over SSH, and restart the
service. Keep the two in sync — a test enforces feature parity between them.

```bash
./deploy.sh          # macOS / Linux
deploy.bat           # Windows
```

Override the defaults with environment variables:

```bash
PBPAD_PI=pi@yourhost.local PBPAD_KEY=~/.ssh/id_pbpad ./deploy.sh
```

## Development

The app is developed off-device with all hardware faked, so the full suite runs
on any machine without a Pi attached.

```bash
pip install pytest pytest-asyncio pillow
python -m pytest
```

Hardware (GPIO, I²C, SPI, the OLED) is stubbed in [tests/_fakes.py](tests/_fakes.py).
The suite includes performance guards for the OLED render path — the Pi Zero is
easy to overload, so those tests count expensive per-frame operations rather
than wall-clock time.

## Project layout

```
main.py            App loop, event wiring, state machine
config.py          Pin map and all tunables
store.py           Persisted settings (JSON)
splash.py          Early-boot OLED splash (own systemd service)
hardware/          OLED, encoders, LEDs, battery gauge, power button
pb/                PixelBlaze client, discovery, and preview stream
ui/screens.py      Screen classes and the menu tree
wifi/              Network scan and NetworkManager control
docs/              Wiring diagram and navigation-tree reference pages
tests/             pytest suite (hardware faked)
```

## License

[MIT](LICENSE) © 2026 Gene Feingold
