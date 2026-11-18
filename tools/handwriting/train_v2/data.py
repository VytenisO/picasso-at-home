"""Data for the v2 handwriting model: real capture samples, synthetic words stitched from the writer's own letters,
and synthetic words from a Hershey script font. Everything becomes the same thing: a list of strokes in x-height
units, x from the left edge of the ink, y down from the baseline (ascenders negative), strokes in writing order.

Three domains, given to the model as a flag, so it can share what it learns about letters and alignment while
keeping the styles apart (generation uses REAL):
  0 HERSHEY   Hershey "scripts" glyphs, joined with smooth connectors
  1 STITCHED  the writer's own isolated letters (letters/capitals/symbols prompts), joined with smooth connectors
  2 REAL      the writer's words and sentence lines as written

numpy only (HersheyFonts is optional, needed only for domain 0).
"""
import json
import math
import pathlib
import random

import numpy as np

HERSHEY, STITCHED, REAL = 0, 1, 2
END = ""          # appended to every text: the attention window parks on it when the writing is done
SPACING = 0.12          # resampling step along the pen path, in x-heights
SCALE = SPACING         # offsets are divided by this, so a pen-down step is about 1.0
SAMPLE_GLOB = "[a-z][0-9][0-9][0-9].json"

# held out from all training: these word prompts (every 10th) and one sentence. Their text is also kept out of the
# synthetic corpus, so they are unseen text for the model, not just unseen samples.
HELDOUT_WORD_IDS = [f"w{n:03d}" for n in range(5, 127, 10)]
HELDOUT_SENTENCE_IDS = ["s008"]


# ---------------------------------------------------------------- geometry helpers
def path_len(p):
    return float(np.sum(np.linalg.norm(np.diff(p, axis=0), axis=1))) if len(p) > 1 else 0.0


def smooth(p, k=3):
    if len(p) < k + 2:
        return p
    q = p.copy()
    ker = np.ones(k) / k
    for d in range(2):
        pad = np.concatenate([np.repeat(p[:1, d], k // 2), p[:, d], np.repeat(p[-1:, d], k // 2)])
        q[:, d] = np.convolve(pad, ker, mode="valid")
    q[0], q[-1] = p[0], p[-1]
    return q


def resample(p, step, phase=0.0):
    """Points every `step` along the path (first and last point kept). A path shorter than step/3 becomes one point."""
    if len(p) == 1:
        return p.copy()
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    L = float(seg.sum())
    if L < step / 3:
        return p[:1].copy()
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    n = max(1, int(round(L / step)))
    t = np.linspace(0.0, L, n + 1)
    if phase and n > 1:                       # jitter the interior samples a little (augmentation)
        t[1:-1] += (np.random.rand(n - 1) - 0.5) * phase * step
        t = np.sort(np.clip(t, 0, L))
    x = np.interp(t, cum, p[:, 0])
    y = np.interp(t, cum, p[:, 1])
    return np.stack([x, y], 1)


def bounds(strokes):
    a = np.concatenate(strokes, 0)
    return a.min(0), a.max(0)


def shift_left(strokes):
    x0 = min(s[:, 0].min() for s in strokes)
    return [s - np.array([x0, 0.0]) for s in strokes]


# ---------------------------------------------------------------- real samples
def load_samples(samples_dir, prompts_path=None):
    """-> list of dicts {id, section, text, strokes (normalised, x-height units)}; two-line prompts are split into
    one item per line (id suffix -1, -2)."""
    prompts = None
    if prompts_path and pathlib.Path(prompts_path).exists():
        prompts = {p["id"]: p["text"] for p in json.loads(pathlib.Path(prompts_path).read_text(encoding="utf-8"))["prompts"]}
    out = []
    for f in sorted(pathlib.Path(samples_dir).glob(SAMPLE_GLOB)):
        s = json.loads(f.read_text(encoding="utf-8"))
        if prompts is not None and prompts.get(s["id"]) != s["text"]:
            continue
        g = s["guide"]
        xh = float(g["x_height"])
        bases = [float(b) for b in g["baselines"]]
        lines = s["text"].split("\n")
        raw = [np.array([[p[0], p[1]] for p in st], dtype=np.float64) for st in s["strokes"] if len(st)]
        if len(lines) != len(bases):
            continue
        per_line = [[] for _ in lines]
        for st in raw:
            # a stroke belongs to the line whose baseline is nearest its middle, measured half an x-height up
            mid = float(np.median(st[:, 1])) + 0.5 * xh
            li = int(np.argmin([abs(mid - b) for b in bases]))
            per_line[li].append(st)
        for li, (text, sts) in enumerate(zip(lines, per_line)):
            if not sts:
                continue
            norm = [np.stack([st[:, 0] / xh, (st[:, 1] - bases[li]) / xh], 1) for st in sts]
            norm = shift_left(norm)
            sid = s["id"] if len(lines) == 1 else f"{s['id']}-{li + 1}"
            out.append({"id": sid, "pid": s["id"], "section": s["section"], "text": text.strip(), "strokes": norm})
    return out


def split(samples):
    held_ids = set(HELDOUT_WORD_IDS) | set(HELDOUT_SENTENCE_IDS)
    train = [s for s in samples if s["pid"] not in held_ids]
    held = [s for s in samples if s["pid"] in held_ids]
    return train, held


def heldout_words(held):
    words = set()
    for s in held:
        for w in s["text"].split():
            words.add(w.strip(".,;:!?\"'()").lower())
    return words


def words_from_lines(samples):
    """Cut multi-word real samples (sentence lines) into one real sample per word, at the widest gaps. A cut is kept
    only if every piece has a plausible width for its word."""
    out = []
    for s in samples:
        if s["section"] != "sentences":
            continue
        toks = s["text"].split()
        if len(toks) < 2:
            continue
        parts = _split_n(s["strokes"], len(toks))
        if parts is None:
            continue
        ok = True
        for t, p in zip(toks, parts):
            (x0, _), (x1, _) = bounds(p)
            if not (0.25 * len(t) <= x1 - x0 <= 2.0 * len(t) + 0.8):
                ok = False
        if ok:
            out += [{"id": f"{s['id']}w{i}", "pid": s["pid"], "section": "linewords", "text": t, "strokes": shift_left(p)}
                    for i, (t, p) in enumerate(zip(toks, parts))]
    return out


def compose(items, rng):
    """Several real samples side by side as one line (word gaps of 0.55 to 1.0 x-height)."""
    strokes, x, texts = [], 0.0, []
    for it in items:
        st = augment(it["strokes"], rng, 0.4)
        if strokes:
            x += rng.uniform(0.55, 1.0)
        strokes += [p + np.array([x, 0.0]) for p in st]
        x = max(float(p[:, 0].max()) for p in strokes)
        texts.append(it["text"])
    return " ".join(texts), strokes


# ---------------------------------------------------------------- glyphs (for stitching)
def _groups(strokes, tol):
    """Strokes whose x ranges overlap (within tol) form one glyph. Writing order is kept inside a glyph."""
    order = sorted(range(len(strokes)), key=lambda i: strokes[i][:, 0].min())
    groups = []
    for i in order:
        lo, hi = strokes[i][:, 0].min(), strokes[i][:, 0].max()
        if groups and lo <= groups[-1]["hi"] + tol:
            groups[-1]["hi"] = max(groups[-1]["hi"], hi)
            groups[-1]["idx"].append(i)
        else:
            groups.append({"lo": lo, "hi": hi, "idx": [i]})
    return [[strokes[i] for i in sorted(g["idx"])] for g in groups]


def _split_n(strokes, n):
    """Split strokes into n glyphs at the n-1 widest horizontal gaps between overlapping clusters."""
    g = _groups(strokes, 0.15)
    if len(g) == n:
        return g
    order = sorted(range(len(strokes)), key=lambda i: strokes[i][:, 0].min())
    cl = []                                     # clusters of truly overlapping strokes
    for i in order:
        lo, hi = strokes[i][:, 0].min(), strokes[i][:, 0].max()
        if cl and lo <= cl[-1][1]:
            cl[-1][1] = max(cl[-1][1], hi)
            cl[-1][2].append(i)
        else:
            cl.append([lo, hi, [i]])
    if len(cl) < n:
        return None
    gaps = sorted(range(len(cl) - 1), key=lambda j: cl[j + 1][0] - cl[j][1], reverse=True)[:n - 1]
    out, cur = [], []
    for j, c in enumerate(cl):
        cur += c[2]
        if j in gaps or j == len(cl) - 1:
            out.append([strokes[i] for i in sorted(cur)])
            cur = []
    return out


def merge_contiguous(strokes, tol=0.05):
    out = []
    for s in strokes:
        if out and np.linalg.norm(out[-1][-1] - s[0]) < tol:
            out[-1] = np.concatenate([out[-1], s[1:]], 0)
        else:
            out.append(s)
    return out


def glyphs_from_samples(samples):
    """char -> list of variants; a variant is a list of strokes, x from 0 at its left edge."""
    gl = {}
    for s in samples:
        if s["section"] not in ("letters", "capitals", "symbols"):
            continue
        toks = s["text"].split()
        grouped = _split_n(s["strokes"], len(toks))
        if grouped is None:
            continue
        for t, g in zip(toks, grouped):
            if len(t) == 1:
                gl.setdefault(t, []).append(shift_left(g))
    return gl


def hershey_glyphs(chars):
    try:
        from HersheyFonts import HersheyFonts
    except ImportError:
        return {}
    f = HersheyFonts()
    f.load_default_font("scripts")
    k = 1.0 / 9.0      # scripts: baseline at y=9, x-height line at y=0

    def one(ch):
        gs = list(f.glyphs_for_text(ch))
        if not gs or not gs[0].strokes:
            return None
        st = [np.array(list(s), dtype=np.float64) for s in gs[0].strokes]
        st = [np.stack([(p[:, 0] - gs[0].left_offset) * k, (p[:, 1] - 9.0) * k], 1) for p in st]
        return merge_contiguous(st)

    out = {}
    for ch in chars:
        g = one(ch)
        if g is None and ch in "æøåÆØÅ":
            g = _compose_nordic(ch, one)
        if g:
            out[ch] = [shift_left(g)]
    return out


def _compose_nordic(ch, one):
    up = ch.isupper()
    if ch in "æÆ":
        a, e = one("A" if up else "a"), one("E" if up else "e")
        if not a or not e:
            return None
        w = bounds(a)[1][0]
        return a + [s + np.array([w * 0.75, 0.0]) for s in e]
    base = one({"ø": "o", "Ø": "O", "å": "a", "Å": "A"}[ch])
    if not base:
        return None
    (x0, y0), (x1, y1) = bounds(base)
    if ch in "øØ":
        return base + [np.array([[x0 - 0.1, y1 + 0.1], [x1 + 0.1, y0 - 0.1]])]
    top = y0 - 0.35
    cx = (x0 + x1) / 2
    ring = np.array([[cx + 0.18 * math.cos(t), top + 0.18 * math.sin(t)] for t in np.linspace(0, 2 * math.pi, 12)])
    return base + [ring]


# ---------------------------------------------------------------- stitching words from glyphs
def _tangent(p, at_end):
    q = p[-min(4, len(p)):] if at_end else p[:min(4, len(p))]
    v = (q[-1] - q[0]) if len(q) > 1 else np.array([1.0, 0.0])
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else np.array([1.0, 0.0])


def _join(a, b, rng):
    p0, p3 = a[-1], b[0]
    d = np.linalg.norm(p3 - p0)
    t0, t1 = _tangent(a, True), _tangent(b, False)
    k = d * rng.uniform(0.25, 0.45)
    p1, p2 = p0 + t0 * k, p3 - t1 * k
    n = max(2, int(d / 0.05))
    t = np.linspace(0, 1, n)[1:-1, None]
    return ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * p1 + 3 * (1 - t) * t * t * p2 + (t ** 3) * p3


def _joinable(c):
    return c.isalpha() and c.islower()


def stitch(text, glyphs, rng, join_prob=0.9):
    """Text -> strokes, built from glyph variants. Lowercase letters inside a word are joined into one stroke; small
    extra strokes (i dots, t bars, ring of å) are written after the word, as people do. Returns None if a character
    has no glyph."""
    strokes = []
    x = 0.0
    for wi, word in enumerate(text.split(" ")):
        if wi:
            x += rng.uniform(0.55, 1.0)
        cur = None            # the open, joined stroke
        later = []            # deferred small strokes
        prev = None
        for c in word:
            vs = glyphs.get(c)
            if not vs:
                return None
            g = [s.copy() for s in vs[rng.randrange(len(vs))]]
            sc = rng.uniform(0.92, 1.08)
            dy = rng.uniform(-0.04, 0.04)
            g = [np.stack([s[:, 0] * sc, s[:, 1] * sc + dy], 1) for s in g]
            joined = prev is not None and _joinable(prev) and _joinable(c) and cur is not None and rng.random() < join_prob
            gap = rng.uniform(-0.18, 0.04) if joined else rng.uniform(0.08, 0.25)
            if prev is None:
                gap = 0.0
            g = [s + np.array([x + gap, 0.0]) for s in g]
            main, rest = g[0], g[1:]
            if joined:
                cur = np.concatenate([cur, _join(cur, main, rng), main], 0)
            else:
                if cur is not None:
                    strokes.append(cur)
                cur = main
            for s in rest:
                small = path_len(s) < 1.3 and (s[:, 1].max() - s[:, 1].min()) < 0.9
                if small and _joinable(c):
                    later.append(s)
                else:
                    strokes.append(cur)
                    cur = s
            x = max(x, max(s[:, 0].max() for s in g))
            prev = c
            if not _joinable(c):
                strokes.append(cur)
                cur = None
        if cur is not None:
            strokes.append(cur)
        strokes += later
    strokes = [s for s in strokes if len(s)]
    return shift_left(strokes) if strokes else None


# ---------------------------------------------------------------- augmentation and encoding
def augment(strokes, rng, strength=1.0):
    g = rng.uniform(1 - 0.1 * strength, 1 + 0.1 * strength)
    sx = g * rng.uniform(1 - 0.07 * strength, 1 + 0.07 * strength)
    sy = g * rng.uniform(1 - 0.05 * strength, 1 + 0.05 * strength)
    shear = rng.uniform(-0.12, 0.12) * strength
    rot = math.radians(rng.uniform(-1.5, 1.5) * strength)
    dy = rng.uniform(-0.06, 0.06) * strength
    c, s_ = math.cos(rot), math.sin(rot)
    out = []
    for st in strokes:
        x, y = st[:, 0] * sx, st[:, 1] * sy
        x = x - shear * y                      # y is negative above the baseline: positive shear leans right
        x, y = c * x - s_ * y, s_ * x + c * y
        out.append(np.stack([x, y + dy], 1))
    return shift_left(out)


def encode(strokes, step=SPACING, phase=0.0, jitter=0.0, presmooth=True):
    """Strokes -> (seq [T,4] of dx, dy (scaled by 1/SCALE), eos, end; yabs [T] y of each point, x-heights below baseline).
    The first offset is from the origin (left edge, baseline)."""
    pts, eos = [], []
    for st in strokes:
        p = smooth(st) if presmooth else st
        p = resample(p, step, phase)
        if jitter:
            p = p + np.random.randn(*p.shape) * jitter
        pts.append(p)
        e = np.zeros(len(p))
        e[-1] = 1
        eos.append(e)
    P = np.concatenate(pts, 0)
    E = np.concatenate(eos, 0)
    D = np.diff(np.concatenate([[[0.0, 0.0]], P], 0), axis=0) / SCALE
    F = np.zeros(len(P))
    F[-1] = 1
    return np.concatenate([D, E[:, None], F[:, None]], 1).astype(np.float32), P[:, 1].astype(np.float32)


def decode(seq):
    """[T,3] scaled offsets -> list of strokes (x-height units)."""
    P = np.cumsum(seq[:, :2] * SCALE, 0)
    strokes, cur = [], []
    for p, e in zip(P, seq[:, 2]):
        cur.append(p)
        if e > 0.5:
            strokes.append(np.array(cur))
            cur = []
    if cur:
        strokes.append(np.array(cur))
    return strokes


# ---------------------------------------------------------------- charset
def build_charset(samples, extra=END):
    chars = set(extra)
    for s in samples:
        chars.update(s["text"])
    chars.discard("\n")
    chars.add(" ")
    return "".join(sorted(chars))
