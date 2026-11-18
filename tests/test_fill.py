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


def test_contour_rings_are_offsets_of_the_shape():
    rings = fill.contour_rings([SQUARE, HOLE], 2.0)
    assert rings and all(r[0] == r[-1] for r in rings)
    # every point of a ring is 1, 3, 5... mm from the nearest edge (square or hole)
    def seg(p, a, b):
        (px, py), (ax, ay), (bx, by) = p, a, b
        u = max(0.0, min(1.0, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2)))
        return math.dist(p, (ax + u * (bx - ax), ay + u * (by - ay)))
    edges = [(r[i], r[(i + 1) % len(r)]) for r in (SQUARE, HOLE) for i in range(len(r))]
    for r in rings:
        for p in r:
            d = min(seg(p, a, b) for a, b in edges)
            assert abs((d - 1.0) / 2.0 - round((d - 1.0) / 2.0)) < 0.1, (p, d)


def test_hatch_one_direction():
    lines = fill.hatch_rings([SQUARE], 2.0, 0, zigzag=False)
    assert len(lines) == 10 and all(len(ln) == 2 and ln[0][0] < ln[1][0] for ln in lines)


def write_json(d, name, data):
    f = pathlib.Path(d) / name
    f.write_text(json.dumps(data))
    return f


def test_plot_svg_fills_per_ink_passes_and_rank():
    with tempfile.TemporaryDirectory() as d:
        svg = pathlib.Path(d) / "f.svg"
        svg.write_text(SVG)
        r, pv = run_plot(svg)
        inks = {i["key"]: i for i in pv["summary"]["fill_inks"]}
        assert set(inks) == {"#000000|", "#cc3333|"} and inks["#cc3333|"]["shapes"] == 1
        # the red shape as contour rings, the black one off: only red fill strokes appear
        fills = write_json(d, "fills.json", {"#cc3333|": {"mode": "contour", "spacing": 1.5},
                                             "#000000|": {"mode": "off"}})
        r, pv = run_plot(svg, "--fills", str(fills))
        assert r.returncode == 0, r.stdout + r.stderr
        keys = [ly["key"] for ly in pv["summary"]["layers"]]
        assert "#cc3333|" in keys
        red = keys.index("#cc3333|")
        assert sum(1 for s in pv["strokes"] if s["l"] == red) >= 4      # rings both sides of the hole
        # hatch with cross: more strokes than without
        h1 = write_json(d, "h1.json", {"#cc3333|": {"mode": "hatch", "spacing": 1.0, "angle": 0, "zigzag": False}})
        h2 = write_json(d, "h2.json", {"#cc3333|": {"mode": "hatch", "spacing": 1.0, "angle": 0, "zigzag": False, "cross": True}})
        n1 = len(run_plot(svg, "--fills", str(h1))[1]["strokes"])
        n2 = len(run_plot(svg, "--fills", str(h2))[1]["strokes"])
        assert n2 > n1 * 1.5
        bad = write_json(d, "bad.json", {"#cc3333|": {"mode": "hatch", "spacing": 0.01}})
        assert run_plot(svg, "--fills", str(bad))[0].returncode != 0
        # passes and layer order: black (layer 0) drawn twice, red first
        r, pv0 = run_plot(svg, "--fills", str(fills))
        layers = [{"key": k} for k in keys]
        layers[keys.index("#000000|")].update(passes=2, rank=5)
        layers[red].update(rank=1, name="Red marker")
        lj = write_json(d, "layers.json", layers)
        r, pv = run_plot(svg, "--fills", str(fills), "--layers", str(lj))
        assert r.returncode == 0, r.stdout + r.stderr
        blacks0 = [i for i in pv0["order"] if pv0["strokes"][i]["l"] == keys.index("#000000|")]
        order = pv["order"]
        assert len(order) == len(pv0["order"]) + len(blacks0)
        assert pv["strokes"][order[0]]["l"] == red
        # each black stroke is followed by its second pass; these are closed, so the same way round
        s = pv["strokes"]
        k = next(n for n in range(len(order)) if s[order[n]]["l"] != red)
        assert s[order[k + 1]]["pts"] == s[order[k]]["pts"]
