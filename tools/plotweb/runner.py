"""One job subprocess at a time: reservation, launch, output parsing, control commands, final state.

States: idle -> starting -> running <-> pausing -> paused -> running ... -> stopping | returning
        -> complete | stopped | interrupted | error
A job is reserved ("starting") before any hardware check or preflight, so Stop can cancel it before
anything is launched, and a second start is refused.

The carriage position the server reports is the commanded (estimated) one: firmware 2.0.1 has no position
readback. After a run the server records whether the carriage should be back at the start point S.
"""
import json
import os
import signal
import subprocess
import threading
import time

from . import hardware

ACTIVE = ("starting", "running", "pausing", "paused", "stopping", "returning")
STOP_GRACE_S = 30.0      # plot_svg needs at most one move chunk (3 s) plus pen-up and cleanup; this is generous
KILL_GRACE_S = 15.0


def _new_job(job_id, kind, mode, sim, meta):
    return {"id": job_id, "kind": kind, "mode": mode, "sim": sim, "state": "starting", "meta": meta or {},
            "started": time.time(), "finished": None, "stop_requested": False,
            "progress": None, "layer": None, "pos": None, "pen": "up", "pause": None,
            "result": None, "message": "Preparing...", "plan_id": (meta or {}).get("plan_id"),
            "moved": False}


class JobRunner:
    def __init__(self, settings, events):
        self.s = settings
        self.ev = events
        self.lock = threading.RLock()
        self.job = None
        self.proc = None
        self.pause_seq = 0
        self.preview = None           # the immutable preview of the running plot job
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
        self._log("debug", "command: " + " ".join(argv))
        threading.Thread(target=self._reader, args=(job_id, proc), daemon=True).start()
        self._emit_state()
        return True, job_id

    def _write(self, text):
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return False
        try:
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
            if self.job["kind"] != "plot" or self.job["state"] != "running":
                return False, "Pause is only possible while a plot is running."
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
            if self.job["kind"] != "plot" or self.job["state"] not in ("running", "pausing", "paused"):
                return False, "Return is only possible while a plot is running or paused."
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
            self._write(word)
            proc = self.proc
        self._log("warn", "Stop requested.")
        self._emit_state()
        if proc is not None:
            threading.Thread(target=self._escalate, args=(j["id"], proc), daemon=True).start()
        return True, ""

    def _escalate(self, job_id, proc):
        """If the process ignores STOP, interrupt it, then kill it and make the machine safe directly."""
        try:
            proc.wait(STOP_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        self._log("error", f"The job did not stop within {STOP_GRACE_S:.0f} s; interrupting it.")
        try:
            if os.name == "posix":
                proc.send_signal(signal.SIGINT)
            else:
                proc.terminate()
            proc.wait(KILL_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            return
        self._log("error", "Still running: killing the job process.")
        proc.kill()
        proc.wait(5)
        with self.lock:
            sim = bool(self.job and self.job.get("sim")) or self.s.sim
        for note in hardware.last_resort_safe(sim):
            self._log("warn", note)

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
            if not self._write(keys[cmd]):
                return False, "The pen test is not accepting commands."
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
                if buf.endswith((b": ", b"> ")):      # an input() prompt has no newline
                    self._line(job_id, buf.decode("utf-8", "replace"))
                    buf = b""
            if buf:
                self._line(job_id, buf.decode("utf-8", "replace"))
        except Exception as ex:  # noqa: BLE001
            self._log("error", f"Lost the job output: {ex!r}")
        rc = proc.wait()
        for f in (proc.stdin, proc.stdout):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        self._finish(job_id, rc)

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
                self._emit_state()
                return
            self._log(level, line, "plot" if j["kind"] == "plot" else "tool")
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
            res = j.get("result")
            j["finished"] = time.time()
            j["returncode"] = rc
            j["pause"] = None
            if j["kind"] == "pentest":
                ok = rc == 0
                j.update(state="complete" if ok else ("stopped" if j["stop_requested"] else "error"),
                         message="Pen test finished, pen raised." if ok else
                         f"Pen test ended: {j.get('last_error') or j.get('last_line') or 'exit code %s' % rc}")
            elif res:
                status = res.get("status", "")
                if "FAILED" in status:
                    j.update(state="error", message="Cleanup failed: the pen may be down or the motors on. "
                                                    "Switch the plotter off, then check it.")
                    self.position = {"state": "unknown", "at": None, "note": "Unknown after a failed cleanup."}
                elif status == "complete":
                    j.update(state="complete", message="Finished. The carriage is back at the start point.")
                elif status.startswith("stopped"):
                    j.update(state="stopped", message="Stopped and returned to the start point.")
                elif status.startswith("interrupted"):
                    j.update(state="stopped" if j["stop_requested"] else "interrupted",
                             message="Stopped. Pen up, motors released. The carriage was left at about "
                                     f"({res['end_mm'][0]:.0f}, {res['end_mm'][1]:.0f}) mm: slide it back to the "
                                     "paper's top-left before the next run.")
                else:
                    j.update(state="error", message=f"The plot stopped with an error: {status}. Pen up and motors "
                                                    "released if the cleanup ran; check the machine.")
                if "FAILED" not in status:
                    if res.get("at_start"):
                        self.position = {"state": "at_start", "at": res.get("end_mm"),
                                         "note": "Should be at the start point (commanded position; there is no "
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
            final = j["state"]
            msg = j["message"]
        self._log({"complete": "info", "stopped": "warn"}.get(final, "error"), f"Job {job_id}: {final}. {msg}")
        self._emit_state()
