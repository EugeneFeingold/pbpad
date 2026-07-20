#!/usr/bin/env bash
# migrate-to-nm.sh — switch this Raspberry Pi from the legacy
# dhcpcd + wpa_supplicant networking stack to NetworkManager.
#
# pbpad's WiFi layer and setup.sh are written against NetworkManager (nmcli).
# On a Pi still running the classic Raspbian stack, none of that works: the
# scanner falls back to a crippled iwlist and joining/reset are no-ops. This
# migrates the machine to the backend pbpad expects.
#
# What it does (nothing changes until you confirm):
#   1. Reads your WiFi SSIDs + PSKs from wpa_supplicant.conf. The password
#      never leaves the Pi.
#   2. Recreates them as NetworkManager connection profiles so NM reconnects
#      the instant it starts.
#   3. Disables dhcpcd + wpa_supplicant, unmasks + enables NetworkManager.
#   4. Drops a console revert helper, then REBOOTS — the switch takes effect
#      on reboot, which is the safe, atomic transition point.
#
# Run ON THE PI as root:
#     sudo bash migrate-to-nm.sh
#
# SAFETY / RECOVERY: if WiFi does not come back after the reboot, attach a
# keyboard + screen and run:
#     sudo /usr/local/sbin/revert-networking.sh
# (also printed below). That re-enables the legacy stack and reboots.
set -euo pipefail

WPA_CONF=/etc/wpa_supplicant/wpa_supplicant.conf
NM_DIR=/etc/NetworkManager/system-connections
REVERT=/usr/local/sbin/revert-networking.sh

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root:  sudo bash $0" >&2
    exit 1
fi
command -v nmcli >/dev/null 2>&1 || {
    echo "NetworkManager isn't installed (no nmcli)." >&2
    echo "Install it first:  sudo apt update && sudo apt install -y network-manager" >&2
    exit 1
}
[ -f "$WPA_CONF" ] || { echo "Not found: $WPA_CONF — nothing to migrate." >&2; exit 1; }

echo "== Reading WiFi networks from $WPA_CONF"

# Parse each `network={ ... }` block into: ssid <TAB> psk <TAB> keymgmt.
# Handles quoted PSKs and pre-hashed (64-hex, unquoted) PSKs; ignores comments.
parsed=$(awk '
    function unq(s){ sub(/^[^"]*"/,"",s); sub(/".*$/,"",s); return s }
    /^[ \t]*network[ \t]*=[ \t]*\{/ { inb=1; ssid=""; psk=""; km=""; next }
    inb && /^[ \t]*\}/ { if (ssid!="") print ssid "\t" psk "\t" km; inb=0; next }
    inb && /^[ \t]*#/ { next }
    inb && /ssid[ \t]*=[ \t]*"/ { ssid=unq($0); next }
    inb && /psk[ \t]*=[ \t]*"/  { psk=unq($0); next }
    inb && /psk[ \t]*=[ \t]*[0-9a-fA-F]/ && $0 !~ /"/ {
        p=$0; sub(/^[ \t]*psk[ \t]*=[ \t]*/,"",p); sub(/[ \t\r]*$/,"",p); psk=p; next }
    inb && /key_mgmt[ \t]*=[ \t]*NONE/ { km="NONE"; next }
' "$WPA_CONF")

# WiFi regulatory country, if configured (needed so the radio comes up right).
CC=$(awk -F= '/^[ \t]*country[ \t]*=/ { gsub(/[ \t\r]/,"",$2); print $2; exit }' "$WPA_CONF" || true)

ssids=(); psks=(); kms=()
while IFS=$'\t' read -r a b c; do
    [ -n "$a" ] || continue
    ssids+=("$a"); psks+=("$b"); kms+=("$c")
done <<< "$parsed"

n=${#ssids[@]}
if [ "$n" -eq 0 ]; then
    echo "No usable networks found (hidden or hex-encoded SSIDs are not auto-migrated)." >&2
    echo "Aborting — refusing to switch to NM with zero profiles (that would lock you out)." >&2
    exit 1
fi

echo "Found $n network(s):"
for s in "${ssids[@]}"; do echo "   - $s"; done
[ -n "$CC" ] && echo "WiFi country: $CC"

echo
echo "This will:"
echo "   * create the $n NetworkManager profile(s) above"
echo "   * disable dhcpcd + wpa_supplicant, enable NetworkManager"
echo "   * REBOOT (the switch takes effect on reboot)"
echo
echo "If WiFi doesn't return, recover at the console with:"
echo "   sudo $REVERT"
echo
read -r -p "Proceed? type 'yes' to continue: " ans
[ "$ans" = "yes" ] || { echo "Aborted. No changes made."; exit 1; }

echo "== Writing NetworkManager profiles"
mkdir -p "$NM_DIR"
for i in "${!ssids[@]}"; do
    ssid=${ssids[$i]}; psk=${psks[$i]}; km=${kms[$i]}
    fname=$(printf '%s' "$ssid" | tr -c 'A-Za-z0-9._-' '_')
    file="$NM_DIR/${fname}.nmconnection"
    uuid=$(cat /proc/sys/kernel/random/uuid)
    {
        printf '[connection]\n'
        printf 'id=%s\n' "$ssid"
        printf 'uuid=%s\n' "$uuid"
        printf 'type=wifi\n'
        printf 'autoconnect=true\n'
        printf 'autoconnect-priority=10\n\n'
        printf '[wifi]\n'
        printf 'mode=infrastructure\n'
        printf 'ssid=%s\n\n' "$ssid"
        if [ -n "$psk" ] && [ "$km" != "NONE" ]; then
            printf '[wifi-security]\n'
            printf 'key-mgmt=wpa-psk\n'
            printf 'psk=%s\n\n' "$psk"
        fi
        printf '[ipv4]\nmethod=auto\n\n'
        printf '[ipv6]\nmethod=auto\n'
    } > "$file"
    chmod 600 "$file"
    chown root:root "$file"
    echo "   wrote $file  ($ssid)"
done

# Persist the regulatory domain so the radio isn't left unconfigured under NM.
if [ -n "$CC" ] && command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_wifi_country "$CC" || true
fi
rfkill unblock wifi 2>/dev/null || true

echo "== Installing console revert helper at $REVERT"
cat > "$REVERT" <<'REV'
#!/bin/sh
# Undo the NetworkManager migration: go back to dhcpcd + wpa_supplicant.
set -e
systemctl disable NetworkManager 2>/dev/null || true
systemctl unmask dhcpcd wpa_supplicant 2>/dev/null || true
systemctl enable dhcpcd wpa_supplicant 2>/dev/null || true
echo "Reverted to the legacy stack. Rebooting..."
reboot
REV
chmod +x "$REVERT"

echo "== Switching services (takes effect on reboot; current link stays up until then)"
systemctl unmask NetworkManager 2>/dev/null || true
systemctl enable NetworkManager
systemctl disable dhcpcd 2>/dev/null || true
systemctl disable wpa_supplicant 2>/dev/null || true
systemctl disable wpa_supplicant@wlan0 2>/dev/null || true

echo
echo "== Done. Rebooting into NetworkManager now."
echo "   If WiFi doesn't come back, at the console run:  sudo $REVERT"
sleep 2
reboot
