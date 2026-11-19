"""Plotter driver on firmware 3.x: probe, instant stop with ES, position read back with QS; 2.0.1 unchanged."""
import argparse
import json
import pathlib
import sys
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import plot_svg  # noqa: E402


class FakeEBB:
    def __init__(self, version, qs=(0, 0)):
        self.version, self.qs, self.sent, self.out = version, qs, [], []
        self.timeout = 3

    def reset_input_buffer(self):
        self.out = []

    def write(self, raw):
        cmd = raw.decode().strip()
        self.sent.append(cmd)
        name = cmd.split(",")[0]
        new = self.version >= (3, 0, 0)
        if name == "V":
            self.out = ["EBBv13_and_above EB Firmware Version " + ".".join(map(str, self.version))]
        elif name in ("ES", "QS", "QM") and not new:
            self.out = [f"!8 Err: Unknown command '{name}:XXXX'"]
        elif name == "QS":
            self.out = [f"{self.qs[0]},{self.qs[1]}", "OK"]
        elif name == "ES":
            self.out = ["1,120,80,300,200", "OK"]
        else:
            self.out = ["OK"]

    def readline(self):
        return (self.out.pop(0) + "\r\n").encode() if self.out else b""


class Ctl:
    def __init__(self):
        self.stop = threading.Event()

    def check_stop(self):
        if self.stop.is_set():
            raise KeyboardInterrupt()


def plotter(ebb, ctl=None):
    cfg = json.loads((ROOT / "config" / "machine.json").read_text())
    args = argparse.Namespace(sim=True, track=False, start_x=0.0, start_y=0.0, port=None, sim_speed=0, ctl=ctl)
    p = plot_svg.Plotter(cfg, args)
    p.sim, p.ser = False, ebb
    p.probe()
    return p


class DriverStop(unittest.TestCase):
    def test_new_firmware_probe_and_setup_clears_counters(self):
        ebb = FakeEBB((3, 1, 7))
        p = plotter(ebb)
        self.assertEqual(p.caps, {"ES", "QS"})
        ebb.sent.clear()
        p.setup()
        self.assertEqual(ebb.sent[:2], ["EM,1,1", "CS"])

    def test_stop_sends_es_then_reads_position(self):
        ebb = FakeEBB((3, 1, 7))
        ctl = Ctl()
        p = plotter(ebb, ctl)
        ax = p.ax
        # board counters: 350 steps on the X motor, 175 on the Y motor (as the axis map lays them out)
        m = [0, 0]
        m[ax[0]["ebb_axis"] - 1] = 350 * ax[0]["sign"]
        m[ax[1]["ebb_axis"] - 1] = 175 * ax[1]["sign"]
        ebb.qs = tuple(m)
        ebb.sent.clear()
        ctl.stop.set()
        self.assertTrue(p.shutdown())
        self.assertEqual(ebb.sent[0], "ES")
        self.assertIn("QS", ebb.sent)
        self.assertEqual(ebb.sent[-1], "EM,0,0")
        self.assertEqual(p.pos, [350, 175])
        self.assertTrue(p.readback)

    def test_normal_end_does_not_e_stop(self):
        ebb = FakeEBB((3, 1, 7))
        p = plotter(ebb, Ctl())
        ebb.sent.clear()
        self.assertTrue(p.shutdown())
        self.assertNotIn("ES", ebb.sent)
        self.assertIn("QS", ebb.sent)

    def test_old_firmware_unchanged(self):
        ebb = FakeEBB((2, 0, 1))
        ctl = Ctl()
        p = plotter(ebb, ctl)
        self.assertEqual(p.caps, set())
        ebb.sent.clear()
        ctl.stop.set()
        self.assertTrue(p.shutdown())
        self.assertEqual(ebb.sent, ["SP,1,300", "EM,0,0"])
        self.assertFalse(p.readback)

    def test_stop_cuts_a_wait_short(self):
        import time
        ebb = FakeEBB((3, 1, 7))
        ctl = Ctl()
        p = plotter(ebb, ctl)
        threading.Timer(0.1, ctl.stop.set).start()
        t0 = time.monotonic()
        p.sleep(5)
        self.assertLess(time.monotonic() - t0, 1.0)
        t0 = time.monotonic()
        p.sleep(0.3, interruptible=False)       # cleanup waits are never cut short
        self.assertGreaterEqual(time.monotonic() - t0, 0.29)


if __name__ == "__main__":
    unittest.main()
