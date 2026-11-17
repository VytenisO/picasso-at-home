#!/usr/bin/env python3
"""Trace the paper edges from a dead start (EBB firmware 2.0.1 safe: SM/SP/SC/EM only).

  calibrate_paper.py                  dry run: print the route and checks, nothing sent (default)
  calibrate_paper.py --run            pen-UP trace on the real machine (asks you to type TRACE)
  calibrate_paper.py --run --draw     same, but the pen is DOWN on the edges (asks you to type DRAW)
  add --sim to run any of these on a virtual machine and virtual clock

Dead start: with the motors released (plotter powered off, or after a previous run ended), slide
the carriage by hand to the machine's top-left corner (the park position), pen up, then start the
script. Wherever the carriage sits when the script starts is (0,0); the script counts its own
steps from there (firmware 2.0.1 has no position readback). Re-park before every run. The route:

  (0,0) -> paper start -> top-right -> bottom-right -> bottom-left -> paper start

The paper start (top-left of the paper, mm from the park corner) and size come from the "paper"
block in ../config/machine.json (x0, y0, width, height), or --paper-x/-y/-w/-h.
X is right, Y is toward you. The motors are released at the end, so the carriage can be slid.

This firmware has no e-stop and no position readback: cut power to abort, and check by eye (or with
--draw) that the carriage returns to the paper start. Net steps sent are printed as a self-check.
"""
import argparse
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import plot_svg as ps  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="move the real machine (default: dry run)")
    ap.add_argument("--draw", action="store_true", help="pen down on the edges (implies --run)")
    ap.add_argument("--sim", action="store_true", help="virtual machine + clock, no hardware")
    ap.add_argument("--port")
    ap.add_argument("--config", default=str(ps.ROOT / "config" / "machine.json"))
    ap.add_argument("--paper-x", type=float, help="paper start, mm right of the park corner")
    ap.add_argument("--paper-y", type=float, help="paper start, mm toward you from the park corner")
    ap.add_argument("--paper-w", type=float, help="paper width, mm")
    ap.add_argument("--paper-h", type=float, help="paper height, mm")
    ap.add_argument("--speed", type=float, default=15.0, help="edge speed mm/s")
    ap.add_argument("--travel-speed", type=float, default=25.0, help="speed from the park corner to the paper start, mm/s")
    ap.add_argument("--pen-delay", type=int, default=250, help="ms to wait after each pen up/down")
    ap.add_argument("--max-speed", type=float, default=ps.MAX_SPEED,
                    help="speed ceiling in mm/s (default %(default)s); raise only for speed tests, no step readback")
    a = ap.parse_args()
    a.track, a.start_x, a.start_y = False, 0.0, 0.0   # Plotter fields; no live tracker here, origin is the park corner

    edge_cap = ps.MAX_SPEED if a.draw else a.max_speed   # drawing never exceeds the plotter's drawing ceiling
    if not (0 < a.speed <= edge_cap and 0 < a.travel_speed <= a.max_speed):
        sys.exit(f"edge speed must be in (0, {edge_cap}] mm/s{' (pen down)' if a.draw else ''}, "
                 f"travel speed in (0, {a.max_speed}] mm/s")
    cfg = json.loads(pathlib.Path(a.config).read_text())
    for k in ("pen_up", "pen_down"):
        if not 7500 <= cfg["servo"][k] <= 22000:
            sys.exit(f"servo {k}={cfg['servo'][k]} outside 7500..22000")
    paper = cfg["paper"]
    px = a.paper_x if a.paper_x is not None else paper["x0"]
    py = a.paper_y if a.paper_y is not None else paper["y0"]
    pw = a.paper_w if a.paper_w is not None else paper["width"]
    ph = a.paper_h if a.paper_h is not None else paper["height"]
    mx, my = cfg["travel_mm"]["x"], cfg["travel_mm"]["y"]
    spmm = (cfg["steps_per_mm"]["x"], cfg["steps_per_mm"]["y"])
    run = a.run or a.draw

    problems = []
    if px < 0 or py < 0:
        problems.append("paper start is left of / above the park corner")
    if pw <= 0 or ph <= 0:
        problems.append("paper width/height must be positive")
    if px + pw > mx - ps.EDGE_MARGIN or py + ph > my - ps.EDGE_MARGIN:
        problems.append(f"paper far corner ({px + pw:.1f},{py + ph:.1f}) mm is inside the {ps.EDGE_MARGIN} mm margin "
                        f"of the {mx} x {my} mm travel")

    # Absolute machine coordinates in mm; the park corner is (0,0)
    route = [("paper start", (px, py)), ("top-right", (px + pw, py)), ("bottom-right", (px + pw, py + ph)),
             ("bottom-left", (px, py + ph)), ("paper start", (px, py))]
    ps.OUT.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    ps.LOGF = open(ps.OUT / f"calibrate_{stamp}.log", "w")
    ps.log(f"calibrate_paper run={run} draw={a.draw} sim={a.sim}")
    dist = (px ** 2 + py ** 2) ** 0.5
    secs = dist / a.travel_speed + 2 * (pw + ph) / a.speed + (2 * (a.pen_delay / 1000 + 0.15) if a.draw else 0)
    src = paper.get("source", "n/a")
    steps_start = (round(px * spmm[0]), round(py * spmm[1]))
    steps_far = (round((px + pw) * spmm[0]), round((py + ph) * spmm[1]))
    route_txt = " -> ".join(f"{n} ({x:.1f},{y:.1f})" for n, (x, y) in route)
    ps.log(f"""
Park corner    : (0,0) mm, carriage placed there by hand before the script starts
Paper          : start ({px:.1f},{py:.1f}) mm, {pw:.1f} x {ph:.1f} mm  [{src}]
Steps          : start {steps_start}  far corner {steps_far}
Estimated time : {ps.fmt(secs)}   speeds: edges {a.speed} mm/s, to paper {a.travel_speed} mm/s
Servo          : up {cfg['servo']['pen_up']}  down {cfg['servo']['pen_down']}   pen {'DOWN on edges' if a.draw else 'UP (no marks)'}
Route          : park (0,0) -> {route_txt}""")
    for p in problems:
        ps.log("PROBLEM: " + p)
    if not run:
        ps.log("Dry run only. Nothing was sent." + ("" if not problems else "  FIX THE PROBLEMS ABOVE before --run."))
        return 1 if problems else 0
    if problems:
        sys.exit("refusing to move: fix the problems above")

    if not a.sim:
        word = "DRAW" if a.draw else "TRACE"
        print("\nCheck: carriage is at the top-left corner right now (motors were released when you put it there), pen UP, power switch in reach.")
        if input(f"Type {word} to start: ").strip() != word:
            sys.exit("not confirmed, nothing sent")

    p = ps.Plotter(cfg, a)
    status = "complete"
    want = list(steps_start)
    t_start = p.now()
    try:
        p.setup()
        for i, (name, (x, y)) in enumerate(route):
            ps.log(f"-> {name} ({x:.1f},{y:.1f}) mm")
            if i == 0:
                p.move_to(*ps.to_steps((x, y), spmm), a.travel_speed)
                if a.draw:
                    p.pen(False, a.pen_delay)
            else:
                p.move_to(*ps.to_steps((x, y), spmm), a.speed)
        if a.draw:
            p.pen(True, a.pen_delay)
        p.idle()
    except KeyboardInterrupt:
        status = "interrupted"
        ps.log("Interrupted: raising pen, releasing motors. Position is now unknown.")
    except Exception as ex:   # noqa: BLE001
        status = f"error: {ex!r}"
        ps.log(f"ERROR: {ex!r}")
    finally:
        if not p.shutdown():
            status += " (cleanup FAILED: check the machine and cut power if needed)"
        ps.log(f"\nStatus: {status}   elapsed {ps.fmt(p.now() - t_start)}   SM commands {p.nsm}")
        ps.log(f"End position {p.pos} steps, expected {want} (paper start)" + ("  OK" if p.pos == want else "  MISMATCH"))
        ps.log("The carriage should rest on the paper start. How far it is off, if at all, is the calibration error.")
    return 0 if status == "complete" and p.pos == want else 1


if __name__ == "__main__":
    sys.exit(main())
