#!/usr/bin/env python3
"""Infill (shape fill) test sheets for one specific pen type. Stdlib only, never touches hardware.

  fill_test.py --pen marker                       fill sheet: styles x line spacing
  fill_test.py --pen bic --pressure               pressure sheet: how a lighter touch changes the look of infill
  fill_test.py --pen marker --record-min-spacing 0.6 --record-best-style zigzag
  fill_test.py --pen bic --record-lightest-marking 13700 --record-soft-fill 13450
                                                  after plotting: store what held up

--pen is required and must be a profile name from ../config/pens.json. A pen's results are valid for that pen
type ONLY: a marker's behaviour says nothing about a ballpoint, a pencil or a fineliner, and vice versa. Pens
are expected to suit different jobs (a ballpoint for lines and contours, a marker for infill); that is a
hypothesis kept in pens.json as "expected_use", and these tests are what confirm or refute it.

FILL SHEET. Square patches, 12 mm each. Columns are line spacing, left to right: 0.3, 0.4, 0.5, 0.6, 0.8,
1.0, 1.5 mm. Rows are fill styles, top to bottom:

  row 1  hatch, one direction, pen lifted between lines    row 4  zigzag at 45 degrees, no lifts
  row 2  zigzag (serpentine), one continuous stroke        row 5  concentric rings, lifted between rings
  row 3  cross-hatch, 0 and 90 degrees, lifted             row 6  zigzag, then the outline drawn on top

Look at: where lines start to merge into a solid (spacing too tight for this pen), where gaps stay (too
open), whether the edges look clean (play shows as ragged ends), and whether zigzag turnarounds show.

PRESSURE SHEET. A small sheet (three styles x five spacings) that you plot several times on the same paper, each
time at a different pen-down servo value, so a lighter touch gives milder, softer lines that can read as tone.
Each block is drawn at its own pen pressure (data-pressure in the SVG, set per stroke by plot_svg.py), so one run
covers every level. A value closer to pen-up means a lighter touch. The levels only ever go LIGHTER than the
configured pen-down (never deeper: a too-deep value can gouge the paper or hurt the servo). The lightest block that
still marks cleanly is the lightest usable value for that pen.

Plot with the pen you are testing, at the speed you will use for real work.
"""
import argparse
import datetime
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPACINGS = [0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5]    # mm
STYLES = ["hatch lifted", "zigzag", "cross-hatch", "zigzag 45 deg", "concentric rings", "zigzag + outline"]
PATCH, PITCH, X0, Y0 = 12.0, 16.0, 4.0, 4.0       # mm
P_SPACINGS = [0.4, 0.6, 0.8, 1.0, 1.5]            # pressure sheet
P_STYLES = ["hatch lifted", "zigzag", "cross-hatch"]
# fraction of the way from pen-down toward pen-up; 0 = the configured pen-down (the heaviest touch we test)
DEFAULT_LEVELS = [0.0, 0.15, 0.3, 0.45, 0.6]
MAX_LEVEL = 0.8                                   # beyond this the pen is effectively up


def clip(p, d, size):
    """Parameter range (t0, t1) of the line p + t*d inside the square [0,size]^2, or None (Liang-Barsky)."""
    t0, t1 = -1e9, 1e9
    for pk, dk in ((p[0], d[0]), (p[1], d[1])):
        if abs(dk) < 1e-12:
            if pk < 0 or pk > size:
                return None
        else:
            ta, tb = (0 - pk) / dk, (size - pk) / dk
            if ta > tb:
                ta, tb = tb, ta
            t0, t1 = max(t0, ta), min(t1, tb)
    return (t0, t1) if t1 - t0 > 1e-6 else None


def hatch(size, spacing, angle_deg):
    """Parallel segments ((x,y),(x,y)) clipped to the square, ordered by offset, all drawn in the same direction."""
    th = math.radians(angle_deg)
    d = (math.cos(th), math.sin(th))
    n = (-math.sin(th), math.cos(th))
    projs = [x * n[0] + y * n[1] for x in (0, size) for y in (0, size)]
    lo, hi = min(projs), max(projs)
    segs = []
    o = lo + spacing / 2
    while o < hi:
        r = clip((n[0] * o, n[1] * o), d, size)
        if r:
            t0, t1 = r
            segs.append(((n[0] * o + d[0] * t0, n[1] * o + d[1] * t0), (n[0] * o + d[0] * t1, n[1] * o + d[1] * t1)))
        o += spacing
    return segs


def zigzag(segs):
    """Join segments into one continuous polyline, alternating direction."""
    pts = []
    for i, (a, b) in enumerate(segs):
        pts += [a, b] if i % 2 == 0 else [b, a]
    return pts


def rings(size, spacing):
    out = []
    k = 0.0
    while size - 2 * k > spacing:
        a, b = k, size - k
        out.append([(a, a), (b, a), (b, b), (a, b), (a, a)])
        k += spacing
    return out


def patch_paths(style, size, spacing):
    if style == "hatch lifted":
        return [list(s) for s in hatch(size, spacing, 0)]
    if style == "zigzag":
        return [zigzag(hatch(size, spacing, 0))]
    if style == "cross-hatch":
        return [list(s) for s in hatch(size, spacing, 0)] + [list(s) for s in hatch(size, spacing, 90)]
    if style == "zigzag 45 deg":
        return [zigzag(hatch(size, spacing, 45))]
    if style == "concentric rings":
        return rings(size, spacing)
    if style == "zigzag + outline":
        return [zigzag(hatch(size, spacing, 0)), [(0, 0), (size, 0), (size, size), (0, size), (0, 0)]]
    raise ValueError(style)


def build(pen, styles, spacings, what, colour=(None, None)):
    paths, layout = [[(0.0, 3.0), (0.0, 0.0), (3.0, 0.0)]], []   # corner mark
    for r, style in enumerate(styles):
        for c, sp in enumerate(spacings):
            ox, oy = X0 + c * PITCH, Y0 + r * PITCH
            for p in patch_paths(style, PATCH, sp):
                paths.append([(ox + x, oy + y) for x, y in p])
        layout.append({"row": r + 1, "style": style})
    w = X0 + (len(spacings) - 1) * PITCH + PATCH + 3.0
    h = Y0 + (len(styles) - 1) * PITCH + PATCH + 3.0
    d = "\n".join('<path d="' + " L".join(f"{'M' if k == 0 else ''}{x:.3f},{y:.3f}" for k, (x, y) in enumerate(p)) + '"/>'
                  for p in paths)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" viewBox="0 0 {w:.1f} {h:.1f}">\n'
           f'<!-- {what}; results valid ONLY for pen type "{pen}" (colour: {colour[0] or "not set"}) -->\n'
           f'<g fill="none" stroke="{colour[1] or "#000"}" stroke-width="0.3" stroke-linecap="round">\n{d}\n</g>\n</svg>\n')
    return svg, {"pen": pen, "colour": colour[0], "sheet": what, "spacings_mm": spacings, "patch_mm": PATCH,
                 "size_mm": [w, h], "rows": layout, "strokes": len(paths)}


def build_pressure(pen, levels, colour=(None, None)):
    """One sheet, one run: a block of patches per pressure level, each in a <g data-pressure="...">.
    Blocks are laid out two across, row by row, in level order."""
    bw = (len(P_SPACINGS) - 1) * PITCH + PATCH
    bh = (len(P_STYLES) - 1) * PITCH + PATCH
    gap = 10.0
    groups, layout = [], []
    for k, lv in enumerate(levels):
        bx, by = X0 + (k % 2) * (bw + gap), Y0 + (k // 2) * (bh + gap)
        paths = []
        for r, style in enumerate(P_STYLES):
            for c, sp in enumerate(P_SPACINGS):
                for p in patch_paths(style, PATCH, sp):
                    paths.append([(bx + c * PITCH + x, by + r * PITCH + y) for x, y in p])
        groups.append((lv, paths))
        layout.append({"block": k + 1, "pressure": lv, "origin_mm": [bx, by]})
    w = X0 + min(2, len(levels)) * (bw + gap) - gap + 3.0
    h = Y0 + math.ceil(len(levels) / 2) * (bh + gap) - gap + 3.0
    body = []
    for lv, paths in groups:
        d = "\n".join('<path d="' + " L".join(f"{'M' if k == 0 else ''}{x:.3f},{y:.3f}" for k, (x, y) in enumerate(p)) + '"/>'
                      for p in paths)
        body.append(f'<g data-pressure="{lv:g}">\n{d}\n</g>')
    corner = '<path d="M0.000,3.000 L0.000,0.000 L3.000,0.000"/>'
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" viewBox="0 0 {w:.1f} {h:.1f}">\n'
           f'<!-- infill pressure test; results valid ONLY for pen type "{pen}" (colour: {colour[0] or "not set"}) -->\n'
           f'<g fill="none" stroke="{colour[1] or "#000"}" stroke-width="0.3" stroke-linecap="round">\n{corner}\n'
           + "\n".join(body) + '\n</g>\n</svg>\n')
    return svg, {"pen": pen, "colour": colour[0], "sheet": "infill pressure test", "spacings_mm": P_SPACINGS,
                 "styles": P_STYLES, "patch_mm": PATCH, "size_mm": [w, h], "blocks": layout,
                 "strokes": sum(len(p) for _, p in groups) + 1}


def plot_cmd(svg_path, config):
    """plot_svg.py command line for a sheet: real path (relative to the repo when inside it), and --config when not the default."""
    try:
        shown = svg_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        shown = str(svg_path.resolve())
    cmd = f"python3 tools/plot_svg.py {shown}"
    if config and pathlib.Path(config).resolve() != (ROOT / "config" / "machine.json").resolve():
        cmd += f" --config {config}"
    return cmd


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pen", required=True, help="pen type this test is for; a profile name in config/pens.json")
    ap.add_argument("--pressure", action="store_true", help="write the pressure sheet and the per-level commands")
    ap.add_argument("--levels", help="pressure levels as fractions from pen-down toward pen-up, e.g. 0,0.2,0.4")
    ap.add_argument("--pens-file", default=str(ROOT / "config" / "pens.json"))
    ap.add_argument("--config", default=str(ROOT / "config" / "machine.json"), help="for the servo pen-up/down values")
    ap.add_argument("--out-dir", default=str(ROOT / "input"))
    ap.add_argument("--record-min-spacing", type=float, metavar="MM",
                    help="tightest line spacing (mm) that still looked clean and not merged, this pen")
    ap.add_argument("--record-best-style", choices=STYLES, help="fill style that looked best for this pen")
    ap.add_argument("--record-lightest-marking", type=int, metavar="SERVO",
                    help="pen-down servo value closest to pen-up that still left a continuous line, this pen")
    ap.add_argument("--record-soft-fill", type=int, metavar="SERVO",
                    help="pen-down servo value that gave the best soft, milder infill, this pen")
    a = ap.parse_args()

    pens_path = pathlib.Path(a.pens_file)
    pens = json.loads(pens_path.read_text())
    if a.pen not in pens:
        sys.exit(f"unknown pen {a.pen!r}; known: {', '.join(sorted(pens))}. Add a profile to {pens_path.name} first. "
                 "Results do not carry over between pen types, so each type needs its own test.")

    recs = {"min_clean_spacing_mm": a.record_min_spacing, "best_style": a.record_best_style,
            "lightest_marking_pen_down": a.record_lightest_marking, "soft_fill_pen_down": a.record_soft_fill}
    if any(v is not None for v in recs.values()):
        res = pens[a.pen].setdefault("fill_results", {})
        res.update({k: v for k, v in recs.items() if v is not None})
        res["recorded"] = datetime.date.today().isoformat()
        res["valid_for_pen_only"] = a.pen
        pens_path.write_text(json.dumps(pens, indent=2) + "\n")
        print(f"recorded fill results for pen {a.pen!r} only: {res}")
        return 0

    out = pathlib.Path(a.out_dir)
    out.mkdir(exist_ok=True)
    colour = (pens[a.pen].get("colour"), pens[a.pen].get("colour_hex"))
    if a.pressure:
        levels = [float(x) for x in a.levels.split(",")] if a.levels else DEFAULT_LEVELS
        if any(l < 0 or l > MAX_LEVEL for l in levels):
            sys.exit(f"levels must be between 0 and {MAX_LEVEL} (0 = the configured pen-down; the test never goes deeper)")
        svg, layout = build_pressure(a.pen, levels, colour)
        name = f"pressure-test-{a.pen}"
    else:
        svg, layout = build(a.pen, STYLES, SPACINGS, "infill test", colour)
        name = f"fill-test-{a.pen}"
    svg_path = out / f"{name}.svg"
    svg_path.write_text(svg)
    (out / f"{name}.json").write_text(json.dumps(layout, indent=2))
    w, h = layout["size_mm"]
    kind = f"{len(levels)} pressure levels x " if a.pressure else ""
    nstyles = len(P_STYLES) if a.pressure else len(layout["rows"])
    print(f"Wrote {svg_path}  ({w:.0f} x {h:.0f} mm, {kind}{nstyles} styles x {len(layout['spacings_mm'])} spacings, "
          f"{layout['strokes']} strokes)")
    print(f"RESULTS FROM THIS SHEET APPLY ONLY TO PEN TYPE {a.pen!r}. Do not use them for any other pen.")
    print(f"Pen colour tag: {colour[0] or 'NOT SET (add \"colour\" to this pen in pens.json)'}")
    print("Spacings, mm, left to right: " + ", ".join(str(x) for x in layout["spacings_mm"]))
    for r in (layout["rows"] if not a.pressure else [{"row": i + 1, "style": s} for i, s in enumerate(P_STYLES)]):
        print(f"  row {r['row']}: {r['style']}")

    if not a.pressure:
        base = plot_cmd(svg_path, a.config)
        print(f"\nDry run : {base} --mode dry\nTrace   : {base} --mode trace\nPlot    : {base} --mode plot")
        print(f"\nThen record what held up, e.g.: fill_test.py --pen {a.pen} --record-min-spacing 0.6 "
              "--record-best-style zigzag")
        return 0

    servo = json.loads(pathlib.Path(a.config).read_text())["servo"]
    up, down = servo["pen_up"], servo["pen_down"]
    base = plot_cmd(svg_path, a.config)
    print(f"\nConfigured servo: pen-up {up}, pen-down {down}. One run draws every block, each at its own pen-down value "
          "(fraction from pen-down toward pen-up; larger = lighter). Blocks, two across, row by row:")
    for b in layout["blocks"]:
        val = round(down + b["pressure"] * (up - down))
        print(f"  block {b['block']}: pressure {b['pressure']:g} -> pen-down {val}")
    print(f"\nDry run : {base} --mode dry\nTrace   : {base} --mode trace\nPlot    : {base} --mode plot")
    print("\nNote the lightest block that still marks cleanly, and which gives the best soft infill, then record the servo values, e.g.: "
          f"fill_test.py --pen {a.pen} --record-lightest-marking 13700 --record-soft-fill 13450")
    return 0


if __name__ == "__main__":
    sys.exit(main())
