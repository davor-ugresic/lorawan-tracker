# lorawan-tracker

Headless LoRaWAN OTAA tracker for the Waveshare SX126x LoRa HAT on Raspberry Pi 4.

## Install (headless, one command)

SSH into a fresh Raspberry Pi OS and run:

```bash
curl -fsSL https://raw.githubusercontent.com/davor-ugresic/lorawan-tracker/main/install.sh | sudo bash
```

The installer adds the signed apt repository and installs the package. During
installation it automatically:

1. Installs dependencies (`LoRaRF`) and patches the SX126x driver for Pi 4 Bookworm.
2. Configures the Pi hardware — enables SPI, frees the UART (disables Bluetooth
   and the serial console) for the GPS.
3. Generates the device credentials (DevEUI, JoinEUI/AppEUI, AppKey)
   deterministically from the Pi's hardware serial number.
4. Installs, enables and starts the `lorawan-tracker` service for the Pi's
   primary user (with linger, so it runs headless without a login), matching
   the desktop GUI so both stay in sync.

If the hardware step changed boot settings, the installer prints
`A REBOOT IS REQUIRED`. Run `sudo reboot` once — the tracker starts
automatically after boot. Otherwise it is already running.

## After install

```bash
systemctl --user status lorawan-tracker.service     # service state
journalctl --user -u lorawan-tracker.service -f      # live logs
cat ~/.config/lorawan-tracker/device_keys.txt        # this device's credentials
```

## Credentials

Credentials are derived from the Pi serial with HMAC-SHA256, so the same board
always yields the same values and they can be recomputed from the serial:

- **DevEUI** — 8 bytes, marker prefix `FE`
- **JoinEUI/AppEUI** — 8 bytes, marker prefix `FD`
- **AppKey** — 16 bytes

Regenerate or preview at any time:

```bash
python3 /usr/lib/lorawan-tracker/lorawan_keygen.py --print-only
```

Because the derivation is public, keys are guessable by anyone who knows the
serial. To harden a deployment, create a secret salt and pass it — it is mixed
into the derivation while remaining reproducible on that board:

```bash
python3 /usr/lib/lorawan-tracker/lorawan_keygen.py \
    --salt ~/.config/lorawan-tracker/site.salt --print-only
```