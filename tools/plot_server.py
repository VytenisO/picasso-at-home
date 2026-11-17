#!/usr/bin/env python3
"""Small Tailscale-friendly web server for the WaterColorBot pen plotter.

Serves a single-page UI for uploading SVG (or raster images, converted to
pen paths), previewing, dry-running, tracing and plotting, by calling the
existing tools/plot_svg.py. Binds to the Tailscale IP by default. A shared
password guards every API call; the physical plot/trace actions require an
extra typed confirmation and a clean preflight dry run.

Run directly:
    /usr/bin/python3 tools/plot_server.py --host 100.75.242.85 --port 8080
Or install the systemd user unit in config/plotter-web.service.

Only stdlib + Pillow (for raster conversion) are needed in this process;
pyserial is needed by the plot_svg.py subprocess for real hardware.
"""
import argparse
import base64
import datetime
import hashlib
import hmac
import http.cookies
import json
import os
import pathlib
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from image_to_svg import convert as convert_raster  # noqa: E402

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
INPUT = ROOT / "input"
RESULTS = ROOT / "results"
CONFIG = ROOT / "config" / "web.json"
INDEX = TOOLS / "web" / "index.html"
MANIFEST = INPUT / "manifest.json"
PY = "/usr/bin/python3"

MAX_UPLOAD = 12 * 1024 * 1024
SESSION_AGE = 7 * 24 * 3600
ALLOWED_IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
ALLOWED = ALLOWED_IMG | {".svg"}

FS_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
JOB = {"running": False, "mode": None, "file": None, "output": "", "returncode": None,
       "started": None, "finished": None, "id": None, "status": None,
       "paused": False, "pause": None, "pos": None, "trail": []}
ACTIVE_PROC = None   # the running plot subprocess, kept so a pen swap can be resumed
ESTOP = {"active": False}
LOG_LOCK = threading.Lock()
ATTEMPTS = {}   # ip -> [fail_count, lock_until]


# ------------------------------------------------------------------- config
def load_config():
    """Load (or create) config/web.json. Returns the config dict + password."""
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
        except Exception:  # noqa: BLE001
            cfg = {}
    changed = False
    for k in ("salt", "secret"):
        if not cfg.get(k):
            cfg[k] = secrets.token_hex(24)
            changed = True
    # Auth can be switched off with PLOTTER_WEB_AUTH=off (for a trusted tailnet).
    auth_env = os.environ.get("PLOTTER_WEB_AUTH", "").strip().lower()
    if auth_env in ("off", "0", "false", "no", "disabled"):
        cfg["auth"] = False
        changed = True
    elif auth_env in ("on", "1", "true", "yes", "enabled"):
        cfg["auth"] = True
        changed = True
    # PBKDF2 hash of the active password. Env var wins; otherwise keep stored hash.
    env_pw = os.environ.get("PLOTTER_WEB_PASSWORD")
    if env_pw:
        cfg["password_hash"] = pbkdf2(env_pw, cfg["salt"])
        changed = True
        password = env_pw
        sourced = "PLOTTER_WEB_PASSWORD"
    elif cfg.get("password_hash"):
        password = None
        sourced = "stored hash"
    else:
        password = secrets.token_urlsafe(12)
        cfg["password_hash"] = pbkdf2(password, cfg["salt"])
        changed = True
        sourced = "generated (first run)"
    cfg.setdefault("port", 8080)
    if changed or not CONFIG.exists():
        CONFIG.parent.mkdir(exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
        CONFIG.chmod(0o600)
    return cfg, password, sourced


def pbkdf2(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200000).hex()


def verify_password(cfg, password):
    return hmac.compare_digest(pbkdf2(password, cfg["salt"]), cfg["password_hash"])


# ------------------------------------------------------------------ session
def make_session(secret):
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def check_session(value, secret):
    try:
        ts, sig = value.split(".", 1)
    except (ValueError, AttributeError):
        return False
    expect = hmac.new(secret.encode(), ts.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False
    try:
        return 0 <= time.time() - int(ts) <= SESSION_AGE
    except ValueError:
        return False


# ------------------------------------------------------------------ manifest
def load_manifest():
    try:
        data = json.loads(MANIFEST.read_text())
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def save_manifest(entries):
    MANIFEST.write_text(json.dumps(entries, indent=2) + "\n")


def safe_name(name):
    name = pathlib.Path(name or "").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "upload"
    return name[:100]


def paper_config():
    """Bed size and paper rectangle for the placement diagram, from machine.json."""
    try:
        cfg = json.loads((ROOT / "config" / "machine.json").read_text())
        p = cfg.get("paper") or {}
        bed = cfg.get("travel_mm") or {}
        x0, y0 = float(p.get("x0", 0)), float(p.get("y0", 0))
        w = float(p.get("width", bed.get("x", 368)))
        h = float(p.get("height", bed.get("y", 227.5)))
        m = max(0.0, float(p.get("margin_mm", 0.0)))
        return {"x0": x0, "y0": y0, "width": w, "height": h, "margin": m,
                "canvas": [x0 + m, y0 + m, x0 + w - m, y0 + h - m],
                "bed_x": float(bed.get("x", 368)), "bed_y": float(bed.get("y", 227.5))}
    except Exception:  # noqa: BLE001
        return {"x0": 55.0, "y0": 0.0, "width": 297.0, "height": 210.0, "margin": 15.0,
                "canvas": [70.0, 15.0, 337.0, 195.0], "bed_x": 368.0, "bed_y": 227.5}


def public_entry(e):
    return {k: e.get(k) for k in ("id", "name", "kind", "bytes", "uploaded", "svg", "orig", "conversion",
                                  "warning", "style", "mask", "layers")}


# --------------------------------------------------------------------- jobs
def arg_number(opts, key, lo, hi, cast=float):
    if key not in opts or opts[key] in (None, ""):
        return None
    v = cast(opts[key])
    if not lo <= v <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return v


def build_argv(entry, opts, mode):
    """Translate UI options into plot_svg.py CLI arguments."""
    argv = [PY, str(TOOLS / "plot_svg.py"), str(INPUT / entry["svg"]), "--mode", mode]
    flags = [
        ("width", "--width", 5.0, 1000.0, float),
        ("x0", "--x0", -500.0, 500.0, float),
        ("y0", "--y0", -500.0, 500.0, float),
        ("start_x", "--start-x", 0.0, 500.0, float),
        ("start_y", "--start-y", 0.0, 500.0, float),
        ("speed", "--speed", 1.0, 40.0, float),
        ("travel_speed", "--travel-speed", 1.0, 40.0, float),
        ("paper_x0", "--paper-x0", 0.0, 1000.0, float),
        ("paper_y0", "--paper-y0", 0.0, 1000.0, float),
        ("paper_width", "--paper-width", 1.0, 1000.0, float),
        ("paper_height", "--paper-height", 1.0, 1000.0, float),
    ]
    for key, flag, lo, hi, cast in flags:
        v = arg_number(opts, key, lo, hi, cast)
        if v is not None:
            argv += [flag, str(v)]
    anchor = opts.get("anchor") or "top-left"
    if anchor not in ("top-left", "top-center", "center"):
        raise ValueError("anchor must be top-left, top-center or center")
    argv += ["--anchor", anchor]
    if mode != "dry":
        argv.append("--track")
    return argv


def run_capture(argv, timeout=240):
    try:
        p = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except Exception as ex:  # noqa: BLE001
        return -1, f"could not start subprocess: {ex!r}"


def job_worker(argv, word):
    global ACTIVE_PROC
    logfile = None
    try:
        proc = subprocess.Popen(argv, cwd=str(ROOT), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        with JOB_LOCK:
            ACTIVE_PROC = proc
        logfile = (RESULTS / f"web_job_{JOB['id']}.log").open("w")
        proc.stdin.write(word + "\n")
        proc.stdin.flush()
        # Keep stdin open: plot_svg.py reads CONTINUE at each pen change.
        for line in proc.stdout:
            if line.startswith("__POS__:"):
                try:
                    pos = json.loads(line[len("__POS__:"):])
                except Exception:  # noqa: BLE001
                    continue
                with LOG_LOCK:
                    JOB["pos"] = pos
                    JOB["trail"].append([pos["x"], pos["y"]])
                    JOB["trail"] = JOB["trail"][-500:]
                logfile.write(line)
            elif line.startswith("__PAUSE__:"):
                try:
                    info = json.loads(line[len("__PAUSE__:"):])
                except Exception:  # noqa: BLE001
                    info = {}
                with LOG_LOCK:
                    JOB["paused"] = True
                    JOB["pause"] = info
                    JOB["status"] = "paused"
                logfile.write(f"Pen change requested: {info.get('label', '?')}\n")
            else:
                with LOG_LOCK:
                    JOB["output"] = (JOB["output"] + line)[-40000:]
                logfile.write(line)
            logfile.flush()
        rc = proc.wait()
        with LOG_LOCK:
            JOB["returncode"] = rc
            JOB["status"] = "complete" if rc == 0 else "error"
    except Exception as ex:  # noqa: BLE001
        with LOG_LOCK:
            JOB["output"] += f"\nserver error: {ex!r}\n"
            JOB["returncode"] = -1
            JOB["status"] = "error"
    finally:
        if logfile:
            logfile.close()
        with JOB_LOCK:
            ACTIVE_PROC = None
        with LOG_LOCK:
            JOB["running"] = False
            JOB["paused"] = False
            JOB["pause"] = None
            if ESTOP["active"]:
                JOB["status"] = "estop"
            JOB["finished"] = datetime.datetime.now().isoformat(timespec="seconds")


def serial_precheck():
    """Read-only EBB check (V query, no motion) with an actionable error message."""
    if serial is None:
        return False, "pyserial is not installed for /usr/bin/python3"
    try:
        port = next((p.device for p in list_ports.comports() if (p.vid, p.pid) == (0x04D8, 0xFD92)), None)
    except Exception as ex:  # noqa: BLE001
        return False, f"could not enumerate serial ports: {ex}"
    if not port:
        return False, "no EBB (04d8:fd92) found - plug in and power the plotter"
    try:
        with serial.Serial(port, 9600, timeout=2) as s:
            s.reset_input_buffer()
            s.write(b"V\r")
            ver = s.readline().decode(errors="replace").strip()
        return True, f"{port} ({ver})"
    except Exception as ex:  # noqa: BLE001
        return False, (f"cannot open {port}: {ex}. Ensure this user is in dialout "
                       f"(sudo usermod -aG dialout $(whoami)); if already added, log out/in "
                       f"or reboot so the user manager refreshes its groups.")


def hard_stop():
    """Abort the running job, then best-effort raise the pen and release motors.

    Firmware 2.0.1 has no ES/QM/QS, so an in-flight move cannot be stopped in
    software; cutting power is the only instant stop. This is the safest
    software equivalent: stop sending commands, park the pen, free the motors.
    """
    notes = []
    proc = ACTIVE_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            notes.append("running job interrupted")
        except Exception as ex:  # noqa: BLE001
            notes.append(f"could not interrupt the job: {ex}")
    if serial is not None:
        try:
            port = next((p.device for p in list_ports.comports() if (p.vid, p.pid) == (0x04D8, 0xFD92)), None)
            if not port:
                notes.append("no EBB found for a direct pen-up/motors-off")
            else:
                with serial.Serial(port, 9600, timeout=2) as s:
                    s.reset_input_buffer()
                    s.write(b"SP,1,300\r")
                    time.sleep(0.4)
                    s.readline()
                    s.write(b"EM,0,0\r")
                    time.sleep(0.1)
                    s.readline()
                notes.append(f"{port}: pen raised, motors released")
        except Exception as ex:  # noqa: BLE001
            notes.append(f"could not reach the EBB: {ex}")
    else:
        notes.append("pyserial unavailable; could not raise the pen directly")
    return notes


def start_job(argv, mode, entry, word):
    with JOB_LOCK:
        if JOB["running"]:
            return False, "a plot job is already running"
        with LOG_LOCK:
            JOB.update(running=True, mode=mode, file=entry["id"], output="",
                       returncode=None, started=datetime.datetime.now().isoformat(timespec="seconds"),
                       finished=None, id=f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}",
                       status="running", paused=False, pause=None, pos=None, trail=[])
            ESTOP["active"] = False
        threading.Thread(target=job_worker, args=(argv, word), daemon=True).start()
        return True, JOB["id"]


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    server_version = "PlotterWeb/1.0"

    def log_message(self, fmt, *args):
        print(f"{datetime.datetime.now():%H:%M:%S} {self.address_string()} {fmt % args}", flush=True)

    # ---- response helpers
    def send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def err(self, code, msg):
        self.send(code, {"ok": False, "error": str(msg)})

    def body_bytes(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return b""
        if n <= 0 or n > MAX_UPLOAD + 4096:
            return b""
        return self.rfile.read(n)

    def body_json(self):
        try:
            return json.loads(self.body_bytes().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def authed(self):
        if not self.server.cfg.get("auth", True):
            return True
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie["plotter_session"].value
        except Exception:  # noqa: BLE001
            return False
        return check_session(value, self.server.cfg["secret"])

    def require_auth(self):
        if not self.authed():
            self.err(401, "not logged in")
            return False
        return True

    # ---- routes
    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(path.query)
        if path.path == "/":
            try:
                html = INDEX.read_text(encoding="utf-8")
            except OSError:
                html = "<h1>UI missing</h1><p>tools/web/index.html not found.</p>"
            return self.send(200, html, "text/html; charset=utf-8")
        if path.path == "/api/me":
            return self.send(200, {"auth": self.authed(), "server": self.server.bind,
                                   "paper": paper_config()})
        if path.path == "/api/files":
            if not self.require_auth():
                return
            with FS_LOCK:
                return self.send(200, {"files": [public_entry(e) for e in load_manifest()]})
        if path.path == "/api/job":
            if not self.require_auth():
                return
            with LOG_LOCK:
                return self.send(200, dict(JOB))
        if path.path == "/api/preview":
            if not self.require_auth():
                return
            return self.serve_file(q.get("name", [""])[0], inline=True)
        if path.path == "/api/mask":
            if not self.require_auth():
                return
            return self.serve_mask(q.get("name", [""])[0])
        if path.path == "/api/download":
            if not self.require_auth():
                return
            return self.serve_file(q.get("name", [""])[0], inline=False)
        return self.err(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            if not self.server.cfg.get("auth", True):
                return self.send(200, {"ok": True})
            data = self.body_json()
            pw = data.get("password", "")
            ip = self.client_address[0]
            now = time.time()
            rec = ATTEMPTS.get(ip, [0, 0])
            if now < rec[1]:
                return self.err(429, "too many attempts, wait a minute")
            if not isinstance(pw, str) or not verify_password(self.server.cfg, pw):
                rec[0] += 1
                if rec[0] >= 5:
                    rec[1] = now + 60
                    rec[0] = 0
                ATTEMPTS[ip] = rec
                return self.err(401, "wrong password")
            ATTEMPTS.pop(ip, None)
            session = make_session(self.server.cfg["secret"])
            return self.send(200, {"ok": True}, headers={"Set-Cookie":
                             f"plotter_session={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_AGE}"})

        if path == "/api/logout":
            return self.send(200, {"ok": True}, headers={"Set-Cookie":
                             "plotter_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})

        # Safety first: the e-stop is reachable even if a session expired.
        if path == "/api/estop":
            return self.estop()

        if not self.require_auth():
            return

        if path == "/api/upload":
            return self.upload()
        if path == "/api/convert_preview":
            return self.convert_preview()
        if path == "/api/delete":
            return self.delete_file()
        if path == "/api/dry":
            return self.dry()
        if path in ("/api/plot", "/api/trace"):
            return self.start(mode="plot" if path.endswith("plot") else "trace")
        if path == "/api/continue":
            return self.continue_job()
        return self.err(404, "not found")

    # ---- file operations
    def find_entry(self, ident):
        with FS_LOCK:
            for e in load_manifest():
                if e["id"] == ident:
                    return e
        return None

    def serve_file(self, ident, inline):
        e = self.find_entry(ident)
        if not e:
            return self.err(404, "unknown file id")
        field = "svg" if inline else "orig"
        name = e.get(field)
        if not name or not (INPUT / name).is_file():
            return self.err(404, "file missing")
        data = (INPUT / name).read_bytes()
        ctype = "image/svg+xml" if field == "svg" else "application/octet-stream"
        headers = {"Content-Security-Policy": "default-src 'none'; sandbox"} if inline else {
            "Content-Disposition": f'attachment; filename="{safe_name(e.get("name") or name)}"'}
        self.send(200, data, ctype, headers)

    def serve_mask(self, ident):
        e = self.find_entry(ident)
        if not e:
            return self.err(404, "unknown file id")
        name = e.get("mask")
        if not name or not (INPUT / name).is_file():
            return self.err(404, "mask missing")
        data = (INPUT / name).read_bytes()
        self.send(200, data, "image/png", {"Content-Security-Policy": "default-src 'none'; sandbox"})

    def conversion_params(self):
        """Raster conversion settings sent as headers by the upload UI."""
        def num(h, default, cast=float):
            try:
                return cast(self.headers.get(h, default))
            except (TypeError, ValueError):
                return default
        style = self.headers.get("X-Style", "contour").strip().lower()
        if style not in ("contour", "halftone", "one-line"):
            style = "contour"
        canvas = paper_config()["canvas"]
        max_w, max_h = canvas[2] - canvas[0], canvas[3] - canvas[1]
        levels = max(0, min(6, num("X-Levels", 0, int)))
        if levels == 1:
            levels = 0
        return {"width_mm": max(10.0, min(max_w, num("X-Width-Mm", 120.0))),
                "threshold": max(0, min(255, num("X-Threshold", 150, int))),
                "blur": max(0.0, min(10.0, num("X-Blur", 1.0))),
                "style": style,
                "density": max(1, min(100, num("X-Density", 40, int))),
                "inks": max(1, min(4, num("X-Inks", 1, int))),
                "levels": levels, "max_width_mm": max_w, "max_height_mm": max_h}

    def convert_preview(self):
        """Convert an uploaded raster with the current settings, without saving it."""
        raw = self.body_bytes()
        safe = safe_name(urllib.parse.unquote(self.headers.get("X-Filename", "")))
        ext = pathlib.Path(safe).suffix.lower()
        if ext not in ALLOWED_IMG:
            return self.err(400, f"preview needs an image file, got {ext or '(none)'}")
        if not raw:
            return self.err(400, "empty upload")
        token = secrets.token_hex(6)
        src = INPUT / f"preview_{token}{ext}"
        dst = INPUT / f"preview_{token}.svg"
        mask = INPUT / f"preview_{token}_mask.png"
        try:
            src.write_bytes(raw)
            params = self.conversion_params()
            stats = convert_raster(src, dst, **params, mask=str(mask))
            svg = dst.read_text(encoding="utf-8")
            mask_b64 = base64.b64encode(mask.read_bytes()).decode() if mask.is_file() else None
            return self.send(200, {"ok": True, "svg": svg, "mask": mask_b64, "stats": stats})
        except Exception as ex:  # noqa: BLE001
            return self.err(400, f"preview failed: {ex}")
        finally:
            for p in (src, dst, mask):
                if p.is_file():
                    try:
                        p.unlink()
                    except OSError:
                        pass

    def upload(self):
        raw = self.body_bytes()
        name = urllib.parse.unquote(self.headers.get("X-Filename", ""))
        safe = safe_name(name)
        ext = pathlib.Path(safe).suffix.lower()
        if ext not in ALLOWED:
            return self.err(400, f"unsupported file type {ext or '(none)'}; allowed: {', '.join(sorted(ALLOWED))}")
        if not raw:
            return self.err(400, "empty upload")
        ident = f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
        try:
            if ext == ".svg":
                import xml.etree.ElementTree as ET
                ET.fromstring(raw)
                stored = f"{ident}_{safe}"
                (INPUT / stored).write_bytes(raw)
                entry = {"id": stored, "name": safe, "kind": "svg", "bytes": len(raw),
                         "uploaded": datetime.datetime.now().isoformat(timespec="seconds"),
                         "svg": stored, "orig": stored}
                try:
                    import plot_svg
                    _, metas, _ = plot_svg.load_svg(INPUT / stored, True)
                    entry["layers"] = [
                        {"label": plot_svg.color_label(k), "color": k[0] or "#888888", "strokes": len(ix)}
                        for k, ix in plot_svg.color_groups(metas)]
                except Exception:  # noqa: BLE001
                    pass  # swatches are cosmetic; the plotter still handles the layers
            else:
                orig = f"{ident}_{safe}"
                (INPUT / orig).write_bytes(raw)
                svg = f"{ident}_{pathlib.Path(safe).stem}.svg"
                mask = f"{pathlib.Path(svg).stem}_mask.png"
                params = self.conversion_params()
                stats = convert_raster(INPUT / orig, INPUT / svg, **params, mask=str(INPUT / mask))
                entry = {"id": svg, "name": safe, "kind": "raster", "bytes": len(raw),
                         "uploaded": datetime.datetime.now().isoformat(timespec="seconds"),
                         "svg": svg, "orig": orig, "conversion": stats,
                         "style": params["style"], "mask": stats.get("mask"),
                         "layers": stats.get("layers", [])}
            with FS_LOCK:
                entries = load_manifest()
                entries.insert(0, entry)
                save_manifest(entries)
            return self.send(200, {"ok": True, "file": public_entry(entry)})
        except Exception as ex:  # noqa: BLE001
            return self.err(400, f"upload/conversion failed: {ex}")

    def delete_file(self):
        ident = self.body_json().get("id", "")
        with FS_LOCK:
            entries = load_manifest()
            keep = []
            found = None
            for e in entries:
                if e["id"] == ident:
                    found = e
                else:
                    keep.append(e)
            if not found:
                return self.err(404, "unknown file id")
            save_manifest(keep)
        for field in ("svg", "orig", "mask"):
            p = INPUT / found.get(field, "")
            if p.is_file() and p.parent == INPUT:
                try:
                    p.unlink()
                except OSError:
                    pass
        return self.send(200, {"ok": True})

    # ---- plot operations
    def get_entry(self):
        data = self.body_json()
        e = self.find_entry(data.get("id", ""))
        if not e:
            self.err(404, "unknown file id")
            return None, None
        return e, data

    def dry(self):
        e, data = self.get_entry()
        if not e:
            return
        try:
            argv = build_argv(e, data.get("options", {}), "dry")
        except (ValueError, TypeError) as ex:
            return self.err(400, str(ex))
        rc, out = run_capture(argv)
        return self.send(200, {"ok": rc == 0, "returncode": rc, "output": out})

    def start(self, mode):
        e, data = self.get_entry()
        if not e:
            return
        opts = data.get("options", {})
        word = mode.upper()
        if data.get("confirm") != word:
            return self.err(400, f"type {word} to confirm")
        ready, msg = serial_precheck()
        if not ready:
            return self.err(409, "plotter not ready: " + msg)
        try:
            argv = build_argv(e, opts, mode)
            pre_argv = build_argv(e, opts, "dry")
        except (ValueError, TypeError) as ex:
            return self.err(400, str(ex))
        with LOG_LOCK:
            if JOB["running"]:
                return self.err(409, "a plot job is already running")
        rc, out = run_capture(pre_argv)
        if rc != 0 or "PROBLEM:" in out:
            return self.err(409, "preflight failed; fix the drawing/options before " + word +
                            "\n\n" + out[-4000:])
        ok, info = start_job(argv, mode, e, word)
        if not ok:
            return self.err(409, info)
        return self.send(200, {"ok": True, "job": info})

    def estop(self):
        """Abort the current job and leave the machine safe (pen up, motors free)."""
        with LOG_LOCK:
            was_running = JOB["running"]
            ESTOP["active"] = True
            JOB["status"] = "estop"
            JOB["paused"] = False
            JOB["pause"] = None
            JOB["output"] += ("\n*** E-STOP: job aborted, pen raised, motors released. "
                              "Carriage position is now unknown — place it by hand before any new run. "
                              "Firmware 2.0.1 cannot stop a move already sent; cut power for an instant stop. ***\n")
        notes = hard_stop()
        with LOG_LOCK:
            JOB["running"] = False
            JOB["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        return self.send(200, {"ok": True, "was_running": was_running, "notes": notes})

    def continue_job(self):
        """Resume a plot that is waiting for the operator to change pens."""
        global ACTIVE_PROC
        with JOB_LOCK:
            if not JOB["running"] or not JOB["paused"]:
                return self.err(409, "no job is waiting for a pen change")
            proc = ACTIVE_PROC
            if proc is None or proc.stdin is None or proc.poll() is not None:
                return self.err(409, "the job is no longer active")
            try:
                proc.stdin.write("CONTINUE\n")
                proc.stdin.flush()
            except Exception as ex:  # noqa: BLE001
                return self.err(500, f"could not resume the job: {ex}")
            with LOG_LOCK:
                JOB["paused"] = False
                JOB["pause"] = None
                JOB["status"] = "running"
        return self.send(200, {"ok": True})


def shutdown_cleanup(signum, frame):
    """On SIGTERM (e.g. systemctl restart), stop the job and leave the pen up."""
    try:
        proc = ACTIVE_PROC
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        hard_stop()
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(0)


def detect_host():
    try:
        out = subprocess.check_output(["tailscale", "ip", "-4"], text=True, timeout=5)
        return out.strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        return "0.0.0.0"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("PLOTTER_WEB_HOST"), help="bind address (default: tailscale IP)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PLOTTER_WEB_PORT", 0)) or None)
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, shutdown_cleanup)
    cfg, password, sourced = load_config()
    host = args.host or detect_host()
    port = args.port or int(cfg.get("port", 8080))
    INPUT.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.cfg = cfg
    srv.bind = f"{host}:{port}"
    print(f"Plotter web UI -> http://{host}:{port}/")
    if not cfg.get("auth", True):
        print("Authentication DISABLED (PLOTTER_WEB_AUTH=off). Anyone on the tailnet can use this UI.")
    elif password:
        print(f"Password ({sourced}): {password}")
        print("Set PLOTTER_WEB_PASSWORD in the environment to use a fixed password.")
    print("Press Ctrl-C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
