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

    def test_12_trace_refuses_pause_and_return(self):
        fid = self.upload("many_strokes.svg")
        d = self.preview(fid)
        code, r = self.call("POST", "/api/job/start", {"id": fid, "settings": d["settings"], "plan_id": d["plan_id"],
                                                      "mode": "trace", "confirm": {"parked": True}})
        self.assertEqual(code, 200, r)
        self.wait_state(("running", "complete"))
        code, r2 = self.call("POST", "/api/job/pause", {"job": r["job"]})
        self.assertEqual(code, 409)
        code, r2 = self.call("POST", "/api/job/return", {"job": r["job"]})
        self.assertEqual(code, 409)
        st = self.wait_state(("complete",))
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
