"""Velocity planning (tools/motion.py) and the driver's use of it, simulator only: corners and U-turns slow
down, straight runs reach the speed, no leg goes faster than asked or changes speed faster than ACCEL."""
import json
import math
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import motion  # noqa: E402
import plot_svg  # noqa: E402


class TestPlan(unittest.TestCase):
    def test_u_turn_stops_and_straight_run_reaches_speed(self):
        v = motion.plan([(0, 0), (50, 0), (0, 0)], 80)
        self.assertEqual(v[1], 0.0)                                   # a full reversal comes to rest
        t = motion.path_time([(0, 0), (100, 0)], 50)
        self.assertAlmostEqual(t, 100 / 50 + 50 / motion.ACCEL, places=6)   # cruise plus the two half ramps

    def test_corners_by_sharpness(self):
        right = motion.plan([(0, 0), (20, 0), (20, 20)], 80)[1]
        self.assertLess(right, 10)                                    # a right angle is slow
        bend = motion.plan([(0, 0), (20, 0), (40, 1)], 80)[1]          # under 3 degrees
        self.assertGreater(bend, 60)
        hatch = motion.plan([(0, 0), (40, 0), (40, 0.8), (0, 0.8)], 80)
        self.assertTrue(all(s < 10 for s in hatch[1:3]))              # zig-zag turnarounds

    def test_pieces_keep_speed_and_accel(self):
        for length, v0, v1, vmax in ((30, 0, 0, 60), (2, 0, 0, 60), (30, 10, 0, 60), (0.3, 4, 4, 60), (80, 0, 25, 25)):
            ps = motion.pieces(length, v0, v1, vmax)
            self.assertAlmostEqual(ps[-1][0], length, places=9)
            self.assertAlmostEqual(sum(dt for _, dt in ps), motion.leg_time(length, v0, v1, vmax), places=9)
            prev_d, prev_v = 0.0, v0
            for d, dt in ps:
                sp = (d - prev_d) / dt
                self.assertLessEqual(sp, vmax + 1e-6)
                self.assertLessEqual(abs(sp - prev_v), motion.ACCEL * dt + 1e-6 if dt < 1 else vmax)
                prev_d, prev_v = d, sp


class TestDriver(unittest.TestCase):
    def plotter(self):
        cfg = json.loads((ROOT / "config" / "machine.json").read_text())
        args = types.SimpleNamespace(sim=True, track=False, start_x=10.0, start_y=10.0, port=None, ctl=None, sim_speed=0)
        p = plot_svg.Plotter(cfg, args)
        sent = []
        p._sm = lambda a1, a2, ms: sent.append((a1, a2, ms))
        return p, sent, cfg

    def test_zigzag_legs_ramp_and_land_on_target(self):
        p, sent, cfg = self.plotter()
        spmm = (cfg["steps_per_mm"]["x"], cfg["steps_per_mm"]["y"])
        st = lambda x, y: (round(x * spmm[0]), round(y * spmm[1]))
        path = [st(40, 0), st(40, 1), st(0, 1), st(0, 2), st(40, 2)]
        p.move_path(path, 80)
        self.assertEqual(tuple(p.pos), path[-1])
        speeds = [math.hypot(a / spmm[0], b / spmm[1]) / (ms / 1000) for a, b, ms in sent]   # axis mapping keeps length
        self.assertLessEqual(max(speeds), 80 * 1.08)                 # whole-ms rounding
        self.assertLess(speeds[0], 15)                                # starts slow, not at full speed
        self.assertGreater(max(speeds), 60)                           # but gets there on the long legs
        self.assertGreater(len(sent), 20)

    def test_estimate_counts_the_slow_corners(self):
        zig = [[(40.0 * (k % 4 in (1, 2)), 0.8 * (k // 2)) for k in range(40)]]   # 20 hatch lines, 40 mm each
        drawn, travel, _, secs = plot_svg.estimate(zig, None, 60, 100, 0)
        self.assertGreater(drawn, 790)
        self.assertGreater(secs, (drawn / 60 + travel / 100) * 1.2)   # every turnaround costs a stop and a ramp


if __name__ == "__main__":
    unittest.main()
