#!/usr/bin/env python3
"""Raster -> pen-path SVG converter (Pillow only, no numpy/scipy).

Three drawing styles, each producing SVG that ``tools/plot_svg.py`` can plot
directly (plain M/L paths, no arcs, no transforms):

  contour    marching-squares outlines at one or more darkness thresholds
  halftone   a grid of dots whose size follows local darkness
  one-line   a single continuous serpentine line that packs tightly in the
             dark regions (the classic "one-line drawing" look)
  hatch      tone by hatching: parallel lines where the picture is dark, a second and third
             pass at other angles in the shadows (fill, as shading)

The dark parts of the photo can be split across several ink layers (pens).
Each ink becomes an Inkscape-style layer with a stroke colour; plot_svg.py
reads those colours and pauses for a pen swap between layers.

Usage:
    python3 tools/image_to_svg.py photo.jpg --out photo.svg
    python3 tools/image_to_svg.py photo.jpg --out photo.svg --style one-line \
        --threshold 150 --density 80 --inks 2
"""
import argparse
import math
import pathlib
import sys
from collections import defaultdict

import fill

try:
    from PIL import Image, ImageFilter, ImageOps
except ImportError:
    Image = None

MAX_PX = 500          # cap the longest side after resize
MAX_POINTS = 30000    # safety cap on emitted path vertices

# Ordered dark -> light. Used both for tone->ink mapping and for SVG labels.
INKS = {
    1: [("Black", "#111111")],
    2: [("Black", "#111111"), ("Blue", "#2463a6")],
    3: [("Black", "#111111"), ("Blue", "#2463a6"), ("Red", "#c0392b")],
    4: [("Key", "#111111"), ("Magenta", "#c2187e"), ("Cyan", "#00a0b0"), ("Yellow", "#d9a400")],
}
STYLES = ("contour", "halftone", "one-line", "lineart", "hatch", "stipple", "sketch")


def read_pixels(path, width_px, blur, levels=0):
    """-> (w, h, pix, im). With levels>=2 the grayscale is posterised first."""
    if Image is None:
        raise RuntimeError("Pillow is required for raster conversion (pip install Pillow)")
    with Image.open(path) as im:
        im = ImageOps.grayscale(im)
        if blur > 0:
            im = im.filter(ImageFilter.GaussianBlur(blur))
        w, h = im.size
        tw = max(1, int(width_px))
        th = max(1, round(h * tw / w))
        if max(tw, th) > MAX_PX:
            f = MAX_PX / max(tw, th)
            tw, th = max(1, round(tw * f)), max(1, round(th * f))
        resample = getattr(Image, "Resampling", Image).LANCZOS
        im = im.resize((tw, th), resample)
        if levels >= 2:
            step = 255.0 / (levels - 1)
            im = im.point(lambda v: int(round(v / step) * step))
        return tw, th, im.load(), im


def write_mask(im, threshold, dst, levels=0):
    """Save the preprocessing preview: posterised tones, or a binary dark-shape mask."""
    mask = im if levels >= 2 else im.point(lambda v: 0 if v < threshold else 255)
    mask.save(dst, format="PNG")
    return pathlib.Path(dst).name


def marching_squares(w, h, pix, threshold):
    """Undirected contour segments [( (x,y), (x,y) ), ...] in pixel space."""
    def interp(a, b):
        va, vb = a[2], b[2]
        f = 0.5 if va == vb else (threshold - va) / (vb - va)
        f = min(1.0, max(0.0, f))
        return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))

    table = {
        1: [(3, 0)], 2: [(0, 1)], 3: [(3, 1)], 4: [(1, 2)],
        5: [(3, 0), (1, 2)], 6: [(0, 2)], 7: [(3, 2)], 8: [(2, 3)],
        9: [(0, 2)], 10: [(0, 1), (2, 3)], 11: [(1, 2)], 12: [(3, 1)],
        13: [(0, 1)], 14: [(3, 0)],
    }
    segs = []
    for y in range(h - 1):
        for x in range(w - 1):
            tl = (x, y, pix[x, y])
            tr = (x + 1, y, pix[x + 1, y])
            br = (x + 1, y + 1, pix[x + 1, y + 1])
            bl = (x, y + 1, pix[x, y + 1])
            idx = (1 if tl[2] < threshold else 0) | (2 if tr[2] < threshold else 0) | \
                  (4 if br[2] < threshold else 0) | (8 if bl[2] < threshold else 0)
            if idx == 0 or idx == 15:
                continue
            edges = (interp(tl, tr), interp(tr, br), interp(br, bl), interp(bl, tl))
            for ia, ib in table[idx]:
                segs.append((edges[ia], edges[ib]))
    return segs


def join_segments(segs, tol=0.8):
    """Chain undirected segments into polylines, one shared-end walk per chain."""
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))

    ends = defaultdict(list)
    for i, (a, b) in enumerate(segs):
        ends[key(a)].append(i)
        ends[key(b)].append(i)
    used = [False] * len(segs)
    polys = []

    for i, (a, b) in enumerate(segs):
        if used[i]:
            continue
        used[i] = True
        poly = [a, b]
        tail = b
        while len(poly) < MAX_POINTS:
            nxt = None
            for j in ends[key(tail)]:
                if not used[j]:
                    s, e = segs[j]
                    other = e if key(s) == key(tail) else s
                    if math.dist(other, tail) <= tol * 2:
                        nxt = (j, other)
                        break
            if nxt is None:
                break
            j, other = nxt
            used[j] = True
            poly.append(other)
            tail = other
        head = a
        while len(poly) < MAX_POINTS:
            nxt = None
            for j in ends[key(head)]:
                if not used[j]:
                    s, e = segs[j]
                    other = e if key(s) == key(head) else s
                    if math.dist(other, head) <= tol * 2:
                        nxt = (j, other)
                        break
            if nxt is None:
                break
            j, other = nxt
            used[j] = True
            poly.insert(0, other)
            head = other
        polys.append(poly)
    return polys


def rdp(pts, eps):
    """Ramer-Douglas-Peucker polyline simplification."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = math.hypot(px - ax, py - ay) if L == 0 else abs(dy * (px - ax) - dx * (py - ay)) / L
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def skeletonize(w, h, mask):
    """Guo-Hall thinning of a binary mask. -> set of (x, y) skeleton pixels, 8-connected, one pixel wide.
    (Zhang-Suen, used before, erases diagonal lines two pixels thick completely.)"""
    rows = [bytearray(1 if mask[y][x] else 0 for x in range(w)) for y in range(h)]
    live = {(x, y) for y in range(1, h - 1) for x in range(1, w - 1) if rows[y][x]}
    changed = True
    while changed:
        changed = False
        for sub in (0, 1):
            drop = []
            for x, y in live:
                r0, r1, r2 = rows[y - 1], rows[y], rows[y + 1]
                p2, p3, p4, p5 = r0[x], r0[x + 1], r1[x + 1], r2[x + 1]
                p6, p7, p8, p9 = r2[x], r2[x - 1], r1[x - 1], r0[x - 1]
                c = ((not p2) and (p3 or p4)) + ((not p4) and (p5 or p6)) +                     ((not p6) and (p7 or p8)) + ((not p8) and (p9 or p2))
                if c != 1:
                    continue
                n1 = (p9 or p2) + (p3 or p4) + (p5 or p6) + (p7 or p8)
                n2 = (p2 or p3) + (p4 or p5) + (p6 or p7) + (p8 or p9)
                n = min(n1, n2)
                if n < 2 or n > 3:
                    continue
                m = ((p6 or p7 or not p9) and p8) if sub == 0 else ((p2 or p3 or not p5) and p4)
                if not m:
                    drop.append((x, y))
            if drop:
                changed = True
                for x, y in drop:
                    rows[y][x] = 0
                live.difference_update(drop)
    return {(x, y) for y in range(h) for x in range(w) if rows[y][x]}


def trace_skeleton(points):
    """Chain skeleton pixels into polylines (endpoints and junctions split paths)."""
    adj = {p: set() for p in points}
    for x, y in points:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    q = (x + dx, y + dy)
                    # a diagonal step is left out where a pixel beside it already links the two: thinning leaves
                    # such staircase corners, and keeping both makes every corner look like a three-way junction
                    if q in adj and not (dx and dy and ((x + dx, y) in adj or (x, y + dy) in adj)):
                        adj[(x, y)].add(q)

    def take(a, b):
        adj[a].discard(b)
        adj[b].discard(a)

    paths = []

    def walk(start):
        while adj[start]:
            nxt = next(iter(adj[start]))
            take(start, nxt)
            path = [start, nxt]
            prev, cur = start, nxt
            while adj[cur]:
                dx, dy = cur[0] - prev[0], cur[1] - prev[1]
                cands = list(adj[cur])
                cands.sort(key=lambda q: -((q[0] - cur[0]) * dx + (q[1] - cur[1]) * dy))
                nxt = cands[0]
                take(cur, nxt)
                path.append(nxt)
                prev, cur = cur, nxt
                if len(adj[cur]) != 1:
                    break
            paths.append(path)

    ends = [p for p, ns in adj.items() if len(ns) == 1]
    for p in ends:
        walk(p)
    for p in list(adj):
        if adj[p]:
            walk(p)
    return paths


def smooth_pixels(path, passes=2):
    """Average each point with its neighbours: a traced pixel chain steps like a staircase, this takes the steps
    out before simplifying. The ends stay put; a closed chain is smoothed all the way round."""
    pts = [(float(x), float(y)) for x, y in path]
    closed = len(pts) > 3 and pts[0] == pts[-1]
    for _ in range(passes):
        if len(pts) < 3:
            break
        if closed:
            ring = pts[:-1]
            n = len(ring)
            ring = [((ring[i - 1][0] + 2 * ring[i][0] + ring[(i + 1) % n][0]) / 4,
                     (ring[i - 1][1] + 2 * ring[i][1] + ring[(i + 1) % n][1]) / 4) for i in range(n)]
            pts = ring + [ring[0]]
        else:
            pts = [pts[0]] + [((pts[i - 1][0] + 2 * pts[i][0] + pts[i + 1][0]) / 4,
                               (pts[i - 1][1] + 2 * pts[i][1] + pts[i + 1][1]) / 4) for i in range(1, len(pts) - 1)] + [pts[-1]]
    return pts


def join_paths(paths, tol=1.5):
    """Merge fragments that share an endpoint, to avoid unnecessary pen lifts."""
    paths = [list(p) for p in paths]
    while True:
        ends = {}
        for i, p in enumerate(paths):
            ends.setdefault((round(p[0][0] / tol), round(p[0][1] / tol)), []).append((i, 0))
            ends.setdefault((round(p[-1][0] / tol), round(p[-1][1] / tol)), []).append((i, -1))
        joined, used = None, set()
        for pairs in ends.values():
            if len(pairs) != 2:
                continue   # junction: never guess which branch continues
            (i, ea), (j, eb) = pairs
            if i == j:
                continue
            p, q = list(paths[i]), list(paths[j])
            if ea == 0:
                p.reverse()
            if eb == -1:
                q.reverse()
            joined = (i, p + q[1:], j)
            used.add(j)
            break
        if not joined:
            break
        i, merged, j = joined
        paths[i] = merged
        paths = [p for k, p in enumerate(paths) if k not in used]
    return paths


def lineart_bands(w, h, pix, threshold, inks, simplify_px, min_len_px):
    """One continuous centreline stroke per dark line in a technical drawing."""
    mask = [[1 if pix[x, y] < threshold else 0 for x in range(w)] for y in range(h)]
    skeleton = skeletonize(w, h, mask)
    bands = [[] for _ in range(inks)]
    for path in join_paths(trace_skeleton(skeleton)):
        pts = rdp(smooth_pixels(path), simplify_px)
        if len(pts) < 2:
            continue
        length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        if length < min_len_px:
            continue
        bands[0].append(pts)
    return bands


def darkness(pix, w, h, threshold):
    """Hard 0/1 mask of the big dark shapes. Blur upstream softens the edges."""
    return [[1.0 if pix[x, y] < threshold else 0.0 for x in range(w)] for y in range(h)]


def band_of(d, n):
    """Map darkness 0..1 (dark=1) to an ink index, darkest ink first."""
    return max(0, min(n - 1, int((1.0 - d) * n)))


# ------------------------------------------------------------------- styles
def contour_bands(w, h, pix, threshold, inks, simplify_px, min_len_px, tone_levels=0):
    """Nested contours: one per tone boundary (or per ink when continuous)."""
    if tone_levels >= 2:
        bounds = [255.0 * (i + 0.5) / tone_levels for i in range(tone_levels - 1)]
        bands = [[] for _ in range(inks)]
        for i, level in enumerate(bounds):
            segs = marching_squares(w, h, pix, level)
            band = min(inks - 1, i * inks // max(1, tone_levels - 1))
            for poly in join_segments(segs):
                poly = rdp(poly, simplify_px)
                if len(poly) < 2:
                    continue
                if sum(math.dist(a, b) for a, b in zip(poly, poly[1:])) < min_len_px:
                    continue
                bands[band].append(poly)
        return bands
    levels = [threshold + (255 - threshold) * i / inks for i in range(inks)]
    bands = [[] for _ in range(inks)]
    for band, level in enumerate(levels):
        segs = marching_squares(w, h, pix, level)
        for poly in join_segments(segs):
            poly = rdp(poly, simplify_px)
            if len(poly) < 2:
                continue
            if sum(math.dist(a, b) for a, b in zip(poly, poly[1:])) < min_len_px:
                continue
            bands[band].append(poly)
    return bands


def halftone_bands(w, h, pix, threshold, inks, density):
    """Grid of round dots; radius follows local darkness, ink follows tone."""
    step = max(3, round(20 - density * 0.16))
    dark = darkness(pix, w, h, threshold)
    bands = [[] for _ in range(inks)]
    sides = 10
    for cy in range(step // 2, h, step):
        for cx in range(step // 2, w, step):
            y0, y1 = max(0, cy - step // 2), min(h, cy + step // 2 + 1)
            x0, x1 = max(0, cx - step // 2), min(w, cx + step // 2 + 1)
            area = (y1 - y0) * (x1 - x0)
            if not area:
                continue
            d = sum(dark[y][x] for y in range(y0, y1) for x in range(x0, x1)) / area
            if d < 0.08:
                continue
            r = (step * 0.48) * math.sqrt(d)
            pts = [(cx + r * math.cos(2 * math.pi * k / sides),
                    cy + r * math.sin(2 * math.pi * k / sides)) for k in range(sides + 1)]
            bands[band_of(d, inks)].append(pts)
    return bands


HATCH_ANGLES = (45.0, 135.0, 0.0, 90.0)
HATCH_TONES = (0.12, 0.35, 0.58, 0.8)      # a pass draws where the tone (0 paper .. 1 black) is above this


def hatch_bands(w, h, pix, threshold, inks, density, mm_per_px, levels=0, angle=0.0):
    """Shading by hatching: one pass per tone step, each at its own angle, darker steps add more lines."""
    passes = levels - 1 if 2 <= levels <= 5 else len(HATCH_ANGLES)
    spacing_mm = 2.4 - 0.019 * density          # density 1..100 -> 2.4 .. 0.5 mm between lines
    spacing = max(2.0, spacing_mm / mm_per_px)
    cut = max(1, threshold)

    def tone(x, y):
        g = pix[min(w - 1, max(0, int(x))), min(h - 1, max(0, int(y)))]
        return max(0.0, (cut - g) / cut)

    bands = [[] for _ in range(inks)]
    for k in range(passes):
        thr = HATCH_TONES[k] if passes == len(HATCH_ANGLES) else (k + 0.5) / passes * 0.9
        lines = fill.tone_runs(tone, w, h, spacing, (HATCH_ANGLES[k] + angle) % 180.0, thr, step=1.0,
                               min_run=max(1.0, 0.6 / mm_per_px))
        mid = thr + (1.0 - thr) / 2
        bands[band_of(mid, inks)].extend(lines)
    return bands


BAYER4 = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


def stipple_bands(w, h, pix, threshold, inks, density, pattern="diffusion"):
    """Dots by dithering the tone on a cell grid: error diffusion (Floyd-Steinberg, organic) or ordered (4x4 Bayer,
    regular). Each dot is a tick a quarter cell long: the pen goes down and up once."""
    cell = max(2, round(10 - density * 0.08))          # density 1..100 -> 10 .. 2 px per cell
    gw, gh = max(1, w // cell), max(1, h // cell)
    cut = max(1, threshold)
    tone = [[0.0] * gw for _ in range(gh)]
    for gy in range(gh):
        for gx in range(gw):
            s = 0.0
            for y in range(gy * cell, gy * cell + cell):
                for x in range(gx * cell, gx * cell + cell):
                    s += max(0.0, min(1.0, (cut - pix[min(w - 1, x), min(h - 1, y)]) / cut))
            tone[gy][gx] = s / (cell * cell)
    bands = [[] for _ in range(inks)]
    tick = cell * 0.25
    for gy in range(gh):
        for gx in range(gw):
            v = tone[gy][gx]
            if pattern == "ordered":
                on = v > (BAYER4[gy % 4][gx % 4] + 0.5) / 16
            else:
                on = v >= 0.5
                err = v - (1.0 if on else 0.0)
                for dx, dy, k in ((1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)):
                    if 0 <= gx + dx < gw and gy + dy < gh:
                        tone[gy + dy][gx + dx] += err * k / 16
            if on:
                cx, cy = (gx + 0.5) * cell, (gy + 0.5) * cell
                bands[band_of(max(0.0, min(1.0, tone[gy][gx] if pattern == "ordered" else v)), inks)].append(
                    [(cx - tick / 2, cy), (cx + tick / 2, cy)])
    return bands


def sketch_bands(w, h, im, inks, simplify_px, min_len_px, sensitivity=18):
    """Centre lines of a photographed drawing: a pixel is ink when it is darker than its neighbourhood by more than
    sensitivity grey levels, so uneven light and paper shadows drop out. Then thinned like line art."""
    radius = max(4, min(w, h) // 30)
    local = im.filter(ImageFilter.BoxBlur(radius)).load()
    pix = im.load()
    mask = [[1 if local[x, y] - pix[x, y] > sensitivity else 0 for x in range(w)] for y in range(h)]
    skeleton = skeletonize(w, h, mask)
    bands = [[] for _ in range(inks)]
    for path in join_paths(trace_skeleton(skeleton)):
        pts = rdp(smooth_pixels(path), simplify_px)
        if len(pts) < 2 or sum(math.dist(a, b) for a, b in zip(pts, pts[1:])) < min_len_px:
            continue
        bands[0].append(pts)
    return bands


def one_line_bands(w, h, pix, threshold, inks, density):
    """One continuous serpentine line, packed by cumulative darkness per column.

    Each column gets `density` y-positions chosen so that equal amounts of ink
    lie between successive passes; in dark regions the passes bunch together
    and in light regions they spread apart. Passes are joined end-to-end into
    a single polyline.
    """
    passes = max(12, min(200, round(12 + density * 1.9)))
    dark = darkness(pix, w, h, threshold)
    cum = [[0.0] * (h + 1) for _ in range(w)]
    for x in range(w):
        s = 0.0
        for y in range(h):
            s += dark[y][x]
            cum[x][y + 1] = s

    # If the drawing is dense, sample fewer columns so we stay under MAX_POINTS.
    step = max(1, math.ceil(w * passes / MAX_POINTS))
    xs = list(range(0, w, step))
    if xs[-1] != w - 1:
        xs.append(w - 1)

    rows = []
    for p in range(passes):
        t = (p + 0.5) / passes
        row = []
        for x in xs:
            total = cum[x][h]
            if total <= 1e-9:
                y = t * (h - 1)
            else:
                target = t * total
                lo, hi = 0, h
                while lo < hi:
                    mid = (lo + hi) // 2
                    if cum[x][mid + 1] < target:
                        lo = mid + 1
                    else:
                        hi = mid
                y = float(lo)
                if lo > 0 and cum[x][lo] > 0:
                    frac = (target - cum[x][lo]) / max(1e-9, cum[x][lo + 1] - cum[x][lo])
                    y = lo - 1 + min(1.0, max(0.0, frac))
            row.append((float(x), y + 0.35 * math.sin(x * 0.21 + p * 1.3)))
        rows.append(row)

    poly = []
    for p, row in enumerate(rows):
        poly.extend(row if p % 2 == 0 else list(reversed(row)))
    poly = rdp(poly, 0.3)
    # A single continuous line has no per-segment tone to band by: it uses the
    # darkest pen only, leaving the other ink layers empty.
    bands = [[poly] if len(poly) >= 2 else []]
    bands += [[] for _ in range(inks - 1)]
    return bands


# -------------------------------------------------------------------- output
def svg_text(width_mm, height_mm, bands, inks, mm_per_px):
    """Wrap per-ink polylines in labelled SVG layers. -> (svg, layer stats, counts)."""
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
             f'width="{width_mm:.3f}mm" height="{height_mm:.3f}mm" '
             f'viewBox="0 0 {width_mm:.3f} {height_mm:.3f}">']
    layer_stats = []
    total_paths = total_points = 0
    for band, polys in enumerate(bands):
        label, color = INKS[inks][band]
        paths = []
        for poly in polys:
            total_points += len(poly)
            d = "M " + " L ".join(f"{x * mm_per_px:.3f},{y * mm_per_px:.3f}" for x, y in poly)
            paths.append(f'<path d="{d}"/>')
        total_paths += len(paths)
        parts.append(f'<g inkscape:groupmode="layer" inkscape:label="{esc(label)}" '
                     f'style="stroke:{color};fill:none">')
        parts.extend(paths)
        parts.append('</g>')
        if paths:
            layer_stats.append({"label": label, "color": color, "paths": len(paths),
                                "points": sum(len(p) for p in polys)})
    parts.append('</svg>\n')
    return "\n".join(parts), layer_stats, total_paths, total_points


def convert(src, dst, width_mm=120.0, threshold=140, blur=1.0, width_px=300,
            simplify_px=0.6, min_len_px=2.0, style="contour", density=40,
            inks=1, mask=None, levels=0, max_width_mm=None, max_height_mm=None,
            single_pen=False, pattern="diffusion", angle=0.0, sensitivity=18):
    """Convert raster file -> layered SVG file. Returns a stats dict.

    levels>=2 posterises the grayscale into that many discrete tones before
    routing; max_width/height fit the result inside the reachable canvas.
    """
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}; choose one of {', '.join(STYLES)}")
    inks = int(inks)
    if inks not in INKS:
        raise ValueError("inks must be 1..4")
    levels = int(levels)
    if levels not in (0, 1) and not 2 <= levels <= 6:
        raise ValueError("levels must be 0 (continuous) or 2..6")
    if style in ("lineart", "sketch"):
        width_px = min(width_px, 300)   # thinning is O(pixels) in pure Python
    w, h, pix, im = read_pixels(pathlib.Path(src), width_px, blur, levels)
    if max_width_mm and width_mm > max_width_mm:
        width_mm = float(max_width_mm)
    mm_per_px = width_mm / w
    height_mm = h * mm_per_px
    if max_height_mm and height_mm > max_height_mm:
        height_mm = float(max_height_mm)
        mm_per_px = height_mm / h
        width_mm = w * mm_per_px

    if style == "contour":
        bands = contour_bands(w, h, pix, threshold, inks, simplify_px, min_len_px, levels)
    elif style == "halftone":
        bands = halftone_bands(w, h, pix, threshold, inks, density)
    elif style == "lineart":
        bands = lineart_bands(w, h, pix, threshold, inks, simplify_px, min_len_px)
    elif style == "hatch":
        bands = hatch_bands(w, h, pix, threshold, inks, density, mm_per_px, levels, angle)
    elif style == "stipple":
        bands = stipple_bands(w, h, pix, threshold, inks, density, pattern)
    elif style == "sketch":
        bands = sketch_bands(w, h, im, inks, simplify_px, min_len_px, sensitivity)
    else:
        bands = one_line_bands(w, h, pix, threshold, inks, density)

    # Global point budget: drop the lightest strokes first if necessary.
    budget = MAX_POINTS
    trimmed = []
    for polys in bands:
        keep = []
        for poly in polys:
            if budget - len(poly) < 0:
                break
            keep.append(poly)
            budget -= len(poly)
        trimmed.append(keep)
    bands = trimmed
    if not any(bands):
        raise ValueError(f"nothing to draw at this threshold/density (style {style}); tune the controls")

    if single_pen and inks > 1:
        bands = [[p for band in bands for p in band]]
        inks = 1   # keep all the tone detail, but plot it with one pen and no swaps
    svg, layers, n_paths, n_points = svg_text(width_mm, height_mm, bands, inks, mm_per_px)
    pathlib.Path(dst).write_text(svg, encoding="utf-8")
    stats = {"source_px": [w, h], "width_mm": round(width_mm, 2),
             "height_mm": round(height_mm, 2), "segments": sum(len(b) for b in bands),
             "paths": n_paths, "points": n_points, "style": style, "inks": inks,
             "levels": levels, "layers": layers}
    if mask:
        stats["mask"] = write_mask(im, threshold, mask, levels)
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--out", required=True, help="output SVG path")
    ap.add_argument("--style", choices=STYLES, default="contour", help="drawing style")
    ap.add_argument("--width-mm", type=float, default=120.0, help="drawing width on paper, mm")
    ap.add_argument("--threshold", type=int, default=140, help="dark-shape cutoff 0..255 (default 140)")
    ap.add_argument("--density", type=int, default=40, help="line passes / dot fineness 1..100")
    ap.add_argument("--inks", type=int, default=1, choices=[1, 2, 3, 4], help="number of pen layers")
    ap.add_argument("--single-pen", action="store_true", help="keep all tone bands but output one ink (no pen swaps)")
    ap.add_argument("--levels", type=int, default=0, choices=[0, 2, 3, 4, 5, 6],
                    help="posterise into N discrete tones before routing (0 = continuous)")
    ap.add_argument("--max-width-mm", type=float, help="cap the drawing width (reachable canvas)")
    ap.add_argument("--max-height-mm", type=float, help="cap the drawing height (reachable canvas)")
    ap.add_argument("--blur", type=float, default=1.0, help="Gaussian blur radius before thresholding")
    ap.add_argument("--mask", help="also write a black/white dark-shape mask PNG here")
    ap.add_argument("--px", type=int, default=300, help="working resolution in pixels (speed/quality)")
    ap.add_argument("--simplify", type=float, default=0.6, help="RDP tolerance in pixels")
    ap.add_argument("--min-len", type=float, default=2.0, help="drop contours shorter than this, pixels")
    a = ap.parse_args(argv)
    if not 0 <= a.threshold <= 255:
        sys.exit("threshold must be 0..255")
    if not 1 <= a.density <= 100:
        sys.exit("density must be 1..100")
    if a.width_mm <= 0 or a.px <= 0:
        sys.exit("width-mm and px must be positive")
    try:
        stats = convert(a.image, a.out, a.width_mm, a.threshold, a.blur, a.px, a.simplify, a.min_len,
                        a.style, a.density, a.inks, a.mask, a.levels, a.max_width_mm, a.max_height_mm,
                        a.single_pen)
    except Exception as ex:  # noqa: BLE001
        sys.exit(f"conversion failed: {ex}")
    print(f"Wrote {a.out}: {stats['style']}, {stats['inks']} ink(s), {stats['paths']} paths, "
          f"{stats['points']} points, {stats['width_mm']} x {stats['height_mm']} mm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
