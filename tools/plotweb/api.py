"""HTTP API and the orchestration between library, previews and the job runner.

Routes are listed in ROUTES_GET / ROUTES_POST; docs/ui-design.md documents them. Everything returns JSON
({"ok": false, "error": "..."} on failure) except static files, file downloads and the event stream.
"""
import hashlib
import http.cookies
import json
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

from . import converters, document, hardware, machinecfg, plotjob, prepare, shapes, tools
from .auth import COOKIE, SESSION_AGE, Auth
from .config import LIMITS, WEB
from .events import EventLog
from .library import MAX_UPLOAD, Library
from .runner import JobRunner

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
        self.ev = EventLog()
        self.auth = Auth(web_cfg)
        self.lib = Library(settings, self.ev)
        self.runner = JobRunner(settings, self.ev)
        self.prep = prepare.Workshop(settings)
        self.docs = document.DocStore(settings, self.lib, self.ev)
        self.plans = {}
        self._fill_layers = {}      # (svg hash, fill spacing, angle) -> (layer keys, layers)
        self.plans_lock = threading.Lock()
        self.hw_lock = threading.Lock()      # one read-only plotter check at a time, never during a job start
        if not settings.sim:
            hardware.ServoGuard(settings, self.runner, self.hw_lock, self.ev.log).start()
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
        return {"auth": authed, "auth_required": self.auth.enabled, "sim": self.s.sim, "machine": bed,
                "limits": LIMITS, "pens": self.s.pens() if authed else {}, "epoch": self.ev.epoch,
                "features": {"shapes": shapes.available()}, "plotter": hardware.LAST if authed else None}

    def state(self):
        seq = self.ev.seq           # first: events after it are replayed; the snapshot is at least this new
        snap = self.runner.snapshot()
        snap.update(epoch=self.ev.epoch, seq=seq, files_rev=self.lib.rev, sim=self.s.sim)
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
        rs = raw_doc.get("settings") if isinstance(raw_doc.get("settings"), dict) else {}
        if isinstance(rs.get("only"), list) and rs["only"]:
            objs = [o for o in objs if o["id"] in rs["only"]]
            if not objs:
                raise ApiError(400, "None of the objects chosen for plotting are on the bed. Plot all of them, "
                                    "or choose again.")
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
        machine_bytes = self.s.machine_path.read_bytes()
        bed = self.s.bed_from(machine_bytes)
        try:
            svg_bytes = document.compose(objs, files, bed)
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
        op = body.get("op")
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
        res = {"ok": ok, "plan_id": pid, "problems": problems, "settings": v, "doc": True, "file": None,
               "bed_key": document.bed_key(objs, raw.get("settings")),
               "file_layers": layers, "objects": objs}
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
        res = {"ok": ok, "plan_id": pid, "problems": problems, "settings": v, "file": e["id"],
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
                                      bed=self._bed(plan), sim=sim, sim_speed=self.s.sim_speed if sim else None,
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
        pen = p["pen"]
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

    def prepare_run(self, body):
        """Run a recipe and dry-run the result: the same strokes and summary a Plot preview gets."""
        t0 = time.time()
        run = self._prep_run(body.get("recipe"))
        fill, angle = self._prep_fill(body.get("settings"))
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

    def _prep_layers(self, raw, keys):
        """Pens chosen in Prepare -> layer settings, with the pen's own speed and pressure when it has them."""
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
            out.append({"key": k, "enabled": g.get("enabled", True) is not False, "pen": pen,
                        "speed": sp if ok_sp else None, "pressure": pr if ok_pr else None})
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


def _thin(strokes):
    total = sum(len(s["pts"]) for s in strokes)
    if total <= MAX_DISPLAY_POINTS:
        return strokes
    k = -(-total // MAX_DISPLAY_POINTS)
    return [dict(s, pts=s["pts"][::k] + ([s["pts"][-1]] if (len(s["pts"]) - 1) % k else [])) for s in strokes]


def _public_summary(sm):
    keep = ("strokes", "points", "est_seconds", "bbox_machine_mm", "paper_mm", "canvas_mm", "layers", "swaps",
            "optimized", "file_order", "duplicates_dropped", "start_at", "total_in_order", "fill_inks",
            "drawing_mm", "travel_mm", "lifts", "doc_mm", "scale", "start_mm", "speed", "travel_speed", "problems")
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

    def raw_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n > MAX_UPLOAD + 4096:
            raise ApiError(413, f"The upload is larger than {MAX_UPLOAD // (1024 * 1024)} MB.")
        return self.rfile.read(n) if n > 0 else b""

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
        e = self.app.lib.get(self.query().get("id", ""))
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
        ident = self.json_body().get("id", "")
        job = self.app.runner.snapshot()["job"]
        if self.app.runner.busy() and job and job["meta"].get("file") == ident:
            raise ApiError(409, "This file is being plotted. Stop the job first.")
        if not self.app.lib.delete(ident):
            raise ApiError(404, "Unknown file.")
        self.send(200, {"ok": True})

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
            svg, stats = hershey.svg(text, str(d.get("font") or "Hershey Sans"), xh, width, xh * 3.0, align)
        except (TypeError, ValueError) as ex:
            raise ApiError(400, str(ex))
        first = " ".join(text.split())[:20] or "text"
        e = self.app.lib.add_svg_text(svg, f"text-{first}.svg", kind="text")
        self.send(200, {"ok": True, "file": {"id": e["id"], "name": e["name"]}, "stats": stats})

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
            src = self.app.prep.add_source(self.raw_body(), name)
        except prepare.PrepareError as ex:
            raise ApiError(400, str(ex))
        self.send(200, {"ok": True, "source": {k: v for k, v in src.items() if k != "path"}})

    def r_prep_from(self):
        self.send(200, self.app.prepare_from(self.json_body()))

    def r_prep_run(self):
        self.send(200, self.app.prepare_run(self.json_body()))

    def r_prep_save(self):
        self.send(200, self.app.prepare_save(self.json_body()))

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
            headers["Content-Disposition"] = 'attachment; filename="prepared.svg"'
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
            slug = self.app.docs.save_project(self.json_body().get("name"))
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
        self.app.ev.emit("config")
        return out

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

    def r_pen_save(self):
        b = self.json_body()
        self._config_edit(machinecfg.save_pen, b.get("name"), b.get("pen"), b.get("rename_from"))
        self.app.ev.log("info", f"Pen profile {b.get('name')} saved.")
        self.send(200, {"me": self.app.me(True)})

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

    def r_tool(self):
        self.send(200, self.app.run_tool(self.json_body()))

    def r_pentest(self):
        b = self.json_body()
        self._control(self.app.runner.pentest, b.get("job"), b.get("cmd"))

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
    "/api/prepare/steps": (Handler.r_prep_steps, True),
    "/api/prepare/file": (Handler.r_prep_file, True),
}
ROUTES_POST = {
    "/api/login": (Handler.r_login, False),
    "/api/logout": (Handler.r_logout, False),
    "/api/job/stop": (Handler.r_stop, False),     # stopping is always allowed, even with an expired session
    "/api/files/upload": (Handler.r_upload, True),
    "/api/files/convert_preview": (Handler.r_convert_preview, True),
    "/api/files/delete": (Handler.r_delete, True),
    "/api/files/rename": (Handler.r_rename, True),
    "/api/export_doc": (Handler.r_export_doc, True),
    "/api/text_art": (Handler.r_text_art, True),
    "/api/text_page": (Handler.r_text_page, True),
    "/api/text_single": (Handler.r_text_single, True),
    "/api/shapes": (Handler.r_shapes, True),
    "/api/live/start": (Handler.r_live_start, True),
    "/api/live/send": (Handler.r_live_send, True),
    "/api/machine/start": (Handler.r_machine_start, True),
    "/api/machine/send": (Handler.r_machine_send, True),
    "/api/machine/calibrate": (Handler.r_calibrate, True),
    "/api/machine/paper": (Handler.r_paper, True),
    "/api/pens/save": (Handler.r_pen_save, True),
    "/api/pens/delete": (Handler.r_pen_delete, True),
    "/api/preview": (Handler.r_preview, True),
    "/api/doc": (Handler.r_doc_put, True),
    "/api/projects/save": (Handler.r_project_save, True),
    "/api/projects/open": (Handler.r_project_open, True),
    "/api/projects/delete": (Handler.r_project_delete, True),
    "/api/job/start": (Handler.r_start, True),
    "/api/job/pause": (Handler.r_pause, True),
    "/api/job/continue": (Handler.r_continue, True),
    "/api/job/return": (Handler.r_return, True),
    "/api/tools/run": (Handler.r_tool, True),
    "/api/pentest": (Handler.r_pentest, True),
    "/api/prepare/source": (Handler.r_prep_source, True),
    "/api/prepare/from_library": (Handler.r_prep_from, True),
    "/api/prepare/run": (Handler.r_prep_run, True),
    "/api/prepare/save": (Handler.r_prep_save, True),
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

