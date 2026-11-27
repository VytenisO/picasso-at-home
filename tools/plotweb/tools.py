"""Tools the UI can run besides plotting a drawing: test sheets, recording their results, the pen test.

Every tool is scoped to ONE pen type: the pen is a required parameter, sheets are tagged with it, and results
are written under that pen only (the scripts enforce this too). To add a tool, add a Tool(...) to REGISTRY:

  kind "sheet"   argv builder writes an SVG; the server adds it to the library tagged with the pen
  kind "record"  argv builder records results into pens.json; output is shown to the operator
  kind "machine" a job that moves hardware; needs the operator's confirmation, runs through the job runner

Schema entries are the same as for converters, plus type "pen" (a profile name from pens.json).
"""
import re
import subprocess

from .converters import clamp_params


class Tool:
    def __init__(self, tid, title, kind, params, argv, help="", confirm=None, record_for=None):
        self.id, self.title, self.kind, self.params, self.argv = tid, title, kind, params, argv
        self.help, self.confirm, self.record_for = help, confirm, record_for

    def public(self):
        return {"id": self.id, "title": self.title, "kind": self.kind, "params": self.params, "help": self.help,
                "confirm": self.confirm, "record_for": self.record_for}


PEN = {"key": "pen", "label": "Pen type", "type": "pen", "required": True,
       "help": "Results are valid for this pen type only."}


def _script(s, name):
    return [s.python, str(s.root / "tools" / name)]


def _sheet_common(s, p):
    return ["--pen", p["pen"], "--pens-file", str(s.pens_path), "--out-dir", str(s.cache / "sheets")]


def _opt(argv, flag, v, fmt="{:g}"):
    if v is not None and v != "":
        argv += [flag, fmt.format(v) if not isinstance(v, str) else v]
    return argv


def _num(key, label, unit, lo, hi, step, integer=False):
    return {"key": key, "label": label, "type": "number", "default": None, "min": lo, "max": hi, "step": step,
            "unit": unit, "optional": True, "integer": integer}


REGISTRY = {t.id: t for t in [
    Tool("stroke_sheet", "Short-stroke and circle sheet", "sheet", [
        PEN,
        {"key": "sheet", "label": "Part", "type": "select", "default": "both",
         "options": [["both", "Lines and circles"], ["lines", "Lines only"], ["circles", "Circles only"]]},
    ], lambda s, p: _script(s, "stroke_test.py") + _sheet_common(s, p) + ["--sheet", p["sheet"]],
        help="Shortest clean line and smallest closed circle for one pen. Plot it at the speed you will use."),
    Tool("fill_sheet", "Infill sheet", "sheet", [PEN],
         lambda s, p: _script(s, "fill_test.py") + _sheet_common(s, p),
         help="Fill styles by line spacing: where lines merge, where gaps stay."),
    Tool("pressure_sheet", "Pressure sheet", "sheet", [
        PEN,
        {"key": "levels", "label": "Levels", "type": "text", "default": "0,0.15,0.3,0.45,0.6",
         "help": "Fractions from pen-down toward pen-up, 0 to 0.8. Larger = lighter. Never deeper than pen-down."},
    ], lambda s, p: _script(s, "fill_test.py") + _sheet_common(s, p) + ["--pressure", "--config",
                                                                       str(s.machine_path)]
        + (["--levels", p["levels"]] if p.get("levels") else []),
        help="Each block at its own lighter touch, in one run."),
    Tool("grid_sheet", "Speed x pressure grid", "sheet", [
        PEN,
        {"key": "speeds", "label": "Speeds (mm/s)", "type": "text", "default": "10,20,30,40",
         "help": "Columns, comma separated, up to 8, at most 40."},
        {"key": "pressures", "label": "Pressures", "type": "text", "default": "0,0.15,0.3,0.45",
         "help": "Rows: fractions from pen-down toward pen-up, 0 to 0.8. Larger = lighter."},
    ], lambda s, p: _script(s, "grid_test.py") + _sheet_common(s, p)
        + ["--speeds", p.get("speeds") or "10,20,30,40", "--pressures", p.get("pressures") or "0,0.15,0.3,0.45"],
        help="One cell per speed and pressure (a patch, a line, a circle), each at its own settings, in one run."),
    Tool("tone_sheet", "Tone ramp", "sheet", [PEN],
         lambda s, p: _script(s, "grid_test.py") + _sheet_common(s, p) + ["--tone"],
         help="Patches hatched from 0.3 to 3 mm apart, labelled: which spacing reads as which grey with this pen."),
    Tool("square_sheet", "Squareness sheet", "sheet", [
        PEN, {"key": "size", "label": "Square size", "type": "number", "default": 150, "min": 20, "max": 200, "step": 1,
              "unit": "mm"},
    ], lambda s, p: _script(s, "grid_test.py") + _sheet_common(s, p) + ["--square", f"{p.get('size') or 150:g}"],
        help="A square with its diagonals: equal diagonals mean the axes are square; the sides check each axis's "
             "scale against the calibration."),
    Tool("backlash_sheet", "Backlash sheet", "sheet", [PEN],
         lambda s, p: _script(s, "grid_test.py") + _sheet_common(s, p) + ["--backlash"],
         help="Short lines drawn towards each other from both sides of a tick: a gap or overlap where they meet is the "
              "backlash of that axis. Plot it with Shorter pen-up travel off (Job, Path planning)."),
    Tool("lost_sheet", "Lost-steps sheet", "sheet", [
        PEN, {"key": "minutes", "label": "Run time at stress speed", "type": "number", "default": 20, "min": 2,
              "max": 120, "step": 1, "unit": "min"},
    ], lambda s, p: _script(s, "lost_steps_test.py") + ["--out-dir", str(s.cache / "sheets"), "--config",
                                                        str(s.machine_path), "--minutes", f"{p.get('minutes') or 20:g}"],
        help="Long, jittery, fast moves that return to the same checkpoint mark: if later marks land away from the "
             "first, steps were lost. Then Record the drift with Lost-steps result."),
    Tool("lost_record", "Lost-steps result", "record", [
        {"key": "dx", "label": "Last mark right of the first (negative: left)", "type": "number", "default": 0,
         "min": -50, "max": 50, "step": 0.1, "unit": "mm"},
        {"key": "dy", "label": "Last mark toward you (negative: away)", "type": "number", "default": 0,
         "min": -50, "max": 50, "step": 0.1, "unit": "mm"},
    ], lambda s, p: _script(s, "lost_steps_test.py") + ["--config", str(s.machine_path), "--out-dir",
                                                        str(s.cache / "sheets"), "--analyse", f"{p['dx']:g}", f"{p['dy']:g}"],
        help="Turns the measured drift of the last checkpoint into steps lost per metre."),
    Tool("stroke_record", "Stroke results", "record", [
        PEN,
        _num("single", "Shortest single line", "mm", 0.1, 20, 0.05),
        _num("double", "Shortest forward+back line", "mm", 0.1, 20, 0.05),
        _num("circle_single", "Smallest circle, one loop", "mm", 0.1, 20, 0.05),
        _num("circle_overlap", "Smallest circle, 30 deg overlap", "mm", 0.1, 20, 0.05),
        _num("circle_double", "Smallest circle, two loops", "mm", 0.1, 20, 0.05),
    ], lambda s, p: _opt(_opt(_opt(_opt(_opt(
        _script(s, "stroke_test.py") + ["--pen", p["pen"], "--pens-file", str(s.pens_path)],
        "--record-single", p.get("single")), "--record-double", p.get("double")),
        "--record-circle-single", p.get("circle_single")), "--record-circle-overlap", p.get("circle_overlap")),
        "--record-circle-double", p.get("circle_double")),
        record_for="stroke_sheet", help="What came out clearly on the stroke sheet, for this pen only."),
    Tool("fill_record", "Fill and pressure results", "record", [
        PEN,
        _num("min_spacing", "Tightest clean spacing", "mm", 0.1, 5, 0.05),
        {"key": "best_style", "label": "Best fill style", "type": "select", "default": "", "optional": True,
         "options": [["", "(not recorded)"], ["hatch lifted", "hatch lifted"], ["zigzag", "zigzag"],
                     ["cross-hatch", "cross-hatch"], ["zigzag 45 deg", "zigzag 45 deg"],
                     ["concentric rings", "concentric rings"], ["zigzag + outline", "zigzag + outline"]]},
        _num("lightest", "Lightest marking pen-down", "servo", 7500, 22000, 10, True),
        _num("soft_fill", "Best soft-fill pen-down", "servo", 7500, 22000, 10, True),
    ], lambda s, p: _opt(_opt(_opt(_opt(
        _script(s, "fill_test.py") + ["--pen", p["pen"], "--pens-file", str(s.pens_path)],
        "--record-min-spacing", p.get("min_spacing")), "--record-best-style", p.get("best_style") or None),
        "--record-lightest-marking", p.get("lightest"), "{:d}"), "--record-soft-fill", p.get("soft_fill"), "{:d}"),
        record_for="fill_sheet", help="What held up on the fill and pressure sheets, for this pen only."),
    Tool("pen_test", "Pen height test", "machine", [],
         lambda s, p: _script(s, "pen_test.py") + ["--config", str(s.machine_path)],
         confirm="Only the pen servo moves (the carriage does not). The pen may touch the paper. Keep fingers "
                 "clear of the pen holder.",
         help="Toggle the pen up and down or sweep its range while you set the holder height by hand."),
]}


def clamp_tool_params(tool, raw, pens):
    """Validate tool parameters. Pen is mandatory and must be a known profile. Raises ValueError."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    plain = [p for p in tool.params if p["type"] != "pen" and not p.get("optional")]
    out.update(clamp_params(plain, raw))
    for p in tool.params:
        if p["type"] == "pen":
            pen = raw.get(p["key"])
            if not pen:
                raise ValueError("Choose the pen type this is for. Results are valid for that pen only.")
            if pen not in pens:
                raise ValueError(f"Unknown pen {pen!r}. Add a profile to config/pens.json first.")
            out[p["key"]] = pen
        elif p.get("optional"):
            v = raw.get(p["key"])
            if v in (None, ""):
                out[p["key"]] = None
                continue
            if p["type"] == "number":
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{p['label']} must be a number")
                if not (p["min"] <= v <= p["max"]):
                    raise ValueError(f"{p['label']} must be between {p['min']} and {p['max']} {p.get('unit', '')}")
                if p.get("integer"):
                    v = int(round(v))
            elif p["type"] == "select" and v not in [o[0] for o in p["options"]]:
                raise ValueError(f"{p['label']}: unknown choice")
            out[p["key"]] = v
    if tool.id == "pressure_sheet" and out.get("levels"):
        if not re.fullmatch(r"\s*\d*\.?\d+(\s*,\s*\d*\.?\d+){0,11}\s*", out["levels"]):
            raise ValueError("Levels must be a comma-separated list of numbers, e.g. 0,0.2,0.4")
        if any(not 0 <= float(x) <= 0.8 for x in out["levels"].split(",")):
            raise ValueError("Levels must be between 0 and 0.8 (0 = the configured pen-down)")
    if tool.kind == "record" and all(v is None for k, v in out.items() if k != "pen"):
        raise ValueError("Fill in at least one result to record.")
    return out


def run_sync(argv, timeout=60):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def written_svg(output):
    m = re.search(r"^Wrote (.+?\.svg)\b", output, re.M)
    return m.group(1).strip() if m else None
