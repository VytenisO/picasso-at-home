"""Technical drawings: the shading finder (find_fills), the tracer's fills, and the Prepare method that runs
as a background task with progress.

The web part runs the server in simulator mode, like test_plotweb.py. Nothing here moves a machine.
"""
import io
import pathlib
import sys
import time
import unittest

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
import find_fills as ff  # noqa: E402
import trace_centerline as tc  # noqa: E402

from test_plotweb import SimServer  # noqa: E402


def sheet():
    """A grey shaded panel with a black grid drawn on it, a thick long line, a black blob and a thin line."""
    im = Image.new("L", (400, 300), 255)
    d = ImageDraw.Draw(im)
    d.rectangle((20, 20, 180, 140), fill=90)                  # grey shade
    for x in range(20, 181, 20):
        d.line([(x, 20), (x, 140)], fill=0, width=2)          # the grid drawn on the shade
    for y in range(20, 141, 20):
        d.line([(20, y), (180, y)], fill=0, width=2)
    d.line([(220, 40), (380, 40)], fill=0, width=9)           # a thick long line: a heavy pen
    d.ellipse((240, 160, 320, 240), fill=0)                   # a solid black blob: a fill
    d.line([(20, 270), (380, 270)], fill=0, width=2)          # an ordinary line
    return im


def find(an, x, y):
    cid = int(an.lab[y, x])
    return next(c for c in an.cands if c["id"] == cid)


class FindFills(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = np.asarray(sheet())
        cls.an = ff.analyze(cls.g, 170, max_w=16)

    def test_thick_line_stays_a_line_blob_and_shade_are_fills(self):
        dec = ff.decide(self.an, {"min_area": 100})
        self.assertEqual(dec[find(self.an, 300, 40)["id"]], "line")
        self.assertEqual(dec[find(self.an, 280, 200)["id"]], "fill")
        self.assertEqual(dec[find(self.an, 50, 50)["id"]], "fill")

    def test_lines_on_a_grey_shade_are_kept_and_the_blob_leaves_no_ink(self):
        dec = ff.decide(self.an, {"min_area": 100})
        ink, fills = ff.apply(self.an, dec)
        self.assertEqual(len(fills), 2)
        levels = sorted(lv for _, _, lv in fills)
        self.assertLess(levels[0], 40)                          # the blob: solid
        self.assertAlmostEqual(levels[1], 90, delta=8)          # the shade
        self.assertTrue(ink[60, 40])                            # a grid line on the shade
        self.assertFalse(ink[50, 50])                           # the shade between the lines
        self.assertFalse(ink[200, 280])                         # inside the blob

    def test_override_at_a_point_wins(self):
        ov = ff.parse_overrides("line:280,200;fill:300,41;bad;line:x,y")
        self.assertEqual(len(ov), 2)
        dec = ff.decide(self.an, {"min_area": 100}, ov)
        self.assertEqual(dec[find(self.an, 280, 200)["id"]], "line")
        self.assertEqual(dec[find(self.an, 300, 40)["id"]], "fill")

    def test_classify_follows_the_thresholds(self):
        c = {"area": 1000, "cover": 0.7, "elong": 10}
        self.assertEqual(ff.classify(c, cover=0.6, aspect=4), "line")
        self.assertEqual(ff.classify(c, cover=0.8, aspect=4), "fill")
        self.assertEqual(ff.classify(c, cover=0.6, aspect=12), "fill")
        self.assertEqual(ff.classify(c, cover=0.8, aspect=4, min_area=2000), "line")
        self.assertAlmostEqual(ff.tone_fraction({"hist": [1, 0, 3] + [0] * 61}, 8), 0.75)

    def test_public_outlines_are_in_image_pixels(self):
        pub = self.an.public()
        self.assertEqual(pub["size"], [400, 300])
        blob = next(c for c in pub["cands"] if c["id"] == find(self.an, 280, 200)["id"])
        xs = [x for r in blob["rings"] for x, _ in r]
        self.assertTrue(235 <= min(xs) <= 245 and 315 <= max(xs) <= 325, (min(xs), max(xs)))

    def test_thinning_reports_progress_and_matches(self):
        seen = []
        ink = self.g < 170
        a = tc.thin(ink)
        b = tc.thin(ink, progress=lambda st, f: seen.append(f), max_dist=10)
        self.assertTrue((a == b).all())
        self.assertTrue(seen and all(0 <= f < 1 for f in seen))

    def test_trace_writes_fills_and_pens(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td) / "t.svg"
            st = tc.trace_grey(self.g, out, 100, pens=2, max_pen_mm=3, shading={"min_area": 100})
            svg = out.read_text()
        self.assertEqual(st["fills"], 2)
        self.assertIn('inkscape:label="fills"', svg)
        self.assertIn('fill="#222222"', svg)                    # the solid blob
        self.assertIn('fill="#606060"', svg)                    # the grey shade
        self.assertEqual(len(st["pens"]), 2)
        self.assertGreater(st["pens"][1]["width_px"], st["pens"][0]["width_px"])


def png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


class TechnicalWeb(SimServer):
    def recipe(self, sid, **p):
        params = {"width_mm": 100, "fill_min_mm2": 1, "max_pen": 3, "enlarge": False}
        params.update(p)
        return {"source": sid, "steps": [{"step": "technical", "params": params}]}

    def wait_task(self, tid, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            code, d = self.call("GET", "/api/tasks")
            t = next(x for x in d["tasks"] if x["id"] == tid)
            if t["state"] not in ("queued", "running"):
                return t
            time.sleep(0.1)
        self.fail("task timed out")

    def test_01_shading_then_trace_in_the_background(self):
        code, d = self.call("POST", "/api/prepare/source", raw=png(sheet()), headers={"X-Filename": "sheet.png"})
        self.assertEqual(code, 200, d)
        sid = d["source"]["id"]
        code, steps = self.call("GET", "/api/prepare/steps")
        tech = next(s for s in steps["steps"] if s["id"] == "technical")
        self.assertTrue(tech["slow"])
        self.assertIn("cover", tech["live"])
        # the shading preview: measured in the background, then from the cache
        code, d = self.call("POST", "/api/prepare/shading", {"recipe": self.recipe(sid)})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["pending"])
        t = self.wait_task(d["task"]["id"])
        self.assertEqual(t["state"], "done", t)
        code, d = self.call("POST", "/api/prepare/shading", {"recipe": self.recipe(sid)})
        self.assertFalse(d["pending"])
        sh = d["shading"]
        self.assertEqual(sh["size"], [400, 300])
        self.assertGreaterEqual(len(sh["cands"]), 3)
        # changing a live setting does not measure again
        code, d = self.call("POST", "/api/prepare/shading", {"recipe": self.recipe(sid, cover=90, aspect=8)})
        self.assertFalse(d["pending"])
        # a slow method waits for Trace, then runs as a task with progress
        settings = {"fill": 0.6}
        code, d = self.call("POST", "/api/prepare/run", {"recipe": self.recipe(sid), "settings": settings})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["needs_trace"])
        code, d = self.call("POST", "/api/prepare/run", {"recipe": self.recipe(sid), "settings": settings, "go": True})
        self.assertTrue(d["pending"])
        t = self.wait_task(d["task"]["id"])
        self.assertEqual(t["state"], "done", t)
        self.assertGreater(t["elapsed"], 0)
        code, r = self.call("POST", "/api/prepare/run", {"recipe": self.recipe(sid), "settings": settings})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["strokes"])
        self.assertEqual(r["stats"]["technical"]["fills"], 2)
        names = [ly["name"] for ly in r["layers"]]
        self.assertTrue(any("fill" in n for n in names), names)
        # an estimate is learned from the run
        self.assertIsNotNone(self.app.tasks.estimate("trace", 0.12))

    def test_01b_small_images_are_enlarged(self):
        im = sheet().resize((200, 150))
        code, d = self.call("POST", "/api/prepare/source", raw=png(im), headers={"X-Filename": "small.png"})
        sid = d["source"]["id"]
        rec = self.recipe(sid, enlarge=True)
        code, d = self.call("POST", "/api/prepare/shading", {"recipe": rec})
        if d["pending"]:
            self.assertEqual(self.wait_task(d["task"]["id"])["state"], "done")
            code, d = self.call("POST", "/api/prepare/shading", {"recipe": rec})
        self.assertEqual(d["shading"]["size"], [800, 600])       # 4x, the most it enlarges
        from plotweb.prepare import tech_factor
        self.assertEqual(tech_factor((1419, 597)), 3)
        self.assertEqual(tech_factor((3825, 6825)), 1)
        self.assertEqual(tech_factor((500, 400), enlarge=False), 1)

    def test_02_cancel(self):
        def slow(report):
            for i in range(200):
                report("Working", i / 200)
                time.sleep(0.02)
        t = self.app.tasks.start("test", "Slow test", slow)
        time.sleep(0.2)
        code, d = self.call("POST", "/api/tasks/cancel", {"id": t["id"]})
        self.assertEqual(code, 200, d)
        self.assertEqual(self.wait_task(t["id"])["state"], "cancelled")
        code, d = self.call("POST", "/api/tasks/cancel", {"id": t["id"]})
        self.assertEqual(code, 409)


if __name__ == "__main__":
    unittest.main()
