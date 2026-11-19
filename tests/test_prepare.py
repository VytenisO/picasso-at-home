"""Prepare workspace: sources, recipes of plugin steps, the dry-run preview, saving to the library and reopening.

Runs the web server in simulator mode, like test_plotweb.py. Nothing here moves a machine.
"""
import io
import json
import unittest

from test_plotweb import FIX, SimServer


def png(w=200, h=120, alpha=False):
    from PIL import Image, ImageDraw
    im = Image.new("RGBA" if alpha else "RGB", (w, h), (255, 255, 255, 0) if alpha else (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.ellipse((20, 20, 100, 100), fill=(0, 0, 0, 255) if alpha else (0, 0, 0))
    d.rectangle((120, 30, 180, 90), fill=(90, 90, 90, 255) if alpha else (90, 90, 90))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


class TestPrepare(SimServer):
    def source(self, raw, name):
        code, d = self.call("POST", "/api/prepare/source", raw=raw, headers={"X-Filename": name})
        self.assertEqual(code, 200, d)
        return d["source"]

    def run_recipe(self, recipe, settings=None, expect=200):
        code, d = self.call("POST", "/api/prepare/run", {"recipe": recipe, "settings": settings or {}})
        self.assertEqual(code, expect, d)
        return d

    def test_00_pdf_page_and_import_report(self):
        import shutil
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf is not installed")
        if not shutil.which("pdftocairo"):
            self.skipTest("pdftocairo is not installed")
        doc = pymupdf.open()
        for n in range(2):
            page = doc.new_page(width=300, height=200)
            page.draw_line((20, 20), (280, 20 + 40 * n))
            page.draw_rect(pymupdf.Rect(40, 60, 140, 140))
            page.insert_text((160, 120), "Hi", fontsize=24)
        raw = doc.tobytes()
        src = self.source(raw, "two-pages.pdf")
        self.assertEqual(src["kind"], "pdf")
        r = self.run_recipe({"source": src["id"], "steps": [{"step": "pdfpage", "params": {"page": 2}}]})
        self.assertGreaterEqual(r["summary"]["strokes"], 3)
        st = r["stats"]
        self.assertEqual(st["page"], 2)
        self.assertGreaterEqual(st["shapes"], 2)
        self.assertGreaterEqual(st["uses"], 2)                  # the two glyphs of "Hi", drawn through <use>
        r = self.run_recipe({"source": src["id"], "steps": [{"step": "pdfpage", "params": {"page": 9}}]}, expect=400)
        code, d = self.call("POST", "/api/prepare/source", raw=b"not a pdf", headers={"X-Filename": "x.pdf"})
        self.assertEqual(code, 400)

    def test_01_steps_catalogue(self):
        code, d = self.call("GET", "/api/prepare/steps")
        self.assertEqual(code, 200)
        by = {s["id"]: s for s in d["steps"]}
        for sid, stage in (("tone", "adjust"), ("contour", "method"), ("text_page", "method"), ("simplify", "clean")):
            self.assertEqual(by[sid]["stage"], stage)
        self.assertEqual(by["text_page"]["accepts"], [])
        self.assertIn(".png", d["kinds"]["image"])

    def test_02_image_recipe_preview_is_a_plot_preview(self):
        src = self.source(png(alpha=True), "blobs.png")
        self.assertEqual(src["kind"], "image")
        self.assertEqual(src["px"], [200, 120])
        recipe = {"source": src["id"], "steps": [
            {"step": "tone", "params": {"contrast": 30}},
            {"step": "contour", "params": {"width_mm": 80, "threshold": 140}},
            {"step": "drop_short", "params": {"min_mm": 0.5}}]}
        r = self.run_recipe(recipe)
        self.assertTrue(r["strokes"])
        self.assertEqual(r["fit"], 1.0)
        b = r["summary"]["bbox_machine_mm"]
        self.assertLess(b[2] - b[0], 81)
        self.assertGreater(r["summary"]["est_seconds"], 0)
        self.assertTrue(r["image"] and r["mask"])
        for kind, key in (("png", r["image"]), ("mask", r["mask"]), ("svg", r["key"])):
            self.assertEqual(self._get_status(f"/api/prepare/file?kind={kind}&key={key}"), 200, kind)
        self.assertEqual(self._get_status("/api/prepare/file?kind=svg&key=../../etc"), 404)
        self.assertEqual(self._get_status(f"/api/prepare/file?kind=source&id={src['id']}"), 200)
        # The same recipe again comes from the cache.
        again = self.run_recipe(recipe)
        self.assertEqual(again["key"], r["key"])
        self.assertEqual(len(again["strokes"]), len(r["strokes"]))

    def _get_status(self, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as ex:
            return ex.code

    def test_03_recipe_validation(self):
        src = self.source(png(), "v.png")
        cases = [
            ({"source": src["id"], "steps": []}, "Choose one method"),
            ({"source": src["id"], "steps": [{"step": "contour"}, {"step": "hatch"}]}, "exactly one method"),
            ({"source": src["id"], "steps": [{"step": "text_page", "params": {"text": "x"}}]}, "does not work"),
            ({"source": None, "steps": [{"step": "tone"}, {"step": "text_art"}]}, "does not work"),
            ({"source": "0123456789abcdef", "steps": [{"step": "contour"}]}, "gone from the server"),
            ({"source": src["id"], "steps": [{"step": "nope"}]}, "not a known step"),
        ]
        for recipe, msg in cases:
            d = self.run_recipe(recipe, expect=400)
            self.assertIn(msg, d["error"], recipe)
        # A switched-off step is skipped, so this one has no method left.
        d = self.run_recipe({"source": src["id"], "steps": [{"step": "contour", "on": False}]}, expect=400)
        self.assertIn("Choose one method", d["error"])
        code, d = self.call("POST", "/api/prepare/source", raw=b"hello", headers={"X-Filename": "notes.txt"})
        self.assertEqual(code, 400)

    def test_04_clean_steps_refuse_fills(self):
        src = self.source((FIX / "filled.svg").read_bytes(), "filled.svg")
        ok = self.run_recipe({"source": src["id"], "steps": [{"step": "svg"}]}, {"fill": 1.5})
        plain = self.run_recipe({"source": src["id"], "steps": [{"step": "svg"}]})
        self.assertGreater(len(ok["strokes"]), len(plain["strokes"]))      # hatch lines from the fill
        d = self.run_recipe({"source": src["id"], "steps": [{"step": "svg"}, {"step": "simplify"}]}, expect=400)
        self.assertIn("filled shapes", d["error"])
        d = self.run_recipe({"source": src["id"], "steps": [{"step": "svg"}]}, {"fill": 0.1}, expect=400)
        self.assertIn("Fill spacing", d["error"])

    def test_05_one_pen_merges_layers(self):
        src = self.source((FIX / "two_layers.svg").read_bytes(), "two_layers.svg")
        two = self.run_recipe({"source": src["id"], "steps": [{"step": "svg"}]})
        self.assertEqual(len(two["layers"]), 2)
        one = self.run_recipe({"source": src["id"], "steps": [{"step": "svg"}, {"step": "one_pen"}]})
        self.assertEqual(len(one["layers"]), 1)
        self.assertEqual(sum(ly["strokes"] for ly in one["layers"]), sum(ly["strokes"] for ly in two["layers"]))

    def test_06_generator_save_reopen_and_update(self):
        recipe = {"source": None, "steps": [{"step": "text_art", "params": {"text": "PLOT", "size": 30}}]}
        r = self.run_recipe(recipe)
        key = r["layers"][0]["key"]
        code, d = self.call("POST", "/api/prepare/save", {"recipe": recipe, "name": "word",
                                                          "settings": {"layers": [{"key": key, "pen": "bic"}]}})
        self.assertEqual(code, 200, d)
        fid = d["file"]["id"]
        _, files = self.call("GET", "/api/files")
        e = next(f for f in files["files"] if f["id"] == fid)
        self.assertEqual((e["kind"], e["method"], e["name"]), ("prepared", "text_art", "word.svg"))
        self.assertEqual(e["settings"]["layers"][0]["pen"], "bic")
        # Plot previews it like any drawing, with the pen chosen in Prepare.
        code, pv = self.call("POST", "/api/preview", {"id": fid, "settings": e["settings"]})
        self.assertEqual(code, 200, pv)
        self.assertTrue(pv["ok"], pv["problems"])
        self.assertEqual(pv["settings"]["layers"][0]["pen"], "bic")
        # Reopen: the recipe comes back, no source for a generator.
        code, back = self.call("POST", "/api/prepare/from_library", {"file": fid})
        self.assertEqual(code, 200, back)
        self.assertIsNone(back["source"])
        self.assertEqual(back["recipe"]["steps"][0]["params"]["text"], "PLOT")
        self.assertEqual(back["replace"], fid)
        # Update in place: same id, new drawing, placement kept.
        self.call("POST", "/api/preview", {"id": fid, "settings": dict(e["settings"], x=100, y=60)})
        recipe["steps"][0]["params"]["text"] = "PLOTS"
        code, d = self.call("POST", "/api/prepare/save", {"recipe": recipe, "name": "word", "replace": fid})
        self.assertEqual(code, 200, d)
        self.assertEqual(d["file"]["id"], fid)
        _, files = self.call("GET", "/api/files")
        e2 = next(f for f in files["files"] if f["id"] == fid)
        self.assertEqual(e2["recipe"]["steps"][0]["params"]["text"], "PLOTS")
        self.assertEqual((e2["settings"]["x"], e2["settings"]["y"]), (100, 60))
        self.assertNotEqual(e2["svg"], e["svg"])

    def test_06b_reopen_an_image_recipe(self):
        src = self.source(png(), "photo.png")
        recipe = {"source": src["id"], "steps": [{"step": "tone", "params": {"contrast": 20}},
                                                 {"step": "contour", "params": {"width_mm": 60}}]}
        self.run_recipe(recipe)
        code, d = self.call("POST", "/api/prepare/save", {"recipe": recipe, "name": "photo"})
        self.assertEqual(code, 200, d)
        code, back = self.call("POST", "/api/prepare/from_library", {"file": d["file"]["id"]})
        self.assertEqual(code, 200, back)
        self.assertEqual(back["source"]["kind"], "image")
        self.assertEqual(back["source"]["id"], src["id"])            # same bytes, same source
        self.assertEqual([s["step"] for s in back["recipe"]["steps"]], ["tone", "contour"])
        again = self.run_recipe(dict(back["recipe"], source=back["source"]["id"]))
        self.assertTrue(again["strokes"])

    def test_07_reopen_an_old_upload(self):
        fid = self.upload("two_layers.svg")
        code, back = self.call("POST", "/api/prepare/from_library", {"file": fid})
        self.assertEqual(code, 200, back)
        self.assertEqual(back["source"]["kind"], "svg")
        self.assertEqual(back["recipe"]["steps"][0]["step"], "svg")
        r = self.run_recipe(dict(back["recipe"], source=back["source"]["id"]))
        self.assertEqual(len(r["layers"]), 2)

    def test_08_oversize_is_a_fit_not_a_problem(self):
        src = self.source(png(400, 100), "wide.png")
        r = self.run_recipe({"source": src["id"], "steps": [{"step": "contour", "params": {"width_mm": 1000}}]})
        self.assertEqual(r["problems"], [])
        self.assertLessEqual(r["fit"], 1.0)
        self.assertTrue(json.dumps(r["stats"]))


if __name__ == "__main__":
    unittest.main()
