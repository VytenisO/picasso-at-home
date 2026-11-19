"""The only places the server itself talks to the EBB. Both are skipped entirely in simulator mode.

  precheck()   read-only 'V' query: is the plotter there and answering?
  last_resort_safe()  after a job process had to be killed: pen up and motors off, best effort
  ServoGuard   after every power-on or reset: put this lift's pen positions into the board
"""
import os
import re
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

EBB = (0x04D8, 0xFD92)


def find_port():
    if serial is None:
        return None
    return next((p.device for p in list_ports.comports() if (p.vid, p.pid) == EBB), None)


def precheck(sim):
    """-> (ok, message). Never called while a job owns the port (the runner reserves first)."""
    if sim:
        return True, "simulator (no hardware)"
    if serial is None:
        return False, "pyserial is not installed for this Python. Install python-pyserial on the plotter PC."
    try:
        port = find_port()
    except Exception as ex:  # noqa: BLE001
        return False, f"Could not list serial ports: {ex}"
    if not port:
        return False, "No plotter found on USB (EBB 04d8:fd92). Plug it in and switch it on."
    try:
        with serial.Serial(port, 9600, timeout=2) as s:
            s.reset_input_buffer()
            s.write(b"V\r")
            ver = s.readline().decode(errors="replace").strip()
        return True, f"{port} ({ver or 'no version reply'})"
    except Exception as ex:  # noqa: BLE001
        return False, (f"Cannot open {port}: {ex}. The server user must be in the serial group "
                       "(dialout or uucp); log out and in again after adding it.")


def last_resort_safe(sim, servo=None):
    """Raise the pen and release the motors directly. Only after the job process is gone.

    servo: {"pen_up", "pen_down"} from machine.json. Sent first, because after a reset firmware 3.x holds its own
    default pen-up (22565), far past the top of this lift."""
    if sim:
        return ["simulator: nothing to do"]
    if serial is None:
        return ["pyserial unavailable: could not raise the pen directly"]
    try:
        port = find_port()
        if not port:
            return ["no plotter found for a direct pen-up/motors-off"]
        with serial.Serial(port, 9600, timeout=2) as s:
            s.reset_input_buffer()
            if servo:
                for k, v in ((4, servo["pen_up"]), (5, servo["pen_down"])):
                    s.write(f"SC,{k},{int(v)}\r".encode())
                    s.readline()
            s.write(b"SP,1,300\r")
            time.sleep(0.4)
            s.readline()
            s.write(b"EM,0,0\r")
            time.sleep(0.1)
            s.readline()
        return [f"{port}: pen raised, motors released"]
    except Exception as ex:  # noqa: BLE001
        return [f"could not reach the plotter: {ex}"]


# Firmware 2.6.0 and later drive the pen servo to their own default pen-up (22565) at every power-on or reset, far
# past the top of this lift: the servo stalls and buzzes until a host sends the real positions. Those live in RAM
# only, so the server puts them back whenever the board comes up fresh. SL/QL is a RAM byte that a reset clears:
# it tells a fresh board from one already set, even when the USB reconnect was too quick to notice.
PEN_PIN = 4          # S2 takes the RP pin number: the pen servo socket (RB1) is RP4
SET_MARK = 1
SERVO_RATE = 100     # S2 rate: from 22565 down to pen up in about 2 s


def _ask(s, line, one_line=False):
    s.write(line.encode() + b"\r")
    out = []
    while True:
        r = s.readline().decode(errors="replace").strip()
        if not r:
            break
        out.append(r)
        if one_line or r == "OK" or r.startswith("!"):
            break
    return out


def firmware_version(text):
    m = re.search(r"Version (\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def settle_servo(s, servo):
    """s: an open serial port to the EBB. -> a message when the pen positions were put in, None when not needed."""
    s.reset_input_buffer()
    ver = firmware_version(" ".join(_ask(s, "V", one_line=True)))
    if not ver or ver < (2, 6, 0):
        return None
    q = _ask(s, "QL")
    if q and q[0].isdigit() and int(q[0]) == SET_MARK:
        return None
    up, down = int(servo["pen_up"]), int(servo["pen_down"])
    for line in (f"SC,4,{up}", f"SC,5,{down}", f"S2,{up},{PEN_PIN},{SERVO_RATE}", f"SL,{SET_MARK}"):
        out = _ask(s, line)
        if "OK" not in out:
            raise RuntimeError(f"{line} -> {out}")
    return f"Plotter switched on or reset: pen servo moved to pen up ({up}), pen down set to {down}."


class ServoGuard(threading.Thread):
    """Watches the EBB's serial port node; when a new one appears (power-on, reset, replug) and no job owns the
    port, runs settle_servo under the hardware lock. Also runs once at server start."""

    def __init__(self, settings, runner, lock, log, interval=0.5):
        super().__init__(daemon=True, name="servo-guard")
        self.s, self.runner, self.lock, self.log, self.interval = settings, runner, lock, log, interval

    @staticmethod
    def _ident(port):
        try:
            st = os.stat(port)
            return port, st.st_ino, st.st_ctime_ns       # udev makes a new node for every USB connect
        except OSError:
            return (port,)

    def _settle(self, port):
        extra = {"exclusive": True} if os.name == "posix" else {}
        with serial.Serial(port, 9600, timeout=0.5, **extra) as ser:
            return settle_servo(ser, self.s.machine()["servo"])

    def run(self):
        seen = pending = None
        since = 0.0
        while True:
            time.sleep(self.interval)
            try:
                port = find_port()
                ident = self._ident(port) if port else None
                if ident is None or ident == seen:
                    pending = None
                    continue
                if ident != pending:                     # give a board that just enumerated a moment
                    pending, since = ident, time.monotonic()
                    continue
                if time.monotonic() - since < 0.3 or self.runner.busy():
                    continue                             # a job sets the pen itself; check again once it ends
                if not self.lock.acquire(timeout=0.2):
                    continue
                try:
                    if self.runner.busy():
                        continue
                    msg = self._settle(port)
                finally:
                    self.lock.release()
                seen = ident
                if msg:
                    self.log("warn", msg)
            except Exception as ex:  # noqa: BLE001
                seen = ident
                self.log("error", f"Could not set the pen servo after power-on: {ex}. If it buzzes at the top, "
                                  "start a jog or switch the plotter off.")
