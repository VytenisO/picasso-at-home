"""Path planning: travel ordering, reversal, closed-path start points, duplicates, and the plot_svg.py flags."""
import json
import math
import pathlib
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import pathplan  # noqa: E402

PLOT = [sys.executable, str(ROOT / "tools" / "plot_svg.py")]


class TestOrder(unittest.TestCase):
    def test_reverses_and_chains(self):
        # three horizontal lines drawn alternately left-right: the file order travels back every time
        strokes = [[(0, 0), (100, 0)], [(0, 10), (100, 10)], [(0, 20), (100, 20)]]
        before = pathplan.travel(strokes, [0, 1, 2], (0, 0))
        order, end = pathplan.order_block(strokes, [0, 1, 2], (0, 0))
        self.assertEqual(order, [0, 1, 2])
        self.assertEqual(strokes[1][0], (100, 10))         # reversed: a serpentine
        self.assertLess(pathplan.travel(strokes, order, (0, 0)), before / 2)
        self.assertEqual(end, (100, 20))

    def test_closed_path_starts_nearest(self):
        sq = [(10, 10), (20, 10), (20, 20), (10, 20), (10, 10)]
        strokes = [list(sq)]
        order, _ = pathplan.order_block(strokes, [0], (21, 21))
        s = strokes[0]
        self.assertEqual(s[0], (20, 20))
        self.assertEqual(s[0], s[-1])
        self.assertEqual(len(s), 5)
        self.assertEqual(set(s), set(sq))

    def test_many_strokes_quickly_and_completely(self):
        rnd = random.Random(1)
        strokes = []
        for _ in range(4000):
            x, y = rnd.uniform(0, 300), rnd.uniform(0, 200)
            strokes.append([(x, y), (x + rnd.uniform(-5, 5), y + rnd.uniform(-5, 5))])
        idx = list(range(len(strokes)))
        before = pathplan.travel(strokes, idx, (0, 0))
        order, _ = pathplan.order_block(strokes, idx, (0, 0))
        self.assertEqual(sorted(order), idx)
        self.assertLess(pathplan.travel(strokes, order, (0, 0)), before / 5)

    def test_greedy_matches_brute_force(self):
        rnd = random.Random(7)
        strokes = [[(rnd.uniform(0, 50), rnd.uniform(0, 50)), (rnd.uniform(0, 50), rnd.uniform(0, 50))] for _ in range(40)]
        ref = [list(s) for s in strokes]
        order, _ = pathplan.order_block(strokes, list(range(40)), (0, 0), cell=3.0)
        # brute force greedy on the reference copy
        cur, left, want = (0, 0), set(range(40)), []
        while left:
            i, rev = min(((i, r) for i in left for r in (False, True)),
                         key=lambda t: (math.dist(cur, ref[t[0]][-1] if t[1] else ref[t[0]][0]), t[0], t[1]))
            want.append(i)
            left.discard(i)
            cur = ref[i][0] if rev else ref[i][-1]
        self.assertEqual(order, want)


class TestDedupe(unittest.TestCase):
    def test_same_ink_only_any_direction_any_start(self):
        sq = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        sq2 = [(10, 10), (10, 0), (0, 0), (0, 10), (10, 10)]     # same square, other start, other direction
        line = [(0, 0), (5, 5)]
        strokes = [sq, sq2, line, line[::-1], line]
        metas = [{"color": "#000"}, {"color": "#000"}, {"color": "#000"}, {"color": "#000"}, {"color": "#f00"}]
        s, m, n = pathplan.dedupe(strokes, metas)
        self.assertEqual(n, 2)
        self.assertEqual([x["color"] for x in m], ["#000", "#000", "#f00"])


class TestFlags(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="pathplan_"))
        lines = "".join(f'<line x1="{0 if k % 2 else 80}" y1="{k * 4}" x2="{80 if k % 2 else 0}" y2="{k * 4}" stroke="#000"/>'
                        for k in range(20))
        dup = '<line x1="0" y1="0" x2="80" y2="0" stroke="#000"/>'
        self.svg = self.tmp / "zig.svg"
        self.svg.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="80mm" height="80mm" viewBox="0 0 80 80">'
                            f'{lines}{dup}</svg>')

    def dry(self, *flags):
        pv = self.tmp / f"pv{len(flags)}{'_'.join(flags)}.json".replace("-", "")
        p = subprocess.run(PLOT + [str(self.svg), "--mode", "dry", "--preview-json", str(pv), *flags],
                           capture_output=True, text=True, timeout=120)
        self.assertTrue(pv.exists(), p.stdout + p.stderr)
        return json.loads(pv.read_text()), p.stdout + p.stderr

    def test_optimize_dedupe_and_start_at(self):
        plain, _ = self.dry()
        opt, out = self.dry("--optimize", "--dedupe")
        s0, s1 = plain["summary"], opt["summary"]
        self.assertEqual(s0["strokes"], 21)
        self.assertEqual(s1["strokes"], 20)
        self.assertEqual(s1["duplicates_dropped"], 1)
        self.assertTrue(s1["optimized"])
        self.assertLess(s1["travel_mm"], s1["file_order"]["travel_mm"])
        self.assertLess(s1["est_seconds"], s1["file_order"]["est_seconds"])
        self.assertIn("Path planning", out)
        # the plan stays inside the same box
        self.assertEqual([round(v, 1) for v in s0["bbox_machine_mm"]], [round(v, 1) for v in s1["bbox_machine_mm"]])
        res, out = self.dry("--optimize", "--dedupe", "--start-at", "5")
        self.assertEqual(len(res["order"]), 15)
        self.assertEqual(res["order"], opt["order"][5:])
        self.assertEqual(res["summary"]["total_in_order"], 20)
        p = subprocess.run(PLOT + [str(self.svg), "--mode", "dry", "--start-at", "25"], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("has 21 strokes", p.stdout + p.stderr)

    def test_optimized_plot_runs_in_sim(self):
        p = subprocess.run(PLOT + [str(self.svg), "--mode", "plot", "--sim", "--optimize", "--dedupe", "--start-at", "3"],
                           input="PLOT\n", capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr)
        self.assertIn("Status: complete", p.stdout)


if __name__ == "__main__":
    unittest.main()
