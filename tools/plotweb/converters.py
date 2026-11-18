"""Upload converters. Each turns an uploaded file into a plot-ready SVG.

To add a converter: write a function fn(src_path, dst_svg_path, params, ctx) -> stats dict, and add a
Converter(...) to REGISTRY with its file extensions and a params schema. The UI builds the settings form from the
schema (GET /api/converters) and the server clamps every value to it before calling fn.

Schema entries: {key, label, type: number|select|bool|text, default, min, max, step, options, help}
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from image_to_svg import convert as convert_raster  # noqa: E402
from mesh_to_svg import convert as convert_mesh  # noqa: E402
from svg_vector import prepare as prepare_svg  # noqa: E402

IMG = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")


class Converter:
    def __init__(self, cid, title, exts, params, fn, previewable=False, help=""):
        self.id, self.title, self.exts, self.params, self.fn = cid, title, tuple(exts), params, fn
        self.previewable, self.help = previewable, help

    def public(self):
        return {"id": self.id, "title": self.title, "exts": list(self.exts), "params": self.params,
                "previewable": self.previewable, "help": self.help}


def clamp_params(schema, raw):
    """Values from the client -> values inside the schema (defaults for anything missing or invalid)."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for p in schema:
        k, t, v = p["key"], p["type"], raw.get(p["key"], p.get("default"))
        if t == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = p["default"]
            if v != v:     # NaN
                v = p["default"]
            v = max(p.get("min", v), min(p.get("max", v), v))
            if p.get("integer"):
                v = int(round(v))
        elif t == "select":
            if v not in [o[0] for o in p["options"]]:
                v = p["default"]
        elif t == "bool":
            v = v in (True, "true", "1", 1, "on", "yes")
        else:
            v = str(v if v is not None else p.get("default", ""))[:p.get("maxlength", 80)]
        out[k] = v
    return out


def _svg(src, dst, params, ctx):
    return {"vector": prepare_svg(src, dst, font_family=params.get("font") or "DejaVu Sans")}


def _raster(src, dst, params, ctx):
    c = ctx["canvas"]
    max_w, max_h = c[2] - c[0], c[3] - c[1]
    levels = params["levels"] if params["levels"] != 1 else 0
    stats = convert_raster(src, dst, width_mm=min(params["width_mm"], max_w), threshold=params["threshold"],
                           blur=params["blur"], style=params["style"], density=params["density"],
                           inks=params["inks"], levels=levels, max_width_mm=max_w, max_height_mm=max_h,
                           single_pen=params["single_pen"], mask=ctx.get("mask"))
    return {"conversion": stats, "style": params["style"], "mask": stats.get("mask")}


def _mesh(src, dst, params, ctx):
    stats = convert_mesh(src, dst, yaw=params["yaw"], pitch=params["pitch"], width_mm=params["width_mm"])
    return {"conversion": stats}


REGISTRY = {c.id: c for c in [
    Converter("svg", "SVG drawing", [".svg"], [
        {"key": "font", "label": "Font for text", "type": "text", "default": "DejaVu Sans",
         "help": "Text in the SVG is turned into outlines with this font."},
    ], _svg, help="Vector drawings are plotted as they are: outlines. Set a hatch spacing under Fill to fill coloured shapes."),
    Converter("raster", "Photo or image", IMG, [
        {"key": "style", "label": "Style", "type": "select", "default": "contour",
         "options": [["contour", "Contours (outlines of dark shapes)"], ["lineart", "Line art (centre lines)"],
                     ["halftone", "Halftone dots"], ["hatch", "Shading by hatching (fill)"],
                     ["one-line", "One continuous line"]]},
        {"key": "inks", "label": "Pens (ink layers)", "type": "number", "default": 1, "min": 1, "max": 4,
         "step": 1, "integer": True, "help": "More pens = darker tones in separate layers, one pen swap each."},
        {"key": "levels", "label": "Tone levels", "type": "number", "default": 0, "min": 0, "max": 6, "step": 1,
         "integer": True, "help": "0 = off. 2-6 posterise the image first."},
        {"key": "width_mm", "label": "Width on paper", "type": "number", "default": 120, "min": 10, "max": 1000,
         "step": 1, "unit": "mm"},
        {"key": "threshold", "label": "Dark cutoff", "type": "number", "default": 150, "min": 0, "max": 255,
         "step": 1, "integer": True, "help": "Pixels darker than this count as ink."},
        {"key": "density", "label": "Density", "type": "number", "default": 40, "min": 1, "max": 100, "step": 1,
         "integer": True},
        {"key": "blur", "label": "Blur", "type": "number", "default": 1.0, "min": 0, "max": 10, "step": 0.5,
         "unit": "px"},
        {"key": "single_pen", "label": "One pen only (no swaps)", "type": "bool", "default": False},
    ], _raster, previewable=True, help="Images are traced into pen paths. Preview before adding."),
    Converter("mesh", "3D model", [".stl", ".obj"], [
        {"key": "yaw", "label": "View yaw", "type": "number", "default": 35, "min": -180, "max": 180, "unit": "deg"},
        {"key": "pitch", "label": "View pitch", "type": "number", "default": 25, "min": -89, "max": 89,
         "unit": "deg"},
        {"key": "width_mm", "label": "Size on paper", "type": "number", "default": 120, "min": 20, "max": 267,
         "unit": "mm"},
    ], _mesh, previewable=True, help="Drawn as a wireframe with hidden lines removed."),
]}


def for_extension(ext):
    ext = ext.lower()
    return next((c for c in REGISTRY.values() if ext in c.exts), None)


def all_extensions():
    return sorted(e for c in REGISTRY.values() for e in c.exts)
