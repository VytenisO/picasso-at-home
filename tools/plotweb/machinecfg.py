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
PEN_FIELDS = ("description", "colour", "colour_hex", "width_mm", "speed", "pressure", "expected_use", "notes")


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


def delete_pen(settings, name):
    pens = settings.pens()
    if name not in pens:
        raise ConfigError(f"There is no pen {name!r}.")
    del pens[name]
    _write(settings, settings.pens_path, pens)
    return pens
