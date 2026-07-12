#!/usr/bin/env python3
"""Minimal LoRaWAN OTAA join script for the Waveshare SX126x HAT.

This script only performs the join-request / join-accept exchange.
It does not send uplinks after joining.
"""

from __future__ import annotations

import argparse
import binascii
import getpass
import json
import hashlib
import secrets
import signal
import time
from pathlib import Path

from LoRaRF import SX126x

# Ensure SIGTERM triggers finally blocks so radio.end() / gpio.cleanup() always runs.
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))

try:
    from Crypto.Cipher import AES
    from Crypto.Hash import CMAC

    def aes_ecb_encrypt_block(key: bytes, block: bytes) -> bytes:
        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.encrypt(block)

    def aes_ecb_decrypt_block(key: bytes, block: bytes) -> bytes:
        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.decrypt(block)

    def aes_cmac(key: bytes, data: bytes) -> bytes:
        mac = CMAC.new(key, ciphermod=AES)
        mac.update(data)
        return mac.digest()

except ImportError:  # pragma: no cover
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.cmac import CMAC

    def aes_ecb_encrypt_block(key: bytes, block: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
        encryptor = cipher.encryptor()
        return encryptor.update(block) + encryptor.finalize()

    def aes_ecb_decrypt_block(key: bytes, block: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
        decryptor = cipher.decryptor()
        return decryptor.update(block) + decryptor.finalize()

    def aes_cmac(key: bytes, data: bytes) -> bytes:
        mac = CMAC(algorithms.AES(key), backend=default_backend())
        mac.update(data)
        return mac.finalize()


DEFAULT_BUS = 0
DEFAULT_CS = 0
DEFAULT_RESET_PIN = 18
DEFAULT_BUSY_PIN = 20
DEFAULT_IRQ_PIN = 16
DEFAULT_TXEN_PIN = 6
DEFAULT_RXEN_PIN = -1
DEFAULT_JOIN_FREQUENCY = 868_100_000
DEFAULT_JOIN_SPREADING_FACTOR = 12
DEFAULT_JOIN_BANDWIDTH = 125_000
DEFAULT_JOIN_CODING_RATE = 5
DEFAULT_JOIN_PREAMBLE = 8
DEFAULT_PUBLIC_SYNC_WORD = 0x3444
DEFAULT_TX_POWER = 14
DEFAULT_JOIN_ACCEPT_TIMEOUT = 12.0
DEFAULT_TX_TIMEOUT = 10.0

EU868_JOIN_CHANNELS = (
    (868_100_000, 125_000, 12),
    (868_300_000, 125_000, 12),
    (868_500_000, 125_000, 12),
    (867_100_000, 125_000, 12),
    (867_300_000, 125_000, 12),
    (867_500_000, 125_000, 12),
    (867_700_000, 125_000, 12),
    (867_900_000, 125_000, 12),
)


def normalize_hex(text: str) -> str:
    value = text.strip().lower().replace(" ", "").replace(":", "").replace("-", "")
    if value.startswith("0x"):
        value = value[2:]
    return value


def parse_hex_bytes(text: str, expected_length: int, name: str) -> bytes:
    value = normalize_hex(text)
    try:
        data = binascii.unhexlify(value)
    except binascii.Error as exc:
        raise ValueError(f"{name} must be hex encoded") from exc
    if len(data) != expected_length:
        raise ValueError(f"{name} must be {expected_length} bytes, got {len(data)}")
    return data


def parse_eui(text: str, name: str) -> bytes:
    return parse_hex_bytes(text, 8, name)[::-1]


def format_eui(eui_le: bytes) -> str:
    return eui_le[::-1].hex().upper()


def format_wire_eui(eui_le: bytes) -> str:
    return eui_le.hex().upper()


def parse_frequency(value: str) -> int:
    text = value.strip().lower()
    if text.endswith("mhz"):
        return int(float(text[:-3]) * 1_000_000)
    if text.endswith("hz"):
        return int(float(text[:-2]))
    return int(text)


def parse_sync_word(value: str) -> int:
    text = value.strip().lower()
    if text in {"public", "lorawan"}:
        return DEFAULT_PUBLIC_SYNC_WORD
    if text in {"private", "p2p"}:
        return 0x0741
    return int(text, 0)


def load_join_config(path: str | None) -> dict:
    if not path:
        return {}

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError("config file must contain a JSON object")

    return data


def prompt_value(label: str, current: str | None = None, secret: bool = False) -> str:
    suffix = f" [{current}]" if current else ""
    prompt = f"{label}{suffix}: "
    value = getpass.getpass(prompt) if secret else input(prompt)
    value = value.strip()
    return value or (current or "")


def make_join_request(app_eui_le: bytes, dev_eui_le: bytes, app_key: bytes, dev_nonce: bytes) -> bytes:
    payload = b"\x00" + app_eui_le + dev_eui_le + dev_nonce
    return payload + aes_cmac(app_key, payload)[:4]


def decrypt_join_accept(app_key: bytes, encrypted: bytes) -> bytes:
    if len(encrypted) % 16 != 0:
        raise ValueError("join-accept payload length must be a multiple of 16 bytes")
    out = bytearray(len(encrypted))
    for offset in range(0, len(encrypted), 16):
        block = encrypted[offset : offset + 16]
        out[offset : offset + 16] = aes_ecb_encrypt_block(app_key, block)
    return bytes(out)


def verify_join_accept_mic(app_key: bytes, payload: bytes) -> bool:
    return aes_cmac(app_key, b"\x20" + payload[:-4])[:4] == payload[-4:]


def parse_join_accept_frame(frame: bytes, app_key: bytes) -> dict[str, bytes]:
    if len(frame) < 17:
        raise ValueError("join-accept frame is too short")
    if frame[0] != 0x20:
        raise ValueError(f"unexpected downlink MType: 0x{frame[0]:02x}")

    decrypted = decrypt_join_accept(app_key, frame[1:])
    if not verify_join_accept_mic(app_key, decrypted):
        raise ValueError("join-accept MIC does not match")

    payload = decrypted[:-4]
    if len(payload) not in {12, 28}:
        raise ValueError("unexpected join-accept plaintext size")

    return {
        "join_nonce": payload[0:3],
        "net_id": payload[3:6],
        "dev_addr": payload[6:10],
        "dl_settings": payload[10:11],
        "rx_delay": payload[11:12],
        "cflist": payload[12:],
    }


def derive_session_keys(app_key: bytes, join_nonce: bytes, net_id: bytes, dev_nonce: bytes) -> tuple[bytes, bytes]:
    if len(join_nonce) != 3 or len(net_id) != 3 or len(dev_nonce) != 2:
        raise ValueError("invalid join-accept key material")

    pad = b"\x00" * 7
    nwk_skey = aes_ecb_encrypt_block(app_key, b"\x01" + join_nonce + net_id + dev_nonce + pad)
    app_skey = aes_ecb_encrypt_block(app_key, b"\x02" + join_nonce + net_id + dev_nonce + pad)
    return nwk_skey, app_skey


def join_fingerprint(app_eui_le: bytes, dev_eui_le: bytes, app_key: bytes) -> str:
    digest = hashlib.sha256(app_eui_le + dev_eui_le + app_key).hexdigest().upper()
    return digest


def save_session_file(
    session_path: Path,
    dev_addr_be: bytes,
    nwk_skey: bytes,
    app_skey: bytes,
    join_fp: str,
) -> None:
    session_path.parent.mkdir(parents=True, exist_ok=True)
    with session_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "dev_addr": dev_addr_be[::-1].hex().upper(),
                "nwk_skey": nwk_skey.hex().upper(),
                "app_skey": app_skey.hex().upper(),
                "fcnt_up": 0,
                "join_fingerprint": join_fp,
            },
            handle,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal LoRaWAN OTAA join script for the Waveshare SX126x HAT")
    parser.add_argument("--bus", type=int, default=DEFAULT_BUS, help="SPI bus number")
    parser.add_argument("--cs", type=int, default=DEFAULT_CS, help="SPI chip-select number")
    parser.add_argument("--reset-pin", type=int, default=DEFAULT_RESET_PIN, help="BCM pin for RESET")
    parser.add_argument("--busy-pin", type=int, default=DEFAULT_BUSY_PIN, help="BCM pin for BUSY")
    parser.add_argument("--irq-pin", type=int, default=DEFAULT_IRQ_PIN, help="BCM pin for DIO1/IRQ")
    parser.add_argument("--txen-pin", type=int, default=DEFAULT_TXEN_PIN, help="BCM pin for TXEN")
    parser.add_argument("--rxen-pin", type=int, default=DEFAULT_RXEN_PIN, help="BCM pin for RXEN")
    parser.add_argument("--config", type=str, default=None, help="JSON config file with app_eui, dev_eui, app_key, and optional dev_nonce")
    parser.add_argument("--app-eui", type=str, default=None, help="JoinEUI/AppEUI as 16 hex characters")
    parser.add_argument("--dev-eui", type=str, default=None, help="DevEUI as 16 hex characters")
    parser.add_argument("--app-key", type=str, default=None, help="AppKey as 32 hex characters")
    parser.add_argument("--dev-nonce", type=str, default=None, help="Optional DevNonce as 4 hex characters")
    parser.add_argument("--join-frequency", type=parse_frequency, default=DEFAULT_JOIN_FREQUENCY, help="Join-request frequency in Hz or MHz")
    parser.add_argument("--join-sf", type=int, default=DEFAULT_JOIN_SPREADING_FACTOR, help="Join-request spreading factor")
    parser.add_argument("--join-bandwidth", type=int, default=DEFAULT_JOIN_BANDWIDTH, help="Join-request bandwidth in Hz")
    parser.add_argument("--join-coding-rate", type=int, default=DEFAULT_JOIN_CODING_RATE, help="Join-request coding rate denominator, for example 5 for 4/5")
    parser.add_argument("--join-preamble", type=int, default=DEFAULT_JOIN_PREAMBLE, help="Join-request preamble length")
    parser.add_argument("--tx-power", type=int, default=DEFAULT_TX_POWER, help="Transmit power in dBm")
    parser.add_argument("--sync-word", type=parse_sync_word, default=DEFAULT_PUBLIC_SYNC_WORD, help="Sync word: public, private, or a hex value")
    parser.add_argument("--tx-timeout", type=float, default=DEFAULT_TX_TIMEOUT, help="Transmit timeout in seconds")
    parser.add_argument("--join-accept-timeout", type=float, default=DEFAULT_JOIN_ACCEPT_TIMEOUT, help="Join accept receive timeout in seconds")
    parser.add_argument("--scan-eu868", action="store_true", help="Try the standard EU868 OTAA join channels one by one")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    config = load_join_config(args.config)

    if config:
        if "bus" in config:
            args.bus = int(config["bus"])
        if "cs" in config:
            args.cs = int(config["cs"])
        if "reset_pin" in config:
            args.reset_pin = int(config["reset_pin"])
        if "busy_pin" in config:
            args.busy_pin = int(config["busy_pin"])
        if "irq_pin" in config:
            args.irq_pin = int(config["irq_pin"])
        if "txen_pin" in config:
            args.txen_pin = int(config["txen_pin"])
        if "rxen_pin" in config:
            args.rxen_pin = int(config["rxen_pin"])

        if "join_frequency" in config:
            args.join_frequency = int(config["join_frequency"])
        if "join_sf" in config:
            args.join_sf = int(config["join_sf"])
        if "join_bandwidth" in config:
            args.join_bandwidth = int(config["join_bandwidth"])
        if "join_coding_rate" in config:
            args.join_coding_rate = int(config["join_coding_rate"])
        if "join_preamble" in config:
            args.join_preamble = int(config["join_preamble"])
        if "tx_power" in config:
            args.tx_power = int(config["tx_power"])
        if "sync_word" in config:
            args.sync_word = parse_sync_word(str(config["sync_word"]))
        if "join_accept_timeout" in config:
            args.join_accept_timeout = float(config["join_accept_timeout"])
        if "tx_timeout" in config:
            args.tx_timeout = float(config["tx_timeout"])
        if config.get("scan_eu868"):
            args.scan_eu868 = True

    app_eui_text = args.app_eui or config.get("app_eui") or config.get("appEui")
    dev_eui_text = args.dev_eui or config.get("dev_eui") or config.get("devEui")
    app_key_text = args.app_key or config.get("app_key") or config.get("appKey")
    dev_nonce_text = args.dev_nonce or config.get("dev_nonce") or config.get("devNonce")

    if not app_eui_text:
        app_eui_text = prompt_value("JoinEUI/AppEUI")
    if not dev_eui_text:
        dev_eui_text = prompt_value("DevEUI")
    if not app_key_text:
        app_key_text = prompt_value("AppKey", secret=True)

    if not app_eui_text or not dev_eui_text or not app_key_text:
        raise ValueError("JoinEUI/AppEUI, DevEUI, and AppKey are required")

    app_eui = parse_eui(app_eui_text, "app-eui")
    dev_eui = parse_eui(dev_eui_text, "dev-eui")
    app_key = parse_hex_bytes(app_key_text, 16, "app-key")
    dev_nonce = parse_hex_bytes(dev_nonce_text, 2, "dev-nonce") if dev_nonce_text else None

    radio = SX126x()
    print("Begin LoRa radio")
    if not radio.begin(args.bus, args.cs, args.reset_pin, args.busy_pin, args.irq_pin, args.txen_pin, args.rxen_pin):
        raise RuntimeError("Could not initialize the SX126x radio")

    try:
        radio.setDio2RfSwitch()
        radio.setTxPower(args.tx_power, radio.TX_POWER_SX1262)
        radio.setSyncWord(args.sync_word)

        channels = EU868_JOIN_CHANNELS if args.scan_eu868 else ((args.join_frequency, args.join_bandwidth, args.join_sf),)
        if args.scan_eu868:
            print("EU868 scan mode enabled: trying standard join channels one by one")

        for frequency, bandwidth, spreading_factor in channels:
            current_dev_nonce = dev_nonce if dev_nonce is not None else secrets.token_bytes(2)
            attempt_marker = secrets.token_hex(3).upper()
            join_request = make_join_request(app_eui, dev_eui, app_key, current_dev_nonce)

            radio.setFrequency(frequency)
            ldro = (2 ** spreading_factor / bandwidth) > 0.016  # LDRO required when symbol time > 16 ms
            radio.setLoRaModulation(spreading_factor, bandwidth, args.join_coding_rate, ldro)
            radio.setLoRaPacket(radio.HEADER_EXPLICIT, args.join_preamble, 255, True, False)

            print(
                "join request frame: "
                f"attempt={attempt_marker} "
                f"app_eui={format_eui(app_eui)} "
                f"dev_eui={format_eui(dev_eui)} "
                f"app_eui_wire={format_wire_eui(app_eui)} "
                f"dev_eui_wire={format_wire_eui(dev_eui)} "
                f"dev_nonce={current_dev_nonce.hex().upper()} "
                f"freq={frequency} "
                f"bw={bandwidth} "
                f"sf={spreading_factor} "
                f"phy={join_request.hex().upper()}"
            )

            radio.beginPacket()
            radio.put(join_request)
            radio.endPacket()
            if not radio.wait(args.tx_timeout):
                raise TimeoutError("join-request transmit timed out")

            t_tx_done = time.time()   # anchor for RX window timing
            print("join request sent, waiting for join accept...")

            # LoRaWAN join-accept receive windows (LoRaWAN 1.0 defaults)
            # RX1: same freq/DR, opens JOIN_ACCEPT_DELAY1 (5 s) after TX
            # RX2: 869.525 MHz / SF12, opens JOIN_ACCEPT_DELAY2 (6 s) after TX
            JOIN_ACCEPT_DELAY2 = 6.0  # seconds after end of TX

            downlink = None
            decoded: dict | None = None
            for rx_freq, rx_sf, rx_bw, rx_label, rx_timeout, rx_abs_delay in [
                (frequency,   spreading_factor, bandwidth, "RX1", args.join_accept_timeout, 0.0),
                (869_525_000, 12,               125_000,  "RX2", 3.0,                      JOIN_ACCEPT_DELAY2),
            ]:
                # Bug 2 fix: open RX2 at exactly JOIN_ACCEPT_DELAY2 after TX end,
                # not after the full RX1 timeout expires.
                if rx_abs_delay > 0:
                    wait_s = (t_tx_done + rx_abs_delay) - time.time()
                    if wait_s > 0:
                        time.sleep(wait_s)

                radio.setFrequency(rx_freq)
                ldro_rx = (2 ** rx_sf / rx_bw) > 0.016
                radio.setLoRaModulation(rx_sf, rx_bw, args.join_coding_rate, ldro_rx)
                radio.setLoRaPacket(radio.HEADER_EXPLICIT, args.join_preamble, 255, False, True)
                radio.request(radio.RX_CONTINUOUS)
                t_rx = time.time()
                while (time.time() - t_rx) < rx_timeout:
                    irq = radio.getIrqStatus()
                    if irq & (radio.IRQ_RX_DONE | radio.IRQ_CRC_ERR | radio.IRQ_HEADER_ERR):
                        # Refresh from radio — available() returns stale TX length without this
                        (radio._payloadTxRx, radio._bufferIndex) = radio.getRxBufferStatus()
                        pkt_len = radio.available()
                        print(f"{rx_label} IRQ=0x{irq:04X} available={pkt_len} t={time.time()-t_rx:.2f}s")
                        if irq & radio.IRQ_RX_DONE and pkt_len > 0:
                            raw = radio.get(pkt_len)
                            if raw and raw[0] == 0x20:  # JoinAccept MHDR
                                # Bug 1 fix: verify MIC before accepting; keep polling on failure.
                                try:
                                    decoded = parse_join_accept_frame(raw, app_key)
                                    downlink = raw
                                except ValueError as exc:
                                    print(f"{rx_label} rejected frame: {exc}, still waiting...")
                                    radio.clearIrqStatus(0x03FF)
                                    continue
                            else:
                                mhdr = raw[0] if raw else 0
                                print(f"{rx_label} ignored spurious frame mhdr=0x{mhdr:02x} len={pkt_len} (not JoinAccept), still waiting...")
                                radio.clearIrqStatus(0x03FF)
                                continue  # keep polling
                        radio.clearIrqStatus(0x03FF)
                        break
                    time.sleep(0.01)
                else:
                    print(f"{rx_label} timeout ({rx_timeout}s)")
                    radio.clearIrqStatus(0x03FF)
                if downlink:
                    break

            if not decoded:
                if args.scan_eu868:
                    continue
                raise TimeoutError("no join accept received")

            # decoded is already populated and MIC-verified in the RX loop above
            nwk_skey, app_skey = derive_session_keys(app_key, decoded["join_nonce"], decoded["net_id"], current_dev_nonce)
            if args.config:
                session_path = Path(args.config).with_name(Path(args.config).stem + "_session.json")
                save_session_file(
                    session_path,
                    decoded["dev_addr"],
                    nwk_skey,
                    app_skey,
                    join_fingerprint(app_eui, dev_eui, app_key),
                )

            print(f"join accept received, dev_addr={decoded['dev_addr'][::-1].hex().upper()}")
            print(f"join nonce={decoded['join_nonce'].hex().upper()} net_id={decoded['net_id'].hex().upper()}")
            print(f"nwk_skey={nwk_skey.hex().upper()}")
            print(f"app_skey={app_skey.hex().upper()}")
            return 0

        raise TimeoutError("no join accept received on any EU868 channel")
    finally:
        radio.end()


if __name__ == "__main__":
    raise SystemExit(main())