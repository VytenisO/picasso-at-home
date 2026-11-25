"""The pen-check button (hardware.PenButton) against a fake EBB: press once for pen down with the motors locked,
again for pen up with them released; presses during a job or while held are dropped; no job starts while down.
Also the pen-depth knob (penpot): the reading to pen-down, a loose wire ignored, the knob followed during a pen check
and by the plot driver between strokes."""
import pathlib
import sys
import threading
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from plotweb import hardware  # noqa: E402
import penpot  # noqa: E402
import plot_svg  # noqa: E402


class FakeEBB:
    def __init__(self):
        self.latched = False     # QB: pressed since the last read
        self.held = False        # B0 low right now
        self.sent = []
        self._out = []
        self.knob = [330]        # A1 readings, the last one repeats
        self.ac = False

    def press(self, hold=False):
        self.latched, self.held = True, hold

    def reset_input_buffer(self):
        self._out = []

    def write(self, data):
        line = data.decode().strip()
        self.sent.append(line)
        if line == "QB":
            self._out += [str(int(self.latched)), "OK"]
            self.latched = self.held
        elif line == "A":
            v = self.knob.pop(0) if len(self.knob) > 1 else self.knob[0]
            self._out += [f"A,0:582,{'1:%d,' % v if self.ac else ''}11:293"]
        elif line == "AC,1,1":
            self.ac = True
            self._out += ["OK"]
        elif line == "PI,B,0":
            self._out += [f"PI,{0 if self.held else 1}"]
        else:
            self._out += ["OK"]

    def readline(self):
        return (self._out.pop(0) + "\r\n").encode() if self._out else b""


class Port(FakeEBB):
    """For plot_svg.Plotter: bytes in, lines out, and a timeout it sets."""
    timeout = 3


class Runner:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = False

    def busy(self):
        return self.active


class TestPenButton(unittest.TestCase):
    def setUp(self):
        self.cfg = {"servo": {"pen_up": 14000, "pen_down": 13040}}
        settings = types.SimpleNamespace(machine=lambda: self.cfg)
        self.logs = []
        self.runner = Runner()
        self.b = hardware.PenButton(settings, self.runner, threading.Lock(), lambda lvl, t: self.logs.append(t))
        self.ebb = FakeEBB()
        self.t = 100.0
        hardware.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=lambda: self.t,
                                              strftime=hardware.time.strftime)

    def tearDown(self):
        import time
        hardware.time = time

    def tick(self, dt=0.2):
        self.t += dt
        self.ebb.sent.clear()
        return self.b.poll(self.ebb, self.t)

    def moves(self):
        return [c for c in self.ebb.sent if c.split(",")[0] in ("EM", "SC", "SP", "S2")]

    def test_two_presses(self):
        self.tick()
        self.assertIsNone(self.tick())
        self.ebb.press()
        self.assertEqual(self.tick(), "down")
        self.assertEqual(self.moves(), ["EM,1,1", "SC,4,14000", "SC,5,13040", "SP,0,300"])
        self.assertIsNotNone(self.b.hold_reason())
        self.tick()                          # re-arms: the button is up again
        self.assertIsNone(self.tick())
        self.ebb.press()
        self.assertEqual(self.tick(), "up")
        self.assertEqual(self.moves(), ["SP,1,300", "EM,0,0"])
        self.assertIsNone(self.b.hold_reason())

    def test_long_hold_is_one_press(self):
        self.tick()
        self.ebb.press(hold=True)
        self.assertEqual(self.tick(), "down")
        for _ in range(5):
            self.assertIsNone(self.tick())   # still held: stays down
        self.ebb.held = False
        self.tick()
        self.ebb.press()
        self.assertEqual(self.tick(), "up")

    def test_press_during_a_job_is_dropped(self):
        self.tick()
        self.ebb.press()                     # pressed while a job had the port: no reads for a while
        self.assertIsNone(self.tick(5.0))
        self.assertIsNone(self.tick())
        self.runner.active = True
        self.ebb.press()
        self.assertIsNone(self.tick())       # a job reserved between the busy check and the press
        self.assertFalse(self.b.down)

    def test_auto_raise(self):
        self.tick()
        self.ebb.press()
        self.tick()
        self.tick()
        self.assertEqual(self.tick(hardware.BUTTON_AUTO_UP + 1), "up")
        self.assertFalse(self.b.down)


    def test_knob_read_when_off_used_when_on(self):
        self.tick()
        self.assertEqual(self.b.pot["raw"], 330)              # read (channel switched on), shown, not used
        self.assertIsNone(self.b.pot["pen_down"])
        self.assertIn("AC,1,1", self.ebb.sent)
        self.cfg["pen_pot"] = {"enabled": True, "adc_shallow": 0, "adc_deep": 640, "depth_max": 1440}
        self.tick()
        self.assertEqual(self.b.pot["pen_down"], 14000 - round(330 / 640 * 1440))
        self.ebb.press()
        self.tick()
        self.assertIn(f"SC,5,{self.b.pot['pen_down']}", self.moves())
        self.tick()
        self.ebb.knob = [600]                                   # turned deeper during the pen check
        self.tick()
        v = 14000 - round(600 / 640 * 1440)
        self.assertEqual(self.moves(), [f"SC,5,{v}", f"S2,{v},4"])
        self.assertIsNone(self.tick())                          # no repeat while it stays put
        self.assertEqual(self.moves(), [])

    def test_loose_knob_is_ignored(self):
        self.cfg["pen_pot"] = {"enabled": True, "adc_shallow": 0, "adc_deep": 640, "depth_max": 1440}
        self.ebb.knob = [100, 400, 250] * 20
        self.tick()
        self.assertIsNone(self.b.pot["pen_down"])
        self.assertIn("unsteady", self.b.pot["problem"])
        self.tick()
        self.ebb.press()
        self.tick()
        self.assertIn("SC,5,13040", self.moves())               # the saved pen-down


class TestKnobMaths(unittest.TestCase):
    servo = {"pen_up": 14000, "pen_down": 13040}

    def test_ends_and_reverse(self):
        pot = {"adc_shallow": 0, "adc_deep": 640, "depth_max": 1440}
        self.assertEqual(penpot.pen_down(self.servo, pot, 0), 14000)
        self.assertEqual(penpot.pen_down(self.servo, pot, 640), 12560)
        self.assertEqual(penpot.pen_down(self.servo, pot, 700), 12560)          # clamped at the deep end
        rev = {"adc_shallow": 640, "adc_deep": 0, "depth_max": 1440}
        self.assertEqual(penpot.pen_down(self.servo, rev, 640), 14000)
        self.assertEqual(penpot.pen_down({"pen_up": 14000, "pen_down": 15000}, pot, 640), 15440)   # lift the other way

    def test_judge(self):
        pot = {"adc_shallow": 0, "adc_deep": 640}
        self.assertEqual(penpot.judge([330, 331, 330], pot), (330, None))
        self.assertIsNone(penpot.judge([330, 500, 330], pot)[0])
        self.assertIsNone(penpot.judge([900, 900, 900], pot)[0])               # past the deep end: shorted?
        self.assertIsNone(penpot.judge([330, None, 330], pot)[0])
        self.assertEqual(penpot.parse("A,0:582,1:330,11:293", 1), 330)
        self.assertIsNone(penpot.parse("A,0:582,11:293", 1))


class TestDriverFollowsKnob(unittest.TestCase):
    def test_next_pen_down_follows_the_knob(self):
        import json
        cfg = json.loads((ROOT / "config" / "machine.json").read_text())
        cfg["servo"] = {"pen_up": 14000, "pen_down": 13040}
        cfg["pen_pot"] = {"enabled": True, "channel": 1, "adc_shallow": 0, "adc_deep": 640, "depth_max": 1440}
        args = types.SimpleNamespace(sim=True, track=False, start_x=10.0, start_y=10.0, port=None, ctl=None, sim_speed=0)
        p = plot_svg.Plotter(cfg, args)
        port = Port()
        p.sim, p.ser, p.pot = False, port, penpot.active(cfg)  # the real-port path, against the fake board
        p.idle = lambda: None
        p.sleep = lambda s, interruptible=True: None
        p.track = lambda force=False: None
        p.setup()
        self.assertIn("SC,5,%d" % (14000 - round(330 / 640 * 1440)), port.sent)
        p.set_pressure(0.5)
        port.knob = [600]
        port.sent.clear()
        p.pen_t = 0
        p.pot_t = -1e9
        p.pen(False)
        v = 14000 - round(600 / 640 * 1440)
        half = round(v + 0.5 * (14000 - v))
        self.assertEqual([c for c in port.sent if c[:2] in ("SC", "SP")], [f"SC,5,{half}", "SP,0,250"])


class TestReserveRefused(unittest.TestCase):
    def test_no_real_job_while_down(self):
        from plotweb import runner as R
        jr = R.JobRunner.__new__(R.JobRunner)
        jr.lock, jr.job, jr.hold = threading.RLock(), None, lambda: "pen down"
        jr._log = jr._emit_state = lambda *a, **k: None
        self.assertEqual(jr.reserve("plot", "plot", False), (False, "pen down"))
        ok, _ = jr.reserve("plot", "simulate", True)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
