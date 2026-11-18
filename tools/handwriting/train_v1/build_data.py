#!/usr/bin/env python3
"""Turn capture samples into the line-level arrays that sjvasquez/handwriting-synthesis trains on.

The model learns whole lines (up to 75 characters, 1200 pen points). Our samples are mostly single words, so lines are
assembled from them: random words joined with natural gaps, slant, size and baseline jitter. The 12 real two-line
sentences are added as they are (split into their two lines), with a few augmented variants. Held-out words and one
sentence stay out of the training lines and go to the validation set.

Output (npy, same names as the original prepare_data.py): x (n,1200,3 offsets+eos), x_len, c (n,75), c_len, for the
directories train/ and val/ under --out.

  python build_data.py --data ~/hw-train/data --writer default --out ~/hw-train/v1data --repo ~/hw-train/v1

Private data: this reads and writes handwriting only under paths you give it. Nothing here goes into git.
"""
import argparse
import json
import pathlib
import random
import sys

import numpy as np
from scipy.signal import savgol_filter

MAX_PTS, MAX_CHARS = 1200, 75


def load(repo):
    sys.path.insert(0, str(repo))
    import drawing  # the repo's own preprocessing, so we match it exactly
    return drawing


def resample(pts, step):
    """Uniform arc-length resampling of one stroke (n,2) to roughly `step` spacing; at least 2 points kept."""
    pts = np.asarray(pts, float)
    if len(pts) >= 5:
        pts = np.stack([savgol_filter(pts[:, 0], 5, 2, mode="nearest"), savgol_filter(pts[:, 1], 5, 2, mode="nearest")], 1)
    seg = np.hypot(*np.diff(pts, axis=0).T) if len(pts) > 1 else np.zeros(0)
    total = float(seg.sum())
    if total < step * 0.5:
        return pts[:1].repeat(2, axis=0) if len(pts) else pts
    s = np.concatenate([[0], np.cumsum(seg)])
    n = max(2, int(round(total / step)) + 1)
    t = np.linspace(0, total, n)
    return np.stack([np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])], 1)


def read_samples(sdir, alphabet):
    """-> list of dict(text, section, strokes=[(n,2) arrays in x-height units, y up, x from left, y from baseline])."""
    out = []
    for f in sorted(pathlib.Path(sdir).glob("[a-z][0-9][0-9][0-9].json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        g = d["guide"]
        xh = float(g["x_height"])
        lines = d["text"].split("\n")
        bl = g["baselines"]
        groups = [[] for _ in lines]
        for s in d["strokes"]:
            a = np.asarray(s, float)[:, :2]
            li = int(np.argmin([abs(a[:, 1].mean() - b) for b in bl[:len(lines)]]))
            groups[li].append(a)
        for text, strokes, b in zip(lines, groups, bl):
            if not strokes or not text or any(ch not in alphabet for ch in text):
                continue
            x0 = min(s[:, 0].min() for s in strokes)
            norm = [np.stack([(s[:, 0] - x0) / xh, (b - s[:, 1]) / xh], 1) for s in strokes]
            out.append({"id": d["id"], "text": text, "section": d["section"], "strokes": norm})
    return out


def path_stats(samples):
    down = sum(np.hypot(*np.diff(s, axis=0).T).sum() for e in samples for s in e["strokes"] if len(s) > 1)
    chars = sum(len(e["text"].replace(" ", "")) for e in samples)
    return down / max(chars, 1)


def to_coords(strokes):
    """list of (n,2) -> (N,3) coords with eos flag on each stroke's last point."""
    rows = []
    for s in strokes:
        e = np.zeros((len(s), 1))
        e[-1] = 1
        rows.append(np.hstack([s, e]))
    return np.vstack(rows)


def finish(drawing, strokes, text):
    """strokes (list of (n,2), upright, y up) -> (offsets (<=1200,3) normalised, char ids) or None."""
    if not strokes:
        return None
    coords = to_coords(strokes)
    if len(coords) < 12:
        return None
    coords = drawing.align(coords)
    coords = drawing.denoise(coords)
    off = drawing.coords_to_offsets(coords)
    if len(off) > MAX_PTS:
        return None
    off = drawing.normalize(off)
    if np.any(np.linalg.norm(off[:, :2], axis=1) > 60):
        return None
    enc = drawing.encode_ascii(text)[:MAX_CHARS]
    return off.astype(np.float32), enc


def place(strokes, dx, dy, sx, sy, shear):
    out = []
    for s in strokes:
        x = s[:, 0] * sx + shear * s[:, 1]
        y = s[:, 1] * sy
        out.append(np.stack([x + dx, y + dy], 1))
    return out


def make_line(rng, tokens, step, gap_mean=0.55):
    """Assemble one training line from word tokens. Returns (strokes, text)."""
    n_chars = rng.randint(12, 48)
    picked, total = [], 0
    while total < n_chars:
        t = rng.choice(tokens)
        picked.append(t)
        total += len(t["text"]) + 1
    shear = rng.uniform(-0.18, 0.18)
    line_sx, line_sy = rng.uniform(0.9, 1.1), rng.uniform(0.93, 1.07)
    cursor, strokes, text = 0.0, [], []
    for t in picked:
        w = max(s[:, 0].max() for s in t["strokes"])
        sc = rng.uniform(0.92, 1.08)
        st = place(t["strokes"], cursor, rng.gauss(0, 0.06), line_sx * sc, line_sy * sc, shear)
        strokes += [resample(s, step) for s in st]
        cursor += w * line_sx * sc + max(0.2, rng.gauss(gap_mean, 0.15))
        text.append(t["text"])
    return strokes, " ".join(text)


def build(args):
    data = pathlib.Path(args.data).expanduser()
    out = pathlib.Path(args.out).expanduser()
    drawing = load(pathlib.Path(args.repo).expanduser())
    alphabet = set(drawing.alphabet[1:])
    samples = read_samples(data / "writers" / args.writer / "samples", alphabet)
    print(len(samples), "usable samples (characters outside the model's alphabet are dropped)")
    rng = random.Random(args.seed)

    # path length per character -> spacing that gives IAM's ~29 points per character
    perchar = path_stats([e for e in samples if e["section"] == "words"])
    step = perchar / args.points_per_char
    print(f"pen-down path per char {perchar:.3f} x-heights -> resample step {step:.4f}")

    words = [e for e in samples if e["section"] == "words"]
    extra = [e for e in samples if e["section"] in ("letters", "capitals", "symbols")]
    sentences = [e for e in samples if e["section"] == "sentences"]
    rng.shuffle(words)
    held = words[: args.holdout_words]
    train_words = words[args.holdout_words:]
    held_sent = {s["id"] for s in sentences[:1]}
    train_sent = [s for s in sentences if s["id"] not in held_sent]
    val_sent = [s for s in sentences if s["id"] in held_sent]
    print(f"{len(train_words)} train words, {len(held)} held out, {len(train_sent)} + {len(val_sent)} sentence lines, "
          f"{len(extra)} letters/capitals/symbols")
    print("held out:", [h["text"] for h in held])

    train_tokens = train_words * 3 + extra          # letters and symbols appear, but less than words
    def lines_from(tokens, n):
        res = []
        tries = 0
        while len(res) < n and tries < n * 5:
            tries += 1
            st, tx = make_line(rng, tokens, step)
            r = finish(drawing, st, tx)
            if r:
                res.append(r)
        return res

    def real(sents, variants):
        res = []
        for s in sents:
            for v in range(variants):
                aug = place([resample(a, step) for a in s["strokes"]], 0, 0,
                            rng.uniform(0.95, 1.05) if v else 1, rng.uniform(0.97, 1.03) if v else 1, rng.uniform(-0.1, 0.1) if v else 0)
                r = finish(drawing, aug, s["text"])
                if r:
                    res.append(r)
        return res

    train = lines_from(train_tokens, args.lines) + real(train_sent, args.sentence_variants)
    val = lines_from(held, args.val_lines) + real(val_sent, 4)
    rng.shuffle(train)
    print(len(train), "train lines,", len(val), "val lines")

    for name, rows in (("train", train), ("val", val)):
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        x = np.zeros([len(rows), MAX_PTS, 3], np.float32)
        xl = np.zeros([len(rows)], np.int16)
        c = np.zeros([len(rows), MAX_CHARS], np.int8)
        cl = np.zeros([len(rows)], np.int8)
        for i, (off, enc) in enumerate(rows):
            x[i, :len(off)] = off
            xl[i] = len(off)
            c[i, :len(enc)] = enc
            cl[i] = len(enc)
        for k, v in (("x", x), ("x_len", xl), ("c", c), ("c_len", cl)):
            np.save(d / f"{k}.npy", v)
        wd = np.mean([np.abs(np.cumsum(o[:, 0])).max() / max(len(e) - 1, 1) for o, e in rows])
        print(f"{name}: mean points/char {np.mean([len(o) / max(len(e) - 1, 1) for o, e in rows]):.1f} (IAM 29.1), "
              f"x extent per char {wd:.1f} (IAM ~9.8)")
    (out / "meta.json").write_text(json.dumps({"step": step, "held_out_words": [h["text"] for h in held]}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="directory holding writers/<name>/samples")
    ap.add_argument("--writer", default="default")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", required=True, help="the sjvasquez checkout (for drawing.py)")
    ap.add_argument("--lines", type=int, default=4000)
    ap.add_argument("--val-lines", type=int, default=200)
    ap.add_argument("--holdout-words", type=int, default=16)
    ap.add_argument("--sentence-variants", type=int, default=25)
    ap.add_argument("--points-per-char", type=float, default=29.0)
    ap.add_argument("--seed", type=int, default=7)
    build(ap.parse_args())
