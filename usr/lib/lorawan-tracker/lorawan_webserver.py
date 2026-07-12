#!/usr/bin/env python3
"""LoRaWAN GPS Tracker — mobile web dashboard.

Lightweight HTTP server (no external dependencies) that serves a
mobile-optimised status page and a JSON API.

Endpoints:
  GET /              mobile dashboard (auto-refreshes every 2 s)
  GET /api/status    current status JSON (from status.json)
  GET /api/track     recent GPS track points as JSON array

Run standalone:
  python3 lorawan_webserver.py          # port 8080

Or install as a systemd user service via the dashboard GUI.
Once running, open on your phone:
  http://10.42.0.1:8080       (when connected to the Pi hotspot)
  http://lorapi-1.local:8080  (mDNS, same network)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR   = Path.home() / ".local" / "share" / "lorawan-tracker"
STATUS_FILE = DATA_DIR / "status.json"
TRACK_FILE  = DATA_DIR / "track.jsonl"
SERVICE_NAME = "lorawan-tracker"
DEFAULT_CONFIG = Path(__file__).resolve().with_name("lorawan_join.json")
SESSION_FILE = DEFAULT_CONFIG.with_name(DEFAULT_CONFIG.stem + "_session.json")

DEFAULT_PORT  = 8080
HISTORY_MAX   = 500   # points kept in memory
TRACK_TAIL    = 200   # lines read from track.jsonl at startup/refresh

# Config file: use user config dir when installed system-wide
_APP_DIR = Path(__file__).resolve().parent
_SYSTEM_INSTALL = str(_APP_DIR).startswith("/usr/")
DEFAULT_CONFIG = (
    Path.home() / ".config" / "lorawan-tracker" / "lorawan_join.json"
    if _SYSTEM_INSTALL else _APP_DIR / "lorawan_join.json"
)
SESSION_FILE = DEFAULT_CONFIG.with_name(DEFAULT_CONFIG.stem + "_session.json")

# ---------------------------------------------------------------------------
# In-memory track buffer — updated by background poller
# ---------------------------------------------------------------------------

_track: deque[dict] = deque(maxlen=HISTORY_MAX)
_last_fix_id: str | None = None


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def _service_config_path() -> Path:
  status = _read_status()
  config = status.get("config")
  if config:
    return Path(str(config)).expanduser()
  return DEFAULT_CONFIG


def _session_path(config_path: Path) -> Path:
  return config_path.with_name(config_path.stem + "_session.json")


def _service_action(action: str) -> dict:
  result = subprocess.run(
    ["systemctl", "--user", action, SERVICE_NAME],
    capture_output=True,
    text=True,
  )
  ok = result.returncode == 0
  return {
    "ok": ok,
    "action": action,
    "service": SERVICE_NAME,
    "state": _read_status().get("state", "unknown"),
    "message": result.stderr.strip() or result.stdout.strip() or ("ok" if ok else "failed"),
  }


def _rejoin_action() -> dict:
  removed = False
  session_file = _session_path(_service_config_path())
  try:
    if session_file.exists():
      session_file.unlink()
      removed = True
  except Exception as exc:
    return {
      "ok": False,
      "action": "rejoin",
      "service": SERVICE_NAME,
      "state": _read_status().get("state", "unknown"),
      "message": f"failed to remove cached session: {exc}",
    }

  result = subprocess.run(
    ["systemctl", "--user", "restart", SERVICE_NAME],
    capture_output=True,
    text=True,
  )
  ok = result.returncode == 0
  return {
    "ok": ok,
    "action": "rejoin",
    "service": SERVICE_NAME,
    "state": _read_status().get("state", "unknown"),
    "message": (
      ("cached session removed, service restart requested" if removed else "cached session not present, service restart requested")
      if ok else
      (result.stderr.strip() or result.stdout.strip() or "failed")
    ),
  }


def _load_track_from_file() -> None:
    """Pre-populate the in-memory track from the persistent track file."""
    if not TRACK_FILE.exists():
        return
    try:
        lines = TRACK_FILE.read_text().splitlines()
        for line in lines[-TRACK_TAIL:]:
            try:
                _track.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass


def _poll_status_loop() -> None:
    """Background thread: watch status.json for new GPS fixes."""
    global _last_fix_id
    _load_track_from_file()
    while True:
        try:
            s = _read_status()
            fix: dict | None = s.get("last_fix")
            if fix:
                fid = fix.get("time")
                if fid and fid != _last_fix_id:
                    _last_fix_id = fid
                    _track.append({
                        "lat":  fix["lat"],
                        "lon":  fix["lon"],
                        "alt":  fix.get("alt_m", 0.0),
                        "t":    fix["time"],
                        "fcnt": s.get("fcnt", 0),
                    })
        except Exception:
            pass
        time.sleep(1)

# ---------------------------------------------------------------------------
# Config read / write helpers
# ---------------------------------------------------------------------------

def _read_config() -> dict:
    try:
        return json.loads(DEFAULT_CONFIG.read_text()) if DEFAULT_CONFIG.exists() else {}
    except Exception:
        return {}


def _write_config(data: dict) -> None:
    DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_config()
    for k, v in data.items():
        if v != "" and v is not None:
            existing[k] = v
    DEFAULT_CONFIG.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")


# HTML template for the config page — /*CFG*/ is replaced at request time
_CONFIG_TPL = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#0f0f0f">
<title>LoRa Config</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:520px;margin:0 auto;padding-bottom:32px}
.hdr{padding:14px 16px;border-bottom:1px solid #222;display:flex;align-items:center;justify-content:space-between}
.hdr h1{font-size:17px;font-weight:700}
.back{color:#4fc3f7;text-decoration:none;font-size:14px;font-weight:600}
.card{margin:10px 12px;padding:14px;background:#1a1a1a;border-radius:14px}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:#666;margin-bottom:12px}
.field{margin-bottom:12px}
.field label{display:block;font-size:11px;color:#888;margin-bottom:5px;letter-spacing:.3px}
.field input[type=text],.field input[type=number]{width:100%;background:#252525;border:1px solid #333;border-radius:8px;color:#e0e0e0;padding:10px 12px;font-size:14px;font-family:monospace}
.field input:focus{outline:none;border-color:#1565c0;background:#1e2a3a}
.toggle{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid #252525}
.toggle:last-child{border-bottom:none}
.toggle span{font-size:13px}
.toggle input[type=checkbox]{width:22px;height:22px;accent-color:#1565c0;cursor:pointer}
.btn{display:block;width:100%;padding:14px;border-radius:10px;border:none;font-size:15px;font-weight:700;color:#fff;cursor:pointer;margin-top:8px;letter-spacing:.2px}
.btn-save{background:#1565c0}.btn-save:active{background:#0d47a1}
.btn-save-restart{background:#1a9641}.btn-save-restart:active{background:#145e31}
.msg{margin:10px 12px;padding:12px;border-radius:10px;font-size:13px;text-align:center;display:none}
.msg.ok{background:#1a564e;border:1px solid #1a9641;display:block}
.msg.err{background:#4a1515;border:1px solid #b71c1c;display:block}
details summary{font-size:10px;color:#555;cursor:pointer;letter-spacing:1px;text-transform:uppercase;padding:4px 0}
details[open] summary{margin-bottom:12px}
</style>
</head>
<body>
<div class="hdr">
  <h1>&#x2699;&#xFE0F;&nbsp;Configuration</h1>
  <a class="back" href="/">&larr; Dashboard</a>
</div>
<div id="msg" class="msg"></div>
<div class="card">
  <div class="card-title">Join Credentials (OTAA)</div>
  <div class="field"><label>AppEUI / JoinEUI (16 hex)</label>
    <input type="text" id="app_eui" maxlength="16" placeholder="0000000000000000" autocomplete="off" spellcheck="false"></div>
  <div class="field"><label>DevEUI (16 hex)</label>
    <input type="text" id="dev_eui" maxlength="16" placeholder="0000000000000000" autocomplete="off" spellcheck="false"></div>
  <div class="field"><label>AppKey (32 hex)</label>
    <input type="text" id="app_key" maxlength="32" placeholder="00000000000000000000000000000000" autocomplete="off" spellcheck="false"></div>
</div>
<div class="card">
  <div class="card-title">GPS</div>
  <div class="field"><label>GPS port</label>
    <input type="text" id="gps_port" placeholder="/dev/ttyAMA0" autocomplete="off" spellcheck="false"></div>
  <div class="field"><label>GPS timeout (seconds, 0 = send 0.0/0.0 immediately)</label>
    <input type="number" id="gps_timeout" min="0" max="300"></div>
</div>
<div class="card">
  <div class="card-title">Service</div>
  <div class="field"><label>Uplink interval (seconds)</label>
    <input type="number" id="uplink_interval" min="1" max="3600"></div>
  <div class="field"><label>Spreading Factor (7&ndash;12)</label>
    <input type="number" id="uplink_sf" min="7" max="12"></div>
  <div class="toggle"><span>Confirmed uplinks</span>
    <input type="checkbox" id="confirmed_uplink"></div>
  <div class="toggle"><span>Scan EU868 channels</span>
    <input type="checkbox" id="scan_eu868"></div>
</div>
<div class="card">
  <details>
    <summary>Advanced &mdash; radio hardware pins</summary>
    <div class="field"><label>TX Power (dBm, 2&ndash;22)</label>
      <input type="number" id="tx_power" min="2" max="22"></div>
    <div class="field"><label>Reset GPIO (BCM)</label>
      <input type="number" id="reset_pin"></div>
    <div class="field"><label>Busy GPIO (BCM)</label>
      <input type="number" id="busy_pin"></div>
    <div class="field"><label>IRQ GPIO (BCM)</label>
      <input type="number" id="irq_pin"></div>
    <div class="field"><label>TX Enable GPIO (BCM, -1 to skip)</label>
      <input type="number" id="txen_pin"></div>
  </details>
</div>
<div style="margin:0 12px">
  <button class="btn btn-save" onclick="save(false)">Save</button>
  <button class="btn btn-save-restart" onclick="save(true)">Save &amp; Restart Service</button>
</div>
<script>
/*CFG*/
const FIELDS=["app_eui","dev_eui","app_key","gps_port","gps_timeout",
  "uplink_interval","uplink_sf","confirmed_uplink","scan_eu868",
  "tx_power","reset_pin","busy_pin","irq_pin","txen_pin"];
window.onload=function(){
  FIELDS.forEach(k=>{
    const el=document.getElementById(k);
    if(!el||_CFG[k]===undefined)return;
    el.type==="checkbox"?el.checked=!!_CFG[k]:el.value=_CFG[k];
  });
};
async function save(andRestart){
  const msg=document.getElementById("msg");
  const d={};
  FIELDS.forEach(k=>{
    const el=document.getElementById(k);
    if(!el)return;
    d[k]=el.type==="checkbox"?el.checked:
         (el.type==="number"?Number(el.value):el.value.trim());
  });
  if(!d.app_eui||!d.dev_eui||!d.app_key){
    msg.className="msg err";msg.textContent="AppEUI, DevEUI and AppKey are required.";return;}
  d.restart=andRestart;
  try{
    const r=await fetch("/api/config",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(d)});
    const j=await r.json();
    msg.className="msg "+(j.ok?"ok":"err");
    msg.textContent=j.ok?(andRestart?"Saved. Service restarting\u2026":"Configuration saved.")
      :"Error: "+(j.message||"unknown");
    window.scrollTo(0,0);
  }catch(e){msg.className="msg err";msg.textContent="Request failed: "+e.message;}
}
</script>
</body>
</html>"""


def _config_page() -> str:
    cfg = _read_config()
    data = {
        "app_eui":          cfg.get("app_eui", ""),
        "dev_eui":          cfg.get("dev_eui", ""),
        "app_key":          cfg.get("app_key", ""),
        "gps_port":         cfg.get("gps_port", "/dev/ttyAMA0"),
        "gps_timeout":      cfg.get("gps_timeout", 90),
        "uplink_interval":  cfg.get("uplink_interval", 10),
        "uplink_sf":        cfg.get("uplink_sf", 7),
        "confirmed_uplink": bool(cfg.get("confirmed_uplink", True)),
        "scan_eu868":       bool(cfg.get("scan_eu868", False)),
        "tx_power":         cfg.get("tx_power", 14),
        "reset_pin":        cfg.get("reset_pin", 18),
        "busy_pin":         cfg.get("busy_pin", 20),
        "irq_pin":          cfg.get("irq_pin", 16),
        "txen_pin":         cfg.get("txen_pin", 6),
    }
    return _CONFIG_TPL.replace("/*CFG*/", "const _CFG=" + json.dumps(data) + ";")


# ---------------------------------------------------------------------------
# HTML  (single-file, no CDN dependencies)
# ---------------------------------------------------------------------------

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0f0f0f">
<title>LoRa Moto Mapper</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:520px;margin:0 auto;padding-bottom:24px}
.hdr{padding:14px 16px;border-bottom:1px solid #222;display:flex;align-items:center;justify-content:space-between}
.hdr h1{font-size:17px;font-weight:700;letter-spacing:.4px}
#state-badge{font-size:12px;font-weight:700;padding:4px 10px;border-radius:20px;background:#ffffff18}
.card{margin:10px 12px;padding:14px;background:#1a1a1a;border-radius:14px}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:#666;margin-bottom:10px}
.gps{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;text-align:center;padding:4px 0 10px}
.big{font-size:32px;font-weight:800;font-variant-numeric:tabular-nums;line-height:1.08;letter-spacing:.2px}
.big.ok{color:#1a9641}
.big.bad{color:#cc0000}
.sub{font-size:12px;color:#888;margin-top:5px}
.controls{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
.ctrl-btn{display:flex;align-items:center;justify-content:center;gap:8px;padding:11px;border-radius:10px;text-decoration:none;font-weight:700;font-size:14px;border:none;cursor:pointer;color:#fff;background:#1565c0}
.ctrl-btn:active{background:#0d47a1}
.ctrl-btn.stop{background:#b71c1c}
.ctrl-btn.stop:active{background:#8e1515}
.ctrl-btn.restart{background:#37474f}
.ctrl-btn.restart:active{background:#263238}
.ctrl-btn.rejoin{background:#e65100}
.ctrl-btn.rejoin:active{background:#bf360c}
.ctrl-btn:disabled{opacity:.45;cursor:default}
.rows{}
.row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid #252525;font-size:13px}
.row:last-child{border-bottom:none}
.rk{color:#777}
.rv{font-variant-numeric:tabular-nums;font-weight:500}
.track-item{font-size:11px;color:#666;padding:4px 0;border-bottom:1px solid #1f1f1f;font-variant-numeric:tabular-nums;display:flex;justify-content:space-between}
.track-item:last-child{border-bottom:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.upd{font-size:10px;color:#444;text-align:center;margin-top:8px;padding-bottom:4px}
</style>
</head>
<body>
<div class="hdr">
  <h1>&#x1F6F0; LoRa Moto Mapper</h1>
  <div style="display:flex;align-items:center;gap:12px">
    <a href="/config" style="color:#888;text-decoration:none;font-size:22px" title="Configure">&#x2699;&#xFE0F;</a>
    <span id="state-badge">&#x25CB; LOADING</span>
  </div>
</div>

<div class="card">
  <div class="card-title">GPS Fix</div>
  <div class="gps">
    <div class="big" id="lat">&#x2014;</div>
    <div class="big" id="lon">&#x2014;</div>
  </div>
  <div class="sub" id="alt-fix">&#x2014;</div>
  <div class="controls">
    <button class="ctrl-btn" id="btn-start" onclick="control('start')">Start</button>
    <button class="ctrl-btn stop" id="btn-stop" onclick="control('stop')">Stop</button>
    <button class="ctrl-btn restart" id="btn-restart" onclick="control('restart')">Restart</button>
    <button class="ctrl-btn rejoin" id="btn-rejoin" onclick="control('rejoin')">Force Rejoin</button>
  </div>
</div>

<div class="card">
  <div class="card-title">Session</div>
  <div class="rows">
    <div class="row"><span class="rk">DevAddr</span><span class="rv" id="dev-addr">&#x2014;</span></div>
    <div class="row"><span class="rk">FCnt</span><span class="rv" id="fcnt">&#x2014;</span></div>
    <div class="row"><span class="rk">Uplinks sent</span><span class="rv" id="uplinks">&#x2014;</span></div>
    <div class="row"><span class="rk">Last TX</span><span class="rv" id="last-tx">&#x2014;</span></div>
    <div class="row"><span class="rk">Last join</span><span class="rv" id="last-join">&#x2014;</span></div>
  </div>
</div>

<div class="card">
  <div class="card-title">
    Track &mdash; <span id="track-count">0</span> points stored
  </div>
  <div id="track-list"></div>
</div>

<div class="upd" id="upd">&#x2014;</div>

<script>
const SL={tracking:"#1a9641",joined:"#1a9641",joining:"#d17000",reconnecting:"#d17000",
  starting:"#d17000",join_failed:"#cc0000",error:"#cc0000",stopped:"#555",not_installed:"#555"};
const ST={tracking:"&#x25CF; TRACKING",joined:"&#x25CF; JOINED",
  joining:"&#x25CB; JOINING&hellip;",reconnecting:"&#x25CB; RECONNECTING&hellip;",
  starting:"&#x25CB; STARTING&hellip;",join_failed:"&#x2715; JOIN FAILED",
  error:"&#x2715; ERROR",stopped:"&#x25CB; STOPPED"};

function fmt(iso){
  if(!iso)return"&mdash;";
  try{const d=new Date(iso);return d.toLocaleTimeString();}catch{return iso;}
}

async function refresh(){
  try{
    const[sr,tr]=await Promise.all([fetch("/api/status"),fetch("/api/track")]);
    const s=await sr.json(), pts=await tr.json();

    const state=s.state||"unknown";
    const badge=document.getElementById("state-badge");
    badge.innerHTML=ST[state]||state.toUpperCase();
    badge.style.color=SL[state]||"#ccc";
    badge.style.background=(SL[state]||"#555")+"28";

    const fix=s.last_fix||{};
    if(fix.lat!=null){
      document.getElementById("lat").textContent=fix.lat.toFixed(6)+"\xb0 N";
      document.getElementById("lon").textContent=fix.lon.toFixed(6)+"\xb0 E";
      document.getElementById("alt-fix").textContent=
        "Altitude "+((fix.alt_m)||0).toFixed(1)+" m \u00b7 "+fmt(fix.time);
    }

    const running = ["tracking","joined","joining","reconnecting","starting"].includes(state);
    document.getElementById("btn-start").disabled = running;
    document.getElementById("btn-stop").disabled = !running;
    document.getElementById("btn-restart").disabled = state === "not_installed";
    document.getElementById("btn-rejoin").disabled = state === "not_installed";

    document.getElementById("dev-addr").textContent=s.dev_addr||"\u2014";
    document.getElementById("fcnt").textContent=s.fcnt??"\u2014";
    document.getElementById("uplinks").textContent=s.uplinks_sent??"\u2014";
    document.getElementById("last-tx").textContent=fmt(s.last_uplink);
    document.getElementById("last-join").textContent=fmt(s.last_join);

    const linkStatus=s.link_status||"unknown";
    const gpsColorClass=linkStatus==="in_range" ? "ok" : "bad";
    document.getElementById("lat").className="big "+gpsColorClass;
    document.getElementById("lon").className="big "+gpsColorClass;

    document.getElementById("track-count").textContent=pts.length;
    const list=document.getElementById("track-list");
    const recent=pts.slice().reverse().slice(0,20);
    list.innerHTML=recent.map(p=>
      `<div class="track-item">
        <span>${fmt(p.t)}</span>
        <span>${p.lat.toFixed(5)}\xb0, ${p.lon.toFixed(5)}\xb0</span>
        <span>${(p.alt||0).toFixed(0)} m</span>
        <span>FC=${p.fcnt}</span>
      </div>`
    ).join("");

    document.getElementById("upd").textContent=
      "Updated "+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("upd").textContent="Error: "+e.message;
  }
}

async function control(action){
  const id = action === "restart" ? "btn-restart" : action === "rejoin" ? "btn-rejoin" : action === "stop" ? "btn-stop" : "btn-start";
  const btn = document.getElementById(id);
  try{
    btn.disabled = true;
    const res = await fetch("/api/"+action, {method:"POST"});
    const data = await res.json();
    document.getElementById("upd").textContent = data.ok
      ? (action.charAt(0).toUpperCase()+action.slice(1)+" requested")
      : ("Error: "+(data.message||"failed"));
    await refresh();
  }catch(e){
    document.getElementById("upd").textContent="Error: "+e.message;
  }
}

refresh();
setInterval(refresh,2000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass  # suppress per-request access log

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/":
            body = _HTML.encode()
            self._respond(200, "text/html; charset=utf-8", body)

        elif path == "/config":
            body = _config_page().encode()
            self._respond(200, "text/html; charset=utf-8", body)

        elif path == "/api/config":
            body = json.dumps(_read_config()).encode()
            self._respond(200, "application/json", body)

        elif path == "/api/status":
            body = json.dumps(_read_status(), indent=2).encode()
            self._respond(200, "application/json", body)

        elif path == "/api/track":
            body = json.dumps(list(_track)).encode()
            self._respond(200, "application/json", body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/api/start":
            body = json.dumps(_service_action("start")).encode()
            self._respond(200, "application/json", body)
        elif path == "/api/stop":
            body = json.dumps(_service_action("stop")).encode()
            self._respond(200, "application/json", body)
        elif path == "/api/restart":
            body = json.dumps(_service_action("restart")).encode()
            self._respond(200, "application/json", body)
        elif path == "/api/rejoin":
          body = json.dumps(_rejoin_action()).encode()
          self._respond(200, "application/json", body)
        elif path == "/api/config":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length)
            try:
                incoming = json.loads(raw)
                restart = bool(incoming.pop("restart", False))
                _write_config(incoming)
                result: dict = {"ok": True, "message": "saved"}
                if restart:
                    result["restart"] = _service_action("restart")
                body = json.dumps(result).encode()
            except Exception as exc:
                body = json.dumps({"ok": False, "message": str(exc)}).encode()
            self._respond(200, "application/json", body)
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="LoRaWAN GPS Tracker web server")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="HTTP port (default %(default)s)")
    args = p.parse_args()

    # Start background poller
    threading.Thread(target=_poll_status_loop, daemon=True).start()

    server = HTTPServer(("0.0.0.0", args.port), _Handler)
    print(f"Web server listening on http://0.0.0.0:{args.port}", flush=True)
    print(f"  Hotspot URL : http://10.42.0.1:{args.port}", flush=True)
    print(f"  mDNS URL    : http://lorapi-1.local:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
