#!/usr/bin/env bash
# Raspberry Pi hardware setup for lorawan-tracker.
# Must be run as root (called via pkexec or sudo from the GUI, or from postinst).
# Safe to run multiple times — all checks are idempotent.
set -euo pipefail

# ── Locate boot config ────────────────────────────────────────────────────────
if [[ -f /boot/firmware/config.txt ]]; then
    BOOT_CFG="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
    BOOT_CFG="/boot/config.txt"
else
    echo "ERROR: Cannot find boot config.txt"
    exit 1
fi

# ── Locate cmdline.txt ────────────────────────────────────────────────────────
if [[ -f /boot/firmware/cmdline.txt ]]; then
    CMDLINE="/boot/firmware/cmdline.txt"
elif [[ -f /boot/cmdline.txt ]]; then
    CMDLINE="/boot/cmdline.txt"
else
    CMDLINE=""
fi

REBOOT_NEEDED=0

echo "=== lorawan-tracker Raspberry Pi setup ==="
echo "Boot config : $BOOT_CFG"
[[ -n "$CMDLINE" ]] && echo "Cmdline     : $CMDLINE"
echo ""

# ── Helper: add or update a key=value line in config.txt ─────────────────────
set_boot_param() {
    local key="$1" value="$2"
    local wanted="${key}=${value}"
    # If the exact line already exists — nothing to do
    if grep -qE "^${key}=${value}$" "$BOOT_CFG"; then
        echo "  [OK]   $wanted"
        return
    fi
    # Replace existing (possibly commented-out) line
    if grep -qE "^#?\s*${key}=" "$BOOT_CFG"; then
        sed -i "s|^#\?\s*${key}=.*|${wanted}|" "$BOOT_CFG"
        echo "  [SET]  $wanted  (replaced existing)"
    else
        echo "" >> "$BOOT_CFG"
        echo "$wanted" >> "$BOOT_CFG"
        echo "  [ADD]  $wanted"
    fi
    REBOOT_NEEDED=1
}

# ── Helper: add dtoverlay if not already present ──────────────────────────────
set_overlay() {
    local overlay="$1"
    if grep -qE "^dtoverlay=${overlay}$" "$BOOT_CFG"; then
        echo "  [OK]   dtoverlay=${overlay}"
        return
    fi
    echo "dtoverlay=${overlay}" >> "$BOOT_CFG"
    echo "  [ADD]  dtoverlay=${overlay}"
    REBOOT_NEEDED=1
}

# ── Step 1: enable_uart=1 ─────────────────────────────────────────────────────
echo "[1/4] Hardware UART"
set_boot_param "enable_uart" "1"

# ── Step 2: disable Bluetooth overlay ────────────────────────────────────────
echo "[2/4] Disable Bluetooth (frees PL011 UART for GPS on GPIO 14/15)"
set_overlay "disable-bt"

# ── Step 3: disable serial console ───────────────────────────────────────────
echo "[3/4] Serial console"
SERIAL_DISABLED=0
if command -v raspi-config &>/dev/null; then
    CONS=$(raspi-config nonint get_serial_cons 2>/dev/null || echo "1")
    if [[ "$CONS" == "0" ]]; then
        raspi-config nonint do_serial_cons 1 2>/dev/null || true
        echo "  [SET]  serial console disabled"
        REBOOT_NEEDED=1
        SERIAL_DISABLED=1
    else
        echo "  [OK]   serial console already disabled"
        SERIAL_DISABLED=1
    fi
fi
# Fallback: edit cmdline.txt directly
if [[ "$SERIAL_DISABLED" -eq 0 && -n "$CMDLINE" ]]; then
    if grep -q "console=serial0\|console=ttyAMA0\|console=ttyS0" "$CMDLINE"; then
        sed -i 's/console=serial0,[0-9]* //g;s/console=ttyAMA0,[0-9]* //g;s/console=ttyS0,[0-9]* //g' "$CMDLINE"
        echo "  [SET]  removed serial console from cmdline.txt"
        REBOOT_NEEDED=1
    else
        echo "  [OK]   no serial console in cmdline.txt"
    fi
fi

# ── Step 4: SPI ───────────────────────────────────────────────────────────────
echo "[4/4] SPI interface (required for LoRa radio)"
if command -v raspi-config &>/dev/null; then
    SPI=$(raspi-config nonint get_spi 2>/dev/null || echo "1")
    if [[ "$SPI" == "1" ]]; then
        raspi-config nonint do_spi 0 2>/dev/null || true
        echo "  [SET]  SPI enabled"
        REBOOT_NEEDED=1
    else
        echo "  [OK]   SPI already enabled"
    fi
else
    set_boot_param "dtparam=spi" "on"
fi

echo ""
if [[ "$REBOOT_NEEDED" -eq 1 ]]; then
    echo "Changes applied. A reboot is required to take effect."
    echo "REBOOT_REQUIRED"
else
    echo "Everything was already configured correctly. No changes made."
    echo "SETUP_OK"
fi
