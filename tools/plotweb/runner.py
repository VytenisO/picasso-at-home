"""One job subprocess at a time: reservation, launch, output parsing, control commands, final state.

States: idle -> starting -> running <-> pausing -> paused -> running ... -> stopping | returning
        -> complete | stopped | interrupted | error
A job is reserved ("starting") before any hardware check or preflight, so Stop can cancel it before
anything is launched, and a second start is refused.

The carriage position the server reports is the commanded (estimated) one: firmware 2.0.1 has no position
readback. After a run the server records whether the carriage should be back at the start point S.
"""
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time

from . import hardware

ACTIVE = ("starting", "running", "pausing", "paused", "stopping", "returning")
STOP_GRACE_S = 30.0      # plot_svg needs at most one move chunk (3 s) plus pen-up and cleanup; this is generous
KILL_GRACE_S = 15.0
MIRROR_MAX_POINTS = 2000    # points in one pad message
LIVE_MAX_POINTS = 150000   # points of written strokes kept for the page view


def _new_job(job_id, kind, mode, sim, meta):
    return {"id": job_id, "kind": kind, "mode": mode, "sim": sim, "state": "starting", "meta": meta or {},
            "started": time.time(), "finished": None, "stop_requested": False,
            "progress": None, "layer": None, "pos": None, "pen": "up", "pause": None,
            "result": None, "message": "Preparing...", "plan_id": (meta or {}).get("plan_id"),
            "moved": False}


def _clean_points(pts):
    """Pad points [[x, y, t], ...] -> rounded lists, or None if the shape or any number is wrong."""
    if not isinstance(pts, list) or not 0 < len(pts) <= MIRROR_MAX_POINTS:
        return None
    out = []
    for pt in pts:
        if not isinstance(pt, (list, tuple)) or len(pt) not in (2, 3):
            return None
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e6 for v in pt):
            return None
        out.append([round(pt[0], 2), round(pt[1], 2)] + ([round(pt[2])] if len(pt) == 3 else []))
    return out


class JobRunner:
    def __init__(self, settings, events):
        self.s = settings
        self.ev = events
        self.lock = threading.RLock()
        self.job = None
        self.proc = None
        self.pause_seq = 0
        self.recovered = None         # Event while an escalated stop is still making the machine safe
        self.preview = None           # the immutable preview of the running plot job
        self.live_strokes = []        # strokes written on the current page of a live session (machine mm)
        self.live_points = 0
        self._wlock = threading.Lock()   # one write to the child's stdin at a time: STOP never lands inside another line
        self.live_sid = None          # id of the pad stroke that is open
        self.live_wq = None           # commands to a live session, written by a thread of their own
        self.position = {"state": "unknown", "at": None,
                         "note": "Park the carriage by hand at the paper's top-left before the first run."}
        self._last_state_emit = 0.0

    # ------------------------------------------------------------------ state
    def snapshot(self):
        with self.lock:
            job = json.loads(json.dumps(self.job)) if self.job else None
            return {"job": job, "position": dict(self.position), "busy": self.busy()}

    def busy(self):
        with self.lock:
            return bool(self.job and self.job["state"] in ACTIVE)

    def _emit_state(self, force=True):
        now = time.monotonic()
        if not force and now - self._last_state_emit < 0.3:
            return
        self._last_state_emit = now
        self.ev.emit("state", **self.snapshot())

    def _log(self, level, text, src="server"):
        self.ev.log(level, text, src)

    # ------------------------------------------------------------ lifecycle
    def reserve(self, kind, mode, sim, meta=None):
        """-> (True, job_id) or (False, reason). Reserving is atomic with the busy check."""
        with self.lock:
            if self.busy():
                j = self.job
                return False, f"A {j['kind']} job is already {j['state']}. Wait for it to finish or stop it first."
            job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(2).hex()
            self.job = _new_job(job_id, kind, mode, sim, meta)
            self.preview = None
            self.live_strokes = []
        self._log("info", f"Job {job_id}: {kind} ({'simulated' if sim else mode}) reserved, running checks.")
        self._emit_state()
        return True, job_id

    def cancel(self, job_id, message, level="error"):
        """Finish a reserved job that never launched."""
        with self.lock:
            if not self.job or self.job["id"] != job_id or self.job["state"] != "starting":
                return
            stopped = self.job["stop_requested"]
            self.job.update(state="stopped" if stopped else "error", finished=time.time(),
                            message="Stopped before anything moved." if stopped else message,
                            result={"status": "not started", "detail": message})
        self._log("info" if stopped else level, message)
        self._emit_state()

    def launch(self, job_id, argv, stdin_text=None, preview=None):
        """Start the subprocess for a reserved job. -> (ok, message)."""
        with self.lock:
            j = self.job
            if not j or j["id"] != job_id or j["state"] != "starting":
                return False, "The job is no longer waiting to start."
            if j["stop_requested"]:
                self.cancel(job_id, "Stopped before anything moved.", "info")
                return False, "Stopped before anything moved."
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            try:
                proc = subprocess.Popen(argv, cwd=str(self.s.root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, bufsize=0, env=env)
            except OSError as ex:
                self.cancel(job_id, f"Could not start the job process: {ex}")
                return False, str(ex)
            self.proc = proc
            self.preview = preview
            j.update(state="running", message="Running.", argv=[os.path.basename(a) if i < 2 else a
                                                                 for i, a in enumerate(argv)])
            if stdin_text:
                self._write(stdin_text)
            if j["kind"] in ("live", "machine"):
                self.live_wq = queue.Queue()      # bounded by hand in live_send (a stroke end is always accepted)
                self.live_sid = None
                threading.Thread(target=self._live_writer, args=(proc, self.live_wq), daemon=True).start()
        self._log("debug", "command: " + " ".join(argv))
        threading.Thread(target=self._reader, args=(job_id, proc), daemon=True).start()
        self._emit_state()
        return True, job_id

    def _write_to(self, proc, text):
        try:
            if proc.stdin is not None and proc.poll() is None:
                with self._wlock:
                    proc.stdin.write(text.encode())
                    proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _live_writer(self, proc, q):
        while True:
            word = q.get()
            if word is None:
                return
            self._write_to(proc, word)

    def _write(self, text):
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return False
        try:
            with self._wlock:
                proc.stdin.write(text.encode())
                proc.stdin.flush()
            return True
        except OSError:
            return False

    # ------------------------------------------------------------- controls
    def _check(self, job_id):
        j = self.job
        if not j or (job_id and j["id"] != job_id):
            return "That job is not the current one (another tab may be out of date). Reload the page."
        return None

    def pause(self, job_id):
        with self.lock:
            err = self._check(job_id)
            if err:
                return False, err
            if self.job["kind"] != "plot" or self.job["mode"] == "trace" or self.job["state"] != "running":
                return False, "Pause is only possible while a plot is running (a trace can only be stopped)."
            if not self._write("PAUSE\n"):
                return False, "The job process is not accepting commands."
            self.job.update(state="pausing", message="Pausing at the end of the current stroke...")
        self._log("info", "Pause requested: the plotter finishes the current stroke, then lifts the pen.")
        self._emit_state()
        return True, ""

    def resume(self, job_id, pause_id):
        with self.lock:
            err = self._check(job_id)
            if err:
                return False, err
            p = self.job.get("pause")
            if self.job["state"] != "paused" or not p:
                return False, "The job is not waiting."
            if pause_id != p.get("id"):
                return False, "This prompt is out of date (the job moved on). Reload the page."
            if p.get("resuming"):
                return True, ""
            if not self._write("CONTINUE\n"):
                return False, "The job process is not accepting commands."
            p["resuming"] = True
            self.job["message"] = "Resuming..."
        self._log("info", "Continue sent.")
        self._emit_state()
        return True, ""

    def return_to_start(self, job_id):
        with self.lock:
            err = self._check(job_id)
            if err:
                return False, err
            if (self.job["kind"] != "plot" or self.job["mode"] == "trace"
                    or self.job["state"] not in ("running", "pausing", "paused")):
                return False, "Return is only possible while a plot is running or paused (a trace returns by itself)."
            if not self._write("RETURN\n"):
                return False, "The job process is not accepting commands."
            self.job.update(state="returning",
                            message="Finishing the current stroke, then travelling back to the start point.")
        self._log("info", "Return requested: finish the stroke, pen up, travel back to S, release motors.")
        self._emit_state()
        return True, ""

    def stop(self, job_id=None):
        """Always allowed. Latched: once requested, nothing new is started for this job."""
        with self.lock:
            j = self.job
            if not j or j["state"] not in ACTIVE:
                return True, "Nothing is running."
            if job_id and j["id"] != job_id:
                job_id = j["id"]       # Stop is never refused because a tab is stale
            j["stop_requested"] = True
            if j["state"] == "starting":
                j["message"] = "Stopping before anything moves..."
                self._emit_state()
                return True, "Cancelled before start."
            if j["state"] == "stopping":
                return True, "Already stopping."
            j.update(state="stopping", message="Stopping: the move already sent finishes, then pen up, motors off.")
            word = "q\n" if j["kind"] == "pentest" else "STOP\n"
            proc = self.proc
            if proc is not None:      # written by a thread: a full pipe must never make Stop wait
                threading.Thread(target=self._write_to, args=(proc, word), daemon=True).start()
        self._log("warn", "Stop requested.")
        self._emit_state()
        if proc is not None:
            threading.Thread(target=self._escalate, args=(j["id"], proc, bool(j.get("sim")) or self.s.sim),
                             daemon=True).start()
        return True, ""

    def _escalate(self, job_id, proc, sim):
        """If the process ignores STOP, interrupt it, then kill it and make the machine safe directly.
        The job stays reserved (busy) until this finishes: the reader waits for self.recovered."""
        rec = threading.Event()
        with self.lock:
            self.recovered = rec          # published before the reader can release the job
        try:
            try:
                proc.wait(STOP_GRACE_S)
                with self.lock:
                    if self.recovered is rec:
                        self.recovered = None     # exited by itself: nothing to recover
                return
            except subprocess.TimeoutExpired:
                pass
            self._log("error", f"The job did not stop within {STOP_GRACE_S:.0f} s; interrupting it.")
            clean = False
            try:
                if os.name == "posix":
                    proc.send_signal(signal.SIGINT)      # plot_svg's own cleanup runs on SIGINT
                    proc.wait(KILL_GRACE_S)
                    clean = True
                else:
                    proc.terminate()                     # no cleanup in the child on Windows
                    proc.wait(KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                self._log("error", "Still running: killing the job process.")
                proc.kill()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    self._log("error", "The job process cannot be killed. Switch the plotter off.")
            except OSError:
                pass
            if not clean:
                try:
                    servo = self.s.machine().get("servo")
                except (OSError, ValueError):
                    servo = None
                for note in hardware.last_resort_safe(sim, servo):
                    self._log("warn", note)
            with self.lock:
                if self.job and self.job["id"] == job_id:
                    self.job["escalated"] = True
        finally:
            rec.set()

    def pentest(self, job_id, cmd):
        keys = {"toggle": "\n", "sweep": "s\n", "quit": "q\n"}
        with self.lock:
            err = self._check(job_id)
            if err:
                return False, err
            if self.job["kind"] != "pentest" or self.job["state"] != "running":
                return False, "The pen test is not running."
            if cmd not in keys:
                return False, "Unknown pen test command."
            if self.job.get("pt_busy"):
                return False, "The pen is still moving. Wait for it to finish."
            if not self._write(keys[cmd]):
                return False, "The pen test is not accepting commands."
            self.job["pt_busy"] = True
        self._emit_state()
        return True, ""

    # ------------------------------------------------------------- output
    def _reader(self, job_id, proc):
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._line(job_id, line.decode("utf-8", "replace").rstrip("\r"))
                if buf.endswith((b": ", b"> ")) and not buf.lstrip().startswith((b"{", b"__")):   # an input() prompt has no newline
                    self._line(job_id, buf.decode("utf-8", "replace"))
                    buf = b""
            if buf:
                self._line(job_id, buf.decode("utf-8", "replace"))
        except Exception as ex:  # noqa: BLE001
            self._log("error", f"Lost the job output: {ex!r}")
        rc = proc.wait()
        rec = self.recovered
        if rec is not None:
            rec.wait(60)                 # keep the job reserved until the escalation has made the machine safe
            with self.lock:
                if self.recovered is rec:
                    self.recovered = None
        for f in (proc.stdin, proc.stdout):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        if self.live_wq is not None:
            try:
                self.live_wq.put_nowait(None)
            except queue.Full:
                pass
        self._finish(job_id, rc)

    def _live_event(self, j, line):
        """JSON events from live_plot.py --control: keep the page's strokes server side, tell the UI what happened."""
        try:
            ev = json.loads(line)
        except ValueError:
            self._log("debug", line, "live")
            return
        kind = ev.get("event")
        with self.lock:
            live = j.setdefault("live", {"page": 1, "lines": 0, "busy": False, "last": ""})
            if kind == "session":
                live["canvas"] = ev.get("canvas_mm")
                live["font"] = ev.get("font")
                j["message"] = "Live writing: type a line and press Write."
            elif kind == "line_start":
                live["busy"] = True
                live["page"] = ev.get("page", live["page"])
                j["message"] = "Writing a line..."
            elif kind == "line_done":
                live["busy"] = False
                live["lines"] += 1
                live["last"] = str(ev.get("text", ""))[:80]
                self.live_strokes.extend(ev.get("strokes") or [])
                self.live_points = sum(len(s.get("pts", ())) for s in self.live_strokes)
                while self.live_points > LIVE_MAX_POINTS and self.live_strokes:
                    self.live_points -= len(self.live_strokes.pop(0).get("pts", ()))
                j["message"] = "Live writing: type the next line."
            elif kind == "new_page":
                live["page"] = ev.get("page", live["page"] + 1)
                live["busy"] = False
                live["paper_change"] = True
                self.live_strokes.clear()
                self.live_points = 0
                j["message"] = "New page: the carriage is at S. Change the paper without moving the carriage."
            elif kind == "mirror_stroke":
                live["strokes"] = ev.get("total", live.get("strokes", 0))
                self.live_strokes.extend(ev.get("strokes") or [])
                self.live_points = sum(len(s.get("pts", ())) for s in self.live_strokes)
                while self.live_points > LIVE_MAX_POINTS and self.live_strokes:
                    self.live_points -= len(self.live_strokes.pop(0).get("pts", ()))
            elif kind == "mirror_status":
                live["pending_points"] = ev.get("pending_points", 0)
                live["pen_open"] = bool(ev.get("open"))
                live["sport"] = bool(ev.get("sport"))
            elif kind == "resumed":
                live["paper_change"] = False
                j["message"] = "Live writing: type the next line."
            elif kind == "page_full":
                live["busy"] = False
                j["message"] = "The page is full. Press New page, change the paper, and continue."
        if kind in ("mirror_stroke", "mirror_status"):
            self._emit_state(force=kind == "mirror_stroke")
            return
        text = {"line_start": "Writing a line", "line_done": f"Wrote: {ev.get('text', '')}",
                "new_page": ev.get("message", "New page"), "page_full": "The page is full: the text did not fit, nothing was drawn.",
                "warning": ev.get("message", ""), "error": ev.get("message", "")}.get(kind)
        if text:
            level = "error" if kind == "error" else "warn" if kind in ("warning", "page_full") else "info"
            self._log(level, text, "live")
            if level == "error":
                with self.lock:
                    j["last_error"] = text
        self._emit_state()

    def _machine_event(self, j, line):
        """JSON events from jog.py --control."""
        try:
            ev = json.loads(line)
        except ValueError:
            self._log("debug", line, "machine")
            return
        kind = ev.get("event")
        with self.lock:
            m = j.setdefault("machine", {"ready": False})
            if kind == "session":
                m.update(area=ev.get("area_mm"), start=ev.get("start_mm"), speed=ev.get("speed"))
            elif kind == "ready":
                m["ready"] = True
                j["message"] = "Machine control: jog, go to a point, or finish."
            elif kind == "moved":
                m["clamped"] = bool(ev.get("clamped"))
            elif kind == "speed":
                m["speed"] = ev.get("speed")
            elif kind == "zeroed":
                m["zeroed"] = True
                j["message"] = "S is set here: plots start from this point."
        text = {"zeroed": "S set here: the tracked position is S again.", "warning": ev.get("message", ""),
                "error": ev.get("message", "")}.get(kind)
        if text:
            level = "error" if kind == "error" else "warn" if kind == "warning" else "info"
            self._log(level, text, "machine")
            if level == "error":
                with self.lock:
                    j["last_error"] = text
        self._emit_state(force=kind not in ("moved",))

    def machine_send(self, job_id, cmd, args=None):
        """Commands for a running machine session (jog.py): move, goto, home, pen, tick, speed, sets, quit."""
        args = args if isinstance(args, dict) else {}

        def num(k, lo, hi):
            v = args.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not lo <= v <= hi:
                raise ValueError(f"{k} must be a number between {lo:g} and {hi:g}")
            return float(v)
        with self.lock:
            err = self._check(job_id)
            if err:
                return False, err
            j = self.job
            if j["kind"] != "machine" or j["state"] != "running" or not (j.get("machine") or {}).get("ready"):
                return False, "Machine control is not running."
            try:
                if cmd == "move":
                    word = f"MOVE {num('dx', -500, 500):.3f} {num('dy', -500, 500):.3f}"
                elif cmd == "goto":
                    word = f"GOTO {num('x', -10, 1000):.3f} {num('y', -10, 1000):.3f}"
                elif cmd == "home":
                    word = "HOME"
                elif cmd == "pen":
                    if args.get("pen") not in ("up", "down"):
                        raise ValueError("pen must be up or down")
                    word = "PEN " + args["pen"].upper()
                elif cmd == "tick":
                    word = f"TICK {num('dx', -20, 20):.3f} {num('dy', -20, 20):.3f}"
                elif cmd == "speed":
                    word = f"SPEED {num('speed', 1, 200):g}"
                elif cmd == "sets":
                    word = "SETS"
                elif cmd == "quit":
                    word = "QUIT HERE" if args.get("here") else "QUIT"
                else:
                    return False, "Unknown machine command."
            except ValueError as ex:
                return False, str(ex)
            if self.live_wq is None or self.live_wq.qsize() >= 40:
                return False, "Too many commands are waiting. Wait for the carriage, or stop."
            self.live_wq.put_nowait(word + "\n")
        return True, ""

    def live_send(self, job_id, cmd, text="", phase=None, pts=None, sid=None):
        """Commands for a running live session: text, newpage, quit. Refused while stopping."""
        with self.lock:
            err = self._check(job_id)
            if err:
                return False, err
            j = self.job
            if j["kind"] != "live" or j["state"] != "running":
                return False, "There is no live writing session running."
            if cmd == "text":
                text = " ".join(str(text).replace("\r", "\n").split("\n"))[:500].strip()
                if not text:
                    return False, "Type something to write."
                word = "TEXT " + text + "\n"
            elif cmd == "newpage":
                word = "NEWPAGE\n"
            elif cmd == "continue":
                word = "CONTINUE\n"
            elif cmd == "mirror":
                clean = _clean_points(pts)
                if phase not in ("start", "move", "end", "stroke") or clean is None:
                    return False, "Bad pad message."
                if (j.get("live") or {}).get("paper_change"):
                    return False, "The paper is being changed: press Continue first."
                if sid is not None and not (isinstance(sid, str) and 0 < len(sid) <= 40):
                    return False, "Bad pad message."
                if phase == "start":
                    self.live_sid = sid
                elif phase in ("move", "end") and sid is not None and sid != self.live_sid:
                    return False, "That stroke is not the open one."
                if phase in ("end", "stroke"):
                    self.live_sid = None
                word = "MIRROR " + phase + " " + json.dumps(clean, separators=(",", ":")) + "\n"
            elif cmd == "quit":
                word = "QUIT\n"
            else:
                return False, "Unknown live command."
            if self.live_wq is None:
                return False, "The session is not accepting commands."
            if not word.startswith("MIRROR end ") and self.live_wq.qsize() >= 300:
                return False, "Too many messages are waiting for the session. Slow down, or stop it."
            self.live_wq.put_nowait(word)
        return True, ""

    def _marker(self, line, name):
        try:
            return json.loads(line[len(name) + 5:])
        except ValueError:
            return {}

    def _line(self, job_id, line):
        j = self.job
        if not line.strip():
            return
        if line.startswith("__POS__:"):
            pos = self._marker(line, "POS")
            with self.lock:
                j["pos"] = {"x": pos.get("x"), "y": pos.get("y")}
                j["pen"] = pos.get("pen", j["pen"])
                j["moved"] = True
            self.ev.emit("pos", x=pos.get("x"), y=pos.get("y"), pen=pos.get("pen"))
            return
        if line.startswith("__PROGRESS__:"):
            pr = self._marker(line, "PROGRESS")
            done_s, total_s, el = pr.get("est_done_s", 0), pr.get("est_total_s", 0) or 1e-9, pr.get("elapsed_s", 0)
            ratio = el / done_s if done_s > 5 else 1.0
            pr["fraction"] = max(0.0, min(1.0, done_s / total_s))
            pr["eta_s"] = max(0.0, (total_s - done_s) * max(0.2, min(5.0, ratio)))
            with self.lock:
                j["progress"] = pr
            self._emit_state(force=False)
            return
        if j["kind"] == "live" and line.startswith('{"event"'):
            self._live_event(j, line)
            return
        if j["kind"] == "machine" and line.startswith('{"event"'):
            self._machine_event(j, line)
            return
        self._log("debug", line, "plot")    # raw protocol lines stay visible under Debug
        if line.startswith("__LAYER__:"):
            info = self._marker(line, "LAYER")
            with self.lock:
                j["layer"] = info
            self._emit_state()
        elif line.startswith("__PAUSE__:"):
            info = self._marker(line, "PAUSE")
            with self.lock:
                self.pause_seq += 1
                info["id"] = self.pause_seq
                if info.get("kind") == "swap":
                    msg = f"Pen change: put in the pen for {info.get('label', 'the next layer')}"
                    msg += f" ({info['pen']})." if info.get("pen") else "."
                else:
                    msg = "Paused. Pen is up; motors stay on, so do not push the carriage."
                stopping = j["state"] in ("stopping", "returning")
                j["pause"] = info
                if not stopping:
                    j.update(state="paused", message=msg)
            self._log("warn", msg)
            self._emit_state()
        elif line.startswith("__RESUMED__"):
            with self.lock:
                if j["state"] == "paused":
                    j.update(state="running", message="Running.")
                j["pause"] = None
            self._emit_state()
        elif line.startswith("__RESULT__:"):
            with self.lock:
                j["result"] = self._marker(line, "RESULT")
        elif line.startswith("__"):
            pass
        else:
            low = line.lower()
            if line.startswith("PROBLEM") or line.startswith("ERROR") or "failed" in low or "traceback" in low:
                level = "error"
            elif line.startswith("WARNING") or line.startswith("Interrupted") or line.startswith("***"):
                level = "warn"
            else:
                level = "info"
            if j["kind"] == "pentest" and line.startswith("["):
                with self.lock:
                    j["pen"] = "down" if line.startswith("[DOWN]") else "up"
                    j["pt_busy"] = False
                self._emit_state()
                return
            if j["kind"] == "pentest" and "shutdown problem" in low:
                level = "error"
                with self.lock:
                    j["cleanup_failed"] = True
            self._log(level, line, "plot" if j["kind"] in ("plot", "live") else "tool")
            with self.lock:
                j["last_line"] = line
                if level == "error":
                    j["last_error"] = line

    def _finish(self, job_id, rc):
        with self.lock:
            j = self.job
            if not j or j["id"] != job_id:
                return
            self.proc = None
            saved_position = dict(self.position)
            res = j.get("result")
            j["finished"] = time.time()
            j["returncode"] = rc
            j["pause"] = None
            if j["kind"] == "pentest":
                ok = rc == 0 and not j.get("cleanup_failed") and not j.get("escalated")
                if j.get("cleanup_failed"):
                    j.update(state="error", message="The pen test could not raise the pen at the end. Check the "
                                                    "pen; switch the plotter off if it is down.")
                else:
                    j.update(state="complete" if ok else ("stopped" if j["stop_requested"] else "error"),
                             message="Pen test finished, pen raised." if ok else
                             f"Pen test ended: {j.get('last_error') or j.get('last_line') or 'exit code %s' % rc}")
            elif res:
                status = res.get("status", "")
                if "FAILED" in status:
                    j.update(state="error", message="Cleanup failed: the pen may be down or the motors on. "
                                                    "Switch the plotter off, then check it.")
                    self.position = {"state": "unknown", "at": None, "note": "Unknown after a failed cleanup."}
                elif status == "complete" and j["kind"] == "machine":
                    j.update(state="complete", message="Machine control finished, motors off. " + (
                        "The carriage is at S." if res.get("at_start") else
                        f"The carriage was left at about ({res['end_mm'][0]:.0f}, {res['end_mm'][1]:.0f}) mm."))
                elif status == "complete":
                    j.update(state="complete", message="Finished. The carriage is back at the start point.")
                elif status.startswith("stopped"):
                    j.update(state="stopped", message="Stopped and returned to the start point.")
                elif status.startswith("interrupted"):
                    where = (f"at ({res['end_mm'][0]:.1f}, {res['end_mm'][1]:.1f}) mm, read back from the board"
                             if res.get("readback") else
                             f"at about ({res['end_mm'][0]:.0f}, {res['end_mm'][1]:.0f}) mm")
                    j.update(state="stopped" if j["stop_requested"] else "interrupted",
                             message=f"Stopped. Pen up, motors released. The carriage was left {where}: slide it "
                                     "back to the paper's top-left before the next run.")
                else:
                    j.update(state="error", message=f"The plot stopped with an error: {status}. Pen up and motors "
                                                    "released if the cleanup ran; check the machine.")
                if "FAILED" not in status:
                    if res.get("at_start"):
                        self.position = {"state": "at_start", "at": res.get("end_mm"),
                                         "note": "At the start point by the board's step count. Steps lost to a "
                                                 "stall are not counted: check by eye before the next run."
                                         if res.get("readback") else
                                         "Should be at the start point (commanded position; there is no "
                                         "readback). Check by eye before the next run."}
                    else:
                        self.position = {"state": "moved", "at": res.get("end_mm"),
                                         "note": "Not at the start point. Slide it back by hand."}
            else:
                # Exited before the plotter was set up (refused, or killed).
                if j["moved"]:
                    j.update(state="error", message="The job process ended without a final report. Position "
                                                    "unknown: switch the plotter off and re-park the carriage.")
                    self.position = {"state": "unknown", "at": None, "note": "Unknown after an abnormal end."}
                elif j["stop_requested"]:
                    j.update(state="stopped", message="Stopped before anything moved.")
                else:
                    detail = j.get("last_error") or j.get("last_line") or f"exit code {rc}"
                    j.update(state="error" if rc else "complete",
                             message=("Nothing was sent: " + detail) if rc else "Done.")
            if j["kind"] == "live" and j["meta"].get("sport") and not j.get("sim"):
                self.position = {"state": "unknown", "at": None,
                                 "note": "Sport mode runs above the tested speed and can lose steps: check the carriage "
                                         "and park it again before the next run."}
            if j.get("sim") and not self.s.sim:
                self.position = saved_position     # simulated runs say nothing about the real carriage
            final = j["state"]
            msg = j["message"]
        self._log({"complete": "info", "stopped": "warn"}.get(final, "error"), f"Job {job_id}: {final}. {msg}")
        self._emit_state()
