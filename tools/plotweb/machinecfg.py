"""Edits of config/machine.json and config/pens.json from the UI: calibration, paper, pen profiles.

Every write keeps the previous file under results/web/backups/ and replaces the file atomically. The servo
pen-up/pen-down values are never written here: they are tuned by hand with a pen mounted (pen height test).
"""
import datetime
import json
import math
import re
import shutil

from .config import LIMITS

PEN_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
PEN_FIELDS = ("description", "colour", "colour_hex", "width_mm", "speed", "pressure", "expected_use", "notes",
              "hatch_mm", "dwell_ms")


class ConfigError(ValueError):
    pass


def _num(v, lo, hi, label):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not lo <= v <= hi:
        raise ConfigError(f"{label} must be a number between {lo:g} and {hi:g}.")
    return float(v)


def _write(settings, path, data):
    backups = settings.cache / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if path.exists():
        shutil.copy2(path, backups / f"{path.stem}_{stamp}{path.suffix}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def calibrate(settings, axis, commanded, measured):
    """Scale steps/mm of one axis by commanded / measured. -> (old, new)."""
    if axis not in ("x", "y"):
        raise ConfigError("Axis must be x or y.")
    commanded = _num(commanded, 10, 400, "The commanded length")
    measured = _num(measured, 5, 500, "The measured length")
    ratio = commanded / measured
    if not 0.8 <= ratio <= 1.25:
        raise ConfigError(f"Measured {measured:g} mm for {commanded:g} mm commanded is more than 20% off. Check the "
                          "measurement (and the axis): a calibration that far out is more likely a misread ruler.")
    cfg = json.loads(settings.machine_path.read_text())
    old = float(cfg["steps_per_mm"][axis])
    new = round(old * ratio, 3)
    cfg["steps_per_mm"][axis] = new
    cfg.setdefault("steps_per_inch", {})[axis] = round(new * 25.4, 1)
    today = datetime.date.today().isoformat()
    cfg["measured"] = today
    cfg["notes"] = (cfg.get("notes") or "") + (f" | {today} web calibration of {axis.upper()}: {commanded:g} mm commanded "
                                               f"measured {measured:g} mm, steps/mm {old:g} -> {new:g}.")
    _write(settings, settings.machine_path, cfg)
    return old, new


def set_paper(settings, raw):
    """Paper corner, size and margin. The sheet must lie on the bed."""
    raw = raw if isinstance(raw, dict) else {}
    cfg = json.loads(settings.machine_path.read_text())
    bx, by = float(cfg["travel_mm"]["x"]), float(cfg["travel_mm"]["y"])
    x0 = _num(raw.get("x0"), 0, bx - 20, "Paper left edge")
    y0 = _num(raw.get("y0"), 0, by - 20, "Paper top edge")
    w = _num(raw.get("width"), 20, 1000, "Paper width")
    h = _num(raw.get("height"), 20, 1000, "Paper height")
    m = _num(raw.get("margin_mm"), 0, 50, "Margin")
    if x0 + w > bx + 0.01 or y0 + h > by + 0.01:
        raise ConfigError(f"A {w:g} x {h:g} mm sheet at ({x0:g}, {y0:g}) mm runs past the bed ({bx:g} x {by:g} mm).")
    if 2 * m >= min(w, h):
        raise ConfigError("The margin leaves no canvas.")
    cfg["paper"] = {"x0": x0, "y0": y0, "width": w, "height": h, "margin_mm": m}
    _write(settings, settings.machine_path, cfg)
    return cfg["paper"]


def set_machine(settings, raw):
    """Travel, axis directions and the speed limits (each at most the driver's ceiling). Steps/mm only through
    calibration, the servo only through the pen height test."""
    from .config import CEILINGS
    raw = raw if isinstance(raw, dict) else {}
    cfg = json.loads(settings.machine_path.read_text())
    tx = _num(raw.get("travel_x"), 50, 2000, "Travel X")
    ty = _num(raw.get("travel_y"), 50, 2000, "Travel Y")
    p = cfg.get("paper") or {}
    if p and (p["x0"] + p["width"] > tx + 0.01 or p["y0"] + p["height"] > ty + 0.01):
        raise ConfigError(f"The paper ({p['width']:g} x {p['height']:g} mm at {p['x0']:g}, {p['y0']:g}) would run past a "
                          f"{tx:g} x {ty:g} mm bed. Move or shrink the paper first.")
    signs = {}
    for ax in ("x", "y"):
        s = raw.get(f"sign_{ax}")
        if s not in (1, -1):
            raise ConfigError(f"The {ax.upper()} direction must be 1 or -1.")
        signs[ax] = s
    lim = {}
    for key, label in (("speed_max", "Drawing speed limit"), ("travel_max", "Pen-up speed limit"),
                       ("speed_default", "Default drawing speed"), ("travel_default", "Default pen-up speed")):
        ceiling = CEILINGS["travel_speed" if key.startswith("travel") else "speed"]
        lim[key] = _num(raw.get(key), 1, ceiling, label)
    if lim["speed_default"] > lim["speed_max"] or lim["travel_default"] > lim["travel_max"]:
        raise ConfigError("A default speed cannot be above its limit.")
    cfg["travel_mm"] = {"x": tx, "y": ty}
    for ax in ("x", "y"):
        cfg["axis"][ax]["sign"] = signs[ax]
    cfg["limits"] = lim
    _write(settings, settings.machine_path, cfg)
    return cfg


def save_pen(settings, name, fields, rename_from=None):
    """Create or edit a pen profile. Test results recorded by the tools are kept."""
    if not isinstance(name, str) or not PEN_NAME.match(name):
        raise ConfigError("A pen name is 1 to 30 lowercase letters, digits, - or _, starting with a letter or digit.")
    fields = fields if isinstance(fields, dict) else {}
    pens = settings.pens()
    if rename_from is not None and rename_from not in pens:
        raise ConfigError(f"There is no pen {rename_from!r} to rename.")
    if (rename_from or name) != name and name in pens:
        raise ConfigError(f"A pen called {name!r} exists already.")
    base = dict(pens.get(rename_from or name) or {})
    out = {}
    for k in PEN_FIELDS:
        v = fields.get(k, base.get(k))
        if v in ("", None):
            out[k] = None
            continue
        if k == "colour_hex":
            if not isinstance(v, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                raise ConfigError("The colour must be like #1a1a1a.")
            v = v.lower()
        elif k == "width_mm":
            v = _num(v, 0.05, 10, "Line width")
        elif k == "speed":
            v = _num(v, LIMITS["speed"]["min"], LIMITS["speed"]["max"], "Speed")
        elif k == "pressure":
            v = _num(v, 0, LIMITS["pressure"]["max"], "Pressure")
        elif k == "hatch_mm":
            v = _num(v, 0.2, 10, "Best hatch spacing")
        elif k == "dwell_ms":
            v = _num(v, 0, 3000, "Dwell")
        else:
            v = str(v)[:400]
        out[k] = v
    keep = {k: v for k, v in base.items() if k not in PEN_FIELDS}       # recorded test results
    entry = {**keep, **{k: v for k, v in out.items() if v is not None or k in base}}
    new = {}
    for k, v in pens.items():                    # keep the order; a renamed pen stays in its place
        if k == (rename_from or name):
            new[name] = entry
        else:
            new[k] = v
    if name not in new:
        new[name] = entry
    _write(settings, settings.pens_path, new)
    return new


BUNDLE_KIND = "pen-plotter-settings"


def backup_bundle(settings, projects_dir):
    """Machine, pens, colour-to-pen memory and saved projects as one JSON document."""
    def read(p, default):
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return default
    projects = {}
    for p in sorted(projects_dir.glob("*.json")):
        d = read(p, None)
        if isinstance(d, dict):
            projects[p.stem] = d
    return {"kind": BUNDLE_KIND, "version": 1, "saved": datetime.datetime.now().isoformat(timespec="seconds"),
            "machine": read(settings.machine_path, {}), "pens": read(settings.pens_path, {}),
            "pen_map": read(settings.cache / "pen_map.json", {}), "projects": projects}


def check_machine(cfg):
    """A machine.json from a backup: the fields every run depends on, in sane ranges. Raises ConfigError."""
    if not isinstance(cfg, dict):
        raise ConfigError("The backup has no machine settings.")
    try:
        for ax in ("x", "y"):
            _num(cfg["steps_per_mm"][ax], 5, 200, f"Steps/mm {ax.upper()}")
            _num(cfg["travel_mm"][ax], 50, 2000, f"Travel {ax.upper()}")
            if cfg["axis"][ax].get("sign") not in (1, -1) or cfg["axis"][ax].get("ebb_axis") not in (1, 2):
                raise ConfigError(f"Axis {ax.upper()} mapping is not valid.")
        _num(cfg["servo"]["pen_up"], 5000, 30000, "Servo pen-up")
        _num(cfg["servo"]["pen_down"], 5000, 30000, "Servo pen-down")
    except (KeyError, TypeError, AttributeError):
        raise ConfigError("The machine settings in the backup are incomplete (steps_per_mm, travel_mm, axis, servo).")


def restore_bundle(settings, projects_dir, b, parts):
    """Write the chosen parts of a backup (machine, pens, pen_map, projects); each old file is kept as a backup.
    -> what was restored."""
    if not isinstance(b, dict) or b.get("kind") != BUNDLE_KIND:
        raise ConfigError("This is not a settings backup from this plotter.")
    done = []
    if "machine" in parts:
        check_machine(b.get("machine"))
        _write(settings, settings.machine_path, b["machine"])
        done.append("machine")
    if "pens" in parts:
        pens = b.get("pens")
        if not isinstance(pens, dict) or not all(isinstance(k, str) and PEN_NAME.match(k) and isinstance(v, dict)
                                                 for k, v in pens.items()):
            raise ConfigError("The pens in the backup are not valid.")
        _write(settings, settings.pens_path, pens)
        done.append("pens")
    if "pen_map" in parts and isinstance(b.get("pen_map"), dict):
        _write(settings, settings.cache / "pen_map.json", b["pen_map"])
        done.append("pen memory")
    if "projects" in parts and isinstance(b.get("projects"), dict):
        n = 0
        for slug, d in b["projects"].items():
            if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", str(slug)) and isinstance(d, dict) and isinstance(d.get("objects"), list):
                (projects_dir / f"{slug}.json").write_text(json.dumps(d, indent=1))
                n += 1
        done.append(f"{n} project{'s' if n != 1 else ''}")
    return done


def import_pens(settings, pens, replace=False):
    """Pen profiles from an exported file: merged in (same names replaced), or replacing all. -> how many."""
    if not isinstance(pens, dict) or not pens or len(pens) > 100:
        raise ConfigError("That is not a pen library (a JSON object of pen profiles).")
    for k, v in pens.items():
        if not isinstance(k, str) or not PEN_NAME.match(k) or not isinstance(v, dict):
            raise ConfigError(f"{str(k)[:30]!r} is not a valid pen profile.")
        for f, lo, hi in (("width_mm", 0.05, 10), ("speed", LIMITS["speed"]["min"], LIMITS["speed"]["max"]),
                          ("pressure", 0, LIMITS["pressure"]["max"]), ("hatch_mm", 0.2, 10), ("dwell_ms", 0, 3000)):
            if v.get(f) is not None:
                _num(v[f], lo, hi, f"{k}: {f}")
    new = dict(pens) if replace else {**settings.pens(), **pens}
    _write(settings, settings.pens_path, new)
    return len(pens)


def delete_pen(settings, name):
    pens = settings.pens()
    if name not in pens:
        raise ConfigError(f"There is no pen {name!r}.")
    del pens[name]
    _write(settings, settings.pens_path, pens)
    return pens
