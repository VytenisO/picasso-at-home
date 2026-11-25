"""The pen-check button (hardware.PenButton) against a fake EBB: press once for pen down with the motors locked,
again for pen up with them released; presses during a job or while held are dropped; no job starts while down."""
import pathlib
import sys
import threading
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from plotweb import hardware  # noqa: E402


class FakeEBB:
    def __init__(self):
        self.latched = False     # QB: pressed since the last read
        self.held = False        # B0 low right now
        self.sent = []
        self._out = []

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
        elif line == "PI,B,0":
            self._out += [f"PI,{0 if self.held else 1}"]
        else:
            self._out += ["OK"]

    def readline(self):
        return (self._out.pop(0) + "\r\n").encode() if self._out else b""


class Runner:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = False

    def busy(self):
        return self.active


class TestPenButton(unittest.TestCase):
    def setUp(self):
        settings = types.SimpleNamespace(machine=lambda: {"servo": {"pen_up": 14000, "pen_down": 13040}})
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

    def test_two_presses(self):
        self.tick()
        self.assertIsNone(self.tick())
        self.ebb.press()
        self.assertEqual(self.tick(), "down")
        self.assertEqual(self.ebb.sent[1:], ["EM,1,1", "SC,4,14000", "SC,5,13040", "SP,0,300"])
        self.assertIsNotNone(self.b.hold_reason())
        self.tick()                          # re-arms: the button is up again
        self.assertIsNone(self.tick())
        self.ebb.press()
        self.assertEqual(self.tick(), "up")
        self.assertEqual(self.ebb.sent[1:], ["SP,1,300", "EM,0,0"])
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
