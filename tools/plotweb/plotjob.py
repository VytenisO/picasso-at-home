"""Plot settings: validation, the plot_svg.py command line, previews and plan ids.

UI placement is the drawing's bounding box in machine mm: x, y (top-left) and w (width). plot_svg.py places the
bbox top-left at canvas top-left + (--x0, --y0) with the carriage parked at the paper's top-left (anchor
top-left), and --width scales the SVG *document* width, so w is converted with the drawing's own geometry
(raw bbox width at scale 1 vs document width), measured by a first dry run.

A plan id is a hash of everything that decides the motion: the SVG bytes, machine.json, and the resolved
settings. Starting a run requires the plan id the operator previewed; any change means preview again.
"""
import hashlib
import json
import math
import pathlib
import re
import subprocess

from .config import LIMITS


class SettingsError(ValueError):
    pass


def _num(raw, key, lo, hi, default=None, allow_none=True, label=None):
    v = raw.get(key, default)
    if v is None or v == "":
        if allow_none:
            return None
        raise SettingsError(f"{label or key} is required")
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise SettingsError(f"{label or key} must be a number")
    if not math.isfinite(v) or not lo <= v <= hi:
        raise SettingsError(f"{label or key} must be between {lo:g} and {hi:g} (got {v:g})")
    return v


def validate(raw, bed, pens, layer_keys):
    """UI settings -> resolved settings. Raises SettingsError with a plain message."""
    raw = raw if isinstance(raw, dict) else {}
    sp, tr = LIMITS["speed"], LIMITS["travel_speed"]
    out = {
        "speed": _num(raw, "speed", sp["min"], sp["max"], sp["default"], False, "Drawing speed"),
        "travel_speed": _num(raw, "travel_speed", tr["min"], tr["max"], tr["default"], False, "Pen-up speed"),
        "x": _num(raw, "x", -1000, 1000, label="Position X"),
        "y": _num(raw, "y", -1000, 1000, label="Position Y"),
        "w": _num(raw, "w", 1, 1000, label="Width"),
        "paper": None,
    }
    fill = raw.get("fill")
    out["fill"] = 0.0 if fill in (None, "", 0, "0") else _num(raw, "fill", 0.2, 50, label="Fill spacing")
    out["fill_angle"] = _num(raw, "fill_angle", 0, 180, 45.0, False, "Fill angle")
    # Path planning: shorter travel is on unless switched off; duplicates are kept unless asked.
    out["optimize"] = raw.get("optimize", True) is not False
    out["dedupe"] = raw.get("dedupe") is True
    sa = raw.get("start_at") or 0
    if not isinstance(sa, int) or isinstance(sa, bool) or not 0 <= sa <= 1_000_000:
        raise SettingsError("start_at must be a whole number of strokes, 0 or more")
    out["start_at"] = sa
    cp = raw.get("copies")
    cp = 1 if cp is None else cp
    if not isinstance(cp, int) or isinstance(cp, bool) or not 1 <= cp <= 50:
        raise SettingsError("Copies must be a whole number from 1 to 50")
    out["copies"] = cp
    fin = raw.get("finish") or "start"
    if fin not in ("start", "stay", "clear"):
        raise SettingsError("finish must be start, stay or clear")
    out["finish"] = fin
    paper = raw.get("paper")
    if paper:
        px0, py0 = bed["paper"][0], bed["paper"][1]
        pw = _num(paper, "width", 20, 1000, allow_none=False, label="Paper width")
        ph = _num(paper, "height", 20, 1000, allow_none=False, label="Paper height")
        if px0 + pw > bed["bed"][0] or py0 + ph > bed["bed"][1]:
            raise SettingsError(f"A {pw:g} x {ph:g} mm sheet does not fit on the bed from the paper corner "
                                f"({px0:g}, {py0:g}) mm; the bed is {bed['bed'][0]:g} x {bed['bed'][1]:g} mm.")
        if pw <= 2 * bed["margin"] or ph <= 2 * bed["margin"]:
            raise SettingsError("The paper is smaller than its margins.")
        out["paper"] = {"width": pw, "height": ph}
    layers = raw.get("layers") or []
    if not isinstance(layers, list) or len(layers) > max(64, len(layer_keys)):
        raise SettingsError("layers must be a list")
    by_key = {}
    for i, ent in enumerate(layers):
        if not isinstance(ent, dict) or not isinstance(ent.get("key"), str):
            raise SettingsError(f"layer {i + 1}: missing key")
        pen = ent.get("pen") or None
        if pen is not None and pen not in pens:
            raise SettingsError(f"layer {i + 1}: unknown pen {pen!r}; known pens: {', '.join(sorted(pens)) or 'none'}")
        pr = LIMITS["pressure"]
        passes = ent.get("passes") or 1
        if not isinstance(passes, int) or isinstance(passes, bool) or not 1 <= passes <= 5:
            raise SettingsError(f"layer {i + 1}: passes must be 1 to 5")
        name = ent.get("name") or None
        if name is not None and (not isinstance(name, str) or len(name) > 40):
            raise SettingsError(f"layer {i + 1}: the name must be text of at most 40 characters")
        rank = ent.get("rank")
        if rank is not None and (not isinstance(rank, (int, float)) or isinstance(rank, bool) or not math.isfinite(rank)):
            raise SettingsError(f"layer {i + 1}: rank must be a number")
        by_key[ent["key"]] = {
            "key": ent["key"], "enabled": ent.get("enabled", True) is not False, "pen": pen,
            "speed": _num(ent, "speed", sp["min"], sp["max"], label=f"Layer {i + 1} speed"),
            "pressure": _num(ent, "pressure", pr["min"], pr["max"], label=f"Layer {i + 1} pressure"),
            "passes": passes, "name": name, "rank": rank,
        }
    # Ordered like the drawing's layers; settings for layers the file does not have are dropped.
    out["layers"] = [by_key.get(k) or {"key": k, "enabled": True, "pen": None, "speed": None, "pressure": None,
                                       "passes": 1, "name": None, "rank": None} for k in layer_keys]
    out["fills"] = validate_fills(raw.get("fills"))
    if layer_keys and not any(ls["enabled"] for ls in out["layers"]):
        raise SettingsError("Every layer is switched off, so there is nothing to plot.")
    return out


def validate_fills(raw):
    """Per fill-ink settings: [{key, mode: hatch|contour|off, spacing, angle, cross, zigzag}], sorted by key."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > 64:
        raise SettingsError("fills must be a list")
    out = {}
    for i, f in enumerate(raw):
        if not isinstance(f, dict) or not isinstance(f.get("key"), str) or len(f["key"]) > 80:
            raise SettingsError(f"fill {i + 1}: missing key")
        mode = f.get("mode")
        if mode in (None, "", "default"):
            continue
        if mode not in ("hatch", "contour", "off"):
            raise SettingsError(f"fill {i + 1}: mode must be hatch, contour or off")
        e = {"key": f["key"], "mode": mode}
        if mode != "off":
            e["spacing"] = _num(f, "spacing", 0.2, 50, 1.0, False, f"Fill {i + 1} spacing")
            e["angle"] = _num(f, "angle", 0, 180, 45.0, False, f"Fill {i + 1} angle")
            e["cross"] = f.get("cross") is True
            e["zigzag"] = f.get("zigzag", True) is not False
        out[f["key"]] = e
    return [out[k] for k in sorted(out)]


def canvas_of(bed, paper=None):
    px0, py0 = bed["paper"][0], bed["paper"][1]
    pw = paper["width"] if paper else bed["paper"][2]
    ph = paper["height"] if paper else bed["paper"][3]
    m = bed["margin"]
    return [px0 + m, py0 + m, px0 + pw - m, py0 + ph - m]


def resolve_placement(v, bed, geom):
    """Fill in x, y, w: default top-left of the canvas, natural size, shrunk to fit the canvas if needed."""
    c = canvas_of(bed, v["paper"])
    if v["x"] is None:
        v["x"] = c[0]
    if v["y"] is None:
        v["y"] = c[1]
    if v["w"] is None and geom and geom.get("raw_w", 0) > 1e-6:
        rw, rh = geom["raw_w"], max(geom.get("raw_h", 0.0), 1e-6)
        fit = min(1.0, (c[2] - c[0]) / rw, (c[3] - c[1]) / rh)
        v["w"] = round(rw * fit, 2)
    return v


def plan_id(svg_bytes, machine_bytes, resolved):
    h = hashlib.sha256()
    h.update(hashlib.sha256(svg_bytes).digest())
    h.update(hashlib.sha256(machine_bytes).digest())
    h.update(json.dumps(resolved, sort_keys=True).encode())
    return h.hexdigest()[:20]


def build_argv(s, svg_path, v, mode, geom, *, preview_json=None, layers_json=None, sim=False, sim_speed=None,
               control=False, machine=None, bed=None):
    argv = [s.python, str(s.root / "tools" / "plot_svg.py"), str(svg_path), "--mode", mode,
            "--config", str(machine or s.machine_path), "--anchor", "top-left",
            "--speed", f"{v['speed']:g}", "--travel-speed", f"{v['travel_speed']:g}"]
    if v.get("paper"):
        argv += ["--paper-width", f"{v['paper']['width']:g}", "--paper-height", f"{v['paper']['height']:g}"]
    bed = bed or s.bed()
    c = canvas_of(bed, v.get("paper"))
    if v.get("absolute"):
        argv.append("--absolute")      # a composed document: its millimetres are machine millimetres
    elif v.get("x") is not None:
        argv += ["--x0", f"{v['x'] - c[0]:.3f}", "--y0", f"{v['y'] - c[1]:.3f}"]
    if v.get("w") and geom and geom.get("raw_w", 0) > 1e-6 and not v.get("absolute"):
        argv += ["--width", f"{v['w'] * geom['doc_w'] / geom['raw_w']:.4f}"]
    if v.get("optimize"):
        argv.append("--optimize")
    if v.get("dedupe"):
        argv.append("--dedupe")
    if v.get("start_at"):
        argv += ["--start-at", str(int(v["start_at"]))]
    if (v.get("copies") or 1) > 1:
        argv += ["--copies", str(int(v["copies"]))]
    if v.get("finish") and v["finish"] != "start":
        argv += ["--finish", v["finish"]]
    if v.get("fill"):
        argv += ["--fill", f"{v['fill']:g}", "--fill-angle", f"{v.get('fill_angle', 45.0):g}"]
    if layers_json:
        argv += ["--layers", str(layers_json)]
        fj = pathlib.Path(layers_json).with_name("fills.json")
        if v.get("fills") and fj.exists():
            argv += ["--fills", str(fj)]
    if preview_json:
        argv += ["--preview-json", str(preview_json)]
    if mode != "dry":
        argv.append("--track")
    if control:
        argv.append("--control")
    if sim:
        argv.append("--sim")
        if sim_speed:
            argv += ["--sim-speed", f"{sim_speed:g}"]
    return argv


def run_dry(argv, timeout=240):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"The dry run took longer than {timeout} s and was stopped. The drawing may be too complex."
    except OSError as ex:
        return -1, f"Could not run plot_svg.py: {ex}"


def friendly_problems(summary, output):
    """Plain-language versions of the placement problems, plus the raw PROBLEM lines."""
    out = []
    if summary:
        b, c = summary.get("bbox_machine_mm"), summary.get("region_mm") or summary.get("canvas_mm")
        if b and c:
            for amount, text in ((c[0] - b[0], "left"), (c[1] - b[1], "top"), (b[2] - c[2], "right"),
                                 (b[3] - c[3], "bottom")):
                if amount > 0.5:
                    out.append(f"The drawing runs {amount:.1f} mm past the {text} edge of the canvas. "
                               "Move it or make it smaller.")
    raw = [ln[len("PROBLEM: "):] for ln in output.splitlines() if ln.startswith("PROBLEM: ")]
    placement = ("canvas", "region", "travel is", "edge margin")   # already said in plain words above
    if not out:
        out = raw[:]
    else:
        out += [r for r in raw if not any(k in r for k in placement)]
    if not raw and not out and output.strip():
        tail = [ln for ln in output.strip().splitlines() if ln.strip() and not ln.startswith("__")]
        if tail:
            out.append(tail[-1])
    return out


def parse_placement(output):
    for line in output.splitlines():
        if line.startswith("__PLACEMENT__:"):
            try:
                return json.loads(line[len("__PLACEMENT__:"):])
            except ValueError:
                return None
    return None


SAFE_ID = re.compile(r"^[0-9a-f]{20}$")
