"""Mirror mode: the plotter follows a pen on a tablet or mouse. Used by live_plot.py.

Two ways to feed it (both can be mixed in one session):
  stroke   a whole stroke is sent after the pen lifts; the plotter then draws it
  point    the stroke is streamed while it is being written: pen down at the first point, a move to every new point,
           pen up at the end. The plotter lags behind a hand (it is slower), so points that queue up are merged.

Points are machine mm, [x, y, t_ms]. Every point is clamped into the canvas, so the pen can never leave the paper.

Speeds: normally the session's drawing speed (never above 40 mm/s). Sport mode (--sport) follows the speed of the hand
instead, up to SPORT_MAX mm/s, and catches up on a backlog within about SPORT_CATCHUP s. It is not meant for regular use:
it is above the tested ceiling, so the motors can lose steps and the position is not to be trusted afterwards.
"""
import math

SPORT_MAX = 100.0       # mm/s, pen down, sport mode only
SPORT_MIN = 5.0
SPORT_CATCHUP = 0.4     # s: a backlog is drawn within about this long (up to SPORT_MAX)
SPORT_PEN_DELAY = 100   # ms for each pen up/down in sport mode
MAX_HISTORY = 20000      # points kept of one stroke for the display; more are thinned
MIN_STEP_MM = 0.25      # points closer than this to the previous one are dropped
SIMPLIFY_MM = 0.08


def _length(pts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


class Mirror:
    def __init__(self, session):
        self.s = session
        self.open = False
        self.cur = []          # points of the stroke being drawn (x, y)
        self.last = None       # last point sent to the plotter (x, y)
        self.last_t = 0.0      # its time (ms)
        self.strokes_done = 0

    # ---- helpers
    def _clamp(self, pt):
        s = self.s
        x = min(max(pt[0], s.cx0), s.cx1)
        y = min(max(pt[1], s.cy0), s.cy1)
        t = pt[2] if len(pt) > 2 else 0.0
        return (x, y, t)

    def _prep(self, pts):
        """Clamp, drop points that barely moved, simplify. Keeps the last point."""
        out = []
        prev = self.last
        for pt in pts:
            x, y, t = self._clamp(pt)
            if prev is not None and math.hypot(x - prev[0], y - prev[1]) < MIN_STEP_MM:
                continue
            out.append((x, y, t))
            prev = (x, y)
        if len(out) > 2:
            xy = [(q[0], q[1]) for q in out]
            idx = _rdp_indices(xy, SIMPLIFY_MM)
            out = [out[i] for i in idx]
        return out

    def speed(self, pts):
        a = self.s.a
        if not a.sport:
            return a.speed
        length = _length(pts)
        dt = (pts[-1][2] - pts[0][2]) / 1000.0 if len(pts) > 1 else 0.0
        v_in = length / dt if dt > 0 else a.speed
        return min(SPORT_MAX, max(SPORT_MIN, v_in, length / SPORT_CATCHUP))

    def _delay(self):
        a = self.s.a
        return min(a.pen_delay, SPORT_PEN_DELAY) if a.sport else a.pen_delay

    # ---- stroke life cycle
    def begin(self, pt):
        s, p, a = self.s, self.s.plotter, self.s.a
        x, y, t = self._clamp(pt)
        p.move_to(*s.steps(x, y), a.travel_speed)
        p.set_pressure(a.pressure if a.pressure is not None else s.engine.fixed_pressure)
        p.pen(False, self._delay())
        self.open = True
        self.cur = [(x, y)]
        self.last = (x, y)
        self.last_t = t

    def move(self, pts):
        if not pts:
            return
        if not self.open:
            self.begin(pts[0])
            pts = pts[1:]
        pts = self._prep(pts)
        if not pts:
            return
        v = self.speed([(self.last[0], self.last[1], self.last_t)] + pts)
        p, s = self.s.plotter, self.s
        for x, y, t in pts:
            p.move_to(*s.steps(x, y), v)
            self.cur.append((x, y))
            self.last = (x, y)
            self.last_t = t
        if len(self.cur) > MAX_HISTORY:
            self.cur = self.cur[::2] + ([self.cur[-1]] if len(self.cur) % 2 == 0 else [])

    def end(self, pts=()):
        s, p = self.s, self.s.plotter
        if not self.open:
            return
        self.move(list(pts))
        if len(self.cur) == 1:                      # a dot: a tiny mark
            x, y = self.cur[0]
            x2, y2, _ = self._clamp((x + 0.2, y, 0))
            p.move_to(*s.steps(x2, y2), self.s.a.speed)
            self.cur.append((x2, y2))
        p.pen(True, self._delay())
        stroke = {"p": self.s.a.pressure if self.s.a.pressure is not None else s.engine.fixed_pressure,
                  "pts": [[round(x, 2), round(y, 2)] for x, y in self.cur]}
        self.open = False
        self.cur = []
        self.strokes_done += 1
        s.lines_done.append({"page": s.page, "mirror": True, "strokes": [stroke]})
        if len(s.lines_done) > 200:
            s.lines_done[-201]["strokes"] = None
        s.emit("mirror_stroke", page=s.page, strokes=[stroke], total=self.strokes_done)

    def stroke(self, pts):
        """A whole stroke at once."""
        if not pts:
            return
        if self.open:
            self.end()
        self.begin(pts[0])
        self.end(pts[1:])

    def phase(self, phase, pts):
        if phase == "start":
            if self.open:
                self.end()
            if pts:
                self.begin(pts[0])
                self.move(pts[1:])
        elif phase == "move":
            self.move(pts)
        elif phase == "end":
            self.end(pts)
        elif phase == "stroke":
            self.stroke(pts)


def _rdp_indices(pts, eps):
    """Indices of the points kept by Ramer-Douglas-Peucker (ends always kept)."""
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        (x1, y1), (x2, y2) = pts[i], pts[j]
        dx, dy = x2 - x1, y2 - y1
        n = dx * dx + dy * dy
        best, bi = -1.0, i
        for k in range(i + 1, j):
            px, py = pts[k]
            if n == 0:
                d = math.hypot(px - x1, py - y1)
            else:
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / n))
                d = math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
            if d > best:
                best, bi = d, k
        if best > eps:
            keep[bi] = True
            stack += [(i, bi), (bi, j)]
    return [i for i, k in enumerate(keep) if k]
