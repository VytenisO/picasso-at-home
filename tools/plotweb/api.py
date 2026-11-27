"""HTTP API and the orchestration between library, previews and the job runner.

Routes are listed in ROUTES_GET / ROUTES_POST; docs/ui-design.md documents them. Everything returns JSON
({"ok": false, "error": "..."} on failure) except static files, file downloads and the event stream.
"""
import hashlib
import http.cookies
import json
import math
import mimetypes
import pathlib
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import converters, document, hardware, machinecfg, plotjob, prepare, shapes, tasks, tools
from .auth import COOKIE, SESSION_AGE, Auth
from .config import CEILINGS, LIMITS, WEB, apply_limits
from .events import EventLog
from .library import MAX_UPLOAD, Library
from .runner import JobRunner

import penpot  # noqa: E402  (tools/ is on sys.path through .config)

TOOL_LOCK = threading.Lock()   # test-sheet and result tools rewrite shared files: one at a time

MAX_DISPLAY_POINTS = 300_000     # preview strokes sent to the browser are thinned beyond this (display only)
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/favicon.svg": "favicon.svg"}


class ApiError(Exception):
    def __init__(self, code, msg, **extra):
        super().__init__(msg)
        self.code, self.msg, self.extra = code, msg, extra


class App:
    def __init__(self, settings, web_cfg):
        self.s = settings
        try:
            apply_limits(settings.machine())
        except (OSError, ValueError):
            pass
        self.ev = EventLog()
        self.auth = Auth(web_cfg)
        self.lib = Library(settings, self.ev)
        self.runner = JobRunner(settings, self.ev)
        self.ntfy = str(web_cfg.get("ntfy") or "").strip()
        if self.ntfy:
            self.runner.notify = self._push
        self.prep = prepare.Workshop(settings)
        self.tasks = tasks.Tasks(self.ev, settings.cache / "tasks.json")
        self.presets = prepare.Presets(settings)
        self.docs = document.DocStore(settings, self.lib, self.ev)
        self.plans = {}
        self._fill_layers = {}      # (svg hash, fill spacing, angle) -> (layer keys, layers)
        self.plans_lock = threading.Lock()
        self.hw_lock = threading.Lock()      # one read-only plotter check at a time, never during a job start
        hardware.load_port_choice(settings)
        if not settings.sim:
            hardware.ServoGuard(settings, self.runner, self.hw_lock, self.ev.log).start()
            self.button = hardware.PenButton(settings, self.runner, self.hw_lock, self.ev.log,
                                             emit=lambda **kw: self.ev.emit("pot", **kw))
            self.runner.hold = self.button.hold_reason
            self.button.start()
        self.plan_dir = settings.cache / "plans"
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        for old in self.plan_dir.iterdir():          # staged plans are only needed for a day
            try:
                if time.time() - old.stat().st_mtime > 2 * 24 * 3600:
                    shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink()
            except OSError:
                pass
        self.ev.log("info", "Server started" + (" in SIMULATOR mode: no hardware is ever touched." if settings.sim
                                                else "."))

    # ------------------------------------------------------------ helpers
    def me(self, authed):
        bed = self.s.bed()
        cfg = self.s.machine()
        apply_limits(cfg)
        return {"auth": authed, "auth_required": self.auth.enabled, "sim": self.s.sim, "machine": bed,
                "limits": LIMITS, "pens": self.s.pens() if authed else {}, "epoch": self.ev.epoch,
                "features": {"shapes": shapes.available(), "homing": bool(self.s.machine().get("homing"))},
                "plotter": hardware.LAST if authed else None,
                "machine_cfg": {"travel": cfg.get("travel_mm"), "signs": {a: cfg["axis"][a].get("sign", 1) for a in ("x", "y")},
                                "limits": cfg.get("limits") or {}, "ceilings": CEILINGS,
                                "steps_per_mm": cfg.get("steps_per_mm"), "measured": cfg.get("measured"),
                                "pen_pot": penpot.settings(cfg)}
                if authed else None}

    def state(self):
        seq = self.ev.seq           # first: events after it are replayed; the snapshot is at least this new
        snap = self.runner.snapshot()
        snap.update(epoch=self.ev.epoch, seq=seq, files_rev=self.lib.rev, sim=self.s.sim)
        snap["pot"] = self.button.pot if getattr(self, "button", None) else None
        return snap

    def _entry(self, ident):
        e = self.lib.get(ident or "")
        if not e:
            raise ApiError(404, "That file is no longer in the library. Reload the file list.")
        p = self.lib.path(e)
        if not p:
            raise ApiError(404, "The file's plot data is missing on the server. Upload it again.")
        return e, p

    def geometry(self, e, svg_path, fill=0.0, fill_angle=45.0):
        """Natural-size geometry and the layer list of a file, cached in its manifest entry.

        With a fill the layer list can have more inks (a fill colour the outlines do not have): that list is
        measured by its own dry run and remembered in memory."""
        data = svg_path.read_bytes()
        # The planner decides the layers (colours, keys), so a new plot_svg.py invalidates the cached geometry.
        digest = hashlib.sha256(data + planner_hash().encode()).hexdigest()[:16]
        if fill:
            g, _, _ = self.geometry(e, svg_path)
            memo = (digest, fill, fill_angle)
            hit = self._fill_layers.get(memo)
            if hit is None:
                tmp = self.plan_dir / f"geomf_{digest}_{secrets.token_hex(4)}.json"
                v = {"speed": LIMITS["speed"]["default"], "travel_speed": LIMITS["travel_speed"]["default"], "x": None,
                     "y": None, "w": None, "paper": None, "layers": [], "fill": fill, "fill_angle": fill_angle}
                rc, out = plotjob.run_dry(plotjob.build_argv(self.s, svg_path, v, "dry", None, preview_json=tmp))
                if not tmp.exists():
                    tail = [ln for ln in out.strip().splitlines() if ln.strip() and not ln.startswith("__")]
                    raise ApiError(400, "This fill cannot be plotted: " + (tail[-1] if tail else f"exit code {rc}"))
                sm = json.loads(tmp.read_text())["summary"]
                tmp.unlink()
                lay = [{k: ly.get(k) for k in ("key", "name", "color", "strokes")} for ly in sm["layers"]]
                hit = ([ly["key"] for ly in lay], lay)
                if len(self._fill_layers) > 200:
                    self._fill_layers.clear()
                self._fill_layers[memo] = hit
            return g, hit[0], hit[1]
        g = e.get("geom")
        natural = self.plan_dir / f"natural_{digest}.json"
        if g and g.get("svg_hash") == digest and g.get("raw_x") is not None and e.get("layers_keys") is not None \
                and natural.exists():
            return g, e["layers_keys"], e.get("layers") or []
        tmp = self.plan_dir / f"geom_{digest}_{secrets.token_hex(4)}.json"
        # Measured in absolute mode: the strokes come out in the drawing's own millimetres, which is what an
        # object's transform starts from. The placement problems this reports do not matter here.
        v = {"speed": LIMITS["speed"]["default"], "travel_speed": LIMITS["travel_speed"]["default"],
             "x": None, "y": None, "w": None, "paper": None, "layers": [], "absolute": True}
        rc, out = plotjob.run_dry(plotjob.build_argv(self.s, svg_path, v, "dry", None, preview_json=tmp))
        if not tmp.exists():
            tail = [ln for ln in out.strip().splitlines() if ln.strip() and not ln.startswith("__")]
            raise ApiError(400, "This drawing cannot be plotted: " + (tail[-1] if tail else f"exit code {rc}"),
                           output=out[-4000:])
        pv = json.loads(tmp.read_text())
        sm = pv["summary"]
        b = sm["bbox_machine_mm"]
        g = {"svg_hash": digest, "raw_x": b[0], "raw_y": b[1], "raw_w": b[2] - b[0], "raw_h": b[3] - b[1],
             "doc_w": sm["doc_mm"][0], "doc_h": sm["doc_mm"][1]}
        tmp.write_text(json.dumps({"geom": g, "strokes": _thin(pv["strokes"]),
                                   "layers": [{k: ly.get(k) for k in ("key", "name", "color", "strokes")}
                                              for ly in sm["layers"]]}, separators=(",", ":")))
        tmp.replace(natural)
        layers = [{k: ly.get(k) for k in ("key", "name", "color", "strokes")} for ly in sm["layers"]]
        keys = [ly["key"] for ly in layers]
        self.lib.update(e["id"], geom=g, layers_keys=keys, layers=layers)
        return g, keys, layers

    def _resolve(self, e, svg_path, raw_settings):
        """-> resolved settings, geometry, plan id, layers, plan dir (with staged svg, machine.json, layers.json)."""
        rs = raw_settings if isinstance(raw_settings, dict) else {}
        try:
            fill = float(rs.get("fill") or 0)
            fill_angle = float(rs.get("fill_angle", 45.0) if rs.get("fill_angle") not in (None, "") else 45.0)
        except (TypeError, ValueError):
            fill, fill_angle = 0.0, 45.0
        if not 0.2 <= fill <= 50 or not 0 <= fill_angle <= 180:
            fill = 0.0       # out of range: validate() below says so
        g, keys, layers = self.geometry(e, svg_path, fill, fill_angle)
        svg_bytes, machine_bytes = svg_path.read_bytes(), self.s.machine_path.read_bytes()
        bed = self.s.bed_from(machine_bytes)
        if e.get("kind") == "sheet" and rs.get("w") not in (None, "", 0):
            raise ApiError(400, f"{e['name']} is a test sheet: it plots at its own size, so a width cannot be set.")
        if e.get("pen"):
            # A test sheet belongs to one pen type: every layer uses it unless the operator chose otherwise.
            raw_settings = dict(raw_settings or {})
            given = {ly.get("key"): ly for ly in raw_settings.get("layers") or [] if isinstance(ly, dict)}
            raw_settings["layers"] = [dict(given.get(k) or {"key": k}, pen=(given.get(k) or {}).get("pen") or e["pen"])
                                      for k in keys]
        try:
            v = plotjob.validate(raw_settings, bed, self.s.pens(), keys)
        except plotjob.SettingsError as ex:
            raise ApiError(400, str(ex))
        plotjob.resolve_placement(v, bed, g)
        if e.get("kind") == "sheet":
            v["as_is"] = True
        pid, d = self._stage(svg_bytes, machine_bytes, v)
        return v, g, pid, layers, d

    def _stage(self, svg_bytes, machine_bytes, v):
        pid = plotjob.plan_id(svg_bytes, machine_bytes, v)
        d = self.plan_dir / pid
        if not (d / "machine.json").exists():
            d.mkdir(exist_ok=True)
            fills = {f["key"]: {k: x for k, x in f.items() if k != "key"} for f in v.get("fills") or []}
            for name, data in (("drawing.svg", svg_bytes), ("layers.json", json.dumps(v["layers"]).encode()),
                               ("fills.json", json.dumps(fills).encode()),
                               ("machine.json", machine_bytes)):     # machine.json last: it marks the stage done
                tmp = d / f".{name}.{secrets.token_hex(4)}"
                tmp.write_bytes(data)
                tmp.replace(d / name)
        return pid, d

    def natural(self, ident):
        """A drawing at natural size in its own mm: geometry box, layers and strokes, for drawing objects."""
        e, p = self._entry(ident)
        g, _, _ = self.geometry(e, p)
        data = json.loads((self.plan_dir / f"natural_{g['svg_hash']}.json").read_text())
        data["file"] = e["id"]
        return data

    def _doc_layers(self, svg_bytes, fill, fill_angle, fills=()):
        """Layer keys and layers of a composed document (one dry run per distinct document and fill settings)."""
        fills_json = json.dumps({f["key"]: {k: x for k, x in f.items() if k != "key"} for f in fills}, sort_keys=True)
        memo = (hashlib.sha256(svg_bytes + planner_hash().encode()).hexdigest()[:20], fill, fill_angle, fills_json)
        hit = self._fill_layers.get(memo)
        if hit is None:
            req = self.plan_dir / f"docl_{secrets.token_hex(4)}"
            req.mkdir()
            try:
                (req / "doc.svg").write_bytes(svg_bytes)
                (req / "layers.json").write_text("[]")
                (req / "fills.json").write_text(fills_json)
                v = {"speed": LIMITS["speed"]["default"], "travel_speed": LIMITS["travel_speed"]["default"],
                     "x": None, "y": None, "w": None, "paper": None, "layers": [], "absolute": True,
                     "fill": fill, "fill_angle": fill_angle, "fills": list(fills)}
                rc, out = plotjob.run_dry(plotjob.build_argv(self.s, req / "doc.svg", v, "dry", None,
                                                             preview_json=req / "pv.json",
                                                             layers_json=req / "layers.json"))
                if not (req / "pv.json").exists():
                    tail = [ln for ln in out.strip().splitlines() if ln.strip() and not ln.startswith("__")]
                    raise ApiError(400, "The bed cannot be plotted: " + (tail[-1] if tail else f"exit code {rc}"))
                sm = json.loads((req / "pv.json").read_text())["summary"]
            finally:
                shutil.rmtree(req, ignore_errors=True)
            lay = [{k: ly.get(k) for k in ("key", "name", "color", "strokes")} for ly in sm["layers"]]
            hit = ([ly["key"] for ly in lay], lay)
            if len(self._fill_layers) > 200:
                self._fill_layers.clear()
            self._fill_layers[memo] = hit
        return hit

    def _resolve_doc(self, raw_doc):
        """The bed document -> resolved settings, objects, plan id, layers, plan dir (like _resolve)."""
        raw_doc = raw_doc if isinstance(raw_doc, dict) else {}
        known = {e["id"] for e in self.lib.list()}
        try:
            objs = document.clean_objects(raw_doc.get("objects") or [], known)
        except document.DocError as ex:
            raise ApiError(400, str(ex))
        if not objs:
            raise ApiError(400, "The bed is empty. Add a drawing from the Library.")
        objs = [o for o in objs if not o.get("guide")]
        if not objs:
            raise ApiError(400, "Everything on the bed is a guide, and guides are not plotted. Right-click a drawing "
                                "and choose Plot it (not a guide).")
        rs = raw_doc.get("settings") if isinstance(raw_doc.get("settings"), dict) else {}
        if isinstance(rs.get("only"), list) and rs["only"]:
            objs = [o for o in objs if o["id"] in rs["only"]]
            if not objs:
                raise ApiError(400, "None of the objects chosen for plotting are on the bed. Plot all of them, "
                                    "or choose again.")
        # Registration: two points of the design (where marks were plotted) and where those marks are now on
        # re-inserted paper. The whole bed is shifted, turned and scaled to match.
        reg = rs.get("register")
        if reg:
            try:
                (ax_, ay_), (bx_, by_) = reg["from"]
                (cx_, cy_), (dx_, dy_) = reg["to"]
                ax_, ay_, bx_, by_, cx_, cy_, dx_, dy_ = (float(v) for v in (ax_, ay_, bx_, by_, cx_, cy_, dx_, dy_))
            except (KeyError, TypeError, ValueError):
                raise ApiError(400, "Registration needs two design points (from) and two measured points (to).")
            d0, d1 = math.hypot(bx_ - ax_, by_ - ay_), math.hypot(dx_ - cx_, dy_ - cy_)
            if d0 < 10 or d1 < 10:
                raise ApiError(400, "The two registration marks must be at least 10 mm apart.")
            k = d1 / d0
            if not 0.95 <= k <= 1.05:
                raise ApiError(400, f"The marks are {d1:.1f} mm apart now and {d0:.1f} mm in the design: more than 5% "
                                    "off. Check that the right marks were measured.")
            ang = math.atan2(dy_ - cy_, dx_ - cx_) - math.atan2(by_ - ay_, bx_ - ax_)
            co, si = math.cos(ang) * k, math.sin(ang) * k

            def reg_pt(x, y):
                return cx_ + co * (x - ax_) - si * (y - ay_), cy_ + si * (x - ax_) + co * (y - ay_)
            moved = []
            for o in objs:
                mx_, my_ = reg_pt(o["x"] + o["w"] / 2, o["y"] + o["h"] / 2)
                w, h = o["w"] * k, o["h"] * k
                moved.append(dict(o, x=round(mx_ - w / 2, 3), y=round(my_ - h / 2, 3), w=round(w, 3), h=round(h, 3),
                                  rot=round((o["rot"] + math.degrees(ang)) % 360, 4)))
            objs = moved
        # Start from: the job's anchor point (one of nine on the objects' box) at the carriage's position now or a
        # saved position, instead of where the objects sit on the bed.
        origin = str(rs.get("origin") or "")
        if origin:
            if origin == "here":
                pos = self.runner.snapshot()["position"]
                if pos.get("state") == "unknown" or not pos.get("at"):
                    raise ApiError(400, "Start from the carriage's position: the position is not known. Park at S or "
                                        "move it with the Machine controls first.")
                tx, ty = pos["at"]
            elif origin.startswith("pos:"):
                p = next((p for p in self.list_get("positions") if p.get("name") == origin[4:]), None)
                if not p:
                    raise ApiError(400, f"There is no saved position called {origin[4:]}.")
                tx, ty = p["x"], p["y"]
            else:
                raise ApiError(400, "Start from must be the bed, here or a saved position.")
            pts = [c for o in objs for c in document.corners(o)]
            x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
            x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
            anc = str(rs.get("origin_anchor") or "tl")
            if len(anc) != 2 or anc[0] not in "tmb" or anc[1] not in "lcr":
                raise ApiError(400, "The job anchor must be one of tl, tc, tr, ml, mc, mr, bl, bc, br.")
            ax = {"l": x0, "c": (x0 + x1) / 2, "r": x1}[anc[1]]
            ay = {"t": y0, "m": (y0 + y1) / 2, "b": y1}[anc[0]]
            dx, dy = float(tx) - ax, float(ty) - ay
            objs = [dict(o, x=round(o["x"] + dx, 3), y=round(o["y"] + dy, 3)) for o in objs]
        try:
            fill = float(rs.get("fill") or 0)
            fill_angle = float(rs.get("fill_angle", 45.0) if rs.get("fill_angle") not in (None, "") else 45.0)
        except (TypeError, ValueError):
            fill, fill_angle = 0.0, 45.0
        if not 0.2 <= fill <= 50 or not 0 <= fill_angle <= 180:
            fill = 0.0       # out of range: validate() below says so
        files = {}
        for o in objs:
            if o["file"] not in files:
                e, p = self._entry(o["file"])
                files[o["file"]] = (p, self.geometry(e, p)[0])
        objs = [self._as_is(o, files[o["file"]][1]) for o in objs]
        machine_bytes = self.s.machine_path.read_bytes()
        bed = self.s.bed_from(machine_bytes)
        try:
            svg_bytes = document.compose(objs, files, bed, document.clean_remap(rs.get("recolor")))
        except (document.DocError, ET.ParseError) as ex:
            raise ApiError(400, f"Could not put the drawings together: {ex}")
        try:
            fills = plotjob.validate_fills(rs.get("fills"))
        except plotjob.SettingsError as ex:
            raise ApiError(400, str(ex))
        keys, layers = self._doc_layers(svg_bytes, fill, fill_angle, fills)
        # A layer seen for the first time takes the pen its colour had last time.
        have = {ly.get("key") for ly in rs.get("layers") or [] if isinstance(ly, dict)}
        pmap = self._pen_map()
        pens = self.s.pens()
        extra = [{"key": k, "pen": pmap[k.split("|")[0]]} for k in keys
                 if k not in have and pmap.get(k.split("|")[0]) in pens]
        try:
            v = plotjob.validate(dict(rs, x=None, y=None, w=None, layers=list(rs.get("layers") or []) + extra), bed,
                                 pens, keys)
        except plotjob.SettingsError as ex:
            raise ApiError(400, str(ex))
        v["absolute"] = True
        pid, d = self._stage(svg_bytes, machine_bytes, v)
        return v, objs, pid, layers, d

    def _as_is(self, o, geom):
        """A test sheet plots exactly as it was made: at its own size, unturned, unmirrored, every stroke as drawn
        (plot_svg AS_IS). -> the object, marked; raises ApiError for a sheet that was resized, turned or mirrored."""
        e = self.lib.get(o["file"]) or {}
        if e.get("kind") != "sheet":
            return o
        w, h = geom["raw_w"], geom["raw_h"]
        off = abs(o["w"] - w) > max(0.2, w * 0.002) or abs(o["h"] - h) > max(0.2, h * 0.002)
        turned = min(o["rot"] % 360, 360 - o["rot"] % 360) > 0.01
        if off or turned or o["fx"] or o["fy"]:
            what = ", ".join(x for x, bad in (("resized", off), ("turned", turned), ("mirrored", o["fx"] or o["fy"])) if bad)
            raise ApiError(400, f"{e.get('name') or 'A test sheet'} is a test sheet and was {what}. It plots exactly as "
                                f"made: {w:.1f} x {h:.1f} mm, unturned and unmirrored. Remove it and add it again from the Library.")
        return dict(o, asis=True)

    def _pen_map(self):
        try:
            m = json.loads((self.s.cache / "pen_map.json").read_text())
            return m if isinstance(m, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember_pens(self, layers):
        """Colour -> pen, from the layers of a checked plan: the next drawing in that colour starts with it."""
        m = self._pen_map()
        new = dict(m)
        for ly in layers:
            if ly.get("pen"):
                new[ly["key"].split("|")[0]] = ly["pen"]
        if new != m:
            (self.s.cache / "pen_map.json").write_text(json.dumps(new, indent=1))

    # ---- small JSON lists kept on the server: saved positions, macros
    def _list_path(self, name):
        return self.s.cache / f"{name}.json"

    def list_get(self, name):
        try:
            d = json.loads(self._list_path(name).read_text())
            return d if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    def list_put(self, name, item, fields):
        nm = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()[:40]
        if not nm:
            raise ApiError(400, "Give it a name.")
        clean = {"name": nm}
        for k, kind in fields.items():
            v = item.get(k)
            if kind == "num":
                try:
                    v = round(float(v), 3)
                except (TypeError, ValueError):
                    raise ApiError(400, f"{k} must be a number.")
                if not -10 <= v <= 1000:
                    raise ApiError(400, f"{k} is off the bed.")
            else:
                v = str(v or "")[:2000]
            clean[k] = v
        items = [x for x in self.list_get(name) if x.get("name") != nm] + [clean]
        if len(items) > 50:
            raise ApiError(400, "At most 50.")
        p = self._list_path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1))
        tmp.replace(p)
        return items

    def list_delete(self, name, nm):
        items = [x for x in self.list_get(name) if x.get("name") != nm]
        p = self._list_path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1))
        tmp.replace(p)
        return items

    def about(self):
        import platform
        import subprocess
        def ver(mod):
            try:
                m = __import__(mod)
                return getattr(m, "__version__", "installed")
            except Exception:  # noqa: BLE001
                return None
        try:
            rev = subprocess.run(["git", "-C", str(self.s.root), "log", "-1", "--format=%h %cs"], capture_output=True,
                                 text=True, timeout=5).stdout.strip() or None
        except Exception:  # noqa: BLE001
            rev = None
        return {"app": rev, "python": platform.python_version(), "system": platform.platform(),
                "pyserial": ver("serial"), "shapely": ver("shapely"), "pillow": ver("PIL"),
                "firmware": (hardware.LAST or {}).get("firmware"), "sim": self.s.sim}

    def _push(self, title, text):
        """POST a message to the ntfy topic URL in web.json ("ntfy"). Best effort, off the job's thread."""
        import urllib.request

        def send():
            try:
                req = urllib.request.Request(self.ntfy, data=str(text).encode()[:3000], method="POST",
                                             headers={"Title": str(title).encode("ascii", "replace").decode()[:200]})
                urllib.request.urlopen(req, timeout=8).close()
            except Exception as ex:  # noqa: BLE001
                self.ev.log("warn", f"Push message to ntfy failed: {ex}")
        threading.Thread(target=send, daemon=True).start()

    def export_doc(self, body):
        """-> SVG text of exactly what the bed plots: every stroke after fills, layer order and the plot's start
        point, in machine mm, one group per pen in plot order."""
        raw = body.get("doc")
        if not isinstance(raw, dict):
            raise ApiError(400, "Send the bed.")
        v, objs, pid, layers, plan = self._resolve_doc(raw)
        ok, problems, pv, out = self._dry(plan, v, None)
        if not pv:
            raise ApiError(409, "Could not plan the bed: " + "; ".join(problems))
        bw, bh = self._bed(plan)["bed"]
        sl = pv["summary"]["layers"]
        groups, seen = {}, []
        for i in pv["order"]:
            s = pv["strokes"][i]
            if s["l"] not in groups:
                groups[s["l"]] = []
                seen.append(s["l"])
            groups[s["l"]].append(s["pts"])
        esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")  # noqa: E731
        parts = [f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
                 f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="{bw:g}mm" height="{bh:g}mm" '
                 f'viewBox="0 0 {bw:g} {bh:g}">']
        for n, l in enumerate(seen):
            ly = sl[l] if l < len(sl) else {}
            name = ly.get("title") or ly.get("name") or f"Layer {n + 1}"
            label = f"{n + 1}. {name}" + (f" ({ly['pen']})" if ly.get("pen") else "")
            d = " ".join("M" + " L".join(f"{x:g} {y:g}" for x, y in pts) for pts in groups[l])
            parts.append(f'<g inkscape:groupmode="layer" inkscape:label="{esc(label)}"><path d="{d}" fill="none" '
                         f'stroke="{esc(ly.get("color") or "#000000")}" stroke-width="0.3" stroke-linecap="round" '
                         f'stroke-linejoin="round"/></g>')
        parts.append("</svg>\n")
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw.get("name") or "bed")).strip("._") or "bed"
        return "".join(parts), f"{name}-plot.svg"

    def rename_file(self, body):
        from .library import safe_name
        e = self.lib.get(body.get("id"))
        if not e:
            raise ApiError(404, "Unknown file.")
        suffix = pathlib.Path(e["name"]).suffix
        stem = str(body.get("name") or "").strip()
        if stem.lower().endswith(suffix.lower()):
            stem = stem[: -len(suffix)]
        stem = safe_name(stem)
        if not stem or stem == "upload" and not str(body.get("name") or "").strip():
            raise ApiError(400, "Give the drawing a name.")
        new = stem[: 100 - len(suffix)] + suffix
        self.lib.update(e["id"], name=new)
        self.ev.log("info", f"Renamed {e['name']} to {new}.")
        return {"ok": True, "file": {"id": e["id"], "name": new}}

    def object_strokes(self, objs):
        """[(colour, stroke in machine mm)] of bed objects, in order."""
        import plot_svg
        bed = self.s.bed()
        out = []
        for o in objs:
            e, p = self._entry(o["file"])
            g = self.geometry(e, p)[0]
            tmp = self.plan_dir / f"strk_{secrets.token_hex(4)}.svg"
            try:
                tmp.write_bytes(document.compose([o], {o["file"]: (p, g)}, bed))
                polys, metas, _, _ = plot_svg.load_svg(str(tmp), True)
            finally:
                tmp.unlink(missing_ok=True)
            out += [(m.get("color") or "#000000", [tuple(pt) for pt in q]) for q, m in zip(polys, metas)]
        return out

    def split_objects(self, objs):
        """Each stroke of the objects as its own drawing. -> [{file, x, y, w, h}]"""
        strokes = [(c, s) for c, s in self.object_strokes(objs) if len(s) >= 2]
        if len(strokes) < 2:
            raise ApiError(400, "Nothing to break apart: one stroke is one piece already.")
        room = document.MAX_OBJECTS - (len(self.docs.get().get("objects") or []) - len(objs))
        if len(strokes) > room:
            raise ApiError(400, f"That makes {len(strokes)} pieces; the bed has room for {room} more objects "
                                f"(at most {document.MAX_OBJECTS}).")
        base = "-".join(sorted({(self.lib.get(o["file"]) or {}).get("name", "").removesuffix(".svg") for o in objs}))[:30]
        out = []
        for n, (c, s) in enumerate(strokes, 1):
            text, b = shapes.svg_layers([(c, [s])])
            e = self.lib.add_svg_text(text, f"{base}-part{n}.svg", kind="shape")
            out.append({"file": {"id": e["id"], "name": e["name"]}, "x": round(b[0], 3), "y": round(b[1], 3),
                        "w": round(b[2] - b[0], 3), "h": round(b[3] - b[1], 3)})
        self.ev.log("info", f"Broke {len(objs)} object(s) apart into {len(out)} pieces.")
        return out

    def join_objects(self, objs, tol):
        """Strokes of several bed objects -> one drawing where touching ends of the same colour are joined."""
        import collections
        import pathplan
        import plot_svg
        bed = self.s.bed()
        by = collections.OrderedDict()
        before = 0
        for o in objs:
            e, p = self._entry(o["file"])
            g = self.geometry(e, p)[0]
            tmp = self.plan_dir / f"join_{secrets.token_hex(4)}.svg"
            try:
                tmp.write_bytes(document.compose([o], {o["file"]: (p, g)}, bed))
                polys, metas, _, _ = plot_svg.load_svg(str(tmp), True)
            finally:
                tmp.unlink(missing_ok=True)
            for q, m in zip(polys, metas):
                by.setdefault(m.get("color") or "#000000", []).append([tuple(pt) for pt in q])
                before += 1
        groups = [(c, pathplan.join_touching(ps, tol)) for c, ps in by.items()]
        after = sum(len(ps) for _, ps in groups)
        if not after:
            raise ApiError(400, "These objects have no lines to join.")
        return shapes.svg_layers(groups), before, after

    LINE_OPS = ("close", "smooth", "hull", "fillet", "cut", "trim", "affine", "warp", "penwidth", "arcs", "grid",
                "set", "fill", "unfill")

    def line_op(self, op, objs, body):
        """The bed's line tools. -> a new drawing (or several, see cut) replacing the objects it was made from."""
        def num(k, lo, hi, label, default=None):
            try:
                v = float(body.get(k, default))
            except (TypeError, ValueError):
                raise ApiError(400, f"{label} must be a number.")
            if not lo <= v <= hi:
                raise ApiError(400, f"{label} must be {lo:g} to {hi:g}.")
            return v
        keep_ids = []
        try:
            if op == "cut":
                if len(objs) < 2:
                    raise ApiError(400, "Select the cutter first, then the objects to cut.")
                cutter, rest = objs[0], objs[1:]
                res = shapes.cut(self.object_strokes([cutter]), self.object_strokes(rest),
                                 "inside" if body.get("keep") == "inside" else "outside")
                keep_ids = [cutter["id"]]
            else:
                strokes = self.object_strokes(objs)
                if not strokes:
                    raise ApiError(400, "These objects have no lines.")
                if op == "close":
                    res = shapes.close_gaps(strokes, num("tolerance", 0.01, 20, "The gap", 1.0))
                elif op == "smooth":
                    res = shapes.smooth(strokes, int(num("passes", 1, 5, "Passes", 2)))
                elif op == "arcs":
                    res = shapes.fit_arcs(strokes, num("tolerance", 0.01, 5, "The tolerance", 0.1))
                elif op == "grid":
                    g = body.get("grid")
                    ok = (isinstance(g, list) and 2 <= len(g) <= 12 and all(isinstance(r, list) and len(r) == len(g[0])
                          and 2 <= len(r) <= 12 for r in g) and all(isinstance(p, list) and len(p) == 2 and
                          all(isinstance(v, (int, float)) and math.isfinite(v) for v in p) for r in g for p in r))
                    if not ok:
                        raise ApiError(400, "The deform grid must be 2 to 12 rows of 2 to 12 points.")
                    res = shapes.grid_warp(strokes, [[(float(x), float(y)) for x, y in r] for r in g])
                elif op == "set":
                    # node editing: the edited strokes (machine mm, per colour) replace the object's
                    new = body.get("strokes")
                    ok = (isinstance(new, list) and 1 <= len(new) <= 5000 and all(
                        isinstance(it, dict) and isinstance(it.get("pts"), list) and 2 <= len(it["pts"]) <= 20000 and
                        all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and
                            math.isfinite(v) and -1000 <= v <= 3000 for v in p) for p in it["pts"]) for it in new))
                    if not ok:
                        raise ApiError(400, "The edited lines are not valid.")
                    cols = {c for c, _ in strokes}
                    res = [((it.get("c") if it.get("c") in cols else strokes[0][0]),
                            [(float(x), float(y)) for x, y in it["pts"]]) for it in new]
                elif op in ("fill", "unfill"):
                    # fill: the closed lines get a fill colour (their own), which the plot hatches; unfill drops it
                    res = strokes
                    if op == "fill" and not any(len(s) > 3 and math.dist(s[0], s[-1]) <= shapes.CLOSE_TOL
                                                for _, s in strokes):
                        raise ApiError(400, "None of these lines is closed, so there is nothing to fill. "
                                            "Line tools, Close nearly closed lines first.")
                elif op == "hull":
                    res = shapes.hull(strokes)
                elif op == "fillet":
                    res = shapes.fillet(strokes, num("radius", 0.1, 50, "The radius", 2))
                elif op == "trim":
                    res = shapes.trim(strokes, num("x", -1000, 2000, "X"), num("y", -1000, 2000, "Y"),
                                      num("tolerance", 0.2, 20, "The distance", 3))
                elif op == "affine":
                    m = body.get("m")
                    if not (isinstance(m, list) and len(m) == 6 and all(isinstance(v, (int, float)) for v in m)):
                        raise ApiError(400, "The transform must be six numbers.")
                    res = shapes.affine(strokes, m)
                elif op == "warp":
                    q = body.get("quad")
                    if not (isinstance(q, list) and len(q) == 4 and all(isinstance(p, list) and len(p) == 2 for p in q)):
                        raise ApiError(400, "The warp needs four corners.")
                    res = shapes.warp(strokes, [(float(a), float(b)) for a, b in q])
                else:      # penwidth: closed shapes offset inward by half the pen's line width
                    w = num("width", 0.05, 10, "The pen width")
                    res = []
                    for c, s in strokes:
                        if len(s) > 3 and math.dist(s[0], s[-1]) <= shapes.CLOSE_TOL:
                            res += [(c, ln) for ln in shapes.run("offset", [[s]], -w / 2, "round")]
                        else:
                            res.append((c, s))
        except shapes.ShapeError as ex:
            raise ApiError(400, str(ex))
        res = [(c, s) for c, s in res if len(s) >= 2]
        if not res:
            raise ApiError(400, "Nothing is left.")
        groups = {}
        for c, s in res:
            groups.setdefault(c, []).append(s)
        text, b = (shapes.svg_filled if op == "fill" else shapes.svg_layers)(list(groups.items()))
        names = sorted({(self.lib.get(o["file"]) or {}).get("name", "").removesuffix(".svg") for o in objs if o["id"] not in keep_ids})
        e = self.lib.add_svg_text(text, f"{op}-{'-'.join(names)[:40]}.svg", kind="shape")
        self.ev.log("info", f"Line tool {op} on {len(objs)} object(s) -> {e['name']}.")
        return {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "x": round(b[0], 3), "y": round(b[1], 3),
                "w": round(b[2] - b[0], 3), "h": round(b[3] - b[1], 3),
                "replace": [o["id"] for o in objs if o["id"] not in keep_ids],
                "colors": sorted(groups)}

    def shape_op(self, body):
        """Union, subtract, intersect or offset of bed objects -> a new library drawing and where it sits."""
        import collections
        import plot_svg
        known = {e["id"] for e in self.lib.list()}
        try:
            objs = document.clean_objects(body.get("objects") or [], known)
        except document.DocError as ex:
            raise ApiError(400, str(ex))
        if not objs:
            raise ApiError(400, "Select the objects first.")
        sheets = [e["name"] for e in (self.lib.get(o["file"]) for o in objs) if e and e.get("kind") == "sheet"]
        if sheets:
            raise ApiError(400, f"{sheets[0]} is a test sheet: it plots exactly as made, so the shape and line tools "
                                "leave it alone. Leave it out of the selection.")
        op = body.get("op")
        if op in self.LINE_OPS:
            return self.line_op(op, objs, body)
        if op == "split":
            return {"ok": True, "pieces": self.split_objects(objs)}
        if op == "strokes":                 # for node editing: the lines of one object in bed millimetres
            if len(objs) != 1:
                raise ApiError(400, "Edit the nodes of one object at a time.")
            st = self.object_strokes(objs)
            if sum(len(s) for _, s in st) > 20000:
                raise ApiError(400, "Too many points to edit by hand (over 20000). Simplify it first.")
            return {"ok": True, "strokes": [{"c": c, "pts": [[round(x, 3), round(y, 3)] for x, y in s]} for c, s in st]}
        if op == "reverse":
            strokes = self.object_strokes(objs)
            if not strokes:
                raise ApiError(400, "These objects have no lines.")
            groups = {}
            for c, s in reversed(strokes):            # last stroke first, each drawn from its other end
                groups.setdefault(c, []).append(s[::-1])
            text, b = shapes.svg_layers(list(groups.items()))
            names = sorted({(self.lib.get(o["file"]) or {}).get("name", "").removesuffix(".svg") for o in objs})
            e = self.lib.add_svg_text(text, f"reversed-{'-'.join(names)[:40]}.svg", kind="shape")
            return {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "x": round(b[0], 3), "y": round(b[1], 3),
                    "w": round(b[2] - b[0], 3), "h": round(b[3] - b[1], 3)}
        if op == "join":
            try:
                tol = float(body.get("tolerance") or 0.2)
            except (TypeError, ValueError):
                tol = 0.2
            if not 0.01 <= tol <= 5:
                raise ApiError(400, "The join distance must be 0.01 to 5 mm.")
            (text, b), before, after = self.join_objects(objs, tol)
            names = sorted({(self.lib.get(o["file"]) or {}).get("name", "").removesuffix(".svg") for o in objs})
            e = self.lib.add_svg_text(text, f"joined-{'-'.join(names)[:40]}.svg", kind="shape")
            self.ev.log("info", f"Joined {len(objs)} object(s): {before} strokes became {after} -> {e['name']}.")
            return {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "x": round(b[0], 3), "y": round(b[1], 3),
                    "w": round(b[2] - b[0], 3), "h": round(b[3] - b[1], 3), "before": before, "after": after}
        try:
            distance = float(body.get("distance") or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "The offset must be a number of mm.")
        bed = self.s.bed()
        per, colors = [], collections.Counter()
        for o in objs:
            e, p = self._entry(o["file"])
            g = self.geometry(e, p)[0]
            tmp = self.plan_dir / f"shape_{secrets.token_hex(4)}.svg"
            try:
                tmp.write_bytes(document.compose([o], {o["file"]: (p, g)}, bed))
                polys, metas, _, _ = plot_svg.load_svg(str(tmp), True)
            finally:
                tmp.unlink(missing_ok=True)
            per.append(polys)
            colors.update(m.get("color") or "#000000" for m in metas)
        try:
            res = shapes.run(op, per, distance, body.get("join") or "round")
        except shapes.ShapeError as ex:
            raise ApiError(400, str(ex))
        text, b = shapes.svg(res, colors.most_common(1)[0][0] if colors else "#000000")
        names = sorted({(self.lib.get(o["file"]) or {}).get("name", "").removesuffix(".svg") for o in objs})
        e = self.lib.add_svg_text(text, f"{op}-{'-'.join(names)[:40]}.svg", kind="shape")
        self.ev.log("info", f"Shape {op} of {len(objs)} object(s) -> {e['name']}.")
        return {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "x": round(b[0], 3), "y": round(b[1], 3),
                "w": round(b[2] - b[0], 3), "h": round(b[3] - b[1], 3)}

    def preview_doc(self, body):
        raw = body["doc"]
        try:
            self.docs.put(raw.get("objects") or [], raw.get("settings"), name=raw.get("name"),
                          client=body.get("client"))
        except document.DocError as ex:
            raise ApiError(400, str(ex))
        v, objs, pid, layers, plan = self._resolve_doc(raw)
        ok, problems, pv, out = self._dry(plan, v, None)
        self._remember_pens(v["layers"])
        with self.plans_lock:
            self.plans[pid] = {"file": None, "doc": True, "ok": ok, "created": time.time()}
            for k in [k for k, p in self.plans.items() if time.time() - p["created"] > 24 * 3600]:
                self.plans.pop(k, None)
        names = ", ".join(sorted({(self.lib.get(o["file"]) or {}).get("name", "?") for o in objs}))
        self.ev.log("info" if ok else "warn",
                    f"Preview of the bed ({len(objs)} object{'s' if len(objs) != 1 else ''}: {names}): "
                    + ("ready to plot." if ok else "; ".join(problems)))
        for ln in out.splitlines():
            if ln.strip() and not ln.startswith("__"):
                self.ev.log("debug", ln, "plot")
        res = {"ok": ok, "plan_id": pid, "problems": problems, "warnings": plotjob.speed_warnings(v), "settings": v,
               "doc": True, "file": None,
               "bed_key": document.bed_key(objs, raw.get("settings")),
               "file_layers": layers, "objects": objs, "time_factor": self.runner.time_factor()}
        if pv:
            res.update(summary=_public_summary(pv["summary"]), placement=pv["placement"], order=pv["order"],
                       strokes=_thin(pv["strokes"]))
        return res

    def _dry(self, plan, v, g):
        req = plan / f"req_{secrets.token_hex(4)}"
        req.mkdir()
        pv_path = req / "preview.json"
        argv = plotjob.build_argv(self.s, plan / "drawing.svg", v, "dry", g, preview_json=pv_path,
                                  layers_json=plan / "layers.json", machine=plan / "machine.json", bed=self._bed(plan))
        try:
            rc, out = plotjob.run_dry(argv)
            pv = json.loads(pv_path.read_text()) if pv_path.exists() else None
        finally:
            shutil.rmtree(req, ignore_errors=True)
        ok = rc == 0 and pv is not None and not pv["summary"]["problems"]
        problems = [] if ok else plotjob.friendly_problems(pv["summary"] if pv else None, out)
        return ok, problems, pv, out

    def _bed(self, plan):
        return self.s.bed_from((plan / "machine.json").read_bytes())

    def preview(self, body):
        e, svg_path = self._entry(body.get("id"))
        v, g, pid, layers, plan = self._resolve(e, svg_path, body.get("settings"))
        ok, problems, pv, out = self._dry(plan, v, g)
        with self.plans_lock:
            self.plans[pid] = {"file": e["id"], "ok": ok, "created": time.time()}
            for k in [k for k, p in self.plans.items() if time.time() - p["created"] > 24 * 3600]:
                self.plans.pop(k, None)
        self.lib.update(e["id"], settings=v)
        self.ev.log("info" if ok else "warn",
                    f"Preview of {e['name']}: " + ("ready to plot." if ok else "; ".join(problems)))
        for ln in out.splitlines():
            if ln.strip() and not ln.startswith("__"):
                self.ev.log("debug", ln, "plot")
        res = {"ok": ok, "plan_id": pid, "problems": problems, "warnings": plotjob.speed_warnings(v), "settings": v,
               "file": e["id"],
               "file_layers": layers}
        if pv:
            res.update(summary=_public_summary(pv["summary"]), placement=pv["placement"], order=pv["order"],
                       strokes=_thin(pv["strokes"]))
        return res

    def start(self, body):
        mode = body.get("mode")
        if mode not in ("plot", "trace", "simulate"):
            raise ApiError(400, "mode must be plot, trace or simulate")
        sim = self.s.sim or mode == "simulate"
        if mode != "simulate" and not (body.get("confirm") or {}).get("parked") is True:
            raise ApiError(400, "Confirm that the carriage is parked by hand at the paper's top-left with the pen up.")
        pid = body.get("plan_id") or ""
        with self.plans_lock:
            plan = self.plans.get(pid)
        if not plan or plan.get("used"):
            raise ApiError(409, "This preview has expired or was already used. Preview again, then start.")
        if isinstance(body.get("doc"), dict):
            known = {x["id"]: x["name"] for x in self.lib.list()}
            objs = [o for o in body["doc"].get("objects") or [] if isinstance(o, dict) and o.get("file") in known]
            only = (body["doc"].get("settings") or {}).get("only") if isinstance(body["doc"].get("settings"), dict) else None
            if isinstance(only, list) and only:
                objs = [o for o in objs if o.get("id") in only]
            names = sorted({known[o["file"]] for o in objs})
            name = str(body["doc"].get("name") or "").strip()[:80] or (
                (names[0] + (f" x{len(objs)}" if len(objs) > 1 else "")) if len(names) == 1
                else f"{len(objs)} objects: {', '.join(names)}"[:120])
            e = {"id": None, "name": name}
            resolve = lambda: self._resolve_doc(body["doc"])  # noqa: E731
            try:
                bk = document.bed_key(document.clean_objects(body["doc"].get("objects") or [], set(known)),
                                      body["doc"].get("settings"))
            except document.DocError:
                bk = None
            st = body["doc"].get("settings") if isinstance(body["doc"].get("settings"), dict) else {}
            meta = {"doc": True, "file": None, "name": name, "plan_id": pid, "bed_key": bk,
                    "start_at": st.get("start_at") if isinstance(st.get("start_at"), int) else 0}
        else:
            e, svg_path = self._entry(body.get("id"))
            resolve = lambda: self._resolve(e, svg_path, body.get("settings"))  # noqa: E731
            meta = {"file": e["id"], "name": e["name"], "plan_id": pid}
        ok, job_id = self.runner.reserve("plot", mode, sim, meta)
        if not ok:
            raise ApiError(409, job_id)
        try:
            v, g, now_pid, _, plan = resolve()
            if meta.get("doc"):
                g = None
            if now_pid != pid:
                raise ApiError(409, "The drawing, its settings or the machine calibration changed since the preview. "
                                    "Preview again and re-check before starting.")
            if not sim:
                with self.hw_lock:
                    ready, msg = hardware.precheck(False)
                if not ready:
                    raise ApiError(409, "Plotter not ready: " + msg)
                self.ev.log("info", "Plotter answered: " + msg)
            ok2, problems, pv, out = self._dry(plan, v, g)   # fresh preflight on the staged inputs
            if not ok2:
                raise ApiError(409, "Preflight failed: " + "; ".join(problems), problems=problems)
            if mode != "simulate":
                with self.plans_lock:
                    self.plans[pid]["used"] = True
            argv = plotjob.build_argv(self.s, plan / "drawing.svg", v, "trace" if mode == "trace" else "plot", g,
                                      layers_json=plan / "layers.json", machine=plan / "machine.json",
                                      bed=self._bed(plan), sim=sim,
                                      sim_speed=(_sim_speed(body.get("sim_speed")) or self.s.sim_speed) if sim else None,
                                      control=True)
            word = None if sim else ("TRACE\n" if mode == "trace" else "PLOT\n")
            job_pv = {"order": pv["order"], "strokes": _thin(pv["strokes"]), "placement": pv["placement"],
                      "summary": _public_summary(pv["summary"]), "file": e["id"], "doc": bool(meta.get("doc")),
                      "plan_id": pid, "settings": {"layers": v["layers"]}}
            launched, msg = self.runner.launch(job_id, argv, word, preview=job_pv)
            if not launched:
                raise ApiError(409, msg)
            return {"ok": True, "job": job_id}
        except ApiError as ex:
            self.runner.cancel(job_id, ex.msg)
            raise
        except Exception as ex:  # noqa: BLE001
            self.runner.cancel(job_id, f"Server error before start: {ex!r}")
            raise

    def live_start(self, body):
        """Start a live writing session (tools/live_plot.py --control). Same gates as a plot: park confirmation,
        plotter precheck, one job at a time. The carriage must be parked at S (the paper's top-left)."""
        sim = self.s.sim or body.get("mode") == "simulate"
        if not sim and not (body.get("confirm") or {}).get("parked") is True:
            raise ApiError(400, "Confirm that the carriage is parked by hand at the paper's top-left with the pen up.")
        import fonts
        font = str(body.get("font") or "")[:80]
        writer = None
        if font.startswith("hand:") or not font:
            writer, font = (font[5:] or "default"), ""
        argv = [self.s.python, str(self.s.root / "tools" / "live_plot.py"), "--control",
                "--config", str(self.s.machine_path)]
        if font:
            if font not in fonts.families() and not fonts.find(font):
                raise ApiError(400, f"Unknown font {font!r}. Pick one from the list.")
            argv += ["--font", font]
        else:
            try:
                from handwriting import writers
            except RuntimeError as ex:
                raise ApiError(400, str(ex))
            if not writers.valid(writer):
                raise ApiError(400, "Unknown writer.")
            if not writers.count(writer):
                raise ApiError(400, f"{writer!r} has no handwriting samples yet. Write some with the handwriting-synth capture page, "
                                    "or choose a font.")
            argv += ["--writer", writer]
        try:
            xh = float(body.get("xheight", 4.5))
            speed = float(body.get("speed", 15))
            pressure = body.get("pressure")
            pressure = None if pressure in (None, "") else float(pressure)
        except (TypeError, ValueError):
            raise ApiError(400, "Size, speed and pressure must be numbers.")
        lim = LIMITS
        if not 2.0 <= xh <= 20.0:
            raise ApiError(400, "Letter height must be between 2 and 20 mm.")
        if not lim["speed"]["min"] <= speed <= lim["speed"]["max"]:
            raise ApiError(400, f"Speed must be between {lim['speed']['min']:g} and {lim['speed']['max']:g} mm/s.")
        if pressure is not None and not lim["pressure"]["min"] <= pressure <= lim["pressure"]["max"]:
            raise ApiError(400, f"Pressure must be between {lim['pressure']['min']:g} and {lim['pressure']['max']:g}.")
        sport = body.get("sport") is True
        if sport and (body.get("confirm") or {}).get("sport") is not True:
            raise ApiError(400, "Sport mode needs its own confirmation: it runs above the tested speed and can lose steps.")
        argv += ["--xheight", f"{xh:g}", "--speed", f"{speed:g}"]
        if sport:
            argv.append("--sport")
        if pressure is not None:
            argv += ["--pressure", f"{pressure:g}"]
        if sim:
            argv.append("--sim")
        elif hardware.PORT_CHOICE:
            argv += ["--port", hardware.PORT_CHOICE]
        ok, job_id = self.runner.reserve("live", "live", sim, {"font": font or f"handwriting ({writer})", "xheight": xh, "sport": sport})
        if not ok:
            raise ApiError(409, job_id)
        try:
            if not sim:
                with self.hw_lock:
                    ready, msg = hardware.precheck(False)
                if not ready:
                    raise ApiError(409, "Plotter not ready: " + msg)
                self.ev.log("info", "Plotter answered: " + msg)
            launched, msg = self.runner.launch(job_id, argv, None if sim else "WRITE" + chr(10))
            if not launched:
                raise ApiError(409, msg)
            return {"ok": True, "job": job_id}
        except ApiError as ex:
            self.runner.cancel(job_id, ex.msg)
            raise
        except Exception as ex:  # noqa: BLE001
            self.runner.cancel(job_id, f"Server error before start: {ex!r}")
            raise

    def machine_start(self, body):
        """Take manual control of the carriage (tools/jog.py --control). Same gates as a plot."""
        sim = self.s.sim or body.get("mode") == "simulate"
        if not sim and not (body.get("confirm") or {}).get("parked") is True:
            raise ApiError(400, "Confirm that the carriage is parked by hand at the paper's top-left with the pen up.")
        argv = [self.s.python, str(self.s.root / "tools" / "jog.py"), "--control", "--config", str(self.s.machine_path)]
        if sim:
            argv += ["--sim", "--sim-speed", f"{self.s.sim_speed:g}"]
        elif hardware.PORT_CHOICE:
            argv += ["--port", hardware.PORT_CHOICE]
        ok, job_id = self.runner.reserve("machine", "machine", sim, {"name": "machine control"})
        if not ok:
            raise ApiError(409, job_id)
        try:
            if not sim:
                with self.hw_lock:
                    ready, msg = hardware.precheck(False)
                if not ready:
                    raise ApiError(409, "Plotter not ready: " + msg)
                self.ev.log("info", "Plotter answered: " + msg)
            launched, msg = self.runner.launch(job_id, argv, None if sim else "TAKE" + chr(10))
            if not launched:
                raise ApiError(409, msg)
            return {"ok": True, "job": job_id}
        except ApiError as ex:
            self.runner.cancel(job_id, ex.msg)
            raise
        except Exception as ex:  # noqa: BLE001
            self.runner.cancel(job_id, f"Server error before start: {ex!r}")
            raise

    def run_tool(self, body):
        tool = tools.REGISTRY.get(body.get("tool"))
        if not tool:
            raise ApiError(404, "Unknown tool.")
        try:
            p = tools.clamp_tool_params(tool, body.get("params"), self.s.pens())
        except ValueError as ex:
            raise ApiError(400, str(ex))
        argv = tool.argv(self.s, p)
        if tool.kind == "machine":
            if not (body.get("confirm") or {}).get("ok") is True:
                raise ApiError(400, "Confirm the safety note before starting this tool.")
            ok, job_id = self.runner.reserve("pentest", "pentest", self.s.sim, {"tool": tool.id})
            if not ok:
                raise ApiError(409, job_id)
            try:
                if self.s.sim:
                    argv = argv + ["--sim"]
                else:
                    with self.hw_lock:
                        ready, msg = hardware.precheck(False)
                    if not ready:
                        raise ApiError(409, "Plotter not ready: " + msg)
                launched, msg = self.runner.launch(job_id, argv, "\n")
                if not launched:
                    raise ApiError(409, msg)
            except ApiError as ex:
                self.runner.cancel(job_id, ex.msg)
                raise
            return {"ok": True, "job": job_id}
        with TOOL_LOCK:
            rc, out = tools.run_sync(argv)
        for ln in out.splitlines():
            if ln.strip():
                self.ev.log("info" if rc == 0 else "error", ln, "tool")
        if rc != 0:
            raise ApiError(400, f"{tool.title} failed: " + (out.strip().splitlines() or ["no output"])[-1],
                           output=out)
        if tool.kind == "record":
            return {"ok": True, "output": out}
        path = tools.written_svg(out)
        if not path or not pathlib.Path(path).is_file():
            raise ApiError(500, "The sheet was generated but its SVG could not be found.", output=out)
        text = pathlib.Path(path).read_text(encoding="utf-8")
        pen = p.get("pen")
        entry = self.lib.add_svg_text(text, pathlib.Path(path).name, kind="sheet", pen=pen, test=tool.id,
                                      notes=out)
        return {"ok": True, "file": {k: entry.get(k) for k in ("id", "name", "kind", "pen", "test")}, "output": out}

    # ------------------------------------------------------------ prepare workspace
    def _prep_dry(self, run, fill, fill_angle):
        """Dry run of a prepared SVG at its natural size, at the canvas top-left. Cached next to the stage."""
        # Keyed by what decides the strokes: the stage, the fill, the machine and the planner itself.
        h = hashlib.sha256(self.s.machine_path.read_bytes())
        h.update(planner_hash().encode())
        memo = self.prep.dir / f"st_{run['key']}_dry_{fill:g}_{fill_angle:g}_{h.hexdigest()[:10]}.json"
        if memo.is_file():
            return json.loads(memo.read_text()), memo.with_suffix(".log").read_text() \
                if memo.with_suffix(".log").is_file() else ""
        v = {"speed": LIMITS["speed"]["default"], "travel_speed": LIMITS["travel_speed"]["default"], "x": None,
             "y": None, "w": None, "paper": None, "layers": [], "fill": fill, "fill_angle": fill_angle}
        tmp = self.prep.dir / f".dry_{secrets.token_hex(4)}.json"
        rc, out = plotjob.run_dry(plotjob.build_argv(self.s, run["svg"], v, "dry", None, preview_json=tmp))
        if not tmp.exists():
            tail = [ln for ln in out.strip().splitlines() if ln.strip() and not ln.startswith("__")]
            raise ApiError(400, "The result cannot be plotted: " + (tail[-1] if tail else f"exit code {rc}"),
                           output=out[-4000:])
        memo.with_suffix(".log").write_text(out, encoding="utf-8")
        pv = json.loads(tmp.read_text())
        tmp.replace(memo)
        return pv, out

    @staticmethod
    def _prep_fill(settings):
        s = settings if isinstance(settings, dict) else {}
        try:
            fill = float(s.get("fill") or 0)
            angle = float(s.get("fill_angle") if s.get("fill_angle") not in (None, "") else 45)
        except (TypeError, ValueError):
            raise ApiError(400, "Fill spacing and angle must be numbers.")
        if fill and not 0.2 <= fill <= 50:
            raise ApiError(400, "Fill spacing must be 0 (off) or between 0.2 and 50 mm.")
        if not 0 <= angle <= 180:
            raise ApiError(400, "Fill angle must be between 0 and 180 degrees.")
        return round(fill, 2), round(angle, 1)

    def _prep_run(self, recipe):
        try:
            return self.prep.run(recipe, self.s.bed()["canvas"])
        except prepare.PrepareError as ex:
            raise ApiError(400, str(ex))

    def _prep_task(self, recipe, fill, angle):
        """A slow method that is not made yet: run it and its dry run as a background task. -> task dict."""
        canvas = self.s.bed()["canvas"]
        try:
            step, _ = self.prep.method_cached(recipe, canvas)
            mp = self.prep.input_megapixels(recipe, canvas)
        except prepare.PrepareError as ex:
            raise ApiError(400, str(ex))
        src = self.prep.check(recipe)[0]
        name = src["name"] if src else step.title

        def work(report):
            run = self.prep.run(recipe, canvas, progress=lambda st, f: report(st, 0.92 * f))
            report("Planning the plot preview", 0.93)
            self._prep_dry(run, fill, angle)
            return {"key": run["key"]}

        return self.tasks.start("trace", f"Tracing {name}", work, key=self._prep_task_key(recipe, fill, angle), size=mp)

    def _prep_task_key(self, recipe, fill, angle):
        return "trace:" + hashlib.sha256(json.dumps([recipe, self.s.bed()["canvas"], fill, angle], sort_keys=True,
                                                    default=str).encode()).hexdigest()

    def prepare_run(self, body):
        """Run a recipe and dry-run the result: the same strokes and summary a Plot preview gets. A slow
        method that is not made yet answers {pending: task} and runs in the background; ask again when
        the task is done (it is then cached)."""
        t0 = time.time()
        recipe = body.get("recipe")
        fill, angle = self._prep_fill(body.get("settings"))
        try:
            step, made = self.prep.method_cached(recipe, self.s.bed()["canvas"])
        except prepare.PrepareError as ex:
            raise ApiError(400, str(ex))
        if step.slow and not made:
            running = self.tasks.find(self._prep_task_key(recipe, fill, angle))
            if running:
                return {"ok": True, "pending": True, "task": running, "method": step.id}
            if not body.get("go"):
                try:
                    mp = self.prep.input_megapixels(recipe, self.s.bed()["canvas"])
                except prepare.PrepareError as ex:
                    raise ApiError(400, str(ex))
                return {"ok": True, "pending": False, "needs_trace": True, "method": step.id,
                        "estimate": self.tasks.estimate("trace", mp), "megapixels": mp and round(mp, 2)}
            return {"ok": True, "pending": True, "task": self._prep_task(recipe, fill, angle), "method": step.id}
        run = self._prep_run(recipe)
        pv, out = self._prep_dry(run, fill, angle)
        sm = pv["summary"]
        b, c = sm["bbox_machine_mm"], sm["canvas_mm"]
        fit = min(1.0, (c[2] - c[0]) / max(b[2] - b[0], 1e-6), (c[3] - c[1]) / max(b[3] - b[1], 1e-6))
        # Too big for the canvas is not a problem here: Plot shrinks it to fit (the fit value says by how much).
        problems = [p for p in plotjob.friendly_problems(sm, out) if not p.startswith("The drawing runs")
                    and "canvas" not in p and "travel is" not in p and "edge margin" not in p] if sm["problems"] else []
        src = run["source"]
        return {"ok": True, "key": run["key"], "method": run["method"].id,
                "image": run["image"].name[3:-4] if run["image"] else None, "mask": run["mask"].name[3:-9] if run["mask"] else None,
                "source": src and {k: v for k, v in src.items() if k != "path"},
                "stats": run["stats"], "summary": _public_summary(sm), "placement": pv["placement"],
                "order": pv["order"], "strokes": _thin(pv["strokes"]), "fit": round(fit, 4),
                "layers": [{k: ly.get(k) for k in ("key", "name", "color", "strokes")} for ly in sm["layers"]],
                "problems": problems, "fill": fill, "fill_angle": angle, "ms": round((time.time() - t0) * 1000)}

    def prepare_shading(self, body):
        """Shading candidates of a technical-drawing recipe for the live preview: from the cache, else
        measured as a background task ({pending: task}; ask again when it is done)."""
        recipe = body.get("recipe")
        canvas = self.s.bed()["canvas"]
        try:
            pub = self.prep.shading(recipe, canvas, cached_only=True)
            if pub is not None:
                return {"ok": True, "pending": False, "shading": pub}
            mp = self.prep.input_megapixels(recipe, canvas)
            src = self.prep.check(recipe)[0]
        except prepare.PrepareError as ex:
            raise ApiError(400, str(ex))

        def work(report):
            self.prep.shading(recipe, canvas, progress=report)
            return True

        key = "shading:" + hashlib.sha256(json.dumps([recipe["source"], [s for s in recipe["steps"]
                                                       if prepare.STEPS.get(s.get("step")) is not None
                                                       and prepare.STEPS[s["step"]].stage != "clean"], canvas],
                                                      sort_keys=True, default=str).encode()).hexdigest()
        t = self.tasks.start("shading", f"Measuring the shading in {src['name'] if src else 'the image'}", work,
                             key=key, size=mp)
        return {"ok": True, "pending": True, "task": t}

    def _prep_layers(self, raw, keys):
        """Pens chosen in Prepare -> layer settings, with the pen's own speed, pressure and dwell when it has them."""
        pens = self.s.pens()
        given = {ly.get("key"): ly for ly in raw or [] if isinstance(ly, dict)}
        out = []
        for k in keys:
            g = given.get(k) or {}
            pen = g.get("pen") if g.get("pen") in pens else None
            prof = pens.get(pen) or {}
            sp, pr = prof.get("speed"), prof.get("pressure")
            ok_sp = isinstance(sp, (int, float)) and LIMITS["speed"]["min"] <= sp <= LIMITS["speed"]["max"]
            ok_pr = isinstance(pr, (int, float)) and 0 <= pr <= LIMITS["pressure"]["max"]
            dw = prof.get("dwell_ms")
            ent = {"key": k, "enabled": g.get("enabled", True) is not False, "pen": pen,
                   "speed": sp if ok_sp else None, "pressure": pr if ok_pr else None}
            if isinstance(dw, (int, float)) and 0 < dw <= 3000:
                ent["dwell_ms"] = dw
            out.append(ent)
        return out

    def prepare_save(self, body):
        recipe = body.get("recipe")
        run = self._prep_run(recipe)
        fill, angle = self._prep_fill(body.get("settings"))
        pv, _ = self._prep_dry(run, fill, angle)
        layers = self._prep_layers((body.get("settings") or {}).get("layers"),
                                   [ly["key"] for ly in pv["summary"]["layers"]])
        src = run["source"]
        name = str(body.get("name") or "").strip()[:80] or \
            (pathlib.Path(src["name"]).stem if src else run["method"].title.lower().replace(" ", "-"))
        old = None
        if body.get("replace"):
            old = self.lib.get(body["replace"])
            if not old:
                raise ApiError(404, "The drawing to update is no longer in the library. Save it as new.")
            job = self.runner.snapshot()["job"]
            if self.runner.busy() and job and job["meta"].get("file") == old["id"]:
                raise ApiError(409, "That drawing is being plotted. Save it as new, or wait for the job.")
        settings = {"fill": fill, "fill_angle": angle, "layers": layers}
        if old:      # keep where it sits on the bed and how fast it plots; the size follows the new drawing
            prev = old.get("settings") or {}
            settings.update({k: prev[k] for k in ("x", "y", "speed", "travel_speed", "paper") if k in prev})
        steps = [s for s in recipe.get("steps") or [] if isinstance(s, dict)]
        try:
            e = self.lib.add_prepared(run["svg"], name, src and src["path"], method=run["method"].id,
                                      recipe={"steps": steps}, settings=settings, replace=old)
        except ValueError as ex:
            raise ApiError(400, str(ex))
        self.ev.log("info", f"{'Updated' if old else 'Added'} {e['name']} from Prepare ({run['method'].title}).")
        return {"ok": True, "file": {k: e.get(k) for k in ("id", "name", "kind")}}

    def prepare_from(self, body):
        """Reopen a library drawing in Prepare: its source, its recipe (or one rebuilt from it), its plot settings."""
        e = self.lib.get(body.get("file") or "")
        if not e:
            raise ApiError(404, "That file is no longer in the library.")
        recipe = prepare.recipe_for_entry(e)
        method = next((prepare.STEPS[s["step"]] for s in (recipe or {}).get("steps", [])
                       if s.get("step") in prepare.STEPS and prepare.STEPS[s["step"]].stage == "method"), None)
        src = None
        if not (method and not method.accepts):          # a generator (text) needs no source
            orig, svg = self.lib.path(e, "orig"), self.lib.path(e, "svg")
            use = orig if orig and orig != svg and prepare.kind_of(orig.suffix) else svg
            if not use:
                raise ApiError(404, "The drawing's files are missing on the server.")
            try:
                # The type comes from the file itself: a prepared drawing is named .svg but its source may be a photo.
                src = self.prep.add_source(use.read_bytes(), pathlib.Path(e.get("name") or use.name).stem + use.suffix)
            except prepare.PrepareError as ex:
                raise ApiError(400, str(ex))
            if method and src["kind"] not in method.accepts:
                recipe = None                            # e.g. a test sheet: reopen the SVG itself
        if not recipe or not method:
            recipe = {"steps": [{"step": prepare.DEFAULT_METHOD[src["kind"]], "params": {}}]}
        st = e.get("settings") or {}
        return {"ok": True, "source": src and {k: v for k, v in src.items() if k != "path"}, "recipe": recipe,
                "name": pathlib.Path(e.get("name") or "drawing").stem, "replace": e["id"],
                "settings": {"fill": st.get("fill") or 0, "fill_angle": st.get("fill_angle", 45),
                             "layers": [{"key": ly.get("key"), "pen": ly.get("pen")} for ly in st.get("layers") or []]}}


_PLANNER = {}


def planner_hash():
    """Short hash of plot_svg.py, re-read when the file changes."""
    p = pathlib.Path(__file__).resolve().parent.parent / "plot_svg.py"
    st = p.stat()
    key = (st.st_mtime_ns, st.st_size)
    if _PLANNER.get("key") != key:
        _PLANNER.update(key=key, hash=hashlib.sha256(p.read_bytes()).hexdigest()[:12])
    return _PLANNER["hash"]


def _sim_speed(v):
    """A simulated run's real-time factor asked for by the page: 1 to 1000, else None (the server's default)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 1000 else None


def _thin(strokes):
    total = sum(len(s["pts"]) for s in strokes)
    if total <= MAX_DISPLAY_POINTS:
        return strokes
    k = -(-total // MAX_DISPLAY_POINTS)
    return [dict(s, pts=s["pts"][::k] + ([s["pts"][-1]] if (len(s["pts"]) - 1) % k else [])) for s in strokes]


def _public_summary(sm):
    keep = ("strokes", "points", "est_seconds", "bbox_machine_mm", "paper_mm", "canvas_mm", "layers", "swaps",
            "optimized", "file_order", "duplicates_dropped", "start_at", "total_in_order", "fill_inks",
            "drawing_mm", "travel_mm", "lifts", "doc_mm", "scale", "start_mm", "speed", "travel_speed", "problems",
            "copies", "est_seconds_all", "finish")
    return {k: sm.get(k) for k in keep}


# ---------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PlotterWeb/2.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/events"):
            self.app.ev.log("debug", f"{self.address_string()} {fmt % args}", "http")

    # ---- helpers
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
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def err(self, code, msg, **extra):
        out = {"ok": False, "error": str(msg)}
        out.update(extra)
        self.send(code, out)

    def raw_body(self, limit=MAX_UPLOAD):
        # read once per request (the headers object is new for each): a second read would wait forever
        if getattr(self, "_body_for", None) is self.headers:
            return self._body
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n > limit + 4096:
            raise ApiError(413, f"The upload is larger than {limit // (1024 * 1024)} MB.")
        self._body = self.rfile.read(n) if n > 0 else b""
        self._body_for = self.headers
        return self._body

    def json_body(self):
        try:
            data = json.loads(self.raw_body().decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "The request body is not valid JSON.")

    def authed(self):
        if not self.app.auth.enabled:
            return True
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
            return self.app.auth.check_session(c[COOKIE].value)
        except (KeyError, http.cookies.CookieError):
            return False

    def query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    # ---- dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path
        try:
            if method == "GET" and (path in STATIC or path.startswith("/js/")):
                return self.static(path)
            table = ROUTES_GET if method == "GET" else ROUTES_POST
            route = table.get(path)
            if not route:
                raise ApiError(404, "Not found.")
            fn, needs_auth = route
            if needs_auth and not self.authed():
                raise ApiError(401, "Not logged in.")
            return fn(self)
        except ApiError as ex:
            self.close_connection = True   # the request body may be unread; never let it prefix the next request
            return self.err(ex.code, ex.msg, **ex.extra)
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as ex:  # noqa: BLE001
            self.close_connection = True
            self.app.ev.log("error", f"Server error on {method} {path}: {ex!r}")
            return self.err(500, f"Server error: {ex}. Details are in the console (Debug).")

    def static(self, path):
        rel = STATIC.get(path) or path.lstrip("/")
        p = (WEB / rel).resolve()
        if WEB.resolve() not in p.parents or not p.is_file() or p.suffix not in (".html", ".css", ".js", ".svg"):
            raise ApiError(404, "Not found.")
        ctype = {".js": "text/javascript", ".css": "text/css", ".html": "text/html",
                 ".svg": "image/svg+xml"}[p.suffix] + "; charset=utf-8"
        csp = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if p.name == "turtle-worker.js":     # runs pasted code: may evaluate it, may not reach the network
            csp = "default-src 'none'; script-src 'unsafe-eval'"
        self.send(200, p.read_bytes(), ctype, {"Content-Security-Policy": csp})

    # ---- routes: session
    def r_me(self):
        self.send(200, self.app.me(self.authed()))

    def r_login(self):
        if not self.app.auth.enabled:
            return self.send(200, {"ok": True})
        ok, msg, code = self.app.auth.login(self.client_address[0], self.json_body().get("password"))
        if not ok:
            raise ApiError(code, msg)
        cookie = (f"{COOKIE}={self.app.auth.make_session()}; Path=/; HttpOnly; SameSite=Strict; "
                  f"Max-Age={SESSION_AGE}")
        self.send(200, {"ok": True}, headers={"Set-Cookie": cookie})

    def r_logout(self):
        self.send(200, {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})

    # ---- routes: state and events
    def r_state(self):
        self.send(200, self.app.state())

    def r_log_download(self):
        evs, _ = self.app.ev.log_since(0)
        text = "\n".join(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e.get('t', 0)))}  "
                         f"{e.get('level', '').upper():5}  {e.get('src', '')}: {e.get('text', '')}" for e in evs)
        self.send(200, text + "\n", "text/plain; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="plotter-log-{time.strftime("%Y%m%d-%H%M")}.txt"'})

    def r_about(self):
        self.send(200, self.app.about())

    def r_diagnostics(self):
        evs, _ = self.app.ev.log_since(0)
        cfg = {}
        try:
            cfg = json.loads(self.app.s.machine_path.read_text())
        except (OSError, ValueError):
            pass
        bundle = {"about": self.app.about(), "machine": cfg, "pens": self.app.s.pens(),
                  "plotter": hardware.LAST, "job": self.app.runner.snapshot(), "history": self.app.runner.history(50),
                  "lifetime": self.app.runner.lifetime(), "log": evs[-500:]}
        self.send(200, json.dumps(bundle, indent=1, default=str), "application/json; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="plotter-diagnostics-{time.strftime("%Y%m%d-%H%M")}.json"'})

    def r_tags(self):
        b = self.json_body()
        tags = b.get("tags") if isinstance(b.get("tags"), list) else []
        tags = sorted({re.sub(r"\s+", " ", str(t)).strip()[:30] for t in tags if str(t).strip()})[:12]
        e = self.app.lib.update(b.get("id", ""), tags=tags)
        if not e:
            raise ApiError(404, "Unknown file.")
        self.send(200, {"ok": True, "tags": tags})

    def r_clipart(self):
        """The selected objects as one library drawing (clip art), as they sit, to drag back in later."""
        known = {e["id"] for e in self.app.lib.list()}
        body = self.json_body()
        try:
            objs = document.clean_objects(body.get("objects") or [], known)
        except document.DocError as ex:
            raise ApiError(400, str(ex))
        if not objs:
            raise ApiError(400, "Select the objects first.")
        strokes = self.app.object_strokes(objs)
        if not strokes:
            raise ApiError(400, "These objects have no lines.")
        groups = {}
        for c, s in strokes:
            groups.setdefault(c, []).append(s)
        text, b = shapes.svg_layers(list(groups.items()))
        name = re.sub(r"[^A-Za-z0-9 _-]+", "", str(body.get("name") or "clip art"))[:40] or "clip art"
        e = self.app.lib.add_svg_text(text, f"{name}.svg", kind="clipart", tags=["clip art"])
        self.app.ev.log("info", f"Saved {len(objs)} object(s) as clip art {e['name']}.")
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}})

    def r_log(self):
        try:
            since = int(self.query().get("since", 0))
        except ValueError:
            since = 0
        evs, seq = self.app.ev.log_since(since)
        self.send(200, {"epoch": self.app.ev.epoch, "seq": seq, "events": evs})

    def r_events(self):
        ev = self.app.ev
        q = self.query()
        since, epoch = q.get("since", "0"), q.get("epoch", "")
        last = self.headers.get("Last-Event-ID")
        if last and ":" in last:
            epoch, since = last.split(":", 1)
        try:
            cursor = int(since)
        except ValueError:
            cursor = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(obj, eid=None):
            data = (f"id: {ev.epoch}:{eid}\n" if eid is not None else "") + "data: " + json.dumps(obj) + "\n\n"
            self.wfile.write(data.encode())

        try:
            self.wfile.write(b"retry: 2000\n\n")
            if epoch != ev.epoch or cursor > ev.seq:
                write({"type": "reset", "epoch": ev.epoch, "seq": ev.seq}, ev.seq)
                cursor = ev.seq
            self.wfile.flush()
            while not self.server.stopping:
                evs, cursor_new, reset = ev.wait(cursor, 15.0)
                if reset:
                    write({"type": "reset", "epoch": ev.epoch, "seq": cursor_new}, cursor_new)
                for e in evs:
                    write(e, e["seq"])
                if not evs and not reset:
                    write({"type": "ping", "t": time.time(), "seq": ev.seq})
                cursor = cursor_new
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    # ---- routes: files
    def r_files(self):
        self.send(200, {"files": self.app.lib.list(), "rev": self.app.lib.rev})

    def _file_out(self, field, ctype, inline):
        e = self.app.lib.get(self.query().get("id", ""), trashed=None)     # the trash shows thumbnails too
        if not e:
            raise ApiError(404, "Unknown file.")
        p = self.app.lib.path(e, field)
        if not p:
            raise ApiError(404, "File missing.")
        ctype = ctype or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"}
        if not inline:
            headers["Content-Disposition"] = f'attachment; filename="{p.name}"'
        self.send(200, p.read_bytes(), ctype, headers)

    def r_file_svg(self):
        self._file_out("svg", "image/svg+xml", True)

    def r_file_orig(self):
        self._file_out("orig", None, False)

    def r_file_mask(self):
        self._file_out("mask", "image/png", True)

    def _upload_meta(self):
        name = urllib.parse.unquote(self.headers.get("X-Filename", ""))
        try:
            params = json.loads(urllib.parse.unquote(self.headers.get("X-Params", "") or "{}"))
        except ValueError:
            params = {}
        return name, params

    def r_upload(self):
        name, params = self._upload_meta()
        raw = self.raw_body()
        try:
            e = self.app.lib.upload(raw, name, params, self.app.s.bed()["canvas"])
        except ValueError as ex:
            raise ApiError(400, str(ex))
        self.app.ev.log("info", f"Added {e['name']} to the library.")
        self.send(200, {"ok": True, "file": {k: e.get(k) for k in ("id", "name", "kind")}})

    def r_convert_preview(self):
        name, params = self._upload_meta()
        try:
            out = self.app.lib.convert_preview(self.raw_body(), name, params, self.app.s.bed()["canvas"])
        except ValueError as ex:
            raise ApiError(400, str(ex))
        self.send(200, dict(out, ok=True))

    def r_delete(self):
        """Delete = move to the trash. Refused while it is being plotted."""
        ident = self.json_body().get("id", "")
        job = self.app.runner.snapshot()["job"]
        if self.app.runner.busy() and job and (job["meta"].get("file") == ident or job["meta"].get("doc")):
            raise ApiError(409, "A plot is running. Delete drawings when it has ended.")
        e = self.app.lib.trash(ident)
        if not e:
            raise ApiError(404, "Unknown file.")
        self.app.ev.log("info", f"Moved {e['name']} to the trash.")
        self.send(200, {"ok": True, "trashed": e["trashed"]})

    def r_trash(self):
        self.app.lib.purge_old()
        self.send(200, {"files": self.app.lib.list(trashed=True)})

    def r_restore(self):
        e = self.app.lib.restore(self.json_body().get("id", ""))
        if not e:
            raise ApiError(404, "Not in the trash.")
        self.app.ev.log("info", f"Restored {e['name']} from the trash.")
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}})

    def r_purge(self):
        b = self.json_body()
        ids = [e["id"] for e in self.app.lib.list(trashed=True)] if b.get("all") else [b.get("id", "")]
        n = sum(1 for i in ids if self.app.lib.get(i, trashed=True) and self.app.lib.delete(i))
        if not b.get("all") and not n:
            raise ApiError(404, "Not in the trash.")
        self.app.ev.log("info", f"Deleted {n} drawing{'s' if n != 1 else ''} for good.")
        self.send(200, {"ok": True, "deleted": n})

    def r_replace(self):
        ident = self.query().get("id", "")
        if self.app.runner.busy():
            raise ApiError(409, "A job is running. Replace the drawing when it has ended.")
        try:
            e = self.app.lib.replace_svg(ident, self.raw_body())
        except ValueError as ex:
            raise ApiError(400, str(ex))
        self.app.ev.log("info", f"Replaced the drawing of {e['name']}.")
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}})

    def r_text_art(self):
        from svg_vector import text_art_svg
        d = self.json_body()
        effect = d.get("effect") if d.get("effect") in ("plain", "arc", "wave", "rise", "circle", "twist") else "arc"
        try:
            size = max(5.0, min(150.0, float(d.get("size", 20))))
            bend = max(0.0, min(100.0, float(d.get("bend", 40))))
            text = str(d.get("text", ""))[:60]
            if not text.strip():
                raise ApiError(400, "Type some text first.")
            svg, stats = text_art_svg(text, size, str(d.get("font", "DejaVu Sans"))[:60],
                                      "bold" if d.get("bold") else "normal", "normal", effect, bend)
        except ApiError:
            raise
        except Exception as ex:  # noqa: BLE001
            raise ApiError(400, f"Text art failed: {ex}")
        if d.get("add"):
            e = self.app.lib.add_svg_text(svg, f"text-{text[:20]}.svg", kind="text")
            return self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}})
        self.send(200, {"ok": True, "svg": svg, "stats": stats})

    def r_fonts(self):
        import fonts
        try:
            from handwriting import writers
            ws = writers.list_writers()
        except RuntimeError:                      # no handwriting-synth project on this machine: fonts only
            ws = []
        self.send(200, {"fonts": fonts.families(), "bundled": sorted({f["family"] for f in fonts.bundled()}),
                        "writers": ws, "hand_samples": sum(w["samples"] for w in ws)})

    def r_text_page(self):
        """Paragraphs of text in a standard font, wrapped to the canvas, as a library drawing."""
        from handwriting.textpage import page_svg
        d = self.json_body()
        try:
            xh = max(2.0, min(20.0, float(d.get("xheight", 4.5))))
            text = str(d.get("text", ""))
            c = self.app.s.bed()["canvas"]
            svg, stats = page_svg(text, str(d.get("font", "Comic Neue"))[:60], xh, c[2] - c[0],
                                  bold=bool(d.get("bold")), italic=bool(d.get("italic")))
        except Exception as ex:  # noqa: BLE001
            raise ApiError(400, f"Text page failed: {ex}")
        if d.get("add"):
            first = " ".join(text.split())[:20] or "text"
            e = self.app.lib.add_svg_text(svg, f"page-{first}.svg", kind="text")
            return self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "stats": stats})
        self.send(200, {"ok": True, "svg": svg, "stats": stats})

    def r_text_single(self):
        """Single-line (Hershey) text as a library drawing, for the bed's text tool."""
        import hershey
        d = self.json_body()
        try:
            xh = float(d.get("xheight", 4.5))
            if not 1.0 <= xh <= 40.0:
                raise ValueError("Letter height must be between 1 and 40 mm.")
            text = str(d.get("text", ""))[:4000]
            if not text.strip():
                raise ValueError("Type some text first.")
            align = d.get("align") if d.get("align") in ("left", "center", "right") else "left"
            width = d.get("width")
            width = float(width) if width else None
            tracking = float(d.get("tracking") or 0)
            if not -2 <= tracking <= 20:
                raise ValueError("Letter spacing must be -2 to 20 mm.")
            fit = float(d.get("fit") or 0) or None
            if fit is not None and not 2 <= fit <= 1000:
                raise ValueError("Fit to a width of 2 to 1000 mm.")
            font_ = str(d.get("font") or "Hershey Sans")
            svg, stats = hershey.svg(text, font_, xh, width, xh * 3.0, align, tracking=tracking, fit=fit)
        except (TypeError, ValueError, KeyError) as ex:
            raise ApiError(400, str(ex))
        params = {"text": text, "font": font_, "xheight": xh, "align": align, "tracking": tracking, "fit": fit}
        first = " ".join(text.split())[:20] or "text"
        if d.get("replace"):                 # edit in place: same drawing, new letters; bed objects keep their place
            e = self.app.lib.get(d["replace"])
            if not e or e.get("kind") != "text":
                raise ApiError(404, "That text is no longer in the library.")
            self.app.lib.replace_svg(e["id"], svg.encode())
            self.app.lib.update(e["id"], text_params=params)
        else:
            e = self.app.lib.add_svg_text(svg, f"text-{first}.svg", kind="text", text_params=params)
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "stats": stats})

    def r_text_path(self):
        """Single-line text along a path (machine mm), for the Line tools."""
        import hershey
        d = self.json_body()
        try:
            xh = float(d.get("xheight", 4.5))
            if not 1.0 <= xh <= 40.0:
                raise ValueError("Letter height must be between 1 and 40 mm.")
            pts = [(float(x), float(y)) for x, y in (d.get("pts") or [])][:5000]
            if len(pts) < 2:
                raise ValueError("The path needs at least two points.")
            text = str(d.get("text", ""))[:500]
            polys = hershey.along_path(text, pts, str(d.get("font") or "Hershey Sans"), xh, float(d.get("tracking") or 0))
        except (TypeError, ValueError, KeyError) as ex:
            raise ApiError(400, str(ex))
        text_, b = shapes.svg_layers([("#000000", polys)])
        e = self.app.lib.add_svg_text(text_, f"path-text-{' '.join(text.split())[:16]}.svg", kind="shape")
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "x": round(b[0], 3), "y": round(b[1], 3)})

    def r_qr(self):
        import qrgen
        d = self.json_body()
        try:
            module = float(d.get("module", 1.0))
            pen = float(d.get("pen", 0.4))
            if not 0.3 <= module <= 10 or not 0.1 <= pen <= module:
                raise ValueError("Module size 0.3 to 10 mm, pen width 0.1 mm up to the module size.")
            text = str(d.get("text") or "")
            if not text:
                raise ValueError("Type what the code should hold.")
            if d.get("kind") == "code128":
                svg, size = qrgen.barcode_svg(text, module / 2.5, max(6.0, module * 12), pen)
            else:
                svg, size = qrgen.svg(text, module, pen)
        except ValueError as ex:
            raise ApiError(400, str(ex))
        prefix = "barcode" if d.get("kind") == "code128" else "qr"
        e = self.app.lib.add_svg_text(svg, f"{prefix}-{' '.join(text.split())[:20]}.svg", kind="shape")
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "modules": size})

    def r_shape_new(self):
        """A rectangle, ellipse, polygon, star or clicked line as a library drawing, for the bed's Shape tool."""
        body = self.json_body()
        try:
            svg, name, at = shapes.make_shape(body)
        except ValueError as ex:
            raise ApiError(400, str(ex))
        # the parameters stay with the drawing, so Edit shape can open them again (not for clicked lines)
        params = None if body.get("kind") == "poly" else {k: body.get(k) for k in ("kind", "w", "h", "r", "n", "inner")
                                                          if isinstance(body.get(k), (int, float, str))}
        if body.get("replace"):
            old = self.app.lib.get(str(body["replace"]))
            if not old or not old.get("shape_params"):
                raise ApiError(400, "That drawing is not an editable shape.")
            try:
                self.app.lib.replace_svg(old["id"], svg.encode())
            except ValueError as ex:
                raise ApiError(400, str(ex))
            e = self.app.lib.update(old["id"], shape_params=params)
            self.app.ev.emit("files")
            return self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "at": at, "replaced": True})
        e = self.app.lib.add_svg_text(svg, f"{name}.svg", kind="shape", shape_params=params)
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "at": at})

    # ---- routes: catalogue
    def r_converters(self):
        self.send(200, {"converters": [c.public() for c in converters.REGISTRY.values()]})

    def r_tools(self):
        self.send(200, {"tools": [t.public() for t in tools.REGISTRY.values()]})

    def r_pens(self):
        self.send(200, {"pens": self.app.s.pens()})

    # ---- routes: prepare
    def r_prep_steps(self):
        self.send(200, {"steps": prepare.public_steps(),
                        "kinds": {k: list(v) for k, v in prepare.KINDS.items()}, "max_bytes": prepare.MAX_SOURCE})

    def r_prep_source(self):
        name, _ = self._upload_meta()
        try:
            src = self.app.prep.add_source(self.raw_body(prepare.MAX_SOURCE), name)   # scans of drawings are large
        except prepare.PrepareError as ex:
            raise ApiError(400, str(ex))
        self.send(200, {"ok": True, "source": {k: v for k, v in src.items() if k != "path"}})

    def r_prep_from(self):
        self.send(200, self.app.prepare_from(self.json_body()))

    def r_prep_run(self):
        self.send(200, self.app.prepare_run(self.json_body()))

    def r_prep_shading(self):
        self.send(200, self.app.prepare_shading(self.json_body()))

    def r_tasks(self):
        self.send(200, {"tasks": self.app.tasks.list()})

    def r_task_cancel(self):
        tid = str(self.json_body().get("id") or "")
        if not self.app.tasks.cancel(tid):
            raise ApiError(409, "That task is not running or waiting any more.")
        self.send(200, {"ok": True, "task": self.app.tasks.get(tid)})

    def r_prep_save(self):
        self.send(200, self.app.prepare_save(self.json_body()))

    def r_presets(self):
        self.send(200, {"presets": self.app.presets.list()})

    def r_preset_put(self):
        try:
            p = self.app.presets.put(self.json_body())
        except prepare.PrepareError as ex:
            raise ApiError(400, str(ex))
        self.app.ev.log("info", f"Saved the Prepare preset {p['name']!r}.")
        self.send(200, {"ok": True, "preset": p, "presets": self.app.presets.list()})

    def r_preset_delete(self):
        try:
            self.app.presets.delete(str(self.json_body().get("name") or ""))
        except prepare.PrepareError as ex:
            raise ApiError(404, str(ex))
        self.send(200, {"ok": True, "presets": self.app.presets.list()})

    def r_prep_file(self):
        """Stage results (adjusted image, mask, SVG) and source originals, for the previews."""
        q = self.query()
        kind = q.get("kind", "")
        sandbox = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                   "Cache-Control": "private, max-age=86400"}
        if kind == "source":
            src = self.app.prep.source(q.get("id", ""))
            if not src or src["kind"] not in ("image", "svg"):
                raise ApiError(404, "No such source image.")
            ctype = "image/svg+xml" if src["kind"] == "svg" else (mimetypes.guess_type(src["path"].name)[0]
                                                                  or "application/octet-stream")
            return self.send(200, src["path"].read_bytes(), ctype, sandbox)
        suffix = {"png": ".png", "mask": "_mask.png", "svg": ".svg"}.get(kind)
        p = suffix and self.app.prep.stage_file(q.get("key", ""), suffix)
        if not p:
            raise ApiError(404, "That preview has expired. Run the recipe again.")
        ctype = "image/svg+xml" if kind == "svg" else "image/png"
        headers = dict(sandbox)
        if q.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="prepared{".svg" if kind == "svg" else "-image.png"}"'
        self.send(200, p.read_bytes(), ctype, headers)

    # ---- routes: plotting
    def r_preview(self):
        body = self.json_body()
        self.send(200, self.app.preview_doc(body) if isinstance(body.get("doc"), dict) else self.app.preview(body))

    def r_shapes(self):
        self.send(200, self.app.shape_op(self.json_body()))

    def r_export_doc(self):
        text, name = self.app.export_doc(self.json_body())
        self.send(200, text, "image/svg+xml; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="{name}"'})

    def r_rename(self):
        self.send(200, self.app.rename_file(self.json_body()))

    def r_natural(self):
        self.send(200, self.app.natural(self.query().get("id", "")))

    # ---- routes: the bed document and saved projects
    def r_doc(self):
        self.send(200, {"doc": self.app.docs.get(), "projects": self.app.docs.projects()})

    def r_doc_put(self):
        b = self.json_body()
        try:
            d = self.app.docs.put(b.get("objects") or [], b.get("settings"), name=b.get("name"), client=b.get("client"))
        except document.DocError as ex:
            raise ApiError(400, str(ex))
        self.send(200, {"doc": d})

    def r_project_save(self):
        try:
            body = self.json_body()
            slug = self.app.docs.save_project(body.get("name"), body.get("template") is True)
        except document.DocError as ex:
            raise ApiError(400, str(ex))
        self.send(200, {"slug": slug, "projects": self.app.docs.projects(), "doc": self.app.docs.get()})

    def r_project_open(self):
        try:
            d, missing = self.app.docs.open_project(self.json_body().get("slug"))
        except document.DocError as ex:
            raise ApiError(400, str(ex))
        self.send(200, {"doc": d, "missing": missing})

    def r_project_delete(self):
        try:
            self.app.docs.delete_project(self.json_body().get("slug"))
        except document.DocError as ex:
            raise ApiError(400, str(ex))
        self.send(200, {"projects": self.app.docs.projects()})

    def r_start(self):
        self.send(200, self.app.start(self.json_body()))

    def r_compile(self):
        """The bed as a compiled job: plot_svg's EBB command stream from a simulated plot, for run_compiled.py."""
        body = self.json_body()
        if not isinstance(body.get("doc"), dict):
            raise ApiError(400, "Send the bed (doc) to compile.")
        v, objs, pid, layers, plan = self.app._resolve_doc(body["doc"])
        out = plan / f"compiled_{secrets.token_hex(4)}.ebb"
        argv = plotjob.build_argv(self.app.s, plan / "drawing.svg", v, "plot", None, sim=True,
                                  layers_json=plan / "layers.json", machine=plan / "machine.json",
                                  bed=self.app._bed(plan)) + ["--emit", str(out)]
        rc, text = plotjob.run_dry(argv)
        if rc != 0 or not out.exists():
            raise ApiError(400, "Could not compile: " + "; ".join(plotjob.friendly_problems(None, text))[:600])
        data = out.read_bytes()
        out.unlink()
        name = re.sub(r"[^\w-]+", "_", str(body["doc"].get("name") or "bed"))[:60] or "bed"
        self.app.ev.log("info", f"Compiled job exported ({len(data) // 1024} kB): run it with tools/run_compiled.py.")
        self.send(200, data, "text/plain; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="{name}.ebb"'})

    def r_job_preview(self):
        pv = self.app.runner.preview
        if not pv:
            raise ApiError(404, "No job preview.")
        self.send(200, pv)

    def _control(self, fn, *args):
        ok, msg = fn(*args)
        if not ok:
            raise ApiError(409, msg)
        self.send(200, {"ok": True, "message": msg})

    def r_pause(self):
        self._control(self.app.runner.pause, self.json_body().get("job"))

    def r_speed(self):
        b = self.json_body()
        self._control(self.app.runner.set_speed, b.get("job"), b.get("factor"))

    def r_continue(self):
        b = self.json_body()
        self._control(self.app.runner.resume, b.get("job"), b.get("pause"))

    def r_return(self):
        self._control(self.app.runner.return_to_start, self.json_body().get("job"))

    def r_stop(self):
        try:
            job = self.json_body().get("job")
        except ApiError:
            job = None
        self._control(self.app.runner.stop, job)

    def r_live_start(self):
        self.send(200, self.app.live_start(self.json_body()))

    def r_live_send(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n > 262144:
            raise ApiError(413, "That message is too large.")
        b = self.json_body()
        self._control(self.app.runner.live_send, b.get("job"), b.get("cmd"), b.get("text", ""), b.get("phase"), b.get("pts"), b.get("sid"))

    def r_machine_start(self):
        self.send(200, self.app.machine_start(self.json_body()))

    def _config_edit(self, fn, *args):
        if self.app.runner.busy():
            raise ApiError(409, "A job is running. Change the machine or pen settings when it has ended.")
        try:
            out = fn(self.app.s, *args)
        except machinecfg.ConfigError as ex:
            raise ApiError(400, str(ex))
        apply_limits(self.app.s.machine())
        self.app.ev.emit("config")
        return out

    def r_doctor(self):
        if not self.app.hw_lock.acquire(timeout=5):
            raise ApiError(409, "Another plotter check is running.")
        try:
            rep = hardware.doctor(self.app.s.sim, self.app.runner.busy())
        finally:
            self.app.hw_lock.release()
        self.send(200, rep)

    def r_backup(self):
        b = machinecfg.backup_bundle(self.app.s, self.app.docs.proj_dir)
        name = f"plotter-settings-{time.strftime('%Y%m%d')}.json"
        self.send(200, json.dumps(b, indent=1), "application/json; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="{name}"'})

    def r_settings_restore(self):
        body = self.json_body()
        parts = [p for p in (body.get("parts") or []) if p in ("machine", "pens", "pen_map", "projects")]
        if not parts:
            raise ApiError(400, "Choose what to restore.")
        done = self._config_edit(machinecfg.restore_bundle, self.app.docs.proj_dir, body.get("bundle"), parts)
        self.app.ev.log("warn", "Restored from a settings backup: " + ", ".join(done) + ". The previous files are in "
                                "results/web/backups/. Preview again before plotting.")
        self.send(200, {"ok": True, "restored": done, "me": self.app.me(True)})

    def r_calibrate(self):
        b = self.json_body()
        old, new = self._config_edit(machinecfg.calibrate, b.get("axis"), b.get("commanded"), b.get("measured"))
        self.app.ev.log("warn", f"Calibration: {str(b.get('axis')).upper()} steps/mm {old:g} -> {new:g}. "
                                "Every drawing must be previewed again.")
        self.send(200, {"old": old, "new": new, "me": self.app.me(True)})

    def r_paper(self):
        paper = self._config_edit(machinecfg.set_paper, self.json_body().get("paper"))
        self.app.ev.log("info", f"Paper set: {paper['width']:g} x {paper['height']:g} mm at ({paper['x0']:g}, "
                                f"{paper['y0']:g}), margin {paper['margin_mm']:g} mm.")
        self.send(200, {"me": self.app.me(True)})

    def r_pot(self):
        pot = self._config_edit(machinecfg.set_pot, self.json_body().get("pot"))
        self.app.ev.log("info", f"Depth knob {'on' if pot['enabled'] else 'off'}: ends read {pot['adc_shallow']} (shallow) "
                                f"and {pot['adc_deep']} (deep), deepest {pot['depth_max']} servo units.")
        self.send(200, {"me": self.app.me(True)})

    def r_pot_keep(self):
        pot = getattr(self.app, "button", None) and self.app.button.pot
        if not pot or pot.get("pen_down") is None:
            raise ApiError(409, "The knob has no good reading right now" + (f": {pot['problem']}" if pot and pot.get("problem")
                                                                             else "."))
        old, new = self._config_edit(machinecfg.keep_pen_down, pot["pen_down"])
        self.app.ev.log("warn", f"Pen-down set from the knob: {old} -> {new}. Preview again before plotting.")
        self.send(200, {"me": self.app.me(True)})

    def r_machine_settings(self):
        cfg = self._config_edit(machinecfg.set_machine, self.json_body().get("machine"))
        self.app.ev.log("warn", f"Machine settings: travel {cfg['travel_mm']['x']:g} x {cfg['travel_mm']['y']:g} mm, "
                                f"directions X {cfg['axis']['x']['sign']:+d} Y {cfg['axis']['y']['sign']:+d}, limits "
                                f"{cfg['limits']['speed_max']:g}/{cfg['limits']['travel_max']:g} mm/s. Preview again "
                                "before plotting.")
        self.send(200, {"me": self.app.me(True)})

    def r_pen_save(self):
        b = self.json_body()
        self._config_edit(machinecfg.save_pen, b.get("name"), b.get("pen"), b.get("rename_from"))
        self.app.ev.log("info", f"Pen profile {b.get('name')} saved.")
        self.send(200, {"me": self.app.me(True)})

    def r_pens_export(self):
        self.send(200, json.dumps(self.app.s.pens(), indent=1), "application/json; charset=utf-8",
                  {"Content-Disposition": 'attachment; filename="pens.json"'})

    def r_pens_import(self):
        b = self.json_body()
        n = self._config_edit(machinecfg.import_pens, b.get("pens"), b.get("replace") is True)
        self.app.ev.log("info", f"Imported {n} pen profile{'s' if n != 1 else ''}.")
        self.send(200, {"me": self.app.me(True), "imported": n})

    def r_pen_delete(self):
        name = self.json_body().get("name")
        self._config_edit(machinecfg.delete_pen, name)
        self.app.ev.log("info", f"Pen profile {name} deleted.")
        self.send(200, {"me": self.app.me(True)})

    def r_machine_send(self):
        b = self.json_body()
        self._control(self.app.runner.machine_send, b.get("job"), b.get("cmd"), b.get("args"))

    def r_live_strokes(self):
        with self.app.runner.lock:
            strokes = list(self.app.runner.live_strokes)
        self.send(200, {"strokes": strokes})   # sent outside the lock: a slow client must never delay Stop

    def r_live_save(self):
        """The live page written so far (or the last session's) as a library drawing, where it was written."""
        with self.app.runner.lock:
            strokes = [s.get("pts") for s in self.app.runner.live_strokes if len(s.get("pts") or ()) >= 2]
        if not strokes:
            raise ApiError(400, "Nothing has been written on this page yet.")
        text, b = shapes.svg_layers([("#000000", [[tuple(p) for p in s] for s in strokes])])
        e = self.app.lib.add_svg_text(text, f"live-page-{time.strftime('%Y%m%d-%H%M')}.svg", kind="text")
        self.app.ev.log("info", f"Saved the live page as {e['name']}: it can be plotted again from the bed.")
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "x": round(b[0], 3), "y": round(b[1], 3)})

    def r_tool(self):
        self.send(200, self.app.run_tool(self.json_body()))

    def r_pentest(self):
        b = self.json_body()
        self._control(self.app.runner.pentest, b.get("job"), b.get("cmd"))

    def r_ports(self):
        try:
            ports = [{"device": p.device, "id": f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None else None,
                      "description": p.description} for p in hardware.list_ports.comports()] if hardware.serial else []
        except Exception:  # noqa: BLE001
            ports = []
        self.send(200, {"ports": ports, "chosen": hardware.PORT_CHOICE})

    def r_port_set(self):
        port = self.json_body().get("port") or None
        if port is not None and (not isinstance(port, str) or len(port) > 120 or not re.fullmatch(r"[\w./:\\-]+", port)):
            raise ApiError(400, "Not a serial port name.")
        if self.app.runner.busy():
            raise ApiError(409, "A job is running. Choose the port when it has ended.")
        hardware.set_port_choice(self.app.s, port)
        self.app.ev.log("info", f"Plotter port: {port or 'found automatically (the EBB USB id)'}.")
        self.send(200, {"ok": True, "chosen": hardware.PORT_CHOICE})

    def r_positions(self):
        self.send(200, {"positions": self.app.list_get("positions")})

    def r_position_save(self):
        self.send(200, {"positions": self.app.list_put("positions", self.json_body(), {"x": "num", "y": "num"})})

    def r_position_delete(self):
        self.send(200, {"positions": self.app.list_delete("positions", str(self.json_body().get("name") or ""))})

    def r_papers(self):
        self.send(200, {"papers": self.app.list_get("papers")})

    def r_paper_save(self):
        b = self.json_body()
        for k in ("width", "height"):
            v = b.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not 20 <= v <= 1000:
                raise ApiError(400, f"Paper {k}: 20 to 1000 mm.")
        self.send(200, {"papers": self.app.list_put("papers", b, {"width": "num", "height": "num", "type": "text",
                                                                  "notes": "text", "pens": "text"})})

    def r_paper_delete(self):
        self.send(200, {"papers": self.app.list_delete("papers", str(self.json_body().get("name") or ""))})

    def r_macros(self):
        self.send(200, {"macros": self.app.list_get("macros")})

    def r_macro_save(self):
        self.send(200, {"macros": self.app.list_put("macros", self.json_body(), {"text": "text"})})

    def r_macro_delete(self):
        self.send(200, {"macros": self.app.list_delete("macros", str(self.json_body().get("name") or ""))})

    def r_stats(self):
        self.send(200, self.app.runner.lifetime())

    def r_history(self):
        self.send(200, {"jobs": self.app.runner.history()})

    def r_plotter(self):
        if not self.app.hw_lock.acquire(timeout=5):
            raise ApiError(409, "Another plotter check is running.")
        try:
            if self.app.runner.busy():
                return self.send(200, dict(hardware.LAST or {}, ok=True, message="A job is using the plotter.",
                                           at=time.strftime("%H:%M:%S")))
            hardware.precheck(self.app.s.sim)
        finally:
            self.app.hw_lock.release()
        self.send(200, dict(hardware.LAST))


ROUTES_GET = {
    "/api/me": (Handler.r_me, False),
    "/api/state": (Handler.r_state, True),
    "/api/log": (Handler.r_log, True),
    "/api/log/download": (Handler.r_log_download, True),
    "/api/about": (Handler.r_about, True),
    "/api/diagnostics": (Handler.r_diagnostics, True),
    "/api/events": (Handler.r_events, True),
    "/api/files": (Handler.r_files, True),
    "/api/files/svg": (Handler.r_file_svg, True),
    "/api/files/orig": (Handler.r_file_orig, True),
    "/api/files/mask": (Handler.r_file_mask, True),
    "/api/converters": (Handler.r_converters, True),
    "/api/fonts": (Handler.r_fonts, True),
    "/api/live/strokes": (Handler.r_live_strokes, True),
    "/api/tools": (Handler.r_tools, True),
    "/api/pens": (Handler.r_pens, True),
    "/api/job/preview": (Handler.r_job_preview, True),
    "/api/files/natural": (Handler.r_natural, True),
    "/api/doc": (Handler.r_doc, True),
    "/api/plotter": (Handler.r_plotter, True),
    "/api/files/trash": (Handler.r_trash, True),
    "/api/history": (Handler.r_history, True),
    "/api/plotter/ports": (Handler.r_ports, True),
    "/api/positions": (Handler.r_positions, True),
    "/api/macros": (Handler.r_macros, True),
    "/api/papers": (Handler.r_papers, True),
    "/api/stats": (Handler.r_stats, True),
    "/api/pens/export": (Handler.r_pens_export, True),
    "/api/plotter/doctor": (Handler.r_doctor, True),
    "/api/backup": (Handler.r_backup, True),
    "/api/prepare/steps": (Handler.r_prep_steps, True),
    "/api/prepare/presets": (Handler.r_presets, True),
    "/api/prepare/file": (Handler.r_prep_file, True),
    "/api/tasks": (Handler.r_tasks, True),
}
ROUTES_POST = {
    "/api/login": (Handler.r_login, False),
    "/api/logout": (Handler.r_logout, False),
    "/api/job/stop": (Handler.r_stop, False),     # stopping is always allowed, even with an expired session
    "/api/files/upload": (Handler.r_upload, True),
    "/api/files/convert_preview": (Handler.r_convert_preview, True),
    "/api/files/delete": (Handler.r_delete, True),
    "/api/files/rename": (Handler.r_rename, True),
    "/api/files/tags": (Handler.r_tags, True),
    "/api/clipart": (Handler.r_clipart, True),
    "/api/restore": (Handler.r_settings_restore, True),
    "/api/plotter/port": (Handler.r_port_set, True),
    "/api/positions/save": (Handler.r_position_save, True),
    "/api/papers/save": (Handler.r_paper_save, True),
    "/api/papers/delete": (Handler.r_paper_delete, True),
    "/api/positions/delete": (Handler.r_position_delete, True),
    "/api/macros/save": (Handler.r_macro_save, True),
    "/api/macros/delete": (Handler.r_macro_delete, True),
    "/api/files/restore": (Handler.r_restore, True),
    "/api/files/purge": (Handler.r_purge, True),
    "/api/files/replace": (Handler.r_replace, True),
    "/api/export_doc": (Handler.r_export_doc, True),
    "/api/text_art": (Handler.r_text_art, True),
    "/api/text_page": (Handler.r_text_page, True),
    "/api/text_single": (Handler.r_text_single, True),
    "/api/text_path": (Handler.r_text_path, True),
    "/api/qr": (Handler.r_qr, True),
    "/api/shapes": (Handler.r_shapes, True),
    "/api/shape_new": (Handler.r_shape_new, True),
    "/api/live/start": (Handler.r_live_start, True),
    "/api/live/send": (Handler.r_live_send, True),
    "/api/live/save": (Handler.r_live_save, True),
    "/api/machine/start": (Handler.r_machine_start, True),
    "/api/machine/send": (Handler.r_machine_send, True),
    "/api/machine/calibrate": (Handler.r_calibrate, True),
    "/api/machine/paper": (Handler.r_paper, True),
    "/api/machine/settings": (Handler.r_machine_settings, True),
    "/api/machine/pot": (Handler.r_pot, True),
    "/api/machine/pot/keep": (Handler.r_pot_keep, True),
    "/api/pens/save": (Handler.r_pen_save, True),
    "/api/pens/delete": (Handler.r_pen_delete, True),
    "/api/pens/import": (Handler.r_pens_import, True),
    "/api/preview": (Handler.r_preview, True),
    "/api/doc": (Handler.r_doc_put, True),
    "/api/projects/save": (Handler.r_project_save, True),
    "/api/projects/open": (Handler.r_project_open, True),
    "/api/projects/delete": (Handler.r_project_delete, True),
    "/api/job/start": (Handler.r_start, True),
    "/api/job/compile": (Handler.r_compile, True),
    "/api/job/pause": (Handler.r_pause, True),
    "/api/job/speed": (Handler.r_speed, True),
    "/api/job/continue": (Handler.r_continue, True),
    "/api/job/return": (Handler.r_return, True),
    "/api/tools/run": (Handler.r_tool, True),
    "/api/pentest": (Handler.r_pentest, True),
    "/api/prepare/source": (Handler.r_prep_source, True),
    "/api/prepare/from_library": (Handler.r_prep_from, True),
    "/api/prepare/run": (Handler.r_prep_run, True),
    "/api/prepare/shading": (Handler.r_prep_shading, True),
    "/api/tasks/cancel": (Handler.r_task_cancel, True),
    "/api/prepare/save": (Handler.r_prep_save, True),
    "/api/prepare/presets/save": (Handler.r_preset_put, True),
    "/api/prepare/presets/delete": (Handler.r_preset_delete, True),
}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, app):
        super().__init__(addr, Handler)
        self.app = app
        self.stopping = False


def shutdown_job(app, timeout=None):
    """On server shutdown: stop a running job and wait for its cleanup."""
    from .runner import KILL_GRACE_S, STOP_GRACE_S
    timeout = timeout or STOP_GRACE_S + KILL_GRACE_S + 30
    if app.runner.busy():
        app.runner.stop()
        end = time.time() + timeout
        while app.runner.busy() and time.time() < end:
            time.sleep(0.2)


def serve(app, host, port):
    srv = Server((host, port), app)
    return srv

