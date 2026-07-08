#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY_ID="$(cat "${REPO_DIR}/.gpg-key-id")"
SUITE="stable"
COMPONENT="main"

# ── Find .deb file ────────────────────────────────────────────────────────────
if [[ -n "$1" ]]; then
    DEB_FILE="$(realpath "$1")"
else
    DEB_FILE="$(ls -t "${HOME}/Documents/LoRa"/lorawan-tracker_*_all.deb 2>/dev/null | head -1)"
fi

if [[ -z "$DEB_FILE" || ! -f "$DEB_FILE" ]]; then
    echo "ERROR: No .deb file found. Pass the path as an argument:"
    echo "  bash publish.sh /path/to/lorawan-tracker_X.Y.Z_all.deb"
    exit 1
fi

DEB_NAME="$(basename "${DEB_FILE}")"
echo "Publishing: ${DEB_NAME}"

# ── Copy .deb into pool ───────────────────────────────────────────────────────
cp "${DEB_FILE}" "${REPO_DIR}/pool/main/${DEB_NAME}"
echo "  Copied to pool/main/"

# ── Generate Packages index ───────────────────────────────────────────────────
cd "${REPO_DIR}"
dpkg-scanpackages --arch all pool/main > dists/${SUITE}/${COMPONENT}/binary-all/Packages 2>/dev/null
gzip -9 -k -f dists/${SUITE}/${COMPONENT}/binary-all/Packages
echo "  Packages index updated."

# ── Generate Release file ─────────────────────────────────────────────────────
PKGS_PATH="dists/${SUITE}/${COMPONENT}/binary-all/Packages"
PKGS_GZ_PATH="${PKGS_PATH}.gz"

PKGS_SIZE=$(wc -c < "${PKGS_PATH}")
PKGSGZ_SIZE=$(wc -c < "${PKGS_GZ_PATH}")
PKGS_MD5=$(md5sum "${PKGS_PATH}" | awk '{print $1}')
PKGSGZ_MD5=$(md5sum "${PKGS_GZ_PATH}" | awk '{print $1}')
PKGS_SHA256=$(sha256sum "${PKGS_PATH}" | awk '{print $1}')
PKGSGZ_SHA256=$(sha256sum "${PKGS_GZ_PATH}" | awk '{print $1}')

cat > "dists/${SUITE}/Release" << EOF
Origin: LoRaWAN Tracker
Label: LoRaWAN Tracker
Suite: ${SUITE}
Codename: ${SUITE}
Date: $(date -Ru)
Architectures: all
Components: ${COMPONENT}
Description: LoRaWAN tracker packages for Raspberry Pi
MD5Sum:
 ${PKGS_MD5} ${PKGS_SIZE} ${COMPONENT}/binary-all/Packages
 ${PKGSGZ_MD5} ${PKGSGZ_SIZE} ${COMPONENT}/binary-all/Packages.gz
SHA256:
 ${PKGS_SHA256} ${PKGS_SIZE} ${COMPONENT}/binary-all/Packages
 ${PKGSGZ_SHA256} ${PKGSGZ_SIZE} ${COMPONENT}/binary-all/Packages.gz
EOF
echo "  Release file generated."

# ── Sign Release file ─────────────────────────────────────────────────────────
gpg --default-key "${KEY_ID}" --batch --yes \
    --armor --detach-sign \
    --output "dists/${SUITE}/Release.gpg" \
    "dists/${SUITE}/Release"
gpg --default-key "${KEY_ID}" --batch --yes \
    --clearsign \
    --output "dists/${SUITE}/InRelease" \
    "dists/${SUITE}/Release"
echo "  Release signed."

# ── Commit and push ───────────────────────────────────────────────────────────
git add -A
git commit -m "Release ${DEB_NAME}"
git push origin main

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Published!  ${DEB_NAME} is now live."
echo "  Friends can update with:  sudo apt update && sudo apt upgrade"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
