"""Sequence-numbered event log feeding the live stream (Server-Sent Events).

Two rings:
  history  console lines (type "log"), long, replayed to a reloaded page
  live     every event, short, for connected streams

A client that falls behind the live ring, or reconnects to a restarted server (new epoch), gets a "reset"
event and re-reads the snapshot. Producers never block on slow clients: they append under a lock and notify.
"""
import collections
import secrets
import threading
import time

LEVELS = ("debug", "info", "warn", "error")


class EventLog:
    def __init__(self, history=5000, live=3000):
        self.epoch = secrets.token_hex(4)
        self.seq = 0
        self.history = collections.deque(maxlen=history)
        self.live = collections.deque(maxlen=live)
        self.cond = threading.Condition()

    def emit(self, etype, **data):
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "t": round(time.time(), 3), "type": etype}
            ev.update(data)
            if etype == "log":
                self.history.append(ev)
            self.live.append(ev)
            self.cond.notify_all()
            return ev

    def log(self, level, text, src="server"):
        if level not in LEVELS:
            level = "info"
        return self.emit("log", level=level, src=src, text=str(text).rstrip("\n"))

    def log_since(self, since=0, limit=5000):
        with self.cond:
            out = [e for e in self.history if e["seq"] > since]
            return out[-limit:], self.seq

    def wait(self, cursor, timeout=15.0):
        """-> (events after cursor, new cursor, reset flag). Blocks up to timeout when there is nothing new."""
        with self.cond:
            if self.seq <= cursor:
                self.cond.wait(timeout)
            if self.seq <= cursor:
                return [], cursor, False
            oldest = self.live[0]["seq"] if self.live else self.seq + 1
            if cursor + 1 < oldest:
                return [], self.seq, True
            evs = [e for e in self.live if e["seq"] > cursor]
            return evs, self.seq, False
