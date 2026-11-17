"""plot_svg.py web-UI flags, run in --sim only. Checks that the opt-in flags keep every existing limit."""
import json
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

    def test_layer_limits(self):
        svg = FIX / "two_layers.svg"
        rc, out = run(svg, "--layers", self.layers([{"speed": 41}]))
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
