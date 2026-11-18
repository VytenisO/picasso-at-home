#!/usr/bin/env python3
"""Write text with a (fine-tuned or pretrained) sjvasquez/handwriting-synthesis checkpoint.

Run from inside the TF2-ported sjvasquez checkout with tfenv.sh sourced:

  python generate.py --ckpt ~/hw-train/ft1 --out ~/hw-train/out/ft1 "hello world" "the quick brown fox"
  python generate.py --ckpt checkpoints --step 17900 --out ~/hw-train/out/base "hello world"      # pretrained, not yours

Each text argument is one line (at most 75 characters from the model's alphabet: a-z, A-Z without Q X Z, digits,
space and ! " # ' ( ) , - . : ; ?). Writes <out>/lines.json and <out>/lines.png. Strokes in lines.json are in
x-heights, x right from the line start, y down from the estimated baseline, in writing order.
"""
import argparse
import json
import logging
import os
import pathlib
import sys

import numpy as np

sys.path.insert(0, os.getcwd())
import drawing  # noqa: E402
from rnn import rnn  # noqa: E402


def load(ckpt, step):
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"
    nn = rnn(
        log_dir="logs_gen", checkpoint_dir=str(pathlib.Path(ckpt).expanduser()), prediction_dir="pred_gen",
        learning_rates=[1e-4], batch_sizes=[32], patiences=[1500], beta1_decays=[.9], validation_batch_size=32,
        optimizer="rms", num_training_steps=100000, warm_start_init_step=0, regularization_constant=0.0,
        keep_prob=1.0, enable_parameter_averaging=False, min_steps_to_checkpoint=2000, log_interval=20,
        logging_level=logging.CRITICAL, grad_clip=10, lstm_size=400, output_mixture_components=20,
        attention_mixture_components=10,
    )
    nn.restore(step)
    return nn


def sample(nn, lines, bias):
    for ln in lines:
        bad = sorted({c for c in ln if c not in drawing.alphabet})
        if bad or len(ln) > 75:
            raise ValueError(f"cannot write {ln!r}: characters outside the model's alphabet {bad} or longer than 75")
    n = len(lines)
    chars = np.zeros([n, 120])
    clen = np.zeros([n])
    for i, ln in enumerate(lines):
        e = drawing.encode_ascii(ln)
        chars[i, :len(e)] = e
        clen[i] = len(e)
    out, attn = nn.session.run([nn.sampled_sequence, nn.sampled_attn], feed_dict={
        nn.prime: False, nn.x_prime: np.zeros([n, 1200, 3]), nn.x_prime_len: np.zeros([n]),
        nn.num_samples: n, nn.sample_tsteps: 40 * max(len(x) for x in lines), nn.c: chars, nn.c_len: clen,
        nn.bias: [bias] * n,
    })
    keep = [~np.all(s == 0.0, axis=1) for s in out]
    return [s[k] for s, k in zip(out, keep)], [a[:len(k)][k] for a, k in zip(attn, keep)]


def to_strokes(offsets, unit, attn=None):
    """normalised offsets -> list of strokes [(x, y)] in x-heights, y down, baseline at 0 (and, with attn, the
    attended character index of every point of every stroke)."""
    c = drawing.offsets_to_coords(offsets.copy())
    pts = c[:, :2] * unit
    ends = np.where(c[:, 2] == 1)[0]
    idx = [i for i, p in enumerate(np.split(pts, ends + 1)) if len(p)]
    parts = [p for p in np.split(pts, ends + 1) if len(p)]
    att = np.split(attn if attn is not None else np.zeros(len(pts), int), ends + 1)
    att = [att[i] for i in idx]
    allp = np.vstack(parts)
    base = np.percentile(allp[:, 1], 20)                 # y is still up here; most ink sits above the baseline
    x0 = allp[:, 0].min()
    strokes = [[(float(x - x0), float(base - y)) for x, y in p] for p in parts]
    return (strokes, att) if attn is not None else strokes


def straighten(strokes, degree=2, strength=1.0, bin_w=1.0):
    """Level the line: fit the baseline through the bottoms of the letters and subtract it (strength 0 = off, 1 = fully
    flat). Returns (strokes, rms of the baseline before, after) in x-heights."""
    pts = np.array([p for s in strokes for p in s])
    if strength <= 0 or len(pts) < 20:
        return strokes, 0.0, 0.0
    bins = np.floor(pts[:, 0] / bin_w).astype(int)
    xs, ys = [], []
    for b in np.unique(bins):
        m = pts[bins == b]
        if len(m) >= 6:
            xs.append(m[:, 0].mean())
            ys.append(np.percentile(m[:, 1], 80))     # y is down: the bottoms of the letters (descenders sit lower still)
    xs, ys = np.array(xs), np.array(ys)
    if len(xs) < 4:
        return strokes, 0.0, 0.0
    deg = min(degree, len(xs) - 2)
    co = np.polyfit(xs, ys, deg)
    for _ in range(2):                                  # drop bins that are descenders, refit
        keep = np.abs(ys - np.polyval(co, xs)) < 0.35
        if keep.sum() >= deg + 2:
            co = np.polyfit(xs[keep], ys[keep], deg)
    before = float(np.sqrt(np.mean((np.polyval(co, xs) - np.polyval(co, xs).mean()) ** 2)))
    adj = [[(x, y - strength * (float(np.polyval(co, x)) - float(np.polyval(co, 0)))) for x, y in s] for s in strokes]
    after = float(np.sqrt(np.mean((np.polyval(co, xs) * (1 - strength) - (np.polyval(co, xs) * (1 - strength)).mean()) ** 2)))
    return adj, before, after


def _plen(s):
    return sum(float(np.hypot(b[0] - a[0], b[1] - a[1])) for a, b in zip(s, s[1:]))


def calibrate(samples_dir):
    """Where the writer puts i/j dots and t crosses, from the isolated-letter samples ('i i i', 'j j j', 't t t').

    Returns {"dots": [(dx, dy)], "crosses": [[(dx, dy), ...]]}: offsets in x-heights from the top of the letter's stem
    (dy positive = up). Computed from private samples at run time, never stored in code."""
    out = {"dots": [], "crosses": []}
    for ch in "ijt":
        for f in sorted(pathlib.Path(samples_dir).expanduser().glob("l0*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            if d["text"] != f"{ch} {ch} {ch}":
                continue
            xh = float(d["guide"]["x_height"])
            S = [np.asarray(s, float)[:, :2] for s in d["strokes"]]
            L = [float(np.hypot(*np.diff(a, axis=0).T).sum()) if len(a) > 1 else 0.0 for a in S]
            order = np.argsort(L)[::-1]
            mains, others = order[:3], order[3:]
            for o in others:
                a = S[o]
                m = min(mains, key=lambda k: abs(S[k][:, 0].mean() - a[:, 0].mean()))
                top = S[m][np.argmin(S[m][:, 1])]
                rel = [((x - top[0]) / xh, (top[1] - y) / xh) for x, y in a]
                if ch == "t":
                    k = np.linspace(0, len(rel) - 1, 10).round().astype(int)
                    out["crosses"].append([rel[i] for i in k])
                else:
                    out["dots"].append((float(np.mean([r[0] for r in rel])), float(np.mean([r[1] for r in rel]))))
    return out


def add_details(text, strokes, att, calib, rng):
    """The model rarely crosses a t and sometimes forgets an i dot. Drop what it drew there and put the writer's own
    dot or cross on every i, j and t, placed from the model's attention (where it was writing that letter)."""
    if not calib["dots"] and not calib["crosses"]:
        return strokes
    def char_of(a):
        a = a[a < len(text)]
        return text[int(np.bincount(a).argmax())] if len(a) else ""
    keep, hist = [], []
    for s, a in zip(strokes, att):
        L = _plen(s)
        ys = [p[1] for p in s]
        small = L < 0.35 or (L < 1.7 and max(ys) - min(ys) < 0.5 and max(p[0] for p in s) - min(p[0] for p in s) > 0.25)
        if small and char_of(a) in "ijt":
            continue
        keep.append(s)
        hist.append(a)
    out = list(keep)
    added = []
    for k, ch in enumerate(text):
        if ch not in "ijt":
            continue
        pts = [p for s, a in zip(keep, hist) for p, ai in zip(s, a) if ai == k]
        if len(pts) < 3:
            continue
        top = min(pts, key=lambda p: p[1])               # y is down: the smallest y is the top of the letter
        if ch == "t" and calib["crosses"]:
            cr = rng.choice(calib["crosses"])
            out.append([(top[0] + dx, top[1] - dy) for dx, dy in cr])
            added.append(out[-1])
        elif ch in "ij" and calib["dots"]:
            dx, dy = rng.choice(calib["dots"])
            x, y = top[0] + dx, top[1] - dy
            out.append([(x, y), (x + 0.06, y + 0.06)])   # a real tick, so a plotter lowers and lifts the pen
            added.append(out[-1])
    # the model sometimes drew its own dot or cross for a letter it later attended elsewhere: drop what duplicates ours
    def centre(s):
        return (sum(p[0] for p in s) / len(s), sum(p[1] for p in s) / len(s))
    def small(s):
        ys = [p[1] for p in s]
        return _plen(s) < 0.7 and max(ys) - min(ys) < 0.5
    final = []
    for s in out:
        if s not in added and small(s):
            cx, cy = centre(s)
            if any(abs(cx - centre(a)[0]) < 0.8 and abs(cy - centre(a)[1]) < 0.6 for a in added):
                continue
        final.append(s)
    return final


def render(path, lines, strokes):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(len(lines), 1, figsize=(11, 1.5 * len(lines)), squeeze=False)
    for ax, ln, st in zip(axes[:, 0], lines, strokes):
        for s in st:
            xs, ys = zip(*s)
            ax.plot(xs, [-y for y in ys], "k", lw=1.2, solid_capstyle="round")
            if _plen(s) < 0.2:
                ax.plot(xs[0], -ys[0], "ko", ms=2.2)
        ax.set_aspect("equal")
        ax.axis("off")
        ax.set_title(ln, fontsize=8, loc="left", color="#888")
    fig.tight_layout()
    fig.savefig(path, dpi=100)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lines", nargs="+")
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--step", type=int, help="checkpoint step (default: the latest in --ckpt)")
    ap.add_argument("--bias", type=float, default=0.75, help="higher = neater, less varied (default %(default)s)")
    ap.add_argument("--unit", type=float, help="x-heights per model unit (default: step from v1data/meta.json)")
    ap.add_argument("--meta", default="~/hw-train/v1data/meta.json")
    ap.add_argument("--straighten", type=float, default=1.0, help="0 = leave the baseline as the model wrote it, 1 = level (default %(default)s)")
    ap.add_argument("--curve", type=int, default=2, help="1 = only remove tilt, 2 = also remove one bend (default %(default)s)")
    ap.add_argument("--samples", help="the writer's samples directory: adds dots on i/j and crosses on t in their style")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    unit = a.unit
    if unit is None:
        mp = pathlib.Path(a.meta).expanduser()
        unit = json.loads(mp.read_text())["step"] if mp.exists() else 0.12
    nn = load(a.ckpt, a.step)
    offs, attn = sample(nn, a.lines, a.bias)
    import random
    calib = calibrate(a.samples) if a.samples else {"dots": [], "crosses": []}
    rng = random.Random(1)
    st = []
    for tx, o, at in zip(a.lines, offs, attn):
        s, att = to_strokes(o, unit, at)
        s, before, after = straighten(s, a.curve, a.straighten)
        print(f"{tx!r}: baseline wander {before:.2f} -> {after:.2f} x-heights (rms)")
        st.append(add_details(tx, s, att, calib, rng))
    out = pathlib.Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "lines.json").write_text(json.dumps({"unit": "x_height", "bias": a.bias,
                                                "lines": [{"text": t, "strokes": s} for t, s in zip(a.lines, st)]}))
    render(out / "lines.png", a.lines, st)
    print("wrote", out)
