#!/usr/bin/env python3
"""Shading finder for technical drawings: which wide ink areas are fills and which are thick lines.

Separate from the line tracer (trace_centerline.py) so a user interface can re-decide live: the slow
part (analyze: thinning, the stroke model) runs once per image and threshold; deciding (classify)
from the measured features is instant, and apply turns the decisions into the ink the tracer gets
plus the filled areas.

  analyze   ink, its skeleton, and every ink area wider than about two typical lines (a candidate)
            with its features: stroke coverage, elongation, width, grey histogram, outline
  classify  one candidate -> fill or line, from the thresholds and any hand override
  apply     the decided fills -> line ink without them (but with the lines drawn on a grey fill),
            and the fills as (area mask, grey level)

A candidate stays a line if it is long for its width (skeleton length >= aspect x median drawn
width) and the strokes our pens would draw along its skeleton (no wider than the widest pen) cover
at least `cover` of it. Otherwise (a blob, a shaded patch, ink wider than any pen) it is a fill.

Needs numpy, scipy and Pillow.
"""
import math
from collections import defaultdict

import numpy as np
from scipy import ndimage as ndi

HIST_STEP = 4                  # grey histogram bin width per candidate (64 bins)
DEFAULTS = {"cover": 0.6, "aspect": 4.0, "tone_min": 40, "min_area": 400, "line_frac": 0.6}


def _tc():
    import trace_centerline as tc     # the tracer's skeleton and graph code; imported late (it imports us)
    return tc


def disk(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def despeckle(ink, min_area):
    lab, n = ndi.label(ink, structure=np.ones((3, 3)))
    if n:
        area = ndi.sum(ink, lab, index=np.arange(1, n + 1))
        keep = np.zeros(n + 1, bool)
        keep[1:] = area >= min_area
        ink = keep[lab]
    return ink


def drop_small(mask, min_area):
    lab, n = ndi.label(mask, structure=np.ones((3, 3)))
    if not n:
        return mask, lab, 0
    area = ndi.sum(mask, lab, index=np.arange(1, n + 1))
    keep = np.zeros(n + 1, bool)
    keep[1:] = area >= min_area
    mask = keep[lab]
    lab, n = ndi.label(mask, structure=np.ones((3, 3)))
    return mask, lab, n


def stage(progress, a, b):
    """A progress callback for a sub-range [a, b] of the caller's."""
    if progress is None:
        return None
    return lambda name, f: progress(name, a + (b - a) * max(0.0, min(1.0, f)))


def stroke_model(sk, dist, spur, max_w):
    """The area our lines would cover on paper: every skeleton edge no wider than max_w px
    (median drawn width) drawn as a pen stroke following its local width, capped at max_w.
    -> boolean mask."""
    tc = _tc()
    nodes, edges = tc.skeleton_graph(sk)
    edges = tc.merge_degree2(tc.prune(nodes, edges, spur))
    widths = tc.edge_widths(edges, dist)
    seeds = np.zeros(sk.shape, bool)
    for (_, _, p), w in zip(edges, widths):
        if w <= max_w:
            p = np.asarray(p, int)
            seeds[p[:, 0], p[:, 1]] = True
    if not seeds.any():
        return seeds
    radius = np.minimum(dist, max_w / 2.0)
    d, (iy, ix) = ndi.distance_transform_edt(~seeds, return_indices=True)
    return d <= radius[iy, ix] + 0.5


def rings_of(mask, eps=1.0):
    """Closed outlines (outer and holes) of a boolean mask along pixel edges, as (y, x)
    point arrays in pixel-corner coordinates, simplified with tolerance eps."""
    rdp = _tc().rdp
    m = np.pad(mask, 1)
    H, W = m.shape
    c = m[1:-1, 1:-1]
    ys, xs = np.nonzero(c & ~m[:-2, 1:-1])            # top sides, run left to right
    segs = [(ys, xs, ys, xs + 1)]
    ys, xs = np.nonzero(c & ~m[1:-1, 2:])             # right sides, down
    segs.append((ys, xs + 1, ys + 1, xs + 1))
    ys, xs = np.nonzero(c & ~m[2:, 1:-1])             # bottom sides, right to left
    segs.append((ys + 1, xs + 1, ys + 1, xs))
    ys, xs = np.nonzero(c & ~m[1:-1, :-2])            # left sides, up
    segs.append((ys + 1, xs, ys, xs))
    nxt = defaultdict(list)
    for y0, x0, y1, x1 in segs:
        for a, b in zip((y0 * W + x0).tolist(), (y1 * W + x1).tolist()):
            nxt[a].append(b)
    rings = []
    for start in list(nxt):
        while nxt[start]:
            ring, v = [start], nxt[start].pop()
            while v != start:
                ring.append(v)
                v = nxt[v].pop()
            ring.append(start)
            p = np.array([(k // W, k % W) for k in ring], float)
            # keep corners only, then simplify the two halves of the loop
            d = np.diff(p, axis=0)
            turn = np.ones(len(p), bool)
            turn[1:-1] = np.any(d[1:] != d[:-1], axis=1)
            p = p[turn]
            if len(p) < 4:
                continue
            h = len(p) // 2
            q = np.vstack([rdp(p[:h + 1], eps)[:-1], rdp(p[h:], eps)])
            if len(q) >= 4:
                rings.append(q)
    return rings


# ------------------------------------------------------------------ analyze
class Analysis:
    """Everything the slow pass measured. Arrays stay here (for apply and the tracer); public()
    is the part a user interface needs to re-decide."""

    def __init__(self, g, ink, dist, sk, width, w0, lab, cands, threshold, max_w):
        self.g, self.ink, self.dist, self.sk = g, ink, dist, sk
        self.width, self.w0, self.lab, self.cands = width, w0, lab, cands
        self.threshold, self.max_w = threshold, max_w

    def public(self, outline_eps=None):
        """-> JSON-able dict: image size, typical width, every candidate's features and outline
        (image px, x then y, simplified for display)."""
        H, W = self.g.shape
        eps = outline_eps or max(1.0, max(H, W) / 1500.0)
        out = []
        for c in self.cands:
            y0, x0, y1, x1 = c["bbox"]
            m = self.lab[y0:y1, x0:x1] == c["id"]
            rings = [[[round(float(x) + x0, 1), round(float(y) + y0, 1)] for y, x in r[:-1]]
                     for r in rings_of(m, eps)]
            out.append(dict(c, rings=rings))
        return {"size": [W, H], "threshold": self.threshold, "max_w": round(self.max_w, 2),
                "width": round(self.width, 2), "hist_step": HIST_STEP, "cands": out}


def analyze(g, threshold=170, max_w=8.0, min_len=12, floor_area=100, progress=None):
    """g: grey image (uint8 array). max_w: widest pen, px. min_len: ink specks smaller than this
    (px) are dropped. floor_area: smallest candidate measured at all (the user's minimum area
    can only be raised above it). progress(stage, fraction) may raise to cancel. -> Analysis."""
    tc = _tc()
    rep = progress or (lambda name, f: None)
    rep("Threshold", 0.0)
    ink = despeckle(g < threshold, min_len)
    dist = ndi.distance_transform_edt(ink)
    sk = tc.thin(ink, progress=stage(progress, 0.02, 0.6), max_dist=float(dist.max()) if ink.any() else 0)
    vals = dist[sk]
    width = float(np.median(vals) * 2) if len(vals) else 3.0
    w0 = float(np.percentile(vals, 25) * 2) if len(vals) else 3.0     # lines are the thin end
    spur = 2.0 * w0
    rep("Finding wide ink", 0.6)
    r = max(2, int(math.ceil(w0)))
    wide = ndi.binary_opening(ink, disk(r))           # wider than about two typical lines
    wide, lab, n = drop_small(wide, floor_area)
    cands = []
    if n:
        H, W = ink.shape
        pad = int(math.ceil(max_w + spur)) + 2
        model = np.zeros(ink.shape, bool)
        slices = ndi.find_objects(lab)
        for i, sl in enumerate(slices):
            rep("Comparing with the strokes", 0.65 + 0.3 * i / len(slices))
            b = (slice(max(sl[0].start - pad, 0), min(sl[0].stop + pad, H)),
                 slice(max(sl[1].start - pad, 0), min(sl[1].stop + pad, W)))
            model[b] |= stroke_model(sk[b], dist[b], spur, max_w)
        idx = np.arange(1, n + 1)
        cov = np.asarray(ndi.mean(model, lab, index=idx))
        skl = np.where(sk, lab, 0)
        length = np.asarray(ndi.sum(sk, skl, index=idx))
        wmed = np.nan_to_num(2.0 * np.asarray(ndi.median(dist, skl, index=idx), float), nan=1.0)
        area = np.asarray(ndi.sum(wide, lab, index=idx))
        cy, cx = np.asarray(ndi.center_of_mass(wide, lab, idx)).T
        for i, sl in enumerate(slices):
            m = lab[sl] == i + 1
            hist = np.bincount((g[sl][m] // HIST_STEP).ravel(), minlength=256 // HIST_STEP)
            cands.append({"id": i + 1, "bbox": [sl[0].start, sl[1].start, sl[0].stop, sl[1].stop],
                          "area": int(area[i]), "cover": round(float(cov[i]), 4),
                          "elong": round(float(length[i] / max(wmed[i], 1.0)), 2),
                          "width": round(float(wmed[i]), 2), "hist": hist.tolist(),
                          "centre": [round(float(cx[i]), 1), round(float(cy[i]), 1)]})
    rep("Measured", 1.0)
    return Analysis(g, ink, dist, sk, width, w0, lab, cands, threshold, max_w)


# ------------------------------------------------------------------ classify
def tone_fraction(cand, tone_min):
    """Share of the candidate's pixels at least tone_min light (a grey shade, not black ink)."""
    h = cand["hist"]
    k = int(math.ceil(tone_min / HIST_STEP))
    tot = sum(h)
    return sum(h[k:]) / tot if tot else 0.0


def classify(cand, cover=0.6, aspect=4.0, min_area=400, override=None, **_):
    """-> "fill" or "line". override ("fill"/"line") wins; areas under min_area stay lines."""
    if override in ("fill", "line"):
        return override
    if cand["area"] < min_area:
        return "line"
    return "fill" if cand["cover"] < cover or cand["elong"] < aspect else "line"


def parse_overrides(text):
    """'fill:x,y;line:x,y' (image px) -> [(kind, x, y)]. Bad entries are skipped."""
    out = []
    for part in (text or "").split(";"):
        kind, _, xy = part.strip().partition(":")
        try:
            x, y = (float(v) for v in xy.split(","))
        except ValueError:
            continue
        if kind in ("fill", "line"):
            out.append((kind, x, y))
    return out


def overrides_by_id(an, overrides):
    """Hand overrides at points -> {candidate id: kind}. A point picks the candidate under it, or
    the nearest one within 4 px (an outline is easy to miss by a pixel)."""
    H, W = an.lab.shape
    out = {}
    for kind, x, y in overrides:
        xi, yi = int(x), int(y)
        if not (0 <= xi < W and 0 <= yi < H):
            continue
        cid = int(an.lab[yi, xi])
        if not cid:
            y0, y1, x0, x1 = max(yi - 4, 0), min(yi + 5, H), max(xi - 4, 0), min(xi + 5, W)
            win = an.lab[y0:y1, x0:x1]
            if win.any():
                ys, xs = np.nonzero(win)
                k = int(np.argmin((ys + y0 - yi) ** 2 + (xs + x0 - xi) ** 2))
                cid = int(win[ys[k], xs[k]])
        if cid:
            out[cid] = kind
    return out


def decide(an, params, overrides=()):
    """-> {candidate id: "fill" | "line"} for every candidate."""
    p = dict(DEFAULTS, **{k: v for k, v in params.items() if v is not None})
    ov = overrides_by_id(an, overrides)
    return {c["id"]: classify(c, override=ov.get(c["id"]), **p) for c in an.cands}


# ------------------------------------------------------------------ apply
def apply(an, decisions, tone_min=40, line_frac=0.6):
    """Decisions -> (ink for the tracer, [(slices, area mask, grey level)]). In a grey fill (most
    pixels at least tone_min light) the lines drawn on the shade stay: inside it, ink is only what
    is darker than line_frac x its grey level. A black fill leaves no line ink."""
    g, lab = an.g, an.lab
    ink = an.ink.copy()
    H, W = ink.shape
    fills = []
    for c in an.cands:
        if decisions.get(c["id"]) != "fill":
            continue
        y0, x0, y1, x1 = c["bbox"]
        # one pixel of margin so the area's anti-aliased rim goes with it
        sl = (slice(max(y0 - 1, 0), min(y1 + 1, H)), slice(max(x0 - 1, 0), min(x1 + 1, W)))
        m = lab[sl] == c["id"]
        mm = ndi.binary_dilation(m)
        gg = g[sl]
        tone = m & (gg >= tone_min)
        if tone.sum() >= 0.5 * m.sum():
            level = float(np.median(gg[tone]))
            ink[sl][mm] = gg[mm] < level * line_frac       # shaded: keep the lines on it
        else:
            level = float(np.median(gg[m]))
            ink[sl][mm] = False                            # solid
        fills.append((sl, m, level))
    return ink, fills
