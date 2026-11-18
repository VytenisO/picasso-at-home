"""HTTP API and the orchestration between library, previews and the job runner.

Routes are listed in ROUTES_GET / ROUTES_POST; docs/ui-design.md documents them. Everything returns JSON
({"ok": false, "error": "..."} on failure) except static files, file downloads and the event stream.
"""
import hashlib
import http.cookies
import json
import mimetypes
import pathlib
import secrets
import shutil
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import converters, hardware, plotjob, prepare, tools
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
        self.plans = {}
        self._fill_layers = {}      # (svg hash, fill spacing, angle) -> (layer keys, layers)
        self.plans_lock = threading.Lock()
        self.hw_lock = threading.Lock()      # one read-only plotter check at a time, never during a job start
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
                "limits": LIMITS, "pens": self.s.pens() if authed else {}, "epoch": self.ev.epoch}

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
        digest = hashlib.sha256(data).hexdigest()[:16]
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
        if g and g.get("svg_hash") == digest and e.get("layers_keys") is not None:
            return g, e["layers_keys"], e.get("layers") or []
        tmp = self.plan_dir / f"geom_{digest}_{secrets.token_hex(4)}.json"
        v = {"speed": LIMITS["speed"]["default"], "travel_speed": LIMITS["travel_speed"]["default"],
             "x": None, "y": None, "w": None, "paper": None, "layers": []}
        rc, out = plotjob.run_dry(plotjob.build_argv(self.s, svg_path, v, "dry", None, preview_json=tmp))
        if not tmp.exists():
            tail = [ln for ln in out.strip().splitlines() if ln.strip() and not ln.startswith("__")]
            raise ApiError(400, "This drawing cannot be plotted: " + (tail[-1] if tail else f"exit code {rc}"),
                           output=out[-4000:])
        pv = json.loads(tmp.read_text())
        tmp.unlink()
        sm = pv["summary"]
        b, sc = sm["bbox_machine_mm"], sm["scale"] or 1.0
        g = {"svg_hash": digest, "raw_w": (b[2] - b[0]) / sc, "raw_h": (b[3] - b[1]) / sc,
             "doc_w": sm["doc_mm"][0], "doc_h": sm["doc_mm"][1]}
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
        pid = plotjob.plan_id(svg_bytes, machine_bytes, v)
        d = self.plan_dir / pid
        if not (d / "machine.json").exists():
            d.mkdir(exist_ok=True)
            for name, data in (("drawing.svg", svg_bytes), ("layers.json", json.dumps(v["layers"]).encode()),
                               ("machine.json", machine_bytes)):     # machine.json last: it marks the stage done
                tmp = d / f".{name}.{secrets.token_hex(4)}"
                tmp.write_bytes(data)
                tmp.replace(d / name)
        return v, g, pid, layers, d

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
        e, svg_path = self._entry(body.get("id"))
        ok, job_id = self.runner.reserve("plot", mode, sim, {"file": e["id"], "name": e["name"], "plan_id": pid})
        if not ok:
            raise ApiError(409, job_id)
        try:
            v, g, now_pid, _, plan = self._resolve(e, svg_path, body.get("settings"))
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
                      "summary": _public_summary(pv["summary"]), "file": e["id"], "plan_id": pid}
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
            from handwriting import writers
            if not writers.valid(writer):
                raise ApiError(400, "Unknown writer.")
            if not writers.count(writer):
                raise ApiError(400, f"{writer!r} has no handwriting samples yet. Write some with tools/handwriting/capture_server.py, "
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
        memo = self.prep.dir / f"st_{run['key']}_dry_{fill:g}_{fill_angle:g}.json"
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
                "image": run["image"].name[3:-4] if run["image"] else None, "mask": bool(run["mask"]),
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
                src = self.prep.add_source(use.read_bytes(), e.get("name") or use.name)
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


def _thin(strokes):
    total = sum(len(s["pts"]) for s in strokes)
    if total <= MAX_DISPLAY_POINTS:
        return strokes
    k = -(-total // MAX_DISPLAY_POINTS)
    return [dict(s, pts=s["pts"][::k] + ([s["pts"][-1]] if (len(s["pts"]) - 1) % k else [])) for s in strokes]


def _public_summary(sm):
    keep = ("strokes", "points", "est_seconds", "bbox_machine_mm", "paper_mm", "canvas_mm", "layers", "swaps",
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
        from handwriting import writers
        ws = writers.list_writers()
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
        self.send(200, self.app.preview(self.json_body()))

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
                return self.send(200, {"ok": True, "message": "A job is using the plotter."})
            ok, msg = hardware.precheck(self.app.s.sim)
        finally:
            self.app.hw_lock.release()
        self.send(200, {"ok": ok, "message": msg})


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
    "/api/text_art": (Handler.r_text_art, True),
    "/api/text_page": (Handler.r_text_page, True),
    "/api/live/start": (Handler.r_live_start, True),
    "/api/live/send": (Handler.r_live_send, True),
    "/api/preview": (Handler.r_preview, True),
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

