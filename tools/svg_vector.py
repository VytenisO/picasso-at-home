#!/usr/bin/env python3
"""Prepare vector SVGs for the pen plotter (no rasterisation / thresholding).

The main job is turning <text> into real glyph outline paths: fontTools reads
the installed fonts and SVGPathPen emits plain M/L/C/Q paths that the plotter
already understands. Everything else in the SVG is left as vectors.

  from svg_vector import prepare
  stats = prepare("drawing.svg", "drawing_for_plot.svg", font_family="DejaVu Sans")

Requires fonttools (pip install fonttools) for text conversion; if it is not
available the file is still copied and the text elements are reported as
unconverted.
"""
import math
import pathlib
import re
import subprocess
import xml.etree.ElementTree as ET

SVGNS = "http://www.w3.org/2000/svg"
for prefix, uri in (("", SVGNS), ("inkscape", "http://www.inkscape.org/namespaces/inkscape"),
                    ("sodipodi", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"),
                    ("xlink", "http://www.w3.org/1999/xlink")):
    ET.register_namespace(prefix, uri)

_FONT_CACHE = {}
_FONT_FAILED = False

try:
    from fontTools.ttLib import TTFont
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.recordingPen import RecordingPen
except ImportError:  # pragma: no cover
    TTFont = None


def font_file(family, weight="normal", style="normal"):
    """Resolve a CSS-ish font description to an installed font file via fontconfig."""
    global _FONT_FAILED
    if _FONT_FAILED:
        return None
    family = (family or "sans-serif").split(",")[0].strip().strip("'\"") or "sans-serif"
    try:
        import fonts as _fonts   # bundled fonts/ (for example Comic Neue) win over installed ones
        mine = _fonts.find(family, str(weight).lower() in ("bold", "700", "800", "900"),
                           str(style).lower() in ("italic", "oblique"))
        if mine:
            return mine
    except Exception:  # noqa: BLE001
        pass
    bits = [family]
    if str(weight).lower() in ("bold", "700", "800", "900"):
        bits.append("bold")
    if str(style).lower() in ("italic", "oblique"):
        bits.append("italic")
    query = ":style=" + " ".join(bits[1:]) if len(bits) > 1 else family
    if query in _FONT_CACHE:
        return _FONT_CACHE[query]
    try:
        out = subprocess.check_output(["fc-match", "-f", "%{file}", query],
                                      text=True, encoding="utf-8", errors="replace", timeout=5).strip()
    except Exception:  # noqa: BLE001
        out = ""
    _FONT_CACHE[query] = out or None
    return _FONT_CACHE[query]


def style_of(el):
    d = {}
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            d[k.strip().lower()] = v.strip()
    for k in ("font-family", "font-size", "font-weight", "font-style", "text-anchor", "letter-spacing"):
        if el.get(k):
            d[k] = el.get(k)
    return d


def _length(s, default=12.0):
    m = re.match(r"\s*(-?[\d.]+)", str(s or ""))
    return float(m.group(1)) if m else default


length = _length   # public alias used by plot_svg.py


def text_to_d(text, x, y, size, family, weight, style, anchor, spacing):
    """Return SVG path data for a string of text, positioned in user units."""
    path = font_file(family, weight, style)
    if not path or not text:
        return None
    try:
        font = TTFont(path, lazy=True)
        cmap = font.getBestCmap()
        glyphs = font.getGlyphSet()
        upm = font["head"].unitsPerEm
        scale = size / upm
        hmtx = font["hmtx"]
        names = [cmap.get(ord(c), ".notdef") for c in text]
        widths = [hmtx[n][0] * scale for n in names]
        total = sum(widths) + spacing * max(0, len(names) - 1)
        if anchor == "middle":
            x -= total / 2
        elif anchor == "end":
            x -= total
        pieces, adv = [], 0.0
        for name, width in zip(names, widths):
            pen = SVGPathPen(glyphs)
            glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, x + adv, y)))
            pieces.append(pen.getCommands())
            adv += width + spacing
        font.close()
        return " ".join(p for p in pieces if p) or None
    except Exception:  # noqa: BLE001
        return None


def text_art_svg(text, size=20.0, family="DejaVu Sans", weight="normal", style="normal",
                 effect="arc", bend=40):
    """Text as deformed vector outlines. -> (svg string, stats).

    `effect` is one of plain / arc / wave / rise / circle / twist and `bend`
    (0..100) controls how strong the deformation is.
    """
    if TTFont is None:
        raise RuntimeError("fonttools is required for text art")
    text = (text or "").strip()[:200]
    if not text:
        raise ValueError("no text given")
    path = font_file(family, weight, style)
    if not path:
        raise ValueError("could not find a font for " + str(family))
    font = TTFont(path, lazy=True)
    cmap = font.getBestCmap()
    glyphs = font.getGlyphSet()
    upm = font["head"].unitsPerEm
    scale = float(size) / upm
    hmtx = font["hmtx"]
    names = [cmap.get(ord(c), ".notdef") for c in text]
    widths = [hmtx[n][0] * scale for n in names]
    total = sum(widths)
    if total <= 0:
        raise ValueError("text has no outline")
    angle = math.radians(bend * 1.8)
    radius = total / angle if angle > 1e-6 else total
    wave_amp = bend * 0.25
    twist_k = math.radians(bend * 3.0)

    def deform(x, y):
        u = x / total
        t = u - 0.5
        if effect == "arc" and angle > 1e-6:
            th = t * angle
            return (radius * math.sin(th), radius - radius * math.cos(th) + y)
        if effect == "circle":
            th = u * 2 * math.pi * max(0.1, bend / 100.0)
            r = total / max(0.1, bend / 100.0) / (2 * math.pi)
            return (r * math.sin(th), r - r * math.cos(th) + y)
        if effect == "wave":
            return (x, y + wave_amp * math.sin(u * 4 * math.pi))
        if effect == "rise":
            return (x, y + t * bend * 0.8)
        if effect == "twist":
            gx = x - total / 2
            gy = y + size * 0.3
            a = t * twist_k
            return (gx * math.cos(a) - gy * math.sin(a) + total / 2,
                    gx * math.sin(a) + gy * math.cos(a) - size * 0.3)
        return (x, y)

    paths, points = [], []

    def note(p):
        if p is not None:
            points.append(p)
        return p

    adv = 0.0
    for name, width in zip(names, widths):
        rec = RecordingPen()
        glyphs[name].draw(TransformPen(rec, (scale, 0, 0, -scale, adv, 0)))
        out = SVGPathPen(glyphs)
        for op, pts in rec.value:
            warped = tuple(note(deform(p[0], p[1])) if p is not None else None for p in pts)
            if op in ("closePath", "endPath"):
                getattr(out, op)()
            else:
                getattr(out, op)(*warped)
        d = out.getCommands()
        if d:
            paths.append(d)
        adv += width
    font.close()
    if not points:
        raise ValueError("text outlines were empty")
    pad = size * 0.15
    minx, maxx = min(p[0] for p in points), max(p[0] for p in points)
    miny, maxy = min(p[1] for p in points), max(p[1] for p in points)
    vw, vh = maxx - minx + 2 * pad, maxy - miny + 2 * pad
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw:.2f}mm" height="{vh:.2f}mm" '
           f'viewBox="{minx - pad:.2f} {miny - pad:.2f} {vw:.2f} {vh:.2f}">\n'
           + "\n".join(f'<path d="{d}" style="fill:none;stroke:#000000"/>' for d in paths)
           + "\n</svg>\n")
    return svg, {"glyphs": len(text), "paths": len(paths), "effect": effect,
                 "width_mm": round(vw, 2), "height_mm": round(vh, 2)}


def prepare(src, dst, font_family="DejaVu Sans"):
    """Copy an SVG to dst with <text> replaced by outline paths. -> stats dict."""
    stats = {"text_elements": 0, "text_glyphs": 0, "font": font_family, "converted": False}
    try:
        tree = ET.parse(src)
    except ET.ParseError as ex:
        raise ValueError(f"invalid SVG: {ex}")
    root = tree.getroot()
    changed = False

    def walk(el, inherited):
        nonlocal changed
        st = dict(inherited)
        st.update(style_of(el))
        if el.tag == f"{{{SVGNS}}}text" or el.tag == "text":
            text = "".join(el.itertext())
            if text.strip():
                x = _length(el.get("x", "0"), 0.0)
                y = _length(el.get("y", "0"), 0.0)
                d = text_to_d(text, x, y, _length(st.get("font-size")),
                              st.get("font-family", font_family),
                              st.get("font-weight", "normal"), st.get("font-style", "normal"),
                              st.get("text-anchor", "start"), _length(st.get("letter-spacing"), 0.0))
                stats["text_elements"] += 1
                if d:
                    stats["text_glyphs"] += len(text)
                    changed = True
                    path = ET.Element(f"{{{SVGNS}}}path")
                    path.set("d", d)
                    if el.get("transform"):
                        path.set("transform", el.get("transform"))
                    path.set("style", "fill:none;stroke:#000000")
                    parent = None
                    for candidate in root.iter():
                        if el in list(candidate):
                            parent = candidate
                            break
                    if parent is not None:
                        parent.insert(list(parent).index(el), path)
                        parent.remove(el)
                    return
        for child in list(el):
            walk(child, st)

    walk(root, {})
    stats["converted"] = changed
    tree.write(dst, encoding="utf-8", xml_declaration=True)
    return stats


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--font", default="DejaVu Sans")
    a = ap.parse_args(argv)
    s = prepare(a.src, a.dst, a.font)
    print(f"Wrote {a.dst}: {s['text_elements']} text element(s), "
          f"{s['text_glyphs']} glyph(s) outlined, font={s['font']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
