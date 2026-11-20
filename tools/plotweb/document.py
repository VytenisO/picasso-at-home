"""The Plot document: several library drawings on the bed, each with its own transform, plus the job settings.

An object is {id, file, x, y, w, h, rot, fx, fy, name?}. (x, y, w, h) is its box in machine mm before rotation;
it turns by `rot` degrees (clockwise on the bed, y points toward the operator) about the box centre, and fx / fy
mirror it inside the box. The drawing's own geometry box (its strokes at natural size, in document mm) is stretched
to fill (w, h), so width and height scale independently.

For a preview or a run the objects are composed into one bed-sized SVG whose millimetres are machine millimetres
and plotted with `plot_svg.py --absolute`: the planner sees one drawing, so layers, pen changes, the time estimate
and every safety check work as they do for a single file.

The working document lives on the server (results/web/document.json) so a reload or another device sees the same
bed. Saved projects are named copies under results/web/projects/.
"""
import copy
import hashlib
import json
import math
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET

from .config import LIMITS

MAX_OBJECTS = 64
SVG_NS = "http://www.w3.org/2000/svg"
# Attributes of an embedded file's root <svg> that place it, rather than style its content.
ROOT_ONLY = {"width", "height", "viewBox", "x", "y", "version", "preserveAspectRatio", "id", "baseProfile"}


class DocError(ValueError):
    pass


def _f(raw, key, lo, hi, default=None, label=None):
    v = raw.get(key, default)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise DocError(f"{label or key} must be a number")
    if not math.isfinite(v) or not lo <= v <= hi:
        raise DocError(f"{label or key} must be between {lo:g} and {hi:g} (got {v:g})")
    return v


def empty():
    return {"rev": 0, "name": "", "objects": [], "settings": {
        "speed": LIMITS["speed"]["default"], "travel_speed": LIMITS["travel_speed"]["default"],
        "fill": 0.0, "fill_angle": 45.0, "paper": None, "layers": []}}


def clean_objects(raw, known_files):
    """Validated object list. Objects whose file left the library are dropped, not refused."""
    if not isinstance(raw, list):
        raise DocError("objects must be a list")
    if len(raw) > MAX_OBJECTS:
        raise DocError(f"At most {MAX_OBJECTS} objects on the bed.")
    out, seen = [], set()
    for i, o in enumerate(raw):
        if not isinstance(o, dict):
            raise DocError(f"object {i + 1}: not an object")
        if o.get("file") not in known_files:
            continue
        oid = str(o.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", oid) or oid in seen:
            oid = secrets.token_hex(4)
        seen.add(oid)
        n = f"Object {i + 1}"
        rot = _f(o, "rot", -1e6, 1e6, 0.0, n + " rotation") % 360.0
        out.append({
            "id": oid, "file": o["file"],
            "x": _f(o, "x", -1000, 1000, label=n + " X"), "y": _f(o, "y", -1000, 1000, label=n + " Y"),
            "w": _f(o, "w", 0.1, 2000, label=n + " width"), "h": _f(o, "h", 0.1, 2000, label=n + " height"),
            "rot": round(rot, 6), "fx": bool(o.get("fx")), "fy": bool(o.get("fy")),
            # the drawing's geometry hash when it was placed: the page re-fits the box if the drawing changed
            "g": re.sub(r"[^0-9a-f]", "", str(o.get("g") or ""))[:16],
        })
        if o.get("locked") is True:
            out[-1]["locked"] = True
        seam = o.get("seam")
        # the start point for closed shapes, as fractions of the object's box, so it moves with the object
        if isinstance(seam, list) and len(seam) == 2 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in seam):
            out[-1]["seam"] = [round(float(seam[0]), 4), round(float(seam[1]), 4)]
        grp = str(o.get("grp") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", grp):
            out[-1]["grp"] = grp
        ink = str(o.get("ink") or "").lower()
        if re.fullmatch(r"#[0-9a-f]{6}", ink):     # every line of the object in this colour: it moves to that layer
            out[-1]["ink"] = ink
    return out


def object_matrix(o, geom):
    """SVG matrix (a, b, c, d, e, f) from the drawing's document mm to machine mm."""
    bx, by = geom.get("raw_x", 0.0), geom.get("raw_y", 0.0)
    bw, bh = geom["raw_w"], geom["raw_h"]
    sx = o["w"] / bw if bw > 1e-6 else 1.0
    sy = o["h"] / bh if bh > 1e-6 else sx
    if bw <= 1e-6:
        sx = sy
    sx, sy = (-sx if o["fx"] else sx), (-sy if o["fy"] else sy)
    t = math.radians(o["rot"])
    co, si = math.cos(t), math.sin(t)
    cx, cy = o["x"] + o["w"] / 2, o["y"] + o["h"] / 2
    mx, my = bx + bw / 2, by + bh / 2
    # translate(cx, cy) rotate(t) scale(sx, sy) translate(-mx, -my)
    a, b, c, d = co * sx, si * sx, -si * sy, co * sy
    return (a, b, c, d, cx - a * mx - c * my, cy - b * mx - d * my)


def corners(o):
    """The rotated box's four corners in machine mm."""
    t = math.radians(o["rot"])
    co, si = math.cos(t), math.sin(t)
    cx, cy = o["x"] + o["w"] / 2, o["y"] + o["h"] / 2
    return [(cx + co * dx - si * dy, cy + si * dx + co * dy)
            for dx, dy in ((-o["w"] / 2, -o["h"] / 2), (o["w"] / 2, -o["h"] / 2), (o["w"] / 2, o["h"] / 2),
                           (-o["w"] / 2, o["h"] / 2))]


def _doc_scale(root):
    """(mm per user unit, viewBox x, viewBox y), the same reading plot_svg.load_svg makes."""
    import plot_svg
    vb = root.get("viewBox")
    w_mm, h_mm = plot_svg.parse_len(root.get("width")), plot_svg.parse_len(root.get("height"))
    px = plot_svg.UNIT_MM["px"]
    if vb:
        vx, vy, vw, vh = (float(v) for v in re.split(r"[\s,]+", vb.strip()))
    else:
        if w_mm is None or h_mm is None:
            raise DocError("A drawing has neither a viewBox nor a width and height.")
        vx = vy = 0.0
        vw, vh = w_mm / px, h_mm / px
    w_mm = vw * px if w_mm is None else w_mm
    h_mm = vh * px if h_mm is None else h_mm
    return min(w_mm / vw, h_mm / vh), vx, vy


def compose(objects, files, bed):
    """-> SVG bytes: every object's drawing, transformed into a bed-sized document in machine mm.

    files: {file id: (svg path, geometry)}. Later objects come later in the file, so with one layer they plot
    after the earlier ones."""
    bw, bh = bed["bed"]
    ET.register_namespace("", SVG_NS)
    root = ET.Element(f"{{{SVG_NS}}}svg", {"width": f"{bw:g}mm", "height": f"{bh:g}mm", "viewBox": f"0 0 {bw:g} {bh:g}"})
    parsed = {}
    for n, o in enumerate(objects):
        path, geom = files[o["file"]]
        if o["file"] not in parsed:
            parsed[o["file"]] = ET.parse(path).getroot()
        src = parsed[o["file"]]
        s, vx, vy = _doc_scale(src)
        a, b, c, d, e, f = object_matrix(o, geom)
        outer = ET.SubElement(root, f"{{{SVG_NS}}}g", {
            "id": f"obj-{o['id']}", "data-obj": str(n),
            "transform": f"matrix({a:.9g} {b:.9g} {c:.9g} {d:.9g} {e:.9g} {f:.9g})"})
        if o.get("seam"):                   # in machine mm: the nearest vertex of each closed stroke starts there
            outer.set("data-seam", f"{o['x'] + o['seam'][0] * o['w']:.3f} {o['y'] + o['seam'][1] * o['h']:.3f}")
        attrs = {k: v for k, v in src.attrib.items() if k not in ROOT_ONLY and not k.startswith("{")}
        attrs["transform"] = f"scale({s:.9g}) translate({-vx:.9g} {-vy:.9g})"
        inner = ET.SubElement(outer, f"{{{SVG_NS}}}g", attrs)
        for child in src:
            inner.append(copy.deepcopy(child))
        if o.get("ink"):
            recolor(inner, o["ink"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


_STYLE_COLOR = re.compile(r"((?:^|;)\s*(?:stroke|fill)\s*:\s*)([^;]+)")


def recolor(group, ink):
    """Every stroke and fill colour under group becomes ink; 'none' stays none. Lines that set no colour inherit
    it from the group."""
    group.set("stroke", ink)
    for el in group.iter():
        for prop in ("stroke", "fill"):
            v = el.get(prop)
            if v and v.strip().lower() not in ("none", "transparent") and el is not group:
                el.set(prop, ink)
        st = el.get("style")
        if st:
            el.set("style", _STYLE_COLOR.sub(
                lambda m: m.group(0) if m.group(2).strip().lower() in ("none", "transparent") else m.group(1) + ink, st))


def bed_key(objects, settings):
    """Hash of what decides the plot order, without the resume point: a stopped plot can resume only on the same
    bed. Objects as validated by clean_objects."""
    st = {k: v for k, v in (settings or {}).items() if k not in ("start_at", "guides")}    # guides are never plotted
    raw = json.dumps({"o": [{k: o.get(k) for k in ("file", "x", "y", "w", "h", "rot", "fx", "fy", "ink", "seam")} for o in objects],
                      "s": st}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def project_name(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:80]
    if not name:
        raise DocError("Give the project a name.")
    return name


def project_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "project"


class DocStore:
    """The working document and the saved projects. One lock; every write bumps rev and emits a 'doc' event."""

    def __init__(self, settings, lib, events):
        self.s, self.lib, self.ev = settings, lib, events
        self.path = settings.cache / "document.json"
        self.proj_dir = settings.cache / "projects"
        self.proj_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _read(self):
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and isinstance(d.get("objects"), list):
                return d
        except (OSError, ValueError):
            pass
        return empty()

    def _write(self, d):
        tmp = self.path.with_suffix(f".{secrets.token_hex(3)}.tmp")
        tmp.write_text(json.dumps(d, indent=1))
        tmp.replace(self.path)
        self.ev.emit("doc", rev=d["rev"], client=d.get("client") or "")

    def get(self):
        with self.lock:
            d = self._read()
            known = {e["id"] for e in self.lib.list()}
            gone = [o for o in d["objects"] if o.get("file") not in known]
            if gone:
                d["objects"] = [o for o in d["objects"] if o.get("file") in known]
            return d

    def put(self, objects, settings, name=None, client=None):
        """Replace the working document. settings are stored as given; a preview validates them."""
        known = {e["id"] for e in self.lib.list()}
        objs = clean_objects(objects, known)
        st = settings if isinstance(settings, dict) else {}
        g = st.get("guides")
        if g is not None and (not isinstance(g, list) or len(g) > 100 or not all(
                isinstance(x, dict) and x.get("axis") in ("x", "y") and isinstance(x.get("at"), (int, float))
                and math.isfinite(x["at"]) for x in g)):
            raise DocError("guides must be a list of {axis: x|y, at: mm}")
        only = st.get("only")
        if only is not None and (not isinstance(only, list) or len(only) > MAX_OBJECTS
                                 or not all(isinstance(x, str) and len(x) <= 40 for x in only)):
            raise DocError("only must be a list of object ids")
        keep = {k: st.get(k) for k in ("speed", "travel_speed", "fill", "fill_angle", "paper", "layers", "optimize",
                                       "dedupe", "fills", "guides", "only", "copies", "finish", "sweep", "by_object",
                                       "keep_motors", "simplify", "corner_starts", "inner_first", "approach",
                                       "start_delay", "end_delay", "trace_shape", "trace_repeat", "notes", "overlaps",
                                       "origin", "origin_anchor")}
        if keep.get("notes") is not None:
            keep["notes"] = str(keep["notes"])[:2000]
        with self.lock:
            d = self._read()
            d["objects"] = objs
            d["settings"] = {**empty()["settings"], **{k: v for k, v in keep.items() if v is not None or k == "paper"}}
            if name is not None:
                d["name"] = str(name)[:80]
            d["rev"] = int(d.get("rev", 0)) + 1
            d["client"] = str(client or "")[:40]
            d["saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._write(d)
            return d

    # ------------------------------------------------------------ projects
    def projects(self):
        out = []
        for p in sorted(self.proj_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text())
                out.append({"slug": p.stem, "name": d.get("name") or p.stem, "objects": len(d.get("objects") or []),
                            "saved": d.get("saved"), "opened": d.get("opened"), "template": bool(d.get("template")),
                            "notes": ((d.get("settings") or {}).get("notes") or "")[:200]})
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda x: x.get("saved") or "", reverse=True)

    def save_project(self, name, template=False):
        name = project_name(name)
        with self.lock:
            d = self._read()
            d["name"], d["client"] = name, ""
            d["saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            slug = project_slug(name)
            old = {}
            try:
                old = json.loads((self.proj_dir / f"{slug}.json").read_text())
            except (OSError, ValueError):
                pass
            (self.proj_dir / f"{slug}.json").write_text(json.dumps(
                {**{k: d.get(k) for k in ("name", "objects", "settings", "saved")},
                 "template": bool(template), "opened": old.get("opened")}, indent=1))
            d["rev"] = int(d.get("rev", 0)) + 1
            self._write(d)
        return slug

    def open_project(self, slug):
        if not re.fullmatch(r"[a-z0-9-]{1,60}", slug or ""):
            raise DocError("No such project.")
        p = self.proj_dir / f"{slug}.json"
        try:
            saved = json.loads(p.read_text())
        except (OSError, ValueError):
            raise DocError("No such project.")
        known = {e["id"] for e in self.lib.list()}
        missing = sum(1 for o in saved.get("objects") or [] if o.get("file") not in known)
        # a template opens as a fresh copy: no name, so saving it asks for a new one and the template stays
        d = self.put(saved.get("objects") or [], saved.get("settings") or {},
                     name="" if saved.get("template") else (saved.get("name") or slug))
        try:
            saved["opened"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            p.write_text(json.dumps(saved, indent=1))
        except OSError:
            pass
        return d, missing

    def delete_project(self, slug):
        if not re.fullmatch(r"[a-z0-9-]{1,60}", slug or ""):
            raise DocError("No such project.")
        p = self.proj_dir / f"{slug}.json"
        if not p.exists():
            raise DocError("No such project.")
        p.unlink()
