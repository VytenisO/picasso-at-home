"""plot_svg.py web-UI flags, run in --sim only. Checks that the opt-in flags keep every existing limit."""
import json
import math
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLOT = [sys.executable, str(ROOT / "tools" / "plot_svg.py")]
FIX = ROOT / "tests" / "fixtures"


def run(*args, stdin=None):
    p = subprocess.run(PLOT + [str(a) for a in args], input=stdin, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


class TestFlags(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="plotsvg_"))

    def layers(self, data):
        f = self.tmp / "layers.json"
        f.write_text(json.dumps(data))
        return f

    def test_unstyled_paths_take_their_group_colour(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from plot_svg import load_svg
        svg = self.tmp / "groups.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 20 10">'
                       '<g style="stroke:#2463a6;fill:none"><path d="M0 0 L5 5"/><path d="M1 0 L6 5" fill="none"/></g>'
                       '<g stroke="#c0392b"><path d="M2 0 L7 5" stroke="#111111"/></g>'
                       '<path d="M3 0 L8 5"/></svg>')
        _, metas, _, _ = load_svg(str(svg), False)
        self.assertEqual([m["color"] for m in metas], ["#2463a6", "#2463a6", "#111111", "#000000"])

    def test_emit_compiled_job_and_check(self):
        job = self.tmp / "job.ebb"
        rc, out = run(FIX / "two_layers.svg", "--sim", "--mode", "plot", "--x0", "80", "--y0", "40", "--width", "60",
                      "--emit", job)
        self.assertEqual(rc, 0, out)
        lines = job.read_text().splitlines()
        self.assertTrue(lines[1].startswith("#HEAD "))
        self.assertTrue(any(ln.startswith("SM,") for ln in lines))
        self.assertTrue(any(ln.startswith("#PAUSE ") for ln in lines))       # the pen change between the layers
        self.assertEqual(lines[-1], "EM,0,0")                                 # motors off at the end
        tool = ROOT / "tools" / "run_compiled.py"
        r = subprocess.run([sys.executable, str(tool), str(job), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = json.loads((ROOT / "config" / "machine.json").read_text())
        cfg["steps_per_mm"]["x"] += 0.5
        other = self.tmp / "m.json"
        other.write_text(json.dumps(cfg))
        r = subprocess.run([sys.executable, str(tool), str(job), "--check", "--config", str(other)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)                                     # another calibration: refused
        rc, out = run(FIX / "two_layers.svg", "--mode", "dry", "--emit", job)
        self.assertNotEqual(rc, 0)

    def test_touching_strokes_join_and_overlaps_go(self):
        svg = self.tmp / "j.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="10mm" viewBox="0 0 40 10">'
                       '<path d="M0 5 L10 5" stroke="#000"/><path d="M10 5 L20 5" stroke="#000"/>'
                       '<path d="M5 5 L15 5" stroke="#000"/></svg>')
        pv = self.tmp / "pv.json"

        def summary(*extra):
            rc, out = run(svg, "--mode", "dry", "--x0", "20", "--y0", "20", "--preview-json", pv, *extra)
            self.assertEqual(rc, 0, out)
            return json.loads(pv.read_text())["summary"]
        s = summary()
        self.assertEqual(s["strokes"], 2)                 # the first two touch end to end: one stroke
        s = summary("--overlaps")
        self.assertEqual((s["strokes"], round(s["drawing_mm"])), (1, 20))

    def test_test_sheet_strokes_plot_as_drawn(self):
        """data-asis (a test sheet on the bed) and --as-is: no join, simplify, dedupe, overlap cut, reorder, reverse,
        passes or dashes; the plain strokes beside them are still optimised."""
        sheet = ('<path d="M30 5 L20 5"/><path d="M0 5 L10 5"/><path d="M10 5 L20 5"/>'      # touching, out of order
                 '<path d="M0 5 L10 5"/><path d="M5 5 L15 5"/>'                              # a repeat, an overlap
                 '<path d="M0 8 L1 8.001 L2 8 L3 8.001 L4 8"/>')                             # simplify would flatten it
        plain = '<path d="M30 9 L20 9"/><path d="M0 9 L10 9"/><path d="M0 9 L10 9"/>'
        pv = self.tmp / "pv.json"

        def plan(body, *extra):
            svg = self.tmp / "s.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="10mm" viewBox="0 0 40 10" '
                           f'stroke="#000" fill="none">{body}</svg>')
            rc, out = run(svg, "--mode", "dry", "--x0", "20", "--y0", "20", "--preview-json", pv, "--optimize",
                          "--dedupe", "--overlaps", "--sweep", "right", "--corner-starts", "--inner-first",
                          "--layers", self.layers([{"passes": 3, "dash": [1, 1], "overdraw": 2}]), *extra)
            self.assertEqual(rc, 0, out)
            d = json.loads(pv.read_text())
            ss = [d["strokes"][i]["pts"] for i in d["order"]]
            x0, y0 = min(x for s in ss for x, _ in s), min(y for s in ss for _, y in s) - 5     # the drawing's own mm
            return [[(round(x - x0, 2), round(y - y0, 2)) for x, y in s] for s in ss]
        want = [[(30, 5), (20, 5)], [(0, 5), (10, 5)], [(10, 5), (20, 5)], [(0, 5), (10, 5)], [(5, 5), (15, 5)],
                [(0, 8), (1, 8.0), (2, 8), (3, 8.0), (4, 8)]]
        self.assertEqual(plan(f'<g data-asis="1">{sheet}</g>')[:6], want)
        self.assertEqual(plan(sheet, "--as-is"), want)
        mixed = plan(f'<g data-asis="1">{sheet}</g>{plain}')
        self.assertEqual(mixed[:6], want)
        rest = mixed[6:]
        self.assertGreater(len(rest), 3)                  # passes and dashes did apply to the plain strokes
        self.assertTrue(all(math.dist(s[0], s[-1]) <= 1.01 for s in rest))     # cut into 1 mm dashes

    def test_stroke_speed_is_used_and_capped(self):
        def est(attr):
            svg = self.tmp / "sp.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="10mm" viewBox="0 0 100 10">'
                           f'<path d="M0 5 L100 5" stroke="#000"{attr}/></svg>')
            pv = self.tmp / "pv.json"
            rc, out = run(svg, "--mode", "dry", "--x0", "20", "--y0", "20", "--speed", "20", "--preview-json", pv)
            self.assertEqual(rc, 0, out)
            return json.loads(pv.read_text())["summary"]["est_seconds"]
        base = est("")
        self.assertAlmostEqual(est(' data-speed="10"') - base, 5.0, delta=0.3)      # 100 mm at 10 instead of 20
        self.assertAlmostEqual(est(' data-speed="400"') - base, -4.05, delta=0.3)   # capped at 200 mm/s; the ramps take 1 s of it

    def test_layer_limits(self):
        svg = FIX / "two_layers.svg"
        rc, out = run(svg, "--layers", self.layers([{"speed": 201}]))
        self.assertNotEqual(rc, 0)
        self.assertIn("speed must be in", out)
        rc, out = run(svg, "--layers", self.layers([{}, {"pressure": 0.91}]))
        self.assertIn("pressure must be in", out)
        rc, out = run(svg, "--layers", self.layers([{"pressure": float("nan")}]))
        self.assertNotEqual(rc, 0)
        rc, out = run(svg, "--layers", self.layers([{"key": "#c0392b|"}]))
        self.assertIn("re-run the preview", out)
        rc, out = run(svg, "--layers", self.layers([{"enabled": False}, {"enabled": False}]))
        self.assertIn("nothing to plot", out)

    def test_preview_json_and_disabled_layer_keeps_placement(self):
        svg = FIX / "two_layers.svg"
        a, b = self.tmp / "a.json", self.tmp / "b.json"
        rc, _ = run(svg, "--preview-json", a)
        self.assertEqual(rc, 0)
        rc, _ = run(svg, "--preview-json", b, "--layers", self.layers([{"enabled": False}]))
        self.assertEqual(rc, 0)
        pa, pb = json.loads(a.read_text()), json.loads(b.read_text())
        self.assertEqual(pa["placement"], pb["placement"])
        self.assertEqual(len(pa["order"]), 4)
        self.assertEqual(len(pb["order"]), 2)
        self.assertEqual(pb["summary"]["swaps"], 0)

    def test_paper_larger_than_bed_does_not_widen_travel(self):
        rc, out = run(FIX / "two_layers.svg", "--paper-height", "260", "--y0", "150")
        self.assertNotEqual(rc, 0)
        self.assertIn("travel is", out)

    def test_control_eof_stops_before_swap(self):
        rc, out = run(FIX / "two_layers.svg", "--mode", "plot", "--sim", "--track", "--control", stdin="")
        self.assertIn('"status": "interrupted"', out)

    def test_control_continue_and_stop(self):
        p = subprocess.Popen(PLOT + [str(FIX / "two_layers.svg"), "--mode", "plot", "--sim", "--track", "--control",
                                     "--sim-speed", "50"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        lines = []
        for line in p.stdout:
            lines.append(line)
            if line.startswith("__PAUSE__"):
                self.assertIn('"kind": "swap"', line)
                p.stdin.write("CONTINUE\n")
                p.stdin.flush()
            if line.startswith("__RESUMED__"):
                p.stdin.write("STOP\n")
                p.stdin.flush()
        p.wait(30)
        out = "".join(lines)
        self.assertIn("__RESUMED__", out)
        self.assertIn('"status": "interrupted"', out)
        self.assertIn('"at_start": false', out)

    def test_sim_speed_needs_sim(self):
        rc, out = run(FIX / "two_layers.svg", "--sim-speed", "5")
        self.assertIn("--sim-speed needs --sim", out)

    def test_without_flags_sim_plot_unchanged(self):
        t = time.time()
        rc, out = run(FIX / "two_layers.svg", "--mode", "plot", "--sim")
        self.assertEqual(rc, 0, out)
        self.assertIn("Status: complete", out)
        self.assertNotIn("__PROGRESS__", out)
        self.assertLess(time.time() - t, 30)


if __name__ == "__main__":
    unittest.main()
