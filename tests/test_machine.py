"""Machine panel: manual control (jog.py through the job runner), calibration, paper and pen profiles. Simulator only;
the machine and pen configs are copies in a temporary directory, never the repo's own."""
import json
import shutil
import sys
import time
import unittest
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__file__))
from test_plotweb import ROOT, SimServer  # noqa: E402

from plotweb.api import App, Server  # noqa: E402
from plotweb.config import Settings  # noqa: E402


class MachineServer(SimServer):
    @classmethod
    def setUpClass(cls):
        import pathlib
        import tempfile
        import threading
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="plotmachine_"))
        shutil.copy(ROOT / "config" / "pens.json", cls.tmp / "pens.json")
        shutil.copy(ROOT / "config" / "machine.json", cls.tmp / "machine.json")
        s = Settings(cls.tmp, machine=cls.tmp / "machine.json", pens=cls.tmp / "pens.json",
                     web_config=cls.tmp / "web.json", sim=True, sim_speed=50)
        cls.app = App(s, {"auth": False, "salt": "00", "secret": "x", "password_hash": ""})
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    def send(self, job, cmd, **args):
        code, r = self.call("POST", "/api/machine/send", {"job": job, "cmd": cmd, "args": args})
        self.assertEqual(code, 200, r)

    def pos_after(self, fn, timeout=20):
        """Run fn, then wait until the commanded position stops changing."""
        fn()
        end, last = time.time() + timeout, None
        while time.time() < end:
            time.sleep(0.3)
            _, st = self.call("GET", "/api/state")
            p = (st["job"] or {}).get("pos")
            if p and p == last:
                return p
            last = p
        self.fail("the carriage did not settle")


class TestMachine(MachineServer):
    def start(self):
        code, r = self.call("POST", "/api/machine/start", {"mode": "simulate"})
        self.assertEqual(code, 200, r)
        end = time.time() + 20
        while time.time() < end:
            _, st = self.call("GET", "/api/state")
            if ((st["job"] or {}).get("machine") or {}).get("ready"):
                return r["job"]
            time.sleep(0.1)
        self.fail("machine control did not get ready")

    def test_01_jog_clamp_set_s_and_finish(self):
        job = self.start()
        sx, sy = self.app.s.bed()["start"]
        p = self.pos_after(lambda: self.send(job, "move", dx=10, dy=5))
        self.assertAlmostEqual(p["x"], sx + 10, delta=0.1)
        self.assertAlmostEqual(p["y"], sy + 5, delta=0.1)
        # far outside: clamped to the reachable area, with a warning
        p = self.pos_after(lambda: self.send(job, "goto", x=900, y=20))
        self.assertAlmostEqual(p["x"], 368 - 5, delta=0.1)
        code, log = self.call("GET", "/api/log?since=0")
        self.assertTrue(any("edge of the reachable area" in e["text"] for e in log["events"]))
        # bad input is refused by the server before it reaches the process
        code, r = self.call("POST", "/api/machine/send", {"job": job, "cmd": "move", "args": {"dx": "1", "dy": 0}})
        self.assertEqual(code, 409)
        # one job at a time
        code, r = self.call("POST", "/api/tools/run", {"tool": "pen_test", "confirm": {"ok": True}})
        self.assertEqual(code, 409)
        # set S here, then finish: the carriage counts as at S
        self.pos_after(lambda: self.send(job, "goto", x=100, y=40))
        self.send(job, "sets")
        time.sleep(0.5)
        self.send(job, "quit")
        st = self.wait_state(("complete",))
        self.assertIn("at S", st["job"]["message"])

    def test_01b_touch(self):
        job = self.start()
        self.send(job, "touch", pressure=0.3, ms=200)
        time.sleep(1.0)
        code, r = self.call("POST", "/api/machine/send", {"job": job, "cmd": "touch", "args": {"pressure": 2, "ms": 200}})
        self.assertEqual(code, 409)
        code, log = self.call("GET", "/api/log?since=0")
        self.assertFalse(any("Traceback" in e["text"] for e in log["events"]))
        self.send(job, "quit")
        self.wait_state(("complete",))

    def test_01c_raw_console_positions_macros(self):
        job = self.start()
        self.send(job, "raw", text="QS")
        time.sleep(0.6)
        for bad in ("BL", "SC,4,30000", "QS QS", "RB"):
            code, r = self.call("POST", "/api/machine/send", {"job": job, "cmd": "raw", "args": {"text": bad}})
            self.assertEqual(code, 409, bad)
        self.send(job, "raw", text="SM,100,10,10")          # moves: the position becomes unknown
        time.sleep(0.6)
        code, st = self.call("GET", "/api/state")
        self.assertTrue(st["job"]["machine"].get("unknown"))
        self.assertTrue(any("QS" in x for x in st["job"]["machine"]["raw"]))
        self.send(job, "quit")
        st = self.wait_state(("complete",))
        self.assertEqual(st["position"]["state"], "unknown")
        code, r = self.call("POST", "/api/positions/save", {"name": "pen swap", "x": 300, "y": 20})
        self.assertEqual(r["positions"][-1], {"name": "pen swap", "x": 300.0, "y": 20.0})
        code, r = self.call("POST", "/api/positions/save", {"name": "far", "x": 5000, "y": 20})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/macros/save", {"name": "dot", "text": "PEN DOWN" + chr(10) + "WAIT 300" + chr(10) + "PEN UP"})
        self.assertEqual(r["macros"][0]["name"], "dot")
        code, r = self.call("POST", "/api/macros/delete", {"name": "dot"})
        self.assertEqual(r["macros"], [])
        code, r = self.call("GET", "/api/plotter/ports")
        self.assertIn("ports", r)
        code, r = self.call("POST", "/api/plotter/port", {"port": "bad port; rm"})
        self.assertEqual(code, 400)
        code, r = self.call("GET", "/api/stats")
        self.assertIn("jobs", r)

    def test_02_finish_here_and_stop(self):
        job = self.start()
        self.pos_after(lambda: self.send(job, "move", dx=30, dy=30))
        self.send(job, "quit", here=True)
        st = self.wait_state(("complete",))
        self.assertIn("left at about", st["job"]["message"])
        job = self.start()
        self.call("POST", "/api/job/stop", {"job": job})
        st = self.wait_state(("stopped", "interrupted"))
        code, r = self.call("POST", "/api/machine/send", {"job": job, "cmd": "home"})
        self.assertEqual(code, 409)

    def test_03_real_mode_needs_confirmation(self):
        code, r = self.call("POST", "/api/machine/start", {"mode": "plot"})
        # this server is a simulator: every job is simulated, so no confirmation is needed and it starts
        self.assertEqual(code, 200, r)
        self.call("POST", "/api/job/stop", {})
        self.wait_state(("stopped", "interrupted", "complete"))


class TestConfig(MachineServer):
    def test_01_calibrate(self):
        before = json.loads((self.tmp / "machine.json").read_text())["steps_per_mm"]["x"]
        code, r = self.call("POST", "/api/machine/calibrate", {"axis": "x", "commanded": 100, "measured": 99.0})
        self.assertEqual(code, 200, r)
        self.assertAlmostEqual(r["new"], round(before * 100 / 99, 3))
        cfg = json.loads((self.tmp / "machine.json").read_text())
        self.assertAlmostEqual(cfg["steps_per_mm"]["x"], r["new"])
        self.assertIn("web calibration", cfg["notes"])
        self.assertTrue(list((self.tmp / "results" / "web" / "backups").glob("machine_*.json")))
        code, r = self.call("POST", "/api/machine/calibrate", {"axis": "x", "commanded": 100, "measured": 70})
        self.assertEqual(code, 400)
        self.assertIn("20%", r["error"])
        code, r = self.call("POST", "/api/machine/calibrate", {"axis": "z", "commanded": 100, "measured": 100})
        self.assertEqual(code, 400)

    def test_02_paper(self):
        code, r = self.call("POST", "/api/machine/paper", {"paper": {"x0": 50, "y0": 6, "width": 297, "height": 210,
                                                                     "margin_mm": 12}})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["me"]["machine"]["margin"], 12)
        code, r = self.call("POST", "/api/machine/paper", {"paper": {"x0": 100, "y0": 6, "width": 297, "height": 210,
                                                                     "margin_mm": 12}})
        self.assertEqual(code, 400)
        self.assertIn("past the bed", r["error"])

    def test_02b_machine_settings_and_limits(self):
        cfg = {"travel_x": 368, "travel_y": 227.5, "sign_x": 1, "sign_y": -1,
               "speed_max": 30, "travel_max": 80, "speed_default": 12, "travel_default": 60}
        code, r = self.call("POST", "/api/machine/settings", {"machine": cfg})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["me"]["limits"]["speed"]["max"], 30)
        self.assertEqual(r["me"]["limits"]["travel_speed"]["default"], 60)
        self.assertEqual(r["me"]["machine_cfg"]["signs"]["y"], -1)
        code, r = self.call("POST", "/api/preview", {"doc": {"objects": [], "settings": {"speed": 40}}})
        self.assertEqual(code, 400)
        for bad, word in (({"speed_max": 10000}, "Drawing speed limit"), ({"speed_default": 50}, "default"),
                          ({"travel_x": 100}, "paper"), ({"sign_x": 0}, "direction")):
            code, r = self.call("POST", "/api/machine/settings", {"machine": dict(cfg, **bad)})
            self.assertEqual(code, 400, bad)
            self.assertIn(word.lower(), r["error"].lower())
        ceil = r and self.call("GET", "/api/me")[1]["machine_cfg"]["ceilings"]
        code, r = self.call("POST", "/api/machine/settings", {"machine": dict(cfg, sign_y=1, speed_max=ceil["speed"],
                            travel_max=ceil["travel_speed"], speed_default=15, travel_default=100)})
        self.assertEqual(code, 200, r)

    def test_04_backup_restore_and_doctor(self):
        with urllib.request.urlopen(self.base + "/api/backup") as r:
            self.assertIn("plotter-settings-", r.headers["Content-Disposition"])
            b = json.loads(r.read())
        self.assertEqual(b["kind"], "pen-plotter-settings")
        sx = b["machine"]["steps_per_mm"]["x"]
        # change the machine, then restore the backup over it
        self.call("POST", "/api/machine/calibrate", {"axis": "x", "commanded": 100, "measured": 98})
        code, r = self.call("POST", "/api/restore", {"bundle": b, "parts": ["machine", "pens"]})
        self.assertEqual(code, 200, r)
        self.assertEqual(json.loads((self.tmp / "machine.json").read_text())["steps_per_mm"]["x"], sx)
        bad = json.loads(json.dumps(b))
        bad["machine"]["servo"]["pen_up"] = 99999
        code, r = self.call("POST", "/api/restore", {"bundle": bad, "parts": ["machine"]})
        self.assertEqual(code, 400)
        self.assertIn("Servo", r["error"])
        code, r = self.call("POST", "/api/restore", {"bundle": {"kind": "other"}, "parts": ["machine"]})
        self.assertEqual(code, 400)
        code, rep = self.call("GET", "/api/plotter/doctor")
        self.assertEqual(code, 200)
        self.assertTrue(rep["sim"])

    def test_05_new_test_sheets_and_pen_import(self):
        pen = next(iter(json.loads((self.tmp / "pens.json").read_text())))
        for tool, params in (("square_sheet", {"pen": pen, "size": 80}), ("backlash_sheet", {"pen": pen}),
                             ("lost_sheet", {"pen": pen, "minutes": 2})):
            code, r = self.call("POST", "/api/tools/run", {"tool": tool, "params": params})
            self.assertEqual(code, 200, (tool, r))
            self.assertEqual(r["file"]["kind"], "sheet")
        code, r = self.call("POST", "/api/tools/run", {"tool": "lost_record", "params": {"dx": 0.8, "dy": -1.2}})
        self.assertEqual(code, 200, r)
        self.assertIn("steps", r["output"])
        with urllib.request.urlopen(self.base + "/api/pens/export") as resp:
            pens = json.loads(resp.read())
        pens["imported-pen"] = {"colour": "red", "width_mm": 0.4, "hatch_mm": 0.8, "dwell_ms": 100}
        code, r = self.call("POST", "/api/pens/import", {"pens": {"imported-pen": pens["imported-pen"]}})
        self.assertEqual(code, 200, r)
        self.assertIn("imported-pen", r["me"]["pens"])
        self.assertIn(pen, r["me"]["pens"])                      # merged, not replaced
        code, r = self.call("POST", "/api/pens/import", {"pens": {"Bad Name": {}}})
        self.assertEqual(code, 400)
        code, r = self.call("POST", "/api/pens/import", {"pens": {"x": {"width_mm": 99}}})
        self.assertEqual(code, 400)

    def test_03_pens(self):
        code, r = self.call("POST", "/api/pens/save", {"name": "fineliner", "pen": {
            "description": "0.3 fineliner", "colour": "blue", "colour_hex": "#2244AA", "width_mm": 0.3, "speed": 20}})
        self.assertEqual(code, 200, r)
        p = r["me"]["pens"]["fineliner"]
        self.assertEqual(p["colour_hex"], "#2244aa")
        self.assertEqual(p["width_mm"], 0.3)
        # rename keeps recorded results and the place in the list
        pens = json.loads((self.tmp / "pens.json").read_text())
        pens["fineliner"]["stroke_results"] = {"single": 0.4}
        (self.tmp / "pens.json").write_text(json.dumps(pens))
        code, r = self.call("POST", "/api/pens/save", {"name": "fine", "rename_from": "fineliner", "pen": {"width_mm": 0.35}})
        self.assertEqual(code, 200, r)
        self.assertNotIn("fineliner", r["me"]["pens"])
        self.assertEqual(r["me"]["pens"]["fine"]["stroke_results"], {"single": 0.4})
        self.assertEqual(r["me"]["pens"]["fine"]["description"], "0.3 fineliner")
        for bad in ({"name": "Bad Name", "pen": {}}, {"name": "x", "pen": {"colour_hex": "blue"}},
                    {"name": "x", "pen": {"speed": 290}}, {"name": "bic", "rename_from": "fine", "pen": {}}):
            code, r = self.call("POST", "/api/pens/save", bad)
            self.assertEqual(code, 400, bad)
        code, r = self.call("POST", "/api/pens/delete", {"name": "fine"})
        self.assertEqual(code, 200)
        self.assertNotIn("fine", r["me"]["pens"])


if __name__ == "__main__":
    unittest.main()
