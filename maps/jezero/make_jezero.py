"""Jezero crater map designs for the pen plotter.

DEM (USGS CTX, 20 m/px) + Perseverance traverse (NASA/JPL MMGIS) in, plot-ready
SVGs out: one combined preview per design plus one SVG per pen (black, red) on the
same A4 page so the two passes register. Everything is polylines in mm, no text
elements or transforms, so tools/plot_svg.py can take the per-pen files directly.

    python make_jezero.py --dem ../../M20_JezeroCrater_CTXDEM_20m.tif --data data --out out

Fetch the traverse first (see fetch_data.sh).
"""
import argparse
import json
import math
import os

import numpy as np
import tifffile
from HersheyFonts import HersheyFonts
from scipy.ndimage import gaussian_filter, map_coordinates, maximum_filter
from scipy.spatial import cKDTree
from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep
from skimage.measure import find_contours

# ---------------------------------------------------------------- projection
# From the DEM's GeoTIFF keys: equirectangular, standard parallel 18.4663 N,
# central meridian 0, Mars 2000 sphere. Tie point is the upper-left pixel corner.
R_MARS = 3396190.0
STD_PAR = 18.4663
K_X = R_MARS * math.cos(math.radians(STD_PAR))
DEM_X0, DEM_Y0, DEM_PX = 4329000.0, 1143420.0, 20.0
PAGE_W, PAGE_H = 297.0, 210.0


def ll_to_xy(lon, lat):
    return K_X * math.radians(lon), R_MARS * math.radians(lat)


def xy_to_ll(x, y):
    return math.degrees(x / K_X), math.degrees(y / R_MARS)


class MapFrame:
    """Projected metres <-> page mm for one rectangular map frame."""

    def __init__(self, frame, scale, center_ll):
        self.fx0, self.fy0, self.fx1, self.fy1 = frame
        self.scale = scale
        self.cx, self.cy = ll_to_xy(*center_ll)
        self.k = 1000.0 / scale  # mm per metre

    @property
    def box(self):
        return box(self.fx0, self.fy0, self.fx1, self.fy1)

    def to_mm(self, x, y):
        mx = (self.fx0 + self.fx1) / 2 + (np.asarray(x) - self.cx) * self.k
        my = (self.fy0 + self.fy1) / 2 - (np.asarray(y) - self.cy) * self.k
        return mx, my

    def ll_mm(self, lon, lat):
        return self.to_mm(*ll_to_xy(lon, lat))

    def extent_xy(self, pad_mm=0):
        x0 = self.cx - ((self.fx1 - self.fx0) / 2 + pad_mm) / self.k
        x1 = self.cx + ((self.fx1 - self.fx0) / 2 + pad_mm) / self.k
        y0 = self.cy - ((self.fy1 - self.fy0) / 2 + pad_mm) / self.k
        y1 = self.cy + ((self.fy1 - self.fy0) / 2 + pad_mm) / self.k
        return x0, y0, x1, y1


# ---------------------------------------------------------------- terrain
class Terrain:
    """DEM window, block-averaged to `cell` metres and Gaussian smoothed."""

    def __init__(self, dem, mf, cell, sigma_m, pad_mm=8):
        x0, y0, x1, y1 = mf.extent_xy(pad_mm)
        c0 = max(0, int((x0 - DEM_X0) / DEM_PX))
        c1 = min(dem.shape[1], int(math.ceil((x1 - DEM_X0) / DEM_PX)))
        r0 = max(0, int((DEM_Y0 - y1) / DEM_PX))
        r1 = min(dem.shape[0], int(math.ceil((DEM_Y0 - y0) / DEM_PX)))
        f = max(1, int(round(cell / DEM_PX)))
        r1 = r0 + (r1 - r0) // f * f
        c1 = c0 + (c1 - c0) // f * f
        w = dem[r0:r1, c0:c1]
        w = w.reshape((r1 - r0) // f, f, (c1 - c0) // f, f)
        z = np.nanmean(w, axis=(1, 3))
        nan = np.isnan(z)
        z[nan] = np.nanmedian(z)
        self.z = gaussian_filter(z, sigma_m / (f * DEM_PX))
        self.z[nan] = np.nan
        self.cell = f * DEM_PX
        self.gx0 = DEM_X0 + c0 * DEM_PX
        self.gy0 = DEM_Y0 - r0 * DEM_PX
        zz = np.where(np.isnan(self.z), np.nanmedian(self.z), self.z)
        gr, gc = np.gradient(zz, self.cell)
        self.g_r, self.g_c = gr, gc  # dz/dm along rows (south) and cols (east)
        self.slope = np.hypot(gr, gc)
        self.mf = mf

    def rc_to_mm(self, r, c):
        x = self.gx0 + (np.asarray(c) + 0.5) * self.cell
        y = self.gy0 - (np.asarray(r) + 0.5) * self.cell
        return self.mf.to_mm(x, y)

    def contours(self, level):
        out = []
        for rc in find_contours(self.z, level):
            mx, my = self.rc_to_mm(rc[:, 0], rc[:, 1])
            out.append(np.column_stack([mx, my]))
        return out

    def mm_to_rc(self, mx, my):
        mf = self.mf
        x = mf.cx + (np.asarray(mx) - (mf.fx0 + mf.fx1) / 2) / mf.k
        y = mf.cy - (np.asarray(my) - (mf.fy0 + mf.fy1) / 2) / mf.k
        return (self.gy0 - y) / self.cell - 0.5, (x - self.gx0) / self.cell - 0.5

    def sample_mm(self, arr, pts):
        r, c = self.mm_to_rc(pts[:, 0], pts[:, 1])
        z = np.where(np.isnan(arr), np.nanmedian(arr), arr)
        return map_coordinates(z, [r, c], order=1, mode="nearest")

    def sample(self, arr, r, c):
        return map_coordinates(arr, [np.atleast_1d(r), np.atleast_1d(c)], order=1, mode="nearest")


# ---------------------------------------------------------------- lettering
FONTS = {}


def font(name):
    if name not in FONTS:
        f = HersheyFonts()
        f.load_default_font(name)
        FONTS[name] = f
    return FONTS[name]


# Characters the Hershey sets lack: base glyph + extra strokes in font units
# (y down, baseline 9, x-height -5, cap -12).
ACUTE = {"é": "e", "í": "i", "á": "a", "ó": "o", "É": "E", "Í": "I", "Á": "A", "Ó": "O"}
UMLAUT = {"ä": "a", "ö": "o", "ü": "u", "Ä": "A", "Ö": "O", "Ü": "U"}


def _glyph_strokes(fname, ch):
    f = font(fname)
    if ch in ACUTE:
        base = ACUTE[ch]
        g = next(iter(f.glyphs_for_text(base)))
        strokes = [list(s) for s in g.strokes]
        if base == "i":  # drop the dot
            strokes = [s for s in strokes if max(y for _, y in s) > -6]
        xs = [x for s in strokes for x, _ in s]
        mid = (min(xs) + max(xs)) / 2
        if ch.isupper():
            strokes.append([(mid - 1, -14), (mid + 3, -18)])
        else:
            strokes.append([(mid - 1, -7), (mid + 3, -12)])
        return strokes, g.left_offset, g.char_width
    if ch in UMLAUT:
        g = next(iter(f.glyphs_for_text(UMLAUT[ch])))
        strokes = [list(s) for s in g.strokes]
        xs = [x for s in strokes for x, _ in s]
        mid = (min(xs) + max(xs)) / 2
        top = -8 if ch.islower() else -14
        for dx in (-2.5, 2.5):
            strokes.append([(mid + dx, top - 1.6), (mid + dx, top)])
        return strokes, g.left_offset, g.char_width
    if ch == "°":
        pts = [(4 + 2 * math.cos(t), -10 + 2 * math.sin(t)) for t in np.linspace(0, 2 * math.pi, 9)]
        return [pts], 0, 9
    if ch == "ß":  # Greek beta from the Hershey Times-Greek set is the classic stand-in
        g = next(iter(font("timesg").glyphs_for_text("b")))
        return [list(s) for s in g.strokes], g.left_offset, g.char_width
    if ch == "„":
        g = next(iter(f.glyphs_for_text(",")))
        s = [list(st) for st in g.strokes]
        s2 = [[(x + 4, y) for x, y in st] for st in s]
        return s + s2, g.left_offset, g.char_width + 4
    if ch == "“":
        g = next(iter(f.glyphs_for_text("\"")))
        return [list(st) for st in g.strokes], g.left_offset, g.char_width
    if ch == "−" or ch == "–":
        return [[(2, 0), (16 if ch == "–" else 14, 0)]], 0, 18 if ch == "–" else 16
    if ch == "·":
        pts = [(4 + 0.9 * math.cos(t), -1.5 + 0.9 * math.sin(t)) for t in np.linspace(0, 2 * math.pi, 7)]
        return [pts], 0, 8
    g = next(iter(f.glyphs_for_text(ch)))
    return [list(s) for s in g.strokes], g.left_offset, g.char_width


def text(s, x, y, cap, fname="futural", anchor="start", angle=0.0, track=0.0, slant=0.0):
    """Single-stroke text. cap = cap height in mm, track = extra letter spacing in mm.
    (x, y) is the baseline anchor. Returns (polylines, keep-out polygon)."""
    k = cap / 21.0
    strokes, cx = [], 0.0
    for ch in s:
        st, left, w = _glyph_strokes(fname, ch)
        for stroke in st:
            if len(stroke) >= 2:
                strokes.append([(cx + (px - left) * k + slant * (9 - py) * k, (py - 9) * k) for px, py in stroke])
        cx += w * k + track
    width = cx - track
    dx = {"start": 0.0, "middle": -width / 2, "end": -width}[anchor]
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)

    def tf(px, py):
        px += dx
        return x + px * ca - py * sa, y + px * sa + py * ca

    lines = [[tf(px, py) for px, py in st] for st in strokes]
    corners = [tf(-0.5 * cap, 0.45 * cap), tf(width + 0.5 * cap, 0.45 * cap),
               tf(width + 0.5 * cap, -1.45 * cap), tf(-0.5 * cap, -1.45 * cap)]
    return lines, Polygon(corners)


def text_width(s, cap, fname="futural", track=0.0):
    return sum(_glyph_strokes(fname, ch)[2] for ch in s) * cap / 21.0 + track * (len(s) - 1)


# ---------------------------------------------------------------- geometry helpers
def circle(cx, cy, r, n=24):
    t = np.linspace(0, 2 * math.pi, n + 1)
    return list(zip(cx + r * np.cos(t), cy + r * np.sin(t)))


def tanaka_shadow(ter, c, off, light=(-1.0, -1.0), cut=-0.2):
    """Tanaka-style relief: a second line just downhill of the contour wherever the
    slope faces away from the light (default NW). Returns the shadow runs."""
    c = np.asarray(c)
    if len(c) < 3:
        return []
    L = np.asarray(light) / math.hypot(*light)
    t = np.gradient(c, axis=0)
    tn = np.hypot(t[:, 0], t[:, 1])
    tn[tn == 0] = 1
    n = np.column_stack([-t[:, 1], t[:, 0]]) / tn[:, None]
    za = ter.sample_mm(ter.z, c + n * 0.5)
    zb = ter.sample_mm(ter.z, c - n * 0.5)
    down = np.where((za < zb)[:, None], n, -n)
    shade = down @ L < cut
    out, run = [], []
    for i in range(len(c)):
        if shade[i]:
            run.append(tuple(c[i] + down[i] * off))
        elif run:
            if len(run) >= 3:
                out.append(run)
            run = []
    if len(run) >= 3:
        out.append(run)
    return out


def hatch_rect(x0, y0, x1, y1, gap, angle=None):
    """Fill a small rectangle with lines (plotter 'solid'). Horizontal by default."""
    out = []
    if angle is None:
        n = max(1, int((y1 - y0) / gap))
        for i in range(n + 1):
            yy = y0 + (y1 - y0) * i / n
            out.append([(x0, yy), (x1, yy)] if i % 2 == 0 else [(x1, yy), (x0, yy)])
        return out
    poly = box(x0, y0, x1, y1)
    d = math.hypot(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    a = math.radians(angle)
    n = int(d / gap)
    for i in range(-n, n + 1):
        ox, oy = -math.sin(a) * i * gap, math.cos(a) * i * gap
        ln = LineString([(cx + ox - math.cos(a) * d, cy + oy - math.sin(a) * d),
                         (cx + ox + math.cos(a) * d, cy + oy + math.sin(a) * d)])
        seg = poly.intersection(ln)
        if not seg.is_empty and seg.length > 0.05:
            out.append(list(seg.coords))
    return out


def chaikin(pts, it=2):
    p = np.asarray(pts)
    closed = np.allclose(p[0], p[-1])
    for _ in range(it):
        q = 0.75 * p[:-1] + 0.25 * p[1:]
        r = 0.25 * p[:-1] + 0.75 * p[1:]
        p2 = np.empty((len(q) * 2, 2))
        p2[0::2], p2[1::2] = q, r
        p = np.vstack([p2, p2[:1]]) if closed else np.vstack([p[:1], p2, p[-1:]])
    return p


def as_lines(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [list(geom.coords)]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out += as_lines(g)
        return out
    return []


def order_strokes(strokes, start=(0.0, 0.0)):
    """Greedy nearest-neighbour stroke order, reversing strokes when the far end is closer."""
    strokes = [s for s in strokes if len(s) >= 2]
    n = len(strokes)
    if n == 0:
        return []
    ends = np.array([s[0] for s in strokes] + [s[-1] for s in strokes])
    tree = cKDTree(ends)
    used = np.zeros(n, bool)
    out, cur = [], np.asarray(start)
    for _ in range(n):
        k = 8
        while True:
            d, idx = tree.query(cur, k=min(k, 2 * n))
            idx = np.atleast_1d(idx)
            hit = [i for i in idx if not used[i % n]]
            if hit or k >= 2 * n:
                break
            k *= 4
        if not hit:
            rest = np.where(~used)[0]
            i = rest[0]
        else:
            i = hit[0]
        j = i % n
        used[j] = True
        s = strokes[j] if i < n else strokes[j][::-1]
        out.append(s)
        cur = np.asarray(s[-1])
    return out


def plot_stats(strokes, draw_v=15.0, trav_v=25.0, pen_s=0.25):
    dd = dt = 0.0
    cur = (0.0, 0.0)
    for s in strokes:
        dt += math.dist(cur, s[0])
        dd += sum(math.dist(a, b) for a, b in zip(s, s[1:]))
        cur = s[-1]
    t = dd / draw_v + dt / trav_v + len(strokes) * 2 * pen_s
    return {"strokes": len(strokes), "draw_m": dd / 1000, "travel_m": dt / 1000, "minutes": t / 60}


# ---------------------------------------------------------------- map content
class Drawing:
    def __init__(self, mf, page=(PAGE_W, PAGE_H)):
        self.mf = mf
        self.page = page
        self.layers = {"black": [], "red": []}
        self.keepout = []       # polygons black terrain lines must avoid
        self.red_keepout = []   # polygons red lines must avoid (e.g. their own labels)
        self.terrain = []       # black lines that get masked (contours, hachures)

    def add(self, lines, layer="black"):
        self.layers[layer] += [list(map(tuple, l)) for l in lines if len(l) >= 2]

    def label(self, s, x, y, cap, fname, layer="black", pad=0.5, **kw):
        lines, poly = text(s, x, y, cap, fname, **kw)
        self.add(lines, layer)
        self.keepout.append(poly.buffer(pad))
        return poly

    def finish(self):
        ko = unary_union(self.keepout) if self.keepout else None
        frame = self.mf.box
        pk = prep(ko) if ko is not None else None
        out = []
        for l in self.terrain:
            g = LineString(l)
            if not frame.contains(g):
                g = g.intersection(frame)
            if pk is not None and pk.intersects(g):
                g = g.difference(ko)
            out += [x for x in as_lines(g) if LineString(x).length > 0.25]
        self.layers["black"] = out + self.layers["black"]


def contour_set(dr, ter, interval, index_every, min_len, label_cap, label_font, double_index,
                label_spacing=70.0, label_sign=True, tanaka=0.0):
    zmin, zmax = np.nanmin(ter.z), np.nanmax(ter.z)
    levels = np.arange(math.ceil(zmin / interval) * interval, zmax, interval)
    frame = dr.mf.box.buffer(-0.3)
    placed = []
    index_lines = []
    for lv in levels:
        is_index = abs(lv % index_every) < 1e-6
        for c in ter.contours(lv):
            ls = LineString(c)
            if ls.length < min_len:
                continue
            c = chaikin(LineString(c).simplify(0.05).coords, 2)
            ls = LineString(c)
            if is_index:
                index_lines.append((lv, ls))
            dr.terrain.append(list(map(tuple, c)))
            if is_index and double_index:
                off = ls.offset_curve(0.18)
                for part in as_lines(off):
                    dr.terrain.append(part)
            if tanaka:
                dr.terrain += tanaka_shadow(ter, c, tanaka * (2.0 if is_index else 1.0))
    # inline labels on index contours: straight-ish stretches, spread out
    for lv, ls in sorted(index_lines, key=lambda t: -t[1].length):
        s = f"{int(lv)}" if label_sign else f"{abs(int(lv))}"
        s = s.replace("-", "−")
        w = text_width(s, label_cap, label_font)
        L = ls.length
        if L < w * 3:
            continue
        nlab = max(1, int(L / label_spacing))
        for i in range(nlab):
            best = None
            for frac in np.linspace(0.1, 0.9, 17):
                sm = (i + frac) / nlab * L
                a, b = ls.interpolate(sm - w / 2 - 0.6), ls.interpolate(sm + w / 2 + 0.6)
                seg = LineString([ls.interpolate(t) for t in np.linspace(sm - w / 2 - 0.6, sm + w / 2 + 0.6, 7)])
                straight = a.distance(b) / max(seg.length, 1e-6)
                if straight < 0.97:
                    continue
                mid = ls.interpolate(sm)
                if not frame.contains(mid.buffer(w)):
                    continue
                if any(mid.distance(p) < label_spacing * 0.45 for p in placed):
                    continue
                if any(k.intersects(mid.buffer(w * 0.7)) for k in dr.keepout):
                    continue
                best = (straight, sm, a, b, mid)
                break
            if not best:
                continue
            _, sm, a, b, mid = best
            ang = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
            if ang > 90:
                ang -= 180
            if ang < -90:
                ang += 180
            # text centred on the line: shift baseline down by half cap (perpendicular)
            ra = math.radians(ang)
            bx, by = mid.x - math.sin(ra) * (-label_cap / 2), mid.y + math.cos(ra) * (label_cap / 2)
            dr.label(s, bx, by, label_cap, label_font, anchor="middle", angle=ang, pad=0.15)
            placed.append(mid)
    return levels


def spot_heights(ter, mf, n, min_sep_mm, window_m, mode="max", margin_mm=8):
    z = np.where(np.isnan(ter.z), np.nanmedian(ter.z), ter.z)
    zz = z if mode == "max" else -z
    w = max(3, int(window_m / ter.cell))
    peaks = np.argwhere((zz == maximum_filter(zz, size=w)))
    vals = zz[peaks[:, 0], peaks[:, 1]]
    order = np.argsort(-vals)
    out = []
    for i in order:
        r, c = peaks[i]
        mx, my = ter.rc_to_mm(r, c)
        if not (mf.fx0 + margin_mm < mx < mf.fx1 - margin_mm and mf.fy0 + margin_mm < my < mf.fy1 - margin_mm):
            continue
        if any(math.hypot(mx - a, my - b) < min_sep_mm for a, b, _ in out):
            continue
        out.append((float(mx), float(my), float(z[r, c])))
        if len(out) >= n:
            break
    return out


def load_traverse(data_dir):
    t = json.load(open(os.path.join(data_dir, "M20_traverse.json")))["features"]
    w = json.load(open(os.path.join(data_dir, "M20_waypoints.json")))["features"]
    lines = []
    for f in sorted(t, key=lambda f: f["properties"]["sol"]):
        g = f["geometry"]
        parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        for p in parts:
            lines.append([(c[0], c[1]) for c in p])
    wps = [(f["properties"]["sol"], f["properties"]["lon"], f["properties"]["lat"], f["properties"]["elev_geoid"])
           for f in w]
    wps.sort()
    return lines, wps


def load_traverse_xyz(data_dir):
    t = json.load(open(os.path.join(data_dir, "M20_traverse.json")))["features"]
    out = []
    for f in sorted(t, key=lambda f: f["properties"]["sol"]):
        g = f["geometry"]
        parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        out += [[(c[0], c[1], c[2]) for c in p] for p in parts]
    return out


def at_sol(wps, sol):
    """Last waypoint at or before `sol`."""
    best = wps[0]
    for w in wps:
        if w[0] <= sol:
            best = w
    return best


def draw_traverse(dr, lines, wps, tick_every, label_every, tick_len, cap, fname, knock=0.55,
                  label_offsets=None):
    mf = dr.mf
    pts = []
    for l in lines:
        for lon, lat in l:
            x, y = mf.ll_mm(lon, lat)
            if not pts or math.dist(pts[-1], (x, y)) > 1e-3:
                pts.append((float(x), float(y)))
    path = LineString(pts).simplify(0.05)
    # one continuous stroke: smooth very lightly, keeps the drive character
    coords = list(path.coords)
    dr.add([coords], "red")
    dr.keepout.append(path.buffer(knock))
    # sol ticks: perpendicular marks at waypoints nearest each multiple
    label_offsets = label_offsets or {}
    for sol in range(tick_every, wps[-1][0] + 1, tick_every):
        w = at_sol(wps, sol)
        x, y = mf.ll_mm(w[1], w[2])
        d = path.project(Point(x, y))
        a, b = path.interpolate(max(0, d - 0.6)), path.interpolate(min(path.length, d + 0.6))
        ang = math.atan2(b.y - a.y, b.x - a.x)
        nx, ny = -math.sin(ang), math.cos(ang)
        p = path.interpolate(d)
        L = tick_len * (1.6 if sol % label_every == 0 else 1.0)
        dr.add([[(p.x - nx * L, p.y - ny * L), (p.x + nx * L, p.y + ny * L)]], "red")
        dr.keepout.append(LineString([(p.x - nx * L, p.y - ny * L), (p.x + nx * L, p.y + ny * L)]).buffer(0.4))
        if sol % label_every == 0:
            ox, oy = label_offsets.get(sol, (nx * (L + 0.8), ny * (L + 0.8)))
            anchor = "start" if ox >= 0 else "end"
            dr.label(str(sol), p.x + ox, p.y + oy + cap / 2, cap, fname, layer="red", anchor=anchor, pad=0.4)
    return path


def landmark(dr, name_lines, lon, lat, dx, dy, cap, fname, symbol="ring", layer="black", leader=True,
             anchor=None, line_gap=1.45, sub=0.8):
    x, y = dr.mf.ll_mm(lon, lat)
    x, y = float(x), float(y)
    if symbol == "ring":
        dr.add([circle(x, y, 0.9, 16)], layer)
        dr.keepout.append(Point(x, y).buffer(1.3))
    elif symbol == "triangle":
        r = 1.3
        tri = [(x, y - r), (x + r * 0.87, y + r * 0.5), (x - r * 0.87, y + r * 0.5), (x, y - r)]
        dr.add([tri], layer)
        dr.keepout.append(Polygon(tri).buffer(0.5))
    elif symbol == "dot":
        dr.add([circle(x, y, 0.35, 8), circle(x, y, 0.15, 6)], layer)
        dr.keepout.append(Point(x, y).buffer(0.8))
    elif symbol == "flag":
        dr.add([[(x, y), (x, y - 3.2), (x + 2.0, y - 2.6), (x, y - 2.0)]], layer)
        dr.add([circle(x, y, 0.5, 10)], layer)
        dr.keepout.append(box(x - 0.8, y - 3.6, x + 2.4, y + 0.8))
    if anchor is None:
        anchor = "start" if dx >= 0 else "end"
    tx, ty = x + dx, y + dy
    if leader and math.hypot(dx, dy) > 3:
        # leader to the near edge of the label block
        lx = tx - (0.6 if anchor == "start" else -0.6)
        ly = ty - cap * 0.4
        r0 = 1.4
        ang = math.atan2(ly - y, lx - x)
        dr.add([[(x + r0 * math.cos(ang), y + r0 * math.sin(ang)), (lx, ly)]], layer)
        dr.keepout.append(LineString([(x, y), (lx, ly)]).buffer(0.45))
    for i, s in enumerate(name_lines):
        c = cap if i == 0 else cap * sub
        dr.label(s, tx, ty + i * cap * line_gap + c * 0.5, c, fname, layer=layer, anchor=anchor)


def graticule_ticks(mf, step_min, label_min, tick, inside=False):
    """Lat/lon ticks along the frame edges. Returns (lines, labels[(text, x, y, anchor, angle)])."""
    lines, labels = [], []
    x0, y0, x1, y1 = mf.extent_xy()
    lon0, lat0 = xy_to_ll(x0, y0)
    lon1, lat1 = xy_to_ll(x1, y1)
    st = step_min / 60.0
    lon = math.ceil(lon0 / st) * st
    while lon <= lon1:
        mx, _ = mf.ll_mm(lon, 0)
        mx = float(mx)
        major = round(lon * 60) % label_min == 0
        L = tick * (1.8 if major else 1.0)
        lines.append([(mx, mf.fy0), (mx, mf.fy0 - L)])
        lines.append([(mx, mf.fy1), (mx, mf.fy1 + L)])
        if major:
            labels.append((dms(lon, "E"), mx, "lon"))
        lon += st
    lat = math.ceil(lat0 / st) * st
    while lat <= lat1:
        _, my = mf.ll_mm(0, lat)
        my = float(my)
        major = round(lat * 60) % label_min == 0
        L = tick * (1.8 if major else 1.0)
        lines.append([(mf.fx0, my), (mf.fx0 - L, my)])
        lines.append([(mf.fx1, my), (mf.fx1 + L, my)])
        if major:
            labels.append((dms(lat, "N"), my, "lat"))
        lat += st
    return lines, labels


def dms(v, hemi):
    d = int(math.floor(v + 1e-9))
    m = int(round((v - d) * 60))
    if m == 60:
        d, m = d + 1, 0
    return f"{d}°{m:02d}'{hemi}"


def grid_crosses(dr, mf, label_min, size=1.6):
    x0, y0, x1, y1 = mf.extent_xy()
    lon0, lat0 = xy_to_ll(x0, y0)
    lon1, lat1 = xy_to_ll(x1, y1)
    st = label_min / 60.0
    lon = math.ceil(lon0 / st) * st
    while lon <= lon1:
        lat = math.ceil(lat0 / st) * st
        while lat <= lat1:
            x, y = mf.ll_mm(lon, lat)
            x, y = float(x), float(y)
            if mf.fx0 + 3 < x < mf.fx1 - 3 and mf.fy0 + 3 < y < mf.fy1 - 3:
                cross = [[(x - size, y), (x + size, y)], [(x, y - size), (x, y + size)]]
                g = MultiLineString(cross)
                ko = unary_union(dr.keepout)
                dr.add(as_lines(g.difference(ko)))
            lat += st
        lon += st


def scale_bar(dr, x, y, mf, km_total, km_div, height, fname, cap, units="KILOMETERS", style="nasa"):
    mm_per_km = 1000 * mf.k
    n = int(km_total / km_div)
    W = km_total * mm_per_km
    # left extension subdivided like old survey bars
    out = [[(x, y), (x + W, y)], [(x, y + height), (x + W, y + height)]]
    for i in range(n + 1):
        xx = x + i * km_div * mm_per_km
        out.append([(xx, y), (xx, y + height)])
        if i < n and i % 2 == 0:
            out += hatch_rect(xx, y, xx + km_div * mm_per_km, y + height, 0.35)
        lab = f"{i * km_div:g}"
        dr.label(lab, xx, y - 1.0, cap, fname, anchor="middle")
    dr.label(units, x + W / 2, y + height + cap + 1.6, cap * 0.9, fname, anchor="middle", track=0.5 if style == "nasa" else 0.2)
    dr.add(out)


def locator(dr, dem, x0, y0, w, h, view_mf, cap, fname):
    """Small inset: whole crater at small scale with the main frame outlined."""
    lon_c, lat_c = 77.69, 18.37
    scale = 56000.0 / w * 1000  # ~56 km across the inset width
    imf = MapFrame((x0, y0, x0 + w, y0 + h), scale, (lon_c, lat_c))
    ter = Terrain(dem, imf, 400.0, 900.0, pad_mm=2)
    ter.mf = imf
    out = [[(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]]
    fr = imf.box
    for lv in (-2400, -1800, -1200):
        for c in ter.contours(lv):
            ls = LineString(c)
            if ls.length < 4:
                continue
            out += as_lines(LineString(chaikin(ls.simplify(0.1).coords, 2)).intersection(fr))
    vx0, vy0, vx1, vy1 = view_mf.extent_xy()
    a = imf.to_mm(vx0, vy0)
    b = imf.to_mm(vx1, vy1)
    rx0, rx1 = float(a[0]), float(b[0])
    ry0, ry1 = float(b[1]), float(a[1])
    out.append([(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1), (rx0, ry0)])
    out.append([(rx0 - 0.3, ry0 - 0.3), (rx1 + 0.3, ry0 - 0.3), (rx1 + 0.3, ry1 + 0.3), (rx0 - 0.3, ry1 + 0.3), (rx0 - 0.3, ry0 - 0.3)])
    dr.add(out)


# ---------------------------------------------------------------- SVG out
def write_svg(path, layers, colors, widths, page=(PAGE_W, PAGE_H)):
    pw, ph = page
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{pw}mm" height="{ph}mm" '
                f'viewBox="0 0 {pw} {ph}">\n')
        for name, lines in layers.items():
            f.write(f'<g id="{name}" fill="none" stroke="{colors[name]}" stroke-width="{widths[name]}" '
                    f'stroke-linecap="round" stroke-linejoin="round">\n')
            for l in lines:
                d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in l)
                f.write(f'<path d="{d}"/>\n')
            f.write("</g>\n")
        f.write("</svg>\n")


# ---------------------------------------------------------------- designs
LANDMARKS = {
    # name: sol whose end-of-drive position marks it. Positions come from the waypoints
    # file; the sol choices are from the mission timeline and should be checked.
    "landing": 13,
    "three_forks": 652,
    "belva": None,
    "cheyava": 1215,
    "witch_hazel": 1400,
}


# Traverse-sheet presets by scale: centre, DEM cell/smoothing, contour interval/index,
# sol tick/label step, graticule tick/label minutes, scale bar km/division.
TRAVERSE_PRESETS = {
    60000: dict(center=(77.338, 18.461), cell=40, sigma=90, ci=20, ix=100, tick=50, lab=250, gt=1, gl=2, km=3, kd=0.5),
    150000: dict(center=(77.49, 18.42), cell=60, sigma=160, ci=25, ix=100, tick=100, lab=500, gt=2, gl=4, km=10, kd=2),
    200000: dict(center=(77.60, 18.38), cell=80, sigma=200, ci=50, ix=250, tick=100, lab=500, gt=5, gl=10, km=10, kd=2),
}


def design_nasa(dem, lines, wps, view, scale=60000):
    if view == "crater":
        mf = MapFrame((14, 14, 200, 196), 300000, (77.63, 18.37))
        ter = Terrain(dem, mf, 100.0, 260.0)
    else:
        P = TRAVERSE_PRESETS[scale]
        mf = MapFrame((14, 14, 283, 160), scale, P["center"])
        ter = Terrain(dem, mf, P["cell"], P["sigma"])
    near = view != "crater" and scale <= 80000
    dr = Drawing(mf)
    F, FM = "futural", "futuram"

    # --- traverse and landmarks first so contours and their labels avoid them
    if view == "crater":
        draw_traverse(dr, lines, wps, 250, 1000, 0.9, 1.6, F, label_offsets={1000: (0, -3.2), 2000: (-1.6, 2.6)})
    elif near:
        draw_traverse(dr, lines, wps, 50, 250, 0.9, 1.8, F,
                      label_offsets={250: (-4.5, 2.6), 500: (0.6, -3.6), 750: (1.4, -2.4), 1000: (-1.5, 3.6),
                                     1250: (1.6, 0.6), 1500: (-2.4, -1.8), 1750: (0, 3.4), 2000: (-1.5, -3.2)})
    else:
        draw_traverse(dr, lines, wps, P["tick"], P["lab"], 0.9, 1.7, F,
                      label_offsets={500: (0.5, -3.4), 1000: (0, -3.4), 1500: (-1.5, -3.2), 2000: (-1.5, 3.4)})
    L = {k: at_sol(wps, s) for k, s in LANDMARKS.items() if s}
    if view == "crater":
        landmark(dr, ["OCTAVIA E. BUTLER", "LANDING SITE"], L["landing"][1], L["landing"][2], 9, 6, 1.6, F, "flag")
        landmark(dr, ["THREE FORKS", "SAMPLE DEPOT"], L["three_forks"][1], L["three_forks"][2], 2, 15, 1.5, F, "ring")
        landmark(dr, ["CHEYAVA FALLS"], L["cheyava"][1], L["cheyava"][2], 3, -20, 1.5, F, "ring")
        landmark(dr, ["WITCH HAZEL", "HILL"], L["witch_hazel"][1], L["witch_hazel"][2], -2, -26, 1.5, F, "ring", sub=1.0)
        x, y = mf.ll_mm(77.80, 18.30)
        dr.label("J E Z E R O", float(x), float(y), 6.5, FM, anchor="middle", track=2.2, pad=1.2)
        dr.label("C R A T E R", float(x), float(y) + 6, 3.2, F, anchor="middle", track=1.6, pad=1.0)
        x, y = mf.ll_mm(77.45, 18.555)
        dr.label("WESTERN FAN", float(x), float(y), 1.6, "timesi", anchor="middle", track=0.6, angle=-8)
    elif not near:
        landmark(dr, ["OCTAVIA E. BUTLER", "LANDING SITE"], L["landing"][1], L["landing"][2], 8, 9, 1.8, F, "flag")
        landmark(dr, ["THREE FORKS", "SAMPLE DEPOT"], L["three_forks"][1], L["three_forks"][2], 2, 15, 1.7, F, "ring")
        landmark(dr, ["CHEYAVA FALLS", "SOL 1215"], L["cheyava"][1], L["cheyava"][2], 3, -16, 1.7, F, "ring")
        landmark(dr, ["WITCH HAZEL HILL"], L["witch_hazel"][1], L["witch_hazel"][2], -3, -20, 1.7, F, "ring")
        landmark(dr, ["SOL 2022"], wps[-1][1], wps[-1][2], -4, 10, 1.7, F, "triangle", layer="red")
        x, y = mf.ll_mm(77.80, 18.33 if scale >= 200000 else 18.30)
        dr.label("J E Z E R O", float(x), float(y), 6.5, FM, anchor="middle", track=2.4, pad=1.2)
        dr.label("C R A T E R", float(x), float(y) + 6, 3.2, F, anchor="middle", track=1.8, pad=1.0)
        x, y = mf.ll_mm(77.42, 18.535)
        dr.label("WESTERN FAN", float(x), float(y), 2.0, "timesi", anchor="middle", track=0.8, angle=-6)
        x, y = mf.ll_mm(77.445, 18.405)
        dr.label("Séítah", float(x), float(y), 1.8, "timesi", anchor="middle", track=0.4)
    else:
        landmark(dr, ["OCTAVIA E. BUTLER", "LANDING SITE", "SOL 0"], L["landing"][1], L["landing"][2], 6, 9, 2.0, F, "flag")
        landmark(dr, ["THREE FORKS", "SAMPLE DEPOT"], L["three_forks"][1], L["three_forks"][2], 3, 13, 1.8, F, "ring")
        landmark(dr, ["CHEYAVA FALLS", "SOL 1215"], L["cheyava"][1], L["cheyava"][2], -4, -14, 1.8, F, "ring")
        landmark(dr, ["WITCH HAZEL HILL"], L["witch_hazel"][1], L["witch_hazel"][2], -3, -14, 1.8, F, "ring")
        landmark(dr, ["CRATER RIM", "REACHED SOL 1349"], at_sol(wps, 1349)[1], at_sol(wps, 1349)[2], 6, 14, 1.6, F, "dot")
        landmark(dr, ["SOL 2022"], wps[-1][1], wps[-1][2], -7, 7, 1.8, F, "triangle", layer="red")
        x, y = mf.ll_mm(77.38, 18.489)
        dr.label("WESTERN FAN", float(x), float(y), 2.4, "timesi", anchor="middle", track=1.2, angle=-4)
        x, y = mf.ll_mm(77.436, 18.425)
        dr.label("Séítah", float(x), float(y), 2.4, "timesi", anchor="middle", track=0.6)
        x, y = mf.ll_mm(77.375, 18.477)
        dr.label("Belva", float(x) + 4, float(y) + 0.5, 1.8, "timesi", anchor="start")
        x, y = mf.ll_mm(77.23, 18.475)
        dr.label("JEZERO RIM", float(x), float(y), 2.2, F, anchor="middle", track=1.4, angle=-62)

    # --- spot heights
    for sx, sy, z in spot_heights(ter, mf, 6 if view == "crater" else 5, 30, 6000 if view == "crater" else 2500):
        if any(k.contains(Point(sx, sy)) for k in dr.keepout):
            continue
        dr.add([circle(sx, sy, 0.3, 8)])
        dr.label(f"{int(round(z))}".replace("-", "−"), sx + 1.0, sy + 0.8, 1.3, F)
        dr.keepout.append(Point(sx, sy).buffer(0.8))

    # --- contours
    if view == "crater":
        contour_set(dr, ter, 50, 250, 4.0, 1.4, F, double_index=True, label_spacing=60)
        ci, ix = 50, 250
    else:
        ci, ix = P["ci"], P["ix"]
        contour_set(dr, ter, ci, ix, 4.0, 1.4, F, double_index=True, label_spacing=55)
    if view == "crater":
        grid_crosses(dr, mf, 10)
    else:
        grid_crosses(dr, mf, P["gl"])
    dr.finish()

    # --- neat line + graticule
    fx0, fy0, fx1, fy1 = mf.fx0, mf.fy0, mf.fx1, mf.fy1
    dr.add([[(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1), (fx0, fy0)]])
    gl, glab = graticule_ticks(mf, 5 if view == "crater" else P["gt"], 10 if view == "crater" else P["gl"], 1.2)
    dr.add(gl)
    for s, pos, kind in glab:
        if kind == "lon":
            dr.label(s, pos, fy0 - 3.0, 1.5, F, anchor="middle")
            if view == "crater":
                dr.label(s, pos, fy1 + 4.6, 1.5, F, anchor="middle")
        else:
            dr.label(s, fx0 - 3.0, pos + 0.75, 1.5, F, anchor="end")

    # --- marginalia
    if view == "crater":
        X = 212
        dr.label("MARS", X, 24, 2.2, F, track=2.0)
        dr.label("SYRTIS MAJOR QUADRANGLE  MC-13", X, 29, 1.5, F, track=0.35)
        dr.label("JEZERO", X, 44, 8.0, FM, track=1.6)
        dr.label("CRATER", X, 56, 8.0, FM, track=1.6)
        dr.add([[(X, 61), (285, 61)], [(X, 61.6), (285, 61.6)]])
        dr.label("PERSEVERANCE ROVER TRAVERSE", X, 68, 1.9, F, track=0.45)
        dr.label("LANDING THROUGH SOL 2022", X, 72.5, 1.6, F, track=0.45)
        ly = 86
    else:
        X = 14
        dr.label("JEZERO CRATER", X, 178, 7.0, FM, track=1.4)
        dr.label("MARS 2020 PERSEVERANCE ROVER TRAVERSE   LANDING THROUGH SOL 2022", X, 185, 1.8, F, track=0.35)
        dr.label(("WESTERN FAN AND CRATER RIM" if near else "WESTERN JEZERO") + "   SYRTIS MAJOR QUADRANGLE  MC-13", X, 189.5, 1.5, F, track=0.35)
        ly = 172
    # legend
    lx = 212 if view == "crater" else 140
    rows = [("traverse", "ROVER TRAVERSE (NASA/JPL MMGIS)"),
            ("tick", f"SOL MARK, EVERY {250 if view == 'crater' else P['tick']} SOLS"),
            ("contour", f"CONTOUR, INTERVAL {ci} M"),
            ("index", f"INDEX CONTOUR, EVERY {ix} M"),
            ("ring", "LANDMARK, ROVER POSITION ON SOL"),
            ("dot", "SPOT HEIGHT, M")]
    if view != "crater":
        rows = rows[:3] + rows[3:4] + rows[4:]
    for i, (kind, s) in enumerate(rows):
        yy = ly + i * 4.4 if view == "crater" else 168 + (i % 3) * 4.4
        xx = lx if view == "crater" else (lx if i < 3 else lx + 61)
        if kind == "traverse":
            dr.add([[(xx, yy), (xx + 4, yy - 0.8), (xx + 8, yy)]], "red")
        elif kind == "tick":
            dr.add([[(xx, yy), (xx + 8, yy)], [(xx + 4, yy - 1), (xx + 4, yy + 1)]], "red")
        elif kind == "contour":
            dr.add([[(xx, yy), (xx + 8, yy)]])
        elif kind == "index":
            dr.add([[(xx, yy), (xx + 8, yy)], [(xx, yy + 0.18), (xx + 8, yy + 0.18)]])
        elif kind == "ring":
            dr.add([circle(xx + 4, yy, 0.9, 16)])
        elif kind == "dot":
            dr.add([circle(xx + 4, yy, 0.3, 8)])
        dr.label(s, xx + 11, yy + 0.7, 1.4, F, track=0.2)
    if view == "crater":
        sy = 124
        dr.label("SCALE 1:300 000", 248.5, sy - 6, 1.9, F, anchor="middle", track=0.5)
        scale_bar(dr, 218.5, sy, mf, 20, 5, 1.2, F, 1.3)
        ny = 148
        notes = ["ELEVATIONS IN METERS, MARS 2000 AREOID",
                 "EQUIRECTANGULAR PROJECTION, MARS 2000 SPHERE",
                 "STANDARD PARALLEL 18°28'N",
                 "",
                 "TOPOGRAPHY: CTX STEREO DEM, 20 M/PX, USGS",
                 "ASTROGEOLOGY / MARS 2020 PROJECT",
                 "TRAVERSE: NASA/JPL-CALTECH, THROUGH SOL 2022",
                 "PLOTTED BY PEN, 2026"]
        for i, s in enumerate(notes):
            if s:
                dr.label(s, X, ny + i * 3.6, 1.3, F, track=0.25)
        # north arrow
        ax, ay = 279, 178
        dr.add([[(ax, ay + 8), (ax, ay - 4)], [(ax - 1.6, ay - 1), (ax, ay - 4.5), (ax + 1.6, ay - 1)]])
        dr.label("N", ax, ay - 6.2, 2.2, F, anchor="middle")
    else:
        scale_bar(dr, 140, 188, mf, P["km"], P["kd"], 1.2, F, 1.3)
        bar_end = 140 + P["km"] * 1000 * mf.k
        dr.label(f"SCALE 1:{scale:,}".replace(",", " "), bar_end + 6, 190, 1.8, F, track=0.5)
        dr.label("ELEVATIONS IN METERS, MARS 2000 AREOID   TOPOGRAPHY: CTX DEM 20 M/PX, USGS   TRAVERSE: NASA/JPL-CALTECH", X, 196, 1.25, F, track=0.2)
        if near:
            locator(dr, dem, 258, 165, 25, 25, mf, 1.2, F)
    return dr


def traverse_profile(lines_xyz):
    """Cumulative distance (km) and elevation along the traverse; lines carry (lon, lat, z)."""
    d, z, last = [], [], None
    tot = 0.0
    for l in lines_xyz:
        for lon, lat, el in l:
            if el > -500:  # a few traverse vertices carry 0 instead of an elevation
                continue
            x, y = ll_to_xy(lon, lat)
            if last is not None:
                tot += math.dist(last, (x, y))
            last = (x, y)
            d.append(tot / 1000)
            z.append(el)
    return np.array(d), np.array(z)


def load_samples(path):
    out = []
    for row in open(path, encoding="utf-8"):
        if row.startswith("#") or not row.strip():
            continue
        n, name, sol, kind, loc = row.rstrip("\n").split(",", 4)
        out.append((int(n), name, int(sol), kind, loc))
    return out


def sample_clusters(samples, wps, mf, radius_mm):
    """Group samples whose rover positions fall within radius_mm on the page."""
    groups = []
    for n, name, sol, kind, loc in samples:
        w = at_sol(wps, sol)
        x, y = (float(v) for v in mf.ll_mm(w[1], w[2]))
        for g in groups:
            if math.hypot(g["x"] - x, g["y"] - y) < radius_mm:
                g["n"].append(n)
                g["pts"].append((x, y))
                break
        else:
            groups.append({"x": x, "y": y, "n": [n], "pts": [(x, y)]})
    for g in groups:
        g["x"] = sum(p[0] for p in g["pts"]) / len(g["pts"])
        g["y"] = sum(p[1] for p in g["pts"]) / len(g["pts"])
    return groups


def sample_markers(dr, samples, wps, radius_mm, cap, F, rings=(4.5, 6.5, 9.0, 12.0)):
    """Red ring per sample site (sites closer than radius_mm share one), numbered with a
    short leader; each label takes the first free spot on rings around its site."""
    groups = sample_clusters(samples, wps, dr.mf, radius_mm)
    for g in groups:
        x, y = g["x"], g["y"]
        dr.add([circle(x, y, 0.55, 10)], "red")
        dr.keepout.append(Point(x, y).buffer(0.9))
    for g in sorted(groups, key=lambda g: min(g["n"])):
        x, y = g["x"], g["y"]
        t = num_range(g["n"])
        w = text_width(t, cap, F)
        ko = unary_union(dr.keepout)
        best = None
        for r in rings:
            for deg in (-90, -60, -120, -30, -150, 0, 180, 90, 60, 120, 30, 150):
                a = math.radians(deg)
                tx, ty = x + r * math.cos(a), y + r * math.sin(a)
                cand = box(tx - w / 2 - 0.4, ty - cap, tx + w / 2 + 0.4, ty + cap)
                if not ko.intersects(cand) and dr.mf.box.buffer(-2).contains(cand):
                    best = (tx, ty, a)
                    break
            if best:
                break
        if not best:
            best = (x, y - 5, -math.pi / 2)
        tx, ty, a = best
        dr.add([[(x + 0.9 * math.cos(a), y + 0.9 * math.sin(a)),
                 (tx - 1.4 * math.cos(a), ty - 1.4 * math.sin(a))]], "red")
        dr.keepout.append(LineString([(x, y), (tx, ty)]).buffer(0.4))
        dr.label(t, tx, ty + cap / 2, cap, F, layer="red", anchor="middle", pad=0.4)
    return groups


def num_range(ns):
    ns = sorted(ns)
    runs = [[ns[0]]]
    for n in ns[1:]:
        if n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    return ",".join(str(r[0]) if len(r) == 1 else f"{r[0]}–{r[-1]}" for r in runs)


def boxed(dr, x0, y0, x1, y1, edges="all"):
    """Knock a box out of the terrain and draw a double border on its open sides."""
    dr.keepout.append(box(x0 - 1.0, y0 - 1.0, x1 + 1.0, y1 + 1.0))
    g = 0.6
    if edges == "tr":  # tucked into the top-right corner of the frame
        dr.add([[(x0, y0), (x0, y1), (x1, y1)], [(x0 + g, y0), (x0 + g, y1 - g), (x1, y1 - g)]])
    elif edges == "bl":  # tucked into the bottom-left corner
        dr.add([[(x0, y0), (x1, y0), (x1, y1)], [(x0, y0 + g), (x1 - g, y0 + g), (x1 - g, y1)]])
    else:
        dr.add([[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]])


def draw_profile(dr, raw_lines, wps, x0, y0, x1, y1, F, samples=None):
    d, z = traverse_profile(raw_lines)
    zmin = math.floor(z.min() / 100) * 100
    zmax = math.ceil(z.max() / 100) * 100
    px0, px1 = x0 + 11, x1 - 4
    py0, py1 = y0 + 15, y1 - 8

    def X(v):
        return px0 + v / d[-1] * (px1 - px0)

    def Y(v):
        return py1 - (v - zmin) / (zmax - zmin) * (py1 - py0)

    dr.label("ELEVATION ALONG THE TRAVERSE, M", x0 + 3, y0 + 4.2, 1.5, F, track=0.35)
    dr.add([[(px0, py0), (px0, py1), (px1, py1)]])
    for v in range(int(zmin), int(zmax) + 1, 200):
        yy = Y(v)
        dr.add([[(px0 - 1, yy), (px0, yy)]])
        dr.add([[(px0 + 2.0 * i, yy), (px0 + 2.0 * i + 0.3, yy)] for i in range(1, int((px1 - px0) / 2.0))])
        dr.label(f"{v}".replace("-", "−"), px0 - 1.6, yy + 0.55, 1.1, F, anchor="end")
    for km in range(0, int(d[-1]) + 1, 5):
        xx = X(km)
        dr.add([[(xx, py1), (xx, py1 + 1)]])
        dr.label(str(km), xx, py1 + 3.0, 1.1, F, anchor="middle")
    dr.label("KM DRIVEN", px1, py1 + 6.0, 1.1, F, anchor="end", track=0.3)
    pts = [(X(a), Y(b)) for a, b in zip(d, z)]
    dr.add([list(LineString(pts).simplify(0.08).coords)], "red")

    tree = cKDTree(np.array([ll_to_xy(lon, lat) for l in raw_lines for lon, lat, el in l if el <= -500]))

    def at(sol):
        w = at_sol(wps, sol)
        _, i = tree.query(ll_to_xy(w[1], w[2]))
        return X(d[i]), Y(z[i])

    marks = [(13, "LANDING", 0), (652, "THREE FORKS", 0), (1215, "CHEYAVA FALLS", 0), (1349, "RIM", 1),
             (1400, "WITCH HAZEL HILL", 2)]
    for sol, lab, alt in marks:
        xx, yy = at(sol)
        top = min(yy - 3.0, py0 + 1.0) - alt * 2.4
        dr.add([[(xx, yy - 0.8), (xx, top)]])
        w = text_width(lab, 1.05, F)
        anchor = "middle"
        if xx - w / 2 < x0 + 2:
            anchor = "start"
        elif xx + w / 2 > x1 - 2:
            anchor = "end"
        dr.label(lab, xx, top - 0.7, 1.05, F, anchor=anchor, pad=0.2)
    if samples:
        for n, name, sol, kind, loc in samples:
            xx, yy = at(sol)
            dr.add([circle(xx, yy, 0.35, 8)], "red")


def design_nasa_full(dem, lines, wps, raw_lines, scale=225000, center=(77.66, 18.35),
                     tanaka=0.0, profile=False, titleblock=False, samples=None):
    """Whole page is map; title, legend, scale and extras sit in boxes inside the neat line."""
    mf = MapFrame((14, 14, 283, 196), scale, center)
    ter = Terrain(dem, mf, 80.0, 220.0)
    dr = Drawing(mf)
    F, FM = "futural", "futuram"

    tb = (199.0, 14.0, 283.0, 92.0) if titleblock else (205.0, 14.0, 283.0, 74.0)
    boxed(dr, *tb, edges="tr")
    pb = kb = None
    if profile:
        pb = (14.0, 152.0, 106.0, 196.0)
        boxed(dr, *pb, edges="bl")
    if samples:
        kb = (14.0, 146.0, 96.0, 196.0)
        boxed(dr, *kb, edges="bl")

    draw_traverse(dr, lines, wps, 100, 500, 0.9, 1.6, F,
                  label_offsets={500: (0.5, -3.2), 1000: (0, -3.2), 1500: (-2.5, -1.2), 2000: (-1.5, -3.2)})
    L = {k: at_sol(wps, s) for k, s in LANDMARKS.items() if s}
    if samples:
        landmark(dr, ["OCTAVIA E. BUTLER", "LANDING SITE"], L["landing"][1], L["landing"][2], 10, 13, 1.6, F, "flag")
        landmark(dr, ["THREE FORKS DEPOT"], L["three_forks"][1], L["three_forks"][2], 3, 20, 1.5, F, "ring")
        landmark(dr, ["SOL 2022"], wps[-1][1], wps[-1][2], -3, 10, 1.6, F, "triangle", layer="red")
        sample_markers(dr, samples, wps, 2.0, 1.3, F)
    else:
        landmark(dr, ["OCTAVIA E. BUTLER", "LANDING SITE"], L["landing"][1], L["landing"][2], 8, 8, 1.6, F, "flag")
        landmark(dr, ["THREE FORKS", "SAMPLE DEPOT"], L["three_forks"][1], L["three_forks"][2], 2, 15, 1.5, F, "ring")
        landmark(dr, ["CHEYAVA FALLS", "SOL 1215"], L["cheyava"][1], L["cheyava"][2], 3, -18, 1.5, F, "ring")
        landmark(dr, ["WITCH HAZEL HILL"], L["witch_hazel"][1], L["witch_hazel"][2], -2, -22, 1.5, F, "ring")
        landmark(dr, ["SOL 2022"], wps[-1][1], wps[-1][2], -3, 10, 1.6, F, "triangle", layer="red")
    x, y = mf.ll_mm(77.78, 18.30)
    dr.label("J E Z E R O", float(x), float(y), 7.0, FM, anchor="middle", track=2.6, pad=1.2)
    dr.label("C R A T E R", float(x), float(y) + 6.5, 3.4, F, anchor="middle", track=1.9, pad=1.0)
    x, y = mf.ll_mm(77.43, 18.545)
    dr.label("WESTERN FAN", float(x), float(y), 1.8, "timesi", anchor="middle", track=0.7, angle=-7)

    for sx, sy, z in spot_heights(ter, mf, 7, 30, 6000):
        if any(k.intersects(box(sx - 1, sy - 2, sx + 10, sy + 1.5)) for k in dr.keepout):
            continue
        dr.add([circle(sx, sy, 0.3, 8)])
        dr.label(f"{int(round(z))}".replace("-", "−"), sx + 1.0, sy + 0.8, 1.3, F)
        dr.keepout.append(Point(sx, sy).buffer(0.8))
    ci, ix = 50, 250
    contour_set(dr, ter, ci, ix, 4.0, 1.4, F, double_index=True, label_spacing=60, tanaka=tanaka)
    grid_crosses(dr, mf, 10)
    dr.finish()

    fx0, fy0, fx1, fy1 = mf.fx0, mf.fy0, mf.fx1, mf.fy1
    dr.add([[(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1), (fx0, fy0)]])
    gl, glab = graticule_ticks(mf, 5, 10, 1.2)
    dr.add(gl)
    for s_, pos, kind in glab:
        if kind == "lon":
            dr.label(s_, pos, fy0 - 3.0, 1.5, F, anchor="middle")
            dr.label(s_, pos, fy1 + 4.6, 1.5, F, anchor="middle")
        else:
            dr.label(s_, fx0 - 3.0, pos + 0.75, 1.5, F, anchor="end")

    rows = [("traverse", "ROVER TRAVERSE"), ("tick", "SOL MARK, EVERY 100 SOLS"),
            ("contour", f"CONTOUR, INTERVAL {ci} M"), ("index", f"INDEX CONTOUR, {ix} M")]
    rows.append(("sample", "SAMPLE SITE, TUBE NUMBERS") if samples else ("ring", "ROVER POSITION ON SOL"))

    def legend(X, Y0, step=3.4):
        for i, (kind, t) in enumerate(rows):
            yy = Y0 + i * step
            xx = X
            if kind == "traverse":
                dr.add([[(xx, yy), (xx + 3, yy - 0.7), (xx + 6, yy)]], "red")
            elif kind == "tick":
                dr.add([[(xx, yy), (xx + 6, yy)], [(xx + 3, yy - 0.9), (xx + 3, yy + 0.9)]], "red")
            elif kind == "contour":
                dr.add([[(xx, yy), (xx + 6, yy)]])
            elif kind == "index":
                dr.add([[(xx, yy), (xx + 6, yy)], [(xx, yy + 0.18), (xx + 6, yy + 0.18)]])
            elif kind == "ring":
                dr.add([circle(xx + 3, yy, 0.8, 16)])
            elif kind == "sample":
                dr.add([circle(xx + 3, yy, 0.55, 10)], "red")
            dr.label(t, xx + 8.5, yy + 0.6, 1.25, F, track=0.2)

    if titleblock:
        bx0, by0, bx1, by1 = tb
        X = bx0 + 3.5
        dr.label("JEZERO CRATER", X, by0 + 9.5, 4.6, FM, track=0.7)
        dr.label("MARS 2020 PERSEVERANCE ROVER  ·  TRAVERSE MAP", X, by0 + 14.2, 1.3, F, track=0.3)
        r1 = by0 + 17.0
        dr.add([[(bx0 + 0.6, r1), (bx1, r1)]])
        legend(X, r1 + 4.0, 3.2)
        sb = r1 + 23.5
        scale_bar(dr, X, sb, mf, 10, 2, 0.9, F, 1.1)
        dr.label(f"1:{scale:,}".replace(",", " "), X + 10000 * mf.k + 3, sb + 1.0, 1.3, F, track=0.3)
        r2 = sb + 6.5
        dr.add([[(bx0 + 0.6, r2), (bx1, r2)]])
        cols = [bx0 + 0.6, bx0 + 28.6, bx0 + 56.6, bx1]
        r5 = by1 - 0.6
        h = (r5 - r2) / 3
        r3, r4 = r2 + h, r2 + 2 * h
        dr.add([[(bx0 + 0.6, r3), (bx1, r3)], [(bx0 + 0.6, r4), (bx1, r4)]])
        for c in cols[1:-1]:
            dr.add([[(c, r2), (c, r5)]])
        cells = [[("SCALE", "1:225 000"), ("SHEET", "1 OF 1"), ("REV", "A")],
                 [("DATA THROUGH", "SOL 2022"), ("DATE", "2026-11-17"), ("DWG NO.", "JZR-0001")],
                 [("DRAWN", "PEN PLOTTER"), ("CHECKED", ""), ("APPROVED", "")]]
        for ri, (top, bot) in enumerate([(r2, r3), (r3, r4), (r4, r5)]):
            for ci_, (lab, val) in enumerate(cells[ri]):
                cx0 = cols[ci_] + 1.2
                dr.label(lab, cx0, top + 2.0, 0.95, F, track=0.25)
                if val:
                    dr.label(val, cx0, bot - 1.3, 1.45, F, track=0.25)
    else:
        X = tb[0] + 5
        dr.label("MARS  ·  SYRTIS MAJOR  MC-13", X, 21, 1.5, F, track=0.45)
        dr.label("JEZERO CRATER", X, 30, 4.4, FM, track=0.7)
        dr.label("PERSEVERANCE ROVER TRAVERSE", X, 35.5, 1.6, F, track=0.4)
        dr.label("LANDING THROUGH SOL 2022", X, 39, 1.4, F, track=0.4)
        legend(X, 45)
        scale_bar(dr, X, 66, mf, 10, 2, 1.0, F, 1.2)
        dr.label(f"SCALE 1:{scale:,}".replace(",", " "), X + 10000 * mf.k + 3, 65.2, 1.4, F, track=0.3)

    if profile:
        draw_profile(dr, raw_lines, wps, *pb, F, samples=samples)
    if samples:
        kx0, ky0, kx1, ky1 = kb
        dr.label("SAMPLE TUBES", kx0 + 3, ky0 + 4.2, 1.5, F, track=0.35)
        dr.label("NO.  NAME  SOL", kx0 + 3, ky0 + 7.4, 0.95, F, track=0.25)
        half = (len(samples) + 1) // 2
        colw = (kx1 - kx0 - 5) / 2
        step = (ky1 - ky0 - 13) / half
        for i, (n, name, sol, kind, loc) in enumerate(samples):
            cxx = kx0 + 3 + (i // half) * colw
            yy = ky0 + 11.2 + (i % half) * step
            dr.label(f"{n}", cxx + 3, yy, 1.05, F, anchor="end")
            nm = name.upper() + (" (A)" if kind == "Atmospheric" else " (R)" if kind == "Regolith" else "")
            dr.label(nm, cxx + 4.5, yy, 1.05, F, track=0.12)
            dr.label(f"{sol}", cxx + colw - 2, yy, 1.05, F, anchor="end")
    note = ("ELEVATIONS IN METERS, MARS 2000 AREOID.  TOPOGRAPHY: CTX DEM 20 M/PX, USGS.  "
            "TRAVERSE: NASA/JPL-CALTECH.  EQUIRECTANGULAR, MARS 2000 SPHERE.")
    if samples:
        note += "  SAMPLES 1-28: NASA (A ATMOSPHERIC, R REGOLITH)."
    dr.label(note, fx0, fy1 + 9.5, 1.2, F, track=0.2)
    return dr


def design_ridgeline(dem, rows=64, exag=0.030, scale=240000, center=(77.66, 18.36)):
    """Stacked east-west elevation profiles with hidden lines removed. Stylised, one pen,
    name only. exag is page mm per metre of height. Rows are spread evenly down the frame,
    with the back row pushed down just far enough that its peaks clear the top edge."""
    fr = (20.0, 14.0, 277.0, 182.0)
    x0, y0, x1, y1 = fr
    mf = MapFrame(fr, scale, center)
    ter = Terrain(dem, mf, 80.0, 160.0, pad_mm=2)
    dr = Drawing(mf)
    xs = np.linspace(x0, x1, 1200)
    zref = np.nanpercentile(ter.z, 2)
    lift = np.array([(ter.sample_mm(ter.z, np.column_stack([xs, np.full_like(xs, m)])) - zref) * exag
                     for m in np.linspace(y0, y1, rows)])
    u = np.clip(np.minimum(xs - x0, x1 - xs) / ((x1 - x0) * 0.10), 0, 1)
    lift = np.maximum(lift, 0) * (0.5 - 0.5 * np.cos(np.pi * u))
    frac = np.linspace(0, 1, rows)
    peak = lift.max(axis=1)
    top = max([y0] + [(y0 + peak[i] - frac[i] * y1) / (1 - frac[i]) for i in range(rows - 1)])
    prof = (top + frac * (y1 - top))[:, None] - lift
    horizon = np.full_like(xs, np.inf)
    for i in range(rows - 1, -1, -1):  # front row first, keep the running horizon
        yv = prof[i]
        vis = yv <= horizon + 1e-6
        seg = []
        for k in range(len(xs)):
            if vis[k]:
                seg.append((float(xs[k]), float(yv[k])))
            else:
                if len(seg) > 1:
                    dr.add([list(LineString(seg).simplify(0.03).coords)])
                seg = []
        if len(seg) > 1:
            dr.add([list(LineString(seg).simplify(0.03).coords)])
        horizon = np.minimum(horizon, yv)
    dr.label("J E Z E R O", (x0 + x1) / 2, 198, 7.5, "futuram", anchor="middle", track=3.0)
    return dr


def design_clean(dem, lines, wps, scale=225000, center=(77.66, 18.35), tanaka=0.0):
    """Whole sheet is map: contours with their heights and the traverse, nothing else inside
    the neat line. Name, scale and sources sit in one line under the frame."""
    mf = MapFrame((14, 14, 283, 190), scale, center)
    ter = Terrain(dem, mf, 80.0, 220.0)
    dr = Drawing(mf)
    F = "futural"
    draw_traverse(dr, lines, wps, 10 ** 6, 10 ** 6, 0.9, 1.6, F)
    contour_set(dr, ter, 50, 250, 4.0, 1.4, F, double_index=True, label_spacing=60, tanaka=tanaka)
    dr.finish()
    fx0, fy0, fx1, fy1 = mf.fx0, mf.fy0, mf.fx1, mf.fy1
    dr.add([[(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1), (fx0, fy0)]])
    gl, glab = graticule_ticks(mf, 5, 10, 1.2)
    dr.add(gl)
    for s_, pos, kind in glab:
        if kind == "lon":
            dr.label(s_, pos, fy0 - 3.0, 1.4, F, anchor="middle")
        else:
            dr.label(s_, fx0 - 3.0, pos + 0.7, 1.4, F, anchor="end")
    t = "JEZERO CRATER, MARS"
    dr.label(t, fx0, fy1 + 8.5, 2.6, "futuram", track=0.9)
    dr.label("PERSEVERANCE TRAVERSE, LANDING TO SOL 2022  ·  CONTOURS 50 M  ·  1:225 000",
             fx0 + text_width(t, 2.6, "futuram", 0.9) + 8, fy1 + 8.5, 1.3, F, track=0.3)
    scale_bar(dr, fx1 - 10000 * mf.k, fy1 + 7.0, mf, 10, 2, 0.8, F, 1.0, units="KM")
    return dr


# Section along the drive, east to west: rover positions on these sols, one per area,
# so it follows the route without its back-and-forth (and has no time axis).
SECTION_SOLS = [13, 298, 575, 749, 913, 1088, 1215, 1401, 1700, 1900]


def design_detail(dem, lines, wps, samples, scale=90000, center=(77.343, 18.466)):
    """Portrait sheet, about 2.5x the full-crater map: western fan and rim with every sample
    site, a terrain section along the route, and the tube key."""
    page = (PAGE_H, PAGE_W)
    mf = MapFrame((18, 18, 192, 172), scale, center)
    ter = Terrain(dem, mf, 40.0, 100.0)
    dr = Drawing(mf, page)
    F, FM = "futural", "futuram"
    fx0, fy0, fx1, fy1 = mf.fx0, mf.fy0, mf.fx1, mf.fy1

    draw_traverse(dr, lines, wps, 10 ** 6, 10 ** 6, 0.9, 1.6, F, knock=0.6)
    L = {k: at_sol(wps, s) for k, s in LANDMARKS.items() if s}
    landmark(dr, ["OCTAVIA E. BUTLER", "LANDING SITE"], L["landing"][1], L["landing"][2], 10, -21, 1.5, F, "flag",
             anchor="end")
    landmark(dr, ["THREE FORKS DEPOT"], L["three_forks"][1], L["three_forks"][2], -2, 10, 1.5, F, "ring")
    landmark(dr, ["CHEYAVA FALLS"], L["cheyava"][1], L["cheyava"][2], 2, -10, 1.5, F, "ring")
    landmark(dr, ["WITCH HAZEL HILL"], L["witch_hazel"][1], L["witch_hazel"][2], 3, 11, 1.5, F, "ring")
    landmark(dr, ["SOL 2022"], wps[-1][1], wps[-1][2], 4, 9, 1.5, F, "triangle", layer="red")
    sample_markers(dr, samples, wps, 1.2, 1.25, F, rings=(3.5, 5.0, 7.0, 9.5))

    # section ends on the map
    sec_ll = [at_sol(wps, s)[1:3] for s in SECTION_SOLS]
    sec_mm = [tuple(float(v) for v in mf.ll_mm(lo, la)) for lo, la in sec_ll]
    for (x, y), t, dx, dy in ((sec_mm[0], "A", -3.0, -2.5), (sec_mm[-1], "A'", -3.0, 1.0)):
        dr.add([circle(x, y, 0.35, 8)])
        dr.label(t, x + dx, y + dy, 2.0, FM, anchor="start" if dx > 0 else "end")

    for nm, lon, lat, cap, ang in (("Séítah", 77.432, 18.416, 2.2, 0), ("Western fan", 77.385, 18.50, 2.6, -8),
                                   ("Belva", 77.372, 18.474, 1.7, 0)):
        x, y = mf.ll_mm(lon, lat)
        dr.label(nm, float(x), float(y), cap, "timesi", anchor="middle", track=0.6, angle=ang)
    x, y = mf.ll_mm(77.232, 18.488)
    dr.label("JEZERO RIM", float(x), float(y), 2.0, F, anchor="middle", track=1.4, angle=-62)

    for sx, sy, z in spot_heights(ter, mf, 4, 40, 3000):
        if any(k.intersects(box(sx - 1, sy - 2, sx + 10, sy + 1.5)) for k in dr.keepout):
            continue
        dr.add([circle(sx, sy, 0.3, 8)])
        dr.label(f"{int(round(z))}".replace("-", "−"), sx + 1.0, sy + 0.8, 1.25, F)
        dr.keepout.append(Point(sx, sy).buffer(0.8))
    contour_set(dr, ter, 20, 100, 3.0, 1.3, F, double_index=True, label_spacing=50)
    grid_crosses(dr, mf, 5)
    dr.finish()

    dr.add([[(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1), (fx0, fy0)]])
    gl, glab = graticule_ticks(mf, 1, 5, 1.2)
    dr.add(gl)
    for s_, pos, kind in glab:
        if kind == "lon":
            dr.label(s_, pos, fy0 - 3.0, 1.4, F, anchor="middle")
        else:
            dr.label(s_, fx0 - 3.0, pos, 1.4, F, anchor="middle", angle=-90)

    # --- terrain section along the route
    path = LineString(chaikin(sec_mm, 3))
    n = 600
    pts = np.array([path.interpolate(t, normalized=True).coords[0] for t in np.linspace(0, 1, n)])
    z = ter.sample_mm(ter.z, pts)
    km = np.linspace(0, path.length / mf.k / 1000, n)
    sx0, sy0, sx1, sy1 = fx0, 180.0, fx1, 232.0
    px0, px1, py0, py1 = sx0 + 10, sx1 - 2, sy0 + 9, sy1 - 7
    zmin, zmax = math.floor(z.min() / 100) * 100, math.ceil(z.max() / 100) * 100

    def X(v):
        return px0 + v / km[-1] * (px1 - px0)

    def Y(v):
        return py1 - (v - zmin) / (zmax - zmin) * (py1 - py0)

    ve = ((py1 - py0) / (zmax - zmin)) / ((px1 - px0) / (km[-1] * 1000))
    dr.label("A", sx0, sy0 + 3.0, 2.0, FM)
    dr.label("A'", sx1, sy0 + 3.0, 2.0, FM, anchor="end")
    dr.label(f"SECTION ALONG THE ROUTE, EAST TO WEST  ·  VERTICAL EXAGGERATION {ve:.0f}X",
             (sx0 + sx1) / 2, sy0 + 2.6, 1.3, F, anchor="middle", track=0.3)
    dr.add([[(px0, py0), (px0, py1), (px1, py1)]])
    for v in range(int(zmin), int(zmax) + 1, 200):
        yy = Y(v)
        dr.add([[(px0 - 1, yy), (px0, yy)]])
        dr.add([[(px0 + 2.0 * i, yy), (px0 + 2.0 * i + 0.3, yy)] for i in range(1, int((px1 - px0) / 2.0))])
        dr.label(f"{v}".replace("-", "−"), px0 - 1.6, yy + 0.55, 1.1, F, anchor="end")
    for k in range(0, int(km[-1]) + 1, 2):
        xx = X(k)
        dr.add([[(xx, py1), (xx, py1 + 1)]])
        dr.label(str(k), xx, py1 + 3.0, 1.1, F, anchor="middle")
    dr.label("KM", px1, py1 + 5.6, 1.1, F, anchor="end", track=0.3)
    # ground line in black, shaded under with sparse verticals like an old section
    gl_pts = [(X(a), Y(b)) for a, b in zip(km, z)]
    dr.add([list(LineString(gl_pts).simplify(0.05).coords)])
    for a, b in zip(km[::8], z[::8]):
        if Y(b) < py1 - 0.6:
            dr.add([[(X(a), Y(b) + 0.5), (X(a), py1)]])

    def along(lon, lat):
        p = Point(*(float(v) for v in mf.ll_mm(lon, lat)))
        d = path.project(p)
        return d / path.length * km[-1]

    marks = [(L["landing"], "LANDING"), (at_sol(wps, 298), "SÉÍTAH"), (L["three_forks"], "DELTA FRONT"),
             (L["cheyava"], "CHEYAVA FALLS"), (L["witch_hazel"], "WITCH HAZEL HILL")]
    prev = None
    for w, lab in marks:
        k = along(w[1], w[2])
        xx = X(k)
        yy = Y(float(np.interp(k, km, z)))
        top = min(yy - 2.5, py0 + 2.0)
        wl = text_width(lab, 1.05, F, 0.2)
        if prev and xx - wl / 2 < prev[0] + 1.5 and top - 2.4 > sy0 + 3:
            top -= 2.4  # stagger a label that would run into the previous one
        dr.add([[(xx, yy - 0.8), (xx, top)]])
        dr.label(lab, xx, top - 0.7, 1.05, F, anchor="middle", track=0.2, pad=0.2)
        prev = (xx + wl / 2, top)
    # samples projected onto the section as red dots
    for n_, name, sol, kind, loc in samples:
        w = at_sol(wps, sol)
        k = along(w[1], w[2])
        xx = X(k)
        yy = Y(float(np.interp(k, km, z)))
        dr.add([circle(xx, yy, 0.45, 8)], "red")
    # --- sample key
    ky0, ky1 = 240.0, 268.0
    dr.add([[(fx0, ky0 - 2.5), (fx1, ky0 - 2.5)]])
    dr.label("SAMPLE TUBES", fx0, ky0 + 1.5, 1.4, F, track=0.35)
    dr.label("NUMBER, NAME, SOL.  A ATMOSPHERIC, R REGOLITH", fx0 + 26, ky0 + 1.5, 1.0, F, track=0.25)
    cols = 4
    per = math.ceil(len(samples) / cols)
    colw = (fx1 - fx0) / cols
    step = (ky1 - ky0 - 5) / (per - 1)
    for i, (n_, name, sol, kind, loc) in enumerate(samples):
        cx = fx0 + (i // per) * colw
        yy = ky0 + 5.5 + (i % per) * step
        dr.label(f"{n_}", cx + 3, yy, 1.1, F, anchor="end", layer="red")
        nm = name.upper() + (" (A)" if kind == "Atmospheric" else " (R)" if kind == "Regolith" else "")
        dr.label(nm, cx + 4.5, yy, 1.1, F, track=0.12)
        dr.label(f"{sol}", cx + colw - 3, yy, 1.1, F, anchor="end")
    # --- footer
    fy = 279.0
    dr.add([[(fx0, fy - 6.0), (fx1, fy - 6.0)]])
    dr.label("JEZERO CRATER", fx0, fy, 3.4, FM, track=1.0)
    dr.label("WESTERN FAN AND RIM  ·  PERSEVERANCE, LANDING TO SOL 2022  ·  CONTOURS 20 M  ·  1:90 000",
             fx0, fy + 5.0, 1.1, F, track=0.25)
    dr.label("CTX DEM 20 M/PX, USGS  ·  TRAVERSE NASA/JPL-CALTECH  ·  ELEVATIONS M, MARS 2000 AREOID",
             fx0, fy + 8.0, 1.0, F, track=0.2)
    scale_bar(dr, fx1 - 5000 * mf.k, fy - 1.0, mf, 5, 1, 0.8, F, 1.0, units="KM")
    return dr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="../../M20_JezeroCrater_CTXDEM_20m.tif")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", default=None, help="comma list of design ids")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    dem = tifffile.imread(args.dem).astype(np.float64)
    dem[dem < -1e30] = np.nan
    lines, wps = load_traverse(args.data)
    raw = load_traverse_xyz(args.data)
    samples = load_samples(os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples.csv"))
    designs = {
        "a-nasa-crater": lambda: design_nasa(dem, lines, wps, "crater"),
        "b-nasa-full": lambda: design_nasa_full(dem, lines, wps, raw),
        "b1-relief-profile": lambda: design_nasa_full(dem, lines, wps, raw, tanaka=0.32, profile=True),
        "b2-drawing": lambda: design_nasa_full(dem, lines, wps, raw, tanaka=0.32, profile=True, titleblock=True),
        "b3-samples": lambda: design_nasa_full(dem, lines, wps, raw, tanaka=0.32, samples=samples),
        "c-clean": lambda: design_clean(dem, lines, wps),
        "c1-clean-relief": lambda: design_clean(dem, lines, wps, tanaka=0.32),
        "d-detail": lambda: design_detail(dem, lines, wps, samples),
        "e-ridgeline": lambda: design_ridgeline(dem),
        "e1-ridge-96": lambda: design_ridgeline(dem, rows=96),
        "e2-ridge-x2": lambda: design_ridgeline(dem, exag=0.06),
        "e3-ridge-128": lambda: design_ridgeline(dem, rows=128, exag=0.045),
        "e4-ridge-160": lambda: design_ridgeline(dem, rows=160, exag=0.05),
    }
    only = args.only.split(",") if args.only else list(designs)
    summary = {}
    for name in only:
        dr = designs[name]()
        black = order_strokes(dr.layers["black"])
        red = order_strokes(dr.layers["red"], black[-1][-1] if black else (0, 0))
        layers = {"black": black, "red": red}
        colors = {"black": "#1a1a1a", "red": "#c8102e"}
        widths = {"black": 0.25, "red": 0.35}
        write_svg(os.path.join(args.out, f"{name}.svg"), layers, colors, widths, dr.page)
        # the plotter's travel is landscape only: portrait sheets go in turned 90 degrees
        pw, ph = dr.page
        if ph > pw:
            def turn(ls):
                return [[(y, pw - x) for x, y in l] for l in ls]
            pen_page, pb, pr = (ph, pw), turn(black), turn(red)
        else:
            pen_page, pb, pr = dr.page, black, red
        write_svg(os.path.join(args.out, f"{name}-pen1-black.svg"), {"black": pb}, colors, widths, pen_page)
        if red:
            write_svg(os.path.join(args.out, f"{name}-pen2-red.svg"), {"red": pr}, colors, widths, pen_page)
        summary[name] = {"black": plot_stats(black), "red": plot_stats(red)}
        print(name, json.dumps(summary[name]))
    with open(os.path.join(args.out, "stats.json"), "w") as f:
        json.dump(summary, f, indent=1)


if __name__ == "__main__":
    main()
