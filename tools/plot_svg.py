#!/usr/bin/env python3
"""Plot an SVG on the WaterColorBot (EBB firmware 2.0.1 safe: SM/SP/SC/EM only).

  plot_svg.py FILE.svg                    dry run: parse, report, no hardware (default)
  plot_svg.py FILE.svg --mode trace       pen-UP trace of the bounding box on the real machine
  plot_svg.py FILE.svg --mode plot        the real plot (asks you to type PLOT)
  add --sim to run any mode on a virtual machine and virtual clock

Setup before trace/plot: motors off, slide the carriage by hand to the start point S
(pen up, ON the paper), tell the script where S is on the machine with --start-x/--start-y
(mm from the machine's top-left corner; default: the paper's top-left corner). S is the
chosen anchor on the PAPER (top-left, top-centre or centre, see --anchor); the drawing is
placed on the inset canvas automatically, margin_mm clear of every paper edge. The paper
rectangle (paper.x0/y0/width/height in config/machine.json, or --paper-*) is checked in
dry runs: S must be on the paper and the whole drawing inside the canvas. X is right, Y
is toward you. Calibration comes from ../config/machine.json.

Ctrl-C is the software e-stop: it raises the pen and releases the motors. Firmware 2.0.1
cannot stop a move already sent, so cutting power is the only instant stop.

Web UI flags (opt-in; without them the behaviour above is unchanged):
  --control           read commands from stdin, one per line: CONTINUE (resume a pause),
                      PAUSE (at the end of the current stroke: pen up, motors stay on),
                      STOP (same cleanup as Ctrl-C, before the next command is sent), RETURN
                      (at the end of the current stroke: travel back to S, then release).
                      End of input counts as STOP. With --sim the pen-swap pause waits too.
  --sim-speed F       with --sim, also sleep in real time at F x speed (watchable runs)
  --preview-json FILE dry run: write the plotted strokes in machine mm and the summary
  --layers FILE       per ink layer (JSON list, in layer order): enabled, speed, pressure, pen
Supported SVG: path (M L H V C S Q T Z, no arcs), line, polyline, polygon, rect, circle,
ellipse, and translate/scale/rotate/skew/matrix transforms. Text is outlined via
tools/svg_vector.py at upload time (or here when fonttools is available). Other unknown
elements are an error unless --ignore-unsupported.
"""
import argparse
import datetime
import json
import math
import pathlib
import queue
import re
import signal
import sys
import threading
import time
import xml.etree.ElementTree as ET

import fill as fillmod
import pathplan

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
EBB_VID, EBB_PID = 0x04D8, 0xFD92
UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6, "px": 25.4 / 96, "": 25.4 / 96}
INKNS = "{http://www.inkscape.org/namespaces/inkscape}"
INK_NAMES = {"#000000": "Black", "#111111": "Black", "#2463a6": "Blue", "#c0392b": "Red",
             "#c2187e": "Magenta", "#00a0b0": "Cyan", "#d9a400": "Yellow"}
CSS_COLORS = {"black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000",
              "blue": "#0000ff", "yellow": "#ffff00", "cyan": "#00ffff", "magenta": "#ff00ff",
              "grey": "#808080", "gray": "#808080", "orange": "#ffa500", "purple": "#800080",
              "pink": "#ffc0cb", "lime": "#00ff00", "navy": "#000080", "teal": "#008080",
              "brown": "#a52a2a"}
MAX_SPEED = 40.0        # mm/s hard ceiling for drawing (pen down)
MAX_PRESSURE = 0.9      # data-pressure ceiling: a fraction of the way from pen-down toward pen-up
MAX_TRAVEL_SPEED = 200.0  # mm/s ceiling for pen-up travel; 100 and 200 ran clean on pen-up traces
CHUNK_STEPS = 3000      # longest single SM leg (per vector) in steps
CHUNK_SECONDS = 3.0     # and in time: no single SM runs longer, so an in-flight move at abort is bounded
LEAD = 0.05             # s: feed the next SM this long before the current one ends
EDGE_MARGIN = 5.0       # mm kept clear of the machine travel limit
OUT = ROOT / "results"
LOGF = None


def log(msg, echo=True):
    if echo:
        print(msg, flush=True)
    if LOGF:
        LOGF.write(f"{datetime.datetime.now():%H:%M:%S} {msg}\n")
        LOGF.flush()


# --------------------------------------------------------------- ink / layers
def normalize_color(s):
    """Accept common SVG colour syntaxes; -> '#rrggbb' or None."""
    if not s:
        return None
    s = s.strip().lower()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return "#" + h if re.fullmatch(r"[0-9a-f]{6}", h) else None
    m = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", s)
    if m:
        return "#%02x%02x%02x" % tuple(min(255, int(v)) for v in m.groups())
    return CSS_COLORS.get(s)


def style_prop(el, prop):
    """Presentation attribute or inline style value of one element, or None."""
    v = el.get(prop)
    if not v:
        m = re.search(r"(?:^|;)\s*" + prop + r"\s*:\s*([^;]+)", el.get("style") or "")
        v = m.group(1) if m else None
    return v.strip() if v else None


def _frac(v, default=1.0):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def element_color(el):
    style = el.get("style") or ""

    def find(prop):
        c = el.get(prop)
        if not c:
            m = re.search(r"(?:^|;)\s*" + prop + r"\s*:\s*([^;]+)", style)
            c = m.group(1) if m else None
        return c

    # The pen is the stroke colour, else the fill colour. An element that sets neither inherits its group's
    # (None here); the walk falls back to black at the top.
    for prop in ("stroke", "fill"):
        c = find(prop)
        if c and c.strip().lower() not in ("none", "inherit"):
            return normalize_color(c) or "#000000"
    return None


def element_layer(el):
    if el.get(INKNS + "groupmode") == "layer":
        return el.get(INKNS + "label") or el.get("id") or None
    return None


def color_label(key):
    color, layer = key
    return layer or INK_NAMES.get(color) or color or "Ink"


def layer_key(key):
    """Stable text id of an ink layer: 'colour|inkscape layer'."""
    return f"{key[0] or ''}|{key[1] or ''}"


def color_groups(metas):
    """Group stroke indices by ink, in order of first appearance."""
    groups, by_key = [], {}
    for i, m in enumerate(metas):
        key = (m.get("color"), m.get("layer"))
        if key not in by_key:
            by_key[key] = len(groups)
            groups.append((key, []))
        groups[by_key[key]][1].append(i)
    return groups


class Control:
    """--control: commands on stdin, one per line. A reader thread sets flags; the plot loop polls them.
    STOP is honoured before the next SM/SP is sent; PAUSE and RETURN at the end of the current stroke.
    End of input (the web server went away) counts as STOP."""

    def __init__(self, stream=None):
        self.lines = queue.Queue()
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.ret = threading.Event()
        self.paused_s = 0.0          # real seconds spent waiting, kept out of the ETA
        self.speed_factor = 1.0      # SPEED f from the operator: drawing speed times f (0.25..1.5) from the next move
        self.stream = stream or sys.stdin
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for raw in self.stream:
                word = raw.strip().upper()
                if word == "STOP":
                    self.stop.set()
                elif word == "PAUSE":
                    self.pause.set()
                elif word == "RETURN":
                    self.ret.set()
                elif word.startswith("SPEED "):
                    try:
                        self.speed_factor = min(1.5, max(0.25, float(word.split()[1])))
                    except (ValueError, IndexError):
                        pass
                    continue
                self.lines.put(word)
        finally:
            self.stop.set()
            self.lines.put(None)

    def check_stop(self):
        if self.stop.is_set():
            raise KeyboardInterrupt()

    def readline(self, prompt):
        print(prompt, flush=True)
        word = self.lines.get()
        if word is None:
            raise EOFError
        return word

    def drain(self):
        """Drop commands left over from before a pause (flags such as STOP stay set). Call before announcing it."""
        while True:
            try:
                if self.lines.get_nowait() is None:
                    self.lines.put(None)
                    return
            except queue.Empty:
                return

    def wait_continue(self):
        """Block until CONTINUE (-> True) or RETURN (-> False); STOP or end of input raises KeyboardInterrupt.
        Call drain() before announcing the pause."""
        t0 = time.monotonic()
        try:
            while True:
                self.check_stop()
                if self.ret.is_set():
                    return False
                try:
                    word = self.lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if word == "CONTINUE":
                    self.pause.clear()
                    return True
        finally:
            self.paused_s += time.monotonic() - t0


def pause_for_swap(key, a, ctl=None, extra=None):
    """Tell the operator (and the web UI) to change pens, then wait for CONTINUE.
    -> False if the operator asked to return to S instead."""
    info = {"kind": "swap", "color": key[0], "layer": key[1], "label": color_label(key)}
    info.update(extra or {})
    log("")
    pen = f" (pen: {info['pen']})" if info.get("pen") else ""
    log(f"*** PEN CHANGE: swap to {color_label(key)}{pen} ***")
    if ctl:
        ctl.drain()
    print("__PAUSE__:" + json.dumps(info), flush=True)
    if EMIT is not None:
        EMIT.append(("PAUSE", info))
    if ctl:
        log("Waiting for CONTINUE (pen up, motors on: do not push the carriage).")
        go = ctl.wait_continue()
        if go:
            print("__RESUMED__:{}", flush=True)
        return go
    if a.sim:
        return True
    try:
        input("Pen swapped? Type CONTINUE to resume: ")
    except EOFError:
        raise SystemExit("input closed while waiting for a pen swap")
    return True


def extend_closed(s, mm):
    """A closed stroke drawn on past its start by mm (along its own first segments): closes the seam."""
    out, left = list(s), mm
    for a, b in zip(s, s[1:]):
        d = math.dist(a, b)
        if d >= left:
            f = left / d if d else 0
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
            return out
        out.append(b)
        left -= d
    return out


def dash_split(s, on, off):
    """A stroke as dashes: on mm drawn, off mm skipped, from its start."""
    out, cur, pos, drawing = [], [s[0]], 0.0, True
    left = on
    for a, b in zip(s, s[1:]):
        d = math.dist(a, b)
        t = 0.0
        while d - t > 1e-9:
            step = min(left, d - t)
            t += step
            p = (a[0] + (b[0] - a[0]) * t / d, a[1] + (b[1] - a[1]) * t / d)
            left -= step
            if drawing:
                cur.append(p)
            if left <= 1e-9:
                if drawing:
                    if len(cur) >= 2:
                        out.append(cur)
                    cur = []
                else:
                    cur = [p]
                drawing = not drawing
                left = on if drawing else off
    if drawing and len(cur) >= 2:
        out.append(cur)
    return out


def inset_line(s, d):
    """An open fill line shortened by d mm at both ends (None if nothing is left)."""
    if d <= 0 or len(s) < 2:
        return s
    total = sum(math.dist(a, b) for a, b in zip(s, s[1:]))
    if total <= 2 * d + 0.05:
        return None

    def cut_front(pts, dist):
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            seg = math.dist(a, b)
            if seg >= dist:
                f = dist / seg if seg else 0
                return [(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)] + pts[i + 1:]
            dist -= seg
        return pts[-1:]
    return cut_front(cut_front(list(s), d)[::-1], d)[::-1]


def point_in(pt, poly):
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def depth_tiers(strokes, idxs, closed_tol=0.1):
    """idxs grouped deepest first by how many closed strokes of the same block enclose each one."""
    if len(idxs) > 2000:
        return [idxs]
    closed = [i for i in idxs if len(strokes[i]) > 3 and math.dist(strokes[i][0], strokes[i][-1]) <= closed_tol]
    boxes = {i: bbox([strokes[i]]) for i in closed}
    depth = {}
    for i in idxs:
        p = strokes[i][0]
        n = 0
        for j in closed:
            if j == i:
                continue
            b = boxes[j]
            if b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3] and point_in(p, strokes[j][:-1]):
                n += 1
        depth[i] = n
    tiers = {}
    for i in idxs:
        tiers.setdefault(depth[i], []).append(i)
    return [tiers[k] for k in sorted(tiers, reverse=True)]


def convex_hull(points):
    """Monotone chain: the convex outline of points, closed."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts + pts[:1]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    ring = lo[:-1] + hi[:-1]
    return ring + ring[:1]


def same_pen(la, lb):
    """Two layers set to the same pen plot one after the other with no pen change."""
    return bool(la.get("pen")) and la.get("pen") == lb.get("pen")


def pause_for_paper(copy, copies, a, ctl=None):
    """Between copies: the carriage is at S, pen up, motors on. Wait for new paper and CONTINUE.
    -> False if the operator asked to return (it is already at S) instead."""
    log("")
    log(f"*** PAPER CHANGE: copy {copy - 1} of {copies} done; put in fresh paper for copy {copy} ***")
    if ctl:
        ctl.drain()
    print("__PAUSE__:" + json.dumps({"kind": "paper", "copy": copy, "copies": copies}), flush=True)
    if EMIT is not None:
        EMIT.append(("PAUSE", {"kind": "paper", "copy": copy, "copies": copies}))
    if ctl:
        go = ctl.wait_continue()
        if go:
            print("__RESUMED__:{}", flush=True)
        return go
    if a.sim:
        return True
    try:
        input("New paper in? Type CONTINUE to plot the next copy: ")
    except EOFError:
        raise SystemExit("input closed while waiting for a paper change")
    return True


# --------------------------------------------------------------- SVG transforms
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m, n):
    """Compose two 2x3 affine matrices (apply n first, then m)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D,
            a * E + c * F + e, b * E + d * F + f)


def mat_apply(m, p):
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def parse_transform(s):
    m = IDENTITY
    if not s or s.strip() in ("", "none"):
        return m
    for name, raw in re.findall(r"([A-Za-z]+)\s*\(([^)]*)\)", s):
        v = [float(x) for x in re.findall(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?", raw)]
        name = name.lower()
        if name == "matrix" and len(v) >= 6:
            t = tuple(v[:6])
        elif name == "translate":
            t = (1.0, 0.0, 0.0, 1.0, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale":
            sy = v[1] if len(v) > 1 else v[0]
            t = (v[0], 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate":
            ang = math.radians(v[0])
            co, si = math.cos(ang), math.sin(ang)
            rot = (co, si, -si, co, 0.0, 0.0)
            if len(v) >= 3:
                cx, cy = v[1], v[2]
                t = mat_mul(mat_mul((1, 0, 0, 1, cx, cy), rot), (1, 0, 0, 1, -cx, -cy))
            else:
                t = rot
        elif name in ("skewx", "skewy"):
            q = math.tan(math.radians(v[0]))
            t = (1.0, 0.0, q, 1.0, 0.0, 0.0) if name == "skewx" else (1.0, q, 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        m = mat_mul(m, t)
    return m


# ---------------------------------------------------------------- SVG parsing
def parse_len(s, default_px=None):
    if s is None:
        return default_px
    m = re.fullmatch(r"\s*(-?[\d.]+(?:[eE][-+]?\d+)?)\s*([a-z%]*)\s*", s)
    if not m or m.group(2) == "%" or m.group(2) not in UNIT_MM:
        raise ValueError(f"unsupported length {s!r}")
    return float(m.group(1)) * UNIT_MM[m.group(2)]


TOK = re.compile(r"([MmLlHhVvCcSsQqTtZzAa])|(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
ARGN = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "Z": 0, "A": 7}


def bez(p0, p1, p2, p3, n=16):
    out = []
    for i in range(1, n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def parse_path(d):
    """-> list of polylines [(x,y),...] in user units."""
    toks = [(m.group(1), m.group(2)) for m in TOK.finditer(d)]
    polys, cur, start = [], None, None
    i, cmd = 0, None
    x = y = 0.0
    last_c2 = last_q = None
    while i < len(toks):
        if toks[i][0]:
            cmd = toks[i][0]
            i += 1
            if cmd in "Zz":
                if cur and start:
                    cur.append(start)
                    x, y = start
                last_c2 = last_q = None
                continue
        if cmd is None:
            raise ValueError("path data does not start with a command")
        up = cmd.upper()
        n = ARGN[up]
        if up == "A":
            raise ValueError("arc commands are not supported")
        args = []
        for _ in range(n):
            if i >= len(toks) or toks[i][1] is None:
                raise ValueError(f"bad argument list for {cmd}")
            args.append(float(toks[i][1]))
            i += 1
        rel = cmd.islower()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if up == "M":
            x, y = args[0] + ox, args[1] + oy
            cur = [(x, y)]
            polys.append(cur)
            start = (x, y)
            cmd = "l" if rel else "L"      # implicit lineto after the first pair
            last_c2 = last_q = None
            continue
        if cur is None:
            raise ValueError("drawing command before any moveto")
        if up == "L":
            x, y = args[0] + ox, args[1] + oy
            cur.append((x, y))
            last_c2 = last_q = None
        elif up == "H":
            x = args[0] + (x if rel else 0.0)
            cur.append((x, y))
            last_c2 = last_q = None
        elif up == "V":
            y = args[0] + (y if rel else 0.0)
            cur.append((x, y))
            last_c2 = last_q = None
        elif up in "CS":
            if up == "C":
                c1 = (args[0] + ox, args[1] + oy)
                c2 = (args[2] + ox, args[3] + oy)
                e = (args[4] + ox, args[5] + oy)
            else:
                c1 = (2 * x - last_c2[0], 2 * y - last_c2[1]) if last_c2 else (x, y)
                c2 = (args[0] + ox, args[1] + oy)
                e = (args[2] + ox, args[3] + oy)
            cur.extend(bez((x, y), c1, c2, e))
            x, y = e
            last_c2, last_q = c2, None
        elif up in "QT":
            if up == "Q":
                q = (args[0] + ox, args[1] + oy)
                e = (args[2] + ox, args[3] + oy)
            else:
                q = (2 * x - last_q[0], 2 * y - last_q[1]) if last_q else (x, y)
                e = (args[0] + ox, args[1] + oy)
            c1 = (x + 2 / 3 * (q[0] - x), y + 2 / 3 * (q[1] - y))
            c2 = (e[0] + 2 / 3 * (q[0] - e[0]), e[1] + 2 / 3 * (q[1] - e[1]))
            cur.extend(bez((x, y), c1, c2, e))
            x, y = e
            last_q, last_c2 = q, None
    return [p for p in polys if len(p) >= 2]


def load_svg(path, ignore_unsupported):
    root = ET.parse(path).getroot()
    if not root.tag.endswith("svg"):
        raise ValueError("not an SVG")
    vb = root.get("viewBox")
    w_mm = parse_len(root.get("width"))
    h_mm = parse_len(root.get("height"))
    if vb:
        vx, vy, vw, vh = (float(v) for v in re.split(r"[\s,]+", vb.strip()))
    else:
        if w_mm is None or h_mm is None:
            raise ValueError("SVG has neither viewBox nor width/height")
        vx = vy = 0.0
        vw, vh = w_mm / UNIT_MM["px"], h_mm / UNIT_MM["px"]
    if w_mm is None:
        w_mm = vw * UNIT_MM["px"]
    if h_mm is None:
        h_mm = vh * UNIT_MM["px"]
    sx, sy = w_mm / vw, h_mm / vh
    if abs(sx - sy) / max(sx, sy) > 0.01:
        log(f"WARNING: width/height ratio differs from viewBox by >1% ({sx:.4f} vs {sy:.4f} mm/unit); using min")
    scale = min(sx, sy)

    polys, metas, skipped = [], [], {}
    fills = []       # filled shapes: rings (document units), colour, darkness, rule, ink meta

    def walk(el, ctx, parent_xform):
        tag = el.tag.split("}")[-1]
        if tag in ("defs", "metadata", "title", "desc", "style", "namedview"):
            return
        if (el.get("display") or "").strip() == "none" or re.search(r"(?:^|;)\s*display\s*:\s*none", el.get("style") or ""):
            return
        xform = mat_mul(parent_xform, parse_transform(el.get("transform")))
        cur = dict(ctx)
        c = element_color(el)
        layer = element_layer(el)
        if c:
            cur["color"] = c
        if layer:
            cur["layer"] = layer
        if el.get("data-pressure") is not None:
            cur["pressure"] = float(el.get("data-pressure"))
        if el.get("data-speed") is not None:
            try:
                cur["speed"] = min(MAX_SPEED, max(1.0, float(el.get("data-speed"))))   # a test cell's own speed
            except ValueError:
                pass
        if el.get("data-seam") is not None:
            try:
                sx_, sy_ = (float(v) for v in el.get("data-seam").split())
                cur["seam"] = (sx_, sy_)        # machine mm: the composed bed document's own units
            except ValueError:
                pass
        if el.get("data-obj") is not None and el.get("data-obj").isdigit():
            cur["obj"] = int(el.get("data-obj"))          # which bed object (the web UI's composed document)
        meta = {"color": cur.get("color") or "#000000", "layer": cur.get("layer"), "pressure": cur.get("pressure"),
                "obj": cur.get("obj"), "speed": cur.get("speed"), "seam": cur.get("seam")}
        fv = style_prop(el, "fill")
        if fv is not None:
            cur["fill"] = None if fv.lower() in ("none", "transparent", "currentcolor") or fv.lower().startswith("url(") \
                else normalize_color(fv)
        fr = style_prop(el, "fill-rule")
        if fr:
            cur["rule"] = "nonzero" if fr.lower() == "nonzero" else "evenodd"
        cur["fop"] = cur.get("fop", 1.0) * _frac(style_prop(el, "fill-opacity")) * _frac(style_prop(el, "opacity"))
        el_rings = []

        def add(poly):
            if len(poly) >= 2:
                q = [mat_apply(xform, p) for p in poly]
                polys.append(q)
                metas.append(dict(meta))
                el_rings.append(q)

        f = lambda k, dflt=0.0: float(el.get(k, dflt))  # noqa: E731
        if tag == "path":
            for poly in parse_path(el.get("d", "")):
                add(poly)
        elif tag == "line":
            add([(f("x1"), f("y1")), (f("x2"), f("y2"))])
        elif tag in ("polyline", "polygon"):
            nums = [float(v) for v in re.findall(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?", el.get("points", ""))]
            pts = list(zip(nums[0::2], nums[1::2]))
            if tag == "polygon" and pts:
                pts.append(pts[0])
            add(pts)
        elif tag == "rect":
            x0, y0, w, h = f("x"), f("y"), f("width"), f("height")
            add([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)])
        elif tag == "circle":
            cx, cy, r = f("cx"), f("cy"), f("r")
            add([(cx + r * math.cos(2 * math.pi * k / 72), cy + r * math.sin(2 * math.pi * k / 72)) for k in range(73)])
        elif tag == "ellipse":
            cx, cy, rx, ry = f("cx"), f("cy"), f("rx"), f("ry")
            add([(cx + rx * math.cos(2 * math.pi * k / 72), cy + ry * math.sin(2 * math.pi * k / 72)) for k in range(73)])
        elif tag == "text":
            # Normally outlined at upload time; support it here too if fonttools is present.
            try:
                import svg_vector
                st = dict(svg_vector.style_of(el))
                txt = "".join(el.itertext())
                d = svg_vector.text_to_d(txt, svg_vector.length(el.get("x", "0"), 0.0),
                                         svg_vector.length(el.get("y", "0"), 0.0),
                                         svg_vector.length(st.get("font-size")),
                                         st.get("font-family", "DejaVu Sans"),
                                         st.get("font-weight", "normal"), st.get("font-style", "normal"),
                                         st.get("text-anchor", "start"),
                                         svg_vector.length(st.get("letter-spacing"), 0.0))
                if d:
                    for poly in parse_path(d):
                        add(poly)
                else:
                    skipped[tag] = skipped.get(tag, 0) + 1
            except Exception:  # noqa: BLE001
                skipped[tag] = skipped.get(tag, 0) + 1
        elif tag in ("g", "svg", "a", "tspan"):
            pass
        else:
            skipped[tag] = skipped.get(tag, 0) + 1
        if cur.get("fill") and el_rings and tag in ("path", "polygon", "polyline", "rect", "circle", "ellipse", "text"):
            fills.append({"rings": el_rings, "color": cur["fill"], "opacity": cur["fop"],
                          "rule": cur.get("rule", "nonzero"), "layer": cur.get("layer"), "pressure": cur.get("pressure"),
                          "obj": cur.get("obj")})
        for child in el:
            walk(child, cur, xform)

    walk(root, {"color": None, "layer": None, "pressure": None, "fill": None, "fop": 1.0}, IDENTITY)
    if skipped:
        msg = f"unsupported elements: {skipped}"
        if not ignore_unsupported:
            raise ValueError(msg + " (use --ignore-unsupported to skip them)")
        log("WARNING: " + msg + " -- skipped")
    mm = [[((px - vx) * scale, (py - vy) * scale) for px, py in p] for p in polys]
    for f in fills:
        f["rings"] = [[((px - vx) * scale, (py - vy) * scale) for px, py in r] for r in f["rings"]]
    return mm, metas, (vw * scale, vh * scale), fills


# ------------------------------------------------------------ path processing
def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (L * L)))
                d = math.hypot(px - (ax + u * dx), py - (ay + u * dy))
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def prepare(polys, metas, eps, join_tol, scale, dx, dy):
    """Scale/offset to mm, join touching strokes (same ink and pen pressure only), simplify."""
    sp = [[(x * scale + dx, y * scale + dy) for x, y in p] for p in polys]
    merged, mmeta = [], []
    # strokes join only within one ink, pen pressure, object, speed and filled shape
    key = lambda m: (m.get("color"), m.get("layer"), m.get("pressure"), m.get("obj"), m.get("speed"),  # noqa: E731
                     m.get("shape"))
    for p, m in zip(sp, metas):
        same_ink = merged and key(m) == key(mmeta[-1])
        if same_ink and math.dist(merged[-1][-1], p[0]) <= join_tol:
            merged[-1].extend(p[1:])
        else:
            merged.append(list(p))
            mmeta.append(dict(m))
    out, outm = [], []
    for p, m in zip(merged, mmeta):
        q = rdp(p, eps)
        if len(q) >= 2:
            out.append(q)
            outm.append(m)
    return out, outm


def bbox(strokes):
    xs = [x for s in strokes for x, _ in s]
    ys = [y for s in strokes for _, y in s]
    return min(xs), min(ys), max(xs), max(ys)


# ------------------------------------------------------------------ machine IO
# --emit: the compiled job, every EBB command with the driver's waits and pauses, for run_compiled.py
EMIT = None


def emit_wait(s):
    if EMIT is not None and s > 0:
        if EMIT and EMIT[-1][0] == "WAIT":
            EMIT[-1] = ("WAIT", EMIT[-1][1] + s)
        else:
            EMIT.append(("WAIT", s))


def write_emit(path, cfg, a):
    head = {"format": "picasso-ebb-1", "steps_per_mm": cfg["steps_per_mm"], "axis": cfg["axis"],
            "servo": cfg["servo"], "start_mm": [a.start_x, a.start_y], "svg": str(a.svg), "mode": a.mode,
            "made": time.strftime("%Y-%m-%d %H:%M:%S")}
    out = ["# compiled pen-plotter job: run with tools/run_compiled.py, carriage parked at S, pen up",
           "#HEAD " + json.dumps(head, separators=(",", ":"))]
    for kind, v in EMIT:
        out.append(v if kind == "CMD" else f"#WAIT {v:.4f}" if kind == "WAIT" else "#PAUSE " + json.dumps(v))
    pathlib.Path(path).write_text("\n".join(out) + "\n")
    log(f"Compiled job   : {sum(1 for k, _ in EMIT if k == 'CMD')} commands written to {path}")


class Plotter:
    def __init__(self, cfg, args):
        self.cfg, self.sim = cfg, args.sim
        self.spmm = (cfg["steps_per_mm"]["x"], cfg["steps_per_mm"]["y"])
        self.ax = (cfg["axis"]["x"], cfg["axis"]["y"])
        self.pos = [0, 0]            # world steps relative to S
        self.sent = [0, 0]           # sum of EBB-axis steps actually sent
        self.vt = 0.0                # virtual clock for --sim
        self.t_end = self.now()
        self.nsm = 0
        self.tracking = args.track
        self.origin = (args.start_x, args.start_y)
        self.last_track = 0.0
        self.pen_state = True
        self.ser = None
        self.caps = set()            # firmware 3.x: ES (instant stop), QS (step position readback)
        self.readback = False        # the end position was read back from the board, not just counted
        self.ctl = getattr(args, "ctl", None)
        self.sim_speed = getattr(args, "sim_speed", None) or 0.0
        if not self.sim:
            port = args.port
            if not port:
                port = next((p.device for p in list_ports.comports() if (p.vid, p.pid) == (EBB_VID, EBB_PID)), None)
            if not port:
                raise SystemExit("No EBB (04d8:fd92) found. Plug in/power the plotter.")
            self.ser = serial.Serial(port, 9600, timeout=3)
            log(f"Connected {port}")
            self.probe()

    def probe(self):
        """Which newer commands the firmware has. 2.0.1 answers ES/QS/QM with an error; 3.x has all three."""
        self.ser.reset_input_buffer()
        self.ser.write(b"V\r")
        ver = self.ser.readline().decode(errors="replace").strip()
        m = re.search(r"Version (\d+)\.(\d+)\.(\d+)", ver)
        if m and tuple(int(x) for x in m.groups()) >= (3, 0, 0):
            self.caps = {"ES", "QS"}
        log(f"Firmware: {ver or 'no answer'}" + ("; instant stop and position readback on" if self.caps else
                                                 "; no instant stop (stops after the move in flight)"))

    def now(self):
        return self.vt if self.sim else time.monotonic()

    def sleep(self, s, interruptible=True):
        """Wait s seconds (virtual in --sim). With an instant stop, a STOP cuts the wait short: the caller's
        next check_stop raises and shutdown() sends ES."""
        if s <= 0:
            return
        if self.sim:
            self.vt += s
            emit_wait(s)
            if self.sim_speed > 0:
                time.sleep(s / self.sim_speed)
        elif interruptible and self.ctl and "ES" in self.caps:
            self.ctl.stop.wait(s)
        else:
            time.sleep(s)

    def cmd(self, line, timeout=3.0):
        if EMIT is not None:
            EMIT.append(("CMD", line))
        if self.sim:
            return ["OK"]
        self.ser.timeout = timeout
        self.ser.write(line.encode() + b"\r")
        out = []
        while True:
            r = self.ser.readline().decode(errors="replace").strip()
            if not r:
                break
            out.append(r)
            if r == "OK" or r.startswith("!"):
                break
        if "OK" not in out:
            raise RuntimeError(f"{line!r} -> {out}")
        return out

    def idle(self):
        self.sleep(self.t_end - self.now() + 0.1)
        self.t_end = max(self.t_end, self.now())

    def track(self, force=False):
        """Emit the current machine position for the web UI's live tracker."""
        if not self.tracking:
            return
        now = self.now()
        if not force and now - self.last_track < 0.2:
            return
        self.last_track = now
        info = {"x": round(self.origin[0] + self.pos[0] / self.spmm[0], 2),
                "y": round(self.origin[1] + self.pos[1] / self.spmm[1], 2),
                "pen": "up" if self.pen_state else "down"}
        print("__POS__:" + json.dumps(info), flush=True)

    def setup(self):
        s = self.cfg["servo"]
        self.cmd("EM,1,1")
        if "QS" in self.caps:
            self.cmd("CS")              # step counters from here: QS then reads the position relative to S
        self.cmd(f"SC,4,{s['pen_up']}")
        self.cmd(f"SC,5,{s['pen_down']}")
        self.down = s["pen_down"]
        self.pen(True)

    def set_pressure(self, frac):
        """Set the next pen-down by a fraction from the configured pen-down (0, heaviest) toward pen-up (1).
        Only call with the pen up: SC,5 takes effect at the next SP,0."""
        s = self.cfg["servo"]
        val = round(s["pen_down"] + (frac or 0.0) * (s["pen_up"] - s["pen_down"]))
        if val != self.down:
            self.cmd(f"SC,5,{val}")
            self.down = val

    def pen(self, up, delay=250):
        self.idle()
        if self.ctl:
            self.ctl.check_stop()   # after the wait: no pen move is sent once STOP is in
        self.pen_state = up
        self.cmd(f"SP,{1 if up else 0},{delay}")
        self.sleep(delay / 1000 + 0.05)
        self.t_end = max(self.t_end, self.now())
        self.track(force=True)

    def shutdown(self, abort=None):
        """Raise the pen, wait for any move already sent to finish, then release the motors. -> True if clean.

        abort (default: a STOP came in): on firmware with ES, end the move in flight at once instead of letting it
        run out, then read the real position back with QS."""
        if abort is None:
            abort = bool(self.ctl and self.ctl.stop.is_set())
        if abort and "ES" in self.caps and not self.sim:
            try:
                self.cmd("ES")
                self.t_end = self.now()
                log("Instant stop (ES): the move in flight was cut short.")
            except Exception as ex:   # noqa: BLE001
                log(f"ES failed ({ex!r}); waiting for the move in flight instead")
        try:
            self.cmd("SP,1,300")
            self.sleep(max(0.0, self.t_end - self.now()) + 0.4, interruptible=False)
            if "QS" in self.caps and not self.sim:
                self.read_position()
            if getattr(self, "keep_motors", False):
                log("Motors left on: they hold the carriage where it is (and get warm). Switch them off from Machine.")
            else:
                self.cmd("EM,0,0")
            return True
        except Exception as ex:   # noqa: BLE001
            log(f"shutdown problem: {ex!r}")
            return False

    def read_position(self):
        """Set pos from the board's step counters (zeroed at S by setup). Best effort: a bad answer keeps the
        counted position."""
        try:
            out = self.cmd("QS")
            m = [int(v) for v in out[0].split(",")[:2]]
            world = [0, 0]
            for w in (0, 1):
                world[w] = m[self.ax[w]["ebb_axis"] - 1] * self.ax[w]["sign"]
            if world != self.pos:
                log(f"Position read back: {world} steps (counted {self.pos}).")
            self.pos = world
            self.readback = True
        except Exception as ex:   # noqa: BLE001
            log(f"Could not read the position back ({ex!r}); keeping the counted one.")

    def _sm(self, a1, a2, ms):
        wait = self.t_end - self.now() - LEAD
        if wait > 0:
            self.sleep(wait)
        if self.ctl:
            self.ctl.check_stop()   # nothing new is sent after STOP; the move in flight still runs out
        self.cmd(f"SM,{ms},{a1},{a2}", timeout=ms / 1000 + 3)
        self.t_end = max(self.t_end, self.now()) + ms / 1000
        self.sent[0] += a1
        self.sent[1] += a2
        self.nsm += 1

    def move_to(self, tx, ty, speed):
        """Absolute world-step target; chunked, vector speed in mm/s."""
        dx, dy = tx - self.pos[0], ty - self.pos[1]
        mm_total = math.hypot(dx / self.spmm[0], dy / self.spmm[1])
        n = max(1, math.ceil(math.hypot(dx, dy) / CHUNK_STEPS), math.ceil(mm_total / speed / CHUNK_SECONDS))
        for k in range(1, n + 1):
            nx = self.pos[0] + round((dx) * k / n) - round(dx * (k - 1) / n)
            ny = self.pos[1] + round((dy) * k / n) - round(dy * (k - 1) / n)
            sx, sy = nx - self.pos[0], ny - self.pos[1]
            if sx == 0 and sy == 0:
                continue
            mm = math.hypot(sx / self.spmm[0], sy / self.spmm[1])
            ms = max(4, math.ceil(mm / speed * 1000))
            a = [0, 0]
            for w, s in ((0, sx), (1, sy)):
                a[self.ax[w]["ebb_axis"] - 1] += s * self.ax[w]["sign"]
            self._sm(a[0], a[1], ms)
            self.pos[0] += sx
            self.pos[1] += sy
            self.track()


def to_steps(pt, spmm):
    return round(pt[0] * spmm[0]), round(pt[1] * spmm[1])


def estimate(strokes, spmm, draw_v, trav_v, pen_s, speeds=None, per_stroke=None):
    """-> drawing mm, travel mm, pen lifts, seconds. speeds: optional per-stroke drawing speed.
    per_stroke: optional list that receives each stroke's estimated seconds (travel to it + draw + lifts)."""
    t = 0.0
    dist_d = dist_t = 0.0
    cur = (0.0, 0.0)
    for i, s in enumerate(strokes):
        d = math.dist(cur, s[0])
        dist_t += d
        sd = sum(math.dist(a, b) for a, b in zip(s, s[1:]))
        dist_d += sd
        v = speeds[i] if speeds else draw_v
        st = sd / v + d / trav_v + 2 * pen_s
        t += st
        if per_stroke is not None:
            per_stroke.append(st)
        cur = s[-1]
    back = math.dist(cur, (0.0, 0.0))
    dist_t += back
    t += back / trav_v
    return dist_d, dist_t, len(strokes), t


def fmt(sec):
    return f"{int(sec // 60)} min {int(sec % 60):02d} s"


# ------------------------------------------------------------------------ main
def main():
    global LOGF
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("svg")
    ap.add_argument("--mode", choices=["dry", "trace", "plot"], default="dry")
    ap.add_argument("--sim", action="store_true", help="virtual machine + clock, no hardware")
    ap.add_argument("--emit", help="with --sim --mode plot: write the EBB command stream (a compiled job) here")
    ap.add_argument("--port")
    ap.add_argument("--config", default=str(ROOT / "config" / "machine.json"))
    ap.add_argument("--width", type=float, help="scale the drawing to this width in mm (keeps aspect)")
    ap.add_argument("--scale", type=float, default=1.0, help="extra uniform scale factor")
    ap.add_argument("--x0", type=float, default=0.0, help="drawing offset right of S, mm")
    ap.add_argument("--y0", type=float, default=0.0, help="drawing offset toward you from S, mm")
    ap.add_argument("--anchor", choices=["top-left", "top-center", "center"], default="top-left",
                    help="which point of the drawing is placed at S (default: top-left)")
    ap.add_argument("--start-x", type=float, help="S distance from machine left edge, mm (default: paper x0)")
    ap.add_argument("--start-y", type=float, help="S distance from machine top edge, mm (default: paper y0)")
    ap.add_argument("--paper-x0", type=float, help="paper top-left X on the bed, mm (overrides config)")
    ap.add_argument("--paper-y0", type=float, help="paper top-left Y on the bed, mm (overrides config)")
    ap.add_argument("--paper-width", type=float, help="paper width in mm (overrides config)")
    ap.add_argument("--paper-height", type=float, help="paper height in mm (overrides config)")
    ap.add_argument("--region-x", type=float, help="plot region left edge, mm (inside the canvas)")
    ap.add_argument("--region-y", type=float, help="plot region top edge, mm")
    ap.add_argument("--region-width", type=float, help="plot region width, mm")
    ap.add_argument("--region-height", type=float, help="plot region height, mm")
    ap.add_argument("--speed", type=float, default=15.0, help="drawing speed mm/s")
    ap.add_argument("--travel-speed", type=float, default=100.0, help="pen-up speed mm/s")
    ap.add_argument("--simplify", type=float, default=0.05, help="RDP tolerance mm")
    ap.add_argument("--join", type=float, default=0.1, help="join strokes whose ends are within this many mm")
    ap.add_argument("--pen-delay", type=int, default=250, help="ms to wait after each pen up/down")
    ap.add_argument("--pen-down", type=int, help="override servo pen-down value")
    ap.add_argument("--pen-up", type=int, help="override servo pen-up value")
    ap.add_argument("--ignore-unsupported", action="store_true")
    ap.add_argument("--fill", type=float, default=0.0, metavar="MM",
                    help="fill shapes that have a fill colour with hatch lines this far apart on paper at full ink (0 = off); "
                         "lighter colours get wider lines, very dark ones a cross pass")
    ap.add_argument("--fill-angle", type=float, default=45.0, help="hatch angle in degrees (default 45)")
    ap.add_argument("--track", action="store_true", help="emit __POS__ lines for the web live tracker")
    ap.add_argument("--control", action="store_true", help="read CONTINUE/PAUSE/STOP/RETURN from stdin (web UI)")
    ap.add_argument("--sim-speed", type=float, help="with --sim: also run in real time at this speed factor")
    ap.add_argument("--preview-json", help="dry run: write the plotted strokes (machine mm) and summary here")
    ap.add_argument("--layers", help="JSON file: per ink layer settings (enabled, speed, pressure, pen, passes, rank, name)")
    ap.add_argument("--fills", help="JSON file: {fill ink key: {mode: hatch|contour|off, spacing, angle, cross, "
                                    "zigzag}}; inks without an entry follow --fill")
    ap.add_argument("--optimize", action="store_true",
                    help="less pen-up travel: within each pen, draw the nearest stroke next, reversing open strokes "
                         "and starting closed ones at the nearest point")
    ap.add_argument("--dedupe", action="store_true", help="drop strokes that repeat an earlier stroke of the same ink")
    ap.add_argument("--start-at", type=int, default=0, metavar="N",
                    help="skip the first N strokes of the plot order (resume a stopped plot)")
    ap.add_argument("--trace-shape", choices=("box", "hull", "objects"), default="box",
                    help="trace mode: the drawing's box, its convex outline, or each object's box")
    ap.add_argument("--trace-repeat", action="store_true",
                    help="trace mode: go round again (up to 50 times) until RETURN or STOP, to line up paper")
    ap.add_argument("--overlaps", action="store_true",
                    help="leave out the parts of strokes that run along an earlier stroke of the same pen (needs shapely)")
    ap.add_argument("--corner-starts", action="store_true",
                    help="closed paths start at a corner (a turn of 30 degrees or more) when they have one")
    ap.add_argument("--inner-first", action="store_true",
                    help="within each pen, shapes inside other shapes are drawn before them")
    ap.add_argument("--approach", type=float, default=0.0, metavar="MM",
                    help="arrive at every stroke start from up-left, this far out: the same last move each time "
                         "(takes up gear backlash)")
    ap.add_argument("--start-delay", type=float, default=0.0, metavar="S", help="wait before the first stroke")
    ap.add_argument("--end-delay", type=float, default=0.0, metavar="S", help="wait after the last stroke")
    ap.add_argument("--keep-motors", action="store_true",
                    help="after a plot that ends normally, leave the motors on: they hold the position (and get warm)")
    ap.add_argument("--by-object", action="store_true",
                    help="within each pen, finish one object before the next, in the document's object order "
                         "(data-obj on the web UI's composed document); nearest stroke next inside an object")
    ap.add_argument("--sweep", choices=("none", "down", "right"), default="none",
                    help="plot order within each pen in 20 mm bands, top to bottom (down) or left to right (right), "
                         "nearest next inside a band: the hand and the drawn ink stay behind the pen")
    ap.add_argument("--copies", type=int, default=1, metavar="N",
                    help="plot the drawing N times (1-50), back at S with a paper-change pause between copies; "
                         "--start-at applies to the first copy only")
    ap.add_argument("--finish", choices=("start", "stay", "clear"), default="start",
                    help="where the carriage ends: back at S (default), where the last stroke ended, or at the bed's "
                         "right edge level with S, clear of the paper")
    ap.add_argument("--absolute", action="store_true",
                    help="the SVG's own millimetres are machine millimetres: no anchoring, --x0/--y0/--width ignored "
                         "(the web UI composes a bed-sized document)")
    a = ap.parse_args()
    global EMIT
    if a.emit:
        if not a.sim or a.mode != "plot":
            sys.exit("--emit compiles a plot: use it with --sim --mode plot (nothing moves)")
        EMIT = []
    if a.sim_speed is not None and not (a.sim and 0 < a.sim_speed <= 1000):
        sys.exit("--sim-speed needs --sim and a factor in (0, 1000]")
    a.ctl = Control() if a.control else None

    def _sigterm(signum, frame):
        raise KeyboardInterrupt()   # run the same pen-up/motors-off cleanup as Ctrl-C

    signal.signal(signal.SIGTERM, _sigterm)

    if not (0 < a.speed <= MAX_SPEED and 0 < a.travel_speed <= MAX_TRAVEL_SPEED):
        sys.exit(f"drawing speed must be in (0, {MAX_SPEED}] mm/s, travel speed in (0, {MAX_TRAVEL_SPEED}] mm/s")
    cfg = json.loads(pathlib.Path(a.config).read_text())
    if a.pen_down:
        cfg["servo"]["pen_down"] = a.pen_down
    if a.pen_up:
        cfg["servo"]["pen_up"] = a.pen_up
    for k in ("pen_up", "pen_down"):
        if not 7500 <= cfg["servo"][k] <= 22000:
            sys.exit(f"servo {k}={cfg['servo'][k]} outside 7500..22000")
    spmm = (cfg["steps_per_mm"]["x"], cfg["steps_per_mm"]["y"])
    mx, my = cfg["travel_mm"]["x"], cfg["travel_mm"]["y"]
    axes = [cfg["axis"][k] for k in ("x", "y")]
    if sorted(ax["ebb_axis"] for ax in axes) != [1, 2] or any(ax["sign"] not in (1, -1) for ax in axes):
        sys.exit("config axis: x and y must map to distinct EBB axes 1 and 2, each with sign 1 or -1")
    if min(spmm) <= 0 or mx <= 0 or my <= 0:
        sys.exit("config steps_per_mm and travel_mm must be positive")

    # Paper rectangle: the region the drawing is allowed to occupy. CLI flags
    # override config/machine.json; start position defaults to the paper corner.
    paper = cfg.get("paper") or {}
    px0 = a.paper_x0 if a.paper_x0 is not None else float(paper.get("x0", 0.0))
    py0 = a.paper_y0 if a.paper_y0 is not None else float(paper.get("y0", 0.0))
    pw = a.paper_width if a.paper_width is not None else float(paper.get("width", mx))
    ph = a.paper_height if a.paper_height is not None else float(paper.get("height", my))
    if pw <= 0 or ph <= 0:
        sys.exit("paper width and height must be positive")
    px1, py1 = px0 + pw, py0 + ph
    margin = max(0.0, float(paper.get("margin_mm", 0.0)))
    cx0, cy0, cx1, cy1 = px0 + margin, py0 + margin, px1 - margin, py1 - margin
    if cx1 <= cx0 or cy1 <= cy0:
        sys.exit("paper margin leaves no reachable canvas")
    region = None
    if None not in (a.region_x, a.region_y, a.region_width, a.region_height):
        rx, ry, rw, rh = a.region_x, a.region_y, a.region_width, a.region_height
        if rw <= 0 or rh <= 0 or rx < cx0 - 0.5 or ry < cy0 - 0.5 or rx + rw > cx1 + 0.5 or ry + rh > cy1 + 0.5:
            sys.exit(f"region ({rx},{ry},{rw},{rh}) must lie inside the canvas "
                     f"({cx0:.1f}..{cx1:.1f} x {cy0:.1f}..{cy1:.1f} mm)")
        region = (rx, ry, rx + rw, ry + rh)
        # The carriage goes to the matching anchor of the chosen region.
        if a.anchor == "top-left":
            a.start_x, a.start_y = rx, ry
        elif a.anchor == "top-center":
            a.start_x, a.start_y = rx + rw / 2, ry
        else:
            a.start_x, a.start_y = rx + rw / 2, ry + rh / 2
    if a.start_x is None:
        a.start_x = px0
    if a.start_y is None:
        a.start_y = py0

    OUT.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    LOGF = open(OUT / f"plot_{stamp}.log", "w")
    log(f"mode={a.mode} sim={a.sim} svg={a.svg}")

    if a.fill and not 0.2 <= a.fill <= 50:
        sys.exit("--fill spacing must be 0 (off) or 0.2..50 mm")
    polys, metas, (w_mm, h_mm), fills = load_svg(a.svg, a.ignore_unsupported)
    base_scale = a.scale * (a.width / w_mm if a.width else 1.0)
    scale = base_scale
    if region:
        rx0, ry0, rx1, ry1 = region
        raw = bbox(polys)
        fit = min((rx1 - rx0) / max(1e-9, (raw[2] - raw[0]) * base_scale),
                  (ry1 - ry0) / max(1e-9, (raw[3] - raw[1]) * base_scale))
        scale = base_scale * fit   # fit the whole drawing into the chosen region
    strokes, metas = prepare(polys, metas, a.simplify, a.join, scale, 0.0, 0.0)
    fills_cfg = {}
    if a.fills:
        fills_cfg = json.loads(pathlib.Path(a.fills).read_text())
        if not isinstance(fills_cfg, dict):
            sys.exit("--fills must be a JSON object")
        for k, fc in fills_cfg.items():
            if not isinstance(fc, dict) or fc.get("mode") not in ("hatch", "contour", "off"):
                sys.exit(f"--fills {k}: mode must be hatch, contour or off")
            if fc["mode"] != "off" and not 0.2 <= float(fc.get("spacing") or 0) <= 50:
                sys.exit(f"--fills {k}: spacing must be 0.2..50 mm")
    fill_inks = {}
    for f in fills:
        k = layer_key((f["color"], f["layer"]))
        fill_inks.setdefault(k, {"key": k, "color": f["color"], "label": f["layer"], "shapes": 0})["shapes"] += 1
    if fills and (a.fill or fills_cfg):
        fpolys, fmetas = [], []
        for fi, f in enumerate(fills):
            rings = [[(x * scale, y * scale) for x, y in r] for r in f["rings"]]
            fc = fills_cfg.get(layer_key((f["color"], f["layer"])))
            try:
                if fc:
                    # this ink's own fill: exactly the spacing given, whatever the colour's darkness
                    sp, ang = float(fc.get("spacing") or 0), float(fc.get("angle", 45.0))
                    if fc["mode"] == "off":
                        lines = []
                    elif fc["mode"] == "contour":
                        lines = fillmod.contour_rings(rings, sp, f["rule"])
                    else:
                        zz = fc.get("zigzag", True) is not False
                        lines = []
                        for k in range(int(fc.get("passes") or 1)):     # each pass turned by angle_step
                            ak = ang + k * float(fc.get("angle_step") or 0)
                            lines += fillmod.hatch_rings(rings, sp, ak, f["rule"], zigzag=zz)
                            if fc.get("cross"):
                                lines += fillmod.hatch_rings(rings, sp, ak + 90.0, f["rule"], zigzag=zz)
                    if fc.get("inset") and lines:
                        lines = [ln for ln in (inset_line(ln, float(fc["inset"])) for ln in lines) if ln]
                    if fc.get("bridge") and lines:
                        lines = pathplan.join_touching([[tuple(p) for p in ln] for ln in lines], float(fc["bridge"]))
                elif a.fill:
                    dark = fillmod.darkness(fillmod.color_luminance(f["color"]), f["opacity"])
                    lines = fillmod.fill_shape(rings, a.fill, dark, a.fill_angle, f["rule"])
                else:
                    lines = []
            except ValueError as ex:
                sys.exit(str(ex))
            # own_layer: the fill gets a layer (and so a pen) of its own, apart from the outlines of the same colour
            flayer = f"{f['layer'] or color_label((f['color'], None))} fill" if fc and fc.get("own_layer") else f["layer"]
            for ln in lines:
                fpolys.append(ln)
                fmetas.append({"color": f["color"], "layer": flayer, "pressure": f["pressure"], "obj": f.get("obj"),
                               "shape": fi + 1, "flayer": f["layer"]})
        fstrokes, fmetas = prepare(fpolys, fmetas, a.simplify, a.join, 1.0, 0.0, 0.0)
        log(f"Fill           : {len(fills)} filled shape(s) -> {len(fstrokes)} fill strokes"
            + (f" (default hatch {a.fill} mm, {a.fill_angle:g} deg)" if a.fill else "")
            + (f", {len(fills_cfg)} ink(s) set on their own" if fills_cfg else ""))
        for m in fmetas:
            m["fill"] = True
        strokes, metas = strokes + fstrokes, metas + fmetas      # inks keep the order of the outlines; fills are drawn first within an ink
    if not strokes:
        sys.exit("no drawable geometry found")
    n_dup = 0
    if a.dedupe:
        strokes, metas, n_dup = pathplan.dedupe(strokes, metas)
        log(f"Duplicates     : {n_dup} stroke(s) repeated an earlier one and were dropped")
    if a.overlaps:
        strokes, metas, cut = pathplan.drop_overlaps(strokes, metas)
        log("Overlaps       : " + ("shapely is missing, nothing removed" if cut is None
                                   else f"{cut:.0f} mm of line that ran over an earlier stroke was left out"))
    groups = color_groups(metas)
    # Per-layer settings (web UI). Placement below uses ALL strokes, so switching a layer off never moves
    # the others; disabled layers are dropped from the plot afterwards.
    lset = [{} for _ in groups]
    if a.layers:
        raw_l = json.loads(pathlib.Path(a.layers).read_text())
        if not isinstance(raw_l, list) or len(raw_l) > len(groups):
            sys.exit(f"--layers must be a JSON list with at most {len(groups)} entries (one per ink layer)")
        for gi, ent in enumerate(raw_l):
            ent = ent or {}
            sp_l, pr_l, pen_l = ent.get("speed"), ent.get("pressure"), ent.get("pen")
            if sp_l is not None and not (0 < float(sp_l) <= MAX_SPEED):
                sys.exit(f"layer {gi + 1}: speed must be in (0, {MAX_SPEED}] mm/s")
            if pr_l is not None and not (0.0 <= float(pr_l) <= MAX_PRESSURE):
                sys.exit(f"layer {gi + 1}: pressure must be in 0..{MAX_PRESSURE}")
            if pen_l is not None and (not isinstance(pen_l, str) or len(pen_l) > 60):
                sys.exit(f"layer {gi + 1}: pen must be a short name")
            want = ent.get("key")
            if want is not None and want != layer_key(groups[gi][0]):
                sys.exit(f"layer {gi + 1}: settings are for {want!r} but this layer is {layer_key(groups[gi][0])!r}; "
                         "re-run the preview")
            passes = ent.get("passes") or 1
            if not isinstance(passes, int) or not 1 <= passes <= 5:
                sys.exit(f"layer {gi + 1}: passes must be 1..5")
            rank = ent.get("rank")
            rank = gi if rank is None else rank
            if not isinstance(rank, (int, float)) or isinstance(rank, bool):
                sys.exit(f"layer {gi + 1}: rank must be a number")
            nm = ent.get("name")
            if nm is not None and (not isinstance(nm, str) or len(nm) > 40):
                sys.exit(f"layer {gi + 1}: name must be short text")

            def rng(k, lo, hi):
                v = ent.get(k)
                if v is None:
                    return None
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                    sys.exit(f"layer {gi + 1}: {k} must be {lo:g}..{hi:g}")
                return float(v)
            dash = ent.get("dash")
            if dash is not None and not (isinstance(dash, list) and len(dash) == 2
                                         and all(isinstance(v, (int, float)) and 0.2 <= v <= 50 for v in dash)):
                sys.exit(f"layer {gi + 1}: dash must be [on mm, off mm], each 0.2..50")
            lset[gi] = {"enabled": ent.get("enabled", True) is not False,
                        "speed": float(sp_l) if sp_l is not None else None,
                        "pressure": float(pr_l) if pr_l is not None else None, "pen": pen_l,
                        "passes": passes, "rank": rank, "name": nm,
                        "pstep": rng("pressure_step", -0.4, 0.4), "dwell": rng("dwell_ms", 0, 3000),
                        "overdraw": rng("overdraw", 0, 30), "dash": dash}
            for i in groups[gi][1]:
                if lset[gi]["pressure"] is not None and metas[i].get("pressure") is None:
                    metas[i]["pressure"] = lset[gi]["pressure"]   # layer default; per-stroke data-pressure wins
    if not any(ls.get("enabled", True) for ls in lset):
        sys.exit("every layer is switched off: nothing to plot")
    stroke_group = {}
    for gi, (_, idxs) in enumerate(groups):
        for i in idxs:
            stroke_group[i] = gi
    x0, y0, x1, y1 = bbox(strokes)
    # The anchor is on the PAPER (where the operator places the carriage);
    # the drawing goes into the target rectangle — the region if chosen, else
    # the inset canvas. Shift by the difference between the two anchor points.
    tx0, ty0, tx1, ty1 = region if region else (cx0, cy0, cx1, cy1)
    if region:
        shift_x = shift_y = 0.0   # S is already the region's anchor point
    elif a.anchor == "top-left":
        shift_x, shift_y = tx0 - px0, ty0 - py0
    elif a.anchor == "top-center":
        shift_x, shift_y = (tx0 + tx1) / 2 - (px0 + px1) / 2, ty0 - py0
    else:
        shift_x, shift_y = (tx0 + tx1) / 2 - (px0 + px1) / 2, (ty0 + ty1) / 2 - (py0 + py1) / 2
    ox, oy = a.x0, a.y0
    if a.absolute:
        ox, oy = -a.start_x, -a.start_y         # document mm = machine mm, S is the origin of the motion
    elif a.anchor == "top-left":
        ox += shift_x - x0
        oy += shift_y - y0
    elif a.anchor == "top-center":
        ox += shift_x - (x0 + x1) / 2
        oy += shift_y - y0
    else:
        ox += shift_x - (x0 + x1) / 2
        oy += shift_y - (y0 + y1) / 2
    gx0, gy0, gx1, gy1 = x0 + ox, y0 + oy, x1 + ox, y1 + oy
    abs_x0, abs_y0 = a.start_x + gx0, a.start_y + gy0
    abs_x1, abs_y1 = a.start_x + gx1, a.start_y + gy1
    pts = sum(len(s) for s in strokes)
    # Plot order: by ink layer when plotting several layers (one pen swap each), else file order.
    # Disabled layers (--layers) are left out.
    by_layer = a.mode in ("plot", "dry") and len(groups) > 1
    order = [i for _, idxs in groups for i in idxs] if by_layer else list(range(len(strokes)))
    order = [i for i in order if lset[stroke_group[i]].get("enabled", True)]
    # Layers plot in their rank order (the operator can reorder pens); within a layer fills come before outlines.
    rank_of = [lset[gi].get("rank", gi) for gi in range(len(groups))]
    obj_of = (lambda i: metas[i].get("obj") or 0) if a.by_object else (lambda i: 0)

    def fill_group(i):
        """Fill strokes of an ink set to 'object' or 'shape' grouping finish one object or shape before the next."""
        m = metas[i]
        if not m.get("fill"):
            return 0
        g = (fills_cfg.get(layer_key((m.get("color"), m.get("flayer", m.get("layer"))))) or {}).get("group", "all")
        return m.get("obj") or 0 if g == "object" else m.get("shape") or 0 if g == "shape" else 0
    order.sort(key=lambda i: ((rank_of[stroke_group[i]], stroke_group[i]) if by_layer else (0, 0), obj_of(i),
                              not metas[i].get("fill"), fill_group(i)))
    # a stroke's own data-speed (test sheets) wins over its layer's speed, which wins over the job's
    speed_of = [metas[i].get("speed") or lset[stroke_group[i]].get("speed") or a.speed for i in range(len(strokes))]
    pen_s = a.pen_delay / 1000 + 0.15
    file_order = None
    if (a.optimize or a.sweep != "none") and order:
        # Measured on the file order first, for the before/after readout. S is (-ox, -oy) in stroke coordinates.
        _, t_file, _, s_file = estimate([[(x + ox, y + oy) for x, y in strokes[i]] for i in order], spmm, a.speed,
                                        a.travel_speed, pen_s, speeds=[speed_of[i] for i in order])
        # a start point picked on the bed: in absolute mode stroke coordinates are machine mm already
        seams = {i: metas[i]["seam"] for i in order if metas[i].get("seam")} if a.absolute else {}
        blocks, cur = [], (-ox, -oy)
        for i in order:     # one block per pen and per fill/outline run: those boundaries stay where they are
            k = (stroke_group[i] if by_layer else 0, obj_of(i), bool(metas[i].get("fill")), fill_group(i))
            if not blocks or blocks[-1][0] != k:
                blocks.append((k, []))
            blocks[-1][1].append(i)
        order = []
        for _, idxs in blocks:
            if a.sweep == "none":
                bands = [idxs]
            else:       # 20 mm bands by each stroke's top (down) or left edge (right), in sweep order
                ax_ = 1 if a.sweep == "down" else 0
                by = {}
                for i in idxs:
                    by.setdefault(math.floor(min(p[ax_] for p in strokes[i]) / 20.0), []).append(i)
                bands = [by[k] for k in sorted(by)]
            for band in bands:
                for tier in (depth_tiers(strokes, band) if a.inner_first else [band]):
                    o, cur = pathplan.order_block(strokes, tier, cur, corners=a.corner_starts, seams=seams)
                    order += o
        file_order = {"travel_mm": t_file, "est_seconds": s_file}
    if any((ls.get("passes") or 1) > 1 for ls in lset):
        # Passes: each stroke is drawn again right away, an open one back over itself (no travel), so the
        # extra passes are new strokes after the original in the order.
        expanded = []
        for i in order:
            expanded.append(i)
            for k in range(1, lset[stroke_group[i]].get("passes") or 1):
                s = strokes[i]
                closed = len(s) > 2 and math.dist(s[0], s[-1]) < 0.1
                strokes.append(list(s) if closed or k % 2 == 0 else s[::-1])
                m = dict(metas[i])
                step = lset[stroke_group[i]].get("pstep")
                if step:          # each pass a little lighter (or heavier), never past the limits
                    m["pressure"] = min(MAX_PRESSURE, max(0.0, (m.get("pressure") or 0.0) + k * step))
                metas.append(m)
                stroke_group[len(strokes) - 1] = stroke_group[i]
                speed_of.append(speed_of[i])
                expanded.append(len(strokes) - 1)
        order = expanded
    if any(ls.get("overdraw") or ls.get("dash") for ls in lset):
        new_order = []
        for i in order:
            ls = lset[stroke_group[i]]
            s = strokes[i]
            if ls.get("overdraw") and len(s) > 3 and math.dist(s[0], s[-1]) < 0.1:
                strokes[i] = s = extend_closed(s, ls["overdraw"])
            if ls.get("dash") and len(s) >= 2:
                pieces = dash_split(s, *ls["dash"])
                if not pieces:
                    continue
                strokes[i] = pieces[0]
                new_order.append(i)
                for pc in pieces[1:]:
                    strokes.append(pc)
                    metas.append(dict(metas[i]))
                    stroke_group[len(strokes) - 1] = stroke_group[i]
                    speed_of.append(speed_of[i])
                    new_order.append(len(strokes) - 1)
            else:
                new_order.append(i)
        order = new_order
    total_in_order = len(order)
    full_order = list(order)
    # Pen changes: between consecutive enabled layers in plot order, unless both name the same pen.
    on_ranked = sorted((gi for gi in range(len(groups)) if lset[gi].get("enabled", True)), key=lambda g: (rank_of[g], g))
    swaps = sum(1 for p_, n_ in zip(on_ranked, on_ranked[1:]) if not same_pen(lset[p_], lset[n_]))
    if not 1 <= a.copies <= 50:
        sys.exit("--copies must be 1 to 50")
    if a.start_at:
        if not 0 < a.start_at < len(order):
            sys.exit(f"--start-at {a.start_at}: the plot has {len(order)} strokes")
        order = order[a.start_at:]
        log(f"Resume         : starting at stroke {a.start_at + 1} of {total_in_order}; the first {a.start_at} are skipped")
    per_stroke = []
    dd, dt, lifts, secs = estimate([[(x + ox, y + oy) for x, y in strokes[i]] for i in order], spmm, a.speed,
                                   a.travel_speed, pen_s, speeds=[speed_of[i] for i in order], per_stroke=per_stroke)
    per_full, secs_full = per_stroke, secs
    if a.copies > 1 and a.start_at:
        per_full = []
        secs_full = estimate([[(x + ox, y + oy) for x, y in strokes[i]] for i in full_order], spmm, a.speed,
                             a.travel_speed, pen_s, speeds=[speed_of[i] for i in full_order], per_stroke=per_full)[3]
    extra = sum(2 * (lset[stroke_group[i]].get("dwell") or 0) / 1000 for i in order) + a.start_delay + a.end_delay
    secs += extra
    secs_full += extra
    secs_all = secs + (a.copies - 1) * secs_full       # each copy ends with the trip back to S
    if file_order:
        log(f"Path planning  : pen-up travel {file_order['travel_mm'] / 1000:.2f} m -> {dt / 1000:.2f} m, "
            f"time {fmt(file_order['est_seconds'])} -> {fmt(secs)}" + (" (whole plot)" if a.start_at else ""))
    problems = []
    # Only the inset canvas is reachable: stay away from every paper edge, and
    # do not rely on the outer bed margin where the canvas is tighter.
    if abs_x0 < min(EDGE_MARGIN, cx0) or abs_y0 < min(EDGE_MARGIN, cy0):
        problems.append(f"drawing starts at machine ({abs_x0:.1f},{abs_y0:.1f}) mm, inside the {EDGE_MARGIN} mm edge margin")
    if abs_x1 > mx - EDGE_MARGIN or abs_y1 > my - EDGE_MARGIN:
        problems.append(f"drawing reaches machine ({abs_x1:.1f},{abs_y1:.1f}) mm; travel is {mx} x {my} mm (margin {EDGE_MARGIN})")
    # The carriage itself sits on the paper (it can be outside the canvas); the
    # drawing must stay inside the canvas.
    if not (0 <= a.start_x <= mx and 0 <= a.start_y <= my):
        problems.append(f"carriage start S=({a.start_x:.1f},{a.start_y:.1f}) mm is outside the machine travel {mx} x {my} mm")
    if a.start_x < px0 - 0.5 or a.start_y < py0 - 0.5:
        problems.append(f"carriage start S=({a.start_x:.1f},{a.start_y:.1f}) mm is OFF the paper "
                        f"(paper starts at {px0:.1f},{py0:.1f}) — slide the carriage onto the paper before starting")
    if a.start_x > px1 + 0.5 or a.start_y > py1 + 0.5:
        problems.append(f"carriage start S=({a.start_x:.1f},{a.start_y:.1f}) mm is past the paper "
                        f"({px0:.1f}..{px1:.1f} x {py0:.1f}..{py1:.1f} mm)")
    if abs_x0 < tx0 - 0.5 or abs_y0 < ty0 - 0.5 or abs_x1 > tx1 + 0.5 or abs_y1 > ty1 + 0.5:
        rname = "region" if region else f"{margin:.0f} mm canvas"
        problems.append(f"drawing would leave the {rname} ({tx0:.1f}..{tx1:.1f} x {ty0:.1f}..{ty1:.1f} mm); "
                        f"it covers {abs_x0:.1f}..{abs_x1:.1f} x {abs_y0:.1f}..{abs_y1:.1f} mm — adjust the region or --width/--x0/--y0")
    log(f"""
Drawing        : {w_mm * scale:.1f} x {h_mm * scale:.1f} mm ({len(polys)} source paths -> {len(strokes)} strokes, {pts} points after simplify {a.simplify} mm)
Placement      : {a.anchor} at S
Geometry bbox  : x {gx0:.1f}..{gx1:.1f}  y {gy0:.1f}..{gy1:.1f} mm relative to S
On the machine : x {abs_x0:.1f}..{abs_x1:.1f} of {mx}   y {abs_y0:.1f}..{abs_y1:.1f} of {my} mm  (S at {a.start_x},{a.start_y})
Paper          : x {px0:.1f}..{px1:.1f}  y {py0:.1f}..{py1:.1f} mm  (carriage must start on the paper)
Canvas         : x {cx0:.1f}..{cx1:.1f}  y {cy0:.1f}..{cy1:.1f} mm  ({margin:.0f} mm clear of every paper edge)""" +
        (f"\nRegion         : x {tx0:.1f}..{tx1:.1f}  y {ty0:.1f}..{ty1:.1f} mm  (drawing fitted to region)" if region else "") + f"""
Steps          : x {round(gx0 * spmm[0])}..{round(gx1 * spmm[0])}  y {round(gy0 * spmm[1])}..{round(gy1 * spmm[1])}
Drawing length : {dd / 1000:.2f} m at {a.speed} mm/s    pen-up travel {dt / 1000:.2f} m at {a.travel_speed} mm/s    pen lifts {lifts}
Estimated time : {fmt(secs)}
Ink layers     : {len(groups)}  ({', '.join(f'{color_label(k)}: {len(ix)} strokes' for k, ix in groups)})""" +
        (f"  -> {len(groups) - 1} pen swap(s) during Plot" if len(groups) > 1 else "") + f"""
Servo          : up {cfg['servo']['pen_up']}  down {cfg['servo']['pen_down']}""")
    used = sorted({m["pressure"] for m in metas if m.get("pressure") is not None})
    for pr in used:
        if not 0.0 <= pr <= MAX_PRESSURE:
            problems.append(f"data-pressure {pr} is outside 0..{MAX_PRESSURE} (0 = configured pen-down, larger = lighter; "
                            "never deeper than the configured pen-down)")
    if used:
        sv = cfg["servo"]
        log("Pen pressure   : " + ", ".join(
            f"{pr:g} -> {round(sv['pen_down'] + pr * (sv['pen_up'] - sv['pen_down']))}" for pr in used[:8])
            + (" ..." if len(used) > 8 else "") + "  (fraction -> servo pen-down value)")
    for p in problems:
        log("PROBLEM: " + p)
    on_paper = not any("paper" in p or "canvas" in p or "region" in p for p in problems)
    if a.mode == "dry":
        # Machine-space placement of the whole SVG canvas, for the web tracker overlay.
        print("__PLACEMENT__:" + json.dumps({
            "origin": [a.start_x + ox, a.start_y + oy],
            "size": [w_mm * scale, h_mm * scale],
            "bbox": [abs_x0, abs_y0, abs_x1, abs_y1],
            "canvas": [tx0, ty0, tx1, ty1]}), flush=True)
    summary = {"svg": a.svg, "mode": a.mode, "strokes": len(strokes), "points": pts, "est_seconds": secs,
               "bbox_machine_mm": [abs_x0, abs_y0, abs_x1, abs_y1], "problems": problems, "args": vars(a),
               "paper_mm": [px0, py0, px1, py1], "canvas_mm": [cx0, cy0, cx1, cy1],
               "region_mm": list(region) if region else None,
               "on_paper": on_paper, "anchor": a.anchor,
               "layers": [{"key": layer_key(k), "color": k[0], "label": k[1], "name": color_label(k), "strokes": len(ix),
                           "enabled": lset[gi].get("enabled", True), "speed": lset[gi].get("speed") or a.speed,
                           "pressure": lset[gi].get("pressure"), "pen": lset[gi].get("pen"),
                           "title": lset[gi].get("name"), "passes": lset[gi].get("passes") or 1, "rank": rank_of[gi]}
                          for gi, (k, ix) in enumerate(groups)],
               "swaps": swaps,
               "drawing_mm": dd, "travel_mm": dt, "lifts": lifts, "doc_mm": [w_mm, h_mm], "scale": scale,
               "start_mm": [a.start_x, a.start_y], "speed": a.speed, "travel_speed": a.travel_speed,
               "optimized": bool(file_order), "file_order": file_order, "duplicates_dropped": n_dup,
               "start_at": a.start_at, "total_in_order": total_in_order, "fill_inks": list(fill_inks.values()),
               "copies": a.copies, "est_seconds_all": secs_all, "finish": a.finish}
    summary["args"] = {k: v for k, v in vars(a).items() if k != "ctl"}

    if a.mode == "dry":
        (OUT / f"plot_{stamp}.json").write_text(json.dumps(summary, indent=2))
        if a.preview_json:
            pv = {"summary": summary,
                  "placement": {"origin": [a.start_x + ox, a.start_y + oy], "size": [w_mm * scale, h_mm * scale],
                                "bbox": [abs_x0, abs_y0, abs_x1, abs_y1], "canvas": [tx0, ty0, tx1, ty1]},
                  "order": order,
                  "strokes": [{"l": stroke_group[i], "p": metas[i].get("pressure"),
                               "pts": [[round(a.start_x + x + ox, 2), round(a.start_y + y + oy, 2)] for x, y in s]}
                              for i, s in enumerate(strokes)]}
            pathlib.Path(a.preview_json).write_text(json.dumps(pv, separators=(",", ":")))
        log("Dry run only. Nothing was sent." + ("" if not problems else "  FIX THE PROBLEMS ABOVE before trace/plot."))
        return 1 if problems else 0
    if problems:
        sys.exit("refusing to move: fix the problems above (adjust --width/--x0/--y0/--start-x/--start-y)")

    if not a.sim:
        print(f"\nCheck: motors off, carriage by hand at the paper's top-left "
              f"({a.start_x:.1f},{a.start_y:.1f}) mm, pen UP over the paper, power switch in reach.")
        word = "PLOT" if a.mode == "plot" else "TRACE"
        try:
            got = a.ctl.readline(f"Type {word} to start: ") if a.ctl else input(f"Type {word} to start: ")
        except EOFError:
            got = ""
        if got.strip() != word:
            sys.exit("not confirmed, nothing sent")
    p = Plotter(cfg, a)
    status = "complete"
    t_start = p.now()
    try:
        p.setup()
        if a.mode == "trace":
            # the frame: the drawing's box, its convex outline, or each object's box; pen up throughout
            on = [i for i in range(len(strokes)) if lset[stroke_group[i]].get("enabled", True)]
            if a.trace_shape == "hull":
                frames = [convex_hull([(x + ox, y + oy) for i in on for x, y in strokes[i]])]
            elif a.trace_shape == "objects":
                by = {}
                for i in on:
                    by.setdefault(metas[i].get("obj"), []).append(strokes[i])
                frames = []
                for ss in by.values():
                    bx0, by0, bx1, by1 = bbox(ss)
                    frames.append([(bx0 + ox, by0 + oy), (bx1 + ox, by0 + oy), (bx1 + ox, by1 + oy),
                                   (bx0 + ox, by1 + oy), (bx0 + ox, by0 + oy)])
            else:
                frames = [[(gx0, gy0), (gx1, gy0), (gx1, gy1), (gx0, gy1), (gx0, gy0)]]
            rounds = 50 if a.trace_repeat else 1
            for r in range(rounds):
                for fr in frames:
                    for cx, cy in fr:
                        p.move_to(*to_steps((cx, cy), spmm), a.travel_speed)
                if a.ctl and a.ctl.ret.is_set():
                    break
                if r == 0 and a.trace_repeat:
                    log("Tracing again until Return to start (or Stop): line the paper up now.")
            p.move_to(0, 0, a.travel_speed)
            p.idle()
        else:
            n = len(order) + (a.copies - 1) * len(full_order)
            next_report = 0.05
            drawn = 0
            est_done = 0.0
            last_prog = [-1.0]
            ctl = a.ctl

            def progress(gi, force=False):
                if not a.track:
                    return
                now = p.now()
                if not force and now - last_prog[0] < 0.25:
                    return
                last_prog[0] = now
                el = now - t_start - (0.0 if a.sim or not ctl else ctl.paused_s)
                print("__PROGRESS__:" + json.dumps({
                    "done": drawn, "strokes": n, "layer": gi, "est_done_s": round(est_done, 2),
                    "est_total_s": round(secs_all, 2), "elapsed_s": round(el, 2), "copy": copy_no[0],
                    "copies": a.copies}), flush=True)

            def draw_one(i):
                nonlocal drawn, next_report, est_done
                s = strokes[i]
                gi = stroke_group[i]
                pts_s = [to_steps((x + ox, y + oy), spmm) for x, y in s]
                if a.approach > 0:          # always the same last move into the start: backlash taken up
                    p.move_to(*to_steps((s[0][0] + ox - a.approach, s[0][1] + oy - a.approach), spmm), a.travel_speed)
                p.move_to(*pts_s[0], a.travel_speed)
                p.set_pressure(metas[i].get("pressure"))
                if drawn == 0 and a.start_delay > 0:
                    p.idle()
                    p.sleep(a.start_delay)
                p.pen(False, a.pen_delay)
                dwell = (lset[gi].get("dwell") or 0) / 1000
                if dwell:
                    p.idle(); p.sleep(dwell)
                sp = min(MAX_SPEED, speed_of[i] * (ctl.speed_factor if ctl else 1.0))
                for tx, ty in pts_s[1:]:
                    p.move_to(tx, ty, sp)
                if dwell:
                    p.idle(); p.sleep(dwell)
                p.pen(True, a.pen_delay)
                est_done += per_stroke[drawn] if drawn < len(per_stroke) else per_full[(drawn - len(order)) % len(per_full)]
                drawn += 1
                progress(gi, force=drawn == n)
                if drawn / n >= next_report:
                    el = p.now() - t_start
                    log(f"{drawn}/{n} strokes  {100 * drawn / n:.0f}%  elapsed {fmt(el)}  "
                        f"eta {fmt(el / (drawn / n) - el)}")
                    next_report += 0.05

            returned = False
            cur_layer = None
            copy_no = [1]
            plan = [(c, i) for c in range(a.copies) for i in (order if c == 0 else full_order)]
            for c, i in plan:
                if c + 1 != copy_no[0]:
                    # a new copy: back to S, then wait for fresh paper (the pen stays the last layer's)
                    p.move_to(0, 0, a.travel_speed)
                    p.idle()
                    copy_no[0] = c + 1
                    if not pause_for_paper(c + 1, a.copies, a, ctl):
                        returned = True
                        break
                gi = stroke_group[i]
                if gi != cur_layer:
                    key = groups[gi][0]
                    info = {"index": gi, "label": lset[gi].get("name") or color_label(key), "color": key[0], "pen": lset[gi].get("pen"),
                            "speed": speed_of[i], "pressure": lset[gi].get("pressure"), "strokes": len(groups[gi][1])}
                    if cur_layer is not None and by_layer and not same_pen(lset[cur_layer], lset[gi]):
                        if not pause_for_swap(key, a, ctl, {"layer_index": gi, "pen": lset[gi].get("pen")}):
                            returned = True
                            break
                    cur_layer = gi
                    if a.track:
                        print("__LAYER__:" + json.dumps(info), flush=True)
                    log(f"Layer {gi + 1}: {info['label']}" + (f", pen {info['pen']}" if info["pen"] else "")
                        + f", {info['speed']:g} mm/s")
                if ctl and ctl.ret.is_set():
                    returned = True
                    break
                if ctl and ctl.pause.is_set():
                    log("Paused by the operator: pen up, motors on (do not push the carriage).")
                    ctl.drain()
                    print("__PAUSE__:" + json.dumps({"kind": "operator", "layer_index": gi}), flush=True)
                    if not ctl.wait_continue():
                        returned = True
                        break
                    log("Resumed.")
                    print("__RESUMED__:{}", flush=True)
                draw_one(i)
            if returned:
                log("Return requested: pen up, travelling back to the start point S.")
                status = "stopped, returned to start"
            if a.end_delay > 0 and not returned:
                p.idle()
                p.sleep(a.end_delay)
            if returned or a.finish == "start":
                p.move_to(0, 0, a.travel_speed)
            elif a.finish == "clear":
                p.move_to(*to_steps((mx - EDGE_MARGIN - a.start_x, 0.0), spmm), a.travel_speed)
                log(f"Finish: parked at the bed's right edge ({mx - EDGE_MARGIN:.0f}, {a.start_y:.0f}) mm, clear of the paper.")
            else:
                log("Finish: staying where the last stroke ended.")
            p.idle()
    except KeyboardInterrupt:
        status = "interrupted"
        log("Interrupted: raising pen, then releasing motors once the move in flight ends. Position is now unknown.")
    except Exception as ex:   # noqa: BLE001
        status = f"error: {ex!r}"
        log(f"ERROR: {ex!r}")
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, signal.SIG_IGN)   # a second Ctrl-C/SIGTERM must not cut the cleanup short
            except (ValueError, OSError):
                pass
        p.keep_motors = bool(getattr(a, "keep_motors", False)) and status == "complete"
        if not p.shutdown(abort=status.startswith(("interrupted", "error"))):
            status += " (cleanup FAILED: pen may be down or motors still on, check the machine and cut power if needed)"
        if EMIT is not None and a.emit and status == "complete":
            write_emit(a.emit, cfg, a)
        el = p.now() - t_start
        summary.update(status=status, elapsed_s=el, sm_commands=p.nsm, net_ebb_steps_sent=p.sent,
                       final_world_steps=p.pos)
        (OUT / f"plot_{stamp}.json").write_text(json.dumps(summary, indent=2))
        log(f"\nStatus: {status}   elapsed {fmt(el)}   SM commands {p.nsm}   net steps sent {p.sent}   end position {p.pos}")
        if a.track:
            print("__RESULT__:" + json.dumps({
                "status": status, "elapsed_s": round(el, 2),
                "end_mm": [round(a.start_x + p.pos[0] / spmm[0], 2), round(a.start_y + p.pos[1] / spmm[1], 2)],
                "readback": p.readback,
                "at_start": p.pos == [0, 0] and not status.startswith(("interrupted", "error")) and "FAILED" not in status}), flush=True)
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
