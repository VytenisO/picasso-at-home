#!/usr/bin/env python3
"""Plot an SVG on the WaterColorBot (EBB firmware 2.0.1 safe: SM/SP/SC/EM only).

  plot_svg.py FILE.svg                    dry run: parse, report, no hardware (default)
  plot_svg.py FILE.svg --mode trace       pen-UP trace of the bounding box on the real machine
  plot_svg.py FILE.svg --mode plot        the real plot (asks you to type PLOT)
  add --sim to run any mode on a virtual machine and virtual clock

Setup before trace/plot: motors off, slide the carriage by hand to the start point S
(pen up, over the paper), tell the script where S is on the machine with --start-x/--start-y
(mm from the machine's top-left corner; default 20,20). The drawing's top-left lands at
S + (--x0, --y0). X is right, Y is toward you. Calibration comes from ../config/machine.json.

This firmware has no e-stop: cut power to abort immediately. Ctrl-C stops sending, raises
the pen and releases the motors, but a move already sent still finishes (<= ~3 s).
Supported SVG: path (M L H V C S Q T Z, no arcs), line, polyline, polygon, rect, circle.
Anything else, or any transform, is an error unless --ignore-unsupported.
"""
import argparse
import datetime
import json
import math
import pathlib
import re
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
MAX_SPEED = 40.0        # mm/s hard ceiling
CHUNK_STEPS = 3000      # longest single SM leg (per vector), keeps any in-flight move < ~3 s
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

    polys, skipped = [], {}

    def walk(el):
        tag = el.tag.split("}")[-1]
        if tag in ("defs", "metadata", "title", "desc", "style", "namedview"):
            return
        t = (el.get("transform") or "").strip()
        if t and t not in ("none",):
            raise ValueError(f"<{tag}> has a transform ({t!r}); flatten the SVG first (e.g. vpype)")
        f = lambda k, dflt=0.0: float(el.get(k, dflt))  # noqa: E731
        if tag == "path":
            polys.extend(parse_path(el.get("d", "")))
        elif tag == "line":
            polys.append([(f("x1"), f("y1")), (f("x2"), f("y2"))])
        elif tag in ("polyline", "polygon"):
            nums = [float(v) for v in re.findall(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?", el.get("points", ""))]
            pts = list(zip(nums[0::2], nums[1::2]))
            if tag == "polygon" and pts:
                pts.append(pts[0])
            if len(pts) >= 2:
                polys.append(pts)
        elif tag == "rect":
            x0, y0, w, h = f("x"), f("y"), f("width"), f("height")
            polys.append([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)])
        elif tag == "circle":
            cx, cy, r = f("cx"), f("cy"), f("r")
            polys.append([(cx + r * math.cos(2 * math.pi * k / 72), cy + r * math.sin(2 * math.pi * k / 72)) for k in range(73)])
        elif tag in ("g", "svg", "a"):
            pass
        else:
            skipped[tag] = skipped.get(tag, 0) + 1
        for c in el:
            walk(c)

    walk(root)
    if skipped:
        msg = f"unsupported elements: {skipped}"
        if not ignore_unsupported:
            raise ValueError(msg + " (use --ignore-unsupported to skip them)")
        log("WARNING: " + msg + " -- skipped")
    mm = [[((px - vx) * scale, (py - vy) * scale) for px, py in p] for p in polys]
    return mm, (vw * scale, vh * scale)


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
            d = math.hypot(px - ax, py - ay) if L == 0 else abs(dy * (px - ax) - dx * (py - ay)) / L
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def prepare(polys, eps, join_tol, scale, dx, dy):
    """scale/offset to mm, join touching strokes, simplify."""
    sp = [[(x * scale + dx, y * scale + dy) for x, y in p] for p in polys]
    merged = []
    for p in sp:
        if merged and math.dist(merged[-1][-1], p[0]) <= join_tol:
            merged[-1].extend(p[1:])
        else:
            merged.append(list(p))
    return [rdp(p, eps) for p in merged if len(p) >= 2]


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

    def setup(self):
        s = self.cfg["servo"]
        self.cmd("EM,1,1")
        self.cmd(f"SC,4,{s['pen_up']}")
        self.cmd(f"SC,5,{s['pen_down']}")
        self.pen(True)

    def pen(self, up, delay=250):
        self.idle()
        self.cmd(f"SP,{1 if up else 0},{delay}")
        self.sleep(delay / 1000 + 0.05)
        self.t_end = max(self.t_end, self.now())

    def shutdown(self):
        try:
            self.cmd("SP,1,300")
            self.sleep(0.4)
            self.cmd("EM,0,0")
        except Exception as ex:   # noqa: BLE001
            log(f"shutdown problem: {ex!r}")

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
        n = max(1, math.ceil(math.hypot(dx, dy) / CHUNK_STEPS))
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
    ap.add_argument("--start-x", type=float, default=20.0, help="S distance from machine left edge, mm")
    ap.add_argument("--start-y", type=float, default=20.0, help="S distance from machine top edge, mm")
    ap.add_argument("--speed", type=float, default=15.0, help="drawing speed mm/s")
    ap.add_argument("--travel-speed", type=float, default=25.0, help="pen-up speed mm/s")
    ap.add_argument("--simplify", type=float, default=0.05, help="RDP tolerance mm")
    ap.add_argument("--join", type=float, default=0.1, help="join strokes whose ends are within this many mm")
    ap.add_argument("--pen-delay", type=int, default=250, help="ms to wait after each pen up/down")
    ap.add_argument("--pen-down", type=int, help="override servo pen-down value")
    ap.add_argument("--pen-up", type=int, help="override servo pen-up value")
    ap.add_argument("--ignore-unsupported", action="store_true")
    a = ap.parse_args()

    if not (0 < a.speed <= MAX_SPEED and 0 < a.travel_speed <= MAX_SPEED):
        sys.exit(f"speeds must be in (0, {MAX_SPEED}] mm/s")
    cfg = json.loads(pathlib.Path(a.config).read_text())
    if a.pen_down:
        cfg["servo"]["pen_down"] = a.pen_down
    if a.pen_up:
        cfg["servo"]["pen_up"] = a.pen_up
    for k in ("pen_up", "pen_down"):
        if not 7500 <= cfg["servo"][k] <= 22000:
            sys.exit(f"servo {k}={cfg['servo'][k]} outside 7500..22000")
    spmm = (cfg["steps_per_mm"]["x"], cfg["steps_per_mm"]["y"])

    OUT.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    LOGF = open(OUT / f"plot_{stamp}.log", "w")
    log(f"mode={a.mode} sim={a.sim} svg={a.svg}")

    polys, (w_mm, h_mm) = load_svg(a.svg, a.ignore_unsupported)
    scale = a.scale * (a.width / w_mm if a.width else 1.0)
    strokes = prepare(polys, a.simplify, a.join, scale, 0.0, 0.0)
    if not strokes:
        sys.exit("no drawable geometry found")
    x0, y0, x1, y1 = bbox(strokes)
    # offsets are applied to the placement, not the geometry, so the drawing's own origin stays (0,0)
    ox, oy = a.x0, a.y0
    gx0, gy0, gx1, gy1 = x0 + ox, y0 + oy, x1 + ox, y1 + oy
    mx, my = cfg["travel_mm"]["x"], cfg["travel_mm"]["y"]
    abs_x0, abs_y0 = a.start_x + gx0, a.start_y + gy0
    abs_x1, abs_y1 = a.start_x + gx1, a.start_y + gy1
    pts = sum(len(s) for s in strokes)
    dd, dt, lifts, secs = estimate([[(x + ox, y + oy) for x, y in s] for s in strokes], spmm, a.speed, a.travel_speed,
                                   (a.pen_delay / 1000 + 0.15))
    problems = []
    if abs_x0 < EDGE_MARGIN or abs_y0 < EDGE_MARGIN:
        problems.append(f"drawing starts at machine ({abs_x0:.1f},{abs_y0:.1f}) mm, inside the {EDGE_MARGIN} mm edge margin")
    if abs_x1 > mx - EDGE_MARGIN or abs_y1 > my - EDGE_MARGIN:
        problems.append(f"drawing reaches machine ({abs_x1:.1f},{abs_y1:.1f}) mm; travel is {mx} x {my} mm (margin {EDGE_MARGIN})")
    log(f"""
Drawing        : {w_mm * scale:.1f} x {h_mm * scale:.1f} mm ({len(polys)} source paths -> {len(strokes)} strokes, {pts} points after simplify {a.simplify} mm)
Geometry bbox  : x {gx0:.1f}..{gx1:.1f}  y {gy0:.1f}..{gy1:.1f} mm relative to S
On the machine : x {abs_x0:.1f}..{abs_x1:.1f} of {mx}   y {abs_y0:.1f}..{abs_y1:.1f} of {my} mm  (S at {a.start_x},{a.start_y})
Steps          : x {round(gx0 * spmm[0])}..{round(gx1 * spmm[0])}  y {round(gy0 * spmm[1])}..{round(gy1 * spmm[1])}
Drawing length : {dd / 1000:.2f} m at {a.speed} mm/s    pen-up travel {dt / 1000:.2f} m at {a.travel_speed} mm/s    pen lifts {lifts}
Estimated time : {fmt(secs)}
Servo          : up {cfg['servo']['pen_up']}  down {cfg['servo']['pen_down']}""")
    for p in problems:
        log("PROBLEM: " + p)
    summary = {"svg": a.svg, "mode": a.mode, "strokes": len(strokes), "points": pts, "est_seconds": secs,
               "bbox_machine_mm": [abs_x0, abs_y0, abs_x1, abs_y1], "problems": problems, "args": vars(a)}

    if a.mode == "dry":
        (OUT / f"plot_{stamp}.json").write_text(json.dumps(summary, indent=2))
        log("Dry run only. Nothing was sent." + ("" if not problems else "  FIX THE PROBLEMS ABOVE before trace/plot."))
        return 1 if problems else 0
    if problems:
        sys.exit("refusing to move: fix the problems above (adjust --width/--x0/--y0/--start-x/--start-y)")

    if not a.sim:
        print("\nCheck: motors off, carriage by hand at S, pen UP over the paper, power switch in reach.")
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
            for i, s in enumerate(strokes, 1):
                pts_s = [to_steps((x + ox, y + oy), spmm) for x, y in s]
                p.move_to(*pts_s[0], a.travel_speed)
                p.pen(False, a.pen_delay)
                for tx, ty in pts_s[1:]:
                    p.move_to(tx, ty, a.speed)
                p.pen(True, a.pen_delay)
                if i / n >= next_report:
                    el = p.now() - t_start
                    log(f"{i}/{n} strokes  {100 * i / n:.0f}%  elapsed {fmt(el)}  eta {fmt(el / (i / n) - el)}")
                    next_report += 0.05
            p.move_to(0, 0, a.travel_speed)
            p.idle()
    except KeyboardInterrupt:
        status = "interrupted"
        log("Interrupted: raising pen, releasing motors. Position is now unknown.")
    except Exception as ex:   # noqa: BLE001
        status = f"error: {ex!r}"
        log(f"ERROR: {ex!r}")
    finally:
        p.shutdown()
        el = p.now() - t_start
        summary.update(status=status, elapsed_s=el, sm_commands=p.nsm, net_ebb_steps_sent=p.sent,
                       final_world_steps=p.pos)
        (OUT / f"plot_{stamp}.json").write_text(json.dumps(summary, indent=2))
        log(f"\nStatus: {status}   elapsed {fmt(el)}   SM commands {p.nsm}   net steps sent {p.sent}   end position {p.pos}")
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
