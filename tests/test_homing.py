"""Switch homing in jog.py, on the simulator (its switches sit at machine zero)."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(cfg, commands):
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "machine.json").write_text(json.dumps(cfg))
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "jog.py"), "--sim", "--control", "--config",
                        str(tmp / "machine.json")], input="\n".join(commands) + "\n", capture_output=True, text=True,
                       timeout=120)
    events = [json.loads(ln) for ln in p.stdout.splitlines() if ln.startswith("{")]
    return events, p.stdout


class Homing(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config" / "machine.json").read_text())

    def test_homes_to_the_switches_then_goes_to_s(self):
        cfg = dict(self.cfg, homing={"x": {"pin": "B3"}, "y": {"pin": "B4"}, "active": 0, "speed": 20})
        events, out = run(cfg, ["MOVE 30 20", "HOMESW", "QUIT"])
        kinds = [e.get("event") for e in events]
        self.assertIn("homed", kinds, out[-500:])
        res = json.loads(next(ln for ln in out.splitlines() if ln.startswith("__RESULT__"))[11:])
        self.assertTrue(res["at_start"])

    def test_without_switches_it_says_so(self):
        cfg = {k: v for k, v in self.cfg.items() if k != "homing"}
        events, _ = run(cfg, ["HOMESW", "QUIT"])
        self.assertTrue(any("No homing switches" in (e.get("message") or "") for e in events))

    def test_bad_pin_is_refused(self):
        cfg = dict(self.cfg, homing={"x": {"pin": "Z9"}, "y": {"pin": "B4"}})
        events, _ = run(cfg, ["HOMESW", "QUIT"])
        self.assertTrue(any("pin must be like" in (e.get("message") or "") for e in events))


if __name__ == "__main__":
    unittest.main()
