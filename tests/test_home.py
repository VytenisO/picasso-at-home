"""home.py in --sim only: limits, confirmation, and the reported position after each kind of run."""
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOME = [sys.executable, str(ROOT / "tools" / "home.py"), "--sim"]


def run(*args, stdin=""):
    p = subprocess.run(HOME + [str(a) for a in args], input=stdin, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


def result(out):
    line = next(ln for ln in out.splitlines() if ln.startswith("__RESULT__:"))
    return json.loads(line[len("__RESULT__:"):])


class TestHome(unittest.TestCase):
    def test_limits(self):
        for args, msg in ((["--speed", "16"], "--speed"), (["--speed", "0"], "--speed"),
                          (["--overrun", "60"], "--overrun"), (["--axis", "x", "--dot"], "--dot")):
            rc, out = run(*args, "--yes")
            self.assertNotEqual(rc, 0, args)
            self.assertIn(msg, out)

    def test_needs_typed_confirmation(self):
        rc, out = run("--track", stdin="no\n")
        self.assertEqual(rc, 1)
        self.assertIn("Nothing moved", out)
        self.assertNotIn("__RESULT__", out)
        rc, out = run("--track", stdin="home\n")
        self.assertEqual(rc, 0, out)

    def test_both_axes_end_at_s(self):
        cfg = json.loads((ROOT / "config" / "machine.json").read_text())
        rc, out = run("--yes", "--track", "--sim-from", "120,80")
        self.assertEqual(rc, 0, out)
        r = result(out)
        self.assertEqual(r["homed"], ["x", "y"])
        self.assertTrue(r["at_start"])
        self.assertAlmostEqual(r["end_mm"][0], cfg["paper"]["x0"], delta=0.1)
        self.assertAlmostEqual(r["end_mm"][1], cfg["paper"]["y0"], delta=0.1)
        self.assertIn("X stop reached after 120.0 mm", out)
        self.assertLess(out.index("X homed"), out.index("Y homed"))

    def test_no_position_reports_before_homed(self):
        rc, out = run("--yes", "--track")
        lines = out.splitlines()
        first_pos = next(i for i, ln in enumerate(lines) if ln.startswith("__POS__"))
        self.assertGreater(first_pos, next(i for i, ln in enumerate(lines) if ln == "Y homed."))

    def test_corner_and_dot(self):
        rc, out = run("--yes", "--track", "--to", "corner", "--dot")
        r = result(out)
        self.assertTrue(r["corner"])
        self.assertFalse(r["at_start"])
        self.assertEqual(r["end_mm"], [0.0, 0.0])
        self.assertIn("Dot at (10.0, 10.0) mm", out)

    def test_one_axis_leaves_position_unknown(self):
        rc, out = run("--yes", "--track", "--axis", "y")
        r = result(out)
        self.assertEqual(r["homed"], ["y"])
        self.assertIsNone(r["end_mm"])
        self.assertFalse(r["at_start"])
        self.assertNotIn("__POS__", out)

    def test_stop_before_homing_finishes(self):
        p = subprocess.Popen(HOME + ["--yes", "--track", "--control", "--sim-speed", "20"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate("STOP\n", timeout=60)
        self.assertEqual(p.returncode, 1, out)
        r = result(out)
        self.assertTrue(r["status"].startswith("interrupted"), r)
        self.assertIsNone(r["end_mm"])
        self.assertIn("Motors released", out)


if __name__ == "__main__":
    unittest.main()
