#!/usr/bin/env python3
"""Two test sheets for one pen type. Stdlib (plus the vendored Hershey font), never touches hardware.

  grid_test.py --pen bic                                  speed x pressure grid
  grid_test.py --pen bic --speeds 10,20,30,40 --pressures 0,0.2,0.4
  grid_test.py --pen marker --tone                        tone ramp: hatch spacing -> grey

GRID. One cell per speed (columns) and pressure (rows): a short zigzag patch, a straight line and a small circle,
each drawn at that cell's speed (data-speed) and pen pressure (data-pressure, a fraction from the configured
pen-down toward pen-up, 0 = heaviest). Labels on the top and left are drawn at the plot's own settings. One run
answers which speed and which touch give clean lines with this pen: pick the cell that looks right.

TONE RAMP. A row of patches hatched at growing spacing, 0.3 mm to 3 mm, labelled. Plotted with a pen it shows
which spacing reads as which grey, for the image methods' density settings.

Plot with the pen you are testing. The sheet is tagged with the pen; results apply to that pen type only.
"""
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import hershey  # noqa: E402

MAX_SPEED = 200.0
MAX_LEVEL = 0.8
CELL, GAP, MARGIN, LABEL = 14.0, 4.0, 3.0, 2.5      # mm
RAMP = [0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.3, 1.6, 2.0, 2.5, 3.0]


def nums(text, lo, hi, label, n_max=8):
    try:
        vals = [float(v) for v in str(text).split(",") if v.strip()]
    except ValueError:
        sys.exit(f"{label}: numbers separated by commas")
    if not 1 <= len(vals) <= n_max:
        sys.exit(f"{label}: 1 to {n_max} values")
    for v in vals:
        if not lo <= v <= hi:
            sys.exit(f"{label}: each value must be {lo:g} to {hi:g}")
    return vals


def path(pts):
    return "M" + " L".join(f"{x:.3f} {y:.3f}" for x, y in pts)


def label(text, x, y, size=LABEL):
    polys, _, _ = hershey.text_strokes(text, "Hershey Sans", size)
    return "".join(f'<path d="{path([(x + px, y + py) for px, py in s])}"/>' for s in polys if len(s) >= 2)


def zigzag(x0, y0, w, h, spacing):
    pts, y, left = [], y0, True
    while y <= y0 + h + 1e-9:
        pts += [(x0, y), (x0 + w, y)] if left else [(x0 + w, y), (x0, y)]
        left = not left
        y += spacing
    return pts


def circle(cx, cy, r, n=24):
    import math
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n + 1)]


def grid_svg(speeds, pressures):
    x0 = MARGIN + 12.0                       # room for the row labels
    y0 = MARGIN + 8.0                        # and the column labels
    w = x0 + len(speeds) * (CELL + GAP) + MARGIN
    h = y0 + len(pressures) * (CELL + GAP) + MARGIN
    body = [f'<g fill="none" stroke="#000000" stroke-width="0.3">',
            label("mm/s", MARGIN, MARGIN + 3.0, 2.0)]
    for c, sp in enumerate(speeds):
        body.append(label(f"{sp:g}", x0 + c * (CELL + GAP) + 3.0, MARGIN + 6.0))
    for r, pr in enumerate(pressures):
        y = y0 + r * (CELL + GAP)
        body.append(label(f"{pr:g}", MARGIN, y + CELL / 2 + LABEL / 2))
        for c, sp in enumerate(speeds):
            x = x0 + c * (CELL + GAP)
            body.append(f'<g data-speed="{sp:g}" data-pressure="{pr:g}">'
                        f'<path d="{path(zigzag(x, y, CELL * 0.55, CELL * 0.55, 0.8))}"/>'
                        f'<path d="{path([(x, y + CELL * 0.8), (x + CELL, y + CELL * 0.8)])}"/>'
                        f'<path d="{path(circle(x + CELL * 0.8, y + CELL * 0.3, 2.0))}"/></g>')
    body.append("</g>")
    return w, h, "".join(body)


def tone_svg(angle_note=True):
    patch, gap = 12.0, 3.0
    x0, y0 = MARGIN, MARGIN + 6.0
    w = x0 + len(RAMP) * (patch + gap) + MARGIN
    h = y0 + patch + MARGIN
    body = ['<g fill="none" stroke="#000000" stroke-width="0.3">']
    for i, sp in enumerate(RAMP):
        x = x0 + i * (patch + gap)
        body.append(label(f"{sp:g}", x + 2.0, MARGIN + 3.5))
        body.append(f'<path d="{path(zigzag(x, y0, patch, patch, sp))}"/>')
    body.append("</g>")
    return w, h, "".join(body)


def square_svg(size):
    """A square with both diagonals and a centre cross: equal diagonals mean the axes are square to each other;
    their difference gives the skew. The sides check each axis's scale."""
    m = MARGIN
    s = size
    body = ['<g fill="none" stroke="#000000" stroke-width="0.3">',
            f'<path d="{path([(m, m), (m + s, m), (m + s, m + s), (m, m + s), (m, m)])}"/>',
            f'<path d="{path([(m, m), (m + s, m + s)])}"/>', f'<path d="{path([(m + s, m), (m, m + s)])}"/>',
            f'<path d="{path([(m + s / 2 - 4, m + s / 2), (m + s / 2 + 4, m + s / 2)])}"/>',
            f'<path d="{path([(m + s / 2, m + s / 2 - 4), (m + s / 2, m + s / 2 + 4)])}"/>',
            label(f"{s:g} mm square: measure both diagonals", m, m + s + 6, 2.5), "</g>"]
    return s + 2 * m, s + 2 * m + 9, "".join(body)


def backlash_svg():
    """Pairs of short lines drawn towards each other from both sides, each pair meant to meet at a drawn reference
    tick: a gap or overlap at the meeting point is the backlash of that axis. Plot with Shorter pen-up travel off,
    so every line keeps its drawing direction."""
    m, n, step, arm = MARGIN, 6, 8.0, 15.0
    body = ['<g fill="none" stroke="#000000" stroke-width="0.3">']
    cx = m + arm + 2
    for k in range(n):                                  # X: left half drawn rightwards, right half drawn leftwards
        y = m + 8 + k * step
        body += [f'<path d="{path([(cx, y - 2), (cx, y + 2)])}"/>',
                 f'<path d="{path([(cx - arm, y), (cx, y)])}"/>', f'<path d="{path([(cx + arm, y), (cx, y)])}"/>']
    x0 = cx + arm + 15
    cy = m + 8 + arm
    for k in range(n):                                  # Y: top half drawn down, bottom half drawn up
        x = x0 + k * step
        body += [f'<path d="{path([(x - 2, cy), (x + 2, cy)])}"/>',
                 f'<path d="{path([(x, cy - arm), (x, cy)])}"/>', f'<path d="{path([(x, cy + arm), (x, cy)])}"/>']
    body.append(label("X", m, m + 4, 2.5) + label("Y", x0, m + 4, 2.5))
    body.append("</g>")
    return x0 + n * step + m, cy + arm + m, "".join(body)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pen", required=True)
    ap.add_argument("--pens-file", default=str(ROOT / "config" / "pens.json"))
    ap.add_argument("--out-dir", default=str(ROOT / "results" / "sheets"))
    ap.add_argument("--tone", action="store_true", help="the tone ramp instead of the grid")
    ap.add_argument("--square", type=float, metavar="MM", help="the squareness sheet: a square this size with diagonals")
    ap.add_argument("--backlash", action="store_true", help="the backlash sheet: lines drawn towards each other")
    ap.add_argument("--speeds", default="10,20,30,40", help="mm/s, comma separated, up to 8 (max 200)")
    ap.add_argument("--pressures", default="0,0.15,0.3,0.45", help="0..0.8, comma separated, up to 8")
    a = ap.parse_args()
    pens = json.loads(pathlib.Path(a.pens_file).read_text())
    if a.pen not in pens:
        sys.exit(f"unknown pen {a.pen!r}; known: {', '.join(sorted(pens))}")
    if a.square:
        if not 20 <= a.square <= 200:
            sys.exit("--square must be 20 to 200 mm")
        w, h, body = square_svg(a.square)
        kind = f"squareness {a.square:g} mm"
    elif a.backlash:
        w, h, body = backlash_svg()
        kind = "backlash"
    elif a.tone:
        w, h, body = tone_svg()
        kind = "tone ramp"
    else:
        speeds = nums(a.speeds, 1, MAX_SPEED, "Speeds")
        pressures = nums(a.pressures, 0, MAX_LEVEL, "Pressures")
        w, h, body = grid_svg(speeds, pressures)
        kind = f"grid {len(speeds)} speeds x {len(pressures)} pressures"
    out = pathlib.Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    svg_path = out / f"{'square' if a.square else 'backlash' if a.backlash else 'tone' if a.tone else 'grid'}_{a.pen}_{stamp}.svg"
    svg_path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}mm" height="{h:.2f}mm" '
                        f'viewBox="0 0 {w:.2f} {h:.2f}">{body}</svg>\n', encoding="utf-8")
    print(f"Wrote {svg_path}  ({w:.0f} x {h:.0f} mm, {kind}, pen {a.pen})")


if __name__ == "__main__":
    main()
