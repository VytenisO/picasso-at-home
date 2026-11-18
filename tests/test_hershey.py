"""Single-line Hershey text: font data, layout, wrapping, and the routes that use it."""
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import hershey  # noqa: E402


def test_every_font_has_all_printable_ascii():
    for name in hershey.FONTS:
        g, xh, base = hershey.font(name)
        assert len(g) == 95 and xh > 0
        assert g[" "][2] == []                       # a space draws nothing
        assert all(len(s) >= 2 for c, (_, _, ss) in g.items() for s in ss)


def test_x_height_and_baseline():
    polys, w, h = hershey.text_strokes("x", "Hershey Sans", xheight=5.0)
    ys = [y for s in polys for _, y in s]
    assert abs(max(ys) - 0) < 1e-6 and abs(min(ys) + 5.0) < 1e-6     # sits on the baseline, 5 mm tall


def test_wrapping_and_alignment():
    text = "one two three four five six seven eight nine ten"
    lines = hershey.lines_of(text, "Hershey Sans", 5, 60)
    assert len(lines) > 2 and " ".join(lines) == text
    left, w, _ = hershey.text_strokes("ab" + chr(10) + "abcdef", "Hershey Sans", 5, 100, align="left")
    right, _, _ = hershey.text_strokes("ab" + chr(10) + "abcdef", "Hershey Sans", 5, 100, align="right")
    assert max(x for s in right for x, _ in s) > 95 and min(x for s in left for x, _ in s) < 3


def test_svg_is_one_stroke_per_letter_part():
    svg, st = hershey.svg("Hi", "Hershey Sans", 5)
    assert svg.startswith("<svg") and st["strokes"] == 5        # H: three strokes, i: stem and dot
    assert st["length_mm"] > 0
