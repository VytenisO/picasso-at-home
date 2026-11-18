#!/usr/bin/env python3
"""Render the evaluation sheets for a v2 model (numpy only).

  python evaluate.py --model DIR --samples SAMPLES_DIR --prompts prompts.json --out DIR/renders [--bias 1.0] [--seed 1]

Writes:
  heldout_words.png   each held-out word: the real sample (never trained on), then three generated attempts
  sentences.png       the held-out sentence (real, then generated) and five unseen sentences
  seen_words.png      words that were in the training set, generated (memorisation check)
  bias.png            one word at several bias values
"""
import argparse
import os
import pathlib
import sys

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
import numpy as np  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import data as D  # noqa: E402
import render  # noqa: E402
from generate import NeuralEngine  # noqa: E402

SENTENCES = [
    "Thank you for the lovely dinner.",
    "The plotter writes slowly but well.",
    "Kjære deg, vi ses på fredag ved sjøen.",
    "Call me at 4:15 tomorrow, Peter!",
    "Write any word you like here.",
]


def gen_line(eng, text, width_xh=40.0, xh_mm=10.0):
    lines = eng.layout(text, xh_mm, width_xh * xh_mm)
    return [[np.array(s["pts"]) / xh_mm for s in ln] for ln in lines]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--samples", required=True)
    ap.add_argument("--prompts")
    ap.add_argument("--out", required=True)
    ap.add_argument("--bias", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tag", default="")
    ap.add_argument("--domain", type=float, default=D.REAL,
                    help="2 real hand (default), 1 stitched letters, 0 Hershey; fractional values blend")
    ap.add_argument("--quick", action="store_true", help="held-out words and sentences only, one attempt per word")
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    eng = NeuralEngine(a.model, bias=a.bias, seed=a.seed, domain=a.domain)
    samples = D.load_samples(a.samples, a.prompts)
    train, held = D.split(samples)
    t = a.tag

    rows = []
    for s in [x for x in held if x["section"] == "words"]:
        rows.append((f"real: {s['text']}", s["strokes"]))
        for k in range(1 if a.quick else 3):
            rows.append((f"gen {k + 1}", eng.word_xh(s["text"])))
    render.rows_png(rows, out / f"heldout_words{t}.png", label_w=150)

    rows = []
    for s in [x for x in held if x["section"] == "sentences"]:
        rows.append((f"real (held out): {s['text']}", s["strokes"]))
        for ln in gen_line(eng, s["text"]):
            rows.append(("gen", ln))
    for text in SENTENCES:
        for i, ln in enumerate(gen_line(eng, text)):
            rows.append((f"gen: {text}" if i == 0 else "", ln))
    render.rows_png(rows, out / f"sentences{t}.png", label_w=10)
    if a.quick:
        print(f"wrote renders to {out}")
        return

    rows = []
    for s in [x for x in train if x["section"] == "words"][::12]:
        rows.append((f"real: {s['text']}", s["strokes"]))
        rows.append(("gen", eng.word_xh(s["text"])))
    render.rows_png(rows, out / f"seen_words{t}.png", label_w=150)

    rows = []
    for b in (0.0, 0.5, 1.0, 1.5, 2.5):
        eng.bias = b
        for k in range(2):
            rows.append((f"bias {b}", eng.word_xh("handwriting")))
    render.rows_png(rows, out / f"bias{t}.png", label_w=150)
    print(f"wrote renders to {out}")


if __name__ == "__main__":
    main()
