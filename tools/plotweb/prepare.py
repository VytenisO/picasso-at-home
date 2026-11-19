"""Prepare workspace: a recipe of plugin steps turns a source (image, SVG, 3D model, or nothing) into a plot-ready SVG.

A recipe is {"source": source id or None, "steps": [{"step": id, "on": bool, "params": {...}}, ...]}. Steps run by
stage, in this order:

  adjust  image -> image (Pillow). Only for image sources. Any number, in the order given.
  method  source -> SVG. Exactly one. Generators (text) take no source.
  clean   SVG -> SVG, on the drawn polylines. Any number. Refused for drawings with filled shapes, which they
          would lose.

Fill spacing and pens are plot settings, not steps: they are saved with the result and applied by plot_svg.py, so
Plot can change them later without redoing the recipe.

To add a step, write its function and append a Step to STEPS. The UI builds the form from `params` (the schema
of converters.py, plus type "textarea") and the server clamps every value to it before calling the function:

  adjust: fn(im, p) -> im                                  (PIL image, RGB)
  method: fn(src_path or None, dst_svg_path, p, ctx) -> stats dict   (ctx: canvas, mask)
  clean:  fn(drawing, p) -> drawing                        (drawing: polys and metas in mm, size)

Every stage result is cached by a hash of the source and the steps up to it, so changing a late step does not
redo the early ones.
"""
import hashlib
import json
import math
import pathlib
import re
import os
import secrets
import sys
import time
from xml.sax.saxutils import escape

from .converters import IMG, clamp_params

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

MAX_SOURCE = 12 * 1024 * 1024
SRC_ID = re.compile(r"^[0-9a-f]{16}$")
KEY = re.compile(r"^[0-9a-f]{24}$")
KINDS = {"image": IMG, "svg": (".svg",), "mesh": (".stl", ".obj")}
STAGES = ("adjust", "method", "clean")


class PrepareError(ValueError):
    pass


class Step:
    def __init__(self, sid, title, stage, accepts, params, fn, help="", group=""):
        self.id, self.title, self.stage, self.accepts = sid, title, stage, tuple(accepts)
        self.params, self.fn, self.help, self.group = params, fn, help, group

    def public(self):
        return {"id": self.id, "title": self.title, "stage": self.stage, "accepts": list(self.accepts),
                "params": self.params, "help": self.help, "group": self.group}


def kind_of(ext):
    ext = ext.lower()
    return next((k for k, exts in KINDS.items() if ext in exts), None)


# ------------------------------------------------------------------ adjust steps (Pillow)
def _pil():
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    return Image, ImageEnhance, ImageFilter, ImageOps


def _tone(im, p):
    _, enh, _, _ = _pil()
    if p["brightness"]:
        im = enh.Brightness(im).enhance(1 + p["brightness"] / 100)
    if p["contrast"]:
        im = enh.Contrast(im).enhance(1 + p["contrast"] / 100)
    if abs(p["gamma"] - 1) > 1e-3:
        g = 1 / p["gamma"]
        lut = [round(255 * (i / 255) ** g) for i in range(256)]
        im = im.point(lut * len(im.getbands()))
    return im


def _invert(im, p):
    return _pil()[3].invert(im)


def _crop(im, p):
    w, h = im.size
    box = (round(w * p["left"] / 100), round(h * p["top"] / 100),
           round(w * (1 - p["right"] / 100)), round(h * (1 - p["bottom"] / 100)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        raise PrepareError("Crop leaves less than 8 pixels. Crop less.")
    return im.crop(box)


def _orient(im, p):
    Image, _, _, ImageOps = _pil()
    if p["rotate"] != "0":
        im = im.rotate(-int(p["rotate"]), expand=True, fillcolor=(255, 255, 255))
    if p["mirror"]:
        im = ImageOps.mirror(im)
    return im


def _sharpen(im, p):
    _, _, flt, _ = _pil()
    if p["denoise"]:
        size = int(p["denoise"]) * 2 + 1
        im = im.filter(flt.MedianFilter(size))
    if p["sharpen"]:
        im = im.filter(flt.UnsharpMask(radius=2, percent=int(p["sharpen"]), threshold=2))
    return im


# ------------------------------------------------------------------ method steps
WIDTH = {"key": "width_mm", "label": "Width on paper", "type": "number", "default": 120, "min": 10, "max": 1000,
         "step": 1, "unit": "mm", "help": "Natural size. Plot can still scale it."}
THRESHOLD = {"key": "threshold", "label": "Dark cutoff", "type": "number", "default": 150, "min": 0, "max": 255,
             "step": 1, "integer": True, "help": "Pixels darker than this count as ink."}
BLUR = {"key": "blur", "label": "Blur", "type": "number", "default": 1.0, "min": 0, "max": 10, "step": 0.5,
        "unit": "px", "help": "Smooths noise before tracing."}
INKS = {"key": "inks", "label": "Pens (ink layers)", "type": "number", "default": 1, "min": 1, "max": 4, "step": 1,
        "integer": True, "help": "More pens: darker tones in separate layers, one pen swap each."}
ONE_PEN = {"key": "single_pen", "label": "Keep the tones, plot with one pen", "type": "bool", "default": False}
DENSITY = {"key": "density", "label": "Density", "type": "number", "default": 40, "min": 1, "max": 100, "step": 1,
           "integer": True}
LEVELS = {"key": "levels", "label": "Tone levels", "type": "number", "default": 0, "min": 0, "max": 6, "step": 1,
          "integer": True, "help": "0 = continuous. 2-6 posterise the image first."}
SMOOTH = {"key": "simplify_px", "label": "Smoothness", "type": "number", "default": 0.6, "min": 0, "max": 5,
          "step": 0.1, "unit": "px", "help": "Higher: fewer points, rounder curves, less detail."}
SPECKS = {"key": "min_len_px", "label": "Ignore specks under", "type": "number", "default": 2, "min": 0, "max": 60,
          "step": 1, "unit": "px"}
DETAIL = {"key": "width_px", "label": "Trace resolution", "type": "number", "default": 300, "min": 60, "max": 500,
          "step": 10, "integer": True, "unit": "px", "help": "Pixels across the image while tracing. More is finer and slower."}


def _raster(style, extra_keys):
    def fn(src, dst, p, ctx):
        from image_to_svg import convert
        c = ctx["canvas"]
        kw = {k: p[k] for k in extra_keys if k in p}
        if "levels" in kw and kw["levels"] == 1:
            kw["levels"] = 0
        stats = convert(src, dst, width_mm=min(p["width_mm"], c[2] - c[0]), threshold=p["threshold"], blur=p["blur"],
                        style=style, inks=p["inks"], single_pen=p.get("single_pen", False), mask=ctx.get("mask"),
                        max_width_mm=c[2] - c[0], max_height_mm=c[3] - c[1], **kw)
        return stats
    return fn


def _svg_as_is(src, dst, p, ctx):
    from svg_vector import prepare
    return prepare(src, dst, font_family=p["font"] or "DejaVu Sans")


def _mesh(src, dst, p, ctx):
    from mesh_to_svg import convert
    return convert(src, dst, yaw=p["yaw"], pitch=p["pitch"], width_mm=p["width_mm"])


def _text_page(src, dst, p, ctx):
    from handwriting.textpage import page_svg
    if not p["text"].strip():
        raise PrepareError("Type some text first.")
    c = ctx["canvas"]
    svg, stats = page_svg(p["text"], p["font"] or "Comic Neue", p["xheight"], c[2] - c[0], bold=p["bold"],
                          italic=p["italic"])
    pathlib.Path(dst).write_text(svg, encoding="utf-8")
    return stats


def _text_single(src, dst, p, ctx):
    import hershey
    if not p["text"].strip():
        raise PrepareError("Type some text first.")
    c = ctx["canvas"]
    width = p["width"] or (c[2] - c[0])
    try:
        svg, stats = hershey.svg(p["text"], p["font"], p["xheight"], width, p["xheight"] * p["spacing"], p["align"])
    except ValueError as ex:
        raise PrepareError(str(ex))
    pathlib.Path(dst).write_text(svg, encoding="utf-8")
    return stats


def _text_art(src, dst, p, ctx):
    from svg_vector import text_art_svg
    if not p["text"].strip():
        raise PrepareError("Type some text first.")
    svg, stats = text_art_svg(p["text"], p["size"], p["font"] or "DejaVu Sans", "bold" if p["bold"] else "normal",
                              "normal", p["effect"], p["bend"])
    pathlib.Path(dst).write_text(svg, encoding="utf-8")
    return stats


# ------------------------------------------------------------------ clean steps (polylines in mm)
def _length(poly):
    return sum(math.dist(a, b) for a, b in zip(poly, poly[1:]))


def _simplify(d, p):
    from plot_svg import rdp
    polys = [rdp(q, p["tolerance"]) for q in d["polys"]]
    keep = [i for i, q in enumerate(polys) if len(q) >= 2]
    return dict(d, polys=[polys[i] for i in keep], metas=[d["metas"][i] for i in keep])


def _drop_short(d, p):
    keep = [i for i, q in enumerate(d["polys"]) if _length(q) >= p["min_mm"]]
    return dict(d, polys=[d["polys"][i] for i in keep], metas=[d["metas"][i] for i in keep])


def _join(d, p):
    tol, polys, metas = p["tolerance"], [], []
    for q, m in zip(d["polys"], d["metas"]):
        q = list(q)
        if polys and (m.get("color"), m.get("layer")) == (metas[-1].get("color"), metas[-1].get("layer")):
            last = polys[-1]
            if math.dist(last[-1], q[0]) <= tol:
                last.extend(q[1:])
                continue
            if math.dist(last[-1], q[-1]) <= tol:
                last.extend(q[-2::-1])
                continue
        polys.append(q)
        metas.append(dict(m))
    return dict(d, polys=polys, metas=metas)


def _one_pen(d, p):
    return dict(d, metas=[dict(m, color="#111111", layer=None) for m in d["metas"]])


def _load_drawing(svg_path):
    from plot_svg import load_svg
    polys, metas, size, fills = load_svg(str(svg_path), True)
    if fills:
        raise PrepareError("Clean-up steps work on lines only, and this drawing has filled shapes they would lose. "
                           "Remove the clean-up steps, or fill it with hatching in Plot.")
    return {"polys": polys, "metas": metas, "size": size}


def _write_drawing(d, dst):
    w, h = d["size"]
    groups, order = {}, []
    for q, m in zip(d["polys"], d["metas"]):
        k = (m.get("color") or "#000000", m.get("layer"))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(q)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
           f'width="{w:.3f}mm" height="{h:.3f}mm" viewBox="0 0 {w:.3f} {h:.3f}">']
    for color, layer in order:
        attrs = f'fill="none" stroke="{color}" stroke-width="0.3" stroke-linecap="round" stroke-linejoin="round"'
        if layer:
            attrs += f' inkscape:groupmode="layer" inkscape:label="{escape(layer, {chr(34): "&quot;"})}"'
        out.append(f"<g {attrs}>")
        for q in groups[(color, layer)]:
            out.append('<path d="M' + " L".join(f"{x:.3f} {y:.3f}" for x, y in q) + '"/>')
        out.append("</g>")
    out.append("</svg>")
    pathlib.Path(dst).write_text("\n".join(out) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ registry
ROTATE = [["0", "None"], ["90", "90 deg clockwise"], ["180", "180 deg"], ["270", "90 deg anticlockwise"]]
FONT = {"key": "font", "label": "Font", "type": "text", "default": "Comic Neue", "list": "fonts"}

STEPS = {s.id: s for s in [
    Step("tone", "Tone", "adjust", ["image"], [
        {"key": "brightness", "label": "Brightness", "type": "number", "default": 0, "min": -100, "max": 100,
         "step": 1, "slider": True},
        {"key": "contrast", "label": "Contrast", "type": "number", "default": 0, "min": -100, "max": 100, "step": 1,
         "slider": True},
        {"key": "gamma", "label": "Gamma", "type": "number", "default": 1.0, "min": 0.2, "max": 5, "step": 0.05,
         "slider": True, "help": "Above 1 lifts the mid-tones, below 1 darkens them."},
    ], _tone, "Brightness, contrast and gamma. Photos usually need a little contrast before tracing."),
    Step("invert", "Invert", "adjust", ["image"], [], _invert, "Light becomes ink. For light lines on a dark photo."),
    Step("crop", "Crop", "adjust", ["image"], [
        {"key": k, "label": f"{k.capitalize()}", "type": "number", "default": 0, "min": 0, "max": 45, "step": 1,
         "unit": "%", "slider": True} for k in ("left", "top", "right", "bottom")
    ], _crop),
    Step("orient", "Rotate and mirror", "adjust", ["image"], [
        {"key": "rotate", "label": "Rotate", "type": "select", "default": "0", "options": ROTATE},
        {"key": "mirror", "label": "Mirror left to right", "type": "bool", "default": False},
    ], _orient),
    Step("sharpen", "Denoise and sharpen", "adjust", ["image"], [
        {"key": "denoise", "label": "Denoise", "type": "number", "default": 0, "min": 0, "max": 3, "step": 1,
         "integer": True, "slider": True, "help": "Median filter: removes speckle and JPEG noise."},
        {"key": "sharpen", "label": "Sharpen", "type": "number", "default": 0, "min": 0, "max": 300, "step": 10,
         "integer": True, "slider": True, "unit": "%"},
    ], _sharpen),

    Step("contour", "Contours", "method", ["image"], [WIDTH, THRESHOLD, LEVELS, INKS, ONE_PEN, BLUR, SMOOTH, SPECKS,
                                                       DETAIL],
         _raster("contour", ("levels", "simplify_px", "min_len_px", "width_px")),
         "Outlines of the dark shapes. Tone levels nest outlines for shading.", "Trace an image"),
    Step("lineart", "Line art (centre lines)", "method", ["image"], [WIDTH, THRESHOLD, INKS, BLUR, SMOOTH, SPECKS],
         _raster("lineart", ("simplify_px", "min_len_px")),
         "One stroke down the middle of each drawn line, instead of two outlines. For drawings and sketches.",
         "Trace an image"),
    Step("hatch", "Shading by hatching", "method", ["image"], [WIDTH, THRESHOLD, LEVELS, DENSITY, INKS, ONE_PEN, BLUR],
         _raster("hatch", ("levels", "density")),
         "Parallel lines where the picture is dark, crossed in the shadows.", "Trace an image"),
    Step("halftone", "Halftone dots", "method", ["image"], [WIDTH, THRESHOLD, DENSITY, INKS, ONE_PEN, BLUR],
         _raster("halftone", ("density",)), "A grid of dots that grow with the darkness.", "Trace an image"),
    Step("one-line", "One continuous line", "method", ["image"], [WIDTH, THRESHOLD, DENSITY, INKS, BLUR],
         _raster("one-line", ("density",)), "A single serpentine line, packed tight in the dark parts.",
         "Trace an image"),
    Step("svg", "Use the drawing as it is", "method", ["svg"], [
        {"key": "font", "label": "Font for text", "type": "text", "default": "DejaVu Sans", "list": "fonts",
         "help": "Text in the SVG becomes outlines in this font."},
    ], _svg_as_is, "Plotted as drawn: every line and outline. Filled shapes can be hatched in Fill.", "Vector"),
    Step("mesh", "Wireframe, hidden lines removed", "method", ["mesh"], [
        {"key": "yaw", "label": "View yaw", "type": "number", "default": 35, "min": -180, "max": 180, "unit": "deg",
         "slider": True},
        {"key": "pitch", "label": "View pitch", "type": "number", "default": 25, "min": -89, "max": 89, "unit": "deg",
         "slider": True},
        {"key": "width_mm", "label": "Size on paper", "type": "number", "default": 120, "min": 20, "max": 267,
         "unit": "mm"},
    ], _mesh, "", "3D model"),
    Step("text_page", "Text in a font", "method", [], [
        {"key": "text", "label": "Text", "type": "textarea", "default": "", "maxlength": 4000,
         "placeholder": "Type or paste text. It is wrapped to the canvas."},
        FONT,
        {"key": "xheight", "label": "Letter height", "type": "number", "default": 4.5, "min": 2, "max": 20,
         "step": 0.5, "unit": "mm"},
        {"key": "bold", "label": "Bold", "type": "bool", "default": False},
        {"key": "italic", "label": "Italic", "type": "bool", "default": False},
    ], _text_page, "Letters are drawn as outlines. Small sizes work best with a fine pen.", "Text"),
    Step("text_single", "Single-line text", "method", [], [
        {"key": "text", "label": "Text", "type": "textarea", "default": "", "maxlength": 4000,
         "placeholder": "Type or paste text. Each letter is drawn once, along its centre line."},
        {"key": "font", "label": "Font", "type": "select", "default": "Hershey Sans",
         "options": [["Hershey Sans", "Sans"], ["Hershey Sans Medium", "Sans, double stroke"],
                     ["Hershey Script", "Script"], ["Hershey Serif", "Serif"]]},
        {"key": "xheight", "label": "Letter height", "type": "number", "default": 4.5, "min": 1, "max": 40,
         "step": 0.5, "unit": "mm", "help": "Height of a small x."},
        {"key": "spacing", "label": "Line spacing", "type": "number", "default": 3, "min": 1.5, "max": 8,
         "step": 0.25, "unit": "x height"},
        {"key": "align", "label": "Align", "type": "select", "default": "left",
         "options": [["left", "Left"], ["center", "Centre"], ["right", "Right"]]},
        {"key": "width", "label": "Wrap at", "type": "number", "default": 0, "min": 0, "max": 400, "unit": "mm",
         "help": "0 = the canvas width."},
    ], _text_single, "A plotter font: one stroke per line of each letter, so text is quick and crisp.", "Text"),
    Step("text_art", "Text art", "method", [], [
        {"key": "text", "label": "Text", "type": "text", "default": "HELLO", "maxlength": 60},
        {"key": "effect", "label": "Effect", "type": "select", "default": "arc",
         "options": [["plain", "Plain"], ["arc", "Arc"], ["wave", "Wave"], ["rise", "Rise"], ["circle", "Circle"],
                     ["twist", "Twist"]]},
        {"key": "size", "label": "Size", "type": "number", "default": 20, "min": 5, "max": 150, "unit": "mm"},
        {"key": "bend", "label": "Bend", "type": "number", "default": 40, "min": 0, "max": 100, "slider": True},
        dict(FONT, default="DejaVu Sans"),
        {"key": "bold", "label": "Bold", "type": "bool", "default": False},
    ], _text_art, "A short word, bent along a curve.", "Text"),

    Step("simplify", "Simplify", "clean", ["image", "svg", "mesh", "none"], [
        {"key": "tolerance", "label": "Tolerance", "type": "number", "default": 0.2, "min": 0.01, "max": 3,
         "step": 0.01, "unit": "mm", "help": "Drops points that move the line less than this."},
    ], _simplify, "Fewer points: faster to plot, smoother motion."),
    Step("drop_short", "Remove short strokes", "clean", ["image", "svg", "mesh", "none"], [
        {"key": "min_mm", "label": "Shorter than", "type": "number", "default": 1.0, "min": 0.1, "max": 50,
         "step": 0.1, "unit": "mm"},
    ], _drop_short, "Specks and stray dots: each one costs a pen lift."),
    Step("join", "Join touching strokes", "clean", ["image", "svg", "mesh", "none"], [
        {"key": "tolerance", "label": "Gap up to", "type": "number", "default": 0.5, "min": 0.05, "max": 5,
         "step": 0.05, "unit": "mm"},
    ], _join, "One stroke where a line was drawn in pieces: fewer pen lifts."),
    Step("one_pen", "Merge all pens into one", "clean", ["image", "svg", "mesh", "none"], [], _one_pen,
         "Every layer becomes one ink: no pen swaps."),
]}

DEFAULT_METHOD = {"image": "contour", "svg": "svg", "mesh": "mesh"}


def public_steps():
    return [s.public() for s in STEPS.values()]


# ------------------------------------------------------------------ store and pipeline
class Workshop:
    """Sources and stage results under cache/prepare/. Files older than two days are removed on start."""

    def __init__(self, settings):
        self.s = settings
        self.dir = settings.cache / "prepare"
        self.dir.mkdir(parents=True, exist_ok=True)
        for old in self.dir.iterdir():
            try:
                if time.time() - old.stat().st_mtime > 2 * 24 * 3600:
                    old.unlink()
            except OSError:
                pass

    # ---- sources
    def add_source(self, raw, filename):
        from .library import safe_name
        safe = safe_name(filename)
        ext = pathlib.Path(safe).suffix.lower()
        kind = kind_of(ext)
        if not kind:
            exts = ", ".join(sorted(e for v in KINDS.values() for e in v))
            raise PrepareError(f"Files of type {ext or '(no extension)'} cannot be prepared. Use {exts}.")
        if not raw:
            raise PrepareError("The file is empty.")
        if len(raw) > MAX_SOURCE:
            raise PrepareError(f"The file is larger than {MAX_SOURCE // (1024 * 1024)} MB.")
        sid = hashlib.sha256(raw).hexdigest()[:16]
        meta = {"id": sid, "name": safe, "ext": ext, "kind": kind, "bytes": len(raw)}
        if kind == "image":
            meta.update(self._image_info(raw))
        elif kind == "svg":
            import xml.etree.ElementTree as ET
            try:
                ET.fromstring(raw)
            except ET.ParseError as ex:
                raise PrepareError(f"This SVG is not valid XML ({ex}).")
        self._atomic(self.dir / f"src_{sid}{ext}", raw)
        self._atomic(self.dir / f"src_{sid}.json", json.dumps(meta).encode())
        return meta

    def _image_info(self, raw):
        import io
        Image = _pil()[0]
        try:
            with Image.open(io.BytesIO(raw)) as im:
                return {"px": list(im.size)}
        except Exception as ex:  # noqa: BLE001
            raise PrepareError(f"This image cannot be read ({ex}).")

    def source(self, sid):
        if not isinstance(sid, str) or not SRC_ID.match(sid):
            return None
        mp = self.dir / f"src_{sid}.json"
        try:
            meta = json.loads(mp.read_text())
        except (OSError, ValueError):
            return None
        p = self.dir / f"src_{sid}{meta['ext']}"
        if not p.is_file():
            return None
        now = time.time()
        for f in (p, mp):
            try:
                os.utime(f, (now, now))       # in use: keep it past the two-day sweep
            except OSError:
                pass
        return dict(meta, path=p)

    def stage_file(self, key, suffix):
        if not isinstance(key, str) or not KEY.match(key):
            return None
        p = self.dir / f"st_{key}{suffix}"
        return p if p.is_file() else None

    @staticmethod
    def _atomic(path, data):
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
        tmp.write_bytes(data)
        tmp.replace(path)

    # ---- recipe
    def check(self, recipe):
        """-> (source meta or None, [(Step, params)] per stage). Raises PrepareError."""
        if not isinstance(recipe, dict):
            raise PrepareError("The recipe is missing.")
        src = None
        if recipe.get("source"):
            src = self.source(recipe["source"])
            if not src:
                raise PrepareError("The source file is gone from the server (sources are kept two days). "
                                   "Drop it in again.")
        kind = src["kind"] if src else "none"
        raw_steps = recipe.get("steps")
        if not isinstance(raw_steps, list) or len(raw_steps) > 40:
            raise PrepareError("The recipe's steps must be a list of at most 40.")
        staged = {st: [] for st in STAGES}
        for i, ent in enumerate(raw_steps):
            if not isinstance(ent, dict) or ent.get("step") not in STEPS:
                raise PrepareError(f"Step {i + 1} is not a known step.")
            if ent.get("on") is False:
                continue
            step = STEPS[ent["step"]]
            ok = kind in step.accepts if step.accepts else kind == "none"     # no accepts: a generator
            if not ok:
                what = {"none": "no source"}.get(kind, f"a {kind} source")
                raise PrepareError(f"{step.title} does not work on {what}.")
            staged[step.stage].append((step, clamp_params(step.params, ent.get("params"))))
        if len(staged["method"]) != 1:
            raise PrepareError("Choose one method." if not staged["method"] else "A recipe has exactly one method.")
        return src, staged

    def run(self, recipe, canvas):
        """-> {svg, image (adjusted PNG or None), mask, key, stats, source}. Cached per stage."""
        src, staged = self.check(recipe)
        h = hashlib.sha256(json.dumps({"src": src and src["id"], "canvas": canvas}).encode())
        cur = src["path"] if src else None
        image = None
        if src and src["kind"] == "image":
            # Every image passes through here once: alpha is flattened onto white paper.
            h.update(b"flatten")
            cur = self._image_stage(h, cur, None, None)
            image = cur
            for step, p in staged["adjust"]:
                h.update(json.dumps([step.id, p], sort_keys=True).encode())
                cur = self._image_stage(h, cur, step, p)
                image = cur
        step, p = staged["method"][0]
        h.update(json.dumps(["method", step.id, p], sort_keys=True).encode())
        key = h.hexdigest()[:24]
        svg, mask = self.dir / f"st_{key}.svg", self.dir / f"st_{key}_mask.png"
        stats_p = self.dir / f"st_{key}.json"
        if not (svg.is_file() and stats_p.is_file()):
            tmp = self.dir / f".m_{key}_{secrets.token_hex(4)}.svg"
            tmp_mask = self.dir / f".m_{key}_{secrets.token_hex(4)}_mask.png"
            try:
                stats = step.fn(cur, tmp, p, {"canvas": canvas, "mask": str(tmp_mask)}) or {}
            except PrepareError:
                raise
            except Exception as ex:  # noqa: BLE001
                raise PrepareError(f"{step.title} failed: {ex}")
            finally:
                if not tmp.is_file():
                    tmp_mask.unlink(missing_ok=True)
            if tmp_mask.is_file():
                tmp_mask.replace(mask)
            stats = {k: v for k, v in stats.items() if k != "mask"}
            self._atomic(stats_p, json.dumps(stats, default=str).encode())
            tmp.replace(svg)
        stats = json.loads(stats_p.read_text())
        for step_c, pc in staged["clean"]:
            h.update(json.dumps(["clean", step_c.id, pc], sort_keys=True).encode())
            nkey = h.hexdigest()[:24]
            out = self.dir / f"st_{nkey}.svg"
            if not out.is_file():
                d = _load_drawing(svg)
                try:
                    d = step_c.fn(d, pc)
                except PrepareError:
                    raise
                except Exception as ex:  # noqa: BLE001
                    raise PrepareError(f"{step_c.title} failed: {ex}")
                if not d["polys"]:
                    raise PrepareError(f"Nothing is left after {step_c.title}. Make it gentler.")
                tmp = self.dir / f".c_{nkey}_{secrets.token_hex(4)}.svg"
                _write_drawing(d, tmp)
                tmp.replace(out)
            svg, key = out, nkey
        return {"svg": svg, "key": key, "image": image, "mask": mask if mask.is_file() else None, "stats": stats,
                "source": src, "method": step}

    def _image_stage(self, h, cur, step, p):
        key = h.hexdigest()[:24]
        out = self.dir / f"st_{key}.png"
        if out.is_file():
            return out
        Image = _pil()[0]
        try:
            with Image.open(cur) as im:
                im.load()
                if step is None:
                    if im.mode in ("RGBA", "LA", "P"):
                        im = im.convert("RGBA")
                        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                        im = Image.alpha_composite(bg, im)
                    im = im.convert("RGB")
                    if max(im.size) > 2400:           # plenty for a trace capped at 500 px; keeps stages fast
                        im.thumbnail((2400, 2400))
                else:
                    im = step.fn(im.convert("RGB"), p)
                tmp = self.dir / f".i_{key}_{secrets.token_hex(4)}.png"
                im.save(tmp, "PNG")
                tmp.replace(out)
        except PrepareError:
            raise
        except Exception as ex:  # noqa: BLE001
            raise PrepareError(f"{step.title if step else 'Reading the image'} failed: {ex}")
        return out


# ------------------------------------------------------------------ presets
PRESET_KINDS = ("image", "svg", "mesh", "none")


class Presets:
    """Named recipes (steps with their settings, plus the fill), shared by every browser: results/web/presets.json."""

    def __init__(self, settings):
        self.path = settings.cache / "presets.json"

    def list(self):
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1))
        tmp.replace(self.path)

    def put(self, body):
        name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:60]
        if not name:
            raise PrepareError("Give the preset a name.")
        kind = body.get("kind")
        if kind not in PRESET_KINDS:
            raise PrepareError("Unknown source kind for the preset.")
        raw = body.get("steps")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 40:
            raise PrepareError("A preset needs the recipe's steps.")
        steps = []
        for ent in raw:
            if not isinstance(ent, dict) or ent.get("step") not in STEPS:
                raise PrepareError("The recipe has an unknown step.")
            st = STEPS[ent["step"]]
            steps.append({"step": st.id, "on": ent.get("on") is not False, "params": clamp_params(st.params, ent.get("params"))})
        if sum(1 for s in steps if STEPS[s["step"]].stage == "method" and s["on"]) != 1:
            raise PrepareError("A preset has exactly one method.")
        st = body.get("settings") if isinstance(body.get("settings"), dict) else {}
        try:
            fill = max(0.0, min(50.0, float(st.get("fill") or 0)))
            angle = max(0.0, min(180.0, float(st.get("fill_angle", 45))))
        except (TypeError, ValueError):
            fill, angle = 0.0, 45.0
        item = {"name": name, "kind": kind, "steps": steps, "settings": {"fill": fill, "fill_angle": angle},
                "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}
        items = [p for p in self.list() if p.get("name") != name] + [item]
        if len(items) > 200:
            raise PrepareError("At most 200 presets.")
        self._save(sorted(items, key=lambda p: p["name"].lower()))
        return item

    def delete(self, name):
        items = self.list()
        keep = [p for p in items if p.get("name") != name]
        if len(keep) == len(items):
            raise PrepareError("No preset by that name.")
        self._save(keep)


def recipe_for_entry(entry):
    """A recipe that rebuilds a library entry made before recipes existed (or None)."""
    if entry.get("recipe"):
        return entry["recipe"]
    kind = entry.get("kind")
    params = entry.get("params") or {}
    if kind == "raster":
        style = params.get("style") or "contour"
        return {"steps": [{"step": style if style in STEPS else "contour", "params": params}]}
    if kind in ("svg", "mesh"):
        return {"steps": [{"step": kind, "params": params}]}
    return None


def copy_source_from(workshop, entry, path):
    """Library entry original -> a Prepare source. -> source meta."""
    return workshop.add_source(path.read_bytes(), entry.get("name") or path.name)

