#!/usr/bin/env python3
"""Lost-steps test sheet: long, jittery, fast, returns to the same marked point. Stdlib only, never touches hardware.

  lost_steps_test.py                       write input/lost-steps-test.svg (+ .json) and print how to plot it
  lost_steps_test.py --minutes 30          a longer run (default 20 minutes at the stress speeds)
  lost_steps_test.py --analyse 0.8 -1.2    after plotting: turn the measured drift of the last checkpoint into steps

Firmware 2.0.1 has no position readback, so lost steps are invisible to the host. This sheet makes them visible on
the paper. A checkpoint mark (a 4 mm circle with a cross through its centre) is drawn at the same spot at the start,
after every few patches, and at the end. In between the carriage does what loses steps: hundreds of short random
zig-zag strokes with sharp reversals (jitter), and long fast pen-up jumps between far-apart patches. If steps were
lost, later checkpoints land away from the first one, and the offset grows along the way, so the stack of marks
shows when it happened. If every mark sits exactly on the first, nothing was lost (the marks overlap to within the
pen play). Pen play makes each mark about 0.2-0.4 mm fuzzy; only drift beyond that counts.

Run it twice: once at the speeds you normally use, once at the stress speeds. The tool prints both commands. A
stress pass that loses steps where the normal one does not tells you the speed ceiling is too high. Drift of one
checkpoint relative to the first in a straight line is a real loss; a mark that only wobbles is pen play.

At the end plot_svg.py parks the carriage back at the paper's top-left (where you parked it), a second, independent
return check: by eye, or against a pencil mark you made at the carriage before the run.

Reading the result, in mm: dx = how far the LAST checkpoint is to the right of the first (negative: left), dy = how
far toward you (negative: away). Then:  lost_steps_test.py --analyse DX DY
"""
import argparse
import json
import math
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CP = (6.0, 6.0)          # checkpoint centre, mm from the sheet's top-left
CP_R = 2.0               # checkpoint circle radius
CP_ARM = 3.5             # cross arm length from the centre
PEN_PENALTY_S = 0.65     # rough time per pen lift cycle (2 x 250 ms delay + margin)


def checkpoint():
    cx, cy = CP
    circ = [(cx + CP_R * math.cos(math.radians(-90 + 5 * k)), cy + CP_R * math.sin(math.radians(-90 + 5 * k)))
            for k in range(73)]
    return [circ, [(cx - CP_ARM, cy), (cx + CP_ARM, cy)], [(cx, cy - CP_ARM), (cx, cy + CP_ARM)]]


def jitter_patch(rng, cx, cy, box, segs):
    """Random zig-zag around (cx, cy) inside a box: short strokes, many sharp reversals."""
    x, y = cx, cy
    pts = [(x, y)]
    ang = rng.uniform(0, 2 * math.pi)
    for _ in range(segs):
        r = rng.random()
        if r < 0.4:
            ang += math.pi + rng.uniform(-0.25, 0.25)     # nearly straight back on itself
        elif r < 0.75:
            ang += rng.choice((-1, 1)) * rng.uniform(1.2, 2.2)   # hard turn
        else:
            ang = rng.uniform(0, 2 * math.pi)
        ln = rng.uniform(0.4, 2.5)
        nx, ny = x + ln * math.cos(ang), y + ln * math.sin(ang)
        if abs(nx - cx) > box / 2 or abs(ny - cy) > box / 2:   # bounce off the box edge
            ang += math.pi
            nx, ny = x + ln * math.cos(ang), y + ln * math.sin(ang)
        x, y = nx, ny
        pts.append((x, y))
    return pts


def build(width, height, minutes, every, draw_speed, travel_speed, seed):
    rng = random.Random(seed)
    cols, rows = 6, 4
    margin = 14.0
    cells = [(margin + (width - 2 * margin) * (c + 0.5) / cols, margin + (height - 2 * margin) * (r + 0.5) / rows)
             for r in range(rows) for c in range(cols)]
    step = 7   # coprime to 24: consecutive cells are far apart, so the jumps between patches are long
    order = [cells[(i * step) % len(cells)] for i in range(len(cells))]
    strokes = checkpoint()
    stats = {"drawn_mm": 0.0, "travel_mm": 0.0, "strokes": 0, "reversals": 0}
    last = [CP]

    def add(poly):
        stats["travel_mm"] += math.dist(last[0], poly[0])
        stats["drawn_mm"] += sum(math.dist(a, b) for a, b in zip(poly, poly[1:]))
        stats["strokes"] += 1
        last[0] = poly[-1]
        strokes.append(poly)

    stats["drawn_mm"] += 2 * math.pi * CP_R + 4 * CP_ARM
    stats["strokes"] += 3
    last[0] = (CP[0], CP[1] - CP_ARM)

    def est_seconds():
        return (stats["drawn_mm"] / draw_speed + stats["travel_mm"] / travel_speed + stats["strokes"] * PEN_PENALTY_S)

    n = 0
    while True:
        cx, cy = order[n % len(order)]
        cx += rng.uniform(-6, 6)
        cy += rng.uniform(-6, 6)
        add(jitter_patch(rng, cx, cy, 12.0, 40))
        stats["reversals"] += 40
        n += 1
        if est_seconds() >= minutes * 60:
            break
        if n % every == 0:
            for s in checkpoint():
                add(s)
    for s in checkpoint():          # the final checkpoint is always last
        add(s)
    checkpoints = sum(1 for s in strokes if len(s) == 73)
    xs = [x for s in strokes for x, _ in s]
    ys = [y for s in strokes for _, y in s]
    d = "\n".join('<path d="' + " L".join(f"{'M' if k == 0 else ''}{x:.3f},{y:.3f}" for k, (x, y) in enumerate(p)) + '"/>'
                  for p in strokes)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.1f}mm" height="{height:.1f}mm" '
           f'viewBox="0 0 {width:.1f} {height:.1f}">\n'
           f'<!-- lost-steps test: {n} jitter patches, {checkpoints} checkpoint marks at ({CP[0]}, {CP[1]}) mm -->\n'
           f'<g fill="none" stroke="#000" stroke-width="0.3" stroke-linecap="round">\n{d}\n</g>\n</svg>\n')
    meta = {"size_mm": [width, height], "patches": n, "checkpoints": checkpoints, "checkpoint_mm": list(CP),
            "strokes": len(strokes), "drawn_mm": round(stats["drawn_mm"], 1), "travel_mm": round(stats["travel_mm"], 1),
            "reversals": stats["reversals"], "extent_mm": [round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1)],
            "est_seconds_at": {"draw_mm_s": draw_speed, "travel_mm_s": travel_speed, "seconds": round(est_seconds())},
            "seed": seed}
    return svg, meta


def analyse(dx, dy, meta_path, config_path):
    meta = json.loads(meta_path.read_text())
    spmm = json.loads(config_path.read_text())["steps_per_mm"]
    sx, sy = dx * spmm["x"], dy * spmm["y"]
    total_m = (meta["drawn_mm"] + meta["travel_mm"]) / 1000.0
    d = math.hypot(dx, dy)
    print(f"Last checkpoint is {dx:+.2f} mm right, {dy:+.2f} mm toward you of the first ({d:.2f} mm)")
    print(f"= {sx:+.0f} steps on X, {sy:+.0f} steps on Y at {spmm['x']} / {spmm['y']} steps/mm")
    print(f"Path travelled: {total_m:.1f} m ({meta['drawn_mm'] / 1000:.1f} m drawn, {meta['travel_mm'] / 1000:.1f} m pen-up, "
          f"{meta['reversals']} sharp reversals, {meta['patches']} patches)")
    if d < 0.4:
        print("Under 0.4 mm: within pen play, no measurable loss on this run.")
    else:
        print(f"About {abs(sx) + abs(sy):.0f} steps lost in total, {(abs(sx) + abs(sy)) / total_m:.0f} per metre travelled. "
              "Check whether the intermediate checkpoints already drifted (when it happened), and compare a slower run.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width", type=float, default=260.0, help="sheet width, mm (must fit the canvas)")
    ap.add_argument("--height", type=float, default=172.0, help="sheet height, mm (must fit the canvas)")
    ap.add_argument("--minutes", type=float, default=20.0, help="target run time at the stress speeds")
    ap.add_argument("--every", type=int, default=8, help="a checkpoint mark after this many jitter patches")
    ap.add_argument("--seed", type=int, default=1, help="random seed; same seed gives the same sheet")
    ap.add_argument("--stress-speed", type=float, default=40.0, help="drawing speed of the stress command, mm/s (max 40)")
    ap.add_argument("--stress-travel", type=float, default=200.0, help="pen-up speed of the stress command, mm/s (max 200)")
    ap.add_argument("--out-dir", default=str(ROOT / "input"))
    ap.add_argument("--config", default=str(ROOT / "config" / "machine.json"))
    ap.add_argument("--analyse", nargs=2, type=float, metavar=("DX_MM", "DY_MM"),
                    help="turn the measured drift of the last checkpoint into lost steps")
    a = ap.parse_args()

    out = pathlib.Path(a.out_dir)
    meta_path = out / "lost-steps-test.json"
    if a.analyse:
        if not meta_path.exists():
            sys.exit(f"{meta_path} not found; generate the sheet first")
        return analyse(a.analyse[0], a.analyse[1], meta_path, pathlib.Path(a.config))
    if not (0 < a.stress_speed <= 40 and 0 < a.stress_travel <= 200):
        sys.exit("stress speeds: drawing in (0, 40] mm/s, travel in (0, 200] mm/s")
    if a.width > 267 or a.height > 180 or a.width < 60 or a.height < 60:
        sys.exit("sheet must be 60..267 x 60..180 mm to fit the paper canvas")

    svg, meta = build(a.width, a.height, a.minutes, a.every, a.stress_speed, a.stress_travel, a.seed)
    out.mkdir(exist_ok=True)
    svg_path = out / "lost-steps-test.svg"
    svg_path.write_text(svg)
    meta_path.write_text(json.dumps(meta, indent=2))
    try:
        shown = svg_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        shown = str(svg_path.resolve())
    cfg = "" if pathlib.Path(a.config).resolve() == (ROOT / "config" / "machine.json").resolve() else f" --config {a.config}"
    base = f"python3 tools/plot_svg.py {shown}{cfg}"
    est = meta["est_seconds_at"]["seconds"]
    print(f"Wrote {svg_path}  ({a.width:.0f} x {a.height:.0f} mm, {meta['patches']} jitter patches, "
          f"{meta['checkpoints']} checkpoint marks, {meta['strokes']} strokes)")
    print(f"Path: {meta['drawn_mm'] / 1000:.1f} m drawn, {meta['travel_mm'] / 1000:.1f} m pen-up jumps, "
          f"{meta['reversals']} sharp reversals; about {est // 60:.0f} min at the stress speeds")
    print("\nCarriage parked at the paper's top-left, pen UP, before every run. Use any pen; it only needs to mark.")
    print("Marks overlap if no steps were lost: that is the first checkpoint (top-left of the sheet) versus the last.")
    print(f"\nDry run      : {base} --mode dry")
    print(f"Normal speed : {base} --mode plot --speed 15 --travel-speed 100")
    print(f"Stress       : {base} --mode plot --speed {a.stress_speed:g} --travel-speed {a.stress_travel:g}")
    print("\nThen measure how far the LAST checkpoint sits from the first (mm, right and toward you are positive), and run:")
    print("  python3 tools/lost_steps_test.py --analyse DX DY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
