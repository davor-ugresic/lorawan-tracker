#!/usr/bin/env python3
"""LoRaWAN GPS Tracker — dashboard GUI.

Monitors the lorawan-tracker systemd user service and shows live
GPS position, join status, uplink statistics, and service logs.

First-time setup:
  1. Click "Configure" to set your device credentials.
  2. Click "Install Service" to register the systemd unit.
  3. Click "Start" — the service joins the network and begins tracking.

Status is read from: ~/.local/share/lorawan-tracker/status.json
Service logs come from: journalctl --user -u lorawan-tracker
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
STATUS_FILE = Path.home() / ".local" / "share" / "lorawan-tracker" / "status.json"
SERVICE_NAME   = "lorawan-tracker"
WEBSRV_NAME    = "lorawan-webserver"
SERVICE_FILE   = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
WEBSRV_FILE    = Path.home() / ".config" / "systemd" / "user" / f"{WEBSRV_NAME}.service"
SERVICE_SCRIPT = APP_DIR / "lorawan_service.py"
WEBSRV_SCRIPT  = APP_DIR / "lorawan_webserver.py"
CONFIG_SCRIPT  = APP_DIR / "lorawan_join_gui.py"
_SYSTEM_INSTALL = str(APP_DIR).startswith("/usr/")
DEFAULT_CONFIG = (
    Path.home() / ".config" / "lorawan-tracker" / "lorawan_join.json"
    if _SYSTEM_INSTALL
    else APP_DIR / "lorawan_join.json"
)
WEB_PORT       = 8080

# State → display text
STATE_LABEL = {
    "tracking":     "● TRACKING",
    "joined":       "● JOINED",
    "joining":      "◌ JOINING…",
    "reconnecting": "◌ RECONNECTING…",
    "starting":     "◌ STARTING…",
    "join_failed":  "✗ JOIN FAILED",
    "error":        "✗ ERROR",
    "stopped":      "○ STOPPED",
    "not_installed": "○ NOT INSTALLED",
}

# State → foreground colour
STATE_COLOR = {
    "tracking":     "#1a9641",
    "joined":       "#1a9641",
    "joining":      "#d17000",
    "reconnecting": "#d17000",
    "starting":     "#d17000",
    "join_failed":  "#cc0000",
    "error":        "#cc0000",
    "stopped":      "#555555",
    "not_installed": "#555555",
}

# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------

def _svc(cmd: str) -> bool:
    r = subprocess.run(["systemctl", "--user", cmd, SERVICE_NAME],
                       capture_output=True)
    return r.returncode == 0


def _svc2(cmd: str, name: str) -> bool:
    r = subprocess.run(["systemctl", "--user", cmd, name],
                       capture_output=True)
    return r.returncode == 0


def _svc_is_active2(name: str) -> bool:
    r = subprocess.run(["systemctl", "--user", "is-active", name],
                       capture_output=True, text=True)
    return r.stdout.strip() == "active"


def _svc_is_installed() -> bool:
    return SERVICE_FILE.exists()


def _svc_is_active() -> bool:
    r = subprocess.run(["systemctl", "--user", "is-active", SERVICE_NAME],
                       capture_output=True, text=True)
    return r.stdout.strip() == "active"


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M:%S")
    except Exception:
        return str(iso)


def _enable_linger() -> bool:
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        return False
    result = subprocess.run(["loginctl", "enable-linger", user], capture_output=True, text=True)
    return result.returncode == 0

# ---------------------------------------------------------------------------
# Dashboard window
# ---------------------------------------------------------------------------

class Dashboard(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LoRa GPS Tracker")
        self.geometry("460x700")
        self.minsize(420, 600)
        self._log_thread: threading.Thread | None = None
        self._tick_n = 0
        self._build_ui()
        self.after(500, self._tick)

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        # Header row
        hdr = ttk.Frame(root)
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="LoRa GPS Tracker",
                 font=("", 15, "bold")).pack(side="left")
        ttk.Button(hdr, text="Configure",
                   command=self._open_config).pack(side="right")

        # Status line
        self._state_lbl = tk.Label(root, text="○ LOADING…",
                                   font=("", 13, "bold"),
                                   fg="#888888", anchor="w")
        self._state_lbl.pack(fill="x", pady=(6, 0))
        self._since_lbl = tk.Label(root, text="", fg="#555555",
                                   anchor="w", font=("", 9))
        self._since_lbl.pack(fill="x", pady=(0, 8))

        ttk.Separator(root).pack(fill="x", pady=(0, 8))

        # GPS section
        gps = ttk.LabelFrame(root, text="GPS Fix", padding=8)
        gps.pack(fill="x", pady=(0, 6))
        gps.columnconfigure(1, weight=1)

        self._lat_var  = tk.StringVar(value="—")
        self._lon_var  = tk.StringVar(value="—")
        self._alt_var  = tk.StringVar(value="—")
        self._fix_time = tk.StringVar(value="—")
        self._link_var = tk.StringVar(value="◌ Link unknown")

        for row, (label, var) in enumerate([
            ("Latitude",  self._lat_var),
            ("Longitude", self._lon_var),
            ("Altitude",  self._alt_var),
            ("Last fix",  self._fix_time),
        ]):
            tk.Label(gps, text=f"{label}:", width=12,
                     anchor="w").grid(row=row, column=0, sticky="w")
            tk.Label(gps, textvariable=var, anchor="w",
                     font=("", 10, "bold")).grid(row=row, column=1,
                                                 sticky="w", padx=6)

        self._link_lbl = tk.Label(gps, textvariable=self._link_var,
                                  anchor="w", padx=10, pady=6)
        self._link_lbl.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # Session section
        sess = ttk.LabelFrame(root, text="Session", padding=8)
        sess.pack(fill="x", pady=(0, 6))
        sess.columnconfigure(1, weight=1)

        self._dev_addr_var    = tk.StringVar(value="—")
        self._fcnt_var        = tk.StringVar(value="—")
        self._uplinks_var     = tk.StringVar(value="—")
        self._last_join_var   = tk.StringVar(value="—")
        self._last_uplink_var = tk.StringVar(value="—")

        for row, (label, var) in enumerate([
            ("DevAddr",      self._dev_addr_var),
            ("FCnt",         self._fcnt_var),
            ("Uplinks sent", self._uplinks_var),
            ("Last join",    self._last_join_var),
            ("Last uplink",  self._last_uplink_var),
        ]):
            tk.Label(sess, text=f"{label}:", width=14,
                     anchor="w").grid(row=row, column=0, sticky="w")
            tk.Label(sess, textvariable=var,
                     anchor="w").grid(row=row, column=1, sticky="w", padx=6)

        # Control buttons
        ctrl = ttk.Frame(root)
        ctrl.pack(fill="x", pady=8)

        self._start_btn   = ttk.Button(ctrl, text="Start",
                                       command=self._start)
        self._stop_btn    = ttk.Button(ctrl, text="Stop",
                                       command=self._stop)
        self._restart_btn = ttk.Button(ctrl, text="Restart",
                                       command=self._restart)
        self._install_btn = ttk.Button(ctrl, text="Install Service",
                                       command=self._install)

        for btn in (self._start_btn, self._stop_btn, self._restart_btn):
            btn.pack(side="left", padx=(0, 6))
        self._install_btn.pack(side="right")

        # Network section
        net = ttk.LabelFrame(root, text="Network / Web UI", padding=8)
        net.pack(fill="x", pady=(0, 6))
        net.columnconfigure(1, weight=1)

        # Web server row
        ttk.Label(net, text="Web server:", anchor="w", width=12
                  ).grid(row=0, column=0, sticky="w")
        self._web_url_var = tk.StringVar(value="—")
        tk.Label(net, textvariable=self._web_url_var, anchor="w",
                 fg="#1565c0", font=("Monospace", 8)
                 ).grid(row=0, column=1, sticky="ew", padx=6)
        web_btns = ttk.Frame(net)
        web_btns.grid(row=0, column=2)
        self._web_start_btn = ttk.Button(web_btns, text="Start", width=6,
                                         command=self._start_webserver)
        self._web_stop_btn  = ttk.Button(web_btns, text="Stop",  width=6,
                                         command=self._stop_webserver)
        self._web_start_btn.pack(side="left", padx=(0, 4))
        self._web_stop_btn.pack(side="left")

        # Hotspot row
        ttk.Label(net, text="Hotspot:", anchor="w", width=12
                  ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._ap_var = tk.StringVar(value="—")
        ttk.Label(net, textvariable=self._ap_var, anchor="w"
                  ).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(net, text="Setup…", command=self._setup_hotspot
                   ).grid(row=1, column=2, pady=(6, 0))

        # Log panel
        log_frame = ttk.LabelFrame(root, text="Service Log", padding=6)
        log_frame.pack(fill="both", expand=True)

        self._log_text = tk.Text(log_frame, wrap="none", height=8,
                                 state="disabled",
                                 font=("Monospace", 8),
                                 bg="#1e1e1e", fg="#d4d4d4",
                                 insertbackground="white")
        self._log_text.pack(side="left", fill="both", expand=True)

        sb_v = ttk.Scrollbar(log_frame, orient="vertical",
                              command=self._log_text.yview)
        sb_v.pack(side="right", fill="y")
        self._log_text.configure(yscrollcommand=sb_v.set)

    # ── Refresh cycle ────────────────────────────────────────────────────

    def _tick(self) -> None:
        self._tick_n += 1
        self._refresh_status()
        if self._tick_n % 3 == 0:      # every 3 s
            self._refresh_log()
            self._refresh_network()
        self.after(1000, self._tick)

    def _refresh_status(self) -> None:
        installed = _svc_is_installed()
        active = _svc_is_active() if installed else False
        status = _read_status()

        # Determine display state
        if not installed:
            state = "not_installed"
        elif not active and status.get("state") not in ("stopping", "stopped"):
            state = "stopped"
        else:
            state = status.get("state", "stopped")

        color = STATE_COLOR.get(state, "#555555")
        label = STATE_LABEL.get(state, f"● {state.upper()}")
        self._state_lbl.configure(text=label, fg=color)

        # Sub-line
        if state == "tracking" and status.get("last_join"):
            since = (f"Joined {_fmt_time(status['last_join'])}  ·  "
                     f"DevAddr {status.get('dev_addr', '?')}")
        elif state in ("joining", "reconnecting", "starting"):
            since = f"Running since {_fmt_time(status.get('started'))}"
        else:
            since = ""
        self._since_lbl.configure(text=since)

        # GPS
        fix: dict = status.get("last_fix") or {}
        self._lat_var.set(f"{fix['lat']:.6f}°" if (fix.get("lat") is not None and (fix["lat"] != 0.0 or fix.get("lon") != 0.0)) else "—")
        self._lon_var.set(f"{fix['lon']:.6f}°" if (fix.get("lon") is not None and (fix["lat"] != 0.0 or fix.get("lon") != 0.0)) else "—")
        self._alt_var.set(f"{fix['alt_m']:.1f} m" if (fix.get("alt_m") is not None and (fix.get("lat") != 0.0 or fix.get("lon") != 0.0)) else "—")
        self._fix_time.set(_fmt_time(fix.get("time")))

        link_status = status.get("link_status", "unknown")
        if link_status == "in_range":
            self._link_var.set("● In range")
            self._link_lbl.configure(fg="#1a9641", bg="#eaf6ee")
        elif link_status == "no_ack":
            self._link_var.set("✗ No ACK received")
            self._link_lbl.configure(fg="#cc0000", bg="#fdebec")
        else:
            self._link_var.set("◌ Link unknown")
            self._link_lbl.configure(fg="#d17000", bg="#fff4e5")

        # Session
        self._dev_addr_var.set(status.get("dev_addr") or "—")
        self._fcnt_var.set(str(status.get("fcnt", "—")))
        self._uplinks_var.set(str(status.get("uplinks_sent", "—")))
        self._last_join_var.set(_fmt_time(status.get("last_join")))
        self._last_uplink_var.set(_fmt_time(status.get("last_uplink")))

        # Button states
        if not installed:
            self._start_btn.configure(state="disabled")
            self._stop_btn.configure(state="disabled")
            self._restart_btn.configure(state="disabled")
            self._install_btn.configure(state="normal")
        else:
            self._start_btn.configure(state="disabled" if active else "normal")
            self._stop_btn.configure(state="normal" if active else "disabled")
            self._restart_btn.configure(state="normal" if installed else "disabled")
            self._install_btn.configure(state="disabled")

    def _refresh_log(self) -> None:
        result = subprocess.run(
            ["journalctl", "--user", "-u", SERVICE_NAME,
             "-n", "50", "--no-pager", "--output=short-monotonic"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            text = result.stdout
        else:
            s = _read_status()
            text = (f"Service not yet in journal.\n"
                    f"State: {s.get('state', '—')}\n"
                    f"Updated: {_fmt_time(s.get('updated'))}\n")

        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", tk.END)
        self._log_text.insert(tk.END, text)
        self._log_text.configure(state="disabled")
        self._log_text.see(tk.END)

    # ── Service control ──────────────────────────────────────────────────

    def _start(self) -> None:
        if not _svc("start"):
            messagebox.showerror("Start failed",
                                 "Could not start the service.\n"
                                 "Run: systemctl --user status lorawan-tracker")

    def _stop(self) -> None:
        _svc("stop")

    def _restart(self) -> None:
        _svc("restart")

    # ── Web server control ───────────────────────────────────────────────

    def _start_webserver(self) -> None:
        if not WEBSRV_FILE.exists():
            self._install_webserver()
            return
        _svc2("start", WEBSRV_NAME)

    def _stop_webserver(self) -> None:
        _svc2("stop", WEBSRV_NAME)

    def _install_webserver(self) -> None:
        unit = "\n".join([
            "[Unit]",
            "Description=LoRaWAN GPS Tracker Web Server",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={sys.executable} -u {WEBSRV_SCRIPT} --port {WEB_PORT}",
            f"WorkingDirectory={Path.home()}",
            "Restart=on-failure",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])
        try:
            WEBSRV_FILE.parent.mkdir(parents=True, exist_ok=True)
            WEBSRV_FILE.write_text(unit)
        except Exception as exc:
            messagebox.showerror("Install web server", str(exc))
            return

        _enable_linger()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", WEBSRV_NAME], capture_output=True)
        _svc2("start", WEBSRV_NAME)

    def _refresh_network(self) -> None:
        # Web server
        web_active = _svc_is_active2(WEBSRV_NAME)
        import socket
        try:
            hn = socket.gethostname()
        except Exception:
            hn = "raspberrypi"
        if web_active:
            self._web_url_var.set(f"http://10.42.0.1:{WEB_PORT}  |  http://{hn}.local:{WEB_PORT}")
            self._web_start_btn.configure(state="disabled")
            self._web_stop_btn.configure(state="normal")
        else:
            self._web_url_var.set("not running")
            self._web_start_btn.configure(state="normal")
            self._web_stop_btn.configure(state="disabled")

        # Hotspot — check if lorawan-hotspot connection is active on wlan0
        r = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "dev", "show", "wlan0"],
            capture_output=True, text=True,
        )
        if "lorawan-hotspot" in r.stdout.lower():
            self._ap_var.set("● 10.42.0.1  (LoRaMoto active)")
        else:
            self._ap_var.set("not active — click Setup to configure")

    # ── Hotspot setup ────────────────────────────────────────────────────

    def _setup_hotspot(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("WiFi Hotspot Setup")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        fr = ttk.Frame(dlg, padding=14)
        fr.pack(fill="both")

        ttk.Label(fr, text="Creates a persistent WiFi access point on wlan0.\n"
                           "Phone connects to it, then opens the tracker in the browser.",
                  wraplength=320, justify="left").grid(row=0, column=0, columnspan=2,
                                                       sticky="w", pady=(0, 10))
        ssid_var = tk.StringVar(value="LoRaMoto")
        pwd_var  = tk.StringVar(value="lorawan123")

        for r_idx, (lbl, var, show) in enumerate([
            ("SSID:",     ssid_var, True),
            ("Password:", pwd_var,  True),
        ], start=1):
            ttk.Label(fr, text=lbl, width=10, anchor="w").grid(row=r_idx, column=0, sticky="w")
            ttk.Entry(fr, textvariable=var, width=22).grid(row=r_idx, column=1, sticky="ew",
                                                            pady=2, padx=(6, 0))

        def _apply() -> None:
            ssid = ssid_var.get().strip()
            pwd  = pwd_var.get().strip()
            if not ssid:
                messagebox.showerror("Hotspot", "SSID cannot be empty", parent=dlg)
                return
            if len(pwd) < 8:
                messagebox.showerror("Hotspot", "Password must be ≥ 8 characters", parent=dlg)
                return
            # Remove old profile if it exists
            subprocess.run(["nmcli", "con", "delete", "lorawan-hotspot"],
                           capture_output=True)
            # Create persistent AP profile
            r = subprocess.run([
                "nmcli", "connection", "add",
                "type", "wifi", "ifname", "wlan0",
                "con-name", "lorawan-hotspot",
                "ssid", ssid,
                "802-11-wireless.mode", "ap",
                "wifi-sec.key-mgmt", "wpa-psk",
                "wifi-sec.psk", pwd,
                "ipv4.method", "shared",
                "ipv4.addresses", "10.42.0.1/24",
                "connection.autoconnect", "yes",
            ], capture_output=True, text=True)
            if r.returncode != 0:
                # nmcli may need pkexec; show the manual command
                cmd = (f"sudo nmcli connection add type wifi ifname wlan0 "
                       f"con-name lorawan-hotspot ssid \"{ssid}\" "
                       f"802-11-wireless.mode ap wifi-sec.key-mgmt wpa-psk "
                       f"wifi-sec.psk \"{pwd}\" ipv4.method shared "
                       f"ipv4.addresses 10.42.0.1/24 connection.autoconnect yes")
                messagebox.showerror(
                    "Hotspot setup failed",
                    f"nmcli returned an error.\n\n"
                    f"Run this manually in a terminal:\n\n{cmd}\n\n"
                    f"Then: sudo nmcli con up lorawan-hotspot",
                    parent=dlg,
                )
                return
            # Bring it up
            subprocess.run(["nmcli", "con", "up", "lorawan-hotspot"],
                           capture_output=True)
            messagebox.showinfo(
                "Hotspot active",
                f"Hotspot created and started.\n\n"
                f"SSID:     {ssid}\n"
                f"Password: {pwd}\n"
                f"IP:       10.42.0.1\n\n"
                f"Web UI:   http://10.42.0.1:{WEB_PORT}",
                parent=dlg,
            )
            dlg.destroy()

        btn_row = ttk.Frame(fr)
        btn_row.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Apply", command=_apply).pack(side="left")

    # ── Install service ──────────────────────────────────────────────────

    def _install(self) -> None:
        config_path = DEFAULT_CONFIG.resolve()

        unit = "\n".join([
            "[Unit]",
            "Description=LoRaWAN GPS Tracker",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={sys.executable} -u {SERVICE_SCRIPT} --config {config_path}",
            f"WorkingDirectory={Path.home()}",
            "Environment=PYTHONUNBUFFERED=1",
            "Restart=on-failure",
            "RestartSec=30s",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])

        try:
            SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
            SERVICE_FILE.write_text(unit)
        except Exception as exc:
            messagebox.showerror("Install Service", str(exc))
            return

        _enable_linger()
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME],
                       capture_output=True)

        # Also install a desktop entry so the dashboard appears in the app menu
        self._install_desktop_entry()

        if messagebox.askyesno("Service installed",
                               "Service installed and enabled on boot.\n\n"
                               "Start the tracker now?"):
            self._start()

    def _install_desktop_entry(self) -> None:
        apps_dir = Path.home() / ".local" / "share" / "applications"
        entry = "\n".join([
            "[Desktop Entry]",
            "Type=Application",
            "Name=LoRa GPS Tracker",
            "Comment=LoRaWAN GPS Tracker Dashboard",
            f"Exec={sys.executable} {APP_DIR / 'lorawan_dashboard.py'}",
            "Icon=network-wireless",
            "Terminal=false",
            "Categories=Network;Utility;",
            "",
        ])
        try:
            apps_dir.mkdir(parents=True, exist_ok=True)
            (apps_dir / "lorawan-tracker.desktop").write_text(entry)
        except Exception:
            pass  # non-critical

    # ── Configure ────────────────────────────────────────────────────────

    def _open_config(self) -> None:
        subprocess.Popen(
            [sys.executable, str(CONFIG_SCRIPT)],
            cwd=str(Path.home()),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    app = Dashboard()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
