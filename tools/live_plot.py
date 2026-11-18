#!/usr/bin/env python3
"""Live handwriting: type a line, the plotter writes it in your hand.

  live_plot.py --sim                          simulate; type lines, Enter writes each one
  live_plot.py --text "Hello there"           write this text and finish
  live_plot.py --file letter.txt              write a file, then finish
  live_plot.py --font "Comic Neue" ...        a standard font instead of your hand (--list-fonts shows them)
  live_plot.py                                real plotter (asks for WRITE first)

Unlike plot_svg.py this process stays alive between lines, so the tracked carriage position carries over. Start it with
the carriage parked BY HAND at the paper's top-left (S), motors off, exactly as for a plot. Nothing can be read back from
the firmware: after any stop or crash the position is unknown and the carriage must be parked again.

Commands (typed on a line of their own):   :newpage  park for a paper change, then :go when the paper is changed
                                           :quit  finish (pen up, return to S)
An empty line leaves a blank line. Ctrl-C raises the pen and releases the motors. --json prints one JSON event per line
(session, line_start, line_done, page_full, error, done) for a UI; --track also emits the __POS__ lines the web tracker reads.
Every written line is saved with its strokes in results/live_<time>.json so a session can be replotted.

--control is the web server's mode: one command per stdin line, TEXT <text> (literal text, written as one or more lines),
NEWPAGE (then nothing is drawn until CONTINUE), QUIT,
STOP (honoured before the next move is sent), and WRITE as the first line to confirm a real run. End of input without a
QUIT counts as STOP.
It implies --json and --track, and line_done events carry the strokes in machine mm.

Safety is plot_svg's: drawing speed <= 40 mm/s, pen-up <= 200 mm/s, pressure 0..0.9, every stroke inside the canvas
(the paper minus its margin) and the machine travel, nothing off the paper. A line that would leave the canvas is refused whole.
"""
import argparse
import collections
import datetime
import json
import pathlib
import queue
import signal
import sys
import threading
import types

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "handwriting"))
import plot_svg as ps  # noqa: E402
from engine import ReplayEngine  # noqa: E402
from mirror import Mirror  # noqa: E402


ASCENT_EM, DESCENT_EM, LINE_PITCH_EM = 2.0, 0.8, 3.5   # in x-heights, as on the capture page


class Session:
    def __init__(self, a, cfg, engine):
        self.a, self.cfg, self.engine = a, cfg, engine
        mx, my = cfg["travel_mm"]["x"], cfg["travel_mm"]["y"]
        self.spmm = (cfg["steps_per_mm"]["x"], cfg["steps_per_mm"]["y"])
        paper = cfg.get("paper") or {}
        self.px0 = float(paper.get("x0", 0.0))
        self.py0 = float(paper.get("y0", 0.0))
        pw, ph = float(paper.get("width", mx)), float(paper.get("height", my))
        m = max(0.0, float(paper.get("margin_mm", 0.0)))
        self.cx0, self.cy0, self.cx1, self.cy1 = self.px0 + m, self.py0 + m, self.px0 + pw - m, self.py0 + ph - m
        if self.cx1 <= self.cx0 or self.cy1 <= self.cy0:
            sys.exit("paper margin leaves no reachable canvas")
        if self.cx0 < ps.EDGE_MARGIN or self.cy0 < ps.EDGE_MARGIN or self.cx1 > mx - ps.EDGE_MARGIN or self.cy1 > my - ps.EDGE_MARGIN:
            sys.exit(f"the canvas ({self.cx0:.0f}..{self.cx1:.0f} x {self.cy0:.0f}..{self.cy1:.0f} mm) is outside the machine "
                     f"travel {mx} x {my} mm with its {ps.EDGE_MARGIN} mm edge margin")
        self.sx, self.sy = self.px0, self.py0        # S: the carriage is parked at the paper's top-left
        if not (0 <= self.sx <= mx and 0 <= self.sy <= my):
            sys.exit(f"carriage start S=({self.sx:.1f},{self.sy:.1f}) mm is outside the machine travel {mx} x {my} mm")
        self.xh = a.xheight
        self.pitch = a.line_pitch or LINE_PITCH_EM * self.xh
        self.width = self.cx1 - self.cx0
        self.baseline = self.cy0 + ASCENT_EM * self.xh   # machine y of the current line's baseline
        self.page, self.lines_done, self.log = 1, [], []
        self.plotter = None

    # ---- events
    def emit(self, event, **kw):
        if self.a.json:
            print(json.dumps({"event": event, **kw}), flush=True)
        else:
            detail = ", ".join(f"{k}={v}" for k, v in kw.items())
            print(f"[{event}] {detail}" if detail else f"[{event}]", flush=True)

    def fits(self):
        return self.baseline + DESCENT_EM * self.xh <= self.cy1

    def steps(self, x, y):
        return ps.to_steps((x - self.sx, y - self.sy), self.spmm)

    # ---- writing
    def check_line(self, lines):
        """Refuse the whole text if any point leaves the canvas; returns an error string or None."""
        y = self.baseline
        for ln in lines:
            for st in ln:
                for px, py in st["pts"]:
                    x, yy = self.cx0 + px, y + py
                    if not (self.cx0 - 0.01 <= x <= self.cx1 + 0.01 and self.cy0 - 0.01 <= yy <= self.cy1 + 0.01):
                        return f"a stroke would leave the canvas at ({x:.1f}, {yy:.1f}) mm"
            y += self.pitch
        return None

    def write_text(self, text):
        try:
            lines = self.engine.layout(text, self.xh, self.width)
        except ValueError as ex:
            self.emit("error", message=str(ex))
            return False
        missing = sorted(self.engine.missing)
        if missing:
            self.emit("warning", message="no sample for: " + " ".join(missing) + " (skipped)")
            self.engine.missing.clear()
        if not lines:
            self.emit("error", message="nothing to write: no letters of this line are in the samples")
            return False
        # all wrapped lines must fit on this page: otherwise nothing is drawn and the page is reported full
        need = self.baseline + (len(lines) - 1) * self.pitch + DESCENT_EM * self.xh
        if need > self.cy1:
            self.emit("page_full", page=self.page, need_mm=round(need, 1), limit_mm=round(self.cy1, 1))
            return False
        err = self.check_line(lines)
        if err:
            self.emit("error", message=err)
            return False
        for ln in lines:
            self.draw_line(ln, text)
            self.baseline += self.pitch
        return True

    def draw_line(self, strokes, text):
        p, a = self.plotter, self.a
        self.emit("line_start", page=self.page, baseline_mm=round(self.baseline, 1), strokes=len(strokes))
        saved = []
        for st in strokes:
            pts = [(self.cx0 + x, self.baseline + y) for x, y in st["pts"]]
            sp = [self.steps(x, y) for x, y in pts]
            p.move_to(*sp[0], a.travel_speed)
            p.set_pressure(a.pressure if a.pressure is not None else st["p"])
            p.pen(False, a.pen_delay)
            for t in sp[1:]:
                p.move_to(*t, a.speed)
            p.pen(True, a.pen_delay)
            saved.append({"p": st["p"], "pts": [[round(x, 2), round(y, 2)] for x, y in pts]})
        self.lines_done.append({"page": self.page, "text": text, "baseline_mm": round(self.baseline, 2), "strokes": saved})
        if len(self.lines_done) > 200:
            self.lines_done[-201]["strokes"] = None     # keep the text of every line but the strokes of the last 200
        extra = {"strokes": saved} if self.a.control else {}
        self.emit("line_done", page=self.page, text=text, **extra)

    def new_page(self):
        p = self.plotter
        p.move_to(0, 0, self.a.travel_speed)
        p.idle()
        self.page += 1
        self.baseline = self.cy0 + ASCENT_EM * self.xh
        self.emit("new_page", page=self.page, hold=True,
                  message="pen up at S; change the paper without moving the carriage, then press Continue")


class StopFlag:
    """The Plotter's ctl hook: nothing new is sent to the firmware once STOP is in."""

    def __init__(self):
        self.stop = threading.Event()

    def check_stop(self):
        if self.stop.is_set():
            raise KeyboardInterrupt()


MAX_PENDING = 50      # typed-ahead items waiting; more are dropped with an error (STOP is never queued behind them)
MAX_PENDING_POINTS = 20000   # mirror points waiting; more are dropped
MAX_BATCH_POINTS = 2000
MERGE_POINTS = 400            # moves merged into one go (keeps simplifying short, so Stop is prompt)
IDLE_END_S = 3.0              # a stroke left open this long with nothing arriving is ended: pen up


class Pending:
    """Points waiting in the queue (reader adds, the main loop subtracts)."""

    def __init__(self):
        self.n = 0
        self.lock = threading.Lock()

    def add(self, k):
        with self.lock:
            self.n += k

    def get(self):
        with self.lock:
            return self.n


def parse_points(text):
    """JSON [[x, y, t], ...] -> list of (x, y, t) floats, or None if anything is wrong."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, list) or not 0 < len(raw) <= MAX_BATCH_POINTS:
        return None
    out = []
    for pt in raw:
        if not isinstance(pt, list) or len(pt) not in (2, 3):
            return None
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e7 for v in pt):
            return None
        out.append((float(pt[0]), float(pt[1]), float(pt[2]) if len(pt) == 3 else 0.0))
    return out


def control_lines(q, flag, pending):
    """--control: TEXT/NEWPAGE/CONTINUE/QUIT/STOP/WRITE commands -> typed queue items. TEXT stays literal text:
    ':newpage', 'WRITE' and the like typed as text are just letters."""
    quit_seen = False
    try:
        for raw in sys.stdin:
            ln = raw.rstrip(chr(13) + chr(10))
            word, _, rest = ln.partition(" ")
            word = word.upper()
            if word == "STOP":
                flag.stop.set()
                q.put(("stop",))            # also wakes a main loop that is waiting for input
            elif word == "TEXT":
                if q.qsize() >= MAX_PENDING:
                    print(json.dumps({"event": "error", "message": "too many lines waiting; this one was dropped"}), flush=True)
                else:
                    q.put(("text", rest[:500]))
            elif word == "NEWPAGE":
                q.put(("newpage",))
            elif word == "CONTINUE":
                q.put(("continue",))
            elif word == "QUIT":
                quit_seen = True
                q.put(("quit",))
            elif word == "WRITE":
                q.put(("write",))
            elif word == "MIRROR":
                phase, _, body = rest.partition(" ")
                pts = parse_points(body) if phase in ("start", "move", "end", "stroke") else None
                if pts is None:
                    print(json.dumps({"event": "error", "message": "bad mirror message ignored"}), flush=True)
                elif pending.get() + len(pts) > MAX_PENDING_POINTS and phase != "end":
                    print(json.dumps({"event": "error", "message": "too many points waiting; some were dropped"}), flush=True)
                else:
                    if phase == "end" and pending.get() + len(pts) > MAX_PENDING_POINTS:
                        pts = pts[-1:]          # over budget: the end only needs its last point
                    pending.add(len(pts))
                    q.put(("mirror", phase, pts))
    finally:
        if not quit_seen:
            flag.stop.set()     # the web server went away without finishing the session: stop
        q.put(("stop",) if not quit_seen else ("eof",))


def stdin_lines(q):
    """Interactive input: :newpage, :go (paper changed, carry on) and :quit are commands; any other line is text."""
    for ln in sys.stdin:
        ln = ln.rstrip(chr(13) + chr(10))
        cmd = ln.strip()
        if cmd == ":newpage":
            q.put(("newpage",))
        elif cmd == ":go":
            q.put(("continue",))
        elif cmd in (":q", ":quit"):
            q.put(("quit",))
        else:
            q.put(("text", ln))
    q.put(("eof",))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="virtual machine and clock, no hardware")
    ap.add_argument("--port")
    ap.add_argument("--config", default=str(ROOT / "config" / "machine.json"))
    ap.add_argument("--writer", default="default", help="whose handwriting to write in (see tools/handwriting/writers.py list)")
    ap.add_argument("--samples", help="samples directory (default: the writer's own)")
    ap.add_argument("--prompts", default=str(ROOT / "handwriting" / "prompts.json"))
    ap.add_argument("--font", help="write in a standard font (family name such as 'Comic Neue', or a .ttf/.otf path) "
                                   "instead of your handwriting; outlines are drawn as contours")
    ap.add_argument("--list-fonts", action="store_true", help="print the available font families and exit")
    ap.add_argument("--xheight", type=float, default=4.5, help="x-height in mm (default %(default)s; the pen's play limits small loops)")
    ap.add_argument("--line-pitch", type=float, help="distance between baselines, mm (default 3.5 x-heights)")
    ap.add_argument("--speed", type=float, default=15.0, help="drawing speed mm/s")
    ap.add_argument("--travel-speed", type=float, default=100.0, help="pen-up speed mm/s")
    ap.add_argument("--pressure", type=float, help="fixed pen pressure 0..0.9 (0 = configured pen-down); default 0")
    ap.add_argument("--sample-pressure", action="store_true", help="map the pen pressure of the samples onto the plotter's")
    ap.add_argument("--pen-delay", type=int, default=250)
    ap.add_argument("--text", help="write this text and finish")
    ap.add_argument("--file", help="write this file (one line per line) and finish")
    ap.add_argument("--json", action="store_true", help="print JSON events")
    ap.add_argument("--track", action="store_true", help="emit __POS__ lines for the web tracker")
    ap.add_argument("--control", action="store_true", help="web server mode: commands on stdin (see above)")
    ap.add_argument("--sport", action="store_true",
                    help="mirror mode follows the speed of the hand, up to 100 mm/s: above the tested ceiling, steps may be lost")
    a = ap.parse_args()
    if not a.samples:
        import writers
        try:
            a.samples = str(writers.samples_dir(a.writer))
        except ValueError as ex:
            sys.exit(str(ex))
    if a.control:
        a.json = a.track = True
    if a.sport and not a.control:
        sys.exit("--sport is only for the web mirror mode (--control)")

    if a.list_fonts:
        import fonts
        print(chr(10).join(fonts.families()))
        return 0
    if not (0 < a.speed <= ps.MAX_SPEED and 0 < a.travel_speed <= ps.MAX_TRAVEL_SPEED):
        sys.exit(f"drawing speed must be in (0, {ps.MAX_SPEED}] mm/s, travel speed in (0, {ps.MAX_TRAVEL_SPEED}] mm/s")
    if a.pressure is not None and not 0.0 <= a.pressure <= ps.MAX_PRESSURE:
        sys.exit(f"--pressure must be in 0..{ps.MAX_PRESSURE}")
    if not 2.0 <= a.xheight <= 20.0:
        sys.exit("--xheight must be 2..20 mm")
    cfg = json.loads(pathlib.Path(a.config).read_text())
    for k in ("pen_up", "pen_down"):
        if not 7500 <= cfg["servo"][k] <= 22000:
            sys.exit(f"servo {k}={cfg['servo'][k]} outside 7500..22000")
    axes = [cfg["axis"][k] for k in ("x", "y")]
    if sorted(ax["ebb_axis"] for ax in axes) != [1, 2] or any(ax["sign"] not in (1, -1) for ax in axes):
        sys.exit("config axis: x and y must map to distinct EBB axes 1 and 2, each with sign 1 or -1")
    if min(cfg["steps_per_mm"].values()) <= 0:
        sys.exit("config steps_per_mm must be positive")

    fixed = a.pressure if a.pressure is not None else 0.0
    if a.font:
        from fontengine import FontEngine
        isfile = pathlib.Path(a.font).suffix.lower() in (".ttf", ".otf")
        try:
            engine = FontEngine(None if isfile else a.font, a.font if isfile else None, fixed_pressure=fixed)
        except ValueError as ex:
            sys.exit(str(ex))
    else:
        engine = ReplayEngine(a.samples, a.prompts, sample_pressure=a.sample_pressure, fixed_pressure=fixed)
    s = Session(a, cfg, engine)
    s.emit("session", sim=a.sim, canvas_mm=[s.cx0, s.cy0, s.cx1, s.cy1], start_mm=[s.sx, s.sy],
           xheight_mm=s.xh, font=a.font,
           words=len(getattr(engine, "words", ())), glyphs=len(getattr(engine, "glyphs", ())))

    lines_in = queue.Queue()
    flag = StopFlag()
    pending = Pending()
    if a.text is not None:
        for ln in a.text.split("\n"):
            lines_in.put(("text", ln))
        lines_in.put(("eof",))
    elif a.file:
        for ln in pathlib.Path(a.file).read_text(encoding="utf-8").splitlines():
            lines_in.put(("text", ln))
        lines_in.put(("eof",))
    elif a.control:
        threading.Thread(target=control_lines, args=(lines_in, flag, pending), daemon=True).start()
    else:
        threading.Thread(target=stdin_lines, args=(lines_in,), daemon=True).start()

    if not a.sim:
        print(f"\nCheck: motors off, carriage by hand at the paper's top-left ({s.sx:.1f},{s.sy:.1f}) mm, "
              "pen UP over the paper, power switch in reach.", flush=True)
        if a.control:
            got = "WRITE" if lines_in.get() == ("write",) else ""
        else:
            try:
                got = input("Type WRITE to start: ")
            except EOFError:
                got = ""
        if got.strip() != "WRITE":
            sys.exit("not confirmed, nothing sent")

    pargs = types.SimpleNamespace(sim=a.sim, port=a.port, track=a.track, start_x=s.sx, start_y=s.sy,
                                  ctl=flag if a.control else None, sim_speed=0)
    p = ps.Plotter(cfg, pargs)
    s.plotter = p
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    status = "complete"
    try:
        p.setup()
        holding, held = False, []      # after NEWPAGE nothing is drawn until the paper has been changed (CONTINUE)

        def handle(item):
            nonlocal holding
            if item[0] == "newpage" and a.sport:
                s.emit("error", message="New page is not available in sport mode: the position cannot be trusted.")
            elif item[0] == "newpage":
                s.new_page()
                holding = True
            elif item[0] == "text":
                ln = item[1]
                if not ln.strip():
                    s.baseline += s.pitch
                    if not s.fits():
                        s.emit("page_full", page=s.page)
                else:
                    s.write_text(ln)

        mirror = Mirror(s)
        peeked = collections.deque()      # items read ahead while merging a backlog, put back in order
        last_status = [0.0]

        def next_item():
            if peeked:
                return peeked.popleft()
            while True:
                try:
                    return lines_in.get(timeout=IDLE_END_S if mirror.open else None)
                except queue.Empty:
                    flag.check_stop()
                    mirror.end()            # the pad went quiet with the pen down
                    s.emit("warning", message="A stroke was left open and was ended.")

        def report():
            import time as _t
            now = _t.monotonic()
            if now - last_status[0] >= 0.3:
                last_status[0] = now
                s.emit("mirror_status", pending_points=pending.get(), open=mirror.open, sport=a.sport)

        def handle_mirror(item):
            phase, pts = item[1], item[2]
            pending.add(-len(pts))
            if phase == "move":
                # merge the moves of this stroke that are already waiting: a backlog is drawn in one go
                while True:
                    try:
                        nxt = peeked.popleft() if peeked else lines_in.get_nowait()
                    except queue.Empty:
                        break
                    if nxt[0] == "mirror" and nxt[1] in ("move", "end") and (nxt[1] == "end" or len(pts) + len(nxt[2]) <= MERGE_POINTS):
                        pending.add(-len(nxt[2]))
                        pts = pts + nxt[2]
                        if nxt[1] == "end":
                            phase = "end"
                            break
                    else:
                        peeked.appendleft(nxt)      # not part of this stroke: next, in its original place
                        break
            mirror.phase(phase, pts)
            report()

        while True:
            flag.check_stop()
            item = next_item()
            flag.check_stop()
            kind = item[0]
            if kind == "stop":
                raise KeyboardInterrupt()
            if kind in ("quit", "eof"):
                mirror.end()
                break
            if kind == "mirror":
                if holding:
                    pending.add(-len(item[2]))      # the paper is being changed: nothing is drawn
                    continue
                handle_mirror(item)
                continue
            mirror.end()                            # anything else after a stroke that never ended: pen up first
            if kind == "continue":
                if holding:
                    holding = False
                    s.emit("resumed", page=s.page)
                    queued, held = held, []
                    for it in queued:
                        flag.check_stop()
                        handle(it)
                continue
            if kind in ("text", "newpage"):
                if holding:
                    held.append(item)
                else:
                    handle(item)
        if not a.sport:                 # sport mode may have lost steps: no automatic trip back to S
            p.move_to(0, 0, a.travel_speed)
        p.idle()
    except KeyboardInterrupt:
        status = "interrupted"
        s.emit("error", message="Interrupted: raising the pen, then releasing the motors. The position is now unknown; park again.")
    except Exception as ex:   # noqa: BLE001
        status = f"error: {ex!r}"
        s.emit("error", message=repr(ex))
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
        if not p.shutdown():
            status += " (cleanup FAILED: pen may be down or motors still on, check the machine and cut power if needed)"
        out = ROOT / "results"
        out.mkdir(exist_ok=True)
        f = out / f"live_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        f.write_text(json.dumps({"status": status, "xheight_mm": s.xh, "start_mm": [s.sx, s.sy], "args": vars(a),
                                 "end_world_steps": p.pos, "lines": s.lines_done}, indent=1))
        if a.track:
            end = [round(s.sx + p.pos[0] / s.spmm[0], 2), round(s.sy + p.pos[1] / s.spmm[1], 2)]
            print("__RESULT__:" + json.dumps({
                "status": status, "elapsed_s": 0, "end_mm": end,
                "at_start": p.pos == [0, 0] and not status.startswith(("interrupted", "error")) and "FAILED" not in status}),
                flush=True)
        s.emit("done", status=status, lines=len(s.lines_done), end_world_steps=p.pos, saved=str(f))
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
