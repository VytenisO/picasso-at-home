#!/usr/bin/env python3
"""Manual control of the carriage for the web UI's Machine panel: jog, go to a point, pen up/down, set S here.

  jog.py --sim --control      simulate; commands on stdin (below)
  jog.py --control            the real plotter: the first line must be TAKE (carriage parked at S, pen up)

Commands, one per line:
  MOVE dx dy               relative move in mm; clamped to the reachable area (the clamp is reported)
  GOTO x y                 absolute move to machine mm
  HOME                     back to S
  PEN UP | PEN DOWN        pen down only moves at drawing speed (at most 40 mm/s)
  TICK dx dy               a short pen-down mark from here by (dx, dy) mm and back, pen up after: calibration ticks
  SPEED v                  pen-up jog speed in mm/s (1..200)
  SETS                     the carriage is at S now: the tracked position becomes S (re-zero after jogging)
  QUIT [HERE]              finish: back to S (unless HERE), pen up, motors off
  STOP                     stop before the next move: pen up, motors off; the position is unknown afterwards
End of input without QUIT counts as STOP. Events are JSON lines; --control implies --track (__POS__ lines).

Nothing is read back from the firmware: the position is counted from S like every other motion tool here.
"""
import argparse
import json
import math
import pathlib
import queue
import signal
import sys
import threading
import types

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import plot_svg as ps  # noqa: E402

TICK_MAX = 20.0          # mm, longest calibration tick


class StopFlag:
    def __init__(self):
        self.stop = threading.Event()

    def check_stop(self):
        if self.stop.is_set():
            raise KeyboardInterrupt()


def emit(event, **kw):
    print(json.dumps({"event": event, **kw}), flush=True)


def reader(q, flag):
    quit_seen = False
    try:
        for raw in sys.stdin:
            ln = raw.strip()
            if not ln:
                continue
            word = ln.split()[0].upper()
            if word == "STOP":
                flag.stop.set()
                q.put(["STOP"])
            else:
                if word == "QUIT":
                    quit_seen = True
                if q.qsize() > 50:
                    emit("error", message="Too many commands waiting; this one was dropped.")
                    continue
                q.put(ln.split())
    finally:
        if not quit_seen:
            flag.stop.set()
        q.put(["STOP"] if not quit_seen else ["EOF"])


def nums(parts, n):
    try:
        v = [float(x) for x in parts[:n]]
    except ValueError:
        return None
    return v if len(v) == n and all(math.isfinite(x) and abs(x) < 10000 for x in v) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--sim-speed", type=float, default=0.0, help="with --sim: real-time factor (0 = instant)")
    ap.add_argument("--port")
    ap.add_argument("--config", default=str(ROOT / "config" / "machine.json"))
    ap.add_argument("--speed", type=float, default=30.0, help="pen-up jog speed mm/s")
    ap.add_argument("--control", action="store_true", help="web server mode: commands on stdin")
    a = ap.parse_args()
    cfg = json.loads(pathlib.Path(a.config).read_text())
    spmm = (cfg["steps_per_mm"]["x"], cfg["steps_per_mm"]["y"])
    mx, my = cfg["travel_mm"]["x"], cfg["travel_mm"]["y"]
    paper = cfg.get("paper") or {}
    sx, sy = float(paper.get("x0", 0.0)), float(paper.get("y0", 0.0))
    # Reachable: the machine travel minus its edge margin, but S itself always counts as reachable.
    area = [min(ps.EDGE_MARGIN, sx), min(ps.EDGE_MARGIN, sy), mx - ps.EDGE_MARGIN, my - ps.EDGE_MARGIN]
    speed = max(1.0, min(ps.MAX_TRAVEL_SPEED, a.speed))

    q = queue.Queue()
    flag = StopFlag()
    threading.Thread(target=reader, args=(q, flag), daemon=True).start()
    emit("session", sim=a.sim, start_mm=[sx, sy], area_mm=area, speed=speed, steps_per_mm=list(spmm))
    if not a.sim:
        print(f"Check: motors off, carriage by hand at S ({sx:.1f},{sy:.1f}) mm, pen up, power switch in reach.",
              flush=True)
        first = q.get()
        if [w.upper() for w in first] != ["TAKE"]:
            sys.exit("not confirmed, nothing sent")

    pargs = types.SimpleNamespace(sim=a.sim, port=a.port, track=True, start_x=sx, start_y=sy, ctl=flag,
                                  sim_speed=a.sim_speed)
    p = ps.Plotter(cfg, pargs)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    status, back = "complete", True

    def here():
        return sx + p.pos[0] / spmm[0], sy + p.pos[1] / spmm[1]

    def go(tx, ty):
        """Move to machine mm, clamped. -> True if clamped."""
        cx = min(max(tx, area[0]), area[2])
        cy = min(max(ty, area[1]), area[3])
        v = speed if p.pen_state else min(speed, ps.MAX_SPEED)
        p.move_to(round((cx - sx) * spmm[0]), round((cy - sy) * spmm[1]), v)
        p.idle()
        p.track(force=True)
        x, y = here()
        clamped = abs(cx - tx) > 0.01 or abs(cy - ty) > 0.01
        emit("moved", x=round(x, 2), y=round(y, 2), pen="up" if p.pen_state else "down", clamped=clamped)
        if clamped:
            emit("warning", message=f"Stopped at the edge of the reachable area ({area[0]:g}..{area[2]:g} x "
                                    f"{area[1]:g}..{area[3]:g} mm).")
        return clamped

    try:
        p.setup()
        p.track(force=True)
        emit("ready", x=sx, y=sy)
        while True:
            flag.check_stop()
            parts = q.get()
            flag.check_stop()
            word, args = parts[0].upper(), parts[1:]
            if word in ("STOP",):
                raise KeyboardInterrupt()
            if word == "EOF":
                break
            if word == "QUIT":
                back = not (args and args[0].upper() == "HERE")
                break
            if word == "MOVE" and (v := nums(args, 2)):
                x, y = here()
                go(x + v[0], y + v[1])
            elif word == "GOTO" and (v := nums(args, 2)):
                go(v[0], v[1])
            elif word == "HOME":
                go(sx, sy)
            elif word == "PEN" and args and args[0].upper() in ("UP", "DOWN"):
                p.pen(args[0].upper() == "UP")
                emit("pen", pen="up" if p.pen_state else "down")
            elif word == "TICK" and (v := nums(args, 2)) and math.hypot(*v) <= TICK_MAX:
                x, y = here()
                p.pen(False)
                go(x + v[0], y + v[1])
                go(x, y)
                p.pen(True)
                emit("pen", pen="up")
            elif word == "TOUCH" and (v := nums(args, 2)) and 0 <= v[0] <= ps.MAX_PRESSURE and 100 <= v[1] <= 3000:
                # a short pen-down at a pressure, then up: how hard a layer's setting presses, without drawing
                p.set_pressure(v[0])
                p.pen(False)
                p.idle()
                p.sleep(v[1] / 1000)
                p.pen(True)
                p.set_pressure(0.0)
                emit("pen", pen="up")
            elif word == "SPEED" and (v := nums(args, 1)) and 1 <= v[0] <= ps.MAX_TRAVEL_SPEED:
                speed = v[0]
                emit("speed", speed=speed)
            elif word == "SETS":
                if not p.pen_state:
                    emit("error", message="Lift the pen before setting S here.")
                    continue
                p.idle()
                p.pos = [0, 0]
                p.track(force=True)
                emit("zeroed", x=sx, y=sy)
            else:
                emit("error", message=f"Not understood: {' '.join(parts)[:80]}")
        if back:
            if not p.pen_state:
                p.pen(True)
            go(sx, sy)
        p.idle()
    except KeyboardInterrupt:
        status = "interrupted"
        emit("error", message="Stopped: raising the pen, then releasing the motors. The position is now unknown.")
    except Exception as ex:   # noqa: BLE001
        status = f"error: {ex!r}"
        emit("error", message=repr(ex))
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
        if not p.shutdown():
            status += " (cleanup FAILED: pen may be down or motors still on, check the machine and cut power if needed)"
        x, y = here()
        print("__RESULT__:" + json.dumps({
            "status": status, "elapsed_s": 0, "end_mm": [round(x, 2), round(y, 2)],
            "at_start": p.pos == [0, 0] and not status.startswith(("interrupted", "error")) and "FAILED" not in status}),
            flush=True)
        emit("done", status=status)
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
