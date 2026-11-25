"""Background tasks: slow work (tracing a large drawing, measuring its shading) off the request thread.

One task runs at a time; the rest wait in a queue (the server may share its machine). A task reports
progress as (stage, fraction); every change goes out on the event stream as a "task" event, so every
open page sees what runs, how far it is and how long it has left. Finished tasks stay listed for a
while, with their run time, and each kind keeps a short history of seconds per megapixel to estimate
the next run before it has made progress of its own.

A task's function gets report(stage, fraction); report raises Cancelled once the task is cancelled,
so cancelling takes effect at the next progress report.
"""
import collections
import json
import secrets
import threading
import time
import traceback

KEEP_DONE = 30            # finished tasks listed
HISTORY = 12              # past runs per kind for the estimate
EMIT_EVERY = 0.4          # s between progress events of one task


class Cancelled(Exception):
    pass


class Tasks:
    def __init__(self, events, store_path=None):
        self.ev = events
        self.store = store_path
        self.lock = threading.Lock()
        self.items = collections.OrderedDict()      # id -> task (internal dict)
        self.queue = collections.deque()
        self.wake = threading.Condition(self.lock)
        self.history = {}
        if store_path:
            try:
                self.history = json.loads(store_path.read_text())
            except (OSError, ValueError):
                self.history = {}
        threading.Thread(target=self._worker, daemon=True, name="tasks").start()

    # ---- public
    def start(self, kind, title, fn, key=None, size=None, on_done=None):
        """Queue fn(report) -> result. key: a task with the same key that is queued or running is
        returned instead of starting another (the same trace asked for twice). size: megapixels,
        for the estimate. -> public task dict."""
        with self.lock:
            if key:
                for t in self.items.values():
                    if t["key"] == key and t["state"] in ("queued", "running"):
                        return self._public(t)
            t = {"id": secrets.token_hex(6), "kind": kind, "title": title, "key": key, "size": size,
                 "state": "queued", "stage": "Waiting", "frac": 0.0, "queued": time.time(), "started": None,
                 "finished": None, "error": None, "fn": fn, "on_done": on_done, "cancel": False,
                 "emitted": 0.0, "result": None}
            self.items[t["id"]] = t
            self.queue.append(t["id"])
            self._trim()
            self.wake.notify()
            pub = self._public(t)
        self._emit(t, force=True)
        return pub

    def find(self, key):
        """The queued or running task with this key, or None."""
        with self.lock:
            for t in self.items.values():
                if t["key"] == key and t["state"] in ("queued", "running"):
                    return self._public(t)
        return None

    def get(self, tid):
        with self.lock:
            t = self.items.get(tid)
            return self._public(t) if t else None

    def result(self, tid):
        with self.lock:
            t = self.items.get(tid)
            return t and t["result"]

    def list(self):
        with self.lock:
            return [self._public(t) for t in reversed(self.items.values())]

    def cancel(self, tid):
        with self.lock:
            t = self.items.get(tid)
            if not t or t["state"] not in ("queued", "running"):
                return False
            t["cancel"] = True
            if t["state"] == "queued":
                self.queue.remove(tid)
                self._finish(t, "cancelled")
        self._emit(t, force=True)
        return True

    def estimate(self, kind, size):
        """Seconds a task of this kind and size is expected to take, from past runs (None if unknown)."""
        h = self.history.get(kind) or []
        if not h:
            return None
        if size:
            rates = sorted(s / max(mp, 0.01) for s, mp in h)
            return rates[len(rates) // 2] * size
        return sorted(s for s, _ in h)[len(h) // 2]

    # ---- worker
    def _worker(self):
        while True:
            with self.lock:
                while not self.queue:
                    self.wake.wait()
                t = self.items[self.queue.popleft()]
                if t["cancel"]:
                    continue
                t["state"], t["started"], t["stage"] = "running", time.time(), "Starting"
            self._emit(t, force=True)

            def report(stage, frac, t=t):
                if t["cancel"]:
                    raise Cancelled()
                t["stage"], t["frac"] = str(stage), max(t["frac"], min(float(frac), 0.999))
                self._emit(t)

            try:
                res = t["fn"](report)
                state, err = "done", None
            except Cancelled:
                res, state, err = None, "cancelled", None
            except Exception as ex:  # noqa: BLE001
                res, state = None, "error"
                err = getattr(ex, "message", None) or str(ex) or ex.__class__.__name__
                self.ev.log("error", f"{t['title']} failed: {err}")
                self.ev.log("debug", traceback.format_exc())
            with self.lock:
                t["result"] = res
                self._finish(t, state, err)
                if state == "done":
                    self._learn(t)
            self._emit(t, force=True)
            secs = t["finished"] - t["started"]
            if state == "done":
                self.ev.log("info", f"{t['title']}: done in {fmt_secs(secs)}.")
            elif state == "cancelled":
                self.ev.log("info", f"{t['title']}: cancelled after {fmt_secs(secs)}.")
            if t["on_done"]:
                try:
                    t["on_done"](t)
                except Exception:  # noqa: BLE001
                    pass

    def _finish(self, t, state, err=None):
        t["state"], t["error"], t["finished"] = state, err, time.time()
        t["fn"] = None
        if state == "done":
            t["frac"], t["stage"] = 1.0, "Done"
        elif state == "cancelled":
            t["stage"] = "Cancelled"

    def _learn(self, t):
        h = self.history.setdefault(t["kind"], [])
        h.append([round(t["finished"] - t["started"], 2), t["size"] or 0])
        del h[:-HISTORY]
        if self.store:
            try:
                tmp = self.store.with_name(f".{self.store.name}.{secrets.token_hex(3)}")
                tmp.write_text(json.dumps(self.history))
                tmp.replace(self.store)
            except OSError:
                pass

    def _trim(self):
        done = [k for k, t in self.items.items() if t["state"] not in ("queued", "running")]
        for k in done[:max(0, len(done) - KEEP_DONE)]:
            del self.items[k]

    # ---- views
    def _public(self, t):
        now = time.time()
        start = t["started"]
        elapsed = ((t["finished"] or now) - start) if start else 0.0
        eta = None
        if t["state"] in ("queued", "running"):
            est = self.estimate(t["kind"], t["size"])
            if t["state"] == "running" and t["frac"] >= 0.08:
                eta = elapsed * (1 - t["frac"]) / t["frac"]
                if est:                       # early progress is uneven: lean on history until halfway
                    w = min(1.0, t["frac"] / 0.5)
                    eta = w * eta + (1 - w) * max(est - elapsed, 0)
            elif est:
                eta = max(est - elapsed, 0)
        return {"id": t["id"], "kind": t["kind"], "title": t["title"], "state": t["state"], "stage": t["stage"],
                "frac": round(t["frac"], 4), "elapsed": round(elapsed, 1), "eta": None if eta is None else round(eta, 1),
                "queued_at": t["queued"], "started_at": start, "finished_at": t["finished"], "error": t["error"],
                "position": (list(self.queue).index(t["id"]) + 1) if t["state"] == "queued" else None}

    def _emit(self, t, force=False):
        now = time.time()
        if not force and now - t["emitted"] < EMIT_EVERY:
            return
        t["emitted"] = now
        with self.lock:
            pub = self._public(t)
        self.ev.emit("task", task=pub)


def fmt_secs(s):
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"
