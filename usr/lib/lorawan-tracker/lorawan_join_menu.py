#!/usr/bin/env python3
"""Small terminal menu for the Waveshare SX126x LoRaWAN join flow."""

from __future__ import annotations

import getpass
import json
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
JOIN_SCRIPT = APP_DIR / "minimal_lorawan_join.py"
DEFAULT_CONFIG = APP_DIR / "lorawan_join.json"


def prompt(text: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    value = getpass.getpass(f"{text}{suffix}: ") if secret else input(f"{text}{suffix}: ")
    value = value.strip()
    return value or (default or "")


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError("config file must contain a JSON object")

    return data


def save_config(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


def build_command(config: dict) -> list[str]:
    command = [sys.executable, str(JOIN_SCRIPT)]

    for key, option in (
        ("bus", "--bus"),
        ("cs", "--cs"),
        ("reset_pin", "--reset-pin"),
        ("busy_pin", "--busy-pin"),
        ("irq_pin", "--irq-pin"),
        ("txen_pin", "--txen-pin"),
        ("rxen_pin", "--rxen-pin"),
        ("join_frequency", "--join-frequency"),
        ("join_sf", "--join-sf"),
        ("join_bandwidth", "--join-bandwidth"),
        ("join_coding_rate", "--join-coding-rate"),
        ("join_preamble", "--join-preamble"),
        ("tx_power", "--tx-power"),
        ("sync_word", "--sync-word"),
        ("join_accept_timeout", "--join-accept-timeout"),
        ("tx_timeout", "--tx-timeout"),
    ):
        if key in config and config[key] not in (None, ""):
            command.extend([option, str(config[key])])

    if config.get("scan_eu868"):
        command.append("--scan-eu868")

    command.extend([
        "--app-eui",
        config["app_eui"],
        "--dev-eui",
        config["dev_eui"],
        "--app-key",
        config["app_key"],
    ])

    if config.get("dev_nonce"):
        command.extend(["--dev-nonce", config["dev_nonce"]])

    return command


def collect_join_config(existing: dict | None = None) -> dict:
    existing = existing or {}

    print("Enter LoRaWAN join data")
    print("Leave a field blank to keep the current value shown in brackets.")

    config = dict(existing)
    config["app_eui"] = prompt("JoinEUI/AppEUI", existing.get("app_eui"))
    config["dev_eui"] = prompt("DevEUI", existing.get("dev_eui"))
    config["app_key"] = prompt("AppKey", existing.get("app_key"), secret=True)
    config["dev_nonce"] = prompt("DevNonce (optional)", existing.get("dev_nonce"))

    config["join_frequency"] = prompt("Join frequency (Hz)", str(existing.get("join_frequency", "868100000")))
    config["join_sf"] = prompt("Spreading factor", str(existing.get("join_sf", "12")))
    config["join_bandwidth"] = prompt("Bandwidth (Hz)", str(existing.get("join_bandwidth", "125000")))
    config["join_coding_rate"] = prompt("Coding rate denominator", str(existing.get("join_coding_rate", "5")))
    config["join_preamble"] = prompt("Preamble length", str(existing.get("join_preamble", "8")))
    config["tx_power"] = prompt("TX power (dBm)", str(existing.get("tx_power", "14")))
    config["sync_word"] = prompt("Sync word", existing.get("sync_word", "0x3444"))
    config["join_accept_timeout"] = prompt("Join accept timeout (seconds)", str(existing.get("join_accept_timeout", "8.0")))
    config["tx_timeout"] = prompt("TX timeout (seconds)", str(existing.get("tx_timeout", "10.0")))

    scan_default = "y" if existing.get("scan_eu868", True) else "n"
    config["scan_eu868"] = prompt("Scan EU868 channels? (y/n)", scan_default).lower().startswith("y")

    return config


def main() -> int:
    if not JOIN_SCRIPT.exists():
        print(f"Missing join script: {JOIN_SCRIPT}", file=sys.stderr)
        return 1

    config = {}
    if DEFAULT_CONFIG.exists():
        try:
            config = load_config(DEFAULT_CONFIG)
        except Exception as exc:
            print(f"Warning: could not read {DEFAULT_CONFIG.name}: {exc}")

    while True:
        print()
        print("LoRaWAN Join Menu")
        print("1) Run join with saved profile")
        print("2) Enter join data now")
        print("3) Save current profile")
        print("4) Quit")

        choice = prompt("Select", "1")

        if choice == "1":
            if not config.get("app_eui"):
                print(f"No saved profile found at {DEFAULT_CONFIG}")
                continue
            command = build_command(config)
            print("Running join with saved profile...")
            return subprocess.call(command)

        if choice == "2":
            config = collect_join_config(config)
            command = build_command(config)
            print("Running join with entered data...")
            return subprocess.call(command)

        if choice == "3":
            if not config.get("app_eui"):
                config = collect_join_config(config)
            save_config(DEFAULT_CONFIG, config)
            print(f"Saved profile to {DEFAULT_CONFIG}")
            continue

        if choice == "4":
            return 0

        print("Invalid selection")


if __name__ == "__main__":
    raise SystemExit(main())