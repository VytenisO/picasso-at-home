"""Centre-line tracing (line art, photo of a drawing), stipple and hatch angle in image_to_svg."""
import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
from PIL import Image, ImageDraw  # noqa: E402

import image_to_svg as I  # noqa: E402


def lengths(paths):
    return sorted((sum(math.dist(a, b) for a, b in zip(p, p[1:])) for p in paths), reverse=True)


def drawing(bg):
    im = Image.new("L", (300, 225))
    px = im.load()
    for y in range(225):
        for x in range(300):
            px[x, y] = bg(x, y)
    d = ImageDraw.Draw(im)
    d.line([(30, 38), (270, 45)], fill=60, width=3)
    d.ellipse([75, 75, 188, 188], outline=70, width=3)
    return im


class Trace(unittest.TestCase):
    def test_lineart_keeps_whole_lines(self):
        im = drawing(lambda x, y: 230)
        L = lengths(I.lineart_bands(300, 225, im.load(), 150, 1, 0.6, 2.0)[0])
        self.assertEqual(len(L), 2)                          # one stroke per drawn line, not hundreds of pieces
        self.assertAlmostEqual(L[1], 240, delta=8)           # the straight line, end to end
        self.assertAlmostEqual(L[0], math.pi * 110, delta=25)  # the circle

    def test_sketch_ignores_uneven_light(self):
        im = drawing(lambda x, y: int(200 - 90 * x / 300 + 30 * y / 225))
        L = lengths(I.sketch_bands(300, 225, im, 1, 0.6, 2.0, 18)[0])
        self.assertEqual(len(L), 2)

    def test_skeleton_of_a_thick_line_is_one_pixel_wide(self):
        mask = [[1 if 40 <= y <= 44 and 10 <= x <= 90 else 0 for x in range(100)] for y in range(80)]
        sk = I.skeletonize(100, 80, mask)
        self.assertGreater(len(sk), 70)
        self.assertLessEqual(max(sum(1 for (x, y) in sk if x == c) for c in range(20, 80)), 1)

    def test_thin_diagonals_survive_thinning(self):
        for width in (1, 2, 3, 4):        # Zhang-Suen erased the even widths completely
            mask = [[1 if 0 <= (x - y) - 5 < width and 3 <= y <= 26 else 0 for x in range(40)] for y in range(30)]
            self.assertGreaterEqual(len(I.skeletonize(40, 30, mask)), 22, width)

    def test_stipple_patterns_and_tone(self):
        im = Image.new("L", (200, 100), 255)
        ImageDraw.Draw(im).rectangle([0, 0, 99, 99], fill=0)      # left half black, right half white
        for pattern in ("diffusion", "ordered"):
            dots = I.stipple_bands(200, 100, im.load(), 200, 1, 60, pattern)[0]
            left = sum(1 for d in dots if d[0][0] < 100)
            self.assertGreater(left, 50, pattern)
            self.assertEqual(len(dots) - left, 0, pattern)        # no dots on white paper
            self.assertTrue(all(len(d) == 2 for d in dots))


if __name__ == "__main__":
    unittest.main()
