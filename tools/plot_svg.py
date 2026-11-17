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
import re
import signal
import sys
import time
import xml.etree.ElementTree as ET

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


def element_color(el):
    style = el.get("style") or ""

    def find(prop):
        c = el.get(prop)
        if not c:
            m = re.search(r"(?:^|;)\s*" + prop + r"\s*:\s*([^;]+)", style)
            c = m.group(1) if m else None
        return c

    c = find("stroke")
    if not c or c.strip().lower() == "none":
        c = find("fill")
    if not c or c.strip().lower() == "none":
        return "#000000"   # unstyled geometry uses the default black pen
    return normalize_color(c) or "#000000"


def element_layer(el):
    if el.get(INKNS + "groupmode") == "layer":
        return el.get(INKNS + "label") or el.get("id") or None
    return None


def color_label(key):
    color, layer = key
    return layer or INK_NAMES.get(color) or color or "Ink"


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


def pause_for_swap(key, a):
    """Tell the operator (and the web UI) to change pens, then wait for CONTINUE."""
    info = {"color": key[0], "layer": key[1], "label": color_label(key)}
    log("")
    log(f"*** PEN CHANGE: swap to {color_label(key)} ***")
    print("__PAUSE__:" + json.dumps(info), flush=True)
    if a.sim:
        return
    try:
        input("Pen swapped? Type CONTINUE to resume: ")
    except EOFError:
        raise SystemExit("input closed while waiting for a pen swap")


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
        meta = {"color": cur.get("color"), "layer": cur.get("layer"), "pressure": cur.get("pressure")}

        def add(poly):
            if len(poly) >= 2:
                polys.append([mat_apply(xform, p) for p in poly])
                metas.append(dict(meta))

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
        for child in el:
            walk(child, cur, xform)

    walk(root, {"color": None, "layer": None, "pressure": None}, IDENTITY)
    if skipped:
        msg = f"unsupported elements: {skipped}"
        if not ignore_unsupported:
            raise ValueError(msg + " (use --ignore-unsupported to skip them)")
        log("WARNING: " + msg + " -- skipped")
    mm = [[((px - vx) * scale, (py - vy) * scale) for px, py in p] for p in polys]
    return mm, metas, (vw * scale, vh * scale)


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
    for p, m in zip(sp, metas):
        same_ink = merged and (m.get("color"), m.get("layer"), m.get("pressure")) == \
                   (mmeta[-1].get("color"), mmeta[-1].get("layer"), mmeta[-1].get("pressure"))
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
        if not self.sim:
            port = args.port
            if not port:
                port = next((p.device for p in list_ports.comports() if (p.vid, p.pid) == (EBB_VID, EBB_PID)), None)
            if not port:
                raise SystemExit("No EBB (04d8:fd92) found. Plug in/power the plotter.")
            self.ser = serial.Serial(port, 9600, timeout=3)
            log(f"Connected {port}")

    def now(self):
        return self.vt if self.sim else time.monotonic()

    def sleep(self, s):
        if s <= 0:
            return
        if self.sim:
            self.vt += s
        else:
            time.sleep(s)

    def cmd(self, line, timeout=3.0):
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
        self.pen_state = up
        self.cmd(f"SP,{1 if up else 0},{delay}")
        self.sleep(delay / 1000 + 0.05)
        self.t_end = max(self.t_end, self.now())
        self.track(force=True)

    def shutdown(self):
        """Raise the pen, wait for any move already sent to finish, then release the motors. -> True if clean."""
        try:
            self.cmd("SP,1,300")
            self.sleep(max(0.0, self.t_end - self.now()) + 0.4)
            self.cmd("EM,0,0")
            return True
        except Exception as ex:   # noqa: BLE001
            log(f"shutdown problem: {ex!r}")
            return False

    def _sm(self, a1, a2, ms):
        wait = self.t_end - self.now() - LEAD
        if wait > 0:
            self.sleep(wait)
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


def estimate(strokes, spmm, draw_v, trav_v, pen_s):
    t = 0.0
    dist_d = dist_t = 0.0
    cur = (0.0, 0.0)
    for s in strokes:
        d = math.dist(cur, s[0])
        dist_t += d
        for a, b in zip(s, s[1:]):
            dist_d += math.dist(a, b)
        cur = s[-1]
    dist_t += math.dist(cur, (0.0, 0.0))
    lifts = len(strokes)
    t = dist_d / draw_v + dist_t / trav_v + lifts * 2 * pen_s
    return dist_d, dist_t, lifts, t


def fmt(sec):
    return f"{int(sec // 60)} min {int(sec % 60):02d} s"


# ------------------------------------------------------------------------ main
def main():
    global LOGF
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("svg")
    ap.add_argument("--mode", choices=["dry", "trace", "plot"], default="dry")
    ap.add_argument("--sim", action="store_true", help="virtual machine + clock, no hardware")
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
    ap.add_argument("--track", action="store_true", help="emit __POS__ lines for the web live tracker")
    a = ap.parse_args()

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

    polys, metas, (w_mm, h_mm) = load_svg(a.svg, a.ignore_unsupported)
    base_scale = a.scale * (a.width / w_mm if a.width else 1.0)
    scale = base_scale
    if region:
        rx0, ry0, rx1, ry1 = region
        raw = bbox(polys)
        fit = min((rx1 - rx0) / max(1e-9, (raw[2] - raw[0]) * base_scale),
                  (ry1 - ry0) / max(1e-9, (raw[3] - raw[1]) * base_scale))
        scale = base_scale * fit   # fit the whole drawing into the chosen region
    strokes, metas = prepare(polys, metas, a.simplify, a.join, scale, 0.0, 0.0)
    if not strokes:
        sys.exit("no drawable geometry found")
    groups = color_groups(metas)
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
    if a.anchor == "top-left":
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
    dd, dt, lifts, secs = estimate([[(x + ox, y + oy) for x, y in s] for s in strokes], spmm, a.speed, a.travel_speed,
                                   (a.pen_delay / 1000 + 0.15))
    problems = []
    # Only the inset canvas is reachable: stay away from every paper edge, and
    # do not rely on the outer bed margin where the canvas is tighter.
    if abs_x0 < min(EDGE_MARGIN, cx0) or abs_y0 < min(EDGE_MARGIN, cy0):
        problems.append(f"drawing starts at machine ({abs_x0:.1f},{abs_y0:.1f}) mm, inside the {EDGE_MARGIN} mm edge margin")
    if abs_x1 > max(mx - EDGE_MARGIN, cx1) or abs_y1 > max(my - EDGE_MARGIN, cy1):
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
               "layers": [{"color": k[0], "label": k[1], "name": color_label(k), "strokes": len(ix)}
                          for k, ix in groups], "swaps": max(0, len(groups) - 1)}

    if a.mode == "dry":
        (OUT / f"plot_{stamp}.json").write_text(json.dumps(summary, indent=2))
        log("Dry run only. Nothing was sent." + ("" if not problems else "  FIX THE PROBLEMS ABOVE before trace/plot."))
        return 1 if problems else 0
    if problems:
        sys.exit("refusing to move: fix the problems above (adjust --width/--x0/--y0/--start-x/--start-y)")

    if not a.sim:
        print(f"\nCheck: motors off, carriage by hand at the paper's top-left "
              f"({a.start_x:.1f},{a.start_y:.1f}) mm, pen UP over the paper, power switch in reach.")
        if input(f"Type {'PLOT' if a.mode == 'plot' else 'TRACE'} to start: ").strip() != ("PLOT" if a.mode == "plot" else "TRACE"):
            sys.exit("not confirmed, nothing sent")
    p = Plotter(cfg, a)
    status = "complete"
    t_start = p.now()
    try:
        p.setup()
        if a.mode == "trace":
            corners = [(gx0, gy0), (gx1, gy0), (gx1, gy1), (gx0, gy1), (gx0, gy0), (0.0, 0.0)]
            for cx, cy in corners:
                log(f"trace -> ({cx:.1f},{cy:.1f}) mm")
                p.move_to(*to_steps((cx, cy), spmm), a.travel_speed)
            p.idle()
        else:
            n = len(strokes)
            next_report = 0.05
            drawn = 0

            def draw_one(i):
                nonlocal drawn, next_report
                s = strokes[i]
                pts_s = [to_steps((x + ox, y + oy), spmm) for x, y in s]
                p.move_to(*pts_s[0], a.travel_speed)
                p.set_pressure(metas[i].get("pressure"))
                p.pen(False, a.pen_delay)
                for tx, ty in pts_s[1:]:
                    p.move_to(tx, ty, a.speed)
                p.pen(True, a.pen_delay)
                drawn += 1
                if drawn / n >= next_report:
                    el = p.now() - t_start
                    log(f"{drawn}/{n} strokes  {100 * drawn / n:.0f}%  elapsed {fmt(el)}  "
                        f"eta {fmt(el / (drawn / n) - el)}")
                    next_report += 0.05

            if a.mode == "plot" and len(groups) > 1:
                for gi, (key, idxs) in enumerate(groups):
                    if gi:
                        pause_for_swap(key, a)
                    for i in idxs:
                        draw_one(i)
            else:
                for i in range(n):
                    draw_one(i)
            p.move_to(0, 0, a.travel_speed)
            p.idle()
    except KeyboardInterrupt:
        status = "interrupted"
        log("Interrupted: raising pen, then releasing motors once the move in flight ends. Position is now unknown.")
    except Exception as ex:   # noqa: BLE001
        status = f"error: {ex!r}"
        log(f"ERROR: {ex!r}")
    finally:
        if not p.shutdown():
            status += " (cleanup FAILED: pen may be down or motors still on, check the machine and cut power if needed)"
        el = p.now() - t_start
        summary.update(status=status, elapsed_s=el, sm_commands=p.nsm, net_ebb_steps_sent=p.sent,
                       final_world_steps=p.pos)
        (OUT / f"plot_{stamp}.json").write_text(json.dumps(summary, indent=2))
        log(f"\nStatus: {status}   elapsed {fmt(el)}   SM commands {p.nsm}   net steps sent {p.sent}   end position {p.pos}")
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
