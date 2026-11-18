#!/usr/bin/env python3
"""Home the carriage against the upper-left end stops (crash homing), then travel to the start point S.

There are no limit switches and firmware 2.0.1 has no position readback. Instead the carriage is driven slowly,
pen up, further than the full travel toward the upper-left. Wherever it started, it ends pressed against the
stops; for the rest of the move the motor skips steps (a buzz, harmless at low speed). That corner is machine
(0, 0), the origin that the paper position in config/machine.json is measured from.

  home.py                      X, then Y, then travel to S (the paper's top-left). Type HOME to confirm.
  home.py --axis x             one axis only (first hardware test). Stays at that stop; position stays unknown.
  home.py --to corner          stay in the stop corner instead of travelling to S
  home.py --dot                after arriving, tap a pen dot 10 mm right of and below S, then return to S.
                               Ten homes from random spots give ten dots: their spread is the repeatability.
  home.py --sim [--sim-from X,Y]   virtual machine, nothing sent; logs where the stops would be hit

Speed is capped at 15 mm/s (default 8). Ctrl-C (or STOP on stdin with --control) is honoured before the next
move chunk (at most 3 s); the pen is raised and the motors released. A stop before both axes are homed leaves the
position unknown. Motors are released at the end unless --hold is given.
"""
import argparse
import json
import pathlib
import signal
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import plot_svg as ps  # noqa: E402

DEFAULT_SPEED = 8.0     # mm/s into the stops
MAX_SPEED = 15.0        # hard ceiling: the stall force grows with speed
DEFAULT_OVERRUN = 10.0  # mm driven beyond the full travel, so any start point reaches the stop
DOT_OFFSET = 10.0       # mm right and down of the end point for --dot
AXES = {"x": 0, "y": 1}


def parse_point(s):
    try:
        x, y = (float(v) for v in s.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected X,Y in mm")
    return x, y


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--axis", choices=["both", "x", "y"], default="both")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED, help=f"mm/s into the stops (max {MAX_SPEED:g})")
    ap.add_argument("--overrun", type=float, default=DEFAULT_OVERRUN, help="mm beyond the full travel (0-50)")
    ap.add_argument("--to", choices=["start", "corner"], default="start", help="where to end after homing both axes")
    ap.add_argument("--travel-speed", type=float, default=50.0, help="mm/s from the corner to S")
    ap.add_argument("--dot", action="store_true", help="tap a dot near the end point (repeatability test)")
    ap.add_argument("--hold", action="store_true", help="keep the motors on at the end")
    ap.add_argument("--yes", action="store_true", help="skip the typed HOME confirmation")
    ap.add_argument("--config", default=str(ps.ROOT / "config" / "machine.json"))
    ap.add_argument("--port")
    ap.add_argument("--sim", action="store_true", help="virtual machine, nothing sent")
    ap.add_argument("--sim-speed", type=float, default=0.0, help="with --sim: real-time factor (0 = instant)")
    ap.add_argument("--sim-from", type=parse_point, default=None, help="with --sim: where the carriage starts, mm")
    ap.add_argument("--control", action="store_true", help="read STOP from stdin (web UI)")
    ap.add_argument("--track", action="store_true", help="print __POS__/__RESULT__ markers (web UI)")
    a = ap.parse_args()

    if not 0 < a.speed <= MAX_SPEED:
        sys.exit(f"--speed must be in (0, {MAX_SPEED:g}] mm/s")
    if not 0 <= a.overrun <= 50:
        sys.exit("--overrun must be in [0, 50] mm")
    if not 0 < a.travel_speed <= ps.MAX_TRAVEL_SPEED:
        sys.exit(f"--travel-speed must be in (0, {ps.MAX_TRAVEL_SPEED:g}] mm/s")
    if a.dot and a.axis != "both":
        sys.exit("--dot needs both axes homed")

    cfg = json.loads(pathlib.Path(a.config).read_text())
    travel = (cfg["travel_mm"]["x"], cfg["travel_mm"]["y"])
    paper = cfg.get("paper") or {}
    s_mm = (float(paper.get("x0", 0.0)), float(paper.get("y0", 0.0)))
    end_mm = s_mm if a.to == "start" else (0.0, 0.0)
    if not (0 <= end_mm[0] <= travel[0] and 0 <= end_mm[1] <= travel[1]):
        sys.exit(f"S {end_mm} mm is outside the travel {travel} mm: check paper.x0/y0 in the config")
    axes = ["x", "y"] if a.axis == "both" else [a.axis]

    a.start_x, a.start_y = 0.0, 0.0     # Plotter origin = the stop corner, so positions are machine mm
    a.ctl = ps.Control() if a.control else None
    track = a.track
    a.track = False                     # no position reports until the position is actually known
    p = ps.Plotter(cfg, a)
    spmm = p.spmm

    for ax in axes:
        dist = travel[AXES[ax]] + a.overrun
        ps.log(f"Home {ax.upper()}: pen up, {dist:.0f} mm toward the stop at {a.speed:g} mm/s "
               f"(up to {dist / a.speed:.0f} s; a buzz at the stop is expected).")
    if a.axis == "both":
        ps.log("Then: " + ("travel to S at (%.1f, %.1f) mm" % s_mm if a.to == "start" else "stay in the corner")
               + (", tap a dot %g mm right of and below it" % DOT_OFFSET if a.dot else "") + ".")
    else:
        ps.log("Only one axis: the carriage stays at that stop and the position stays unknown.")

    if not a.yes:
        try:
            prompt = "Pen up, path to the upper-left clear, power switch in reach? Type HOME to start: "
            word = a.ctl.readline(prompt) if a.ctl else input(prompt)
        except (EOFError, KeyboardInterrupt):
            word = ""
        if word.strip().upper() != "HOME":
            ps.log("Not confirmed. Nothing moved.")
            return 1

    def _sigterm(signum, frame):
        raise KeyboardInterrupt()
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, OSError):
        pass

    sim_pos = list(a.sim_from) if (a.sim and a.sim_from) else None
    homed = []
    status = "error"
    t0 = p.now()
    try:
        p.setup()                       # motors on, servo values, pen up
        for ax in axes:
            i = AXES[ax]
            dist = travel[i] + a.overrun
            target = list(p.pos)
            target[i] -= round(dist * spmm[i])
            p.move_to(target[0], target[1], a.speed)
            p.idle()
            p.pos[i] = 0                # pressed against the stop: this axis is now at machine 0
            homed.append(ax)
            if sim_pos is not None:
                ps.log(f"(sim) {ax.upper()} stop reached after {sim_pos[i]:.1f} mm; "
                       f"{dist - sim_pos[i]:.1f} mm skipped against it.")
                sim_pos[i] = 0.0
            ps.log(f"{ax.upper()} homed.")
        if len(homed) == 2:
            p.tracking = track
            p.track(force=True)
            if a.dot:
                dot = (end_mm[0] + DOT_OFFSET, end_mm[1] + DOT_OFFSET)
                p.move_to(round(dot[0] * spmm[0]), round(dot[1] * spmm[1]), a.travel_speed)
                p.pen(False)
                p.pen(True)
                ps.log(f"Dot at ({dot[0]:.1f}, {dot[1]:.1f}) mm.")
            p.move_to(round(end_mm[0] * spmm[0]), round(end_mm[1] * spmm[1]), a.travel_speed)
            p.idle()
            p.track(force=True)
        status = "complete"
    except KeyboardInterrupt:
        status = "interrupted"
        ps.log("Interrupted: raising the pen, then releasing the motors once the move in flight ends.")
    except Exception as ex:   # noqa: BLE001
        status = f"error: {ex!r}"
        ps.log(f"ERROR: {ex!r}")
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, signal.SIG_IGN)   # a second Ctrl-C must not cut the cleanup short
            except (ValueError, OSError):
                pass
        if a.hold and status == "complete":
            try:
                p.cmd("SP,1,300")
                p.idle()
                ok = True
            except Exception as ex:   # noqa: BLE001
                ps.log(f"shutdown problem: {ex!r}")
                ok = False
        else:
            ok = p.shutdown()
        if not ok:
            status += " (cleanup FAILED: pen may be down or motors on, check the machine and cut power if needed)"
        known = len(homed) == 2
        pos_mm = [round(p.pos[0] / spmm[0], 2), round(p.pos[1] / spmm[1], 2)] if known else None
        at_start = known and a.to == "start" and status == "complete"
        ps.log(f"Status: {status}. Homed: {', '.join(h.upper() for h in homed) or 'none'}. "
               + (f"Carriage at ({pos_mm[0]:.1f}, {pos_mm[1]:.1f}) mm." if known else "Position unknown.")
               + ("" if a.hold and status == "complete" else " Motors released."))
        if track:
            print("__RESULT__:" + json.dumps({
                "status": status, "homed": homed, "end_mm": pos_mm, "at_start": at_start,
                "corner": known and a.to == "corner" and status == "complete",
                "hold": bool(a.hold and status == "complete"),
                "elapsed_s": round(p.now() - t0, 2), "time": time.strftime("%Y-%m-%d %H:%M:%S")}), flush=True)
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
