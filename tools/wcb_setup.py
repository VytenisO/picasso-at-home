#!/usr/bin/env python3
"""WaterColorBot jog + calibration, one guided run. Needs pyserial.

Phases: connect -> pen servo up/down -> axis probe (direction, scale) ->
calibration L-shape (steps/mm per axis) -> return/backlash dot.
Every step is logged and the results are written to results/wcb_setup_<ts>.json
(also on abort or crash), so a partial run is still readable.

Safety: small slow moves only, pen up for all probes, ES + pen up + motors off on
Ctrl-C or any error. There are no limit switches: you place the carriage by hand.
Run with --sim to dry-run the logic with no hardware.
"""
import argparse
import datetime
import json
import math
import pathlib
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

EBB_VID, EBB_PID = 0x04D8, 0xFD92
SERVO_MIN, SERVO_MAX = 7500, 22000   # PWM units of 83.3 ns, i.e. ~0.6-1.8 ms
RATE_CAP = 1000                       # steps/s ceiling for every move
MAX_MOVE = 8000                       # steps, single-move ceiling
PROBE_STEPS = 500
TARGET_MM = 40.0                      # calibration line length aimed for
DIRS = {"r": ("x", 1), "l": ("x", -1), "d": ("y", 1), "u": ("y", -1)}

OUT = pathlib.Path(__file__).resolve().parent.parent / "results"
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
LOGF = None
R = {"started": STAMP, "phases": {}}


def log(msg, echo=True):
    line = f"{datetime.datetime.now():%H:%M:%S} {msg}"
    if echo:
        print(msg, flush=True)
    if LOGF:
        LOGF.write(line + "\n")
        LOGF.flush()


def ask(prompt, default=""):
    try:
        s = input(f"\n{prompt} ")
    except EOFError:
        raise SystemExit("input closed, aborting")
    log(f"  > {prompt.strip()} :: {s!r}", echo=False)
    return s.strip() or default


class EBB:
    def __init__(self, port, sim):
        self.sim = sim
        self.pos = [0, 0]
        self.caps = {}
        self.sim_old = sim == "old"
        self.s = None if sim else serial.Serial(port, 9600, timeout=2)

    def cmd(self, line, ok=True):
        log(f"  -> {line}", echo=False)
        if self.sim:
            name = line.split(",")[0]
            if self.sim_old and name in ("QM", "QS", "ES"):
                out = [f"!8 Err: Unknown command '{name}:XXXX'"]
            else:
                out = {"V": ["SIM EBB"], "QM": ["QM,0,0,0,0"], "QS": [f"{self.pos[0]},{self.pos[1]}", "OK"]}.get(name, ["OK"])
        else:
            self.s.write(line.encode() + b"\r")
            out = []
            while True:
                r = self.s.readline().decode(errors="replace").strip()
                if not r:
                    break
                out.append(r)
                # read to the end of the reply: 3.x answers QS/ES with data then OK; V and QM end on the timeout
                if r == "OK" or r.startswith("!"):
                    break
        log(f"  <- {out}", echo=False)
        if ok and not self.sim and "OK" not in out:
            raise RuntimeError(f"no OK for {line!r}: {out}")
        return out

    def probe_caps(self):
        """Old firmware (e.g. 2.0.1) lacks QM/QS/ES; detect instead of assuming."""
        self.caps = {}
        for c in ("QM", "QS", "ES"):
            out = self.cmd(c, ok=False)
            self.caps[c] = not any(x.startswith("!") for x in out)
        log(f"Firmware command support: {self.caps}")
        return self.caps

    def wait_idle(self, est_ms):
        time.sleep(0 if self.sim else est_ms / 1000 + 0.25)
        if not self.caps.get("QM"):
            return   # timed wait only; est_ms is the move/servo duration
        for _ in range(100):
            q = self.cmd("QM", ok=False)
            f = next((x for x in q if x.startswith("QM")), "QM,0,0,0,0").split(",")[1:]
            if all(v.strip() == "0" for v in f):
                return
            time.sleep(0.1)

    def pen(self, up, delay=400):
        self.cmd(f"SP,{1 if up else 0},{delay}")
        self.wait_idle(delay)

    def move(self, a1, a2):
        if max(abs(a1), abs(a2)) > MAX_MOVE:
            raise ValueError(f"move {a1},{a2} exceeds {MAX_MOVE} steps")
        ms = max(200, math.ceil(max(abs(a1), abs(a2)) / RATE_CAP * 1000))
        self.cmd(f"SM,{ms},{a1},{a2}")
        self.wait_idle(ms)
        self.pos[0] += a1
        self.pos[1] += a2


def find_port():
    for p in list_ports.comports():
        if (p.vid, p.pid) == (EBB_VID, EBB_PID):
            return p.device
    return None


def nudge(e, label, val, lo, hi, apply, big=500, small=100):
    """Interactive servo nudge. + / - small step, a / z big step, Enter accepts."""
    while True:
        k = ask(f"{label} = {val}.  [+/-] +/-{small}  [a/z] +/-{big}  [Enter] accept:")
        if k == "":
            return val
        step = {"+": small, "-": -small, "a": big, "z": -big}.get(k)
        if step is None:
            continue
        val = max(lo, min(hi, val + step))
        apply(val)


def phase_servo(e):
    log("\n== PHASE 1: pen servo ==")
    log("Mount the pen. Servo values are clamped to "
        f"{SERVO_MIN}..{SERVO_MAX}. Both start equal so the first SP does not move anything.")
    ask("Carriage anywhere over paper, pen mounted. Press Enter to start the servo.")
    up = 14000
    e.cmd(f"SC,4,{up}")
    e.cmd(f"SC,5,{up}")
    e.pen(True)

    def set_up(v):
        e.cmd(f"SC,4,{v}")
        e.cmd(f"SC,5,{v}")
        e.pen(True, 500)

    log("Adjust PEN-UP: the pen must clear the paper by a few mm. Higher value moves one way, lower the other.")
    up = nudge(e, "pen-up", up, SERVO_MIN, SERVO_MAX, set_up, big=800, small=200)
    log("Adjust PEN-DOWN: nudge slowly until the tip just touches the paper with light spring pressure.")
    down = up

    def set_down(v):
        e.cmd(f"SC,5,{v}")
        e.pen(False, 500)

    log("Starting down-move from the up position in small steps; use the direction that lowers the pen.")
    down = nudge(e, "pen-down", down, SERVO_MIN, SERVO_MAX, set_down, big=300, small=60)
    e.pen(True, 500)
    R["phases"]["servo"] = {"pen_up": up, "pen_down": down}
    log(f"Servo done: up={up} down={down}")


def phase_probe(e):
    log("\n== PHASE 2: axis probe (pen up) ==")
    ask("Motors are OFF. Slide the carriage by hand to a spot about 2 cm in from the top-left corner "
        "of the paper area (call it S). Pen up. Press Enter to energise the motors.")
    e.cmd("EM,1,1")   # both motors, 1/16 microstepping
    e.cmd("CS")
    e.pos = [0, 0]
    e.pen(True, 300)
    probe = {}
    for ax in (1, 2):
        mv = (PROBE_STEPS, 0) if ax == 1 else (0, PROBE_STEPS)
        e.move(*mv)
        d = ask(f"Axis {ax} moved +{PROBE_STEPS} steps. Which way did the pen carriage go? "
                "[r]ight [l]eft [d]own-page(toward you) [u]p-page(away) [n]one:").lower()
        if d not in DIRS:
            R["phases"]["probe"] = {"error": f"axis {ax} reported {d!r}"}
            raise RuntimeError(f"axis {ax}: no usable motion reported ({d!r}); check belts, power, motor cables")
        mm = float(ask("About how many mm did it move (rough ruler estimate)?"))
        if mm <= 0:
            raise RuntimeError("zero movement reported")
        probe[ax] = {"dir": d, "mm": mm, "steps_per_mm_est": PROBE_STEPS / mm}
        e.move(-mv[0], -mv[1])
        log(f"axis {ax}: +steps -> {d}, ~{PROBE_STEPS / mm:.1f} steps/mm (rough)")
    names = {DIRS[probe[a]["dir"]][0] for a in probe}
    if len(names) != 2:
        R["phases"]["probe"] = probe
        raise RuntimeError("both axes moved along the same world axis (mixed-axis machine?), stopping")
    R["phases"]["probe"] = probe
    return probe


def phase_calibrate(e, probe):
    log("\n== PHASE 3: calibration L-shape ==")
    wmap = {}   # world axis -> (ebb axis index 0/1, step sign)
    for ax, p in probe.items():
        name, sign = DIRS[p["dir"]]
        wmap[name] = (ax - 1, sign, p["steps_per_mm_est"])
    n = {}
    for name in ("x", "y"):
        est = wmap[name][2]
        n[name] = int(max(800, min(6000, round(TARGET_MM * est))))
    log(f"Planned lines: X {n['x']} steps, Y {n['y']} steps (aim ~{TARGET_MM:.0f} mm each).")
    ask("Carriage must still be at S, with clear paper to the right and below it for the lines. "
        "Press Enter to draw the L (X line to the right, then Y line toward you).")

    def vec(name, steps):
        i, s, _ = wmap[name]
        v = [0, 0]
        v[i] = steps * s
        return v

    for name in ("x", "y"):
        e.pen(False, 500)
        e.move(*vec(name, n[name]))
        e.pen(True, 500)
        e.move(*vec(name, -n[name]))
    # backlash: out and back on both axes, then drop a dot at S
    for name in ("x", "y"):
        e.move(*vec(name, n[name] // 2))
    for name in ("y", "x"):
        e.move(*vec(name, -(n[name] // 2)))
    e.pen(False, 500)
    e.pen(True, 500)
    qs = e.cmd("QS") if e.caps.get("QS") else "unsupported on this firmware"
    R["phases"]["calibration_planned"] = {"steps": n, "wmap": {k: list(v[:2]) for k, v in wmap.items()},
                                          "step_counter_after": qs, "software_position": e.pos}
    OUT.joinpath(f"wcb_setup_{STAMP}.json").write_text(json.dumps(R, indent=2))   # survive a later crash
    log("Drawn. Measure with a ruler, in mm, edge of start dot to far end of each line.")
    lx = float(ask("Length of the X line (mm):"))
    ly = float(ask("Length of the Y line (mm):"))
    bl = ask("Return dot offset from the L corner as 'dx dy' in mm (+right, +toward you); Enter if they coincide:", "0 0")
    dx, dy = (float(v) for v in (bl.split() + ["0", "0"])[:2])
    cal = {
        "x": {"ebb_axis": wmap["x"][0] + 1, "step_sign": wmap["x"][1], "steps": n["x"], "measured_mm": lx,
              "steps_per_mm": n["x"] / lx, "steps_per_inch": n["x"] / lx * 25.4},
        "y": {"ebb_axis": wmap["y"][0] + 1, "step_sign": wmap["y"][1], "steps": n["y"], "measured_mm": ly,
              "steps_per_mm": n["y"] / ly, "steps_per_inch": n["y"] / ly * 25.4},
        "return_offset_mm": {"dx": dx, "dy": dy},
        "microstep_mode": "EM,1,1 (1/16)",
    }
    R["phases"]["calibration"] = cal
    log(f"X: {cal['x']['steps_per_mm']:.2f} steps/mm ({cal['x']['steps_per_inch']:.0f}/in)   "
        f"Y: {cal['y']['steps_per_mm']:.2f} steps/mm ({cal['y']['steps_per_inch']:.0f}/in)   "
        f"return offset {dx},{dy} mm")


def main():
    global LOGF
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--sim", nargs="?", const="new", choices=["new", "old"],
                    help="no hardware, logic dry-run; 'old' mimics firmware 2.0.1")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    LOGF = open(OUT / f"wcb_setup_{STAMP}.log", "w")
    e = None
    try:
        if not a.sim:
            if serial is None:
                raise SystemExit("pyserial missing: pacman -S python-pyserial")
            port = a.port or find_port()
            if not port:
                raise SystemExit("No EBB (04d8:fd92) found. Plug in and power the plotter, check `lsusb`.")
        else:
            port = "SIM"
        e = EBB(port, a.sim or False)
        v = e.cmd("V", ok=False)
        R["port"], R["firmware"] = port, v
        log(f"Connected {port}: {v}")
        R["caps"] = e.probe_caps()
        e.cmd("EM,0,0")      # motors off until the user has placed the carriage
        phase_servo(e)
        probe = phase_probe(e)
        phase_calibrate(e, probe)
        R["status"] = "complete"
    except KeyboardInterrupt:
        log("Interrupted.")
        R["status"] = "interrupted"
    except SystemExit as ex:
        R["status"] = f"exit: {ex}"
        log(str(ex))
    except Exception as ex:
        R["status"] = f"error: {ex!r}"
        log(f"ERROR: {ex!r}")
    finally:
        if e:
            try:
                if e.caps.get("ES"):
                    e.cmd("ES", ok=False)
                e.cmd("SP,1,300")
                e.cmd("EM,0,0")
                R["final_step_counters"] = e.cmd("QS") if e.caps.get("QS") else "unsupported"
            except Exception as ex:
                log(f"cleanup problem: {ex!r}")
        path = OUT / f"wcb_setup_{STAMP}.json"
        path.write_text(json.dumps(R, indent=2))
        log(f"\nStatus: {R.get('status')}\nResults: {path}\nLog: {OUT / f'wcb_setup_{STAMP}.log'}")
    return 0 if R.get("status") == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
