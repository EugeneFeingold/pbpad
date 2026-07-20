#!/bin/bash
set -e

echo "=== pbpad setup ==="

# Python packages install into the pi user's site (~/.local), and the app runs
# as pi. Resolve the real user so pip runs as pi even under `sudo bash setup.sh`
# — otherwise the import checks and installs run as root, miss pi's packages,
# and rebuild everything (notably Pillow) from source.
RUN_USER="${SUDO_USER:-$(id -un)}"

# System packages — skip apt entirely if everything is already installed
APT_PKGS="python3-pip python3-rpi.gpio python3-gpiozero python3-pil libjpeg-dev zlib1g-dev i2c-tools python3-spidev"
missing=""
for p in $APT_PKGS; do
    dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
done
if [ -n "$missing" ]; then
    sudo apt-get update
    sudo apt-get install -y $missing
else
    echo "apt packages already installed, skipping"
fi

# Enable I2C interface (only if not already enabled)
if [ "$(sudo raspi-config nonint get_i2c 2>/dev/null)" = "0" ]; then
    echo "I2C already enabled, skipping"
else
    sudo raspi-config nonint do_i2c 0
fi

# Enable SPI (for the WS2812 strip on MOSI/GPIO10)
if [ "$(sudo raspi-config nonint get_spi 2>/dev/null)" = "0" ]; then
    echo "SPI already enabled, skipping"
else
    sudo raspi-config nonint do_spi 0
fi

# Give the pi user access to /dev/spidev*, so the service can drive the LEDs
# without root. Membership only takes effect after a reboot (or fresh login).
if ! id -nG pi | grep -qw spi; then
    sudo usermod -aG spi pi
fi

# Reassign the onboard ACT LED to GPIO12 so an externally-visible LED on that
# pin blinks with SD-card activity (the built-in LED is hidden in the enclosure).
# Newer RPi OS uses /boot/firmware/config.txt; older uses /boot/config.txt.
BOOT_CONFIG=""
for f in /boot/firmware/config.txt /boot/config.txt; do
    if [ -f "$f" ]; then BOOT_CONFIG="$f"; break; fi
done
if [ -n "$BOOT_CONFIG" ]; then
    for line in "dtparam=act_led_gpio=12" "dtparam=act_led_trigger=mmc0"; do
        if ! grep -qxF "$line" "$BOOT_CONFIG"; then
            echo "adding '$line' to $BOOT_CONFIG (takes effect on reboot)"
            echo "$line" | sudo tee -a "$BOOT_CONFIG" > /dev/null
        fi
    done
else
    echo "warning: no /boot/*/config.txt found; skipping ACT LED remap"
fi

# Allow the pi user to power off and reboot without a password (physical
# power button, and the Settings > Restart > Restart device menu action).
sudo tee /etc/sudoers.d/pbpad-poweroff > /dev/null << 'EOF'
pi ALL=(ALL) NOPASSWD: /sbin/poweroff, /sbin/reboot
EOF
sudo chmod 440 /etc/sudoers.d/pbpad-poweroff

# Preload known WiFi networks into NetworkManager with a high autoconnect
# priority (default is 0, so these preempt any lower-priority profile when in
# range). Credentials live in ~/.pbpad-wifi.conf on the Pi (NOT the repo) —
# one network per line, pipe-separated, with an optional priority field:
#
#     # comments and blank lines are ignored
#     My Home Network|your-wifi-password|10
#     My Home Network 5GHz|your-wifi-password|10
#
# Skipped if the file doesn't exist or a profile already exists.
WIFI_CONF="/home/${RUN_USER}/.pbpad-wifi.conf"
add_wifi() {
    local ssid="$1" psk="$2" prio="$3"
    if sudo nmcli -t -f NAME connection show | grep -Fxq "$ssid"; then
        echo "wifi '$ssid' already known, skipping"
        return
    fi
    echo "adding wifi '$ssid' (priority $prio)"
    sudo nmcli connection add type wifi con-name "$ssid" ssid "$ssid" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk" \
        connection.autoconnect yes \
        connection.autoconnect-priority "$prio" > /dev/null
}
if [ -f "$WIFI_CONF" ]; then
    while IFS='|' read -r ssid psk prio; do
        # trim whitespace, skip blanks + comments
        ssid="${ssid#"${ssid%%[![:space:]]*}"}"; ssid="${ssid%"${ssid##*[![:space:]]}"}"
        [ -z "$ssid" ] && continue
        case "$ssid" in \#*) continue ;; esac
        add_wifi "$ssid" "$psk" "${prio:-10}"
    done < "$WIFI_CONF"
else
    echo "no wifi config at $WIFI_CONF; skipping (see the header of setup.sh for the format)"
fi

# Python packages — install each only if its module can't already be imported,
# so re-runs don't rebuild Pillow (pulled in by luma.oled) from source.
pip_ensure() {
    local module="$1"; shift
    if sudo -u "$RUN_USER" python3 -c "import $module" 2>/dev/null; then
        echo "  $module already present, skipping"
    else
        echo "  installing $module ..."
        sudo -u "$RUN_USER" pip3 install "$@"
    fi
}

pip_ensure luma.oled.device luma.oled   # also pulls in Pillow
pip_ensure smbus2 smbus2                 # LC709203F battery gauge
pip_ensure lzstring lzstring
pip_ensure websocket websocket-client
pip_ensure json5 json5
pip_ensure pytz pytz
pip_ensure click click

# pixelblaze-client must be installed without deps — py_mini_racer fails
# to compile on ARM. lzstring (installed above) is the only dep we need.
pip_ensure pixelblaze --no-deps pixelblaze-client

# Early boot splash: paints "Starting... please wait" on the OLED as soon as
# the filesystem is up, long before the main app finishes loading.
sudo tee /etc/systemd/system/pbpad-splash.service > /dev/null << 'EOF'
[Unit]
Description=pbpad boot splash
DefaultDependencies=no
# systemd-modules-load loads i2c-dev, which creates /dev/i2c-1 (the OLED bus).
After=local-fs.target systemd-modules-load.service
# Gate only the app, NOT basic.target: as a oneshot this holds up whatever it's
# ordered before until it exits, and its i2c retry can take several seconds. We
# want the app to wait for the splash (so they don't race the display), but the
# rest of the OS should boot in parallel.
Before=pbpad.service

[Service]
Type=oneshot
User=pi
WorkingDirectory=/home/pi/dev/pbpad
ExecStart=/usr/bin/python3 splash.py

[Install]
WantedBy=sysinit.target
EOF

# Install pbpad systemd service
sudo tee /etc/systemd/system/pbpad.service > /dev/null << 'EOF'
[Unit]
Description=PBPad PixelBlaze Controller
After=network.target pbpad-splash.service
# Appliance: never stop trying to restart, even in a tight crash loop.
StartLimitIntervalSec=0

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/dev/pbpad
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF

# --- Boot speed: disable services this device does not use ---
# Bluetooth (unused), waiting for network-online (the app tolerates no WiFi),
# the hotkey daemon, and the modem manager. Missing units are ignored.
sudo systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
sudo systemctl disable --now hciuart.service 2>/dev/null || true
sudo systemctl disable --now bluetooth.service 2>/dev/null || true
sudo systemctl disable --now triggerhappy.service 2>/dev/null || true
sudo systemctl disable --now ModemManager.service 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable pbpad
sudo systemctl enable pbpad-splash

echo ""
echo "=== Setup complete ==="
echo "Start the service with: sudo systemctl start pbpad"
echo "Or run directly with:   python3 main.py"
