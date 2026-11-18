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


def hatch_rings(rings, spacing, angle=45.0, rule="evenodd", min_run=MIN_RUN_MM):
    """Hatch lines for one filled shape (a list of closed rings, the holes included).

    -> list of polylines, joined into zigzags where neighbouring lines are close.
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
    link = spacing * LINK_FACTOR
    for k in range(k0, k1 + 1):
        v = (k + 0.5) * spacing
        runs = [(a, b) for a, b in _intervals(rot, v, rule) if b - a >= min_run]
        forward = k % 2 == 0
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
