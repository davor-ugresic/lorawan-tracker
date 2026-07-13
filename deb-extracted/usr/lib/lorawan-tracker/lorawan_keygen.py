#!/usr/bin/env python3
"""Headless LoRaWAN credential generator for lorawan-tracker.

Derives DevEUI, JoinEUI/AppEUI and AppKey deterministically from the
Raspberry Pi's hardware serial number, so the same board always produces the
same credentials and an operator can always recompute them from the serial.

Design:
  * Values are derived with HMAC-SHA256 using distinct domain labels, so the
    three credentials are independent but reproducible.
  * DevEUI  starts with the marker byte 0xFE and JoinEUI with 0xFD so devices
    provisioned by this generator are easy to recognise on the network server.
  * If /etc/lorawan-tracker/site.salt exists its contents are mixed into the
    derivation. This lets an operator harden the keys so they are not derivable
    from the (semi-public) serial alone, while still being reproducible on this
    board. Without it, credentials are a pure function of the serial — which is
    the intended default for a known/self-hosted network.

Usage:
  lorawan_keygen.py [--config PATH] [--salt PATH] [--force] [--print-only]

Exit codes:
  0  config written (or already present and valid)
  1  could not determine the Pi serial number
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from pathlib import Path

DOMAIN = b"lorawan-tracker/v1"
DEFAULT_CONFIG = Path("/etc/lorawan-tracker/lorawan_join.json")
DEFAULT_SALT = Path("/etc/lorawan-tracker/site.salt")
KEYS_TXT = "device_keys.txt"

# Fixed marker first-bytes so our devices are recognisable on the network server.
DEV_EUI_PREFIX = 0xFE
JOIN_EUI_PREFIX = 0xFD


def read_pi_serial() -> str | None:
    """Return the Raspberry Pi serial as a lowercase hex string, or None."""
    # Preferred: device tree serial-number (16 hex chars, NUL-terminated).
    dt = Path("/sys/firmware/devicetree/base/serial-number")
    try:
        if dt.exists():
            raw = dt.read_bytes().rstrip(b"\x00").decode("ascii", "ignore").strip()
            if raw:
                return raw.lower()
    except Exception:
        pass
    # Fallback: /proc/cpuinfo "Serial" line.
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("serial"):
                value = line.split(":", 1)[1].strip()
                if value:
                    return value.lower()
    except Exception:
        pass
    return None


def _derive(serial: str, salt: bytes, label: bytes, nbytes: int, prefix: int | None) -> bytes:
    key = DOMAIN + b"|" + salt
    msg = b"|" + label + b"|" + serial.encode("ascii")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    out = bytearray(digest[:nbytes])
    if prefix is not None:
        out[0] = prefix
    return bytes(out)


def generate_credentials(serial: str, salt: bytes = b"") -> dict[str, str]:
    dev_eui = _derive(serial, salt, b"dev_eui", 8, DEV_EUI_PREFIX)
    join_eui = _derive(serial, salt, b"join_eui", 8, JOIN_EUI_PREFIX)
    app_key = _derive(serial, salt, b"app_key", 16, None)
    return {
        "app_eui": join_eui.hex().upper(),
        "dev_eui": dev_eui.hex().upper(),
        "app_key": app_key.hex().upper(),
    }


def build_config(creds: dict[str, str]) -> dict[str, object]:
    return {
        "app_eui": creds["app_eui"],
        "dev_eui": creds["dev_eui"],
        "app_key": creds["app_key"],
        "scan_eu868": True,
        "join_retry_delay": 30,
        "join_accept_timeout": 12.0,
        "confirmed_uplink": False,
    }


def _has_valid_keys(cfg: object) -> bool:
    return (
        isinstance(cfg, dict)
        and bool(cfg.get("app_eui"))
        and bool(cfg.get("dev_eui"))
        and bool(cfg.get("app_key"))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate LoRaWAN credentials from the Pi serial")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Config JSON path to write")
    parser.add_argument("--salt", default=str(DEFAULT_SALT), help="Optional site salt file mixed into derivation")
    parser.add_argument("--force", action="store_true", help="Overwrite existing credentials")
    parser.add_argument("--print-only", action="store_true", help="Print credentials without writing files")
    args = parser.parse_args()

    serial = read_pi_serial()
    if not serial:
        print("ERROR: could not determine the Raspberry Pi serial number.", file=sys.stderr)
        return 1

    salt = b""
    salt_path = Path(args.salt)
    if salt_path.exists():
        try:
            salt = salt_path.read_bytes().strip()
        except Exception:
            salt = b""

    config_path = Path(args.config)

    # Preserve existing credentials unless --force. Deterministic derivation means
    # regenerating would yield identical values anyway, but we avoid clobbering any
    # manual edits (e.g. a custom JoinEUI registered on the network server).
    if config_path.exists() and not args.force:
        try:
            existing = json.loads(config_path.read_text())
        except Exception:
            existing = None
        if _has_valid_keys(existing):
            print(f"Credentials already present in {config_path}; leaving unchanged.")
            _print_summary(serial, existing, salt)
            return 0

    creds = generate_credentials(serial, salt)
    config = build_config(creds)

    if args.print_only:
        _print_summary(serial, config, salt)
        return 0

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    try:
        config_path.chmod(0o600)
    except Exception:
        pass

    keys_txt = config_path.parent / KEYS_TXT
    keys_txt.write_text(
        "LoRaWAN Tracker — device credentials\n"
        "(deterministically derived from the Pi serial; keep this safe)\n\n"
        f"Pi serial : {serial}\n"
        f"Salted    : {'yes' if salt else 'no'}\n"
        f"DevEUI    : {creds['dev_eui']}\n"
        f"JoinEUI   : {creds['app_eui']}\n"
        f"AppKey    : {creds['app_key']}\n"
    )
    try:
        keys_txt.chmod(0o600)
    except Exception:
        pass

    print(f"Credentials written to {config_path}")
    _print_summary(serial, config, salt)
    return 0


def _print_summary(serial: str, cfg: dict, salt: bytes) -> None:
    print("---------------------------------------------")
    print(f"  Pi serial : {serial}")
    print(f"  Salted    : {'yes' if salt else 'no'}")
    print(f"  DevEUI    : {cfg.get('dev_eui')}")
    print(f"  JoinEUI   : {cfg.get('app_eui')}")
    print(f"  AppKey    : {cfg.get('app_key')}")
    print("---------------------------------------------")


if __name__ == "__main__":
    raise SystemExit(main())
