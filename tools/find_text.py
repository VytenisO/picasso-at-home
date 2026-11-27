"""Text in a technical drawing, for the tracer: traced lettering is unreadable at plot size, so text the OCR reads
is erased from the image and written back as pen strokes.

1. OCR (`ocr_text.py`, run in its own Python, `$PLOTTER_OCR_PYTHON`) gives text boxes with a reading and a
   confidence.
2. `analyze` measures each box against the ink: the letter blobs lying (almost) wholly inside it, their cap
   height (dots, commas and quote marks left out; words on one line share it) and stroke width, and whether the
   reading is plausible (about as long as its box: "S R RIG" across a whole title is not).
3. `decide` picks per box: rewrite (confident and plausible), else trace as drawn; hand edits override, and may
   also change the string or drop the box.
4. `erase_mask` is what the tracer removes; `rewrite` gives the strokes: thin lettering in single-line Hershey,
   medium in Hershey Sans Medium, heavy lettering as a bold outline font whose letters are filled (the plotter
   hatches them), each fitted to its box: cap height, length, angle. Text below the smallest height grows to it.

Needs numpy, scipy and Pillow; the bold outlines also fontTools and a bold font (DejaVu Sans Bold via fontconfig).
"""
import json
import math
import os
import subprocess
import sys
import unicodedata

import numpy as np
from scipy import ndimage as ndi

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {"text_conf": 60, "text_min_mm": 1.5, "text_font": "Hershey Sans"}
INSIDE = 0.85          # share of a blob inside a box for it to count as lettering
THIN_FONTS = ("Hershey Sans", "Hershey Sans Medium", "Hershey Serif")


# ---- OCR
def ocr_python():
    p = os.environ.get("PLOTTER_OCR_PYTHON", "").strip()
    return p if p and os.path.isfile(p) else None


def available():
    return ocr_python() is not None


def run_ocr(png, timeout=1800):
    """-> [{quad, text, conf}] from the OCR helper, in the image's pixels."""
    exe = ocr_python()
    if not exe:
        raise RuntimeError("Text reading is not set up on this server (PLOTTER_OCR_PYTHON).")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_text.py")
    r = subprocess.run([exe, script, str(png)], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"Reading the text failed: {tail[0][:200]}")
    return json.loads(r.stdout)["boxes"]


# ---- geometry
def geom(quad):
    """Quad (corners in any order) -> (origin: bottom-left of the text, unit along it, unit up, length, height).
    Text reads left to right, or bottom to top when upright."""
    q = np.asarray(quad, float)
    c = q.mean(0)
    e = [q[(i + 1) % 4] - q[i] for i in range(4)]
    i = 0 if np.linalg.norm(e[0]) + np.linalg.norm(e[2]) >= np.linalg.norm(e[1]) + np.linalg.norm(e[3]) else 1
    u = e[i] - e[i + 2]
    n = np.linalg.norm(u)
    u = u / n if n else np.array([1.0, 0.0])
    if u[0] < -0.2 or (abs(u[0]) <= 0.2 and u[1] > 0):
        u = -u
    up = np.array([u[1], -u[0]])                  # image y grows downward
    pu, pv = (q - c) @ u, (q - c) @ up
    L, h = float(pu.max() - pu.min()), float(pv.max() - pv.min())
    return c + u * pu.min() - up * (h / 2), u, up, L, h


def box_raster(shape, quads, pad_frac=0.08):
    """Padded quads painted as their index + 1 (0: none)."""
    from PIL import Image, ImageDraw
    H, W = shape
    img = Image.new("I", (W, H), 0)
    d = ImageDraw.Draw(img)
    for k, q in enumerate(quads, 1):
        o, u, up, L, h = geom(q)
        p = max(2.0, pad_frac * h)
        pts = [o - u * p - up * p, o + u * (L + p) - up * p, o + u * (L + p) + up * (h + p), o - u * p + up * (h + p)]
        d.polygon([tuple(map(float, t)) for t in pts], fill=k)
    return np.asarray(img)


def letters(ink, quads):
    """-> (labels, blob -> owning box index (1-based) for the blobs that are lettering)."""
    lab, n = ndi.label(ink, structure=np.ones((3, 3)))
    if not n or not quads:
        return lab, {}
    bm = box_raster(ink.shape, quads)
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    inside = np.bincount(lab[bm > 0], minlength=n + 1)
    frac = inside / np.maximum(sizes, 1)
    owner = {}
    objs = ndi.find_objects(lab)
    for i in np.nonzero(frac >= INSIDE)[0]:
        if i == 0:
            continue
        sl = objs[i - 1]
        k = int(np.bincount(bm[sl][lab[sl] == i]).argmax())
        if k:
            owner[int(i)] = k
    return lab, owner


def plausible(text, L, h):
    """A reading is about as long as its box: letters run 0.3 to 1.7 cap heights apart (2.6 for one or two)."""
    n = max(len(text), 1)
    r = L / (n * 0.7 * max(h, 1e-6))
    return 0.3 <= r <= (1.7 if n > 2 else 2.6)


def analyze(g, boxes, threshold=170):
    """Grey image + OCR boxes -> [box dict]: id, quad, text, conf, length, height, angle (deg), cap and stroke
    (px, 0 when no lettering blob is inside), blobs, plausible."""
    ink = g < threshold
    quads = [b["quad"] for b in boxes]
    lab, owner = letters(ink, quads)
    objs = ndi.find_objects(lab)
    dist = ndi.distance_transform_edt(ink)
    heights, strokes = {}, {}
    for i, k in owner.items():
        sl = objs[i - 1]
        heights.setdefault(k, []).append(sl[0].stop - sl[0].start)
        strokes.setdefault(k, []).append(dist[sl][lab[sl] == i])
    out = []
    for k, b in enumerate(boxes, 1):
        o, u, up, L, h = geom(b["quad"])
        hs = heights.get(k, [])
        hs = [x for x in hs if x >= 0.6 * max(hs)] if hs else []
        upright = abs(u[0]) >= 0.97
        # blob heights are measured upright: on slanted text the box height says more
        cap = (float(np.median(hs)) if upright else 0.7 * h) if hs else 0.0
        out.append({"id": k, "quad": [[round(float(x), 1), round(float(y), 1)] for x, y in b["quad"]],
                    "text": b["text"], "conf": float(b["conf"]), "length": round(L, 1), "height": round(h, 1),
                    "angle": round(math.degrees(math.atan2(-u[1], u[0])), 1), "cap": round(cap, 1),
                    # stroke width: twice the distance to the paper near the letters' middles
                    "stroke": round(2 * float(np.percentile(np.concatenate(strokes[k]), 90)), 2) if k in strokes else 0.0,
                    "blobs": len(heights.get(k, [])), "plausible": plausible(b["text"], L, h)})
    # words on one line share a height: same direction, baselines within a third, gaps under three heights
    g_ = {b["id"]: geom(b["quad"]) for b in out}
    for b in out:
        if not b["cap"]:
            continue
        o, u, up, L, h = g_[b["id"]]
        near = [c["cap"] for c in out if c["cap"] and abs(g_[c["id"]][1] @ u) > 0.99
                and abs((g_[c["id"]][0] - o) @ up) < b["cap"] / 3
                and max((g_[c["id"]][0] - o) @ u - L, (o - g_[c["id"]][0]) @ u - g_[c["id"]][3]) < 3 * b["cap"]]
        b["line_cap"] = round(float(np.median(near)), 1)
    return out


# ---- decisions
def parse_edits(s):
    """Hand edits as saved in the recipe: a JSON list of {x, y, k: rewrite|trace|drop, s?: string}."""
    if not s:
        return []
    try:
        v = json.loads(s)
    except ValueError:
        return []
    out = []
    for e in v if isinstance(v, list) else []:
        if isinstance(e, dict) and e.get("k") in ("rewrite", "trace", "drop"):
            try:
                x, y = float(e["x"]), float(e["y"])
            except (KeyError, TypeError, ValueError):
                continue
            s_ = e.get("s")
            out.append({"x": x, "y": y, "k": e["k"], "s": s_[:200] if isinstance(s_, str) else None})
    return out


def _inside(quad, x, y):
    q = quad
    ins = False
    for i in range(4):
        (x1, y1), (x2, y2) = q[i], q[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            ins = not ins
    return ins


def box_at(boxes, x, y):
    for b in boxes:
        if _inside(b["quad"], x, y):
            return b
    return None


def auto_kind(b, conf_min):
    ok = (b["conf"] * 100 >= conf_min and b["plausible"] and b["cap"] > 0
          and any(ch.isalnum() for ch in b["text"]))
    return "rewrite" if ok else "trace"


def decide(boxes, params=None, edits=()):
    """-> {box id: {kind: rewrite|trace|drop, text, hand}}. The same rule the page shows live."""
    p = dict(DEFAULTS, **(params or {}))
    out = {b["id"]: {"kind": auto_kind(b, p["text_conf"]), "text": b["text"], "hand": False} for b in boxes}
    for e in edits:
        b = box_at(boxes, e["x"], e["y"])
        if b is None:
            continue
        d = out[b["id"]]
        d.update(kind=e["k"], hand=True)
        if e.get("s") is not None:
            d["text"] = e["s"]
    for b in boxes:
        d = out[b["id"]]
        if d["kind"] == "rewrite" and (not b["cap"] or not d["text"].strip()):
            d["kind"] = "trace"
    return out


def erase_mask(g, boxes, decisions, threshold=170):
    """The ink the tracer must leave out: the lettering of every rewritten or dropped box."""
    gone = [b for b in boxes if decisions[b["id"]]["kind"] in ("rewrite", "drop")]
    ink = g < threshold
    if not gone:
        return np.zeros_like(ink)
    lab, owner = letters(ink, [b["quad"] for b in gone])
    keep = np.zeros(lab.max() + 1, bool)
    keep[list(owner)] = True
    return keep[lab]


# ---- writing it back
def ascii_text(s):
    s = unicodedata.normalize("NFKC", s).replace("\u2044", "/").replace("\u00d7", "x")
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s)


def bold_font():
    """A bold outline font file: DejaVu Sans Bold through fontconfig, else Arial Bold on Windows, else None."""
    try:
        r = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip() and os.path.isfile(r.stdout.strip()):
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arialbd.ttf")
    return win if os.path.isfile(win) else None


_outline_cache = {}


def _outline_font(path):
    if path not in _outline_cache:
        from fontTools.pens.basePen import BasePen
        from fontTools.ttLib import TTFont

        class Flat(BasePen):
            def __init__(self, gs):
                super().__init__(gs)
                self.rings, self.cur = [], []

            def _moveTo(self, p):
                self.cur = [p]

            def _lineTo(self, p):
                self.cur.append(p)

            def _curveToOne(self, p1, p2, p3):
                p0 = self.cur[-1]
                for i in range(1, 7):
                    t = i / 6
                    self.cur.append(tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t * t * c
                                          + t ** 3 * d for a, b, c, d in zip(p0, p1, p2, p3)))

            def _qCurveToOne(self, p1, p2):
                p0 = self.cur[-1]
                for i in range(1, 6):
                    t = i / 5
                    self.cur.append(tuple((1 - t) ** 2 * a + 2 * (1 - t) * t * b + t * t * c
                                          for a, b, c in zip(p0, p1, p2)))

            def _closePath(self):
                if len(self.cur) > 2:
                    self.rings.append(self.cur + [self.cur[0]])
                self.cur = []

            _endPath = _closePath

        f = TTFont(path)
        gs, cmap = f.getGlyphSet(), f.getBestCmap()
        glyphs = {}

        def glyph(ch):
            if ch not in glyphs:
                name = cmap.get(ord(ch)) or cmap.get(ord("?"))
                pen = Flat(gs)
                gs[name].draw(pen)
                glyphs[ch] = (pen.rings, f["hmtx"].metrics[name][0])
            return glyphs[ch]
        cap = max(y for r in glyph("H")[0] for _, y in r)
        _outline_cache[path] = (glyph, cap)
    return _outline_cache[path]


def outline_text(text, path, cap_px, tracking=0.0):
    """-> (rings in px, baseline at y = 0, y up; advance width px)."""
    glyph, cap = _outline_font(path)
    s = cap_px / cap
    rings, x = [], 0.0
    for ch in text:
        rs, adv = glyph(ch)
        rings += [[(x + px * s, py * s) for px, py in r] for r in rs]
        x += adv * s + tracking
    return rings, x - (tracking if text else 0.0)


def style_of(b, mm_per_px, pen_mm=0.4):
    """thin, medium or bold, from the measured stroke width on paper against the pen."""
    sw = b["stroke"] * mm_per_px
    return "thin" if sw < 1.75 * pen_mm else "medium" if sw < 2.75 * pen_mm else "bold"


def rewrite(boxes, decisions, mm_per_px, params=None, pen_mm=0.4, bold_path=None):
    """-> (polylines in image px [(x, y)], filled letters as rings in image px, {style: count})."""
    import hershey
    p = dict(DEFAULTS, **(params or {}))
    thin_font = p["text_font"] if p["text_font"] in THIN_FONTS else "Hershey Sans"
    min_px = p["text_min_mm"] / mm_per_px
    lines, rings, styles = [], [], {}
    for b in boxes:
        d = decisions[b["id"]]
        if d["kind"] != "rewrite":
            continue
        text = ascii_text(d["text"]).strip()
        if not text:
            continue
        o, u, up, L, h = geom(b["quad"])
        o, u, up = np.asarray(o), np.asarray(u), np.asarray(up)
        cap = max(b.get("line_cap") or b["cap"], min_px)
        st = style_of(b, mm_per_px, pen_mm)
        if st == "bold" and not bold_path:
            st = "medium"
        styles[st] = styles.get(st, 0) + 1
        if st == "bold":
            rs, w = outline_text(text, bold_path, cap)
            if not rs or w <= 0:
                continue
            kx = min(1.2, max(0.6, L / w))
            if L > w * kx and len(text) > 1:      # a wide title: wider letters up to 1.2x, the rest as spacing
                rs, w = outline_text(text, bold_path, cap, tracking=(L - w * kx) / kx / (len(text) - 1))
            pts = [[tuple(map(float, o + u * (x * kx) + up * y)) for x, y in r] for r in rs]
            rings += pts
            lines += pts                            # the outline is drawn too, the hatch fills it
            continue
        font = "Hershey Sans Medium" if st == "medium" else thin_font
        gl, _, _ = hershey.font(font)
        cs = gl["H"][2]
        cap_units = max(y for s in cs for _, y in s) - min(y for s in cs for _, y in s)
        xh_units = hershey.font(font)[1]
        polys, w, _ = hershey.text_strokes(text, font, xheight=cap * xh_units / cap_units)
        if not polys or w <= 0:
            continue
        kx = min(1.3, max(0.6, L / w))
        lines += [[tuple(map(float, o + u * (x * kx) - up * y)) for x, y in s] for s in polys]   # hershey y grows down
    return lines, rings, styles
