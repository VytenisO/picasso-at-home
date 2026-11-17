#!/usr/bin/env python3
"""Short-stroke and small-circle test sheet for one specific pen type. Stdlib only, never touches hardware.

  stroke_test.py --pen bic                       write input/stroke-test-bic.svg and print how to plot it
  stroke_test.py --pen bic --sheet circles       only the circles (lines | circles | both, default both)
  stroke_test.py --pen bic --record-single 1.0 --record-double 0.5 --record-circle-single 3
                                                 after plotting: store what came out (see below)

--pen is required and must be a profile name from ../config/pens.json. The result belongs to that pen
type ONLY (a BIC ballpoint says nothing about a pencil or a fineliner): the sheet name carries the pen,
the results are stored under the pen, and nothing is ever applied to another pen.

The sheet is a grid of strokes of increasing length. Reading it, top to bottom, left to right:

  block 1  horizontal, one pass          block 3  vertical, one pass
  block 2  horizontal, forward + back    block 4  vertical, forward + back

Below that, circles of increasing diameter. Play in the pen shows up as a gap where the loop should close:

  block 5  clockwise, one loop           block 7  clockwise, one loop plus 30 degrees of overlap
  block 6  counter-clockwise, one loop   block 8  clockwise, two full loops

Each block is two identical rows (repeatability). A short "reference" line starts every line row, and a small
corner mark at the top-left shows which way the sheet faces. Sizes run left to right.
Plot it with the same pen, pen-down value and speed you intend to use for real work; the speed matters.

Recording (all in mm, stored under the pen with the date):
  --record-single / --record-double          shortest line that came out clearly, one pass / forward+back
  --record-circle-single / -overlap / -double   smallest circle diameter that closed cleanly, per variant
"""
import argparse
import datetime
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LENGTHS = [0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]        # mm, line test
DIAMETERS = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]      # mm, circle test
COL_X0, COL_PITCH = 18.0, 11.0                             # mm: first column x, spacing
ROW_PITCH = 9.0                                            # mm
REF_LEN = 10.0                                             # mm: reference line at the start of every line row
LINE_BLOCKS = [("horizontal", False), ("horizontal", True), ("vertical", False), ("vertical", True)]
# (label, direction, sweep in degrees). Start point is the top of the circle; +1 = clockwise as seen on the paper.
CIRCLE_BLOCKS = [("circle clockwise, one loop", +1, 360.0),
                 ("circle counter-clockwise, one loop", -1, 360.0),
                 ("circle clockwise, one loop + 30 deg overlap", +1, 390.0),
                 ("circle clockwise, two loops", +1, 720.0)]
REPEATS = 2


def circle(cx, cy, diam, direction, sweep):
    """Polyline for an arc starting at the top; y points down, so +1 is clockwise on the paper."""
    n = max(24, int(sweep / 5))
    r = diam / 2
    pts = []
    for k in range(n + 1):
        t = math.radians(-90.0 + direction * sweep * k / n)
        pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    return pts


def build(pen, sheet="both", colour=(None, None)):
    """-> (svg text, layout dict). Origin (0,0) is the sheet's top-left corner mark."""
    paths = [[(0.0, 3.0), (0.0, 0.0), (3.0, 0.0)]]   # corner mark
    layout = []
    row = 0
    if sheet in ("lines", "both"):
        for orient, double in LINE_BLOCKS:
            for rep in range(REPEATS):
                cy = 8.0 + row * ROW_PITCH
                paths.append([(2.0, cy), (2.0 + REF_LEN, cy)])   # reference, horizontal, single pass
                for i, ln in enumerate(LENGTHS):
                    cx = COL_X0 + i * COL_PITCH
                    if orient == "horizontal":
                        a, b = (cx, cy), (cx + ln, cy)
                    else:
                        a, b = (cx, cy - ln / 2), (cx, cy + ln / 2)
                    paths.append([a, b, a] if double else [a, b])
                layout.append({"row": row + 1, "block": f"{orient} {'forward+back' if double else 'single'}",
                               "repeat": rep + 1, "y_mm": cy})
                row += 1
    if sheet in ("circles", "both"):
        for label, direction, sweep in CIRCLE_BLOCKS:
            for rep in range(REPEATS):
                cy = 8.0 + row * ROW_PITCH
                for i, dia in enumerate(DIAMETERS):
                    paths.append(circle(COL_X0 + i * COL_PITCH + dia / 2, cy, dia, direction, sweep))
                layout.append({"row": row + 1, "block": label, "repeat": rep + 1, "y_mm": cy})
                row += 1
    w = COL_X0 + (len(LENGTHS) - 1) * COL_PITCH + max(max(LENGTHS), max(DIAMETERS)) + 4.0
    h = 8.0 + (row - 1) * ROW_PITCH + 6.0
    d = "\n".join('<path d="' + " L".join(f"{'M' if k == 0 else ''}{x:.3f},{y:.3f}" for k, (x, y) in enumerate(p)) + '"/>'
                  for p in paths)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" viewBox="0 0 {w:.1f} {h:.1f}">\n'
           f'<!-- stroke and circle test; results valid ONLY for pen type "{pen}" (colour: {colour[0] or "not set"}) -->\n'
           f'<g fill="none" stroke="{colour[1] or "#000"}" stroke-width="0.3" stroke-linecap="round">\n{d}\n</g>\n</svg>\n')
    return svg, {"pen": pen, "colour": colour[0], "sheet": sheet, "lengths_mm": LENGTHS, "circle_diameters_mm": DIAMETERS,
                 "size_mm": [w, h], "rows": layout}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pen", required=True, help="pen type this test is for; a profile name in config/pens.json")
    ap.add_argument("--sheet", choices=["lines", "circles", "both"], default="both", help="which part to generate")
    ap.add_argument("--pens-file", default=str(ROOT / "config" / "pens.json"))
    ap.add_argument("--out-dir", default=str(ROOT / "input"))
    ap.add_argument("--record-single", type=float, metavar="MM", help="shortest single-pass line that came out clearly")
    ap.add_argument("--record-double", type=float, metavar="MM", help="shortest forward+back line that came out clearly")
    ap.add_argument("--record-circle-single", type=float, metavar="MM", help="smallest diameter closing with one loop")
    ap.add_argument("--record-circle-overlap", type=float, metavar="MM",
                    help="smallest diameter closing with 30 degrees of overlap")
    ap.add_argument("--record-circle-double", type=float, metavar="MM", help="smallest diameter closing with two loops")
    a = ap.parse_args()

    pens_path = pathlib.Path(a.pens_file)
    pens = json.loads(pens_path.read_text())
    if a.pen not in pens:
        sys.exit(f"unknown pen {a.pen!r}; known: {', '.join(sorted(pens))}. Add a profile to {pens_path.name} first. "
                 "Results do not carry over between pen types, so each type needs its own test.")

    recs = {"min_single_mm": a.record_single, "min_double_mm": a.record_double,
            "min_circle_single_loop_mm": a.record_circle_single,
            "min_circle_overlap_mm": a.record_circle_overlap,
            "min_circle_double_loop_mm": a.record_circle_double}
    if any(v is not None for v in recs.values()):
        res = pens[a.pen].setdefault("short_stroke_results", {})
        res.update({k: v for k, v in recs.items() if v is not None})
        res["recorded"] = datetime.date.today().isoformat()
        res["valid_for_pen_only"] = a.pen
        pens_path.write_text(json.dumps(pens, indent=2) + "\n")
        print(f"recorded for pen {a.pen!r} only: {res}")
        return 0

    out = pathlib.Path(a.out_dir)
    out.mkdir(exist_ok=True)
    colour = (pens[a.pen].get("colour"), pens[a.pen].get("colour_hex"))
    svg, layout = build(a.pen, a.sheet, colour)
    tag = "" if a.sheet == "both" else f"-{a.sheet}"
    svg_path = out / f"stroke-test-{a.pen}{tag}.svg"
    svg_path.write_text(svg)
    (out / f"stroke-test-{a.pen}{tag}.json").write_text(json.dumps(layout, indent=2))
    w, h = layout["size_mm"]
    print(f"Wrote {svg_path}  ({w:.0f} x {h:.0f} mm, {len(layout['rows'])} rows, sheet: {a.sheet})")
    print(f"RESULTS FROM THIS SHEET APPLY ONLY TO PEN TYPE {a.pen!r}. Do not use them for any other pen.")
    print(f"Pen colour tag: {colour[0] or 'NOT SET (add \"colour\" to this pen in pens.json)'}")
    if a.sheet in ("lines", "both"):
        print("Line lengths, mm, left to right after the reference: " + ", ".join(str(x) for x in LENGTHS))
    if a.sheet in ("circles", "both"):
        print("Circle diameters, mm, left to right: " + ", ".join(str(x) for x in DIAMETERS))
    for r in layout["rows"]:
        print(f"  row {r['row']}: {r['block']}, repeat {r['repeat']}")
    base = f"python3 tools/plot_svg.py input/{svg_path.name}"
    print(f"\nDry run : {base} --mode dry\nTrace   : {base} --mode trace\nPlot    : {base} --mode plot")
    print(f"\nThen record what came out, e.g.: stroke_test.py --pen {a.pen} --record-single 1.0 "
          "--record-double 0.5 --record-circle-single 3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
