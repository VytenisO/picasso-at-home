#!/usr/bin/env python3
"""Write text in the user's hand with the v2 model. numpy + Pillow only (no torch).

  python generate.py "some text"                     -> renders/generated.json + .png in the model directory
  python generate.py "some text" --out /tmp/x --xh 5 --width 180 --bias 1.0 --seed 3
  python generate.py --text-file notes.txt --units xh

Output JSON: {"text", "units", "xh_mm", "lines": [[{"pts": [[x, y], ...], "p": pressure}, ...], ...]}. In mm (the
default) it is exactly what engine.ReplayEngine.layout returns: x right from the line start, y down from the baseline
(ascenders negative), strokes in writing order. With --units xh the numbers are in x-heights instead.

Characters the model was not trained on: --on-missing substitute (default) replaces accented letters and typographic
quotes and dashes by their plain form and says so on stderr; a character with no substitute is an error.
--on-missing error refuses any unknown character; --on-missing skip drops them (and says which).
"""
import argparse
import json
import os
import pathlib
import sys
import unicodedata

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")      # tiny matrix-vector products: threads only add overhead
import numpy as np  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(HERE))
import data as D  # noqa: E402
from infer import NumpySynth  # noqa: E402

DEFAULT_MODEL = ROOT / "handwriting" / "writers" / "default" / "model" / "v2"
SPACE_EM = 0.75
SIMPLIFY_MM = 0.05
SUBST = {"‘": "'", "’": "'", "‚": ",", "“": '"', "”": '"', "„": '"', "«": '"',
         "»": '"', "–": "-", "—": "-", "−": "-", "…": "...", " ": " ", "\t": " ",
         "ä": "æ", "Ä": "Æ", "ö": "ø", "Ö": "Ø", "*": "x", "×": "x"}


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    a, b = np.asarray(pts[0]), np.asarray(pts[-1])
    ab = b - a
    n = np.linalg.norm(ab)
    P = np.asarray(pts)
    d = np.abs(ab[0] * (P[:, 1] - a[1]) - ab[1] * (P[:, 0] - a[0])) / n if n > 1e-12 else np.linalg.norm(P - a, axis=1)
    i = int(np.argmax(d))
    if d[i] > eps:
        return rdp(pts[:i + 1], eps)[:-1] + rdp(pts[i:], eps)
    return [pts[0], pts[-1]]


class NeuralEngine:
    """Same interface as engine.ReplayEngine: word(w, xh_mm) and layout(text, xh_mm, width_mm)."""

    def __init__(self, model_dir=DEFAULT_MODEL, bias=1.0, seed=None, on_missing="substitute", fixed_pressure=0.0,
                 tries=6, style=1.0, domain=None):
        self.net = NumpySynth(model_dir)
        self.bias, self.on_missing, self.fixed_pressure, self.tries = bias, on_missing, fixed_pressure, tries
        # style 1.0 = the real hand only; lower values blend in the style learned from the stitched isolated letters,
        # which is stiffer but spells unseen words more reliably. domain overrides it (0 Hershey, 1 stitched, 2 real).
        self.domain = domain if domain is not None else 1.0 + float(style)
        self.rng = np.random.default_rng(seed)
        self.missing, self.substituted = set(), {}

    def can_write(self, ch):
        return ch == " " or ch in self.net.writable

    def clean(self, text):
        out = []
        for ch in text:
            if ch == "\n" or self.can_write(ch):
                out.append(ch)
                continue
            if self.on_missing == "error":
                raise ValueError(f"the v2 model cannot write {ch!r} (U+{ord(ch):04X}); it knows: {''.join(sorted(self.net.writable))}")
            if self.on_missing == "skip":
                self.missing.add(ch)
                continue
            rep = SUBST.get(ch)
            if rep is None:
                base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
                rep = base if base and all(self.can_write(c) for c in base) else None
            if rep is None or not all(self.can_write(c) for c in rep):
                raise ValueError(f"the v2 model cannot write {ch!r} (U+{ord(ch):04X}) and has no substitute for it")
            self.substituted[ch] = rep
            out.append(rep)
        return "".join(out)

    def word_xh(self, w):
        """One word -> strokes in x-heights (x from 0, y down from baseline). Retries implausible samples."""
        best = None
        for _ in range(self.tries):
            seq, ended = self.net.run(w, domain=self.domain, bias=self.bias, rng=self.rng)
            if not len(seq):
                continue
            st = D.decode(seq.astype(np.float32))
            st = D.shift_left(st)
            (x0, y0), (x1, y1) = D.bounds(st)
            width = x1 - x0
            ok = ended and width <= 1.0 + 1.6 * len(w) and width >= 0.25 * len(w) and y0 > -3.2 and y1 < 2.6
            score = (ok, ended, -abs(width - 0.9 * len(w)))
            if best is None or score > best[0]:
                best = (score, st)
            if ok:
                break
        return best[1] if best else []

    def word(self, w, xh_mm):
        w = self.clean(w)
        if not w.strip():
            return None
        st = self.word_xh(w)
        if not st:
            return None
        width = max(float(s[:, 0].max()) for s in st) * xh_mm
        out = []
        for s in st:
            pts = [(float(x) * xh_mm, float(y) * xh_mm) for x, y in s]
            if len(pts) == 1:
                pts = [pts[0], (pts[0][0] + 0.2, pts[0][1])]
            out.append({"pts": rdp(pts, SIMPLIFY_MM), "p": self.fixed_pressure})
        return out, width

    def layout(self, text, xh_mm, width_mm):
        text = self.clean(text)
        lines = []
        for para in text.split("\n"):
            cur, x = [], 0.0
            for w in para.split():
                built = self.word(w, xh_mm)
                if built is None:
                    continue
                strokes, wd = built
                if cur and x + wd > width_mm:
                    lines.append(cur)
                    cur, x = [], 0.0
                if wd > width_mm:
                    raise ValueError(f"the word {w!r} is {wd:.0f} mm wide, wider than the {width_mm:.0f} mm line; use a smaller size")
                for st in strokes:
                    cur.append({"pts": [(px + x, py) for px, py in st["pts"]], "p": st["p"]})
                x += wd + SPACE_EM * xh_mm * float(self.rng.uniform(0.85, 1.15))
            if cur:
                lines.append(cur)
        return lines


def render_lines(lines, xh_mm, path, label=""):
    import render
    rows = []
    for i, ln in enumerate(lines):
        rows.append((label if i == 0 else "", [np.array(s["pts"]) / xh_mm for s in ln]))
    return render.rows_png(rows, path, label_w=0 if not label else 10)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?")
    ap.add_argument("--text-file")
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--out", help="output prefix (writes .json and .png); default <model>/renders/generated")
    ap.add_argument("--xh", type=float, default=5.0, help="x-height in mm")
    ap.add_argument("--width", type=float, default=180.0, help="line width in mm")
    ap.add_argument("--bias", type=float, default=1.0, help="neatness: 0 = raw samples, higher = tidier, less varied")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--style", type=float, default=1.0,
                    help="1 = real hand only, 0 = stiffer style of the isolated letters (more legible); blend between")
    ap.add_argument("--units", choices=["mm", "xh"], default="mm")
    ap.add_argument("--on-missing", choices=["substitute", "error", "skip"], default="substitute")
    a = ap.parse_args()
    text = pathlib.Path(a.text_file).read_text(encoding="utf-8") if a.text_file else a.text
    if not text:
        ap.error("give text or --text-file")
    eng = NeuralEngine(a.model, bias=a.bias, seed=a.seed, on_missing=a.on_missing, style=a.style)
    try:
        lines = eng.layout(text, a.xh, a.width)
    except ValueError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    if eng.substituted:
        print("substituted: " + ", ".join(f"{k!r}->{v!r}" for k, v in eng.substituted.items()), file=sys.stderr)
    if eng.missing:
        print("skipped (not in the model): " + "".join(sorted(eng.missing)), file=sys.stderr)
    out = pathlib.Path(a.out) if a.out else pathlib.Path(a.model) / "renders" / "generated"
    out.parent.mkdir(parents=True, exist_ok=True)
    k = 1.0 if a.units == "mm" else 1.0 / a.xh
    js = {"text": text, "units": a.units, "xh_mm": a.xh, "engine": "v2",
          "lines": [[{"pts": [[round(x * k, 4), round(y * k, 4)] for x, y in s["pts"]], "p": s["p"]} for s in ln]
                    for ln in lines]}
    out.with_suffix(".json").write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")
    render_lines(lines, a.xh, str(out.with_suffix(".png")))
    print(f"wrote {out.with_suffix('.json')} and {out.with_suffix('.png')} ({len(lines)} line(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
