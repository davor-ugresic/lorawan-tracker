#!/usr/bin/env python3
"""GUI launcher for the Waveshare SX126x LoRaWAN join flow."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path


try:
    from importlib.metadata import version as _pkg_version
    VERSION = _pkg_version("lorawan-tracker")
except Exception:
    try:
        _vf = Path(__file__).resolve().parent / "version.txt"
        VERSION = _vf.read_text().strip() if _vf.exists() else ""
        if not VERSION:
            import subprocess as _sp2
            VERSION = _sp2.check_output(
                ["dpkg-query", "-W", "-f=${Version}", "lorawan-tracker"],
                text=True, stderr=_sp2.DEVNULL
            ).strip()
    except Exception:
        VERSION = "1.3.9"

APP_DIR = Path(__file__).resolve().parent
JOIN_SCRIPT = APP_DIR / "minimal_lorawan_join.py"
SERVICE_SCRIPT = APP_DIR / "lorawan_service.py"
# When installed system-wide, keep the user's config in their home directory
_SYSTEM_INSTALL = str(APP_DIR).startswith("/usr/")
DEFAULT_CONFIG = (
    Path.home() / ".config" / "lorawan-tracker" / "lorawan_join.json"
    if _SYSTEM_INSTALL
    else APP_DIR / "lorawan_join.json"
)
SERVICE_NAME = "lorawan-tracker"
SERVICE_DIR = Path.home() / ".config" / "systemd" / "user"
SERVICE_FILE = SERVICE_DIR / f"{SERVICE_NAME}.service"
STATUS_FILE = Path.home() / ".local" / "share" / "lorawan-tracker" / "status.json"
SERVICE_LOG_FILE = Path.home() / ".local" / "share" / "lorawan-tracker" / "service.log"
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "lorawan_join_gui.desktop"
LORA_LIB = Path.home() / "sx126x_lorawan_hat_code" / "python" / "lora"


FIELDS = [
    ("app_eui", "JoinEUI/AppEUI"),
    ("dev_eui", "DevEUI"),
    ("app_key", "AppKey"),
    ("dev_nonce", "DevNonce (optional)"),
    ("bus", "SPI bus"),
    ("cs", "SPI CS"),
    ("reset_pin", "RESET pin"),
    ("busy_pin", "BUSY pin"),
    ("irq_pin", "IRQ pin"),
    ("txen_pin", "TXEN pin"),
    ("rxen_pin", "RXEN pin"),
    ("join_frequency", "Join frequency (Hz)"),
    ("join_sf", "Join SF"),
    ("join_bandwidth", "Join bandwidth (Hz)"),
    ("join_coding_rate", "Join coding rate"),
    ("join_preamble", "Join preamble"),
    ("uplink_frequency", "Uplink frequency (Hz)"),
    ("uplink_sf", "Uplink SF"),
    ("uplink_interval", "Uplink interval (s)"),
    ("tx_power", "TX power (dBm)"),
    ("sync_word", "Sync word"),
    ("join_accept_timeout", "Join accept timeout (s)"),
    ("tx_timeout", "TX timeout (s)"),
]

DEFAULTS = {
    "bus": 0,
    "cs": 0,
    "reset_pin": 18,
    "busy_pin": 20,
    "irq_pin": 16,
    "txen_pin": 6,
    "rxen_pin": -1,
    "join_frequency": 868100000,
    "join_sf": 12,
    "join_bandwidth": 125000,
    "join_coding_rate": 5,
    "join_preamble": 8,
    "uplink_frequency": 868300000,
    "uplink_sf": 7,
    "uplink_interval": 10,
    "tx_power": 14,
    "sync_word": "0x3444",
    "join_accept_timeout": 8.0,
    "tx_timeout": 10.0,
    "scan_eu868": True,
    "confirmed_uplink": True,
}


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object")
    return data


def save_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _svc(cmd: str) -> bool:
    result = subprocess.run(["systemctl", "--user", cmd, SERVICE_NAME], capture_output=True, text=True)
    return result.returncode == 0


def _svc_is_installed() -> bool:
    return SERVICE_FILE.exists()


def _svc_is_active() -> bool:
    result = subprocess.run(["systemctl", "--user", "is-active", SERVICE_NAME], capture_output=True, text=True)
    return result.stdout.strip() == "active"


def _svc_is_enabled() -> bool:
    result = subprocess.run(["systemctl", "--user", "is-enabled", SERVICE_NAME], capture_output=True, text=True)
    return result.stdout.strip() == "enabled"


def _read_service_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def _read_cached_session(profile_path: Path) -> dict:
    session_path = profile_path.with_name(profile_path.stem + "_session.json")
    try:
        session = json.loads(session_path.read_text())
    except Exception:
        return {}
    return session if isinstance(session, dict) else {}


def _remove_cached_session(profile_path: Path) -> bool:
    session_path = profile_path.with_name(profile_path.stem + "_session.json")
    try:
        session_path.unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception:
        raise


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


def _service_unit(profile_path: Path) -> str:
    return "\n".join([
        "[Unit]",
        "Description=LoRaWAN GPS Tracker",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={sys.executable} -u {SERVICE_SCRIPT} --config {profile_path}",
        f"WorkingDirectory={Path.home()}",
        f"Environment=PYTHONPATH={LORA_LIB}",
        "Environment=PYTHONUNBUFFERED=1",
        "Restart=on-failure",
        "RestartSec=30s",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


class JoinGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Pi LoRa Device  v{VERSION}")
        self.update_idletasks()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(max(1100, int(screen_width * 0.9)), max(860, screen_width - 40))
        height = min(max(780, int(screen_height * 0.88)), max(600, screen_height - 40))
        self.geometry(f"{width}x{height}")
        self.minsize(min(1000, max(780, screen_width - 80)), min(720, max(500, screen_height - 80)))

        self.config_path = tk.StringVar(value=str(DEFAULT_CONFIG))
        self.scan_eu868 = tk.BooleanVar(value=bool(DEFAULTS["scan_eu868"]))
        self.confirmed_uplink = tk.BooleanVar(value=bool(DEFAULTS["confirmed_uplink"]))
        self.status_text = tk.StringVar(value="Ready")
        self.entries: dict[str, tk.StringVar] = {key: tk.StringVar() for key, _ in FIELDS}
        self._service_status = tk.StringVar(value="Service not installed")
        self._session: dict[str, str] = {}
        self._session_vars = {
            "dev_addr": tk.StringVar(value="—"),
            "nwk_skey": tk.StringVar(value="—"),
            "app_skey": tk.StringVar(value="—"),
        }
        self._gps_status = tk.StringVar(value="—")
        self._tracker_interval = tk.StringVar(value="10")

        self._build_ui()
        self._load_from_default()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        banner = ttk.LabelFrame(root, text="Device mode", padding=10)
        banner.pack(fill="x", pady=(0, 8))
        ttk.Label(
            banner,
            text="This app runs the Raspberry Pi 4 + SX126x HAT as a standalone LoRaWAN device. It does not run a server.",
            wraplength=820,
            justify="left",
        ).pack(anchor="w")

        path_row = ttk.Frame(root)
        path_row.pack(fill="x", pady=(0, 8))
        ttk.Label(path_row, text="Profile file").pack(side="left")
        path_entry = ttk.Entry(path_row, textvariable=self.config_path)
        path_entry.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(path_row, text="Browse", command=self._browse_profile).pack(side="left", padx=(0, 6))
        ttk.Button(path_row, text="Load", command=self.load_profile).pack(side="left")

        button_row = ttk.Frame(root)
        button_row.pack(fill="x", pady=(10, 8))
        ttk.Button(button_row, text="Load Profile", command=self.load_profile).pack(side="left")
        ttk.Button(button_row, text="Save Profile", command=self.save_profile).pack(side="left", padx=8)
        ttk.Button(button_row, text="Save As…", command=self.save_profile_as).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Use Example", command=self.load_example).pack(side="left", padx=8)
        ttk.Button(button_row, text="Settings…", command=self._open_settings_dialog).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Setup Pi…", command=self._open_pi_setup_dialog).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Install Auto-start", command=self.install_autostart).pack(side="right")
        ttk.Button(button_row, text="Dashboard…", command=self._open_dashboard).pack(side="right", padx=(0, 8))
        ttk.Button(button_row, text="Clear Log", command=self.clear_log).pack(side="right", padx=(0, 8))

        status_row = ttk.Frame(root)
        status_row.pack(fill="x", pady=(0, 6))
        ttk.Label(status_row, text="Status").pack(side="left")
        ttk.Label(status_row, textvariable=self.status_text, relief="sunken", anchor="w").pack(side="left", fill="x", expand=True, padx=8)
        self.progress = ttk.Progressbar(status_row, mode="indeterminate", length=140)
        self.progress.pack(side="right")

        keys_frame = ttk.LabelFrame(root, text="Session Keys", padding=8)
        keys_frame.pack(fill="x", pady=(0, 6))
        keys_frame.columnconfigure(1, weight=1)
        for _row, (_key, _label) in enumerate([("dev_addr", "DevAddr"), ("nwk_skey", "NwkSKey"), ("app_skey", "AppSKey")]):
            ttk.Label(keys_frame, text=f"{_label}:", width=10, anchor="w").grid(row=_row, column=0, sticky="w", pady=2)
            ttk.Entry(keys_frame, textvariable=self._session_vars[_key], state="readonly", width=42
                      ).grid(row=_row, column=1, sticky="ew", padx=4)
            ttk.Button(keys_frame, text="Copy", width=6,
                       command=lambda k=_key: self._copy_to_clipboard(self._session_vars[k].get())
                       ).grid(row=_row, column=2, padx=(0, 4))
        self._keys_save_label = ttk.Label(keys_frame, text="No session yet", foreground="gray")
        self._keys_save_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        service_frame = ttk.LabelFrame(root, text="Tracker Service", padding=8)
        service_frame.pack(fill="x", pady=(0, 6))
        service_frame.columnconfigure(1, weight=1)
        ttk.Label(service_frame, text="Service:", width=10, anchor="w").grid(row=0, column=0, sticky="w")
        ttk.Label(service_frame, textvariable=self._service_status, anchor="w").grid(row=0, column=1, sticky="ew", padx=4)
        self._service_dev_addr = tk.StringVar(value="—")
        self._service_fcnt = tk.StringVar(value="—")
        self._service_last_join = tk.StringVar(value="—")
        self._service_last_uplink = tk.StringVar(value="—")
        self._service_uplinks = tk.StringVar(value="—")
        for row, (label, var) in enumerate([
            ("DevAddr", self._service_dev_addr),
            ("FCnt", self._service_fcnt),
            ("Last join", self._service_last_join),
            ("Last uplink", self._service_last_uplink),
            ("Uplinks sent", self._service_uplinks),
        ], start=1):
            ttk.Label(service_frame, text=f"{label}:", width=10, anchor="w").grid(row=row, column=0, sticky="w", pady=1)
            ttk.Label(service_frame, textvariable=var, anchor="w").grid(row=row, column=1, sticky="ew", padx=4, pady=1)
        service_btns = ttk.Frame(service_frame)
        service_btns.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._service_start_btn = ttk.Button(service_btns, text="Start Service", command=self.start_service)
        self._service_stop_btn = ttk.Button(service_btns, text="Stop Service", command=self.stop_service)
        self._service_restart_btn = ttk.Button(service_btns, text="Restart Service", command=self.restart_service)
        self._service_rejoin_btn = ttk.Button(service_btns, text="Force Rejoin", command=self.force_rejoin_service)
        self._service_install_btn = ttk.Button(service_btns, text="Install Service", command=self.install_service)
        self._service_start_btn.pack(side="left")
        self._service_stop_btn.pack(side="left", padx=(6, 0))
        self._service_restart_btn.pack(side="left", padx=(6, 0))
        self._service_rejoin_btn.pack(side="left", padx=(6, 0))
        self._service_install_btn.pack(side="right")

        service_log_frame = ttk.LabelFrame(root, text="Service Log", padding=8)
        service_log_frame.pack(fill="both", expand=True, pady=(0, 6))
        self._service_log = tk.Text(service_log_frame, wrap="none", height=12, state="disabled")
        self._service_log.pack(side="left", fill="both", expand=True)
        service_log_scrollbar = ttk.Scrollbar(service_log_frame, orient="vertical", command=self._service_log.yview)
        service_log_scrollbar.pack(side="right", fill="y")
        self._service_log.configure(yscrollcommand=service_log_scrollbar.set)
        service_log_actions = ttk.Frame(service_log_frame)
        service_log_actions.pack(fill="x", pady=(4, 0))
        ttk.Button(service_log_actions, text="Copy Service Log", command=self.copy_service_log).pack(side="left")
        ttk.Button(service_log_actions, text="Clear Service Log", command=self.clear_service_log).pack(side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(root, text="Output", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, wrap="word", height=8)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.tag_configure("success", foreground="#006400")
        self.log.tag_configure("warning", foreground="#B8860B")
        self.log.tag_configure("error",   foreground="#CC0000")
        self.log.tag_configure("info",    foreground="#888888")

        self._log_line(f"Join script: {JOIN_SCRIPT}")
        self._log_line(f"Service script: {SERVICE_SCRIPT}")
        self._log_line(f"Default profile: {DEFAULT_CONFIG}")
        self._set_status("Ready")
        self.after(1000, self._refresh_service_status)

    def _refresh_service_log(self, schedule: bool = True) -> None:
        if SERVICE_LOG_FILE.exists():
            try:
                lines = SERVICE_LOG_FILE.read_text(encoding="utf-8").splitlines()
                text = "\n".join(lines[-200:]) + ("\n" if lines else "")
            except Exception:
                text = "Service log unavailable.\n"
        else:
            result = subprocess.run(
                ["journalctl", "--user", "-u", SERVICE_NAME, "-n", "120", "--no-pager", "--output=short-monotonic"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                text = result.stdout
            else:
                status = _read_service_status()
                text = (
                    "Service log not yet available.\n"
                    f"State: {status.get('state', '—')}\n"
                    f"Updated: {_fmt_time(status.get('updated'))}\n"
                )

        self._service_log.configure(state="normal")
        self._service_log.delete("1.0", tk.END)
        self._service_log.insert(tk.END, text)
        self._service_log.configure(state="disabled")
        self._service_log.see(tk.END)

        if schedule:
            self.after(2000, self._refresh_service_log)

    def _open_dashboard(self) -> None:
        import subprocess as _sp
        _sp.Popen(
            [sys.executable, str(APP_DIR / "lorawan_dashboard.py")],
            cwd=str(Path.home()),
        )

    def _open_pi_setup_dialog(self) -> None:
        import threading
        import subprocess as _sp

        SETUP_SCRIPT = str(APP_DIR / "rpi_setup.sh")

        dlg = tk.Toplevel(self)
        dlg.title("Raspberry Pi Hardware Setup")
        dlg.resizable(True, True)
        dlg.transient(self)
        dlg.grab_set()

        info = ttk.LabelFrame(dlg, text="What this does", padding=10)
        info.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(
            info,
            text=(
                "Configures this Raspberry Pi for full lorawan-tracker operation:\n"
                "  \u2022  enable_uart=1  — activates the hardware UART\n"
                "  \u2022  dtoverlay=disable-bt  — frees the full PL011 UART for GPS (GPIO 14/15)\n"
                "  \u2022  Disables serial console  — so the GPS module can use the UART\n"
                "  \u2022  Enables SPI  — required for the LoRa radio\n\n"
                "After applying, a reboot is required.  Bluetooth will be disabled."
            ),
            justify="left",
        ).pack(anchor="w")

        log_frame = ttk.LabelFrame(dlg, text="Output", padding=4)
        log_frame.pack(fill="both", expand=True, padx=10, pady=4)
        log_text = tk.Text(log_frame, wrap="word", height=14, state="disabled",
                           font=("Monospace", 10))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
        log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        log_text.pack(fill="both", expand=True)
        log_text.tag_configure("ok",      foreground="#006400")
        log_text.tag_configure("set",     foreground="#B8600B")
        log_text.tag_configure("add",     foreground="#00008B")
        log_text.tag_configure("reboot",  foreground="#CC0000", font=("Monospace", 10, "bold"))
        log_text.tag_configure("success", foreground="#006400", font=("Monospace", 10, "bold"))

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btn_row, text="Close", command=dlg.destroy).pack(side="left")

        reboot_btn = ttk.Button(
            btn_row, text="Reboot Now",
            command=lambda: _sp.Popen(["sudo", "reboot"])
        )
        reboot_btn.pack(side="right", padx=(6, 0))
        reboot_btn.state(["disabled"])

        apply_btn = ttk.Button(btn_row, text="Apply Configuration")
        apply_btn.pack(side="right")

        def _append(line: str) -> None:
            log_text.configure(state="normal")
            tag = ""
            if "[OK]" in line:     tag = "ok"
            elif "[SET]" in line:  tag = "set"
            elif "[ADD]" in line:  tag = "add"
            elif "REBOOT_REQUIRED" in line: tag = "reboot"
            elif "SETUP_OK" in line:        tag = "success"
            log_text.insert("end", line + "\n", tag)
            log_text.see("end")
            log_text.configure(state="disabled")

        def _run() -> None:
            apply_btn.state(["disabled"])
            reboot_needed = False

            def _worker() -> None:
                import re as _re
                nonlocal reboot_needed
                for launcher in (["pkexec", "bash", SETUP_SCRIPT],
                                  ["sudo", "bash", SETUP_SCRIPT]):
                    try:
                        proc = _sp.Popen(
                            launcher,
                            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True
                        )
                        for raw in proc.stdout:  # type: ignore[union-attr]
                            line = raw.rstrip()
                            if "REBOOT_REQUIRED" in line:
                                reboot_needed = True
                            dlg.after(0, _append, line)
                        proc.wait()
                        if proc.returncode == 0 and reboot_needed:
                            dlg.after(0, lambda: reboot_btn.state(["!disabled"]))
                        elif proc.returncode != 0 and proc.returncode != 126:  # 126 = cancelled pkexec
                            dlg.after(0, _append, f"\nError: process exited with code {proc.returncode}")
                        break  # success or cancelled — don't try next launcher
                    except FileNotFoundError:
                        continue  # pkexec not found, try sudo
                    except Exception as exc:
                        dlg.after(0, _append, f"Error: {exc}")
                        break

                # Always ensure the service unit points to the installed package
                svc = Path.home() / ".config" / "systemd" / "user" / "lorawan-tracker.service"
                if svc.exists():
                    original = svc.read_text()
                    fixed = _re.sub(
                        r'ExecStart=\S+lorawan_service\.py',
                        f'ExecStart=/usr/bin/python3 -u {SERVICE_SCRIPT}',
                        original
                    )
                    fixed = _re.sub(r'Environment=PYTHONPATH=[^\n]*sx126x[^\n]*\n?', '', fixed)
                    if fixed != original:
                        svc.write_text(fixed)
                        _sp.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
                        dlg.after(0, _append, f"\nService unit → updated to use /usr/lib/lorawan-tracker/")
                        dlg.after(0, _append, "Run 'Restart Service' in the main window to apply.")
                    else:
                        dlg.after(0, _append, "\nService unit: already using installed package path.")

            threading.Thread(target=_worker, daemon=True).start()

        apply_btn.configure(command=_run)

        dlg.update_idletasks()
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        w = max(600, min(800, int(sw * 0.58)))
        h = max(500, min(700, int(sh * 0.68)))
        dlg.geometry(f"{w}x{h}")
        dlg.minsize(520, 440)

    def _open_settings_dialog(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Device Join Settings")
        dlg.resizable(True, True)
        dlg.transient(self)
        dlg.grab_set()

        form = ttk.LabelFrame(dlg, text="Device join settings", padding=10)
        form.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        form.columnconfigure(0, weight=1)

        for index, (key, label) in enumerate(FIELDS):
            row = ttk.Frame(form)
            row.grid(row=index, column=0, sticky="ew", pady=2)
            row.columnconfigure(1, weight=1)
            ttk.Label(row, text=label, width=22).grid(row=0, column=0, sticky="w")
            ttk.Entry(row, textvariable=self.entries[key]).grid(row=0, column=1, sticky="ew")

        options = ttk.Frame(form)
        options.grid(row=len(FIELDS), column=0, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(options, text="Scan EU868 channels", variable=self.scan_eu868).pack(side="left")
        ttk.Checkbutton(options, text="Confirmed uplinks", variable=self.confirmed_uplink).pack(side="left", padx=(12, 0))

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btn_row, text="Close", command=dlg.destroy).pack(side="right")

        dlg.update_idletasks()
        dlg_screen_width = dlg.winfo_screenwidth()
        dlg_screen_height = dlg.winfo_screenheight()
        dlg_width = min(max(720, int(dlg_screen_width * 0.58)), max(620, dlg_screen_width - 60))
        dlg_height = min(max(760, int(dlg_screen_height * 0.82)), max(680, dlg_screen_height - 80))
        dlg.geometry(f"{dlg_width}x{dlg_height}")
        dlg.minsize(min(720, max(620, dlg_screen_width - 120)), min(680, max(600, dlg_screen_height - 120)))

    def _browse_profile(self) -> None:
        current = Path(self.config_path.get()).expanduser()
        initial_dir = current.parent if current.parent.exists() else DEFAULT_CONFIG.parent
        filename = filedialog.askopenfilename(
            title="Open profile file",
            initialdir=str(initial_dir),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if filename:
            self.config_path.set(filename)
            try:
                self._apply_config(load_config(Path(filename)))
                self._log_line(f"Loaded profile from {Path(filename).name}")
            except Exception as exc:
                messagebox.showerror("Open profile", str(exc))

    def _load_from_default(self) -> None:
        if DEFAULT_CONFIG.exists():
            try:
                self._apply_config(load_config(DEFAULT_CONFIG))
                self._log_line(f"Loaded {DEFAULT_CONFIG.name}")
            except Exception as exc:
                self._log_line(f"Could not load default profile: {exc}")
        else:
            self._apply_config(DEFAULTS)

    def _apply_config(self, config: dict) -> None:
        merged = dict(DEFAULTS)
        merged.update(config)

        for key, var in self.entries.items():
            value = merged.get(key, "")
            var.set(str(value))

        self.scan_eu868.set(bool(merged.get("scan_eu868", False)))
        self.confirmed_uplink.set(bool(merged.get("confirmed_uplink", True)))
        self._set_status("Profile loaded")

    def _service_profile_path(self) -> Path:
        service_status = _read_service_status()
        config_path = service_status.get("config") or self.config_path.get()
        return Path(str(config_path)).expanduser().resolve()

    def _save_current_profile(self, profile_path: Path | None = None) -> Path:
        if profile_path is None:
            profile_path = Path(self.config_path.get()).expanduser().resolve()
        config = self._normalize_config(self._read_config())
        save_config(profile_path, config)
        return profile_path

    def _read_config(self) -> dict:
        config: dict[str, object] = {}
        for key, entry in self.entries.items():
            value = entry.get().strip()
            if value:
                config[key] = value

        config["scan_eu868"] = self.scan_eu868.get()
        config["confirmed_uplink"] = self.confirmed_uplink.get()
        return config

    def _normalize_config(self, config: dict) -> dict:
        normalized = dict(config)

        integer_fields = {
            "bus",
            "cs",
            "reset_pin",
            "busy_pin",
            "irq_pin",
            "txen_pin",
            "rxen_pin",
            "join_frequency",
            "join_sf",
            "join_bandwidth",
            "join_coding_rate",
            "join_preamble",
            "uplink_frequency",
            "uplink_sf",
            "uplink_interval",
            "tx_power",
        }
        float_fields = {"join_accept_timeout", "tx_timeout"}

        for key in integer_fields:
            if key in normalized and normalized[key] != "":
                v = str(normalized[key])
                try:
                    normalized[key] = int(v, 0)
                except ValueError:
                    normalized[key] = int(float(v))  # handle stored floats like "10.0"

        for key in float_fields:
            if key in normalized and normalized[key] != "":
                normalized[key] = float(str(normalized[key]))

        if "sync_word" in normalized and normalized["sync_word"] != "":
            sync_word = str(normalized["sync_word"]).strip()
            normalized["sync_word"] = int(sync_word, 0) if sync_word.lower().startswith("0x") or sync_word.isdigit() else sync_word

        if not normalized.get("app_eui") or not normalized.get("dev_eui") or not normalized.get("app_key"):
            raise ValueError("JoinEUI/AppEUI, DevEUI, and AppKey are required")

        return normalized

    def load_profile(self) -> None:
        path = Path(self.config_path.get()).expanduser()
        try:
            config = load_config(path)
        except Exception as exc:
            messagebox.showerror("Load profile", str(exc))
            return

        self._apply_config(config)
        self._log_line(f"Loaded profile from {path}")

    def load_example(self) -> None:
        example = APP_DIR / "lorawan_join.example.json"
        try:
            self._apply_config(load_config(example))
            self.config_path.set(str(DEFAULT_CONFIG))
            self._log_line(f"Loaded example profile from {example.name}")
            self._set_status("Example profile loaded")
        except Exception as exc:
            messagebox.showerror("Use example", str(exc))

    def save_profile(self) -> None:
        path = Path(self.config_path.get()).expanduser()
        # If the file doesn't exist yet, ask where to save it
        if not path.exists():
            chosen = filedialog.asksaveasfilename(
                title="Save profile as",
                initialdir=str(DEFAULT_CONFIG.parent),
                initialfile=path.name,
                defaultextension=".json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            )
            if not chosen:
                return
            path = Path(chosen)
            self.config_path.set(str(path))
        try:
            config = self._normalize_config(self._read_config())
            save_config(path, config)
        except Exception as exc:
            messagebox.showerror("Save profile", str(exc))
            return
        self._log_line(f"Saved profile to {path}")
        self._set_status("Profile saved")

    def save_profile_as(self) -> None:
        current = Path(self.config_path.get()).expanduser()
        chosen = filedialog.asksaveasfilename(
            title="Save profile as",
            initialdir=str(current.parent if current.parent.exists() else DEFAULT_CONFIG.parent),
            initialfile=current.name,
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not chosen:
            return
        path = Path(chosen)
        self.config_path.set(str(path))
        try:
            config = self._normalize_config(self._read_config())
            save_config(path, config)
        except Exception as exc:
            messagebox.showerror("Save profile as", str(exc))
            return
        self._log_line(f"Saved profile to {path}")
        self._set_status("Profile saved")

    def copy_service_log(self) -> None:
        try:
            text = self._service_log.get("1.0", "end-1c")
        except tk.TclError:
            text = ""
        if not text:
            messagebox.showinfo("Copy Service Log", "The service log is empty.")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self._set_status("Service log copied to clipboard")

    def clear_service_log(self) -> None:
        if not messagebox.askyesno("Clear Service Log", "Clear the saved service log file?"):
            return
        try:
            SERVICE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            SERVICE_LOG_FILE.write_text("", encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Clear Service Log", str(exc))
            return

        self._refresh_service_log(schedule=False)
        self._set_status("Service log cleared")

    def install_autostart(self) -> None:
        try:
            AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
            exec_cmd = "lorawan-tracker" if _SYSTEM_INSTALL else f"{sys.executable} {APP_DIR / 'lorawan_join_gui.py'}"
            entry_lines = [
                "[Desktop Entry]",
                "Type=Application",
                "Name=LoRaWAN Join GUI",
                f"Exec={exec_cmd}",
                "Terminal=false",
                "X-GNOME-Autostart-enabled=true",
                "NoDisplay=false",
                "",
            ]
            if not _SYSTEM_INSTALL:
                entry_lines.insert(4, f"Path={APP_DIR}")
            AUTOSTART_FILE.write_text("\n".join(entry_lines), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Install auto-start", str(exc))
            return

        self._log_line(f"Installed autostart entry at {AUTOSTART_FILE}")
        self._set_status("Auto-start installed")

    def install_service(self) -> None:
        try:
            profile_path = self._save_current_profile()

            SERVICE_DIR.mkdir(parents=True, exist_ok=True)
            SERVICE_FILE.write_text(_service_unit(profile_path), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Install service", str(exc))
            return

        _enable_linger()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
        subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], capture_output=True, text=True)

        self._log_line(f"Installed service at {SERVICE_FILE}")
        self._set_status("Service installed")
        self._refresh_service_status(schedule=False)

        if messagebox.askyesno("Install service", "Service installed and enabled on boot. Start it now?"):
            self.start_service()

    def start_service(self) -> None:
        if not _svc_is_installed():
            if messagebox.askyesno("Start service", "Service is not installed. Install it now?"):
                self.install_service()
            return
        try:
            self._save_current_profile(self._service_profile_path())
        except Exception as exc:
            messagebox.showerror("Start service", str(exc))
            return
        if _svc("start"):
            self._log_line("Service start requested.")
        else:
            messagebox.showerror("Start service", "Could not start the service. Check systemctl --user status lorawan-tracker")
        self._refresh_service_status(schedule=False)

    def stop_service(self) -> None:
        if _svc("stop"):
            self._log_line("Service stop requested.")
        self._refresh_service_status(schedule=False)

    def restart_service(self) -> None:
        try:
            self._save_current_profile(self._service_profile_path())
        except Exception as exc:
            messagebox.showerror("Restart service", str(exc))
            return
        if _svc("restart"):
            self._log_line("Service restart requested.")
        else:
            messagebox.showerror("Restart service", "Could not restart the service. Check systemctl --user status lorawan-tracker")
        self._refresh_service_status(schedule=False)

    def force_rejoin_service(self) -> None:
        if not _svc_is_installed():
            messagebox.showerror("Force rejoin", "Service is not installed. Install it first.")
            return

        try:
            profile_path = self._save_current_profile(self._service_profile_path())
        except Exception as exc:
            messagebox.showerror("Force rejoin", str(exc))
            return

        try:
            removed = _remove_cached_session(profile_path)
        except Exception as exc:
            messagebox.showerror("Force rejoin", f"Could not remove cached session: {exc}")
            return

        if _svc("restart"):
            self._log_line("Force rejoin requested.")
            if removed:
                self._log_line(f"Removed cached session {profile_path.with_name(profile_path.stem + '_session.json')}")
            else:
                self._log_line("No cached session file was present.")
        else:
            messagebox.showerror("Force rejoin", "Could not restart the service. Check systemctl --user status lorawan-tracker")
        self._refresh_service_status(schedule=False)

    def _refresh_service_status(self, schedule: bool = True) -> None:
        installed = _svc_is_installed()
        active = _svc_is_active() if installed else False
        enabled = _svc_is_enabled() if installed else False
        service_status = _read_service_status() if installed else {}
        cached_session = _read_cached_session(Path(self.config_path.get()).expanduser())

        if not installed:
            display_status = "Not installed"
        elif active:
            display_status = "Running"
        elif enabled:
            display_status = "Installed, enabled on boot"
        else:
            display_status = "Installed, stopped"

        self._service_status.set(display_status)
        self._service_dev_addr.set(str(service_status.get("dev_addr", "—")))
        self._service_fcnt.set(str(service_status.get("fcnt", "—")))
        self._service_last_join.set(_fmt_time(service_status.get("last_join")))
        self._service_last_uplink.set(_fmt_time(service_status.get("last_uplink")))
        self._service_uplinks.set(str(service_status.get("uplinks_sent", "—")))
        for key, var in self._session_vars.items():
            var.set(cached_session.get(key, "—"))
        if cached_session.get("dev_addr") and cached_session.get("nwk_skey") and cached_session.get("app_skey"):
            self._keys_save_label.configure(text=f"Loaded → {Path(self.config_path.get()).expanduser().stem}_session.json", foreground="#006400")
        else:
            self._keys_save_label.configure(text="No cached session", foreground="gray")
        self._service_start_btn.configure(state="disabled" if (not installed or active) else "normal")
        self._service_stop_btn.configure(state="normal" if active else "disabled")
        self._service_restart_btn.configure(state="normal" if installed else "disabled")
        self._service_rejoin_btn.configure(state="normal" if installed else "disabled")
        self._service_install_btn.configure(state="disabled" if installed else "normal")
        self._refresh_service_log(schedule=False)
        if schedule:
            self.after(2000, self._refresh_service_status)

    def clear_log(self) -> None:
        self.log.delete("1.0", tk.END)

    def _set_controls_state(self, state: str) -> None:
        for widget in self.winfo_children():
            self._set_state_recursive(widget, state)

    def _set_state_recursive(self, widget: tk.Widget, state: str) -> None:
        if isinstance(widget, (ttk.Entry, ttk.Button, ttk.Checkbutton)):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._set_state_recursive(child, state)

    def _set_status(self, text: str) -> None:
        self.status_text.set(text)

    def _log_line(self, text: str, tag: str | None = None) -> None:
        def append() -> None:
            if tag:
                self.log.insert(tk.END, text + "\n", tag)
            else:
                self.log.insert(tk.END, text + "\n")
            self.log.see(tk.END)

        self.after(0, append)

    def _copy_to_clipboard(self, text: str) -> None:
        if text and text != "—":
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            self._set_status("Copied to clipboard")


def main() -> int:
    app = JoinGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())