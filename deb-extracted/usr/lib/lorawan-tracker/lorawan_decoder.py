#!/usr/bin/env python3
"""LoRaWAN 1.0 uplink decoder for the Waveshare SX126x GPS tracker.

Decodes a raw LoRaWAN PHY frame received by the Semtech UDP server and
extracts the Cayenne LPP GPS payload produced by lorawan_uplink.py.

LoRaWAN frame layout (unconfirmed data-up, MHDR=0x40):
  MHDR(1) | DevAddr(4 LE) | FCtrl(1) | FCnt(2 LE) | FOpts(0-15) | FPort(1) | FRMPayload(N) | MIC(4)

Cayenne LPP GPS type 0x88 (11 bytes):
  channel(1) | 0x88(1) | lat_i24(3 BE, °×10000) | lon_i24(3 BE, °×10000) | alt_i24(3 BE, m×100)

Standalone usage:
  # Decode a base64 PHY payload (as it appears in rxpk.data from the gateway):
  python3 lorawan_decoder.py --session lorawan_join_session.json --b64 QMcBaOhAAAAB...

  # Or hex:
  python3 lorawan_decoder.py --session lorawan_join_session.json --hex 40c7016ae8...

Server integration — call decode_uplink() for each rxpk received:
  import base64, lorawan_decoder as dec
  result = dec.decode_uplink(base64.b64decode(rxpk["data"]), nwk_skey, app_skey)
  if result and result["gps"]:
      lat, lon, alt = result["gps"]["lat"], result["gps"]["lon"], result["gps"]["alt_m"]
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
from pathlib import Path

# ---------------------------------------------------------------------------
# Crypto  (try pycryptodome, fall back to cryptography)
# ---------------------------------------------------------------------------

try:
    from Crypto.Cipher import AES  # type: ignore[import]
    from Crypto.Hash import CMAC as _CMAC_MOD  # type: ignore[import]

    def _aes_ecb_encrypt(key: bytes, block: bytes) -> bytes:
        return AES.new(key, AES.MODE_ECB).encrypt(block)

    def _aes_cmac(key: bytes, data: bytes) -> bytes:
        mac = _CMAC_MOD.new(key, ciphermod=AES)
        mac.update(data)
        return mac.digest()

except ImportError:
    from cryptography.hazmat.backends import default_backend  # type: ignore[import]
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore[import]
    from cryptography.hazmat.primitives.cmac import CMAC as _CMAC2  # type: ignore[import]

    def _aes_ecb_encrypt(key: bytes, block: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
        enc = cipher.encryptor()
        return enc.update(block) + enc.finalize()

    def _aes_cmac(key: bytes, data: bytes) -> bytes:
        mac = _CMAC2(algorithms.AES(key), backend=default_backend())
        mac.update(data)
        return mac.finalize()

# ---------------------------------------------------------------------------
# LoRaWAN 1.0 frame parsing
# ---------------------------------------------------------------------------

class FrameError(ValueError):
    """Raised when the PHY frame cannot be decoded."""


def _decrypt_frm_payload(key: bytes, dev_addr_le: bytes, fcnt: int, payload: bytes) -> bytes:
    """AES-128 counter-mode decryption per LoRaWAN 1.0 §4.3.3 (uplink, dir=0).
    Encryption and decryption use the same operation."""
    blocks = (len(payload) + 15) // 16
    keystream = bytearray()
    for i in range(1, blocks + 1):
        A = (bytes([0x01, 0, 0, 0, 0, 0])
             + dev_addr_le
             + struct.pack("<I", fcnt)
             + bytes([0x00, i]))
        keystream += _aes_ecb_encrypt(key, A)
    return bytes(a ^ b for a, b in zip(payload, keystream))


def _verify_mic(nwk_skey: bytes, msg: bytes, dev_addr_le: bytes, fcnt: int) -> tuple[bool, bytes]:
    """Compute and compare the 4-byte MIC per LoRaWAN 1.0 §4.4 (uplink, dir=0)."""
    B0 = (bytes([0x49, 0, 0, 0, 0, 0])
          + dev_addr_le
          + struct.pack("<I", fcnt)
          + bytes([0x00, len(msg)]))
    expected = _aes_cmac(nwk_skey, B0 + msg)[:4]
    return expected, expected


def _compute_mic(nwk_skey: bytes, msg: bytes, dev_addr_le: bytes, fcnt: int) -> bytes:
    B0 = (bytes([0x49, 0, 0, 0, 0, 0])
          + dev_addr_le
          + struct.pack("<I", fcnt)
          + bytes([0x00, len(msg)]))
    return _aes_cmac(nwk_skey, B0 + msg)[:4]


def parse_frame(phy: bytes) -> dict:
    """Split a raw PHY payload into its structural fields.

    Returns a dict with keys:
      mhdr, mtype, dev_addr_le, dev_addr_hex, fctrl, fcnt,
      fopts, fport, frm_payload_enc, mic_received
    """
    if len(phy) < 13:
        raise FrameError(f"PHY too short ({len(phy)} bytes, need ≥13)")

    mhdr = phy[0]
    mtype = (mhdr >> 5) & 0x07

    # DevAddr in FHDR is little-endian
    dev_addr_le = phy[1:5]
    dev_addr_hex = dev_addr_le[::-1].hex().upper()

    fctrl = phy[5]
    fcnt_low = struct.unpack_from("<H", phy, 6)[0]   # 16-bit FCnt from frame
    foptslen = fctrl & 0x0F
    fopts = phy[8 : 8 + foptslen]

    # After FHDR: FPort + FRMPayload + MIC
    body_start = 8 + foptslen
    if len(phy) < body_start + 1 + 4:
        raise FrameError("PHY too short to contain FPort + MIC")

    fport = phy[body_start]
    frm_payload_enc = phy[body_start + 1 : -4]
    mic_received = phy[-4:]

    return {
        "mhdr": mhdr,
        "mtype": mtype,
        "dev_addr_le": dev_addr_le,
        "dev_addr_hex": dev_addr_hex,
        "fctrl": fctrl,
        "fcnt_low": fcnt_low,
        "fopts": fopts,
        "fport": fport,
        "frm_payload_enc": frm_payload_enc,
        "mic_received": mic_received,
    }


# ---------------------------------------------------------------------------
# Cayenne LPP GPS parser
# ---------------------------------------------------------------------------

def _i24(b: bytes) -> int:
    """Decode a 3-byte big-endian signed integer."""
    val = int.from_bytes(b, "big")
    return val - (1 << 24) if val >= (1 << 23) else val


def parse_cayenne_lpp(payload: bytes) -> list[dict]:
    """Parse all Cayenne LPP records from a payload.

    Each GPS record (type 0x88) returns:
      {"channel": int, "type": "gps",
       "lat": float, "lon": float, "alt_m": float}
    """
    records = []
    i = 0
    while i < len(payload):
        if i + 1 >= len(payload):
            break
        channel = payload[i]
        lpp_type = payload[i + 1]

        if lpp_type == 0x88:          # GPS location — 9 data bytes
            if i + 11 > len(payload):
                break
            lat = _i24(payload[i + 2 : i + 5]) / 10_000.0
            lon = _i24(payload[i + 5 : i + 8]) / 10_000.0
            alt = _i24(payload[i + 8 : i + 11]) / 100.0
            records.append({
                "channel": channel,
                "type": "gps",
                "lat": lat,
                "lon": lon,
                "alt_m": alt,
            })
            i += 11
        elif lpp_type == 0x67:        # Temperature — 2 data bytes
            if i + 4 > len(payload):
                break
            temp = struct.unpack_from(">h", payload, i + 2)[0] / 10.0
            records.append({"channel": channel, "type": "temperature", "celsius": temp})
            i += 4
        elif lpp_type == 0x68:        # Humidity — 1 data byte
            if i + 3 > len(payload):
                break
            records.append({"channel": channel, "type": "humidity", "percent": payload[i + 2] / 2.0})
            i += 3
        else:
            # Unknown type — stop parsing
            break
    return records


# ---------------------------------------------------------------------------
# Main decode entry point
# ---------------------------------------------------------------------------

def decode_uplink(
    phy: bytes,
    nwk_skey: bytes,
    app_skey: bytes,
    fcnt_full: int | None = None,
) -> dict | None:
    """Decode a LoRaWAN uplink PHY frame.

    Args:
        phy:        Raw PHY bytes (as received from gateway, no base64).
        nwk_skey:   Network Session Key (16 bytes).
        app_skey:   Application Session Key (16 bytes).
        fcnt_full:  Known 32-bit FCnt (server-side counter). If None, uses the
                    16-bit FCnt from the frame (fine for FCnt < 65536).

    Returns a dict:
        {
          "dev_addr":   "E86A0701",     # big-endian hex
          "mtype":      2,              # 2=unconfirmed up, 4=confirmed up
          "fport":      1,
          "fcnt":       42,
          "mic_ok":     True,
          "payload_hex": "0188...",     # raw decrypted payload hex
          "gps": {                      # None if no GPS LPP record
              "lat": 45.12345,
              "lon": 19.84321,
              "alt_m": 88.0
          },
          "lpp_records": [...]          # all parsed LPP records
        }
    Returns None if the frame cannot be parsed at all.
    """
    try:
        f = parse_frame(phy)
    except FrameError as exc:
        print(f"[decoder] frame parse error: {exc}")
        return None

    fcnt = fcnt_full if fcnt_full is not None else f["fcnt_low"]

    # Verify MIC
    msg_for_mic = phy[:-4]
    computed_mic = _compute_mic(nwk_skey, msg_for_mic, f["dev_addr_le"], fcnt)
    mic_ok = computed_mic == f["mic_received"]

    # Decrypt FRMPayload (using AppSKey for FPort ≠ 0, NwkSKey for FPort=0)
    key = app_skey if f["fport"] != 0 else nwk_skey
    payload = _decrypt_frm_payload(key, f["dev_addr_le"], fcnt, f["frm_payload_enc"])

    # Parse Cayenne LPP records
    lpp_records = parse_cayenne_lpp(payload)
    gps = next((r for r in lpp_records if r["type"] == "gps"), None)

    return {
        "dev_addr":    f["dev_addr_hex"],
        "mtype":       f["mtype"],
        "fport":       f["fport"],
        "fcnt":        fcnt,
        "fcnt_low":    f["fcnt_low"],
        "mic_ok":      mic_ok,
        "payload_hex": payload.hex().upper(),
        "gps":         gps,
        "lpp_records": lpp_records,
    }


# ---------------------------------------------------------------------------
# Semtech UDP server integration example
# ---------------------------------------------------------------------------

def handle_push_data(rxpk_list: list[dict], nwk_skey: bytes, app_skey: bytes) -> list[dict]:
    """Process the rxpk list from a Semtech PUSH_DATA packet.

    Call this inside your server's packet handler when you receive a PUSH_DATA.
    Returns a list of decoded uplink results (one per packet).

    Example server integration:
        import base64, json, lorawan_decoder as dec

        NWK_SKEY = bytes.fromhex(session["nwk_skey"])
        APP_SKEY = bytes.fromhex(session["app_skey"])

        # In your PUSH_DATA handler:
        body = json.loads(raw_body)
        results = dec.handle_push_data(body.get("rxpk", []), NWK_SKEY, APP_SKEY)
        for r in results:
            if r and r["mic_ok"] and r["gps"]:
                lat, lon, alt = r["gps"]["lat"], r["gps"]["lon"], r["gps"]["alt_m"]
                print(f"GPS update: {lat:.5f}, {lon:.5f}  alt {alt:.0f}m  fcnt={r['fcnt']}")
    """
    results = []
    for pkt in rxpk_list:
        raw = base64.b64decode(pkt.get("data", ""))
        result = decode_uplink(raw, nwk_skey, app_skey)
        if result:
            result["freq"] = pkt.get("freq")
            result["rssi"] = pkt.get("rssi")
            result["snr"]  = pkt.get("lsnr")
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# CLI — standalone test tool
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Decode a LoRaWAN uplink frame")
    p.add_argument("--session", required=True,
                   help="Session JSON file (dev_addr, nwk_skey, app_skey)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--b64", help="Base64-encoded PHY payload (as in rxpk.data)")
    src.add_argument("--hex", dest="hexstr", help="Hex-encoded PHY payload")
    p.add_argument("--fcnt", type=int, default=None,
                   help="Full 32-bit FCnt (optional; defaults to 16-bit value from frame)")
    args = p.parse_args()

    session = json.loads(Path(args.session).read_text())
    nwk_skey = bytes.fromhex(session["nwk_skey"])
    app_skey = bytes.fromhex(session["app_skey"])

    phy = base64.b64decode(args.b64) if args.b64 else bytes.fromhex(args.hexstr)

    print(f"PHY ({len(phy)} bytes): {phy.hex().upper()}")
    result = decode_uplink(phy, nwk_skey, app_skey, fcnt_full=args.fcnt)

    if result is None:
        print("Could not decode frame.")
        return 1

    print(f"DevAddr : {result['dev_addr']}")
    print(f"FCnt    : {result['fcnt']}  (low16={result['fcnt_low']})")
    print(f"FPort   : {result['fport']}")
    print(f"MIC     : {'OK ✓' if result['mic_ok'] else 'FAIL ✗'}")
    print(f"Payload : {result['payload_hex']}")

    if result["gps"]:
        g = result["gps"]
        print(f"\nGPS fix :")
        print(f"  Latitude  : {g['lat']:.6f}°")
        print(f"  Longitude : {g['lon']:.6f}°")
        print(f"  Altitude  : {g['alt_m']:.1f} m")
    else:
        print("\nNo GPS record in payload.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
