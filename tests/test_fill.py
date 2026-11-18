"""Fill (hatching) tests: geometry, plot_svg --fill, tone hatching, settings validation. No hardware."""
import json
import math
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import fill  # noqa: E402

SQUARE = [(0, 0), (20, 0), (20, 20), (0, 20)]
HOLE = [(6, 6), (14, 6), (14, 14), (6, 14)]


def total(lines):
    return sum(math.dist(a, b) for ln in lines for a, b in zip(ln, ln[1:]))


def test_hatch_covers_a_square_at_the_spacing():
    lines = fill.hatch_rings([SQUARE], 1.0, 0)
    assert abs(total(lines) - 20 * 20) < 25          # ~20 lines of 20 mm, plus short links
    assert len(lines) <= 2                           # zigzag: joined, not one stroke per line
    for ln in lines:
        for x, y in ln:
            assert -1e-6 <= x <= 20 + 1e-6 and -1e-6 <= y <= 20 + 1e-6


def test_hole_even_odd_and_nonzero():
    eo = fill.hatch_rings([SQUARE, HOLE], 1.0, 0, "evenodd")
    same_dir = fill.hatch_rings([SQUARE, HOLE], 1.0, 0, "nonzero")      # both rings wind the same way: no hole
    assert total(eo) < total(same_dir) - 6 * 8 * 0.9
    opposite = fill.hatch_rings([SQUARE, HOLE[::-1]], 1.0, 0, "nonzero")
    assert abs(total(opposite) - total(eo)) < 3
    for ln in eo:                                    # no point of a hatch line strictly inside the hole
        for x, y in ln:
            assert not (6.2 < x < 13.8 and 6.2 < y < 13.8)


def test_angle_and_darkness():
    d45 = fill.fill_shape([SQUARE], 1.0, 0.5, 45, cross=True)
    d100 = fill.fill_shape([SQUARE], 1.0, 1.0, 45, cross=True)
    assert total(d100) > 3.0 * total(d45)            # half-dark: twice the spacing; full dark also gets a cross pass
    assert fill.fill_shape([SQUARE], 1.0, 0.05) == []   # almost white: left blank


def test_tone_runs_follow_the_tone():
    left_dark = lambda x, y: 1.0 if x < 10 else 0.0  # noqa: E731
    lines = fill.tone_runs(left_dark, 20, 20, 1.0, 0, 0.5, step=0.25)
    assert lines and all(x <= 10.3 for ln in lines for x, _ in ln)
    assert fill.tone_runs(lambda x, y: 0.0, 20, 20, 1.0, 0, 0.5) == []


def run_plot(svg, *extra):
    with tempfile.TemporaryDirectory() as d:
        pv = pathlib.Path(d) / "pv.json"
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "plot_svg.py"), str(svg), "--mode", "dry", "--sim",
                            "--preview-json", str(pv), *extra], capture_output=True, text=True, encoding="utf-8")
        return r, (json.loads(pv.read_text()) if pv.exists() else None)


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="30mm" viewBox="0 0 60 30">'
       '<rect x="2" y="2" width="20" height="20" fill="#000" stroke="#000"/>'
       '<path d="M30,2 h20 v20 h-20 z M36,8 h8 v8 h-8 z" fill="#c33" stroke="none"/>'
       '<circle cx="12" cy="26" r="2" fill="none" stroke="#000"/></svg>')


def test_plot_svg_fill_option():
    with tempfile.TemporaryDirectory() as d:
        svg = pathlib.Path(d) / "f.svg"
        svg.write_text(SVG)
        r0, pv0 = run_plot(svg)
        r1, pv1 = run_plot(svg, "--fill", "1.0")
        assert r0.returncode == 0 and r1.returncode == 0, (r0.stderr, r1.stderr)
        n0, n1 = len(pv0["strokes"]), len(pv1["strokes"])
        assert n1 > n0 and pv1["summary"]["drawing_mm"] > 5 * pv0["summary"]["drawing_mm"]
        # the same inks in the same order, so saved layer settings still fit
        assert [ly["key"] for ly in pv1["summary"]["layers"]][:len(pv0["summary"]["layers"])] == \
               [ly["key"] for ly in pv0["summary"]["layers"]]
        # fills are drawn before the outline of the same ink
        order = pv1["order"]
        assert order.index(next(i for i in order if i >= n0)) < order.index(0)
        bad, _ = run_plot(svg, "--fill", "0.05")
        assert bad.returncode != 0


def test_photo_hatch_style():
    from PIL import Image
    import image_to_svg
    with tempfile.TemporaryDirectory() as d:
        im = Image.new("L", (120, 80))
        for x in range(120):
            for y in range(80):
                im.putpixel((x, y), int(255 * x / 119))
        src, dst = pathlib.Path(d) / "g.png", pathlib.Path(d) / "g.svg"
        im.save(src)
        st = image_to_svg.convert(src, dst, width_mm=60, threshold=200, style="hatch", density=40)
        assert st["paths"] > 3 and dst.read_text().count("<path") == st["paths"]
