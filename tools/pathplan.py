"""Path planning for plot_svg.py: shorter pen-up travel and duplicate strokes removed.

order_block() orders strokes greedily by nearest next start, reversing open strokes and starting closed ones at
the vertex nearest the pen. A grid of cells makes each lookup near constant time, so tens of thousands of hatch
strokes plan in about a second. dedupe() drops strokes that repeat an earlier stroke of the same ink exactly
(either direction, any start point on a closed path).
"""
import math


def is_closed(s, tol):
    return len(s) > 3 and math.dist(s[0], s[-1]) <= tol


def corner_vertices(s, min_turn=30.0):
    """Vertices of a closed stroke where it turns by min_turn degrees or more."""
    ring = s[:-1]
    n = len(ring)
    out = []
    for k in range(n):
        a, b, c = ring[k - 1], ring[k], ring[(k + 1) % n]
        a1, a2 = math.atan2(b[1] - a[1], b[0] - a[0]), math.atan2(c[1] - b[1], c[0] - b[0])
        turn = abs((math.degrees(a2 - a1) + 180) % 360 - 180)
        if turn >= min_turn:
            out.append(k)
    return out


def order_block(strokes, idxs, cur, cell=4.0, closed_tol=0.1, corners=False, seams=None):
    """Order the strokes idxs from position cur. Strokes are changed in place (reversed or turned to start at a
    vertex; with corners, a corner vertex when the path has one). -> (order, position after the last stroke)."""
    if not idxs:
        return [], cur
    grid = {}
    for i in idxs:
        s = strokes[i]
        ks = range(len(s) - 1) if is_closed(s, closed_tol) else ((0, len(s) - 1) if len(s) > 1 else (0,))
        if corners and is_closed(s, closed_tol):
            ks = corner_vertices(s) or ks
        if seams and seams.get(i) is not None and is_closed(s, closed_tol):     # a chosen seam: start there
            sx, sy = seams[i]
            ks = [min(range(len(s) - 1), key=lambda k: (s[k][0] - sx) ** 2 + (s[k][1] - sy) ** 2)]
        for k in ks:
            x, y = s[k]
            grid.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append((x, y, i, k))
    remaining = set(idxs)
    gx0, gx1 = min(c[0] for c in grid), max(c[0] for c in grid)
    gy0, gy1 = min(c[1] for c in grid), max(c[1] for c in grid)
    order = []
    while remaining:
        cx, cy = math.floor(cur[0] / cell), math.floor(cur[1] / cell)
        rmax = max(cx - gx0, gx1 - cx, cy - gy0, gy1 - cy, 0)
        best, bd2 = None, math.inf
        r = 0
        # Square rings of cells around the pen. A point in ring r is at least (r - 1) cells away, so stop once
        # that is farther than the best found.
        while r <= rmax and (best is None or ((r - 1) * cell) ** 2 <= bd2):
            for gx in range(cx - r, cx + r + 1):
                gys = range(cy - r, cy + r + 1) if abs(gx - cx) == r else (cy - r, cy + r)
                for gy in gys:
                    lst = grid.get((gx, gy))
                    if not lst:
                        continue
                    live = [c for c in lst if c[2] in remaining]
                    if len(live) != len(lst):
                        grid[(gx, gy)] = live
                    for x, y, i, k in live:
                        d2 = (x - cur[0]) ** 2 + (y - cur[1]) ** 2
                        if d2 < bd2:
                            best, bd2 = (i, k), d2
            r += 1
        i, k = best
        s = strokes[i]
        if is_closed(s, closed_tol):
            if k:
                ring = s[:-1]
                strokes[i] = ring[k:] + ring[:k] + [ring[k]]
        elif k and k == len(s) - 1:
            strokes[i] = s[::-1]
        order.append(i)
        remaining.discard(i)
        cur = strokes[i][-1]
    return order, cur


def _canon(s, q, closed_tol):
    pts = [(round(x / q), round(y / q)) for x, y in s]
    if is_closed(s, closed_tol):
        ring = pts[:-1]
        k = ring.index(min(ring))
        fwd = ring[k:] + ring[:k]
        back = [fwd[0]] + fwd[:0:-1]
        return ("c", tuple(min(fwd, back)))
    return ("o", tuple(min(pts, pts[::-1])))


def dedupe(strokes, metas, tol=0.05, closed_tol=0.1):
    """Drop strokes that repeat an earlier one of the same ink and pressure. -> (strokes, metas, dropped)."""
    seen = set()
    out_s, out_m = [], []
    for s, m in zip(strokes, metas):
        key = (m.get("color"), m.get("layer"), m.get("pressure"), bool(m.get("fill")), _canon(s, tol, closed_tol))
        if key in seen:
            continue
        seen.add(key)
        out_s.append(s)
        out_m.append(m)
    return out_s, out_m, len(strokes) - len(out_s)


def drop_overlaps(strokes, metas, tol=0.1):
    """Parts of strokes that run along an earlier stroke of the same ink, pen and fill-ness (within tol mm) are cut
    out; what is left of a stroke stays, in pieces. Needs shapely. -> (strokes, metas, mm removed)."""
    try:
        from shapely.geometry import LineString
        from shapely.ops import unary_union
        from shapely.strtree import STRtree
    except ImportError:
        return strokes, metas, None
    lines = [LineString(s) if len(s) >= 2 else None for s in strokes]
    bufs = [ln.buffer(tol, cap_style="flat") if ln is not None else None for ln in lines]
    idx = [i for i, b in enumerate(bufs) if b is not None]
    tree = STRtree([bufs[i] for i in idx])
    key = lambda m: (m.get("color"), m.get("layer"), m.get("pressure"), bool(m.get("fill")))  # noqa: E731
    out_s, out_m, removed = [], [], 0.0
    for i, (s, m) in enumerate(zip(strokes, metas)):
        ln = lines[i]
        if ln is None or ln.length == 0:
            out_s.append(s)
            out_m.append(m)
            continue
        cands = [idx[j] for j in tree.query(ln) if idx[j] < i and key(metas[idx[j]]) == key(m)]
        if not cands:
            out_s.append(s)
            out_m.append(m)
            continue
        rest = ln.difference(unary_union([bufs[j] for j in cands]))
        removed += ln.length - rest.length
        for part in getattr(rest, "geoms", [rest]):
            if part.geom_type == "LineString" and part.length > tol * 2:
                out_s.append([tuple(p) for p in part.coords])
                out_m.append(dict(m))
    return out_s, out_m, removed


def join_touching(paths, tol=0.2):
    """Chain strokes whose ends lie within tol mm into one stroke (fewer pen lifts). Where three or more ends meet
    nothing is joined there: which branch continues would be a guess. Closed strokes stay as they are."""
    paths = [list(p) for p in paths if len(p) >= 2]
    while True:
        grid = {}
        for i, p in enumerate(paths):
            if len(p) > 2 and math.dist(p[0], p[-1]) <= tol:
                continue
            for e in (0, -1):
                grid.setdefault((int(p[e][0] // tol), int(p[e][1] // tol)), []).append((i, e))
        used, merged = set(), False
        for (cx, cy), items in list(grid.items()):
            for i, ea in items:
                if i in used:
                    continue
                pt = paths[i][ea]
                near = [(j, eb) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j, eb in grid.get((cx + dx, cy + dy), ())
                        if (j, eb) != (i, ea) and j not in used and math.dist(pt, paths[j][eb]) <= tol]
                if len(near) != 1 or near[0][0] == i or near[0][0] in used:
                    continue
                j, eb = near[0]
                p = paths[i] if ea == -1 else paths[i][::-1]
                q = paths[j] if eb == 0 else paths[j][::-1]
                paths[i], paths[j] = p + q[1:], None
                used.update((i, j))
                merged = True
        paths = [p for p in paths if p is not None]
        if not merged:
            return paths


def travel(strokes, order, start):
    """Pen-up distance of an order, from start and back to it."""
    cur, d = start, 0.0
    for i in order:
        d += math.dist(cur, strokes[i][0])
        cur = strokes[i][-1]
    return d + math.dist(cur, start)
