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
