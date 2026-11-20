#!/usr/bin/env python3
"""DXF and HPGL drawings to plottable SVG (mm). Stdlib only.

  cad_to_svg.py part.dxf out.svg          ASCII DXF: LINE, LWPOLYLINE, POLYLINE/VERTEX, CIRCLE, ARC, ELLIPSE, SPLINE
                                          (through its fit or control points), POINT is skipped. Units from
                                          $INSUNITS (mm when missing). Layers keep their DXF colour number as a pen.
  cad_to_svg.py plot.plt out.svg          HPGL: IN, SP, PU, PD, PA, PR, CI (40 plotter units per mm). Each SP pen number
                                          is its own colour.

Both formats count y upwards; the SVG counts down, so the drawing is flipped to look as it did in the program.
"""
import math
import pathlib
import re
import sys

INSUNITS_MM = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 0.0000254, 9: 0.0254, 10: 914.4}
ACI = {1: "#e00000", 2: "#d9a400", 3: "#00a000", 4: "#00a0b0", 5: "#2463a6", 6: "#c2187e", 7: "#000000"}
PEN_COLORS = ["#000000", "#000000", "#e00000", "#00a000", "#2463a6", "#c2187e", "#d9a400", "#00a0b0", "#7f4f24"]


def _arc(cx, cy, r, a0, a1, step_deg=5.0):
    while a1 <= a0:
        a1 += 360.0
    n = max(2, math.ceil((a1 - a0) / step_deg))
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * k / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * k / n))) for k in range(n + 1)]


def _bulge_pts(p0, p1, bulge):
    """An LWPOLYLINE segment with a bulge (tan of a quarter of the arc angle) as points."""
    if abs(bulge) < 1e-9:
        return [p1]
    (x0, y0), (x1, y1) = p0, p1
    ang = 4 * math.atan(bulge)
    d = math.dist(p0, p1)
    if d < 1e-12:
        return [p1]
    r = d / (2 * math.sin(abs(ang) / 2))
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    h = math.sqrt(max(0.0, r * r - (d / 2) ** 2)) * (1 if (bulge > 0) == (abs(ang) < math.pi) else -1)
    ux, uy = (x1 - x0) / d, (y1 - y0) / d
    cx, cy = mx - uy * h, my + ux * h
    a0 = math.degrees(math.atan2(y0 - cy, x0 - cx))
    a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
    pts = _arc(cx, cy, r, a0, a1) if bulge > 0 else _arc(cx, cy, r, a1, a0)[::-1]
    return pts[1:]


def read_dxf(text):
    """-> [(colour, polyline)] in drawing units, and the unit in mm."""
    lines = text.splitlines()
    pairs = [(lines[i].strip(), lines[i + 1].strip()) for i in range(0, len(lines) - 1, 2)]
    unit = 1.0
    for i, (c, v) in enumerate(pairs):
        if c == "9" and v == "$INSUNITS" and i + 1 < len(pairs):
            try:
                unit = INSUNITS_MM.get(int(pairs[i + 1][1]), 1.0)
            except ValueError:
                pass
    out, i, in_ent = [], 0, False
    ents = []
    while i < len(pairs):
        c, v = pairs[i]
        if c == "2" and v == "ENTITIES":
            in_ent = True
        elif c == "0" and v == "ENDSEC":
            in_ent = False
        elif in_ent and c == "0":
            ent = {"type": v, "codes": []}
            j = i + 1
            while j < len(pairs) and pairs[j][0] != "0":
                ent["codes"].append(pairs[j])
                j += 1
            ents.append(ent)
            i = j
            continue
        i += 1

    def f(ent, code, default=0.0):
        for c, v in ent["codes"]:
            if c == code:
                try:
                    return float(v)
                except ValueError:
                    return default
        return default

    def colour(ent):
        for c, v in ent["codes"]:
            if c == "62":
                try:
                    return ACI.get(abs(int(v)), "#000000")
                except ValueError:
                    break
        return "#000000"
    k = 0
    while k < len(ents):
        e = ents[k]
        t, col = e["type"], colour(e)
        if t == "LINE":
            out.append((col, [(f(e, "10"), f(e, "20")), (f(e, "11"), f(e, "21"))]))
        elif t == "LWPOLYLINE":
            xs = [float(v) for c, v in e["codes"] if c == "10"]
            ys = [float(v) for c, v in e["codes"] if c == "20"]
            bul, cur = [], None
            for c, v in e["codes"]:          # a bulge (42) belongs to the vertex before it
                if c == "10":
                    if cur is not None:
                        bul.append(cur)
                    cur = 0.0
                elif c == "42" and cur is not None:
                    cur = float(v)
            if cur is not None:
                bul.append(cur)
            closed = int(f(e, "70")) & 1
            vs = list(zip(xs, ys))
            if vs:
                pts = [vs[0]]
                for n in range(1, len(vs) + (1 if closed else 0)):
                    pts += _bulge_pts(vs[n - 1], vs[n % len(vs)], bul[n - 1] if n - 1 < len(bul) else 0.0)
                out.append((col, pts))
        elif t == "POLYLINE":
            closed = int(f(e, "70")) & 1
            pts = []
            k += 1
            while k < len(ents) and ents[k]["type"] == "VERTEX":
                pts.append((f(ents[k], "10"), f(ents[k], "20")))
                k += 1
            if closed and pts:
                pts.append(pts[0])
            if len(pts) >= 2:
                out.append((col, pts))
            continue
        elif t == "CIRCLE":
            out.append((col, _arc(f(e, "10"), f(e, "20"), f(e, "40"), 0, 360)))
        elif t == "ARC":
            out.append((col, _arc(f(e, "10"), f(e, "20"), f(e, "40"), f(e, "50"), f(e, "51"))))
        elif t == "ELLIPSE":
            cx, cy, mx, my, ratio = f(e, "10"), f(e, "20"), f(e, "11"), f(e, "21"), f(e, "40", 1.0)
            t0, t1 = f(e, "41", 0.0), f(e, "42", 2 * math.pi)
            a = math.hypot(mx, my)
            rot = math.atan2(my, mx)
            while t1 <= t0:
                t1 += 2 * math.pi
            n = max(8, math.ceil((t1 - t0) / math.radians(5)))
            pts = []
            for s in range(n + 1):
                tt = t0 + (t1 - t0) * s / n
                ex, ey = a * math.cos(tt), a * ratio * math.sin(tt)
                pts.append((cx + ex * math.cos(rot) - ey * math.sin(rot), cy + ex * math.sin(rot) + ey * math.cos(rot)))
            out.append((col, pts))
        elif t == "SPLINE":
            fit = list(zip([float(v) for c, v in e["codes"] if c == "11"], [float(v) for c, v in e["codes"] if c == "21"]))
            ctl = list(zip([float(v) for c, v in e["codes"] if c == "10"], [float(v) for c, v in e["codes"] if c == "20"]))
            pts = fit if len(fit) >= 2 else ctl
            if len(pts) >= 2:
                out.append((col, pts))
        k += 1
    return out, unit


def read_hpgl(text):
    """-> [(colour, polyline)] in plotter units (1/40 mm)."""
    out, cur, pen_down, absolute, pos, pen = [], None, False, True, (0.0, 0.0), 1
    for raw in re.split(r"[;\n]", text):
        raw = raw.strip()
        if len(raw) < 2:
            continue
        cmd, arg = raw[:2].upper(), raw[2:]
        nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", arg)]
        if cmd == "IN":
            pen_down, absolute, pos = False, True, (0.0, 0.0)
        elif cmd == "SP":
            pen = int(nums[0]) if nums else 0
            cur = None
        elif cmd in ("PU", "PD", "PA", "PR"):
            if cmd == "PU":
                pen_down = False
                cur = None
            elif cmd == "PD":
                pen_down = True
            elif cmd == "PA":
                absolute = True
            elif cmd == "PR":
                absolute = False
            for x, y in zip(nums[0::2], nums[1::2]):
                nxt = (x, y) if absolute else (pos[0] + x, pos[1] + y)
                if pen_down and pen:
                    if cur is None:
                        cur = [pos]
                        out.append((PEN_COLORS[pen % len(PEN_COLORS)], cur))
                    cur.append(nxt)
                pos = nxt
        elif cmd == "CI" and nums:
            out.append((PEN_COLORS[pen % len(PEN_COLORS)], _arc(pos[0], pos[1], nums[0], 0, 360)))
    return [(c, p) for c, p in out if len(p) >= 2]


def convert(src, dst):
    """-> stats. Raises ValueError with a plain message."""
    raw = pathlib.Path(src).read_bytes()
    text = raw.decode("utf-8", "replace")
    ext = pathlib.Path(src).suffix.lower()
    if ext == ".dxf":
        if "ENTITIES" not in text:
            raise ValueError("This DXF has no ENTITIES section (binary DXF is not read; save it as ASCII DXF).")
        strokes, unit = read_dxf(text)
    else:
        strokes, unit = read_hpgl(text), 1 / 40.0
    if not strokes:
        raise ValueError("Nothing drawable was found in the file.")
    pts = [p for _, s in strokes for p in s]
    x0, y1 = min(p[0] for p in pts), max(p[1] for p in pts)
    w = (max(p[0] for p in pts) - x0) * unit
    h = (y1 - min(p[1] for p in pts)) * unit
    by = {}
    for c, s in strokes:
        by.setdefault(c, []).append("M" + " L".join(f"{(x - x0) * unit:.3f} {(y1 - y) * unit:.3f}" for x, y in s))
    body = "".join(f'<path d="{" ".join(ds)}" fill="none" stroke="{c}" stroke-width="0.3"/>' for c, ds in by.items())
    pathlib.Path(dst).write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{max(w, 0.1):.3f}mm" '
                                 f'height="{max(h, 0.1):.3f}mm" viewBox="0 0 {max(w, 0.1):.3f} {max(h, 0.1):.3f}">'
                                 f'{body}</svg>', encoding="utf-8")
    return {"strokes": len(strokes), "size_mm": [round(w, 2), round(h, 2)], "colours": len(by)}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(convert(sys.argv[1], sys.argv[2]))
