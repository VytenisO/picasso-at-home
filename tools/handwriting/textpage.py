"""Lay out a block of text in a standard font as a plottable SVG (one polyline path per stroke, mm units)."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

LINE_PITCH_EM = 2.6     # baseline distance, in x-heights (a standard font needs less than handwriting)
MAX_CHARS = 4000
MAX_LINES = 80


def page_svg(text, family, xh_mm, width_mm, bold=False, italic=False):
    """-> (svg string, stats). Paragraphs split on newlines; each is word-wrapped to width_mm."""
    from fontengine import FontEngine
    text = (text or "").replace("\r", "")[:MAX_CHARS]
    if not text.strip():
        raise ValueError("no text given")
    eng = FontEngine(family, bold=bold, italic=italic)
    lines = []
    for para in text.split("\n"):
        got = eng.layout(para, xh_mm, width_mm) if para.strip() else [[]]
        lines += got or [[]]
    if len(lines) > MAX_LINES:
        raise ValueError(f"{len(lines)} lines is too many (limit {MAX_LINES}); shorten the text")
    pitch = LINE_PITCH_EM * xh_mm
    top = 1.6 * xh_mm                      # room above the first baseline for capitals and ascenders
    height = top + (len(lines) - 1) * pitch + 0.8 * xh_mm
    paths = []
    for i, ln in enumerate(lines):
        y0 = top + i * pitch
        for st in ln:
            d = "M" + " L".join(f"{x:.2f},{y0 + y:.2f}" for x, y in st["pts"])
            paths.append(f'<path d="{d}" style="fill:none;stroke:#000000"/>')
    if not paths:
        raise ValueError("none of these characters exist in that font")
    w = max(max(x for st in ln for x, _ in st["pts"]) for ln in lines if ln) + 0.1
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}mm" height="{height:.2f}mm" '
           f'viewBox="0 0 {w:.2f} {height:.2f}">\n' + "\n".join(paths) + "\n</svg>\n")
    return svg, {"lines": len(lines), "width_mm": round(w, 1), "height_mm": round(height, 1),
                 "missing": "".join(sorted(eng.missing))}
