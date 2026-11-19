"""settle_servo: put the pen positions into a freshly reset EBB, and only then."""
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
from plotweb import hardware  # noqa: E402

SERVO = {"pen_up": 14000, "pen_down": 13040}


class FakeEBB:
    """Answers like firmware 3.x in legacy mode; ql is the SL/QL RAM byte."""

    def __init__(self, version="3.1.7", ql=0):
        self.version, self.ql, self.sent, self.out = version, ql, [], []

    def reset_input_buffer(self):
        self.out = []

    def write(self, raw):
        cmd = raw.decode().strip()
        self.sent.append(cmd)
        name = cmd.split(",")[0]
        if name == "V":
            self.out = [f"EBBv13_and_above EB Firmware Version {self.version}"]
        elif name == "QL":
            self.out = [str(self.ql), "OK"]
        elif name == "SL":
            self.ql = int(cmd.split(",")[1])
            self.out = ["OK"]
        else:
            self.out = ["OK"]

    def readline(self):
        return (self.out.pop(0) + "\r\n").encode() if self.out else b""


class SettleServo(unittest.TestCase):
    def test_fresh_board_gets_positions_then_mark(self):
        ebb = FakeEBB(ql=0)
        msg = hardware.settle_servo(ebb, SERVO)
        self.assertIn("14000", msg)
        self.assertEqual(ebb.sent, ["V", "QL", "SC,4,14000", "SC,5,13040", "S2,14000,4,100", "SL,1"])
        self.assertEqual(ebb.ql, 1)

    def test_board_already_set_is_left_alone(self):
        ebb = FakeEBB(ql=1)
        self.assertIsNone(hardware.settle_servo(ebb, SERVO))
        self.assertEqual(ebb.sent, ["V", "QL"])

    def test_old_firmware_is_left_alone(self):
        ebb = FakeEBB(version="2.0.1")
        self.assertIsNone(hardware.settle_servo(ebb, SERVO))
        self.assertEqual(ebb.sent, ["V"])

    def test_refused_command_raises(self):
        ebb = FakeEBB()
        real = ebb.write

        def write(raw):
            real(raw)
            if raw.startswith(b"S2"):
                ebb.out = ["!8 Err: Unknown command"]
        ebb.write = write
        with self.assertRaises(RuntimeError):
            hardware.settle_servo(ebb, SERVO)

    def test_version_parse(self):
        self.assertEqual(hardware.firmware_version("EBBv13_and_above EB Firmware Version 3.1.7"), (3, 1, 7))
        self.assertIsNone(hardware.firmware_version("garbage"))


class GuardThread(unittest.TestCase):
    def test_runs_as_a_thread_and_settles_a_fresh_board(self):
        import threading
        import time
        from unittest import mock
        ebb = FakeEBB(ql=0)
        logs = []

        class Ctx:
            def __enter__(self):
                return ebb

            def __exit__(self, *a):
                return False

        settings = mock.Mock()
        settings.machine.return_value = {"servo": SERVO}
        runner = mock.Mock()
        runner.busy.return_value = False
        port = mock.patch.object(hardware, "find_port", return_value="/dev/fake")
        ser = mock.patch.object(hardware, "serial", mock.Mock(Serial=lambda *a, **k: Ctx()))
        with port, ser:
            g = hardware.ServoGuard(settings, runner, threading.Lock(), lambda lvl, msg: logs.append((lvl, msg)),
                                    interval=0.05)
            g.start()
            time.sleep(0.8)
        self.assertTrue(g.is_alive())
        self.assertIn("SL,1", ebb.sent)
        self.assertTrue(any("switched on" in m for _, m in logs), logs)


if __name__ == "__main__":
    unittest.main()
