"""Shape operations on bed objects: union, subtract, intersect and offset (shapely).

Each object is turned into its strokes in machine mm (the composed SVG read by plot_svg.load_svg). Closed strokes
become areas, even-odd within one object so holes stay holes; open strokes only take part in an offset, where
they grow into a band around the line. The result is a new drawing of outlines in machine mm.
"""
import math

try:
    import shapely
    from shapely.geometry import LineString, MultiLineString, Polygon
    from shapely.ops import unary_union
except ImportError:          # the page hides the shape tools when the server has no shapely
    shapely = None

CLOSE_TOL = 0.15     # mm: a stroke whose ends are this close counts as closed
OPS = ("union", "subtract", "intersect", "offset")


class ShapeError(ValueError):
    pass


def available():
    return shapely is not None


def area_of(strokes):
    """Closed strokes of one object -> one area (even-odd), open strokes -> lines."""
    area, lines = None, []
    for s in strokes:
        if len(s) >= 4 and math.dist(s[0], s[-1]) <= CLOSE_TOL:
            p = shapely.make_valid(Polygon(s))
            if p.is_empty or p.area < 1e-6:
                continue
            area = p if area is None else area.symmetric_difference(p)
        elif len(s) >= 2:
            lines.append(LineString(s))
    return area, lines


def run(op, objects_strokes, distance=0.0, join="round"):
    """objects_strokes: one list of strokes (lists of (x, y) mm) per object, in order. -> list of polylines."""
    if shapely is None:
        raise ShapeError("Shape tools need shapely on the server (python3-shapely).")
    if op not in OPS:
        raise ShapeError("Unknown shape operation.")
    parts = [area_of(s) for s in objects_strokes]
    areas = [a for a, _ in parts if a is not None and not a.is_empty]
    if op == "offset":
        if not -50 <= distance <= 50 or distance == 0:
            raise ShapeError("The offset must be between -50 and 50 mm, and not 0.")
        js = {"round": "round", "mitre": "mitre", "bevel": "bevel"}.get(join, "round")
        geoms = areas + [ln for _, lines in parts for ln in lines if distance > 0]
        if not geoms:
            raise ShapeError("Nothing to offset: an inward offset needs closed shapes.")
        res = unary_union(geoms).buffer(distance, join_style=js, mitre_limit=4.0, quad_segs=16)
    else:
        if len(objects_strokes) < 2:
            raise ShapeError("Select at least two objects.")
        if len(areas) < 2:
            raise ShapeError("These need closed shapes in at least two of the objects; lines have no inside.")
        if op == "union":
            res = unary_union(areas)
        elif op == "intersect":
            res = areas[0]
            for a in areas[1:]:
                res = res.intersection(a)
        else:
            if parts[0][0] is None:
                raise ShapeError("The first object (the one that is cut) has no closed shape.")
            res = parts[0][0].difference(unary_union(areas[1:]) if len(areas) > 1 else Polygon())
    out = outlines(res)
    if not out:
        raise ShapeError("The result is empty: the shapes do not overlap that way.")
    return out


# ------------------------------------------------------------------ line edits (strokes in machine mm)
# Each takes [(colour, stroke)] and returns the same, for the bed's line tools. Pure geometry; the ones that need
# areas use shapely and say so when it is missing.

def _closed(s, tol=0.05):
    return len(s) > 2 and math.dist(s[0], s[-1]) <= tol


def close_gaps(strokes, tol):
    """Strokes whose ends are within tol mm get closed exactly."""
    out = []
    for c, s in strokes:
        s = list(s)
        if len(s) >= 3 and 0 < math.dist(s[0], s[-1]) <= tol:
            s.append(s[0])
        out.append((c, s))
    return out


def smooth(strokes, passes=2):
    """Chaikin corner cutting: rounder curves, the ends of open strokes stay put."""
    out = []
    for c, s in strokes:
        pts = list(s)
        closed = _closed(pts)
        for _ in range(passes):
            if len(pts) < 3:
                break
            ring = pts[:-1] if closed else pts
            new = [] if closed else [ring[0]]
            n = len(ring)
            for i in range(n if closed else n - 1):
                a, b = ring[i], ring[(i + 1) % n]
                new += [(0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]),
                        (0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1])]
            if closed:
                new.append(new[0])
            else:
                new.append(ring[-1])
            pts = new
        out.append((c, pts))
    return out


def densify(s, step=1.0):
    out = [s[0]]
    for a, b in zip(s, s[1:]):
        n = max(1, math.ceil(math.dist(a, b) / step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n + 1)]
    return out


def affine(strokes, m):
    a, b, c, d, e, f = m
    return [(col, [(a * x + c * y + e, b * x + d * y + f) for x, y in s]) for col, s in strokes]


def warp(strokes, quad):
    """Bilinear map of the strokes' bounding box onto quad (top-left, top-right, bottom-right, bottom-left)."""
    xs = [x for _, s in strokes for x, _ in s]
    ys = [y for _, s in strokes for _, y in s]
    x0, y0, w, h = min(xs), min(ys), max(max(xs) - min(xs), 1e-9), max(max(ys) - min(ys), 1e-9)
    (ax, ay), (bx, by), (cx, cy), (dx, dy) = quad

    def f(x, y):
        u, v = (x - x0) / w, (y - y0) / h
        tx, ty = ax + (bx - ax) * u, ay + (by - ay) * u
        bx_, by_ = dx + (cx - dx) * u, dy + (cy - dy) * u
        return tx + (bx_ - tx) * v, ty + (by_ - ty) * v
    return [(col, [f(x, y) for x, y in densify(s, 1.0)]) for col, s in strokes]


def hull(strokes):
    if shapely is None:
        raise ShapeError("The convex outline needs shapely on the server.")
    from shapely.geometry import MultiPoint
    g = MultiPoint([p for _, s in strokes for p in s]).convex_hull
    if g.geom_type != "Polygon":
        raise ShapeError("The selection is a line or a point: it has no outline.")
    return [(strokes[0][0], [(round(x, 3), round(y, 3)) for x, y in g.exterior.coords])]


def fillet(strokes, r):
    """Round every corner of the closed strokes with radius r (convex and concave); open strokes stay."""
    if shapely is None:
        raise ShapeError("Rounding corners needs shapely on the server.")
    out = []
    for c, s in strokes:
        if not _closed(s, CLOSE_TOL) or len(s) < 4:
            out.append((c, s))
            continue
        p = shapely.make_valid(Polygon(s))
        g = p.buffer(-r, join_style="round").buffer(r, join_style="round")
        g = g.buffer(r, join_style="round").buffer(-r, join_style="round")
        if g.is_empty:
            raise ShapeError(f"A radius of {r:g} mm is larger than a shape: nothing would be left.")
        out += [(c, ln) for ln in outlines(g)]
    return out


def cut(cutter, strokes, keep="outside"):
    """Lines cut by the closed shapes of the cutter: keep what lies outside (or inside) them."""
    if shapely is None:
        raise ShapeError("Cutting needs shapely on the server.")
    area, _ = area_of([s for _, s in cutter])
    if area is None or area.is_empty:
        raise ShapeError("The first object selected is the cutter: it needs a closed shape.")
    out = []
    for c, s in strokes:
        ln = LineString(s) if len(s) >= 2 else None
        if ln is None:
            continue
        g = ln.difference(area) if keep == "outside" else ln.intersection(area)
        for part in getattr(g, "geoms", [g]):
            if part.geom_type == "LineString" and not part.is_empty and part.length > 0.05:
                out.append((c, [(round(x, 3), round(y, 3)) for x, y in part.coords]))
    if not out:
        raise ShapeError("Nothing is left after the cut.")
    return out


def trim(strokes, x, y, tol=3.0):
    """Split every line where lines cross, and drop the piece nearest (x, y) (within tol mm)."""
    if shapely is None:
        raise ShapeError("Trimming needs shapely on the server.")
    from shapely.geometry import Point
    from shapely.ops import linemerge
    pieces = []
    by = {}
    for c, s in strokes:
        if len(s) >= 2:
            by.setdefault(c, []).append(LineString(s))
    for c, lines in by.items():
        noded = unary_union(lines)
        for part in getattr(noded, "geoms", [noded]):
            pieces.append((c, part))
    if not pieces:
        raise ShapeError("These objects have no lines to trim.")
    pt = Point(x, y)
    i = min(range(len(pieces)), key=lambda k: pieces[k][1].distance(pt))
    if pieces[i][1].distance(pt) > tol:
        raise ShapeError(f"No line within {tol:g} mm of the click.")
    del pieces[i]
    out = []
    for c in by:
        rest = [g for col, g in pieces if col == c]
        if not rest:
            continue
        merged = linemerge(rest) if len(rest) > 1 else rest[0]
        for part in getattr(merged, "geoms", [merged]):
            out.append((c, [(round(px, 3), round(py, 3)) for px, py in part.coords]))
    if not out:
        raise ShapeError("Nothing is left.")
    return out


def outlines(g):
    """Every boundary of a geometry as closed polylines."""
    out = []
    if g.is_empty:
        return out
    b = g.boundary
    lines = b.geoms if isinstance(b, MultiLineString) else [b]
    for ln in lines:
        pts = [(round(x, 3), round(y, 3)) for x, y in ln.coords]
        if len(pts) >= 2:
            out.append(pts)
    return out


def make_shape(d):
    """A drawn shape as SVG text in mm, from the bed's Shape tool. -> (svg, name, [x, y] of its top-left or None).

    kind rect (w, h, r: corner radius), ellipse (w, h), polygon (w, h, n sides), star (w, h, n points, inner: % of
    the outer radius), poly (pts: machine mm, closed). Raises ValueError with a plain message."""
    kind = d.get("kind")

    def num(k, lo, hi, label, default=None):
        v = d.get(k, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number.")
        if not lo <= v <= hi:
            raise ValueError(f"{label} must be {lo:g} to {hi:g}.")
        return v

    at = None
    if kind == "poly":
        pts = d.get("pts")
        if not isinstance(pts, list) or not 2 <= len(pts) <= 2000:
            raise ValueError("A line needs 2 to 2000 points.")
        try:
            pts = [(float(x), float(y)) for x, y in pts]
        except (TypeError, ValueError):
            raise ValueError("Points must be [x, y] pairs in mm.")
        if not all(math.isfinite(v) and -1000 <= v <= 2000 for p in pts for v in p):
            raise ValueError("Points must be on the bed.")
        x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
        w, h = max(max(p[0] for p in pts) - x0, 0.01), max(max(p[1] for p in pts) - y0, 0.01)
        body = "M" + " L".join(f"{x - x0:.3f} {y - y0:.3f}" for x, y in pts) + (" Z" if d.get("closed") else "")
        name, at = f"line-{len(pts)}pts", [x0, y0]
    else:
        w = num("w", 1, 500, "Width")
        h = num("h", 1, 500, "Height")
        if kind == "rect":
            r = min(num("r", 0, 250, "Corner radius", 0), w / 2, h / 2)
            if r > 0:
                body = (f"M{r:g} 0 H{w - r:g} A{r:g} {r:g} 0 0 1 {w:g} {r:g} V{h - r:g} A{r:g} {r:g} 0 0 1 {w - r:g} {h:g} "
                        f"H{r:g} A{r:g} {r:g} 0 0 1 0 {h - r:g} V{r:g} A{r:g} {r:g} 0 0 1 {r:g} 0 Z")
            else:
                body = f"M0 0 H{w:g} V{h:g} H0 Z"
            name = f"rect-{w:g}x{h:g}"
        elif kind == "ellipse":
            rx, ry = w / 2, h / 2
            body = f"M0 {ry:g} A{rx:g} {ry:g} 0 1 1 {w:g} {ry:g} A{rx:g} {ry:g} 0 1 1 0 {ry:g} Z"
            name = f"ellipse-{w:g}x{h:g}"
        elif kind in ("polygon", "star"):
            n = int(num("n", 3, 64, "Sides" if kind == "polygon" else "Points", 5))
            inner = num("inner", 5, 95, "Inner radius", 45) / 100 if kind == "star" else 1.0
            k = 2 * n if kind == "star" else n
            raw = [((inner if kind == "star" and i % 2 else 1.0) * math.cos(-math.pi / 2 + 2 * math.pi * i / k),
                    (inner if kind == "star" and i % 2 else 1.0) * math.sin(-math.pi / 2 + 2 * math.pi * i / k))
                   for i in range(k)]
            bx0, by0 = min(p[0] for p in raw), min(p[1] for p in raw)
            bw, bh = max(p[0] for p in raw) - bx0, max(p[1] for p in raw) - by0
            body = "M" + " L".join(f"{(x - bx0) / bw * w:.3f} {(y - by0) / bh * h:.3f}" for x, y in raw) + " Z"
            name = f"{kind}-{n}-{w:g}x{h:g}"
        else:
            raise ValueError("Unknown shape.")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.3f}mm" height="{h:.3f}mm" viewBox="0 0 {w:.3f} {h:.3f}">'
           f'<path d="{body}" fill="none" stroke="#000000" stroke-width="0.3" stroke-linejoin="round"/></svg>')
    return svg, name, at


def svg_layers(groups):
    """groups: [(colour, polys)] -> (svg text, bounds), like svg() with one path per colour."""
    allp = [s for _, ps in groups for s in ps]
    xs = [x for s in allp for x, _ in s]
    ys = [y for s in allp for _, y in s]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    w, h = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
    body = "".join(
        f'<path d="{" ".join("M" + " L".join(f"{x - x0:.3f} {y - y0:.3f}" for x, y in s) for s in ps)}" fill="none" '
        f'stroke="{c}" stroke-width="0.3" stroke-linecap="round" stroke-linejoin="round"/>' for c, ps in groups if ps)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.3f}mm" height="{h:.3f}mm" '
            f'viewBox="0 0 {w:.3f} {h:.3f}">{body}</svg>'), [x0, y0, x1, y1]


def svg(polys, color="#000000"):
    """-> (svg text, bounds [x0, y0, x1, y1] in mm). The drawing's own mm are machine mm minus the corner."""
    xs = [x for s in polys for x, _ in s]
    ys = [y for s in polys for _, y in s]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    w, h = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
    d = " ".join("M" + " L".join(f"{x - x0:.3f} {y - y0:.3f}" for x, y in s) for s in polys)
    text = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.3f}mm" height="{h:.3f}mm" viewBox="0 0 {w:.3f} {h:.3f}">'
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="0.3"/></svg>')
    return text, [x0, y0, x1, y1]
