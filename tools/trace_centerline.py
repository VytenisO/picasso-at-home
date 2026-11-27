#!/usr/bin/env python3
"""Technical line drawing (raster) -> single-stroke centreline SVG for the plotter.

Built for clean scanned or rendered line art: HAER measured drawings, manual
figures, three-views. Every drawn line becomes one pen stroke along its middle
(not two outline edges), traced at full resolution.

Pipeline:
  1. threshold, drop specks
  2. thin to a one-pixel skeleton (Zhang-Suen, vectorised)
  3. skeleton -> graph: end points, junction clusters, edges between them
  4. prune short spurs that thinning leaves at corners and line ends
  5. snap junctions: edges stop short of each junction and meet at its centre,
     so T, X and L joints are single points instead of skeleton Y-shapes
  6. simplify each edge (Ramer-Douglas-Peucker): straight lines come out as one
     segment, curves as fine polylines
  7. join edges through junctions where they continue straight, so a line that
     is crossed by another stays one stroke
  8. order strokes to cut pen-up travel, write SVG in mm

Shading pass (before tracing): every ink area wider than about two typical lines is
compared with the strokes the tracer would draw through it (pen strokes along the
skeleton, no wider than --max-pen). If it is long for its width and those strokes cover
most of it, it is a thick line and stays one (the weight classes give it a heavier pen).
If not, it is shading: it is written as a
filled closed path in a "fills" layer, grey level as the fill colour, for the plotter's
hatch fill, and in a grey area the lines drawn on the shade are traced with a threshold
below its grey level. --no-fills turns this off.

Needs numpy, scipy and Pillow.

Usage:
    python tools/trace_centerline.py drawing.png --out drawing.svg --width-mm 250
"""
import argparse
import math
import pathlib
import sys
from collections import defaultdict

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from find_fills import despeckle, rings_of, stage  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

# 8-neighbourhood in clockwise order starting north: (dy, dx)
NB = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]


# ------------------------------------------------------------------ raster
def load_grey(path, crop=None):
    im = Image.open(path).convert("L")
    if crop:
        im = im.crop(crop)
    return np.asarray(im)


def load_ink(path, threshold, min_area, crop=None):
    return despeckle(load_grey(path, crop) < threshold, min_area)


def thin(ink, progress=None, max_dist=0.0):
    """Zhang-Suen thinning on a boolean array. Each pass only looks at the box around the
    pixels the previous pass removed: thin lines are done after a few passes, and wide ink
    areas (one pass per pixel of half-width) then no longer cost a pass over the whole image.
    progress(stage, fraction), with max_dist (the ink's largest distance to paper) as the
    expected number of passes."""
    full = np.pad(ink.astype(np.uint8), 1)
    H, W = full.shape
    y0, y1, x0, x1 = 0, H, 0, W
    passes = 0
    while True:
        if progress:
            progress("Thinning", min(passes / max(max_dist, 1.0), 0.99))
        passes += 1
        img = full[y0:y1, x0:x1]                      # a view: changes land in full
        hit = []
        for step in (0, 1):
            P = [img[1 + dy:img.shape[0] - 1 + dy, 1 + dx:img.shape[1] - 1 + dx] for dy, dx in NB]
            p2, p3, p4, p5, p6, p7, p8, p9 = P
            c = img[1:-1, 1:-1]
            B = sum(p.astype(np.int16) for p in P)
            seq = P + [P[0]]
            A = sum(((seq[i] == 0) & (seq[i + 1] == 1)).astype(np.int16) for i in range(8))
            if step == 0:
                m = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                m = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            rm = (c == 1) & (B >= 2) & (B <= 6) & (A == 1) & m
            if rm.any():
                c[rm] = 0
                hit.append(np.nonzero(rm))
        if not hit:
            return destair(full[1:-1, 1:-1].astype(bool))
        ys = np.concatenate([h[0] for h in hit]) + y0 + 1
        xs = np.concatenate([h[1] for h in hit]) + x0 + 1
        # next pass: removed pixels' box plus two for the neighbourhoods
        y0, y1 = max(int(ys.min()) - 2, 0), min(int(ys.max()) + 3, H)
        x0, x1 = max(int(xs.min()) - 2, 0), min(int(xs.max()) + 3, W)


def _redundant_lut():
    """For each 8-bit neighbour pattern: True when the pixel can go without changing the
    picture's topology, i.e. it has two or more neighbours and they stay one 8-connected
    group among themselves without it (staircase corners, blob pixels)."""
    lut = np.zeros(256, bool)
    for code in range(256):
        on = [i for i in range(8) if code >> i & 1]
        if len(on) < 2:
            continue
        pos = {i: NB[i] for i in on}
        seen, stack = {on[0]}, [on[0]]
        while stack:
            i = stack.pop()
            for j in on:
                if j not in seen and max(abs(pos[i][0] - pos[j][0]), abs(pos[i][1] - pos[j][1])) == 1:
                    seen.add(j)
                    stack.append(j)
        lut[code] = len(seen) == len(on)
    return lut


_LUT = _redundant_lut()


def _codes(sk):
    p = np.pad(sk, 1)
    H, W = sk.shape
    code = np.zeros(sk.shape, np.int32)
    for i, (dy, dx) in enumerate(NB):
        code |= p[1 + dy:H + 1 + dy, 1 + dx:W + 1 + dx].astype(np.int32) << i
    return code


def destair(sk):
    """Remove redundant skeleton pixels. Thinning leaves staircase corners on slanted lines
    and curves, which otherwise read as branch points. Candidates are found in bulk, then
    removed one at a time with a fresh check, so a line can never be cut."""
    sk = sk.copy()
    H, W = sk.shape
    ys, xs = np.nonzero(sk & _LUT[_codes(sk)])
    for y, x in zip(ys, xs):
        code = 0
        for i, (dy, dx) in enumerate(NB):
            yy, xx = y + dy, x + dx
            if 0 <= yy < H and 0 <= xx < W and sk[yy, xx]:
                code |= 1 << i
        if _LUT[code]:
            sk[y, x] = False
    return sk


# ------------------------------------------------------------------ graph
def skeleton_graph(sk):
    """-> nodes {id: (y, x) centre}, edges [(n0, n1, [(y, x), ...])].
    End points have one neighbour; junction pixels have three or more branches
    (counted as 0->1 transitions around the pixel, which ignores staircase steps)."""
    p = np.pad(sk, 1)
    P = [p[1 + dy:p.shape[0] - 1 + dy, 1 + dx:p.shape[1] - 1 + dx].astype(np.int8) for dy, dx in NB]
    count = sum(P)
    seq = P + [P[0]]
    trans = sum(((seq[i] == 0) & (seq[i + 1] == 1)).astype(np.int8) for i in range(8))
    node_px = sk & ((count == 1) | (trans >= 3) | (count >= 4))
    lab, n = ndi.label(node_px, structure=np.ones((3, 3)))
    nodes = {}
    if n:
        cents = ndi.center_of_mass(node_px, lab, range(1, n + 1))
        nodes = {i + 1: c for i, c in enumerate(cents)}
    H, W = sk.shape
    visited = np.zeros_like(sk)
    edges = []

    def nbrs(y, x):
        for dy, dx in NB:
            yy, xx = y + dy, x + dx
            if 0 <= yy < H and 0 <= xx < W and sk[yy, xx]:
                yield yy, xx

    def walk(start_node, y, x, path):
        # follow non-node pixels until a node pixel is reached
        while True:
            visited[y, x] = True
            path.append((y, x))
            nxt = None
            for yy, xx in nbrs(y, x):
                # path[0] is the start node pixel; do not end on the node we just left
                if lab[yy, xx] and (lab[yy, xx] != start_node or len(path) > 3):
                    return lab[yy, xx], path + [(yy, xx)]
                if not lab[yy, xx] and not visited[yy, xx]:
                    # prefer 4-neighbours so diagonal shortcuts do not skip pixels
                    if nxt is None or (yy == y or xx == x):
                        nxt = (yy, xx)
            if nxt is None:
                return None, path
            y, x = nxt

    ys, xs = np.nonzero(lab)
    for y, x in zip(ys, xs):
        nid = lab[y, x]
        for yy, xx in nbrs(y, x):
            if not lab[yy, xx] and not visited[yy, xx]:
                end, path = walk(nid, yy, xx, [(y, x)])
                if end is None:
                    continue
                edges.append((nid, end, path))
            elif lab[yy, xx] and lab[yy, xx] != nid and nid < lab[yy, xx]:
                # two node clusters touching directly
                edges.append((nid, lab[yy, xx], [(y, x), (yy, xx)]))
    # closed loops without any node
    ys, xs = np.nonzero(sk & ~visited & (lab == 0))
    for y, x in zip(ys, xs):
        if visited[y, x]:
            continue
        nid = max(nodes, default=0) + 1
        nodes[nid] = (float(y), float(x))
        _, path = walk(-1, y, x, [])
        edges.append((nid, nid, path + [path[0]]))
    # dedupe edges found from both ends
    seen, out = set(), []
    for a, b, path in edges:
        key = (min(a, b), max(a, b), min(path[0], path[-1]), max(path[0], path[-1]), len(path))
        if key in seen:
            continue
        seen.add(key)
        out.append((a, b, path))
    return nodes, out


def prune(nodes, edges, spur):
    """Repeatedly drop edges shorter than spur that end in a free end point."""
    while True:
        deg = defaultdict(int)
        for a, b, _ in edges:
            deg[a] += 1
            deg[b] += 1
        keep = []
        dropped = False
        for a, b, path in edges:
            short = len(path) < spur
            if short and a != b and (deg[a] == 1) != (deg[b] == 1):
                dropped = True
                continue
            keep.append((a, b, path))
        edges = keep
        if not dropped:
            return edges


def merge_degree2(edges):
    """Join edges meeting at nodes of degree two into one chain."""
    adj = defaultdict(list)
    for i, (a, b, _) in enumerate(edges):
        adj[a].append(i)
        adj[b].append(i)
    used = [False] * len(edges)
    out = []
    for i, (a, b, path) in enumerate(edges):
        if used[i]:
            continue
        used[i] = True
        chain, s, e = list(path), a, b
        for end in ("e", "s"):
            while True:
                n = e if end == "e" else s
                if len(adj[n]) != 2 or s == e:
                    break
                j = next((k for k in adj[n] if not used[k]), None)
                if j is None:
                    break
                used[j] = True
                ja, jb, jp = edges[j]
                seg = list(jp) if ja == n else list(reversed(jp))
                other = jb if ja == n else ja
                if end == "e":
                    chain = chain + seg[1:]
                    e = other
                else:
                    chain = list(reversed(seg))[:-1] + chain
                    s = other
        out.append((s, e, chain))
    return out


# ------------------------------------------------------------------ geometry
def rdp(pts, eps):
    pts = np.asarray(pts, float)
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        d = b - a
        L = math.hypot(*d)
        seg = pts[i + 1:j] - a
        if L < 1e-9:
            dist = np.hypot(seg[:, 0], seg[:, 1])
        else:
            dist = np.abs(seg[:, 0] * d[1] - seg[:, 1] * d[0]) / L
        k = int(np.argmax(dist))
        if dist[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return pts[keep]


def place_junctions(nodes, edges, trim):
    """Move each junction to where its edges' lines meet. Thinning bends every edge towards
    the skeleton's Y-shaped junction, and the junction pixels' centre sits off the lines,
    pulled towards the side branch, so a line crossed by a T would come out kinked. Each edge
    is fitted with a line over the stretch just past the distorted zone (trim .. 3 x trim + 4
    px from the junction, at most half the edge), then the point closest to all of those
    lines (least squares) is the junction. Kept where that is ill-posed or far off."""
    ends = defaultdict(list)
    for a, b, path in edges:
        p = np.asarray(path, float)
        if a != b:
            ends[a].append(p)
            ends[b].append(p[::-1])
    out = dict(nodes)
    for n, lst in ends.items():
        if len(lst) < 3:
            continue
        c = np.asarray(nodes[n], float)
        A = np.zeros((2, 2))
        rhs = np.zeros(2)
        for p in lst:
            d = np.hypot(*(p - c).T)
            sel = p[(d > trim) & (d <= 3 * trim + 4) & (np.arange(len(p)) <= len(p) // 2)]
            if len(sel) < 3:
                continue
            m = sel.mean(axis=0)
            u, s, vt = np.linalg.svd(sel - m, full_matrices=False)
            if s[0] < 1e-9 or (len(s) > 1 and s[1] > 0.5 * s[0]):
                continue                    # no clear direction
            v = vt[0]
            P = np.eye(2) - np.outer(v, v)  # projector onto the line's normal
            w = len(sel)
            A += w * P
            rhs += w * P @ m
        if np.linalg.cond(A) > 50:
            continue                        # lines (nearly) parallel or too few
        x = np.linalg.solve(A, rhs)
        if np.hypot(*(x - c)) <= 2 * trim + 1:
            out[n] = (float(x[0]), float(x[1]))
    return out


def snap_ends(nodes, edges, trim):
    """Cut each edge back by `trim` px at junction ends and end it at the junction centre."""
    deg = defaultdict(int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    out = []
    for a, b, path in edges:
        p = np.asarray(path, float)
        if len(p) > 2 * trim + 2:
            if deg[a] >= 3:
                c = np.asarray(nodes[a])
                d = np.hypot(*(p - c).T)
                k = int(np.argmax(d > trim))
                p = np.vstack([c, p[k:]])
            if deg[b] >= 3:
                c = np.asarray(nodes[b])
                d = np.hypot(*(p - c).T)
                k = len(p) - int(np.argmax(d[::-1] > trim))
                p = np.vstack([p[:k], c])
        out.append((a, b, p))
    return out


def chaikin_soft(p, max_turn_deg, it=2):
    """Round only gentle bends (curves drawn as many small steps); keep real corners."""
    for _ in range(it):
        if len(p) < 3:
            return p
        out = [p[0]]
        for i in range(1, len(p) - 1):
            v1, v2 = p[i] - p[i - 1], p[i + 1] - p[i]
            ang = abs(math.degrees(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1 @ v2)))
            if ang < max_turn_deg:
                out += [0.75 * p[i] + 0.25 * p[i - 1], 0.75 * p[i] + 0.25 * p[i + 1]]
            else:
                out.append(p[i])
        out.append(p[-1])
        p = np.asarray(out)
    return p


def join_through(edges, nodes, max_bend_deg):
    """At junctions, join the pair of edges that continue most straight into one stroke.
    Call it per weight class so a thin line never continues into a heavy one."""
    strokes = [list(map(tuple, p)) for _, _, p in edges]
    ends = defaultdict(list)  # node -> [(stroke index, at_start)]
    for i, (a, b, _) in enumerate(edges):
        ends[a].append((i, True))
        ends[b].append((i, False))

    def direction(s, at_start):
        pts = np.asarray(strokes[s])
        if not at_start:
            pts = pts[::-1]
        # direction pointing away from the junction, over the first ~25 px
        acc = np.cumsum(np.r_[0, np.hypot(*np.diff(pts, axis=0).T)])
        k = int(np.searchsorted(acc, 25.0))
        k = min(max(k, 1), len(pts) - 1)
        d = pts[k] - pts[0]
        n = math.hypot(*d)
        return d / n if n else d

    pairs = []
    for n, lst in ends.items():
        if len(lst) < 3:
            continue
        cand = []
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                (si, ai), (sj, aj) = lst[i], lst[j]
                if si == sj:
                    continue
                cosang = -float(direction(si, ai) @ direction(sj, aj))
                bend = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))
                if bend <= max_bend_deg:
                    cand.append((bend, lst[i], lst[j]))
        used = set()
        for bend, ei, ej in sorted(cand):
            if ei in used or ej in used:
                continue
            used |= {ei, ej}
            pairs.append((ei, ej))
    # chain strokes through the chosen pairs
    link = {}
    for ei, ej in pairs:
        link[ei] = ej
        link[ej] = ei
    done = [False] * len(strokes)
    out = []
    for s in range(len(strokes)):
        if done[s]:
            continue
        # walk to one free end of this chain
        cur, at_start = s, True
        seen = set()
        while (cur, at_start) in link and cur not in seen:
            seen.add(cur)
            cur, nxt_start = link[(cur, at_start)]
            at_start = not nxt_start
        chain = []
        seen = set()
        while cur is not None and cur not in seen and not done[cur]:
            seen.add(cur)
            done[cur] = True
            pts = strokes[cur] if at_start else strokes[cur][::-1]
            chain += pts if not chain else pts[1:]
            far = (cur, not at_start)
            if far in link:
                cur, at_start = link[far]
            else:
                cur = None
        if chain:
            out.append(chain)
    return out


def order(strokes):
    """Greedy nearest-end ordering, reversing strokes where that is shorter."""
    if not strokes:
        return []
    starts = np.array([s[0] for s in strokes])
    ends_ = np.array([s[-1] for s in strokes])
    left = set(range(len(strokes)))
    cur = np.array([0.0, 0.0])
    out = []
    while left:
        idx = np.fromiter(left, int)
        ds = np.hypot(*(starts[idx] - cur).T)
        de = np.hypot(*(ends_[idx] - cur).T)
        k = int(np.argmin(np.minimum(ds, de)))
        i = idx[k]
        s = strokes[i] if ds[k] <= de[k] else strokes[i][::-1]
        out.append(s)
        cur = np.asarray(s[-1])
        left.remove(i)
    return out


# ------------------------------------------------------------------ line weights
def edge_widths(edges, dist):
    """Drawn width of each edge in px: twice the median distance-transform value along the
    middle 60 % of its skeleton (the ends are inflated where lines meet)."""
    out = []
    for _, _, p in edges:
        p = np.asarray(p, int)
        k = len(p) // 5
        core = p[k:len(p) - k] if len(p) - 2 * k >= 3 else p
        out.append(2.0 * float(np.median(dist[core[:, 0], core[:, 1]])))
    return np.asarray(out)


def weight_classes(widths, lengths, n):
    """Split edge widths into n weight classes, thinnest first: 1-D k-means on log width,
    weighted by edge length. -> (class per edge, class centre widths in px)."""
    if n <= 1 or len(widths) == 0:
        return np.zeros(len(widths), int), [float(np.median(widths)) if len(widths) else 0.0]
    x = np.log(np.maximum(widths, 0.5))
    w = np.asarray(lengths, float)
    srt = np.argsort(x)
    cw = np.cumsum(w[srt]) / w.sum()
    c = np.array([x[srt][min(np.searchsorted(cw, (i + 0.5) / n), len(x) - 1)] for i in range(n)])
    for _ in range(50):
        lab = np.argmin(np.abs(x[:, None] - c[None, :]), axis=1)
        new = np.array([np.average(x[lab == i], weights=w[lab == i]) if (lab == i).any() else c[i]
                        for i in range(n)])
        if np.allclose(new, c):
            break
        c = new
    rank = np.argsort(c)
    remap = np.empty(n, int)
    remap[rank] = np.arange(n)
    return remap[lab], [float(np.exp(c[i])) for i in rank]


# ------------------------------------------------------------------ main
def trace(ink, spur, trim, eps, bend, soft, pens=1, splits=None, progress=None):
    """-> (stroke lists, one per weight class, thinnest first), class centre widths in px."""
    rep = progress or (lambda name, f: None)
    dist = ndi.distance_transform_edt(ink)
    sk = thin(ink, progress=stage(progress, 0.0, 0.55), max_dist=float(dist.max()) if ink.any() else 0)
    rep("Following the lines", 0.55)
    nodes, edges = skeleton_graph(sk)
    edges = prune(nodes, edges, spur)
    edges = merge_degree2(edges)
    rep("Line weights", 0.72)
    widths = edge_widths(edges, dist)
    lengths = [len(p) for _, _, p in edges]
    if splits:
        cls = np.searchsorted(np.asarray(sorted(splits)), widths)
        centres = [float(np.median(widths[cls == i])) if (cls == i).any() else 0.0
                   for i in range(len(splits) + 1)]
    else:
        cls, centres = weight_classes(widths, lengths, pens)
    rep("Junctions", 0.76)
    nodes = place_junctions(nodes, edges, trim)
    edges = snap_ends(nodes, edges, trim)
    rep("Simplifying", 0.84)
    edges = [(a, b, chaikin_soft(rdp(p, eps), soft) if soft else rdp(p, eps)) for a, b, p in edges]
    out = []
    for i in range(len(centres)):
        rep("Joining and ordering strokes", 0.9 + 0.1 * i / len(centres))
        sub = [e for e, c in zip(edges, cls) if c == i]
        strokes = join_through(sub, nodes, bend)
        out.append(order([s for s in strokes if len(s) >= 2]))
    return out, centres


def trace_grey(g, out, width_mm, threshold=170, min_len=12, pens=3, splits=None, eps=1.0, bend=25.0,
               soft=20.0, spur=0.0, trim=0.0, fills=True, max_pen_mm=1.0, shading=None, overrides=(),
               analysis=None, erase=None, text=None, progress=None):
    """Grey image (uint8 array) -> SVG at out. shading: find_fills thresholds (cover, aspect,
    tone_min, min_area, line_frac); overrides: [(kind, x, y)] hand decisions at image points;
    analysis: a find_fills.Analysis of this image at this threshold and pen width, to skip that
    pass. erase: bool mask of ink to leave out (lettering written back as text); text: (polylines,
    filled rings) in image px (x, y) for the text layer. progress(stage, fraction). -> stats dict."""
    import find_fills as ff
    sh = dict(ff.DEFAULTS, **(shading or {}))
    mm_per_px = width_mm / g.shape[1]
    max_w = max_pen_mm / mm_per_px
    if fills:
        an = analysis or ff.analyze(g, threshold, max_w, min_len, progress=stage(progress, 0.0, 0.4))
        decisions = ff.decide(an, sh, overrides)
        if erase is not None:
            # a wide ink area that is mostly erased lettering is no fill: the text layer draws it
            for c in an.cands:
                y0, x0, y1, x1 = c["bbox"]
                m = an.lab[y0:y1, x0:x1] == c["id"]
                if m.any() and erase[y0:y1, x0:x1][m].mean() > 0.5:
                    decisions[c["id"]] = "line"
        ink, areas = ff.apply(an, decisions, sh["tone_min"], sh["line_frac"])
        if erase is not None:
            ink &= ~erase
        width = an.width
        if areas:
            ink = despeckle(ink, min_len)
            # typical width again: the skeleton outside the filled areas plus the lines traced
            # inside them (thinned again only there)
            keep = an.sk.copy()
            vals = []
            for sl, m, _ in areas:
                keep[sl][ndi.binary_dilation(m, ff.disk(2))] = False
                sub = ink[sl] & m
                if sub.any():
                    vals.append(ndi.distance_transform_edt(np.pad(sub, 1))[1:-1, 1:-1][thin(sub)])
            vals = np.concatenate([an.dist[keep]] + vals)
            if len(vals):
                width = float(np.median(vals) * 2)
        n_cand = len(an.cands)
        t0, t1 = 0.45, 0.97
    else:
        ink = despeckle(g < threshold, min_len)
        if erase is not None:
            ink &= ~erase
        dist = ndi.distance_transform_edt(ink)
        sk0 = thin(ink, progress=stage(progress, 0.0, 0.3), max_dist=float(dist.max()) if ink.any() else 0)
        width = float(np.median(dist[sk0]) * 2) if sk0.any() else 3.0
        areas, n_cand = [], 0
        t0, t1 = 0.32, 0.97
    spur = spur or 2.0 * width
    trim = trim or 1.0 * width
    if progress:
        progress("Outlining fills", t0 - 0.03)
    fill_rings = [([r + (sl[0].start, sl[1].start) for r in rings_of(m, eps)], level)
                  for sl, m, level in areas]
    layers, centres = trace(ink, spur, trim, eps, bend, soft, pens, splits, progress=stage(progress, t0, t1))
    widths_mm = [c * mm_per_px for c in centres]
    if progress:
        progress("Writing the SVG", t1)
    write_svg(out, layers, ink.shape, width_mm, widths_mm, fills=fill_rings, text=text)
    return {"width_px": round(width, 2), "spur_px": round(spur, 1), "trim_px": round(trim, 1),
            "candidates": n_cand, "fills": len(areas), "kept_as_lines": n_cand - len(areas),
            "pens": [{"width_px": round(c, 2), "width_mm": round(w, 3), "strokes": len(st),
                      "points": int(sum(len(x) for x in st))} for c, w, st in zip(centres, widths_mm, layers)],
            "layers": layers, "widths_mm": widths_mm, "fill_rings": fill_rings, "shape": list(ink.shape)}


PEN_COLOURS = ["#111111", "#2463a6", "#c0392b", "#7a4a1e", "#2e7d32", "#6a1b9a"]


def fill_tone(level):
    """A fill's grey level -> one of three tones, so a drawing has at most three fill inks
    (the plotter hatches each by its darkness: the darkest gets a cross-hatch)."""
    return 0x22 if level < 64 else 0x60 if level < 128 else 0xa0


TEXT_COLOUR = "#111111"


def write_svg(path, layers, shape, width_mm, widths_mm, first_pen=1, fills=(), text=None):
    """Filled areas first (closed paths with a grey fill, for the plotter's hatch fill), then
    one <g> per weight class, thinnest first, stroke width set to the drawn width, then the
    rewritten text as its own layer. fills: [(rings in (y, x) px, grey level)];
    text: (polylines, filled letter rings), both in (x, y) px."""
    H, W = shape
    k = width_mm / W
    h_mm = H * k
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
                f'width="{width_mm:.2f}mm" height="{h_mm:.2f}mm" '
                f'viewBox="0 0 {width_mm:.3f} {h_mm:.3f}">\n')
        if fills:
            f.write('<g id="fills" inkscape:groupmode="layer" inkscape:label="fills" stroke="none" '
                    'fill-rule="evenodd">\n')
            for rings, level in fills:
                v = fill_tone(level)
                d = " ".join("M" + " L".join(f"{x * k:.3f},{y * k:.3f}" for y, x in r[:-1]) + " Z"
                             for r in rings)
                f.write(f'<path fill="#{v:02x}{v:02x}{v:02x}" d="{d}"/>\n')
            f.write("</g>\n")
        lines, rings = text or ([], [])
        if rings:
            d = " ".join("M" + " L".join(f"{x * k:.3f},{y * k:.3f}" for x, y in r[:-1]) + " Z" for r in rings)
            f.write('<g id="textfill" inkscape:groupmode="layer" inkscape:label="text fill" stroke="none" '
                    f'fill-rule="nonzero"><path fill="{TEXT_COLOUR}" d="{d}"/></g>\n')
        for i, (strokes, wmm) in enumerate(zip(layers, widths_mm)):
            n = first_pen + i
            col = PEN_COLOURS[(n - 1) % len(PEN_COLOURS)]
            f.write(f'<g id="pen{n}" fill="none" stroke="{col}" stroke-width="{wmm:.2f}" '
                    f'stroke-linecap="round" stroke-linejoin="round">\n')
            for s in strokes:
                d = "M" + " L".join(f"{x * k:.3f},{y * k:.3f}" for y, x in s)
                f.write(f'<path d="{d}"/>\n')
            f.write("</g>\n")
        if lines:
            w = min(widths_mm) if widths_mm else 0.3
            f.write(f'<g id="text" inkscape:groupmode="layer" inkscape:label="text" fill="none" '
                    f'stroke="{TEXT_COLOUR}" stroke-width="{w:.2f}" stroke-linecap="round" stroke-linejoin="round">\n')
            for s in lines:
                f.write('<path d="M' + " L".join(f"{x * k:.3f},{y * k:.3f}" for x, y in s) + '"/>\n')
            f.write("</g>\n")
        f.write("</svg>\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width-mm", type=float, default=250.0, help="drawing width on paper")
    ap.add_argument("--threshold", type=int, default=170, help="ink is darker than this (0-255)")
    ap.add_argument("--min-area", type=int, default=12, help="drop ink specks smaller than this, px")
    ap.add_argument("--crop", help="x0,y0,x1,y1 in source pixels")
    ap.add_argument("--spur", type=float, default=0, help="prune spurs shorter than this, px (0: 2x stroke width)")
    ap.add_argument("--trim", type=float, default=0, help="junction snap radius, px (0: 1x stroke width)")
    ap.add_argument("--eps", type=float, default=1.0, help="simplify tolerance, px")
    ap.add_argument("--bend", type=float, default=25.0, help="join through junctions up to this bend, deg")
    ap.add_argument("--soft", type=float, default=20.0, help="round bends gentler than this, deg (0: off)")
    ap.add_argument("--pens", type=int, default=1, help="split lines into this many weight classes (pens)")
    ap.add_argument("--splits", help="class boundaries as drawn width in px, e.g. 5,7 (overrides --pens)")
    ap.add_argument("--split-files", action="store_true", help="also write one SVG per pen, same page")
    ap.add_argument("--no-fills", action="store_true",
                    help="no shading pass: trace every ink area as lines")
    ap.add_argument("--max-pen", type=float, default=1.0,
                    help="widest line a pen draws, mm on paper; wider ink areas become fills")
    ap.add_argument("--cover", type=float, default=0.6,
                    help="a wide ink area stays a line if the line strokes cover at least this share of it")
    ap.add_argument("--aspect", type=float, default=4.0,
                    help="a wide ink area stays a line only if it is at least this many times longer than wide")
    ap.add_argument("--tone-min", type=int, default=40,
                    help="a fill area is shaded (lines on it kept) if most of it is at least this light")
    ap.add_argument("--fill-min-area", type=int, default=400, help="smallest fill area, px")
    a = ap.parse_args(argv)
    crop = tuple(int(v) for v in a.crop.split(",")) if a.crop else None
    g = load_grey(a.image, crop)
    splits = [float(v) for v in a.splits.split(",")] if a.splits else None
    st = trace_grey(g, a.out, a.width_mm, a.threshold, a.min_area, a.pens, splits, a.eps, a.bend, a.soft,
                    a.spur, a.trim, not a.no_fills, a.max_pen,
                    {"cover": a.cover, "aspect": a.aspect, "tone_min": a.tone_min, "min_area": a.fill_min_area})
    if a.split_files and len(st["layers"]) > 1:
        stem = a.out[:-4] if a.out.lower().endswith(".svg") else a.out
        for i, (strokes, wmm) in enumerate(zip(st["layers"], st["widths_mm"])):
            write_svg(f"{stem}-pen{i + 1}.svg", [strokes], g.shape, a.width_mm, [wmm], first_pen=i + 1)
    print(f"stroke width ~{st['width_px']:.1f}px, spur {st['spur_px']:.0f}px, trim {st['trim_px']:.0f}px",
          file=sys.stderr)
    if st["candidates"]:
        print(f"  shading pass: {st['candidates']} wide ink area(s), {st['fills']} filled, "
              f"{st['kept_as_lines']} kept as lines (stroke coverage >= {a.cover:g})", file=sys.stderr)
    for i, pen in enumerate(st["pens"]):
        print(f"  pen {i + 1}: drawn width ~{pen['width_px']:.1f}px = {pen['width_mm']:.2f} mm at this size, "
              f"{pen['strokes']} strokes, {pen['points']} points", file=sys.stderr)
    print(f"wrote {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
