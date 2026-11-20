#!/usr/bin/env python3
"""Script the plotter web server: upload a drawing, put it on the bed, preview, start, watch. Standard library only.

    python tools/plotter_client.py http://robotlab:8080 drawing.svg --simulate
    python tools/plotter_client.py http://robotlab:8080 drawing.svg --x 80 --y 40 --width 120 --plot

--plot moves the real machine: the carriage must be parked by hand at S with the pen up, the same promise the web
page asks for. Without --plot or --simulate it uploads, places and previews only. See docs/api.md.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class Plotter:
    def __init__(self, base, password=None):
        self.base = base.rstrip("/")
        self.cookie = None
        if password:
            self.call("POST", "/api/login", {"password": password})

    def call(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if raw is None else {}
        h.update(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if r.headers.get("Set-Cookie"):
                    self.cookie = r.headers["Set-Cookie"].split(";", 1)[0]
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as ex:
            try:
                msg = json.loads(ex.read()).get("error")
            except ValueError:
                msg = None
            raise SystemExit(f"{method} {path}: {ex.code} {msg or ex.reason}")

    def upload(self, path, params=None):
        p = pathlib.Path(path)
        return self.call("POST", "/api/files/upload", raw=p.read_bytes(),
                         headers={"X-Filename": urllib.parse.quote(p.name),
                                  "X-Params": urllib.parse.quote(json.dumps(params or {}))})["file"]

    def place(self, file_id, x=None, y=None, width=None):
        """The drawing as the only object on the bed: natural size (or width mm), at x, y or the canvas corner."""
        g = self.call("GET", f"/api/files/natural?id={urllib.parse.quote(file_id)}")["geom"]
        canvas = self.call("GET", "/api/me")["machine"]["canvas"]
        w = width or g["raw_w"]
        h = w * g["raw_h"] / g["raw_w"] if g["raw_w"] else g["raw_h"]
        return {"id": "o1", "file": file_id, "x": canvas[0] if x is None else x, "y": canvas[1] if y is None else y,
                "w": w, "h": h, "rot": 0, "fx": False, "fy": False, "g": g.get("svg_hash", "")}

    def preview(self, objects, settings=None):
        doc = {"objects": objects, "settings": settings or {}}
        return doc, self.call("POST", "/api/preview", {"doc": doc})

    def start(self, doc, plan_id, mode, parked=False):
        return self.call("POST", "/api/job/start", {"doc": doc, "plan_id": plan_id, "mode": mode,
                                                    "confirm": {"parked": parked}})

    def wait(self, every=2.0, out=sys.stdout, auto_continue=False):
        """Until the job ends. At a pen change (paused) it asks for Enter, or goes on by itself with auto_continue."""
        seen = None
        while True:
            job = self.call("GET", "/api/state").get("job") or {}
            st = job.get("state")
            if st == "paused" and job.get("pause") and job["pause"] != seen:
                seen = job["pause"]
                print(f"\npaused: {job.get('message') or 'waiting'}", file=out)
                if not auto_continue:
                    input("Press Enter to continue (Ctrl+C leaves it paused)... ")
                self.call("POST", "/api/job/continue", {"job": job.get("id"), "pause": job["pause"].get("id")})
                continue
            pct = job.get("progress", {}).get("pct") if isinstance(job.get("progress"), dict) else None
            print(f"\r{st or 'waiting'} {'' if pct is None else f'{pct:.0f}%'}    ", end="", file=out, flush=True)
            if st in ("complete", "stopped", "interrupted", "error"):
                print(file=out)
                return job
            time.sleep(every)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("server")
    ap.add_argument("drawing", help="SVG (or an image, DXF, HPGL: the server converts it)")
    ap.add_argument("--x", type=float, help="left edge on the bed, mm (default: the canvas corner)")
    ap.add_argument("--y", type=float, help="top edge on the bed, mm")
    ap.add_argument("--width", type=float, help="width in mm (default: the drawing's own size)")
    ap.add_argument("--speed", type=float, help="drawing speed, mm/s")
    ap.add_argument("--password", help="only when the server asks for one")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--simulate", action="store_true", help="run it on the simulator (nothing moves)")
    g.add_argument("--plot", action="store_true", help="plot on the machine: the carriage is parked at S, pen up")
    ap.add_argument("--yes", action="store_true", help="go on by itself at pen changes (for the simulator)")
    a = ap.parse_args()
    p = Plotter(a.server, a.password)
    f = p.upload(a.drawing)
    print(f"uploaded {f['name']} as {f['id']}")
    obj = p.place(f["id"], a.x, a.y, a.width)
    doc, pv = p.preview([obj], {"speed": a.speed} if a.speed else {})
    s = pv.get("summary") or {}
    print(f"preview: {s.get('strokes')} strokes, {s.get('drawing_mm', 0):.0f} mm of line, about "
          f"{s.get('est_seconds_all', s.get('est_seconds', 0)) / 60:.1f} min")
    if not pv["ok"]:
        raise SystemExit("not plottable: " + "; ".join(pv["problems"]))
    if a.simulate or a.plot:
        p.start(doc, pv["plan_id"], "simulate" if a.simulate else "plot", parked=a.plot)
        job = p.wait(auto_continue=a.yes)
        print(f"ended: {job.get('state')}")


if __name__ == "__main__":
    main()
