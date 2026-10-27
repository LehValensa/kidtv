#!/usr/bin/env python3
"""kidtv - limit Chrome video playback for one Linux user (MPRIS / playerctl).

Runs as a root systemd service, so the kid (a normal user) can neither stop it
nor edit its state. It talks to the kid's session D-Bus through `runuser`,
and pauses Chrome whenever playback is not allowed:
  - inside the quiet windows (sleep / nap), or
  - after the daily allowance has been used up.
Only seconds of *actual playback* are counted.

A small web page (default port 8731) lets a parent see the status, add time,
lift quiet times and change all settings from a phone.

Usage:
  kidtv run             daemon + web page (used by the systemd unit)
  kidtv status          used / left today and whether playback is allowed now
  sudo kidtv extend [MIN]   add MIN minutes to today's allowance (default 60;
                            negative removes time)
  sudo kidtv unlock [MIN]   ignore quiet windows for MIN minutes (default 60)
  sudo kidtv lock           cancel unlock
  sudo kidtv reset          set today's used time back to 0

Environment (set in the systemd unit):
  KIDTV_PORT   web port           (default 8731)
  KIDTV_BIND   web bind address   (default 0.0.0.0)
  KIDTV_USER   kid's account, only the first-run default; later set on the web page
  KIDTV_STATE / KIDTV_CONFIG   file locations (default /var/lib/kidtv/...)
"""
import copy
import fcntl
import json
import os
import pwd
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date, datetime
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ----------------------------------------------------------------- settings
STATE_FILE = Path(os.environ.get("KIDTV_STATE", "/var/lib/kidtv/state.json"))
CONFIG_FILE = Path(os.environ.get("KIDTV_CONFIG", str(STATE_FILE.with_name("config.json"))))
WEB_BIND = os.environ.get("KIDTV_BIND", "0.0.0.0")
WEB_PORT = int(os.environ.get("KIDTV_PORT", "8731"))

PLAYER_PREFIXES = ("chrome", "chromium")       # MPRIS names to control
POLL_SECONDS = 2
ESCALATE_WINDOW_SEC = 60                       # "N pauses within this many seconds"

# Defaults for everything that can be changed on the web page.
DEFAULTS = {
    "kid_user": os.environ.get("KIDTV_USER", "sofy"),
    "daily_allowance_min": 120,                # per day, resets at midnight
    "blocked_windows": ["21:00-07:00",         # sleep
                        "13:00-15:00"],        # nap
    "warn_at_min": [10, 2],                    # popup when this many minutes left
    "escalate": False,                         # close Chrome if kid keeps pressing play
    "escalate_after_pauses": 5,
    "chrome_process": "chrome",                # `pkill -x` name (Chromium: "chromium")
}
# --------------------------------------------------------------------------

# Values only the daemon knows; shown on the web page.
LIVE = {"pending": 0.0, "playing": False, "players": 0}


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ------------------------------------------------------------------- state
def _fresh(day):
    return {"date": day, "used": 0.0, "bonus_min": 0, "unlock_until": 0.0}


def load():
    """Today's state; a new day starts from scratch."""
    today = date.today().isoformat()
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("date") != today:
        data = {}
    st = _fresh(today)
    st.update(data)
    return st


def update(fn):
    """Locked read-modify-write, so the daemon, web page and CLI don't clash."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(str(STATE_FILE) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = load()
        fn(st)
        _atomic_write(STATE_FILE, json.dumps(st))


# ------------------------------------------------------------------ config
WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
PROCESS_RE = re.compile(r"^[A-Za-z0-9._+-]{1,15}$")


def parse_window(text):
    m = WINDOW_RE.match(str(text))
    if not m:
        raise ValueError(f"Bad time window '{text}' - use HH:MM-HH:MM")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError(f"Invalid time in '{text}'")
    s, e = dtime(h1, m1), dtime(h2, m2)
    if s == e:
        raise ValueError(f"Start and end are the same in '{text}'")
    return s, e


def fmt_window(s, e):
    return f"{s:%H:%M}-{e:%H:%M}"


def _as_list(value):
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[\n,;]+", value) if p.strip()]
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    raise ValueError("Expected a list")


def validate_config(data, check_user=True):
    """Return a clean config dict, or raise ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError("Config must be an object")
    cfg = copy.deepcopy(DEFAULTS)
    for key in DEFAULTS:
        if key in data:
            cfg[key] = data[key]

    user = str(cfg["kid_user"]).strip()
    if check_user:
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            raise ValueError(f"No such Linux user: '{user}'")
        if pw.pw_uid == 0:
            raise ValueError("The kid's account cannot be root")
    cfg["kid_user"] = user

    try:
        allowance = int(cfg["daily_allowance_min"])
    except (TypeError, ValueError):
        raise ValueError("Daily allowance must be a whole number of minutes")
    if not 0 <= allowance <= 1440:
        raise ValueError("Daily allowance must be between 0 and 1440 minutes")
    cfg["daily_allowance_min"] = allowance

    cfg["blocked_windows"] = [fmt_window(*parse_window(w))
                              for w in _as_list(cfg["blocked_windows"])]

    try:
        warns = {int(x) for x in _as_list(cfg["warn_at_min"])}
    except (TypeError, ValueError):
        raise ValueError("Warning times must be whole numbers of minutes")
    cfg["warn_at_min"] = sorted((w for w in warns if w > 0), reverse=True)

    cfg["escalate"] = bool(cfg["escalate"])
    try:
        pauses = int(cfg["escalate_after_pauses"])
    except (TypeError, ValueError):
        raise ValueError("'Close Chrome after' must be a whole number")
    if not 2 <= pauses <= 50:
        raise ValueError("'Close Chrome after' must be between 2 and 50 pauses")
    cfg["escalate_after_pauses"] = pauses

    proc = str(cfg["chrome_process"]).strip()
    if not PROCESS_RE.match(proc):
        raise ValueError("Chrome process name: up to 15 letters/digits/._+-")
    cfg["chrome_process"] = proc
    return cfg


_last_cfg_error = [None]


def load_config():
    """Current config. A broken file falls back to defaults instead of crashing."""
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    try:
        cfg, err = validate_config(data, check_user=False), None
    except ValueError as exc:
        cfg, err = copy.deepcopy(DEFAULTS), str(exc)
    if err != _last_cfg_error[0]:
        _last_cfg_error[0] = err
        if err:
            log(f"config file problem, using defaults: {err}")
    return cfg


def save_config(changes):
    merged = load_config()
    merged.update({k: v for k, v in changes.items() if k in DEFAULTS})
    cfg = validate_config(merged, check_user=True)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(str(CONFIG_FILE) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2))
    log(f"settings saved: {cfg}")
    return cfg


# ------------------------------------------------------------------- rules
def in_window(t, start, end):
    if start <= end:
        return start <= t < end
    return t >= start or t < end    # window crosses midnight


def allowance_sec(st, cfg):
    return max(0, cfg["daily_allowance_min"] + st["bonus_min"]) * 60


def block_reason(st, pending, now, cfg):
    """None if playback is allowed right now, else a short reason."""
    if time.time() >= st["unlock_until"]:
        for w in cfg["blocked_windows"]:
            s, e = parse_window(w)
            if in_window(now.time(), s, e):
                return f"quiet time {fmt_window(s, e)}"
    if st["used"] + pending >= allowance_sec(st, cfg):
        return "daily allowance used up"
    return None


def fmt(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


def status_dict():
    cfg, st, now = load_config(), load(), datetime.now()
    used = st["used"] + LIVE["pending"]
    allowance = allowance_sec(st, cfg)
    return {
        "time": now.strftime("%H:%M"),
        "used_sec": int(used),
        "allowance_sec": allowance,
        "left_sec": int(max(0, allowance - used)),
        "bonus_min": st["bonus_min"],
        "unlock_left_sec": int(max(0, st["unlock_until"] - time.time())),
        "blocked": block_reason(st, LIVE["pending"], now, cfg),
        "playing": LIVE["playing"],
        "players": LIVE["players"],
    }


# ----------------------------------------------------------------- actions
def do_extend(minutes):
    update(lambda s: s.__setitem__("bonus_min", s["bonus_min"] + minutes))
    log(f"allowance changed by {minutes:+d} min")


def do_unlock(minutes):
    update(lambda s: s.__setitem__("unlock_until", time.time() + minutes * 60))
    log(f"quiet windows lifted for {minutes} min")


def do_lock():
    update(lambda s: s.__setitem__("unlock_until", 0.0))
    log("quiet windows active again")


def do_reset():
    update(lambda s: s.__setitem__("used", 0.0))
    LIVE["pending"] = 0.0
    log("used time reset to 0")


# ----------------------------------------------------------------- players
def run_as_kid(user, args, timeout=5):
    """Run a command as the kid, with access to the kid's session D-Bus."""
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return None
    rt = f"/run/user/{uid}"
    if not os.path.exists(f"{rt}/bus"):
        return None                  # kid not logged in
    cmd = ["runuser", "-u", user, "--", "env",
           f"XDG_RUNTIME_DIR={rt}",
           f"DBUS_SESSION_BUS_ADDRESS=unix:path={rt}/bus", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def chrome_players(user):
    r = run_as_kid(user, ["playerctl", "-l"])
    if not r or r.returncode != 0:
        return []
    names = (ln.strip() for ln in r.stdout.splitlines())
    return [n for n in names if n and n.lower().startswith(PLAYER_PREFIXES)]


def player_status(user, name):
    r = run_as_kid(user, ["playerctl", "-p", name, "status"])
    return r.stdout.strip() if r and r.returncode == 0 else ""


def pause(user, name):
    run_as_kid(user, ["playerctl", "-p", name, "pause"])


def notify(user, msg):
    # "normal" urgency on purpose: desktops (GNOME especially) keep "critical"
    # popups on screen until clicked. "transient" keeps them out of the history.
    run_as_kid(user, ["notify-send", "-u", "normal", "-t", "10000",
                      "-h", "int:transient:1", "-a", "Kid TV", "TV", msg])


# ------------------------------------------------------------------ daemon
def cmd_run():
    cfg = load_config()
    try:
        pwd.getpwnam(cfg["kid_user"])
    except KeyError:
        log(f"WARNING: user '{cfg['kid_user']}' does not exist - fix it on the web page")
    log(f"kidtv started: user '{cfg['kid_user']}', {cfg['daily_allowance_min']} min/day, "
        f"quiet {cfg['blocked_windows']}")
    start_web()

    last = time.monotonic()
    pauses = deque()
    warned = set()
    last_notice = 0.0

    def flush():
        p, LIVE["pending"] = LIVE["pending"], 0.0
        if p > 0:
            update(lambda s: s.__setitem__("used", s["used"] + p))

    while True:
        time.sleep(POLL_SECONDS)
        try:
            cfg = load_config()
            user = cfg["kid_user"]
            now_m = time.monotonic()
            dt = min(now_m - last, 5.0)
            last = now_m
            st = load()
            reason = block_reason(st, LIVE["pending"], datetime.now(), cfg)
            players = chrome_players(user)
            playing = [p for p in players if player_status(user, p) == "Playing"]
            LIVE["players"] = len(players)
            LIVE["playing"] = bool(playing)

            if playing and reason:
                for p in playing:
                    pause(user, p)
                log(f"paused {playing}: {reason}")
                pauses.append(now_m)
                while pauses and now_m - pauses[0] > ESCALATE_WINDOW_SEC:
                    pauses.popleft()
                if now_m - last_notice > 30:
                    notify(user, f"TV time is over ({reason}).")
                    last_notice = now_m
                if cfg["escalate"] and len(pauses) >= cfg["escalate_after_pauses"]:
                    log("kid keeps resuming playback - closing Chrome")
                    subprocess.run(["pkill", "-u", user, "-x", cfg["chrome_process"]])
                    pauses.clear()
            elif playing:
                LIVE["pending"] += dt
                left = allowance_sec(st, cfg) - st["used"] - LIVE["pending"]
                warned = {w for w in warned if left <= w * 60}   # re-arm after extend
                for w in cfg["warn_at_min"]:
                    if left <= w * 60 and w not in warned:
                        warned.add(w)
                        notify(user, f"About {max(1, round(left / 60))} min of TV left.")

            if LIVE["pending"] >= 10 or (LIVE["pending"] > 0 and not playing):
                flush()
        except Exception as exc:     # keep the daemon alive no matter what
            log(f"error: {exc!r}")


# --------------------------------------------------------------------- web
def _minutes(data, lo, hi):
    try:
        m = int(data.get("minutes"))
    except (TypeError, ValueError):
        raise ValueError("Minutes must be a whole number")
    if not lo <= m <= hi:
        raise ValueError(f"Minutes must be between {lo} and {hi}")
    return m


class Handler(BaseHTTPRequestHandler):
    server_version = "kidtv"

    def log_message(self, fmt_, *args):      # keep the journal quiet
        pass

    def _reply(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._reply(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._reply(200, status_dict())
        elif path == "/api/config":
            self._reply(200, load_config())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValueError("Content-Type must be application/json")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 10_000:
                raise ValueError("Request too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            if path == "/api/extend":
                do_extend(_minutes(data, -1440, 1440))
            elif path == "/api/unlock":
                do_unlock(_minutes(data, 1, 1440))
            elif path == "/api/lock":
                do_lock()
            elif path == "/api/reset":
                do_reset()
            elif path == "/api/config":
                save_config(data)
            else:
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, {"ok": True, "status": status_dict(), "config": load_config()})
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
        except Exception as exc:
            log(f"web error: {exc!r}")
            self._reply(500, {"error": "internal error"})


def start_web():
    try:
        srv = ThreadingHTTPServer((WEB_BIND, WEB_PORT), Handler)
    except OSError as exc:
        log(f"web interface NOT started on {WEB_BIND}:{WEB_PORT}: {exc}")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"web interface on http://{WEB_BIND}:{WEB_PORT}/")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kid TV</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1c1e21;--mut:#6b7280;--acc:#2563eb;--ok:#16a34a;--bad:#dc2626;--bd:#e5e7eb}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#1a1d24;--fg:#e8eaed;--mut:#9aa0a6;--acc:#60a5fa;--ok:#4ade80;--bad:#f87171;--bd:#2a2e37}}
*{box-sizing:border-box}
body{margin:0 auto;max-width:520px;padding:12px;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif}
h1{font-size:1.25rem;margin:8px 4px 12px}
h2{font-size:1rem;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px;margin-bottom:12px}
.big{font-size:2.6rem;font-weight:700;line-height:1.1}
.mut{color:var(--mut);font-size:.9rem}
.badge{display:inline-block;padding:4px 10px;border-radius:99px;font-size:.9rem;font-weight:600;margin-bottom:8px;border:1px solid currentColor}
.badge.ok{color:var(--ok)}.badge.bad{color:var(--bad)}
.bar{height:8px;background:var(--bd);border-radius:99px;margin:10px 0 8px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--acc);transition:width .4s}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.row:first-of-type{margin-top:0}
button,input,textarea{font:inherit;color:var(--fg)}
button{flex:1 1 28%;padding:12px 8px;border-radius:10px;border:1px solid var(--bd);background:var(--card);cursor:pointer}
button:active{opacity:.6}
button.p{background:var(--acc);border-color:var(--acc);color:#fff}
button.d{color:var(--bad)}
input[type=text],input[type=number],textarea{width:100%;padding:10px;border-radius:10px;border:1px solid var(--bd);background:var(--bg)}
input.n{flex:1 1 28%;width:auto}
label{display:block;margin:12px 0 4px;font-size:.9rem;font-weight:600}
label.c{display:flex;gap:8px;align-items:center;font-weight:400}
.hint{color:var(--mut);font-size:.8rem;margin-top:3px}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--ok);color:#fff;padding:10px 18px;border-radius:99px;opacity:0;pointer-events:none;transition:opacity .25s;max-width:90%;text-align:center}
#toast.show{opacity:1}#toast.bad{background:var(--bad)}
</style></head><body>
<h1>📺 Kid TV</h1>

<div class="card">
  <div id="state" class="badge ok">…</div>
  <div class="big" id="left">–</div>
  <div class="mut">left today · used <span id="used">–</span> of <span id="allow">–</span></div>
  <div class="bar"><i id="barfill"></i></div>
  <div class="mut" id="extra"></div>
</div>

<div class="card">
  <h2>Add time today</h2>
  <div class="row">
    <button class="p" onclick="act('/api/extend',{minutes:15},'+15 min')">+15 min</button>
    <button class="p" onclick="act('/api/extend',{minutes:30},'+30 min')">+30 min</button>
    <button class="p" onclick="act('/api/extend',{minutes:60},'+60 min')">+60 min</button>
  </div>
  <div class="row">
    <input class="n" id="cm" type="number" inputmode="numeric" value="10" min="1" max="1440">
    <button onclick="custom(1)">Add</button>
    <button class="d" onclick="custom(-1)">Remove</button>
  </div>
</div>

<div class="card">
  <h2>Sleep / nap times</h2>
  <div class="mut">Allow TV during the quiet windows for a while:</div>
  <div class="row">
    <button onclick="act('/api/unlock',{minutes:30},'Quiet times off for 30 min')">30 min</button>
    <button onclick="act('/api/unlock',{minutes:60},'Quiet times off for 60 min')">60 min</button>
    <button onclick="act('/api/unlock',{minutes:120},'Quiet times off for 2 h')">2 h</button>
  </div>
  <div class="row">
    <button onclick="act('/api/lock',{},'Quiet times are on again')">Quiet times on again</button>
    <button class="d" onclick="if(confirm('Reset used time to 0?'))act('/api/reset',{},'Used time reset')">Reset used time</button>
  </div>
</div>

<div class="card">
  <h2>Settings</h2>
  <form id="cfg">
    <label for="kid_user">Kid's Linux account</label>
    <input type="text" id="kid_user" name="kid_user" autocapitalize="none" autocomplete="off">

    <label for="daily_allowance_min">Daily allowance (minutes)</label>
    <input type="number" id="daily_allowance_min" name="daily_allowance_min" inputmode="numeric" min="0" max="1440">

    <label for="blocked_windows">Quiet windows (no TV)</label>
    <textarea id="blocked_windows" name="blocked_windows" rows="3"></textarea>
    <div class="hint">One per line as HH:MM-HH:MM. A window may cross midnight, e.g. 21:00-07:00.</div>

    <label for="warn_at_min">Warn when this many minutes are left</label>
    <input type="text" id="warn_at_min" name="warn_at_min" autocomplete="off">
    <div class="hint">Comma separated, e.g. 10, 2. Leave empty for no warnings.</div>

    <label class="c"><input type="checkbox" id="escalate" name="escalate"> Close Chrome if the kid keeps pressing play</label>
    <label for="escalate_after_pauses">...after this many pauses within a minute</label>
    <input type="number" id="escalate_after_pauses" name="escalate_after_pauses" inputmode="numeric" min="2" max="50">

    <label for="chrome_process">Chrome process name</label>
    <input type="text" id="chrome_process" name="chrome_process" autocapitalize="none" autocomplete="off">
    <div class="hint">"chrome" for Google Chrome, "chromium" for Chromium.</div>

    <div class="row"><button class="p" type="submit">Save settings</button></div>
  </form>
</div>
<div id="toast"></div>

<script>
const $=s=>document.querySelector(s);
const fmt=s=>{s=Math.max(0,Math.round(s));return Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m'};
let tt;
function toast(msg,bad){const t=$('#toast');t.textContent=msg;t.className='show'+(bad?' bad':'');clearTimeout(tt);tt=setTimeout(()=>{t.className=''},2500)}
async function call(path,body){
  const opt=body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
  const r=await fetch(path,opt);
  let j={};try{j=await r.json()}catch(e){}
  if(!r.ok)throw new Error(j.error||('HTTP '+r.status));
  return j;
}
function render(s){
  $('#left').textContent=fmt(s.left_sec);
  $('#used').textContent=fmt(s.used_sec);
  $('#allow').textContent=fmt(s.allowance_sec);
  $('#barfill').style.width=(s.allowance_sec?Math.min(100,100*s.used_sec/s.allowance_sec):100)+'%';
  const b=$('#state');
  if(s.blocked){b.textContent='⛔ Blocked: '+s.blocked;b.className='badge bad'}
  else{b.textContent='✅ TV allowed';b.className='badge ok'}
  const x=[s.playing?'▶ playing now':'⏸ not playing','Chrome players: '+s.players];
  if(s.unlock_left_sec>0)x.push('quiet times off for '+fmt(s.unlock_left_sec));
  if(s.bonus_min)x.push('bonus today: '+(s.bonus_min>0?'+':'')+s.bonus_min+' min');
  $('#extra').textContent=x.join(' · ');
}
async function refresh(){
  try{render(await call('/api/status'))}
  catch(e){const b=$('#state');b.textContent='⚠ no connection';b.className='badge bad'}
}
function fillCfg(c){
  const f=$('#cfg');
  f.kid_user.value=c.kid_user;
  f.daily_allowance_min.value=c.daily_allowance_min;
  f.blocked_windows.value=c.blocked_windows.join('\n');
  f.warn_at_min.value=c.warn_at_min.join(', ');
  f.escalate.checked=c.escalate;
  f.escalate_after_pauses.value=c.escalate_after_pauses;
  f.chrome_process.value=c.chrome_process;
}
async function act(path,body,msg){
  try{
    const j=await call(path,body||{});
    render(j.status);
    if(path==='/api/config')fillCfg(j.config);
    toast(msg||'Done');
  }catch(e){toast(e.message,true)}
}
function custom(sign){
  const m=parseInt($('#cm').value,10);
  if(!m||m<1){toast('Enter minutes',true);return}
  act('/api/extend',{minutes:sign*m},(sign>0?'Added ':'Removed ')+m+' min');
}
$('#cfg').addEventListener('submit',e=>{
  e.preventDefault();
  const f=e.target;
  act('/api/config',{
    kid_user:f.kid_user.value.trim(),
    daily_allowance_min:Number(f.daily_allowance_min.value),
    blocked_windows:f.blocked_windows.value,
    warn_at_min:f.warn_at_min.value,
    escalate:f.escalate.checked,
    escalate_after_pauses:Number(f.escalate_after_pauses.value),
    chrome_process:f.chrome_process.value.trim()
  },'Settings saved');
});
(async()=>{
  try{fillCfg(await call('/api/config'))}catch(e){toast(e.message,true)}
  refresh();setInterval(refresh,5000);
})();
</script></body></html>
"""


# --------------------------------------------------------------------- CLI
def cmd_status():
    s, cfg = status_dict(), load_config()
    print(f"Used today : {fmt(s['used_sec'])}")
    print(f"Allowance  : {fmt(s['allowance_sec'])} "
          f"({cfg['daily_allowance_min']} + {s['bonus_min']} bonus min)")
    print(f"Left       : {fmt(s['left_sec'])}")
    if s["unlock_left_sec"] > 0:
        print(f"Quiet windows lifted for another {fmt(s['unlock_left_sec'])}")
    print(f"Right now  : {'BLOCKED - ' + s['blocked'] if s['blocked'] else 'playback allowed'}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        arg = int(argv[2]) if len(argv) > 2 else 60
    except ValueError:
        sys.exit("Minutes must be a whole number.")
    try:
        if cmd == "run":
            cmd_run()
        elif cmd == "status":
            cmd_status()
        elif cmd == "extend":
            do_extend(arg)
            cmd_status()
        elif cmd == "unlock":
            do_unlock(arg)
        elif cmd == "lock":
            do_lock()
        elif cmd == "reset":
            do_reset()
        else:
            sys.exit(__doc__)
    except PermissionError:
        sys.exit("Permission denied - run this command with sudo.")


if __name__ == "__main__":
    main(sys.argv)
