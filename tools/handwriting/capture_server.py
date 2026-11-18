#!/usr/bin/env python3
"""Handwriting capture server: shows prompts on a tablet and stores the pen strokes. Stdlib only.

  capture_server.py                       http://127.0.0.1:8765 (this computer only)
  capture_server.py --host tailscale      listen on this computer's Tailscale address, so a tablet on your tailnet can use it
  capture_server.py --host 0.0.0.0        every network interface (anyone on the LAN can write into your samples)
  capture_server.py --writer anna         the writer served at / (default: default); every writer is also at /w/<name>/

Open the printed URL in the tablet's browser, write each prompt on the ruled guide with the pen, and press Save. Samples go to
handwriting/writers/<writer>/samples/<prompt id>.json (kept out of git: it is somebody's handwriting).
Several people can use one server: each opens /w/<their name>/ (make the name first: writers.py new NAME). Saving again replaces a sample and keeps the old
one as <id>.prev.json. The prompt list is handwriting/prompts.json (build it with tools/handwriting/prompts.py).

Sample format (version 1), one JSON object per prompt:
  id, text, section, lines, saved (ISO time), pointer (pen|touch|mouse), canvas {w, h, dpr},
  guide {x_height, ascender, descender, pitch, baselines: [y, ...], left}     all in CSS pixels
  strokes: [[ [x, y, t_ms, pressure], ... ], ...]   in writing order; x and y in CSS pixels, y down; t from the first point of the
           sample; pressure 0..1 (a mouse reports 0.5). A single-point stroke is a dot (i, j, full stop).
There is no login: do not expose it beyond a network you trust.
"""
import argparse
import datetime
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import writers  # noqa: E402
ROOT = HERE.parent.parent
WEB = HERE / "web"
MAX_BODY = 8 * 1024 * 1024
MAX_POINTS = 40000
ID_RE = re.compile(r"^[a-z]\d{3}$")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
LOCK = threading.Lock()


def validate(body, prompt):
    """-> error string or None. Strict about shape and numbers; never trusts the browser."""
    if not isinstance(body, dict):
        return "body must be an object"
    strokes = body.get("strokes")
    if not isinstance(strokes, list) or not strokes:
        return "strokes must be a non-empty list"
    n = 0
    for s in strokes:
        if not isinstance(s, list) or not s:
            return "each stroke must be a non-empty list of points"
        for pt in s:
            if not (isinstance(pt, list) and len(pt) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                                  and math.isfinite(v) for v in pt)):
                return "each point must be [x, y, t_ms, pressure] with finite numbers"
            n += 1
        if n > MAX_POINTS:
            return "too many points"
    for key in ("canvas", "guide"):
        if not isinstance(body.get(key), dict):
            return f"{key} must be an object"
    return None


def clean_pressure(v):
    """What the pen reported, kept small and typed: {real, type, min, max, levels} or None."""
    if not isinstance(v, dict):
        return None
    num = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None
    return {"real": bool(v.get("real")), "type": str(v.get("type", ""))[:16], "min": num(v.get("min")),
            "max": num(v.get("max")), "levels": int(num(v.get("levels")) or 0)}


class Store:
    def __init__(self, root, prompts_path):
        self.dir = pathlib.Path(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prompts_path = pathlib.Path(prompts_path)
        doc = json.loads(self.prompts_path.read_text(encoding="utf-8"))
        self.prompts = {p["id"]: p for p in doc["prompts"]}

    def status(self):
        out = {}
        for pid in self.prompts:
            f = self.dir / f"{pid}.json"
            if f.exists():
                try:
                    s = json.loads(f.read_text(encoding="utf-8"))
                    if s.get("text") != self.prompts[pid]["text"]:
                        continue   # the prompt list was rebuilt and this id now means a different text: not done
                    out[pid] = {"strokes": len(s["strokes"]), "saved": s.get("saved")}
                except (OSError, ValueError, KeyError):
                    out[pid] = {"strokes": 0, "saved": None, "damaged": True}
        return out

    def load(self, pid):
        f = self.dir / f"{pid}.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

    def save(self, pid, body):
        p = self.prompts[pid]
        rec = {"version": 1, "id": pid, "text": p["text"], "section": p["section"], "lines": p.get("lines", 1),
               "saved": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "pointer": str(body.get("pointer", ""))[:16], "pressure_info": clean_pressure(body.get("pressure_info")), "canvas": body["canvas"], "guide": body["guide"],
               "strokes": body["strokes"]}
        f = self.dir / f"{pid}.json"
        with LOCK:
            if f.exists():
                os.replace(f, self.dir / f"{pid}.prev.json")
            tmp = self.dir / f"{pid}.tmp"
            tmp.write_text(json.dumps(rec, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, f)


class Stores:
    """One Store per writer, opened on first use. Only writers that already exist can be used."""

    def __init__(self, prompts_path, default, fixed_dir=None):
        self.prompts_path, self.default, self.fixed_dir = prompts_path, default, fixed_dir
        self.by = {}
        self.lock = threading.Lock()

    def get(self, name):
        name = name or self.default
        with self.lock:
            if name not in self.by:
                if name == self.default and self.fixed_dir:
                    d = pathlib.Path(self.fixed_dir)
                else:
                    if not writers.valid(name) or not any(w["name"] == name for w in writers.list_writers()):
                        return None
                    d = writers.samples_dir(name)
                self.by[name] = Store(d, self.prompts_path)
            return self.by[name]


WRITER_PATH = re.compile(r"^/w/([a-z][a-z0-9_-]{0,31})(/.*)?$")


def make_handler(stores):
    class H(BaseHTTPRequestHandler):
        server_version = "capture"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def send(self, code, body, ctype="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def route(self):
            """-> (store, path inside the writer), or None after answering."""
            path = self.path.split("?")[0]
            m = WRITER_PATH.match(path)
            if path in ("/w", "/w/"):
                links = "".join(f'<li><a href="/w/{w["name"]}/">{w["name"]}</a> ({w["samples"]} samples)</li>'
                                for w in writers.list_writers())
                self.send(200, ("<!doctype html><meta name=viewport content='width=device-width'><title>Writers</title>"
                                "<h1>Who is writing?</h1><ul>" + links + "</ul>").encode(), "text/html; charset=utf-8")
                return None
            if m:
                name, rest = m.group(1), m.group(2)
                if rest is None:
                    self.send_response(301)
                    self.send_header("Location", f"/w/{name}/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                path = rest
            else:
                name = None
            store = stores.get(name)
            if store is None:
                self.send(404, {"error": "no such writer: make one with tools/handwriting/writers.py new NAME"})
                return None
            return store, path

        def do_GET(self):
            r = self.route()
            if r is None:
                return
            store, path = r
            if path == "/api/prompts":
                return self.send(200, json.loads(store.prompts_path.read_text(encoding="utf-8")))
            if path == "/api/samples":
                return self.send(200, store.status())
            m = re.fullmatch(r"/api/sample/([a-z]\d{3})", path)
            if m:
                if m.group(1) not in store.prompts:
                    return self.send(404, {"error": "unknown prompt"})
                s = store.load(m.group(1))
                return self.send(200 if s else 404, s or {"error": "no sample yet"})
            name = "capture.html" if path in ("/", "/index.html") else path.lstrip("/")
            f = (WEB / name)
            if "/" not in name and f.suffix in TYPES and f.is_file():
                return self.send(200, f.read_bytes(), TYPES[f.suffix])
            self.send(404, {"error": "not found"})

        def do_POST(self):
            r = self.route()
            if r is None:
                return
            store, path = r
            m = re.fullmatch(r"/api/sample/([a-z]\d{3})", path)
            if not m or m.group(1) not in store.prompts:
                return self.send(404, {"error": "unknown prompt"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.send(400, {"error": "bad length"})
            if not 0 < n <= MAX_BODY:
                return self.send(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self.send(400, {"error": "not JSON"})
            err = validate(body, store.prompts[m.group(1)])
            if err:
                return self.send(400, {"error": err})
            store.save(m.group(1), body)
            self.send(200, {"ok": True, "id": m.group(1)})

    return H


def resolve_host(host):
    if host != "tailscale":
        return host
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.split()[0]
    except (OSError, subprocess.SubprocessError, IndexError) as ex:
        sys.exit(f"cannot get the Tailscale address ({ex}); pass --host with an address")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on, or 'tailscale' (default %(default)s)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--prompts", default=str(ROOT / "handwriting" / "prompts.json"))
    ap.add_argument("--writer", default="default", help="the writer served at / (default %(default)s)")
    ap.add_argument("--samples", help="use this samples directory for --writer (default: the writer's own)")
    a = ap.parse_args()
    if not pathlib.Path(a.prompts).exists():
        sys.exit(f"{a.prompts} not found; run tools/handwriting/prompts.py first")
    host = resolve_host(a.host)
    if not writers.valid(a.writer):
        sys.exit("writer name: lowercase letters, digits, - and _, starting with a letter")
    a.samples = a.samples or str(writers.create(a.writer))
    stores = Stores(a.prompts, a.writer, a.samples)
    store = stores.get(a.writer)
    srv = ThreadingHTTPServer((host, a.port), make_handler(stores))
    done = len(store.status())
    stale = len(list(store.dir.glob("[a-z][0-9][0-9][0-9].json"))) - done
    if stale > 0:
        print(f"NOTE: {stale} saved sample(s) no longer match their prompt text (prompt list rebuilt); they are kept but not counted.")
    print(f"Handwriting capture for '{a.writer}': http://{host}:{a.port}/   ({done} of {len(store.prompts)} prompts already saved)")
    print(f"Everyone: http://{host}:{a.port}/w/   (new writers: tools/handwriting/writers.py new NAME)")
    if host in ("0.0.0.0", "::"):
        print("WARNING: listening on every interface with no login; anyone on the network can write into the samples.")
    print(f"Samples: {a.samples}   Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
