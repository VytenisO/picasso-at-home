#!/usr/bin/env python3
"""Single-line text in the Hershey fonts (tools/hershey/, see its README for their origin and licence).

A pen draws a Hershey letter once along its centre line, where an outline font costs two passes round every
stroke. Text is laid out in mm with the font's own advance widths and wrapped to a width.

  hershey.py "Hello there" --font "Hershey Script" --xheight 5 > hello.svg
"""
import argparse
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent / "hershey"
FONTS = {"Hershey Sans": "futural.jhf", "Hershey Sans Medium": "futuram.jhf", "Hershey Script": "scripts.jhf",
         "Hershey Serif": "timesr.jhf"}
_cache = {}


def _parse(path):
    """-> {char: (left, right, strokes)} for ASCII 32..126 in file order; y grows downward (as in SVG)."""
    lines = path.read_text(encoding="ascii").splitlines()
    glyphs, i, code = {}, 0, 32
    while i < len(lines) and code < 127:
        ln = lines[i]
        i += 1
        if len(ln) < 8:
            continue
        n = int(ln[5:8])
        data = ln[8:]
        while len(data) < 2 * n and i < len(lines):      # long records continue on the next lines
            data += lines[i]
            i += 1
        left, right = ord(data[0]) - 82, ord(data[1]) - 82
        strokes, cur = [], []
        for k in range(2, 2 * n, 2):
            pair = data[k:k + 2]
            if pair == " R":
                if len(cur) > 1:
                    strokes.append(cur)
                cur = []
            else:
                cur.append((ord(pair[0]) - 82, ord(pair[1]) - 82))
        if len(cur) > 1:
            strokes.append(cur)
        glyphs[chr(code)] = (left, right, strokes)
        code += 1
    return glyphs


def font(name):
    if name not in FONTS:
        raise ValueError(f"Unknown single-line font {name!r}; there are {', '.join(FONTS)}")
    if name not in _cache:
        g = _parse(HERE / FONTS[name])
        # x-height from the letter x: its top to the baseline (the bottom of x)
        xs = [p for s in g["x"][2] for p in s]
        top, base = min(y for _, y in xs), max(y for _, y in xs)
        _cache[name] = (g, base - top, base)
    return _cache[name]


def lines_of(text, name, xheight, width_mm=None):
    """Wrap text to width_mm (None: no wrapping). -> list of line strings."""
    g, xh_units, _ = font(name)
    k = xheight / xh_units

    def adv(s):
        return sum((g.get(c, g["?"])[1] - g.get(c, g["?"])[0]) * k for c in s)
    out = []
    for para in text.replace("\r", "").split("\n"):
        if not width_mm:
            out.append(para)
            continue
        cur = ""
        for word in para.split(" "):
            trial = (cur + " " + word) if cur else word
            if cur and adv(trial) > width_mm:
                out.append(cur)
                cur = word
            else:
                cur = trial
        out.append(cur)
    return out


def text_strokes(text, name="Hershey Sans", xheight=4.5, width_mm=None, pitch=None, align="left"):
    """-> (polylines in mm with the first baseline at y = 0, width, height). pitch: baseline distance in mm."""
    g, xh_units, base = font(name)
    k = xheight / xh_units
    pitch = pitch or xheight * 3.0
    lines = lines_of(text, name, xheight, width_mm)
    widths = [sum((g.get(c, g["?"])[1] - g.get(c, g["?"])[0]) * k for c in ln) for ln in lines]
    box_w = width_mm if (width_mm and align != "left") else max(widths or [0])
    polys = []
    for n, ln in enumerate(lines):
        x = {"left": 0.0, "center": (box_w - widths[n]) / 2, "right": box_w - widths[n]}[align]
        y0 = n * pitch
        for c in ln:
            left, right, strokes = g.get(c, g["?"])
            for s in strokes:
                polys.append([(round(x + (px - left) * k, 3), round(y0 + (py - base) * k, 3)) for px, py in s])
            x += (right - left) * k
    return polys, box_w, (len(lines) - 1) * pitch


def svg(text, name="Hershey Sans", xheight=4.5, width_mm=None, pitch=None, align="left", color="#000000"):
    """A plottable SVG (mm) of the text, with a little margin. -> (svg text, stats)."""
    polys, w, h = text_strokes(text, name, xheight, width_mm, pitch, align)
    if not polys:
        raise ValueError("Nothing to draw: the text is empty or has no drawable letters.")
    xs = [p[0] for s in polys for p in s]
    ys = [p[1] for s in polys for p in s]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    m = 1.0
    W, H = (x1 - x0) + 2 * m, (y1 - y0) + 2 * m
    d = " ".join("M" + " L".join(f"{x - x0 + m:.3f} {y - y0 + m:.3f}" for x, y in s) for s in polys)
    out = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.3f}mm" height="{H:.3f}mm" viewBox="0 0 {W:.3f} {H:.3f}">'
           f'<path d="{d}" fill="none" stroke="{color}" stroke-width="0.3" stroke-linecap="round" '
           f'stroke-linejoin="round"/></svg>')
    length = sum(math.dist(a, b) for s in polys for a, b in zip(s, s[1:]))
    return out, {"strokes": len(polys), "lines": len(lines_of(text, name, xheight, width_mm)),
                 "size_mm": [round(W, 1), round(H, 1)], "length_mm": round(length, 1)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("--font", default="Hershey Sans", choices=list(FONTS))
    ap.add_argument("--xheight", type=float, default=4.5)
    ap.add_argument("--width", type=float, help="wrap to this width in mm")
    a = ap.parse_args(argv)
    s, _ = svg(a.text, a.font, a.xheight, a.width)
    sys.stdout.write(s)


if __name__ == "__main__":
    main()
