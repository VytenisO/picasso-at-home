#!/usr/bin/env python3
"""Handwriting engine: text in, pen strokes out, in the user's own hand (stdlib only).

This is the replay engine, the first of the two planned engines. It reads the samples written by capture_server.py
(handwriting/samples/*.json) and builds a line of text from them:
  - a word that was written as a prompt is replayed exactly as written (normalised to a common x-height and baseline);
  - any other word is stitched from single letters written in the letters, capitals and symbols sections, with
    plain gaps and no joins (a trained model replaces this and will join letters; the interface stays the same).

Interface for every engine:  Engine.layout(text, xh_mm, width_mm) -> list of lines, each a list of strokes
  stroke = {"pts": [(x, y), ...], "p": pressure fraction or None}     mm, x right from the line start,
  y down from the baseline (ascenders are negative), strokes in writing order.
"""
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import plot_svg as ps  # noqa: E402  (for rdp, so strokes are simplified like every other plot)

SPACE_EM = 0.7      # word space, in x-heights
GAP_EM = 0.22       # gap between stitched letters, in x-heights
SIMPLIFY_MM = 0.05


def _bounds(stroke):
    xs = [p[0] for p in stroke]
    return min(xs), max(xs)


def _groups(strokes, tol):
    """Strokes whose x ranges overlap (within tol) belong to the same glyph; an i dot joins its stem."""
    ranges = sorted(((_bounds(s), s) for s in strokes), key=lambda r: r[0][0])
    out = []
    for (lo, hi), s in ranges:
        if out and lo <= out[-1]["hi"] + tol:
            out[-1]["hi"] = max(out[-1]["hi"], hi)
            out[-1]["strokes"].append(s)
        else:
            out.append({"lo": lo, "hi": hi, "strokes": [s]})
    return out


class ReplayEngine:
    def __init__(self, samples_dir, prompts_path=None, sample_pressure=False, fixed_pressure=0.0):
        self.words, self.glyphs = {}, {}
        self.sample_pressure, self.fixed_pressure = sample_pressure, fixed_pressure
        self.missing = set()
        d = pathlib.Path(samples_dir)
        prompts = None
        if prompts_path and pathlib.Path(prompts_path).exists():
            prompts = {p["id"]: p["text"] for p in json.loads(pathlib.Path(prompts_path).read_text(encoding="utf-8"))["prompts"]}
        for f in sorted(d.glob("[a-z][0-9][0-9][0-9].json")):
            try:
                s = json.loads(f.read_text(encoding="utf-8"))
                if prompts is not None and prompts.get(s["id"]) != s["text"]:
                    continue                         # prompt list was rebuilt: this id means something else now
                self._add(s)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if not self.words and not self.glyphs:
            raise SystemExit(f"no handwriting samples in {d}; write some with tools/handwriting/capture_server.py first")

    # ---- loading
    def _norm(self, sample, strokes):
        """CSS px strokes -> mm-less units: x from the left of the group, y from the first baseline, scaled so the
        x-height is 1.0. Returns [(pts, pressure_mean)] with pts in x-height units."""
        g = sample["guide"]
        xh = float(g["x_height"])
        base = float(g["baselines"][0])
        out = []
        for st in strokes:
            pts = [((p[0]), (p[1] - base)) for p in st]
            pr = sum(p[3] for p in st) / len(st)
            out.append((pts, pr))
        return out, xh

    def _unit(self, sample, group):
        norm, xh = self._norm(sample, group)
        x0 = min(p[0] for pts, _ in norm for p in pts)
        res = [([((x - x0) / xh, y / xh) for x, y in pts], pr) for pts, pr in norm]
        width = max(x for pts, _ in res for x, _ in pts)
        return {"strokes": res, "width": width}

    def _add(self, s):
        sec, text = s["section"], s["text"]
        strokes = s["strokes"]
        xh = float(s["guide"]["x_height"])
        if sec == "words" and s.get("lines", 1) == 1:
            self.words[text] = self._unit(s, strokes)
        elif sec in ("letters", "capitals", "symbols"):
            grouped = [gr["strokes"] for gr in _groups(strokes, 0.15 * xh)]
            tokens = text.split()
            if sec == "symbols":
                if len(grouped) == len(tokens):
                    for tok, gr in zip(tokens, grouped):
                        self.glyphs.setdefault(tok, self._unit(s, gr))
            elif grouped and tokens:
                self.glyphs[tokens[0]] = self._unit(s, grouped[0])    # the first of the three separate letters

    # ---- building
    def _strokes_of(self, unit, xh_mm, dx):
        out = []
        for pts, pr in unit["strokes"]:
            mm = [(dx + x * xh_mm, y * xh_mm) for x, y in pts]
            if len(mm) == 1:
                mm = [mm[0], (mm[0][0] + 0.2, mm[0][1])]
            mm = [tuple(p) for p in ps.rdp(mm, SIMPLIFY_MM)]
            if self.sample_pressure:
                frac = min(0.45, max(0.0, 0.5 * (1.0 - pr)))
            else:
                frac = self.fixed_pressure
            out.append({"pts": mm, "p": frac})
        return out

    def word(self, w, xh_mm):
        """-> (strokes starting at x=0, width_mm) or None if nothing in the word can be written."""
        if w in self.words:
            u = self.words[w]
            return self._strokes_of(u, xh_mm, 0.0), u["width"] * xh_mm
        strokes, x = [], 0.0
        for ch in w:
            u = self.glyphs.get(ch)
            if u is None:
                self.missing.add(ch)
                continue
            strokes += self._strokes_of(u, xh_mm, x)
            x += u["width"] * xh_mm + GAP_EM * xh_mm
        return (strokes, max(0.0, x - GAP_EM * xh_mm)) if strokes else None

    def layout(self, text, xh_mm, width_mm):
        lines, cur, x = [], [], 0.0
        for w in text.split():
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
            x += wd + SPACE_EM * xh_mm
        if cur:
            lines.append(cur)
        return lines

    def can_write(self, ch):
        return ch.isspace() or ch in self.glyphs
