#!/usr/bin/env python3
"""LoRaWAN GPS Tracker — background service daemon.

Orchestrates OTAA join followed by continuous GPS uplink transmission.
Designed to run as a systemd user service (lorawan-tracker.service).

Cycle:
  1. Run minimal_lorawan_join.py — retry until join succeeds.
  2. Run lorawan_uplink.py — transmit GPS forever.
  3. If uplink exits unexpectedly, go back to step 1.

Status is written to ~/.local/share/lorawan-tracker/status.json
so the dashboard can display it without any IPC.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "lorawan_join.json"
STATUS_DIR = Path.home() / ".local" / "share" / "lorawan-tracker"
STATUS_FILE = STATUS_DIR / "status.json"
LOG_FILE = STATUS_DIR / "service.log"
TRACK_FILE  = STATUS_DIR / "track.jsonl"
TRACK_MAX   = 2000   # lines to keep in track.jsonl
JOIN_SCRIPT = APP_DIR / "minimal_lorawan_join.py"
UPLINK_SCRIPT = APP_DIR / "lorawan_uplink.py"
LORA_LIB = Path.home() / "sx126x_lorawan_hat_code" / "python" / "lora"

JOIN_RETRY_DELAY = 30    # seconds between join retries
REJOIN_DELAY = 5         # seconds before re-joining after uplink exits

# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------

_shutdown = False
_current_proc: subprocess.Popen | None = None  # type: ignore[type-arg]


def _handle_signal(sig: int, frame: object) -> None:
    global _shutdown
    _shutdown = True
    if _current_proc and _current_proc.poll() is None:
        _current_proc.terminate()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _append_track(lat: float, lon: float, alt_m: float, fcnt: int) -> None:
    """Append a GPS fix to the persistent track file; trim to TRACK_MAX lines."""
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({"lat": lat, "lon": lon, "alt": alt_m,
                        "t": _now_iso(), "fcnt": fcnt})
    try:
        with TRACK_FILE.open("a") as f:
            f.write(entry + "\n")
        # Trim periodically: only when file has grown too large
        if TRACK_FILE.stat().st_size > 300_000:
            lines = TRACK_FILE.read_text().splitlines()
            TRACK_FILE.write_text("\n".join(lines[-TRACK_MAX:]) + "\n")
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M:%S")
    except Exception:
        return iso


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def _make_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(LORA_LIB) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _write_status(patch: dict) -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        existing: dict = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
    except Exception:
        existing = {}
    existing.update(patch)
    existing["updated"] = _now_iso()
    try:
        STATUS_FILE.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def _session_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.stem + "_session.json")


def _normalize_hex(value: object) -> str:
    return str(value).strip().replace(" ", "").replace(":", "").replace("-", "").upper()


def _join_fingerprint(config: dict) -> str | None:
    app_eui = config.get("app_eui") or config.get("appEui")
    dev_eui = config.get("dev_eui") or config.get("devEui")
    app_key = config.get("app_key") or config.get("appKey")
    if not app_eui or not dev_eui or not app_key:
        return None
    try:
        app_eui_le = bytes.fromhex(_normalize_hex(app_eui))[::-1]
        dev_eui_le = bytes.fromhex(_normalize_hex(dev_eui))[::-1]
        app_key_bytes = bytes.fromhex(_normalize_hex(app_key))
    except Exception:
        return None
    return hashlib.sha256(app_eui_le + dev_eui_le + app_key_bytes).hexdigest().upper()


def _load_session_snapshot(config_path: Path) -> dict | None:
    session_path = _session_path(config_path)
    try:
        session = json.loads(session_path.read_text())
    except Exception:
        return None

    if not isinstance(session, dict):
        return None

    if not session.get("dev_addr") or not session.get("nwk_skey") or not session.get("app_skey"):
        return None

    try:
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
    except Exception:
        config = {}
    current_fp = _join_fingerprint(config)
    cached_fp = str(session.get("join_fingerprint", "")).strip().upper() or None
    if current_fp is None or cached_fp is None or cached_fp != current_fp:
        return None

    try:
        dev_addr = str(session["dev_addr"]).strip()
        nwk_skey = str(session["nwk_skey"]).strip()
        app_skey = str(session["app_skey"]).strip()
        if len(bytes.fromhex(dev_addr)) != 4:  # DevAddr is 4 bytes (8 hex chars)
            return None
        if len(bytes.fromhex(nwk_skey)) != 16:
            return None
        if len(bytes.fromhex(app_skey)) != 16:
            return None
        if "fcnt_up" in session:
            int(session["fcnt_up"])
    except Exception:
        return None

    return session

# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def run_join(config_path: Path) -> bool:
    """Run the OTAA join script.  Returns True when a session file is produced."""
    global _current_proc
    _write_status({"state": "joining"})
    _log("Starting OTAA join…")

    cmd = [sys.executable, "-u", str(JOIN_SCRIPT), "--config", str(config_path)]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path.home()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=_make_env(),
        )
        _current_proc = proc
        for raw in proc.stdout:  # type: ignore[union-attr]
            _log(f"join: {raw.rstrip()}")
            if _shutdown:
                proc.terminate()
                break
        rc = proc.wait()
        _current_proc = None
    except Exception as exc:
        _current_proc = None
        _write_status({"state": "join_failed", "last_error": str(exc)})
        _log(f"Join error: {exc}")
        return False

    session_path = _session_path(config_path)
    if rc == 0 and session_path.exists():
        try:
            s: dict = json.loads(session_path.read_text())
            _write_status({
                "state": "joined",
                "dev_addr": s.get("dev_addr", "?"),
                "last_join": _now_iso(),
                "fcnt": s.get("fcnt_up", 0),
                "uplinks_sent": 0,
                "last_uplink": None,
                "last_ack": None,
                "link_status": "unknown",
            })
        except Exception:
            pass
        _log("Join succeeded.")
        return True
    else:
        _write_status({"state": "join_failed", "last_error": f"exit {rc}"})
        _log(f"Join failed (exit {rc}).")
        return False

# ---------------------------------------------------------------------------
# Uplink
# ---------------------------------------------------------------------------

def run_uplink(config_path: Path, interval: float, uplink_offset: int = 0) -> tuple[int, int]:
    """Run the GPS uplink script until it exits.  Returns (uplinks_sent, exit_code)."""
    global _current_proc
    _write_status({"state": "tracking"})
    _log("Starting GPS uplink loop…")

    session_path = _session_path(config_path)
    try:
        config: dict = json.loads(config_path.read_text()) if config_path.exists() else {}
    except Exception:
        config = {}
    uplink_sf = int(config.get("uplink_sf", 7))
    cmd = [
        sys.executable, "-u", str(UPLINK_SCRIPT),
        "--session", str(session_path),
        "--config", str(config_path),
        "--uplink-sf", str(uplink_sf),
        "--interval", str(interval),
    ]

    sent_this_run = 0
    rc = 0
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path.home()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=_make_env(),
        )
        _current_proc = proc
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip()
            _log(f"uplink: {line}")

            if line.startswith("gps_fix "):
                try:
                    parts = dict(p.split("=") for p in line.split() if "=" in p)
                    lat  = float(parts["lat"])
                    lon  = float(parts["lon"])
                    alt  = float(parts["alt"])
                    fcnt_now = int(parts.get("fcnt", 0))
                    _write_status({
                        "last_fix": {
                            "lat": lat,
                            "lon": lon,
                            "alt_m": alt,
                            "time": _now_iso(),
                        }
                    })
                    _append_track(lat, lon, alt, fcnt_now)
                except Exception:
                    pass

            elif line.startswith("uplink_sent "):
                try:
                    parts = dict(p.split("=") for p in line.split() if "=" in p)
                    acked = parts.get("ack", "0") == "1"
                    sent_this_run += 1
                    _write_status({
                        "fcnt": int(parts.get("new_fcnt", 0)),
                        "uplinks_sent": uplink_offset + sent_this_run,
                        "last_uplink": _now_iso(),
                        "link_status": "in_range" if acked else "no_ack",
                        "last_ack": _now_iso() if acked else _read_status().get("last_ack"),
                    })
                except Exception:
                    pass

            if _shutdown:
                proc.terminate()
                break

        rc = proc.wait()
        _current_proc = None
    except Exception as exc:
        _current_proc = None
        _write_status({"state": "error", "last_error": str(exc)})
        _log(f"Uplink error: {exc}")
        rc = 1

    return sent_this_run, rc

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="LoRaWAN GPS tracker service daemon")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="Join config JSON")
    p.add_argument("--interval", type=int, default=10, help="Uplink interval seconds")
    args = p.parse_args()

    config_path = Path(args.config)

    _write_status({
        "state": "starting",
        "pid": os.getpid(),
        "started": _now_iso(),
        "config": str(config_path),
        "last_fix": None,
        "uplinks_sent": 0,
    })
    _log(f"LoRaWAN tracker service starting — config: {config_path}")

    total_uplinks = 0
    cached_session = _load_session_snapshot(config_path)

    while not _shutdown:
        # ── Join ──────────────────────────────────────────────────────────
        if cached_session is None:
            if not run_join(config_path):
                _log(f"Retrying join in {JOIN_RETRY_DELAY} s…")
                for _ in range(JOIN_RETRY_DELAY):
                    if _shutdown:
                        break
                    time.sleep(1)
                continue
            cached_session = _load_session_snapshot(config_path)
        else:
            _write_status({
                "state": "joined",
                "dev_addr": cached_session.get("dev_addr", "?"),
                "fcnt": cached_session.get("fcnt_up", 0),
                "last_join": cached_session.get("last_join", _now_iso()),
                "uplinks_sent": cached_session.get("uplinks_sent", 0),
                "link_status": cached_session.get("link_status", "unknown"),
            })
            _log(f"Resuming from cached session dev_addr={cached_session.get('dev_addr', '?')}")

        if _shutdown:
            break

        # ── Uplink ────────────────────────────────────────────────────────
        try:
            config_for_interval: dict = json.loads(config_path.read_text()) if config_path.exists() else {}
        except Exception:
            config_for_interval = {}
        interval = float(config_for_interval.get("uplink_interval", args.interval))
        sent, rc = run_uplink(config_path, interval, uplink_offset=total_uplinks)
        total_uplinks += sent

        if _shutdown:
            break

        if rc != 0 or sent == 0:
            cached_session = None
            _log(f"Uplink worker exited (code {rc}, sent {sent}). Forcing re-join in {REJOIN_DELAY} s…")
            _write_status({"state": "reconnecting", "fcnt": 0, "uplinks_sent": 0, "link_status": "unknown"})
        else:
            _log(f"Uplink loop ended ({sent} sent). Re-joining in {REJOIN_DELAY} s…")
            _write_status({"state": "reconnecting", "fcnt": 0, "uplinks_sent": 0, "link_status": "unknown"})
        time.sleep(REJOIN_DELAY)

    _write_status({"state": "stopped"})
    _log("Service stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
