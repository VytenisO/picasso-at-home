"""End-to-end tests of the web server in simulator mode. No hardware is touched: the server runs with sim=True,
so every job is plot_svg.py --sim and the serial port is never opened.

    python -m unittest discover -s tests -v
"""
import json
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from plotweb.api import App, Server, shutdown_job  # noqa: E402
from plotweb.config import Settings  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


class SimServer(unittest.TestCase):
    sim_speed = 4.0

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="plotweb_"))
        shutil.copy(ROOT / "config" / "pens.json", cls.tmp / "pens.json")
        s = Settings(cls.tmp, pens=cls.tmp / "pens.json", web_config=cls.tmp / "web.json", sim=True,
                     sim_speed=cls.sim_speed)
        cls.app = App(s, {"auth": False, "salt": "00", "secret": "x", "password_hash": ""})
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stopping = True
        shutdown_job(cls.app, 20)
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        if self.app.runner.busy():
            self.app.runner.stop()
            self.wait_idle()

    # ---- http helpers
    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if raw is None else {}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read() or b"{}")

    def upload(self, name, params=None):
        raw = (FIX / name).read_bytes()
        code, d = self.call("POST", "/api/files/upload", raw=raw,
                            headers={"X-Filename": name, "X-Params": json.dumps(params or {})})
        self.assertEqual(code, 200, d)
        return d["file"]["id"]

    def wait_state(self, states, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            _, st = self.call("GET", "/api/state")
            job = st.get("job") or {}
            if job.get("state") in states:
                return st
            time.sleep(0.1)
        self.fail(f"timed out waiting for {states}; last: {job.get('state')} {job.get('message')}")

    def wait_idle(self):
        end = time.time() + 60
        while self.app.runner.busy() and time.time() < end:
            time.sleep(0.1)

    def preview(self, fid, settings=None):
        code, d = self.call("POST", "/api/preview", {"id": fid, "settings": settings or {}})
        self.assertEqual(code, 200, d)
        return d


class TestApi(SimServer):
    def test_01_me_and_static(self):
        code, me = self.call("GET", "/api/me")
        self.assertEqual(code, 200)
        self.assertTrue(me["sim"])
        self.assertEqual(me["limits"]["speed"]["max"], 40.0)
        self.assertEqual(me["limits"]["travel_speed"]["max"], 200.0)
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn("Content-Security-Policy", r.headers)
            self.assertIn(b"<html", r.read().lower())
        with urllib.request.urlopen(self.base + "/js/turtle-worker.js") as r:   # runs pasted code: no network
            self.assertEqual(r.headers["Content-Security-Policy"], "default-src 'none'; script-src 'unsafe-eval'")
        code, _ = self.call("GET", "/js/../../plot_svg.py")
        self.assertEqual(code, 404)

    def test_01b_fonts_and_text_page(self):
        code, d = self.call("GET", "/api/fonts")
        self.assertEqual(code, 200)
        self.assertIn("Comic Neue", d["bundled"])
        body = {"text": "Hello there\nSecond paragraph in a standard font", "font": "Comic Neue", "xheight": 4.5}
        code, d = self.call("POST", "/api/text_page", body)
        self.assertEqual(code, 200, d)
        self.assertIn("<path", d["svg"])
        self.assertGreaterEqual(d["stats"]["lines"], 2)
        code, d = self.call("POST", "/api/text_page", dict(body, add=True))
        self.assertEqual(code, 200, d)
        fid = d["file"]["id"]
        self.assertEqual(self.preview(fid)["summary"]["problems"], [])
        code, d = self.call("POST", "/api/text_page", {"text": "   ", "font": "Comic Neue"})
        self.assertEqual(code, 400)
        code, d = self.call("POST", "/api/text_page", {"text": "x" * 70, "font": "Comic Neue", "xheight": 20})
        self.assertEqual(code, 400)   # one word wider than the page

    def test_01c_live_writing(self):
        body = {"font": "Comic Neue", "xheight": 4.5, "speed": 15, "mode": "simulate"}
        code, d = self.call("POST", "/api/live/start", dict(body, font="No Such Font"))
        self.assertEqual(code, 400)
        code, d = self.call("POST", "/api/live/start", dict(body, speed=80))
        self.assertEqual(code, 400)
        code, d = self.call("POST", "/api/live/start", dict(body, pressure=0.95))
        self.assertEqual(code, 400)
        code, d = self.call("POST", "/api/live/start", body)
        self.assertEqual(code, 200, d)
        job = d["job"]
        self.wait_state(["running"])
        code, d = self.call("POST", "/api/live/send", {"job": job, "cmd": "text", "text": "Hello there"})
        self.assertEqual(code, 200, d)
        end = time.time() + 30
        while time.time() < end:
            _, st = self.call("GET", "/api/state")
            if (st["job"].get("live") or {}).get("lines", 0) >= 1:
                break
            time.sleep(0.1)
        self.assertEqual(st["job"]["live"]["lines"], 1)
        code, d = self.call("GET", "/api/live/strokes")
        self.assertGreater(len(d["strokes"]), 3)
        n1 = len(d["strokes"])
        long_text = "A much longer line, written to make one event larger than a read chunk of 4096 bytes"
        self.call("POST", "/api/live/send", {"job": job, "cmd": "text", "text": long_text})
        end = time.time() + 30
        while time.time() < end:
            _, st = self.call("GET", "/api/state")
            if st["job"]["live"]["lines"] >= 2:
                break
            time.sleep(0.1)
        self.assertGreaterEqual(st["job"]["live"]["lines"], 2)
        code, d = self.call("GET", "/api/live/strokes")
        self.assertGreater(len(d["strokes"]), n1 + 50)
        code, d = self.call("POST", "/api/live/send", {"job": "wrong", "cmd": "text", "text": "x"})
        self.assertEqual(code, 409)
        # pause holds typed lines until continue; return goes to S; the page saves as a drawing
        self.call("POST", "/api/live/send", {"job": job, "cmd": "pause"})
        time.sleep(0.8)
        _, st = self.call("GET", "/api/state")
        self.assertTrue(st["job"]["live"].get("paused"))
        lines = st["job"]["live"]["lines"]
        self.call("POST", "/api/live/send", {"job": job, "cmd": "text", "text": "held"})
        time.sleep(1.0)
        _, st = self.call("GET", "/api/state")
        self.assertEqual(st["job"]["live"]["lines"], lines)
        self.call("POST", "/api/live/send", {"job": job, "cmd": "continue"})
        end = time.time() + 30
        while time.time() < end:
            _, st = self.call("GET", "/api/state")
            if st["job"]["live"]["lines"] > lines:
                break
            time.sleep(0.1)
        self.assertGreater(st["job"]["live"]["lines"], lines)
        code, d = self.call("POST", "/api/live/send", {"job": job, "cmd": "home"})
        self.assertEqual(code, 200, d)
        code, d = self.call("POST", "/api/live/save", {})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["file"]["name"].startswith("live-page-"))
        code, d = self.call("POST", "/api/live/send", {"job": job, "cmd": "quit"})
        self.assertEqual(code, 200, d)
        st = self.wait_state(["complete", "error", "stopped"])
        self.assertEqual(st["job"]["state"], "complete", st["job"])
        self.assertTrue(st["job"]["result"]["at_start"])

    def test_01d_live_stop(self):
        code, d = self.call("POST", "/api/live/start", {"font": "Comic Neue", "mode": "simulate"})
        self.assertEqual(code, 200, d)
        self.wait_state(["running"])
        code, d = self.call("POST", "/api/job/stop", {})
        self.assertEqual(code, 200, d)
        st = self.wait_state(["stopped", "interrupted", "complete", "error"])
        self.assertIn(st["job"]["state"], ("stopped", "interrupted"))

    def test_01e_mirror_and_sport(self):
        body = {"font": "Comic Neue", "mode": "simulate"}
        code, d = self.call("POST", "/api/live/start", dict(body, sport=True))
        self.assertEqual(code, 400)                       # sport needs its own confirmation
        code, d = self.call("POST", "/api/live/start", dict(body, sport=True, confirm={"sport": True}))
        self.assertEqual(code, 200, d)
        job = d["job"]
        self.wait_state(["running"])
        send = lambda **kw: self.call("POST", "/api/live/send", dict({"job": job, "cmd": "mirror"}, **kw))
        self.assertEqual(send(phase="start", pts=[[100, 100, 0]])[0], 200)
        self.assertEqual(send(phase="move", pts=[[110, 102, 10], [120, 105, 20]])[0], 200)
        self.assertEqual(send(phase="end", pts=[[130, 110, 30]])[0], 200)
        self.assertEqual(send(phase="stroke", pts=[[150, 80, 0], [160, 90, 20], [170, 80, 40]])[0], 200)
        for bad in ({"phase": "nope", "pts": [[1, 1]]}, {"phase": "move", "pts": []}, {"phase": "move", "pts": [[1]]},
                    {"phase": "move", "pts": [["a", 1]]}, {"phase": "move", "pts": [[1e9, 1]]},
                    {"phase": "move", "pts": [[float("nan"), 1]]}, {"phase": "move", "pts": [[1, 1]] * 2001}):
            code, _ = send(**bad)
            self.assertEqual(code, 409, bad)
        end = time.time() + 30
        while time.time() < end:
            _, st = self.call("GET", "/api/state")
            if (st["job"].get("live") or {}).get("strokes", 0) >= 2:
                break
            time.sleep(0.1)
        self.assertEqual(st["job"]["live"]["strokes"], 2)
        _, d = self.call("GET", "/api/live/strokes")
        self.assertEqual(len(d["strokes"]), 2)
        self.call("POST", "/api/live/send", {"job": job, "cmd": "quit"})
        st = self.wait_state(["complete", "error", "stopped"])
        self.assertEqual(st["job"]["state"], "complete", st["job"])

    def test_02_upload_rejects(self):
        code, d = self.call("POST", "/api/files/upload", raw=b"hello", headers={"X-Filename": "x.exe"})
        self.assertEqual(code, 400)
        self.assertIn("cannot be plotted", d["error"])
        code, d = self.call("POST", "/api/files/upload", raw=b"<svg", headers={"X-Filename": "x.svg"})
        self.assertEqual(code, 400)

    def test_03_preview_placement_and_validation(self):
        fid = self.upload("two_layers.svg")
        d = self.preview(fid)
        self.assertTrue(d["ok"], d["problems"])
        self.assertEqual(len(d["summary"]["layers"]), 2)
        self.assertAlmostEqual(d["settings"]["x"], 70.0)
        b = d["summary"]["bbox_machine_mm"]
        self.assertAlmostEqual(b[0], 70.0, places=1)
        # move and resize: bbox follows {x, y, w}
        d = self.preview(fid, {"x": 120, "y": 40, "w": 50})
        b = d["summary"]["bbox_machine_mm"]
        self.assertAlmostEqual(b[0], 120.0, delta=0.2)
        self.assertAlmostEqual(b[1], 40.0, delta=0.2)
        self.assertAlmostEqual(b[2] - b[0], 50.0, delta=0.3)
        # off the canvas: a plain-language problem, not ok
        d = self.preview(fid, {"x": 300, "y": 40, "w": 100})
        self.assertFalse(d["ok"])
        self.assertTrue(any("right edge" in p for p in d["problems"]), d["problems"])
        # ceilings are enforced before anything runs
        code, d = self.call("POST", "/api/preview", {"id": fid, "settings": {"speed": 41}})
        self.assertEqual(code, 400)
        self.assertIn("between", d["error"])
        code, d = self.call("POST", "/api/preview", {"id": fid, "settings": {"travel_speed": 250}})
        self.assertEqual(code, 400)
        code, d = self.call("POST", "/api/preview", {"id": fid, "settings": {
            "layers": [{"key": "#000000|", "pressure": 0.95}]}})
        self.assertEqual(code, 400)
        code, d = self.call("POST", "/api/preview", {"id": fid, "settings": {"paper": {"width": 400, "height": 210}}})
        self.assertEqual(code, 400)
        self.assertIn("does not fit", d["error"])

    def test_03b_fill(self):
        fid = self.upload("filled.svg")
        d0 = self.preview(fid)
        d1 = self.preview(fid, {"fill": 1.0, "fill_angle": 30})
        self.assertTrue(d1["ok"], d1["problems"])
        self.assertEqual(d1["settings"]["fill"], 1.0)
        self.assertNotEqual(d0["plan_id"], d1["plan_id"])
        self.assertGreater(d1["summary"]["drawing_mm"], 5 * d0["summary"]["drawing_mm"])
        self.assertEqual(len(d1["summary"]["layers"]), len(d0["summary"]["layers"]))
        for bad in ({"fill": 0.05}, {"fill": 80}, {"fill": 1, "fill_angle": 400}):
            code, d = self.call("POST", "/api/preview", {"id": fid, "settings": bad})
            self.assertEqual(code, 400, bad)

    def test_04_start_requires_confirmation_and_current_plan(self):
        fid = self.upload("two_layers.svg")
        d = self.preview(fid)
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"], "plan_id": d["plan_id"],
                                                      "mode": "plot"})
        self.assertEqual(code, 400)
        self.assertIn("parked", r["error"])
        changed = dict(d["settings"], speed=20)
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": changed, "plan_id": d["plan_id"],
                                                      "mode": "plot", "confirm": {"parked": True}})
        self.assertEqual(code, 409)
        self.assertIn("changed since the preview", r["error"])
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": {}, "plan_id": "0" * 20,
                                                      "mode": "plot", "confirm": {"parked": True}})
        self.assertEqual(code, 409)
        self.wait_idle()

    def test_05_full_run_with_pen_swap_pause_and_reload(self):
        fid = self.upload("two_layers.svg")
        d = self.preview(fid, {"layers": [{"key": "#000000|", "pen": "bic", "speed": 25},
                                          {"key": "#c0392b|", "pen": "marker", "pressure": 0.2}]})
        self.assertTrue(d["ok"], d["problems"])
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"], "plan_id": d["plan_id"],
                                                      "mode": "plot", "confirm": {"parked": True}})
        self.assertEqual(code, 200, r)
        job = r["job"]
        # a used plan cannot be started twice
        code, r2 = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"],
                                                       "plan_id": d["plan_id"], "mode": "plot",
                                                       "confirm": {"parked": True}})
        self.assertEqual(code, 409)
        st = self.wait_state(("paused",))
        pause = st["job"]["pause"]
        self.assertEqual(pause["kind"], "swap")
        self.assertEqual(pause["pen"], "marker")
        # reload: snapshot + job preview give everything the page needs
        code, pv = self.call("GET", "/api/job/preview")
        self.assertEqual(code, 200)
        self.assertEqual(len(pv["order"]), 4)
        self.assertGreaterEqual(st["job"]["progress"]["done"], 2)
        # a stale continue is refused
        code, r = self.call("POST", "/api/job/continue", {"job": job, "pause": pause["id"] + 1})
        self.assertEqual(code, 409)
        code, r = self.call("POST", "/api/job/continue", {"job": job, "pause": pause["id"]})
        self.assertEqual(code, 200, r)
        st = self.wait_state(("complete",))
        self.assertEqual(st["position"]["state"], "at_start")
        code, log = self.call("GET", "/api/log?since=0")
        texts = [e["text"] for e in log["events"]]
        self.assertTrue(any("PEN CHANGE" in t for t in texts))
        self.assertTrue(any(e["level"] == "debug" for e in log["events"]))

    def test_06_operator_pause_then_stop(self):
        fid = self.upload("many_strokes.svg")
        d = self.preview(fid)
        self.assertTrue(d["ok"], d["problems"])
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"], "plan_id": d["plan_id"],
                                                      "mode": "plot", "confirm": {"parked": True}})
        self.assertEqual(code, 200, r)
        job = r["job"]
        self.wait_state(("running",))
        time.sleep(0.5)
        code, r = self.call("POST", "/api/job/pause", {"job": job})
        self.assertEqual(code, 200, r)
        st = self.wait_state(("paused",))
        self.assertEqual(st["job"]["pause"]["kind"], "operator")
        code, r = self.call("POST", "/api/job/stop", {"job": job})
        self.assertEqual(code, 200)
        st = self.wait_state(("stopped",))
        self.assertEqual(st["position"]["state"], "moved")
        self.assertIn("slide it back", st["job"]["message"])

    def test_07_return_to_start(self):
        fid = self.upload("many_strokes.svg")
        d = self.preview(fid)
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"], "plan_id": d["plan_id"],
                                                      "mode": "simulate"})
        self.assertEqual(code, 200, r)
        self.wait_state(("running",))
        time.sleep(0.5)
        code, r = self.call("POST", "/api/job/return", {"job": r["job"]})
        self.assertEqual(code, 200, r)
        st = self.wait_state(("stopped",))
        self.assertEqual(st["position"]["state"], "at_start")

    def test_08_events_stream_replays_and_resets(self):
        _, st = self.call("GET", "/api/state")
        req = urllib.request.Request(self.base + f"/api/events?since=0&epoch=stale")
        with urllib.request.urlopen(req, timeout=20) as r:
            seen = b""
            while b"reset" not in seen:
                seen += r.readline()
        self.assertIn(b'"type": "reset"', seen)
        self.app.ev.log("info", "hello stream")
        req = urllib.request.Request(self.base + f"/api/events?since={st['seq']}&epoch={st['epoch']}")
        with urllib.request.urlopen(req, timeout=20) as r:
            seen = b""
            end = time.time() + 10
            while b"hello stream" not in seen and time.time() < end:
                seen += r.readline()
        self.assertIn(b"hello stream", seen)

    def test_09_sheets_are_per_pen(self):
        code, r = self.call("POST", "/api/tools/run", {"tool": "stroke_sheet", "params": {"sheet": "lines"}})
        self.assertEqual(code, 400)
        self.assertIn("pen", r["error"])
        code, r = self.call("POST", "/api/tools/run", {"tool": "stroke_sheet", "params": {"pen": "nope"}})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/tools/run", {"tool": "stroke_sheet", "params": {"pen": "bic"}})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["file"]["pen"], "bic")
        code, r = self.call("POST", "/api/tools/run", {"tool": "pressure_sheet",
                                                      "params": {"pen": "bic", "levels": "0,0.95"}})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/tools/run", {"tool": "stroke_record", "params": {"pen": "bic"}})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/tools/run", {"tool": "stroke_record", "params": {"pen": "bic",
                                                                                         "single": 1.0}})
        self.assertEqual(code, 200, r)
        pens = json.loads((self.tmp / "pens.json").read_text())
        self.assertEqual(pens["bic"]["short_stroke_results"]["min_single_mm"], 1.0)
        self.assertNotIn("short_stroke_results", pens["marker"])

    def test_10_pen_test_needs_confirmation(self):
        code, r = self.call("POST", "/api/tools/run", {"tool": "pen_test"})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/tools/run", {"tool": "pen_test", "confirm": {"ok": True}})
        self.assertEqual(code, 200, r)
        st = self.wait_state(("running",))
        code, r2 = self.call("POST", "/api/pentest", {"job": r["job"], "cmd": "toggle"})
        self.assertEqual(code, 200, r2)
        time.sleep(0.5)
        code, r2 = self.call("POST", "/api/job/stop", {"job": r["job"]})
        st = self.wait_state(("complete", "stopped"))
        self.assertEqual(st["job"]["kind"], "pentest")

    def test_12_trace_refuses_pause_return_ends_it(self):
        fid = self.upload("many_strokes.svg")
        d = self.preview(fid)
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"], "plan_id": d["plan_id"],
                                                      "mode": "trace", "confirm": {"parked": True}})
        self.assertEqual(code, 200, r)
        self.wait_state(("running", "complete"))
        code, r2 = self.call("POST", "/api/job/pause", {"job": r["job"]})
        self.assertEqual(code, 409)
        code, r2 = self.call("POST", "/api/job/return", {"job": r["job"]})
        self.assertIn(code, (200, 409))          # accepted while it runs (it ends a repeating trace), refused once done
        st = self.wait_state(("complete", "stopped"))
        self.assertEqual(st["position"]["state"], "at_start")

    def test_13_plan_inputs_are_staged(self):
        fid = self.upload("two_layers.svg")
        d = self.preview(fid)
        plan = self.app.plan_dir / d["plan_id"]
        for name in ("drawing.svg", "machine.json", "layers.json"):
            self.assertTrue((plan / name).is_file(), name)
        # editing the stored drawing after the preview makes the plan stale
        e = self.app.lib.get(fid)
        p = self.app.lib.path(e)
        p.write_text(p.read_text().replace('width="40"', 'width="41"'))
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"], "plan_id": d["plan_id"],
                                                      "mode": "plot", "confirm": {"parked": True}})
        self.assertEqual(code, 409)
        self.assertIn("changed since the preview", r["error"])

    def test_11_one_job_at_a_time(self):
        fid = self.upload("many_strokes.svg")
        d = self.preview(fid)
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"], "plan_id": d["plan_id"],
                                                      "mode": "simulate"})
        self.assertEqual(code, 200, r)
        code, r2 = self.call("POST", "/api/tools/run", {"tool": "pen_test", "confirm": {"ok": True}})
        self.assertEqual(code, 409)
        self.call("POST", "/api/job/stop", {})
        self.wait_state(("stopped", "complete"))


class TestDocument(SimServer):
    """Several objects on the bed, composed into one plan."""

    def obj(self, fid, x, y, w, h, **kw):
        return dict({"id": f"o{x:g}{y:g}", "file": fid, "x": x, "y": y, "w": w, "h": h, "rot": 0, "fx": False,
                     "fy": False}, **kw)

    def doc_preview(self, objects, settings=None):
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": objects, "settings": settings or {}}})
        self.assertEqual(code, 200, d)
        return d

    def test_01_natural_geometry(self):
        fid = self.upload("two_layers.svg")
        code, n = self.call("GET", f"/api/files/natural?id={fid}")
        self.assertEqual(code, 200, n)
        g = n["geom"]
        # the line runs corner to corner of the 100 x 60 mm document
        self.assertAlmostEqual(g["raw_x"], 0, places=1)
        self.assertAlmostEqual(g["raw_w"], 100, places=1)
        self.assertAlmostEqual(g["raw_h"], 60, places=1)
        self.assertEqual([ly["key"] for ly in n["layers"]], ["#000000|", "#c0392b|"])
        self.assertTrue(n["strokes"])

    def test_02_two_objects_rotated_and_mirrored(self):
        fid = self.upload("two_layers.svg")
        a = self.obj(fid, 80, 30, 100, 60)
        b = self.obj(fid, 200, 100, 50, 60, rot=90, fx=True)       # non-uniform, turned a quarter, mirrored
        d = self.doc_preview([a, b])
        self.assertTrue(d["ok"], d["problems"])
        bb = d["summary"]["bbox_machine_mm"]
        self.assertAlmostEqual(bb[0], 80, delta=0.2)
        self.assertAlmostEqual(bb[1], 30, delta=0.2)
        # b is 50 x 60 turned 90 degrees about its centre (225, 130): it spans x 195..255, y 105..155
        self.assertAlmostEqual(bb[2], 255, delta=0.2)
        self.assertAlmostEqual(bb[3], 155, delta=0.2)
        self.assertEqual(len(d["summary"]["layers"]), 2)       # same inks merge into one layer each
        self.assertEqual(d["summary"]["layers"][0]["strokes"], 2 * 2)
        # the bed is the server's working document now
        code, cur = self.call("GET", "/api/doc")
        self.assertEqual([o["id"] for o in cur["doc"]["objects"]], [a["id"], b["id"]])

    def test_03_outside_the_canvas_is_a_problem(self):
        fid = self.upload("two_layers.svg")
        d = self.doc_preview([self.obj(fid, 300, 30, 100, 60)])
        self.assertFalse(d["ok"])
        self.assertTrue(any("right edge" in p for p in d["problems"]), d["problems"])

    def test_04_run_the_bed_and_reject_a_changed_one(self):
        fid = self.upload("many_strokes.svg")
        objs = [self.obj(fid, 80, 30, 40, 40), self.obj(fid, 140, 30, 40, 40, rot=45)]
        d = self.doc_preview(objs)
        self.assertTrue(d["ok"], d["problems"])
        moved = [dict(objs[0], x=81), objs[1]]
        code, r = self.call("POST", "/api/job/start", {"doc": {"objects": moved, "settings": {}},
                                                      "plan_id": d["plan_id"], "mode": "simulate"})
        self.assertEqual(code, 409)
        self.assertIn("changed since the preview", r["error"])
        code, r = self.call("POST", "/api/job/start", {"doc": {"objects": objs, "settings": {}},
                                                      "plan_id": d["plan_id"], "mode": "simulate"})
        self.assertEqual(code, 200, r)
        code, pv = self.call("GET", "/api/job/preview")
        self.assertTrue(pv["doc"])
        st = self.wait_state(("complete",), timeout=120)
        self.assertEqual(st["job"]["meta"]["name"], "many_strokes.svg x2")
        code, r = self.call("POST", "/api/job/speed", {"job": st["job"]["id"], "factor": 0.5})
        self.assertEqual(code, 409)                                  # the job has ended
        code, h = self.call("GET", "/api/history")
        self.assertEqual(h["jobs"][0]["name"], "many_strokes.svg x2")
        self.assertEqual(h["jobs"][0]["state"], "complete")
        self.assertTrue(h["jobs"][0]["sim"])

    def test_04h_copies_and_finish(self):
        fid = self.upload("two_layers.svg")
        objs = [self.obj(fid, 80, 30, 40, 40)]
        one = self.doc_preview(objs)
        three = self.doc_preview(objs, {"copies": 3, "finish": "clear"})
        self.assertEqual(three["summary"]["copies"], 3)
        self.assertEqual(three["summary"]["finish"], "clear")
        self.assertAlmostEqual(three["summary"]["est_seconds_all"], 3 * one["summary"]["est_seconds"], delta=1)
        for bad in ({"copies": 0}, {"copies": 51}, {"copies": 2.5}, {"finish": "away"}):
            code, d = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": bad}})
            self.assertEqual(code, 400, bad)

    def test_04b_planning_and_resume_point(self):
        fid = self.upload("many_strokes.svg")
        objs = [self.obj(fid, 80, 30, 60, 60)]
        d = self.doc_preview(objs)
        s = d["summary"]
        self.assertTrue(s["optimized"])                          # on unless switched off
        self.assertLessEqual(s["travel_mm"], s["file_order"]["travel_mm"])
        off = self.doc_preview(objs, {"optimize": False})
        self.assertFalse(off["summary"]["optimized"])
        self.assertNotEqual(off["plan_id"], d["plan_id"])
        self.assertNotEqual(off["bed_key"], d["bed_key"])
        later = self.doc_preview(objs, {"start_at": 5})
        self.assertEqual(len(later["order"]), len(d["order"]) - 5)
        self.assertEqual(later["bed_key"], d["bed_key"])        # same bed, other resume point
        code, r = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": {"start_at": 10 ** 6}}})
        self.assertEqual(code, 200)
        self.assertFalse(r["ok"])
        code, r = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": {"start_at": "5"}}})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/job/start", {"doc": {"objects": objs, "settings": {"start_at": 5}},
                                                      "plan_id": later["plan_id"], "mode": "simulate"})
        self.assertEqual(code, 200, r)
        st = self.wait_state(("complete",), timeout=120)
        self.assertEqual(st["job"]["meta"]["bed_key"], d["bed_key"])
        self.assertEqual(st["job"]["meta"]["start_at"], 5)
        self.assertEqual(st["job"]["progress"]["strokes"], len(d["order"]) - 5)

    def test_04c_fill_inks_layer_modes_and_pen_memory(self):
        fid = self.upload("filled.svg")
        objs = [self.obj(fid, 80, 30, 60, 30)]
        d = self.doc_preview(objs)
        inks = {i["key"] for i in d["summary"]["fill_inks"]}
        self.assertEqual(inks, {"#000000|", "#2463a6|"})
        n0 = d["summary"]["strokes"]
        fills = [{"key": "#2463a6|", "mode": "contour", "spacing": 1.0}, {"key": "#000000|", "mode": "hatch",
                                                                          "spacing": 0.8, "angle": 0, "cross": True}]
        d = self.doc_preview(objs, {"fills": fills, "layers": [{"key": "#000000|", "pen": "bic", "passes": 2,
                                                                 "name": "Outline"}]})
        self.assertTrue(d["ok"], d["problems"])
        self.assertGreater(d["summary"]["strokes"], n0 + 10)      # rings and cross-hatch added
        black = next(ly for ly in d["settings"]["layers"] if ly["key"] == "#000000|")
        self.assertEqual((black["passes"], black["name"]), (2, "Outline"))
        code, r = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": {
            "fills": [{"key": "#2463a6|", "mode": "zigzag"}]}}})
        self.assertEqual(code, 400)
        # the colour remembers its pen: a new bed with black ink starts with the bic
        fid2 = self.upload("two_layers.svg")
        d = self.doc_preview([self.obj(fid2, 80, 30, 100, 60)])
        black = next(ly for ly in d["settings"]["layers"] if ly["key"] == "#000000|")
        self.assertEqual(black["pen"], "bic")

    def test_04d_single_line_text(self):
        code, r = self.call("POST", "/api/text_single", {"text": "Hello", "font": "Hershey Script", "xheight": 6})
        self.assertEqual(code, 200, r)
        d = self.doc_preview([self.obj(r["file"]["id"], 80, 30, 40, 12)])
        self.assertTrue(d["ok"], d["problems"])
        code, r = self.call("POST", "/api/text_single", {"text": "  "})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/text_single", {"text": "x", "font": "Comic Sans"})
        self.assertEqual(code, 400)

    def test_04e_shape_operations(self):
        sq = b'<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="20mm" viewBox="0 0 20 20"><rect x="0" y="0" width="20" height="20" stroke="#000"/></svg>'
        code, d = self.call("POST", "/api/files/upload", raw=sq, headers={"X-Filename": "sq.svg", "X-Params": "{}"})
        fid = d["file"]["id"]
        a, b = self.obj(fid, 100, 50, 20, 20), self.obj(fid, 110, 60, 20, 20)
        code, me = self.call("GET", "/api/me")
        if not me["features"]["shapes"]:
            self.skipTest("no shapely here")

        def op(name, objs, **kw):
            code, r = self.call("POST", "/api/shapes", dict({"op": name, "objects": objs}, **kw))
            self.assertEqual(code, 200, r)
            return r
        r = op("union", [a, b])
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (100, 50, 30, 30))
        code, n = self.call("GET", f"/api/files/natural?id={r['file']['id']}")
        self.assertEqual(len(n["strokes"]), 1)                         # one outline, the overlap gone
        r = op("intersect", [a, b])
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (110, 60, 10, 10))
        r = op("subtract", [a, b])
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (100, 50, 20, 20))
        r = op("offset", [a], distance=2, join="mitre")
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (98, 48, 24, 24))
        r = op("offset", [a], distance=-3)
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (103, 53, 14, 14))
        far = self.obj(fid, 200, 150, 20, 20)
        code, r = self.call("POST", "/api/shapes", {"op": "intersect", "objects": [a, far]})
        self.assertEqual(code, 400)
        self.assertIn("empty", r["error"])
        code, r = self.call("POST", "/api/shapes", {"op": "union", "objects": [a]})
        self.assertEqual(code, 400)

    def test_04f_join_rename_only_ink_export(self):
        line = lambda x0, x1: (f'<svg xmlns="http://www.w3.org/2000/svg" width="{x1 - x0}mm" height="1mm" '
                               f'viewBox="{x0} 0 {x1 - x0} 1"><path d="M{x0} 0.5 L{x1} 0.5" stroke="#000"/></svg>').encode()
        ids = []
        for n, (x0, x1) in enumerate(((0, 10), (10, 20))):
            code, d = self.call("POST", "/api/files/upload", raw=line(x0, x1),
                                headers={"X-Filename": f"seg{n}.svg", "X-Params": "{}"})
            self.assertEqual(code, 200, d)
            ids.append(d["file"]["id"])
        a, b = self.obj(ids[0], 100, 50, 10, 1), self.obj(ids[1], 110, 50, 10, 1)
        # join: two touching segments become one stroke
        code, r = self.call("POST", "/api/shapes", {"op": "join", "objects": [a, b]})
        self.assertEqual(code, 200, r)
        self.assertEqual((r["before"], r["after"]), (2, 1))
        self.assertEqual((r["x"], r["w"]), (100, 20))
        # rename keeps the extension and cleans the name
        code, r = self.call("POST", "/api/files/rename", {"id": ids[0], "name": "Left half!"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["file"]["name"], "Left_half.svg")
        code, r = self.call("POST", "/api/files/rename", {"id": ids[0], "name": "  "})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/files/rename", {"id": "nope", "name": "x"})
        self.assertEqual(code, 404)
        # plot only one object (apart: the planner itself joins strokes that touch end to end)
        b = self.obj(ids[1], 115, 50, 10, 1)
        full = self.doc_preview([a, b])
        one = self.doc_preview([a, b], {"only": [a["id"]]})
        self.assertEqual(full["summary"]["strokes"], 2)
        self.assertEqual(one["summary"]["strokes"], 1)
        self.assertNotEqual(full["plan_id"], one["plan_id"])
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": [a], "settings": {"only": ["gone"]}}})
        self.assertEqual(code, 400)
        # ink: the second object moves to a red layer
        two = self.doc_preview([a, dict(b, ink="#ff0000")])
        self.assertEqual(sorted(l["color"] for l in two["file_layers"]), ["#000000", "#ff0000"])
        # export: what will be plotted, one group per pen in plot order
        code, raw = None, None
        req = urllib.request.Request(self.base + "/api/export_doc", method="POST",
                                     data=json.dumps({"doc": {"name": "my bed", "objects": [a, dict(b, ink="#ff0000")],
                                                              "settings": {}}}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            self.assertIn("my_bed-plot.svg", resp.headers["Content-Disposition"])
            raw = resp.read().decode()
        self.assertEqual(raw.count("<path"), 2)
        self.assertIn('stroke="#ff0000"', raw)
        self.assertIn("M100 50.5 L110 50.5", raw.replace(".0 ", " ").replace(".0L", "L"))

    def test_04g_trash_restore_purge_replace(self):
        sq = b'<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 20 10"><rect width="20" height="10" stroke="#000" fill="none"/></svg>'
        code, d = self.call("POST", "/api/files/upload", raw=sq, headers={"X-Filename": "box.svg", "X-Params": "{}"})
        fid = d["file"]["id"]
        o = self.obj(fid, 100, 50, 20, 10)
        self.doc_preview([o])
        # trash: gone from the library and from the bed, still in the trash, thumbnail still served
        code, r = self.call("POST", "/api/files/delete", {"id": fid})
        self.assertEqual(code, 200, r)
        code, lib = self.call("GET", "/api/files")
        self.assertNotIn(fid, [f["id"] for f in lib["files"]])
        code, t = self.call("GET", "/api/files/trash")
        self.assertIn(fid, [f["id"] for f in t["files"]])
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": [o], "settings": {}}})
        self.assertEqual(code, 400)                       # the object's drawing is not in the library
        with urllib.request.urlopen(self.base + f"/api/files/svg?id={fid}") as resp:
            self.assertEqual(resp.status, 200)
        # restore
        code, r = self.call("POST", "/api/files/restore", {"id": fid})
        self.assertEqual(code, 200, r)
        self.assertTrue(self.doc_preview([o])["ok"])
        # replace: same id, new drawing, natural size changes
        tall = b'<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="30mm" viewBox="0 0 10 30"><rect width="10" height="30" stroke="#000" fill="none"/></svg>'
        code, n0 = self.call("GET", f"/api/files/natural?id={fid}")
        code, r = self.call("POST", f"/api/files/replace?id={fid}", raw=tall)
        self.assertEqual(code, 200, r)
        code, n1 = self.call("GET", f"/api/files/natural?id={fid}")
        self.assertNotEqual(n0["geom"]["svg_hash"], n1["geom"]["svg_hash"])
        self.assertAlmostEqual(n1["geom"]["raw_h"] / n1["geom"]["raw_w"], 3.0, places=1)
        code, r = self.call("POST", f"/api/files/replace?id={fid}", raw=b"<svg")
        self.assertEqual(code, 400)
        # purge
        self.call("POST", "/api/files/delete", {"id": fid})
        code, r = self.call("POST", "/api/files/purge", {"id": fid})
        self.assertEqual(code, 200, r)
        code, t = self.call("GET", "/api/files/trash")
        self.assertNotIn(fid, [f["id"] for f in t["files"]])
        code, r = self.call("POST", "/api/files/restore", {"id": fid})
        self.assertEqual(code, 404)

    def test_04i_lock_and_group_are_kept(self):
        fid = self.upload("two_layers.svg")
        objs = [self.obj(fid, 80, 30, 40, 40, locked=True, grp="g1"), self.obj(fid, 140, 30, 40, 40, grp="g1"),
                self.obj(fid, 200, 30, 40, 40, locked="yes", grp="bad group!")]
        code, r = self.call("POST", "/api/doc", {"objects": objs, "settings": {}})
        self.assertEqual(code, 200, r)
        code, d = self.call("GET", "/api/doc")
        got = d["doc"]["objects"]
        self.assertEqual([o.get("locked") for o in got], [True, None, None])
        self.assertEqual([o.get("grp") for o in got], ["g1", "g1", None])
        self.assertTrue(self.doc_preview(objs)["ok"])          # neither changes the plan

    def test_04j_order_by_object(self):
        wide = b'<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="10mm" viewBox="0 0 60 10"><path d="M0 5 L4 5" stroke="#000"/><path d="M56 5 L60 5" stroke="#000"/></svg>'
        dot = b'<svg xmlns="http://www.w3.org/2000/svg" width="4mm" height="10mm" viewBox="0 0 4 10"><path d="M0 5 L4 5" stroke="#000"/></svg>'
        ids = []
        for n, raw in (("wide", wide), ("mid", dot)):
            code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": f"{n}.svg", "X-Params": "{}"})
            ids.append(d["file"]["id"])
        objs = [self.obj(ids[0], 80, 40, 60, 10), self.obj(ids[1], 100, 40, 4, 10)]

        def xs(settings):
            pv = self.doc_preview(objs, settings)
            self.assertTrue(pv["ok"], pv["problems"])
            return [round(pv["strokes"][i]["pts"][0][0]) for i in pv["order"]]
        near = xs({})
        self.assertEqual(near[:2], [80, 100])          # nearest next: the middle object comes between
        by = xs({"by_object": True})
        self.assertEqual(sorted(by[:2]), [80, 136])     # the wide object first, both its ends
        self.assertIn(by[2], (100, 104))                # then the middle one (from either end)

    def test_04k_prepare_presets(self):
        code, r = self.call("GET", "/api/prepare/presets")
        self.assertEqual(code, 200)
        steps = [{"step": "lineart", "on": True, "params": {"width": 120, "nonsense": 5}}, {"step": "join", "params": {"tolerance": 0.5}}]
        code, r = self.call("POST", "/api/prepare/presets/save", {"name": "  Ink  sketch ", "kind": "image", "steps": steps,
                                                                    "settings": {"fill": 1.5}})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["preset"]["name"], "Ink sketch")
        self.assertNotIn("nonsense", r["preset"]["steps"][0]["params"])
        self.assertEqual(r["preset"]["settings"]["fill"], 1.5)
        code, r = self.call("POST", "/api/prepare/presets/save", {"name": "Ink sketch", "kind": "image", "steps": steps[1:]})
        self.assertEqual(code, 400)                                  # no method
        code, r = self.call("POST", "/api/prepare/presets/save", {"name": "x", "kind": "image", "steps": [{"step": "rm -rf"}]})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/prepare/presets/delete", {"name": "Ink sketch"})
        self.assertEqual(code, 200, r)
        self.assertEqual([p["name"] for p in r["presets"]], [])

    def test_04l_layer_follows_its_pen(self):
        code, r = self.call("POST", "/api/pens/save", {"name": "slowpen", "pen": {"speed": 12, "pressure": 0.2}})
        self.assertEqual(code, 200, r)
        fid = self.upload("two_layers.svg")
        objs = [self.obj(fid, 80, 30, 40, 40)]
        keys = [l["key"] for l in self.doc_preview(objs)["file_layers"]]
        pv = self.doc_preview(objs, {"layers": [{"key": keys[0], "pen": "slowpen"},
                                                {"key": keys[1], "pen": "slowpen", "speed": 30}]})
        ls = {l["key"]: l for l in pv["settings"]["layers"]}
        self.assertEqual((ls[keys[0]]["speed"], ls[keys[0]]["pressure"]), (12, 0.2))     # from the pen
        self.assertEqual(ls[keys[1]]["speed"], 30)                                     # its own wins
        code, r = self.call("POST", "/api/pens/save", {"name": "slowpen", "pen": {"speed": 18, "pressure": 0.2}})
        pv = self.doc_preview(objs, {"layers": [{"key": keys[0], "pen": "slowpen"}]})
        self.assertEqual(pv["settings"]["layers"][0]["speed"], 18)                     # retuned pen, same job
        self.call("POST", "/api/pens/delete", {"name": "slowpen"})

    def test_04m_break_apart_and_reverse(self):
        raw = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="10mm" viewBox="0 0 40 10">'
               b'<path d="M0 2 L10 2" stroke="#000"/><path d="M20 8 L30 8" stroke="#f00"/><path d="M35 0 L40 10" stroke="#000"/></svg>')
        code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": "three.svg", "X-Params": "{}"})
        o = self.obj(d["file"]["id"], 100, 50, 40, 10)
        code, r = self.call("POST", "/api/shapes", {"op": "split", "objects": [o]})
        self.assertEqual(code, 200, r)
        ps = r["pieces"]
        self.assertEqual(len(ps), 3)
        self.assertEqual([(p["x"], p["y"]) for p in ps], [(100, 52), (120, 58), (135, 50)])
        code, n = self.call("GET", f"/api/files/natural?id={ps[1]['file']['id']}")
        self.assertEqual(n["layers"][0]["color"], "#ff0000")
        code, r = self.call("POST", "/api/shapes", {"op": "split", "objects": [self.obj(ps[0]["file"]["id"], 1, 1, 10, 0.3)]})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/shapes", {"op": "reverse", "objects": [o]})
        self.assertEqual(code, 200, r)
        code, n = self.call("GET", f"/api/files/natural?id={r['file']['id']}")
        firsts = [s["pts"][0] for s in n["strokes"]]
        self.assertEqual(len(firsts), 3)
        self.assertGreater(firsts[0][0], firsts[-1][0])        # the last stroke now comes first, from its far end

    def test_04n_fill_own_layer_and_simplify(self):
        raw = (b'<svg xmlns="http://www.w3.org/2000/svg" width="30mm" height="30mm" viewBox="0 0 30 30">'
               b'<rect x="2" y="2" width="26" height="26" stroke="#000000" fill="#000000"/></svg>')
        code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": "box.svg", "X-Params": "{}"})
        objs = [self.obj(d["file"]["id"], 100, 40, 30, 30)]
        pv = self.doc_preview(objs)
        key = pv["summary"]["fill_inks"][0]["key"]
        same = self.doc_preview(objs, {"fills": [{"key": key, "mode": "hatch", "spacing": 2}]})
        own = self.doc_preview(objs, {"fills": [{"key": key, "mode": "hatch", "spacing": 2, "own_layer": True}]})
        self.assertEqual(len(same["file_layers"]), 1)
        self.assertEqual(len(own["file_layers"]), 2)          # the hatch on a layer of its own: its own pen
        self.assertIn("fill", own["file_layers"][1]["name"].lower())
        a = self.doc_preview(objs, {"simplify": 0})
        self.assertEqual(a["settings"]["simplify"], 0)
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": {"simplify": 3}}})
        self.assertEqual(code, 400)

    def test_04o_line_tools(self):
        code, me = self.call("GET", "/api/me")
        if not me["features"]["shapes"]:
            self.skipTest("no shapely here")
        sq = b'<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="20mm" viewBox="0 0 20 20"><rect width="20" height="20" stroke="#000" fill="none"/></svg>'
        cross = (b'<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="20mm" viewBox="0 0 20 20">'
                 b'<path d="M0 10 L20 10" stroke="#000"/><path d="M10 0 L10 20" stroke="#000"/></svg>')
        ids = []
        for n, raw in (("sq", sq), ("cross", cross)):
            code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": f"{n}.svg", "X-Params": "{}"})
            ids.append(d["file"]["id"])
        box, x = self.obj(ids[0], 100, 50, 20, 20), self.obj(ids[1], 100, 50, 20, 20)

        def op(name, objs, **kw):
            code, r = self.call("POST", "/api/shapes", dict({"op": name, "objects": objs}, **kw))
            self.assertEqual(code, 200, (name, r))
            return r
        r = op("fillet", [box], radius=3)
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (100, 50, 20, 20))
        r = op("hull", [box, x])
        self.assertEqual(r["w"], 20)
        r = op("affine", [box], m=[2, 0, 0, 2, -100, -50])            # doubled about (100, 50)
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (100, 50, 40, 40))
        r = op("warp", [box], quad=[[105, 50], [115, 50], [120, 70], [100, 70]])
        self.assertEqual((r["x"], r["w"]), (100, 20))
        r = op("trim", [x], x=118, y=60)                                # the right arm of the cross goes
        self.assertEqual((r["x"], r["w"]), (100, 10))
        small = self.obj(ids[0], 105, 55, 10, 10)
        r = op("cut", [small, x], keep="outside")
        self.assertEqual(r["replace"], [x["id"]])                       # the cutter stays
        code, n = self.call("GET", f"/api/files/natural?id={r['file']['id']}")
        self.assertEqual(len(n["strokes"]), 4)                          # each arm cut in two by the hole
        r = op("penwidth", [box], width=1)
        self.assertEqual((r["x"], r["w"]), (100.5, 19))
        code, r = self.call("POST", "/api/shapes", {"op": "fillet", "objects": [box], "radius": 30})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/shapes", {"op": "trim", "objects": [x], "x": 150, "y": 150})
        self.assertEqual(code, 400)

    def test_04p_pen_behaviours_and_job_options(self):
        raw = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm" viewBox="0 0 40 40">'
               b'<rect x="0" y="0" width="40" height="40" stroke="#000" fill="none"/>'
               b'<rect x="10" y="10" width="20" height="20" stroke="#000" fill="#000"/></svg>')
        code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": "nest.svg", "X-Params": "{}"})
        objs = [self.obj(d["file"]["id"], 100, 40, 40, 40)]
        base = self.doc_preview(objs)
        key = base["file_layers"][0]["key"]
        fkey = base["summary"]["fill_inks"][0]["key"]
        pv = self.doc_preview(objs, {
            "layers": [{"key": key, "dash": [2, 1], "dwell_ms": 200, "overdraw": 3, "passes": 2, "pressure_step": 0.1}],
            "fills": [{"key": fkey, "mode": "hatch", "spacing": 2, "passes": 2, "angle_step": 90, "inset": 0.3, "bridge": 0.5}],
            "corner_starts": True, "inner_first": True, "approach": 1, "start_delay": 2, "end_delay": 1})
        self.assertTrue(pv["ok"], pv["problems"])
        self.assertGreater(pv["summary"]["strokes"], base["summary"]["strokes"] * 10)     # dashes
        self.assertGreater(pv["summary"]["est_seconds"], base["summary"]["est_seconds"] + 3)
        for bad in ({"layers": [{"key": key, "dash": [2]}]}, {"layers": [{"key": key, "dwell_ms": 9000}]},
                    {"approach": 50}, {"fills": [{"key": fkey, "mode": "hatch", "passes": 9}]}):
            code, r = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": bad}})
            self.assertEqual(code, 400, bad)

    def test_04q_text_edit_path_qr(self):
        code, r = self.call("POST", "/api/text_single", {"text": "Hello", "xheight": 5, "tracking": 1})
        self.assertEqual(code, 200, r)
        fid = r["file"]["id"]
        w1 = r["stats"]["size_mm"][0]
        code, lib = self.call("GET", "/api/files")
        e = next(f for f in lib["files"] if f["id"] == fid)
        self.assertEqual(e["text_params"]["text"], "Hello")
        code, r = self.call("POST", "/api/text_single", {"text": "Hello there", "xheight": 5, "replace": fid})
        self.assertEqual((code, r["file"]["id"]), (200, fid))                   # edited in place: same drawing
        self.assertGreater(r["stats"]["size_mm"][0], w1)
        code, r = self.call("POST", "/api/text_single", {"text": "Squeeze", "xheight": 5, "fit": 20})
        self.assertAlmostEqual(r["stats"]["size_mm"][0], 22, delta=0.2)          # 20 mm plus the 1 mm margins
        code, r = self.call("POST", "/api/text_single", {"text": "x", "tracking": 50})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/text_path", {"text": "Wave", "xheight": 4, "pts": [[100, 50], [140, 60], [180, 50]]})
        self.assertEqual(code, 200, r)
        code, r = self.call("POST", "/api/text_path", {"text": "Far too long for this", "xheight": 6, "pts": [[0, 0], [10, 0]]})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/qr", {"text": "https://example.org", "module": 1, "pen": 0.4})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["modules"], 25)                                       # version 2
        code, r = self.call("POST", "/api/qr", {"text": "x" * 300})
        self.assertEqual(code, 400)

    def test_04r_cad_tags_clipart_projects_diagnostics(self):
        dxf = "\n".join(["0", "SECTION", "2", "ENTITIES", "0", "LINE", "10", "0", "20", "0", "11", "40", "21", "0",
                          "0", "CIRCLE", "62", "1", "10", "20", "20", "20", "40", "8", "0", "ENDSEC", "0", "EOF"]).encode()
        code, d = self.call("POST", "/api/prepare/source", raw=dxf, headers={"X-Filename": "part.dxf"})
        self.assertEqual(code, 200, d)
        self.assertEqual(d["source"]["kind"], "svg")                          # a DXF becomes an SVG source
        code, d = self.call("POST", "/api/prepare/source", raw=b"IN;SP1;PU0,0;PD800,0,800,800;", headers={"X-Filename": "p.plt"})
        self.assertEqual(code, 200, d)
        fid = self.upload("two_layers.svg")
        code, r = self.call("POST", "/api/files/tags", {"id": fid, "tags": ["cards", " cards ", "Xmas"]})
        self.assertEqual(r["tags"], ["Xmas", "cards"])
        code, r = self.call("POST", "/api/clipart", {"objects": [self.obj(fid, 80, 30, 40, 40)], "name": "stamp"})
        self.assertEqual(code, 200, r)
        code, lib = self.call("GET", "/api/files")
        self.assertEqual(next(f for f in lib["files"] if f["id"] == r["file"]["id"])["kind"], "clipart")
        code, r = self.call("POST", "/api/doc", {"objects": [self.obj(fid, 80, 30, 40, 40)], "settings": {"notes": "Use the 0.3 pen"}})
        code, r = self.call("POST", "/api/projects/save", {"name": "Card template", "template": True})
        self.assertEqual(code, 200, r)
        code, r = self.call("POST", "/api/projects/open", {"slug": "card-template"})
        self.assertEqual(r["doc"]["name"], "")                                # a template opens as a fresh copy
        self.assertEqual(r["doc"]["settings"]["notes"], "Use the 0.3 pen")
        code, d = self.call("GET", "/api/doc")
        p = next(x for x in d["projects"] if x["slug"] == "card-template")
        self.assertTrue(p["template"] and p["opened"])
        self.call("POST", "/api/projects/delete", {"slug": "card-template"})
        code, a = self.call("GET", "/api/about")
        self.assertIn("python", a)
        for path in ("/api/log/download", "/api/diagnostics"):
            with urllib.request.urlopen(self.base + path) as resp:
                self.assertIn("attachment", resp.headers["Content-Disposition"])

    def test_04s_start_point_fill_order_overlaps(self):
        raw = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm" viewBox="0 0 40 40">'
               b'<path d="M0 0 L40 0 L40 40 L0 40 Z" stroke="#000" fill="none"/></svg>')
        code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": "sq.svg", "X-Params": "{}"})
        fid = d["file"]["id"]
        first = lambda pv: pv["strokes"][pv["order"][0]]["pts"][0]  # noqa: E731
        plain = first(self.doc_preview([self.obj(fid, 100, 50, 40, 40)]))
        seamed = first(self.doc_preview([self.obj(fid, 100, 50, 40, 40, seam=[1, 1])]))
        self.assertEqual([round(v) for v in seamed], [140, 90])           # starts at the corner nearest the point
        self.assertNotEqual([round(v) for v in plain], [140, 90])
        moved = first(self.doc_preview([self.obj(fid, 60, 50, 40, 40, seam=[1, 1])]))
        self.assertEqual([round(v) for v in moved], [100, 90])            # the point moves with the object
        pv = self.doc_preview([self.obj(fid, 100, 50, 40, 40), self.obj(fid, 100, 50, 40, 40, id="b")], {"overlaps": True})
        self.assertEqual(pv["summary"]["strokes"], 1)                     # the second square lies on the first
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": [self.obj(fid, 100, 50, 40, 40)],
                                                             "settings": {"fills": [{"key": "#000000|", "mode": "hatch", "group": "bogus"}]}}})
        self.assertEqual(code, 400, d)
        fid = self.upload("filled.svg")
        objs = [self.obj(fid, 80, 40, 50, 50), self.obj(fid, 150, 40, 50, 50)]
        code, d = self.call("GET", "/api/doc")
        for grp in ("object", "shape"):
            pv = self.doc_preview(objs, {"fill": 2, "fills": [{"key": k["key"], "mode": "hatch", "spacing": 2, "group": grp}
                                                             for k in self.doc_preview(objs, {"fill": 2})["summary"]["fill_inks"]]})
            self.assertGreater(pv["summary"]["strokes"], 4)

    def test_04t_start_from(self):
        fid = self.upload("two_layers.svg")
        objs = [self.obj(fid, 100, 50, 40, 40)]
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": {"origin": "here"}}})
        pos = self.call("GET", "/api/state")[1]["position"]
        if pos["state"] == "unknown":                                    # depends on what ran before in this class
            self.assertEqual(code, 400)
            self.assertIn("not known", d["error"])
        else:
            self.assertEqual(code, 200, d)
        self.call("POST", "/api/positions/save", {"name": "corner", "x": 150, "y": 80})
        for anc, want in (("tl", (150, 80)), ("mc", (130, 60)), ("br", (110, 40))):
            pv = self.doc_preview(objs, {"origin": "pos:corner", "origin_anchor": anc})
            b = pv["summary"]["bbox_machine_mm"]
            plain = self.doc_preview(objs)["summary"]["bbox_machine_mm"]
            self.assertAlmostEqual(b[0] - plain[0], want[0] - 100, 1)     # the whole job shifts by anchor -> point
            self.assertAlmostEqual(b[1] - plain[1], want[1] - 50, 1)
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": {"origin": "pos:nope"}}})
        self.assertEqual(code, 400)
        self.call("POST", "/api/positions/delete", {"name": "corner"})

    def test_04u_compiled_job(self):
        fid = self.upload("two_layers.svg")
        req = urllib.request.Request(self.base + "/api/job/compile", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"doc": {"objects": [self.obj(fid, 100, 50, 40, 40)], "settings": {},
                                                              "name": "card"}}).encode())
        with urllib.request.urlopen(req, timeout=120) as r:
            self.assertIn('filename="card.ebb"', r.headers["Content-Disposition"])
            text = r.read().decode()
        self.assertIn("#HEAD ", text)
        self.assertIn("\nSM,", text)

    def test_04v_pages(self):
        raw = (b'<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="20mm" viewBox="0 0 20 20">'
               b'<path d="M0 0 L20 20" stroke="#000"/></svg>')
        code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": "pg.svg", "X-Params": "{}"})
        fid = d["file"]["id"]
        doc = {"objects": [self.obj(fid, 100, 50, 20, 20), self.obj(fid, 100, 50, 20, 20, id="p2", page=1)],
               "settings": {"pages": 2}}
        code, pv = self.call("POST", "/api/preview", {"doc": dict(doc, settings={"pages": 2, "dedupe": True, "overlaps": True})})
        self.assertEqual(pv["summary"]["strokes"], 2)          # repeats are per page
        code, pv = self.call("POST", "/api/preview", {"doc": doc})
        self.assertTrue(pv["ok"], pv)
        self.assertEqual(pv["summary"]["strokes"], 2)          # the same spot on two sheets: not merged or deduped
        code, r = self.call("POST", "/api/job/start", {"doc": doc, "plan_id": pv["plan_id"], "mode": "plot",
                                                      "confirm": {"parked": True}})
        self.assertEqual(code, 200, r)
        st = self.wait_state(("paused",))
        self.assertEqual(st["job"]["pause"]["kind"], "page")
        self.assertEqual(st["job"]["pause"]["page"], 1)
        self.assertIn("page 2", st["job"]["message"])
        self.call("POST", "/api/job/continue", {"job": r["job"], "pause": st["job"]["pause"]["id"]})
        self.wait_state(("complete",))

    def test_04w_registration(self):
        raw = (b'<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 20 10">'
               b'<path d="M0 0 L20 0 L20 10" stroke="#000" fill="none"/></svg>')
        code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": "rg.svg", "X-Params": "{}"})
        objs = [self.obj(d["file"]["id"], 150, 80, 20, 10)]
        bb = lambda st: [round(v, 1) for v in self.doc_preview(objs, st)["summary"]["bbox_machine_mm"]]  # noqa: E731
        plain = bb({})
        shifted = bb({"register": {"from": [[100, 50], [200, 50]], "to": [[105, 55], [205, 55]]}})
        self.assertEqual(shifted, [v + 5 for v in plain])
        turned = bb({"register": {"from": [[100, 50], [200, 50]], "to": [[100, 50], [100, 150]]}})
        self.assertEqual(turned, [60.0, 100.0, 70.0, 120.0])           # a quarter turn about mark 1
        code, r = self.call("POST", "/api/preview", {"doc": {"objects": objs, "settings": {
            "register": {"from": [[100, 50], [200, 50]], "to": [[100, 50], [180, 50]]}}}})
        self.assertEqual(code, 400)                                     # 20% apart: the wrong marks
        self.assertIn("5%", r["error"])

    def test_04x_tile_clip(self):
        raw = (b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="10mm" viewBox="0 0 100 10">'
               b'<path d="M0 5 L100 5" stroke="#000"/></svg>')
        code, d = self.call("POST", "/api/files/upload", raw=raw, headers={"X-Filename": "tl.svg", "X-Params": "{}"})
        fid = d["file"]["id"]
        # the right half of a 100 mm line, moved onto the canvas and cut to a 50 mm box
        o = self.obj(fid, 50, 60, 100, 10, clip=[100, 50, 150, 80])
        s = self.doc_preview([o])["summary"]
        self.assertEqual(round(s["drawing_mm"]), 50)
        self.assertEqual([round(v) for v in s["bbox_machine_mm"]], [100, 65, 150, 65])

    def test_05_empty_bed_and_bad_objects(self):
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": [], "settings": {}}})
        self.assertEqual(code, 400)
        self.assertIn("empty", d["error"])
        fid = self.upload("two_layers.svg")
        code, d = self.call("POST", "/api/preview", {"doc": {"objects": [self.obj(fid, 80, 30, 0, 60)]}})
        self.assertEqual(code, 400)
        self.assertIn("width", d["error"])

    def test_06_projects(self):
        fid = self.upload("two_layers.svg")
        self.doc_preview([self.obj(fid, 80, 30, 100, 60)], {"speed": 20})
        code, r = self.call("POST", "/api/projects/save", {"name": "Card front"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["slug"], "card-front")
        code, _ = self.call("POST", "/api/doc", {"objects": [], "settings": {}})
        self.assertEqual(code, 200)
        code, r = self.call("POST", "/api/projects/open", {"slug": "card-front"})
        self.assertEqual(code, 200, r)
        self.assertEqual(len(r["doc"]["objects"]), 1)
        self.assertEqual(r["doc"]["settings"]["speed"], 20)
        self.assertEqual(r["doc"]["name"], "Card front")
        code, r = self.call("POST", "/api/projects/delete", {"slug": "card-front"})
        self.assertEqual(r["projects"], [])
        code, r = self.call("POST", "/api/projects/open", {"slug": "../etc"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
