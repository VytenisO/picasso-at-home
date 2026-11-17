#!/usr/bin/env python3
"""Raster -> pen-path SVG converter (Pillow only, no numpy/scipy).

Three drawing styles, each producing SVG that ``tools/plot_svg.py`` can plot
directly (plain M/L paths, no arcs, no transforms):

  contour    marching-squares outlines at one or more darkness thresholds
  halftone   a grid of dots whose size follows local darkness
  one-line   a single continuous serpentine line that packs tightly in the
             dark regions (the classic "one-line drawing" look)

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
STYLES = ("contour", "halftone", "one-line")


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
            inks=1, mask=None, levels=0, max_width_mm=None, max_height_mm=None):
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
                        a.style, a.density, a.inks, a.mask, a.levels, a.max_width_mm, a.max_height_mm)
    except Exception as ex:  # noqa: BLE001
        sys.exit(f"conversion failed: {ex}")
    print(f"Wrote {a.out}: {stats['style']}, {stats['inks']} ink(s), {stats['paths']} paths, "
          f"{stats['points']} points, {stats['width_mm']} x {stats['height_mm']} mm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
