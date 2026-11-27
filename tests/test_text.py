"""The text fix-up of technical drawings: find_text (measuring OCR boxes against the ink, decisions, hand edits,
the lettering erased, the text written back), the tracer's text layer, and the Prepare Text view endpoint.

No OCR is needed: the boxes are given, and the web test puts a stand-in `paddleocr` package on PYTHONPATH for the
helper process, so `ocr_text.py` runs as on the server. Nothing here moves a machine.

    python -m unittest discover -s tests -p test_text.py -v
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

import numpy as np
from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import find_text as ft  # noqa: E402
import trace_centerline as tc  # noqa: E402

import test_technical  # noqa: E402
from test_technical import png  # noqa: E402


def drawing():
    """Block letters in a box, a long line passing just under them, and a box whose reading is nonsense."""
    im = Image.new("L", (600, 300), 255)
    d = ImageDraw.Draw(im)
    for i in range(5):                                        # five "letters", 20 px tall, 3 px strokes
        x = 60 + i * 22
        d.rectangle((x, 40, x + 3, 60), fill=0)
        d.rectangle((x, 40, x + 14, 43), fill=0)
        d.rectangle((x, 57, x + 14, 60), fill=0)
    d.line([(20, 66), (580, 66)], fill=0, width=3)            # a leader line close under the text
    for i in range(18):                                       # a long title the reader gets wrong
        x = 40 + i * 28
        d.rectangle((x, 150, x + 4, 180), fill=0)
    return im


BOXES = [
    {"quad": [[56, 36], [170, 36], [170, 63], [56, 63]], "text": "ABCDE", "conf": 0.95},
    {"quad": [[36, 146], [550, 146], [550, 184], [36, 184]], "text": "S R RIG", "conf": 0.9},
    {"quad": [[400, 30], [440, 30], [440, 60], [400, 60]], "text": "X", "conf": 0.2},
]


class FindText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = np.asarray(drawing())
        cls.boxes = ft.analyze(cls.g, BOXES)

    def test_measures_letters_and_plausibility(self):
        a, b, c = self.boxes
        self.assertEqual(a["blobs"], 5)
        self.assertAlmostEqual(a["cap"], 21, delta=1)
        self.assertAlmostEqual(a["stroke"], 4, delta=1.5)
        self.assertTrue(a["plausible"])
        self.assertFalse(b["plausible"], "a short reading across a long box")
        self.assertEqual(c["blobs"], 0)
        self.assertEqual(c["cap"], 0)

    def test_decisions_follow_confidence_plausibility_and_edits(self):
        d = ft.decide(self.boxes, {"text_conf": 60})
        self.assertEqual([d[b["id"]]["kind"] for b in self.boxes], ["rewrite", "trace", "trace"])
        edits = ft.parse_edits(json.dumps([{"x": 100, "y": 50, "k": "rewrite", "s": "HELLO"},
                                           {"x": 300, "y": 165, "k": "drop"}, {"x": "bad"}, {"k": "nope"}]))
        self.assertEqual(len(edits), 2)
        d = ft.decide(self.boxes, {"text_conf": 60}, edits)
        self.assertEqual(d[1], {"kind": "rewrite", "text": "HELLO", "hand": True})
        self.assertEqual(d[2]["kind"], "drop")
        self.assertEqual(ft.parse_edits("not json"), [])
        # nothing measured inside: never rewritten, even by hand
        d = ft.decide(self.boxes, {}, [{"x": 420, "y": 45, "k": "rewrite", "s": "Y"}])
        self.assertEqual(d[3]["kind"], "trace")

    def test_erase_takes_the_letters_and_keeps_the_line(self):
        d = ft.decide(self.boxes, {})
        er = ft.erase_mask(self.g, self.boxes, d)
        ink = self.g < 170
        self.assertFalse((ink & ~er)[40:61, 60:160].any(), "the letters are gone")
        self.assertTrue((ink & ~er)[66, 20:580].all(), "the leader line under them stays")
        self.assertFalse(er[150:181].any(), "a traced box keeps its lettering")

    def test_rewrite_fits_the_box_and_grows_small_text(self):
        d = ft.decide(self.boxes, {})
        mm = 0.1
        lines, rings, styles = ft.rewrite(self.boxes, d, mm, {"text_min_mm": 0})
        self.assertEqual(styles, {"thin": 1})
        self.assertFalse(rings)
        pts = np.array([p for s in lines for p in s])
        self.assertGreater(pts[:, 0].min(), 50)
        self.assertLess(pts[:, 0].max(), 176)
        self.assertAlmostEqual(pts[:, 1].max() - pts[:, 1].min(), 21, delta=3)
        lines, _, _ = ft.rewrite(self.boxes, d, mm, {"text_min_mm": 4})        # 40 px: grows from its left end
        pts = np.array([p for s in lines for p in s])
        self.assertAlmostEqual(pts[:, 1].max() - pts[:, 1].min(), 40, delta=4)

    def test_upright_text_reads_bottom_to_top(self):
        o, u, up, L, h = ft.geom([[10, 100], [30, 100], [30, 10], [10, 10]])
        self.assertAlmostEqual(u[1], -1)
        self.assertAlmostEqual(L, 90)
        self.assertAlmostEqual(h, 20)
        b = {"id": 1, "quad": [[10, 100], [30, 100], [30, 10], [10, 10]], "text": "AB", "conf": 1, "cap": 14,
             "stroke": 2, "plausible": True}
        lines, _, _ = ft.rewrite([b], {1: {"kind": "rewrite", "text": "AB"}}, 0.1, {"text_min_mm": 0})
        pts = np.array([p for s in lines for p in s])
        self.assertGreater(np.ptp(pts[:, 1]), 2 * np.ptp(pts[:, 0]), "the text runs up the page")

    def test_heavy_lettering_is_filled_when_a_bold_font_exists(self):
        b = dict(self.boxes[0], stroke=40.0)
        d = {b["id"]: {"kind": "rewrite", "text": "AB"}}
        lines, rings, styles = ft.rewrite([b], d, 0.1, {}, bold_path=ft.bold_font())
        if ft.bold_font():
            self.assertEqual(styles, {"bold": 1})
            self.assertTrue(rings)
        else:
            self.assertEqual(styles, {"medium": 1})

    def test_tracer_leaves_erased_ink_out_and_writes_a_text_layer(self):
        d = ft.decide(self.boxes, {})
        er = ft.erase_mask(self.g, self.boxes, d)
        lines, rings, _ = ft.rewrite(self.boxes, d, 0.1, {})
        out = pathlib.Path(tempfile.mkdtemp()) / "t.svg"
        try:
            tc.trace_grey(self.g, str(out), 60, pens=1, fills=False, erase=er, text=(lines, rings))
            svg = out.read_text(encoding="utf-8")
            self.assertIn('inkscape:label="text"', svg)
            text_part = svg.split('id="text"')[1]
            self.assertEqual(text_part.count("<path"), len(lines))
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)

    def test_ascii_text(self):
        self.assertEqual(ft.ascii_text("1⁄2 × 3°"), "1/2 x 3?")


FAKE_PADDLE = '''
import json, os
class PaddleOCR:
    def __init__(self, **kw):
        pass
    def predict(self, path):
        boxes = json.load(open(os.environ["FAKE_OCR_BOXES"]))
        return [{"rec_polys": [b["quad"] for b in boxes], "rec_texts": [b["text"] for b in boxes],
                 "rec_scores": [b["conf"] for b in boxes]}]
'''


class TextWeb(test_technical.TechnicalWeb):
    """The Text view endpoint with a stand-in text reader."""

    @classmethod
    def setUpClass(cls):
        cls.fake = pathlib.Path(tempfile.mkdtemp(prefix="fakeocr_"))
        (cls.fake / "paddleocr").mkdir()
        (cls.fake / "paddleocr" / "__init__.py").write_text(FAKE_PADDLE)
        (cls.fake / "boxes.json").write_text(json.dumps(BOXES))
        cls.env = {k: os.environ.get(k) for k in ("PLOTTER_OCR_PYTHON", "PYTHONPATH", "FAKE_OCR_BOXES")}
        os.environ["PLOTTER_OCR_PYTHON"] = sys.executable
        os.environ["PYTHONPATH"] = str(cls.fake)
        os.environ["FAKE_OCR_BOXES"] = str(cls.fake / "boxes.json")
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for k, v in cls.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.fake, ignore_errors=True)

    # the inherited shading tests run elsewhere
    test_01_shading_then_trace_in_the_background = None
    test_01b_small_images_are_enlarged = None
    test_02_cancel = None

    def test_text_view_then_trace(self):
        code, me = self.call("GET", "/api/me")
        self.assertTrue(me["features"]["ocr"])
        code, steps = self.call("GET", "/api/prepare/steps")
        tech = next(s for s in steps["steps"] if s["id"] == "technical")
        self.assertIn("text_conf", [q["key"] for q in tech["params"]])
        self.assertIn("text_edits", tech["live"])
        code, d = self.call("POST", "/api/prepare/source", raw=png(drawing()), headers={"X-Filename": "text.png"})
        sid = d["source"]["id"]
        code, d = self.call("POST", "/api/prepare/text", {"recipe": self.recipe(sid, fills=False)})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["pending"])
        t = self.wait_task(d["task"]["id"])
        self.assertEqual(t["state"], "done", t)
        code, d = self.call("POST", "/api/prepare/text", {"recipe": self.recipe(sid, fills=False, text_conf=90)})
        self.assertFalse(d["pending"], "a live setting reads nothing again")
        tx = d["text"]
        self.assertEqual(tx["size"], [600, 300])
        self.assertEqual([b["text"] for b in tx["boxes"]], ["ABCDE", "S R RIG", "X"])
        # Trace: the edit renames the first text; the text layer is part of the result
        edits = json.dumps([{"x": 110, "y": 50, "k": "rewrite", "s": "HELLO"}])
        rec = self.recipe(sid, fills=False, text_edits=edits)
        code, d = self.call("POST", "/api/prepare/run", {"recipe": rec, "settings": {}, "go": True})
        t = self.wait_task(d["task"]["id"])
        self.assertEqual(t["state"], "done", t)
        code, r = self.call("POST", "/api/prepare/run", {"recipe": rec, "settings": {}})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["stats"]["text"]["rewritten"], 1)
        self.assertEqual(r["stats"]["text"]["traced"], 2)
        self.assertIn("text", [ly["name"] for ly in r["layers"]])
        # another string or font only writes the text again: no Trace needed, the line trace is kept
        for change in ({"text_edits": json.dumps([{"x": 110, "y": 50, "k": "rewrite", "s": "WORLD"}])},
                       {"text_edits": edits, "text_font": "Hershey Serif", "text_min_mm": 3}):
            rec2 = self.recipe(sid, fills=False, **change)
            code, r2 = self.call("POST", "/api/prepare/run", {"recipe": rec2, "settings": {}})
            self.assertEqual(code, 200, r2)
            self.assertFalse(r2.get("needs_trace"), change)
            self.assertEqual(r2["stats"]["text"]["rewritten"], 1)
            self.assertNotEqual(r2["key"], r["key"])
        # leaving it out erases the same ink: quick as well
        rec3 = self.recipe(sid, fills=False, text_edits=json.dumps([{"x": 110, "y": 50, "k": "drop"}]))
        code, r3 = self.call("POST", "/api/prepare/run", {"recipe": rec3, "settings": {}})
        self.assertFalse(r3.get("needs_trace"))
        self.assertEqual(r3["stats"]["text"]["dropped"], 1)
        # tracing it as drawn keeps ink that was erased: the lines are traced again
        rec4 = self.recipe(sid, fills=False, text_edits=json.dumps([{"x": 110, "y": 50, "k": "trace"}]))
        code, r4 = self.call("POST", "/api/prepare/run", {"recipe": rec4, "settings": {}})
        self.assertTrue(r4.get("needs_trace"))

    def test_without_a_reader_the_text_settings_are_hidden(self):
        os.environ.pop("PLOTTER_OCR_PYTHON")
        try:
            code, me = self.call("GET", "/api/me")
            self.assertFalse(me["features"]["ocr"])
            code, steps = self.call("GET", "/api/prepare/steps")
            tech = next(s for s in steps["steps"] if s["id"] == "technical")
            self.assertNotIn("text", [q["key"] for q in tech["params"]])
            code, d = self.call("POST", "/api/prepare/text", {"recipe": {"source": "x", "steps": []}})
            self.assertEqual(code, 400)
        finally:
            os.environ["PLOTTER_OCR_PYTHON"] = sys.executable


if __name__ == "__main__":
    unittest.main()
