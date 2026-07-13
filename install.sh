#!/usr/bin/env bash
#
# lorawan-tracker one-command headless installer.
#
#   curl -fsSL https://raw.githubusercontent.com/davor-ugresic/lorawan-tracker/main/install.sh | sudo bash
#
# Adds the signed apt repository and installs the package. The package's
# postinst then configures the Raspberry Pi hardware, generates device
# credentials from the Pi serial, and enables + starts the tracker service.
#
set -euo pipefail

REPO_BASE="https://raw.githubusercontent.com/davor-ugresic/lorawan-tracker/main"
KEY_URL="${REPO_BASE}/lorawan-tracker.gpg"
KEYRING="/etc/apt/keyrings/lorawan-tracker.gpg"
SOURCES="/etc/apt/sources.list.d/lorawan-tracker.list"
SUITE="stable"
COMPONENT="main"

# ── Require root ──────────────────────────────────────────────────────────────
if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo -E bash "$0" "$@"
    fi
    echo "ERROR: please run as root (or install sudo)." >&2
    exit 1
fi

echo "==> Installing prerequisites..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true
apt-get install -y --no-install-recommends ca-certificates curl gnupg >/dev/null

echo "==> Adding lorawan-tracker apt repository..."
install -d -m 0755 /etc/apt/keyrings
# The published key is ASCII-armored; dearmor into a binary keyring for signed-by.
curl -fsSL "${KEY_URL}" | gpg --dearmor -o "${KEYRING}"
chmod 0644 "${KEYRING}"

echo "deb [signed-by=${KEYRING}] ${REPO_BASE} ${SUITE} ${COMPONENT}" > "${SOURCES}"
echo "    ${SOURCES}"

echo "==> Updating package lists..."
apt-get update -qq \
    -o Dir::Etc::sourcelist="sources.list.d/lorawan-tracker.list" \
    -o Dir::Etc::sourceparts="-" \
    -o APT::Get::List-Cleanup="0" || apt-get update -qq

echo "==> Installing lorawan-tracker..."
apt-get install -y lorawan-tracker

echo ""
echo "Done. Check status with:  systemctl status lorawan-tracker.service"
echo "If the installer reported a reboot is required, run:  sudo reboot"
