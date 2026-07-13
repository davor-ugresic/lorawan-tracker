#!/usr/bin/env python3
"""LoRaWAN GPS uplink transmitter for Waveshare SX126x HAT + L76K GNSS module.

Reads GPS coordinates from the onboard GNSS over UART and transmits them
as Cayenne LPP uplink frames using a previously obtained OTAA session.

Run lorawan_join_gui.py (or minimal_lorawan_join.py) first to obtain session keys,
then run this script with --session pointing at the saved session JSON.
"""

from __future__ import annotations

import argparse
import json
import signal
import struct
import time
from pathlib import Path

try:
    import serial  # type: ignore[import]
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

from LoRaRF import SX126x

# Ensure SIGTERM triggers finally blocks so radio.end() / gpio.cleanup() always runs.
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))

# ---------------------------------------------------------------------------
# Crypto  (mirrors minimal_lorawan_join.py — try pycryptodome first)
# ---------------------------------------------------------------------------

try:
    from Crypto.Cipher import AES  # type: ignore[import]
    from Crypto.Hash import CMAC as _CMAC_MOD  # type: ignore[import]

    def aes_ecb_encrypt_block(key: bytes, block: bytes) -> bytes:
        return AES.new(key, AES.MODE_ECB).encrypt(block)

    def aes_cmac(key: bytes, data: bytes) -> bytes:
        mac = _CMAC_MOD.new(key, ciphermod=AES)
        mac.update(data)
        return mac.digest()

except ImportError:
    from cryptography.hazmat.backends import default_backend  # type: ignore[import]
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore[import]
    from cryptography.hazmat.primitives.cmac import CMAC as _CMAC2  # type: ignore[import]

    def aes_ecb_encrypt_block(key: bytes, block: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
        enc = cipher.encryptor()
        return enc.update(block) + enc.finalize()

    def aes_cmac(key: bytes, data: bytes) -> bytes:
        mac = _CMAC2(algorithms.AES(key), backend=default_backend())
        mac.update(data)
        return mac.finalize()

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_BUS = 0
DEFAULT_CS = 0
DEFAULT_RESET_PIN = 18
DEFAULT_BUSY_PIN = 20
DEFAULT_IRQ_PIN = 16
DEFAULT_TXEN_PIN = 6
DEFAULT_RXEN_PIN = -1
DEFAULT_TX_POWER = 14
DEFAULT_SYNC_WORD = 0x3444
DEFAULT_UPLINK_FREQUENCY = 868_300_000
DEFAULT_UPLINK_SF = 7
DEFAULT_UPLINK_BW = 125_000
DEFAULT_UPLINK_CR = 5
DEFAULT_UPLINK_PREAMBLE = 8
DEFAULT_TX_TIMEOUT = 10.0
DEFAULT_CONFIRMED_UPLINK = False
DEFAULT_GPS_PORT = "/dev/ttyAMA0"
DEFAULT_GPS_BAUD = 9600
DEFAULT_GPS_POWER_PIN = -1   # -1 = skip; set to 4 for Waveshare HAT power key GPIO
DEFAULT_GPS_TIMEOUT = 90.0
DEFAULT_INTERVAL = 10.0
DEFAULT_FPORT = 1
DEFAULT_RX1_TIMEOUT = 4.0   # seconds to poll for ACK in RX1 (>1 s to absorb server/gateway jitter)
DEFAULT_RX2_TIMEOUT = 2.0
DEFAULT_RX2_FREQUENCY = 869_525_000

# Standard EU868 uplink channels — rotate across them
EU868_UPLINK_CHANNELS = (868_100_000, 868_300_000, 868_500_000)

# ---------------------------------------------------------------------------
# LoRaWAN 1.0 uplink frame builder
# ---------------------------------------------------------------------------

def _encrypt_frm_payload(app_skey: bytes, dev_addr_le: bytes, fcnt: int, payload: bytes) -> bytes:
    """AES-128 counter-mode encryption per LoRaWAN 1.0 spec §4.3.3 (uplink, dir=0)."""
    blocks = (len(payload) + 15) // 16
    keystream = bytearray()
    for i in range(1, blocks + 1):
        A = (bytes([0x01, 0, 0, 0, 0, 0])
             + dev_addr_le
             + struct.pack("<I", fcnt)
             + bytes([0x00, i]))
        keystream += aes_ecb_encrypt_block(app_skey, A)
    return bytes(a ^ b for a, b in zip(payload, keystream))


def _compute_mic(nwk_skey: bytes, msg: bytes, dev_addr_le: bytes, fcnt: int) -> bytes:
    """CMAC MIC per LoRaWAN 1.0 spec §4.4 (uplink, dir=0)."""
    B0 = (bytes([0x49, 0, 0, 0, 0, 0])
          + dev_addr_le
          + struct.pack("<I", fcnt)
          + bytes([0x00, len(msg)]))
    return aes_cmac(nwk_skey, B0 + msg)[:4]


def build_uplink_frame(
    dev_addr_le: bytes,
    nwk_skey: bytes,
    app_skey: bytes,
    fcnt: int,
    port: int,
    payload: bytes,
    confirmed: bool = False,
) -> bytes:
    """Return a complete LoRaWAN data-up PHY frame."""
    mhdr = bytes([0x80 if confirmed else 0x40])                  # MType=100 confirmed or 010 unconfirmed
    fhdr = dev_addr_le + bytes([0x00]) + struct.pack("<H", fcnt & 0xFFFF)
    fport = bytes([port])
    enc_payload = _encrypt_frm_payload(app_skey, dev_addr_le, fcnt, payload)
    msg = mhdr + fhdr + fport + enc_payload
    return msg + _compute_mic(nwk_skey, msg, dev_addr_le, fcnt)


def _downlink_is_ack(raw: bytes, dev_addr_le: bytes) -> bool:
    """Return True when the received downlink is an ACK for our DevAddr."""
    if len(raw) < 12:
        return False
    if raw[1:5] != dev_addr_le:
        return False
    if raw[0] not in (0x60, 0xA0):
        return False
    return bool(raw[5] & 0x20)


def _wait_for_ack(
    radio: SX126x,
    dev_addr_le: bytes,
    frequency: int,
    spreading_factor: int,
    bandwidth: int,
    coding_rate: int,
    timeout: float,
    label: str,
) -> bool:
    """Wait for a confirmed downlink ACK in the current RX window."""
    ldro = (2 ** spreading_factor / bandwidth) > 0.016
    radio.setFrequency(frequency)
    radio.setLoRaModulation(spreading_factor, bandwidth, coding_rate, ldro)
    radio.setLoRaPacket(radio.HEADER_EXPLICIT, DEFAULT_UPLINK_PREAMBLE, 255, False, True)
    radio.request(radio.RX_CONTINUOUS)

    start = time.time()
    while (time.time() - start) < timeout:
        irq = radio.getIrqStatus()
        if irq & (radio.IRQ_RX_DONE | radio.IRQ_CRC_ERR | radio.IRQ_HEADER_ERR):
            (radio._payloadTxRx, radio._bufferIndex) = radio.getRxBufferStatus()
            pkt_len = radio.available()
            if irq & radio.IRQ_RX_DONE and pkt_len > 0:
                raw = radio.get(pkt_len)
                if _downlink_is_ack(raw, dev_addr_le):
                    print(
                        f"uplink_down window={label} ack=1 len={pkt_len} "
                        f"raw={raw.hex().upper()}"
                    )
                    radio.clearIrqStatus(0x03FF)
                    return True
                mhdr = raw[0] if raw else 0
                print(
                    f"uplink_down window={label} ack=0 mhdr=0x{mhdr:02x} "
                    f"len={pkt_len} raw={raw.hex().upper()}"
                )
            radio.clearIrqStatus(0x03FF)
        time.sleep(0.01)

    print(f"uplink_no_ack window={label} timeout={timeout:.1f}s")
    return False

# ---------------------------------------------------------------------------
# Cayenne LPP GPS payload  (channel=1, type=0x88, total 11 bytes)
# Latitude/longitude resolution: 0.0001°  Altitude resolution: 0.01 m
# ---------------------------------------------------------------------------

def encode_cayenne_lpp_gps(lat: float, lon: float, alt_m: float, channel: int = 1) -> bytes:
    _I24_MIN, _I24_MAX = -8_388_608, 8_388_607

    def i24(v: int) -> bytes:
        return max(_I24_MIN, min(_I24_MAX, v)).to_bytes(3, "big", signed=True)

    return (bytes([channel, 0x88])
            + i24(round(lat * 10_000))
            + i24(round(lon * 10_000))
            + i24(round(alt_m * 100)))

# ---------------------------------------------------------------------------
# GPS reader — NMEA $G?GGA over UART
# ---------------------------------------------------------------------------

def _parse_gga(sentence: str) -> tuple[float, float, float] | None:
    """Parse a $GPGGA or $GNGGA sentence.  Returns (lat°, lon°, alt_m) or None."""
    try:
        f = sentence.split(",")
        if len(f) < 10 or not f[6] or int(f[6]) == 0:
            return None
        lat_raw, lat_dir = f[2], f[3]
        lon_raw, lon_dir = f[4], f[5]
        alt_raw = f[9]
        if not lat_raw or not lon_raw:
            return None
        lat = float(lat_raw[:2]) + float(lat_raw[2:]) / 60.0
        if lat_dir == "S":
            lat = -lat
        lon = float(lon_raw[:3]) + float(lon_raw[3:]) / 60.0
        if lon_dir == "W":
            lon = -lon
        return lat, lon, float(alt_raw) if alt_raw else 0.0
    except (ValueError, IndexError):
        return None
def _parse_rmc(sentence: str) -> tuple[float, float, float] | None:
    """Parse a $GPRMC or $GNRMC sentence.  Returns (lat°, lon°, 0.0) or None."""
    try:
        f = sentence.split(",")
        if len(f) < 7 or f[2] != "A":  # 'A' = active / valid fix
            return None
        lat_raw, lat_dir = f[3], f[4]
        lon_raw, lon_dir = f[5], f[6].split(".")[0] if "." in f[6] else f[6]
        if not lat_raw or not lon_raw:
            return None
        lat = float(lat_raw[:2]) + float(lat_raw[2:]) / 60.0
        if lat_dir == "S":
            lat = -lat
        lon = float(lon_raw[:3]) + float(lon_raw[3:]) / 60.0
        if lon_dir == "W":
            lon = -lon
        return lat, lon, 0.0  # RMC carries no altitude
    except (ValueError, IndexError):
        return None


def _detect_gps_port(requested: str) -> str:
    """Return requested port if it exists; otherwise auto-detect a GPS UART."""
    if Path(requested).exists():
        return requested
    for candidate in ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0", "/dev/ttyUSB0"):
        if Path(candidate).exists():
            print(f"gps_autodetect {requested} not found, using {candidate}")
            return candidate
    return requested  # will fail at open time with a clear OS error


def _power_on_gps(pin: int) -> None:
    """Assert GPIO pin HIGH (lgpio) to enable GPS power before radio init."""
    try:
        import lgpio  # type: ignore[import]
        h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(h, pin)
        lgpio.gpio_write(h, pin, 1)
        lgpio.gpiochip_close(h)
        print(f"gps_power pin={pin} HIGH")
        time.sleep(0.5)
    except Exception as exc:
        print(f"gps_power_warning pin={pin}: {exc}")


def read_gps_fix(port: str, baud: int, timeout: float) -> tuple[float, float, float] | None:
    """Block until a valid GGA fix arrives or timeout expires."""
    if not HAS_SERIAL:
        print("gps_error pyserial not installed — cannot read GPS UART")
        return None
    try:
        with serial.Serial(port, baud, timeout=1.0) as ser:
            deadline = time.time() + timeout
            print(f"gps_status=waiting port={port} timeout={int(timeout)}s")
            while time.time() < deadline:
                try:
                    line = ser.readline().decode("ascii", errors="replace").strip()
                    if line.startswith(("$GPGGA", "$GNGGA")):
                        fix = _parse_gga(line)
                        if fix is not None:
                            return fix
                    elif line.startswith(("$GPRMC", "$GNRMC")):
                        fix = _parse_rmc(line)
                        if fix is not None:
                            return fix
                except Exception:
                    pass
    except Exception as exc:
        print(f"gps_error {exc}")
    return None

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="LoRaWAN GPS uplink for Waveshare SX126x + L76K HAT"
    )
    p.add_argument("--session", required=True,
                   help="Session JSON produced by the join script")
    p.add_argument("--config", default=None,
                   help="Join config JSON (radio pin / power settings)")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help="Seconds between uplinks (default %(default)s)")
    p.add_argument("--count", type=int, default=0,
                   help="Stop after N uplinks; 0 = run forever")
    p.add_argument("--confirmed", dest="confirmed", action="store_true",
                   help="Send confirmed uplinks and wait for ACKs")
    p.add_argument("--unconfirmed", dest="confirmed", action="store_false",
                   help="Send unconfirmed uplinks")
    p.set_defaults(confirmed=None)
    p.add_argument("--fport", type=int, default=DEFAULT_FPORT,
                   help="LoRaWAN FPort (default %(default)s)")
    p.add_argument("--uplink-sf", type=int, default=DEFAULT_UPLINK_SF,
                   help="Spreading factor (default %(default)s)")
    p.add_argument("--uplink-bw", type=int, default=DEFAULT_UPLINK_BW,
                   help="Bandwidth Hz (default %(default)s)")
    p.add_argument("--gps-port", default=DEFAULT_GPS_PORT,
                   help="GPS UART device (default %(default)s)")
    p.add_argument("--gps-baud", type=int, default=DEFAULT_GPS_BAUD,
                   help="GPS UART baud rate (default %(default)s)")
    p.add_argument("--gps-power-pin", type=int, default=DEFAULT_GPS_POWER_PIN,
                   help="BCM GPIO for GPS power-key; -1 = skip (default %(default)s)")
    p.add_argument("--gps-timeout", type=float, default=DEFAULT_GPS_TIMEOUT,
                   help="Max seconds to wait for GPS fix (default %(default)s)")
    # Radio hardware — all overridden by --config if present
    p.add_argument("--bus", type=int, default=DEFAULT_BUS)
    p.add_argument("--cs", type=int, default=DEFAULT_CS)
    p.add_argument("--reset-pin", type=int, default=DEFAULT_RESET_PIN)
    p.add_argument("--busy-pin", type=int, default=DEFAULT_BUSY_PIN)
    p.add_argument("--irq-pin", type=int, default=DEFAULT_IRQ_PIN)
    p.add_argument("--txen-pin", type=int, default=DEFAULT_TXEN_PIN)
    p.add_argument("--rxen-pin", type=int, default=DEFAULT_RXEN_PIN)
    p.add_argument("--tx-power", type=int, default=DEFAULT_TX_POWER)
    p.add_argument("--sync-word", type=lambda x: int(x, 0), default=DEFAULT_SYNC_WORD)
    p.add_argument("--tx-timeout", type=float, default=DEFAULT_TX_TIMEOUT)
    return p

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_parser().parse_args()

    # Apply radio settings from config file (same keys as lorawan_join.json)
    if args.config:
        cfg = _load_json(Path(args.config))
        for attr, key in [
            ("bus", "bus"), ("cs", "cs"), ("reset_pin", "reset_pin"),
            ("busy_pin", "busy_pin"), ("irq_pin", "irq_pin"),
            ("txen_pin", "txen_pin"), ("rxen_pin", "rxen_pin"),
            ("tx_power", "tx_power"),
        ]:
            if key in cfg:
                setattr(args, attr, int(cfg[key]))
        if "tx_timeout" in cfg:
            args.tx_timeout = float(cfg["tx_timeout"])
        if "sync_word" in cfg:
            raw = str(cfg["sync_word"]).strip()
            args.sync_word = int(raw, 0) if raw.lower().startswith("0x") else int(raw)
        if "confirmed_uplink" in cfg:
            args.confirmed = bool(cfg["confirmed_uplink"])
        if "gps_port" in cfg and str(cfg["gps_port"]).strip():
            args.gps_port = str(cfg["gps_port"]).strip()
        if "gps_timeout" in cfg:
            args.gps_timeout = float(cfg["gps_timeout"])
        if "uplink_frequency" in cfg:
            args.uplink_frequency = int(cfg["uplink_frequency"])
        else:
            args.uplink_frequency = DEFAULT_UPLINK_FREQUENCY
        _uplink_channels: tuple[int, ...] = (
            EU868_UPLINK_CHANNELS if cfg.get("scan_eu868") else (args.uplink_frequency,)
        )
    else:
        args.uplink_frequency = DEFAULT_UPLINK_FREQUENCY
        _uplink_channels = EU868_UPLINK_CHANNELS  # default to EU868 channel rotation

    if args.confirmed is None:
        args.confirmed = DEFAULT_CONFIRMED_UPLINK

    # Load session keys
    session_path = Path(args.session)
    session = _load_json(session_path)
    dev_addr_hex: str = session["dev_addr"]
    nwk_skey = bytes.fromhex(session["nwk_skey"])
    app_skey = bytes.fromhex(session["app_skey"])
    # Session stores DevAddr big-endian (human-readable); frame needs little-endian
    dev_addr_le = bytes.fromhex(dev_addr_hex)[::-1]
    fcnt = int(session.get("fcnt_up", 0))

    print(f"session loaded dev_addr={dev_addr_hex} fcnt_up={fcnt}")
    print(f"confirmed_uplink={int(bool(args.confirmed))}")

    # Auto-detect GPS port if the configured device doesn't exist
    args.gps_port = _detect_gps_port(args.gps_port)

    # Power GPS before opening the lgpio chip for the radio
    if args.gps_power_pin >= 0:
        _power_on_gps(args.gps_power_pin)

    # Initialize radio
    radio = SX126x()
    print("initializing radio...")
    if not radio.begin(
        args.bus, args.cs, args.reset_pin, args.busy_pin,
        args.irq_pin, args.txen_pin, args.rxen_pin,
    ):
        raise RuntimeError("Could not initialize SX126x radio")

    try:
        radio.setDio2RfSwitch()
        radio.setTxPower(args.tx_power, radio.TX_POWER_SX1262)
        radio.setSyncWord(args.sync_word)

        sent = 0
        pending: dict | None = None
        _ch_idx = 0  # EU868 uplink channel index; advances per successfully transmitted frame

        while args.count == 0 or sent < args.count:
            if pending is None:
                fix = read_gps_fix(args.gps_port, args.gps_baud, args.gps_timeout)

                if fix is None:
                    print("gps_status=no_fix sending 0.0 0.0")
                    fix = (0.0, 0.0, 0.0)

                lat, lon, alt = fix
                print(f"gps_fix lat={lat:.6f} lon={lon:.6f} alt={alt:.1f}")

                payload = encode_cayenne_lpp_gps(lat, lon, alt)
                pending = {
                    "lat": lat,
                    "lon": lon,
                    "alt": alt,
                    "payload": payload,
                    "freq": _uplink_channels[_ch_idx % len(_uplink_channels)],
                    "fcnt": fcnt,
                }

            if pending is not None:
                lat = pending["lat"]
                lon = pending["lon"]
                alt = pending["alt"]
                payload = pending["payload"]
                freq = pending["freq"]

                ldro = (2 ** args.uplink_sf / args.uplink_bw) > 0.016
                radio.setFrequency(freq)
                radio.setLoRaModulation(args.uplink_sf, args.uplink_bw, DEFAULT_UPLINK_CR, ldro)
                radio.setLoRaPacket(radio.HEADER_EXPLICIT, DEFAULT_UPLINK_PREAMBLE, 255, True, False)

                frame = build_uplink_frame(
                    dev_addr_le, nwk_skey, app_skey, pending["fcnt"], args.fport, payload,
                    confirmed=bool(args.confirmed),
                )
                mode = "confirmed" if args.confirmed else "unconfirmed"
                print(
                    f"uplink_up mode={mode} fcnt={pending['fcnt']} freq={freq} sf={args.uplink_sf} "
                    f"len={len(frame)} payload={payload.hex().upper()} frame={frame.hex().upper()}"
                )

                radio.beginPacket()
                radio.put(frame)
                radio.endPacket()

                if radio.wait(args.tx_timeout):
                    radio.clearIrqStatus(0x03FF)  # clear TX_DONE before opening RX window
                    acked = True
                    if args.confirmed:
                        # RX1: same frequency / data rate; RX2: fixed EU868 downlink slot.
                        acked = _wait_for_ack(
                            radio, dev_addr_le, freq, args.uplink_sf, args.uplink_bw,
                            DEFAULT_UPLINK_CR, DEFAULT_RX1_TIMEOUT, "RX1",
                        ) or _wait_for_ack(
                            radio, dev_addr_le, DEFAULT_RX2_FREQUENCY, 12, 125_000,
                            DEFAULT_UPLINK_CR, DEFAULT_RX2_TIMEOUT, "RX2",
                        )

                    if acked or not args.confirmed:
                        fcnt += 1
                        session["fcnt_up"] = fcnt
                        _save_json(session_path, session)
                        sent += 1
                        print(f"uplink_sent seq={sent} new_fcnt={fcnt} ack={int(acked)}")
                        pending = None
                        _ch_idx += 1  # advance to next EU868 channel
                    else:
                        print(f"uplink_retry_pending fcnt={pending['fcnt']}")
                else:
                    print("uplink_timeout TX timed out — FCnt not incremented")

            if args.count != 0 and sent >= args.count:
                break

            print(f"uplink_sleep interval={int(args.interval)}s")
            time.sleep(args.interval)

    finally:
        radio.end()

    print(f"uplink_done total={sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
