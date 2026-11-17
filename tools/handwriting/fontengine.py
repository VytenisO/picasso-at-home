#!/usr/bin/env python3
"""Font engine: any installed or bundled outline font as pen strokes. Same interface as ReplayEngine in engine.py.

A TrueType outline is drawn as its contours, so every stroke of a letter is drawn twice, once along each edge (a thick
font looks like outlined type). Thin, monoline fonts look best at small sizes. Comic Neue (an open lookalike of Comic Sans,
fonts/) is bundled. Needs fonttools.
"""
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import fonts  # noqa: E402
import plot_svg as ps  # noqa: E402

from fontTools.pens.basePen import BasePen  # noqa: E402
from fontTools.pens.boundsPen import BoundsPen  # noqa: E402
from fontTools.ttLib import TTFont  # noqa: E402

SIMPLIFY_MM = 0.05
SPACE_EM = 1.0       # fallback space width, in x-heights, if the font has no space glyph
CURVE_STEPS = 12


class _Flatten(BasePen):
    """Glyph outline -> list of closed polylines in font units (y up)."""

    def __init__(self, glyphs):
        super().__init__(glyphs)
        self.contours, self.cur = [], []

    def _moveTo(self, p):
        self.cur = [p]

    def _lineTo(self, p):
        self.cur.append(p)

    def _curveToOne(self, p1, p2, p3):
        p0 = self.cur[-1]
        for i in range(1, CURVE_STEPS + 1):
            t = i / CURVE_STEPS
            u = 1 - t
            self.cur.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                             u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))

    def _qCurveToOne(self, p1, p2):
        p0 = self.cur[-1]
        for i in range(1, CURVE_STEPS + 1):
            t = i / CURVE_STEPS
            u = 1 - t
            self.cur.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                             u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))

    def _closePath(self):
        if len(self.cur) > 1:
            self.cur.append(self.cur[0])
            self.contours.append(self.cur)
        self.cur = []

    def _endPath(self):
        if len(self.cur) > 1:
            self.contours.append(self.cur)
        self.cur = []


class FontEngine:
    def __init__(self, family=None, path=None, bold=False, italic=False, fixed_pressure=0.0):
        p = path or fonts.find(family, bold, italic)
        if not p:
            try:
                import svg_vector
                p = svg_vector.font_file(family, "bold" if bold else "normal", "italic" if italic else "normal")
            except Exception:  # noqa: BLE001
                p = None
        if not p or not pathlib.Path(p).exists():
            raise ValueError(f"no font file found for {family!r}")
        self.path = str(p)
        self.font = TTFont(self.path, lazy=True, fontNumber=0)
        self.cmap = self.font.getBestCmap()
        self.glyphs = self.font.getGlyphSet()
        self.hmtx = self.font["hmtx"]
        self.upm = self.font["head"].unitsPerEm
        self.fixed_pressure = fixed_pressure
        self.missing = set()
        xh = getattr(self.font["OS/2"], "sxHeight", 0) if "OS/2" in self.font else 0
        if not xh:
            gn = self.cmap.get(ord("x"))
            if gn:
                bp = BoundsPen(self.glyphs)
                self.glyphs[gn].draw(bp)
                xh = bp.bounds[3] if bp.bounds else 0
        self.xheight = xh or self.upm * 0.5
        self._cache = {}

    def _glyph(self, ch):
        if ch in self._cache:
            return self._cache[ch]
        name = self.cmap.get(ord(ch))
        if name is None:
            self._cache[ch] = None
            return None
        fl = _Flatten(self.glyphs)
        self.glyphs[name].draw(fl)
        res = (self.hmtx[name][0], fl.contours)
        self._cache[ch] = res
        return res

    def word(self, w, xh_mm):
        k = xh_mm / self.xheight
        strokes, x = [], 0.0
        for ch in w:
            g = self._glyph(ch)
            if g is None:
                self.missing.add(ch)
                continue
            adv, contours = g
            for c in contours:
                pts = [(x + px * k, -py * k) for px, py in c]
                pts = [tuple(q) for q in ps.rdp(pts, SIMPLIFY_MM)]
                if len(pts) >= 2:
                    strokes.append({"pts": pts, "p": self.fixed_pressure})
            x += adv * k
        return (strokes, x) if strokes else None

    def layout(self, text, xh_mm, width_mm):
        sp = self._glyph(" ")
        space = (sp[0] if sp else SPACE_EM * self.xheight) * xh_mm / self.xheight
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
            x += wd + space
        if cur:
            lines.append(cur)
        return lines
