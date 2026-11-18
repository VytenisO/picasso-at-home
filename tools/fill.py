#!/usr/bin/env python3
"""Fills for a pen plotter: hatching of closed shapes and of tone. Stdlib only, never touches hardware.

A pen cannot fill, so a filled shape becomes parallel lines. Two users:

  hatch_rings   closed shapes (with holes) -> hatch lines clipped to the shape, even-odd or nonzero rule
  tone_runs     a darkness function (a photo) -> hatch lines wherever it is dark enough

Both return polylines [(x, y), ...] in the units they were given (mm for the plotter). Adjacent lines of a shape
are joined into one continuous zigzag stroke when their ends are close, so the pen lifts as rarely as possible
(a pen lift costs about 0.65 s here).

Hatch lines sit on a global grid (offset k * spacing), so neighbouring shapes with the same spacing and angle line up.
"""
import math

MIN_RUN_MM = 0.2        # runs shorter than this are not drawn
LINK_FACTOR = 1.8       # ends of neighbouring lines closer than this many spacings are joined into a zigzag
DARK_MIN = 0.10         # fills lighter than this are left blank (it would read as paper anyway)
CROSS_AT = 0.75         # fills darker than this get a second, perpendicular pass
MAX_LINES = 20000       # safety limit on hatch lines for one shape


def darkness(luminance, opacity=1.0):
    """0 = paper white, 1 = full ink. luminance 0..1."""
    return max(0.0, min(1.0, (1.0 - luminance) * opacity))


def color_luminance(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def spacing_for(dark, base):
    """Line spacing for a darkness: full ink = base, lighter = proportionally wider. None = too light to draw."""
    if dark < DARK_MIN:
        return None
    return base / dark


def _rotate(p, c, s):
    return (p[0] * c + p[1] * s, -p[0] * s + p[1] * c)


def _intervals(rings_r, v, rule):
    """u-intervals of the shape on the scanline at height v. rings_r are rotated rings."""
    hits = []
    for ring in rings_r:
        n = len(ring)
        for i in range(n):
            a, b = ring[i], ring[(i + 1) % n]
            if a[1] == b[1]:
                continue
            lo, hi = (a, b) if a[1] < b[1] else (b, a)
            if lo[1] <= v < hi[1]:
                u = lo[0] + (v - lo[1]) / (hi[1] - lo[1]) * (hi[0] - lo[0])
                hits.append((u, 1 if b[1] > a[1] else -1))
    hits.sort()
    out = []
    if rule == "nonzero":
        w, start = 0, None
        for u, d in hits:
            before = w
            w += d
            if before == 0 and w != 0:
                start = u
            elif before != 0 and w == 0 and start is not None:
                out.append((start, u))
                start = None
    else:
        for i in range(0, len(hits) - 1, 2):
            out.append((hits[i][0], hits[i + 1][0]))
    return out


def hatch_rings(rings, spacing, angle=45.0, rule="evenodd", min_run=MIN_RUN_MM, zigzag=True):
    """Hatch lines for one filled shape (a list of closed rings, the holes included).

    -> list of polylines, joined into zigzags where neighbouring lines are close. zigzag=False: every line on
    its own and all in the same direction (a pen that drags one way only, at a pen lift per line).
    """
    rings = [[tuple(p) for p in r] for r in rings if len(r) >= 3]
    if not rings or spacing <= 0:
        return []
    th = math.radians(angle)
    c, s = math.cos(th), math.sin(th)
    rot = [[_rotate(p, c, s) for p in r] for r in rings]
    vs = [p[1] for r in rot for p in r]
    vlo, vhi = min(vs), max(vs)
    k0 = math.ceil(vlo / spacing - 0.5)
    k1 = math.floor(vhi / spacing - 0.5)
    if k1 - k0 > MAX_LINES:
        raise ValueError("fill spacing is too fine for this shape")
    chains = []           # finished polylines (rotated frame)
    open_chains = []      # chains extended on the previous line
    link = spacing * LINK_FACTOR if zigzag else -1.0
    for k in range(k0, k1 + 1):
        v = (k + 0.5) * spacing
        runs = [(a, b) for a, b in _intervals(rot, v, rule) if b - a >= min_run]
        forward = k % 2 == 0 or not zigzag
        nxt = []
        taken = set()
        for a, b in (runs if forward else reversed(runs)):
            p0, p1 = ((a, v), (b, v)) if forward else ((b, v), (a, v))
            best, bd = None, link
            for i, ch in enumerate(open_chains):
                if i in taken:
                    continue
                d = math.dist(ch[-1], p0)
                if d <= bd:
                    best, bd = i, d
            if best is None:
                nxt.append([p0, p1])
            else:
                taken.add(best)
                open_chains[best].extend([p0, p1])
                nxt.append(open_chains[best])
        chains.extend(ch for i, ch in enumerate(open_chains) if i not in taken)
        open_chains = nxt
    chains.extend(open_chains)
    cb, sb = math.cos(-th), math.sin(-th)
    return [[(p[0] * cb + p[1] * sb, -p[0] * sb + p[1] * cb) for p in ch] for ch in chains]


def fill_shape(rings, base_spacing, dark, angle=45.0, rule="evenodd", cross=True):
    """Hatch a shape of the given darkness: the darker, the closer the lines; very dark gets a cross pass."""
    sp = spacing_for(dark, base_spacing)
    if sp is None:
        return []
    out = hatch_rings(rings, sp, angle, rule)
    if cross and dark >= CROSS_AT:
        out += hatch_rings(rings, sp, angle + 90.0, rule)
    return out


# ------------------------------------------------------------------ contour fill
CONTOUR_MAX_CELLS = 250_000   # grid size limit for one shape; the cell grows for big shapes


def _edt_1d(f, n):
    """Squared distance transform of one row (Felzenszwalb and Huttenlocher)."""
    INF = 1e20
    d = [0.0] * n
    v = [0] * n
    z = [0.0] * (n + 1)
    k = 0
    v[0] = 0
    z[0], z[1] = -INF, INF
    for q in range(1, n):
        while True:
            p = v[k]
            s = ((f[q] + q * q) - (f[p] + p * p)) / (2 * q - 2 * p)
            if s <= z[k]:
                k -= 1
                if k < 0:
                    k = 0
                    break
                continue
            break
        k += 1
        v[k] = q
        z[k] = s
        z[k + 1] = INF
    k = 0
    for q in range(n):
        while z[k + 1] < q:
            k += 1
        d[q] = (q - v[k]) ** 2 + f[v[k]]
    return d


def contour_rings(rings, spacing, rule="evenodd", first=None, max_cells=CONTOUR_MAX_CELLS):
    """Concentric outlines inside a filled shape: the iso-lines of the distance to its edge at first, first +
    spacing, ... (first defaults to half the spacing). Pure Python on a grid of at most max_cells cells, with
    linear interpolation along the cell edges, so the rings are accurate to a small fraction of a cell.

    -> list of closed polylines (and open ones where a ring touches the grid edge)."""
    rings = [[tuple(p) for p in r] for r in rings if len(r) >= 3]
    if not rings or spacing <= 0:
        return []
    first = spacing / 2 if first is None else first
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return []
    cell = min(spacing / 3, 0.25)
    if (w / cell + 3) * (h / cell + 3) > max_cells:
        cell = math.sqrt(w * h / max_cells) * 1.05
    nx, ny = int(w / cell) + 3, int(h / cell) + 3       # one empty cell of border all round
    ox, oy = x0 - cell, y0 - cell
    INF = 1e20
    inside = [[False] * nx for _ in range(ny)]
    for j in range(ny):
        v = oy + (j + 0.5) * cell
        for a, b in _intervals(rings, v, rule):
            i0 = max(0, math.ceil((a - ox) / cell - 0.5))
            i1 = min(nx - 1, math.floor((b - ox) / cell - 0.5))
            row = inside[j]
            for i in range(i0, i1 + 1):
                row[i] = True
    # exact Euclidean distance from each inside cell to the nearest outside cell, in cells
    cols = [[0.0 if not inside[j][i] else INF for j in range(ny)] for i in range(nx)]
    cols = [_edt_1d(c, ny) for c in cols]
    dist = []
    for j in range(ny):
        row = _edt_1d([cols[i][j] for i in range(nx)], nx)
        dist.append([math.sqrt(d) * cell - cell / 2 if inside[j][i] else -cell / 2 for i, d in enumerate(row)])
    # marching squares on the cell centres, every level at once
    top = max(max(r) for r in dist)
    levels = []
    lv = first
    while lv < top and len(levels) < 2000:
        levels.append(lv)
        lv += spacing
    if not levels:
        return []
    segs = {}           # level index -> list of (edge key a, point a, edge key b, point b)
    for j in range(ny - 1):
        r0, r1 = dist[j], dist[j + 1]
        for i in range(nx - 1):
            a, b, c, d = r0[i], r0[i + 1], r1[i + 1], r1[i]      # corners: tl, tr, br, bl
            lo, hi = min(a, b, c, d), max(a, b, c, d)
            if hi < first:
                continue
            k0 = max(0, math.ceil((lo - first) / spacing))
            k1 = min(len(levels) - 1, math.floor((hi - first) / spacing))
            for k in range(k0, k1 + 1):
                L = levels[k]
                case = (a > L) * 8 + (b > L) * 4 + (c > L) * 2 + (d > L)
                if case in (0, 15):
                    continue
                cx, cy = ox + (i + 0.5) * cell, oy + (j + 0.5) * cell

                def pt(edge):
                    if edge == 0:
                        t = (L - a) / (b - a)
                        return ("h", i, j), (cx + t * cell, cy)
                    if edge == 1:
                        t = (L - b) / (c - b)
                        return ("v", i + 1, j), (cx + cell, cy + t * cell)
                    if edge == 2:
                        t = (L - d) / (c - d)
                        return ("h", i, j + 1), (cx + t * cell, cy + cell)
                    t = (L - a) / (d - a)
                    return ("v", i, j), (cx, cy + t * cell)
                pairs = _MS[case]
                if case in (5, 10) and (a + b + c + d) / 4 > L:
                    pairs = _MS_SADDLE[case]
                for e0, e1 in pairs:
                    segs.setdefault(k, []).append(pt(e0) + pt(e1))
    out = []
    for k in sorted(segs):
        out += _chain(segs[k])
    return out


# edges: 0 top, 1 right, 2 bottom, 3 left; corner bits: tl 8, tr 4, br 2, bl 1
# Saddles (5, 10): the default keeps the two high corners apart; _MS_SADDLE joins them (cell centre high).
_MS = {1: [(3, 2)], 2: [(2, 1)], 3: [(3, 1)], 4: [(0, 1)], 5: [(0, 1), (3, 2)], 6: [(0, 2)], 7: [(3, 0)],
       8: [(0, 3)], 9: [(0, 2)], 10: [(0, 3), (2, 1)], 11: [(0, 1)], 12: [(3, 1)], 13: [(2, 1)], 14: [(3, 2)]}
_MS_SADDLE = {5: [(3, 0), (2, 1)], 10: [(0, 1), (2, 3)]}


def _chain(segs):
    """Join segments that share an edge crossing into polylines."""
    ends = {}
    for n, (ka, pa, kb, pb) in enumerate(segs):
        ends.setdefault(ka, []).append(n)
        ends.setdefault(kb, []).append(n)
    used = [False] * len(segs)
    out = []
    for n in range(len(segs)):
        if used[n]:
            continue
        used[n] = True
        ka, pa, kb, pb = segs[n]
        line = [pa, pb]
        keys = [ka, kb]
        for direction in (1, 0):            # grow from the tail, then from the head
            key = keys[-1] if direction else keys[0]
            while True:
                nxt = next((m for m in ends.get(key, ()) if not used[m]), None)
                if nxt is None:
                    break
                used[nxt] = True
                qa, qpa, qb, qpb = segs[nxt]
                if qa == key:
                    key, p = qb, qpb
                else:
                    key, p = qa, qpa
                if direction:
                    line.append(p)
                    keys.append(key)
                else:
                    line.insert(0, p)
                    keys.insert(0, key)
        if keys[0] == keys[-1] and len(line) > 2:
            line[-1] = line[0]
        out.append(line)
    return out


def tone_runs(sample, width, height, spacing, angle, threshold, step=None, min_run=MIN_RUN_MM):
    """Hatch lines over a width x height area wherever sample(x, y) (darkness 0..1) is above threshold.

    One pass at one angle; call it again with other angles and higher thresholds for cross-hatched shadows.
    -> list of polylines, joined into zigzags where neighbouring runs line up.
    """
    if spacing <= 0:
        return []
    th = math.radians(angle)
    c, s = math.cos(th), math.sin(th)
    corners = [_rotate(p, c, s) for p in ((0, 0), (width, 0), (width, height), (0, height))]
    ulo, uhi = min(p[0] for p in corners), max(p[0] for p in corners)
    vlo, vhi = min(p[1] for p in corners), max(p[1] for p in corners)
    k0, k1 = math.ceil(vlo / spacing - 0.5), math.floor(vhi / spacing - 0.5)
    if k1 - k0 > MAX_LINES:
        raise ValueError("fill spacing is too fine for this area")
    step = step or max(spacing / 3, 0.15)
    cb, sb = math.cos(-th), math.sin(-th)
    link = spacing * LINK_FACTOR
    chains, open_chains = [], []
    for k in range(k0, k1 + 1):
        v = (k + 0.5) * spacing
        runs, start = [], None
        n = int((uhi - ulo) / step) + 1
        last = None
        for i in range(n + 1):
            u = ulo + i * step
            x, y = u * cb + v * sb, -u * sb + v * cb
            on = 0 <= x <= width and 0 <= y <= height and sample(x, y) > threshold
            if on and start is None:
                start = u
            if on:
                last = u
            if not on and start is not None:
                if last - start >= min_run:
                    runs.append((start, last))
                start = None
        if start is not None and last is not None and last - start >= min_run:
            runs.append((start, last))
        forward = k % 2 == 0
        nxt, taken = [], set()
        for a, b in (runs if forward else reversed(runs)):
            p0, p1 = ((a, v), (b, v)) if forward else ((b, v), (a, v))
            best, bd = None, link
            for j, ch in enumerate(open_chains):
                if j not in taken:
                    d = math.dist(ch[-1], p0)
                    if d <= bd:
                        best, bd = j, d
            if best is None:
                nxt.append([p0, p1])
            else:
                taken.add(best)
                open_chains[best].extend([p0, p1])
                nxt.append(open_chains[best])
        chains.extend(ch for j, ch in enumerate(open_chains) if j not in taken)
        open_chains = nxt
    chains.extend(open_chains)
    return [[(p[0] * cb + p[1] * sb, -p[0] * sb + p[1] * cb) for p in ch] for ch in chains]
