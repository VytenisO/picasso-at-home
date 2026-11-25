"""The only places the server itself talks to the EBB. Both are skipped entirely in simulator mode.

  precheck()   read-only 'V' query: is the plotter there and answering?
  last_resort_safe()  after a job process had to be killed: pen up and motors off, best effort
  ServoGuard   after every power-on or reset: put this lift's pen positions into the board
  PenButton    a push button on B0: first press locks the motors and lowers the pen, second press raises it again
"""
import json
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
LAST = None          # the last answer from the plotter: {ok, port, firmware, message, at}; shown in the Machine tab


def _remember(ok, message, port=None, firmware=None):
    global LAST
    LAST = {"ok": ok, "port": port, "firmware": firmware, "message": message, "at": time.strftime("%H:%M:%S")}
    return ok, message


PORT_CHOICE = None   # a serial port picked in the Machine tab; None = find the EBB by its USB id


def load_port_choice(settings):
    global PORT_CHOICE
    try:
        PORT_CHOICE = json.loads((settings.cache / "port.json").read_text()).get("port") or None
    except (OSError, ValueError, AttributeError):
        PORT_CHOICE = None


def set_port_choice(settings, port):
    global PORT_CHOICE
    PORT_CHOICE = port or None
    (settings.cache / "port.json").write_text(json.dumps({"port": PORT_CHOICE}))


def find_port():
    if serial is None:
        return None
    ports = list(list_ports.comports())
    if PORT_CHOICE and any(p.device == PORT_CHOICE for p in ports):
        return PORT_CHOICE
    return next((p.device for p in ports if (p.vid, p.pid) == EBB), None)


def precheck(sim):
    """-> (ok, message). Never called while a job owns the port (the runner reserves first)."""
    if sim:
        return _remember(True, "simulator (no hardware)", "simulator")
    if serial is None:
        return _remember(False, "pyserial is not installed for this Python. Install python-pyserial on the plotter PC.")
    try:
        port = find_port()
    except Exception as ex:  # noqa: BLE001
        return _remember(False, f"Could not list serial ports: {ex}")
    if not port:
        return _remember(False, "No plotter found on USB (EBB 04d8:fd92). Plug it in and switch it on.")
    try:
        with serial.Serial(port, 9600, timeout=2) as s:
            s.reset_input_buffer()
            s.write(b"V\r")
            ver = s.readline().decode(errors="replace").strip()
        fw = firmware_version(ver)
        return _remember(True, f"{port} ({ver or 'no version reply'})", port, ".".join(map(str, fw)) if fw else None)
    except Exception as ex:  # noqa: BLE001
        return _remember(False, (f"Cannot open {port}: {ex}. The server user must be in the serial group "
                                 "(dialout or uucp); log out and in again after adding it."), port)


def doctor(sim, busy):
    """What the server sees of the plotter connection, with the likely cause when something is wrong."""
    rep = {"sim": sim, "pyserial": serial is not None, "ports": [], "user": None, "groups": [], "ebb": None,
           "answer": None, "advice": []}
    try:
        import getpass
        rep["user"] = getpass.getuser()
    except Exception:  # noqa: BLE001
        pass
    if os.name == "posix":
        try:
            import grp
            rep["groups"] = sorted({grp.getgrgid(g).gr_name for g in os.getgroups()})
        except Exception:  # noqa: BLE001
            pass
    if sim:
        rep["advice"].append("The server runs in simulator mode: it never touches the plotter.")
        return rep
    if serial is None:
        rep["advice"].append("pyserial is missing for the server's Python: install python3-serial (python-pyserial).")
        return rep
    try:
        ports = list(list_ports.comports())
    except Exception as ex:  # noqa: BLE001
        rep["advice"].append(f"Listing serial ports failed: {ex}")
        return rep
    for p in ports:
        vp = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None else None
        rep["ports"].append({"device": p.device, "id": vp, "description": p.description, "ebb": (p.vid, p.pid) == EBB})
    ebb = next((p for p in rep["ports"] if p["ebb"]), None)
    if not ebb:
        boot = any(p["id"] == "04d8:003c" for p in rep["ports"])
        rep["advice"].append("The board is in bootloader mode (04d8:003c): press its reset button or power-cycle it."
                             if boot else
                             "No EBB (04d8:fd92) on USB. Check the USB cable, then switch the plotter off and on. "
                             "A charge-only cable shows nothing at all.")
        return rep
    rep["ebb"] = ebb["device"]
    if os.name == "posix" and not os.access(ebb["device"], os.R_OK | os.W_OK):
        rep["advice"].append(f"The server user cannot open {ebb['device']}: add it to the serial group (dialout or uucp), "
                             "then log out and in (or restart the service).")
    if busy:
        rep["answer"] = "not asked: a job is using the plotter"
        return rep
    ok, msg = precheck(False)
    rep["answer"] = msg
    if ok and LAST and LAST.get("firmware"):
        fw = tuple(int(x) for x in LAST["firmware"].split("."))
        if fw < (3, 0, 0):
            rep["advice"].append(f"Firmware {LAST['firmware']}: works, but Stop waits for the move in flight and "
                                 "positions are not read back. 3.x adds both.")
    elif not ok:
        rep["advice"].append(msg)
    if not rep["advice"]:
        rep["advice"].append("All good: the board answers.")
    return rep


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
    def _port_id(port):       # not "_ident": threading.Thread keeps the thread id in self._ident
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
                ident = None
                port = find_port()
                ident = self._port_id(port) if port else None
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


# A push button between B0 and GND, for checking the pen height by eye. The firmware counts B0 pulled low as a PRG
# press ("alt PRG", on by default, B0 has a pull-up) and keeps it until QB reads it, so polling cannot miss a tap.
# First press: motors locked, pen down at machine.json's pen_down. Second press: pen up, motors released.
# Only between jobs; a press during a job is thrown away, and no job starts while the pen is down.
BUTTON_POLL = 0.2        # s between QB reads
BUTTON_STALE = 1.0       # s without a read (a job ran, the port was busy): whatever QB latched is old, drop it
BUTTON_AUTO_UP = 120.0   # s: the pen goes back up by itself if the second press never comes


class PenButton(threading.Thread):
    def __init__(self, settings, runner, lock, log, interval=BUTTON_POLL):
        super().__init__(daemon=True, name="pen-button")
        self.s, self.runner, self.lock, self.log, self.interval = settings, runner, lock, log, interval
        self.down = False          # pen down and motors locked by the button
        self.since = 0.0
        self.armed = True          # False until the button is let go after a press (a long hold is one press)
        self.last_read = 0.0

    def hold_reason(self):
        """For JobRunner.reserve: why no job may start now, or None."""
        return ("The pen is down for a height check: press the plotter's button again to raise it first."
                if self.down else None)

    def poll(self, ser, now):
        """One look at the button over an open port. -> 'down', 'up' or None."""
        ser.reset_input_buffer()
        q = _ask(ser, "QB")
        pressed = bool(q) and q[0].split(",")[-1].strip() == "1"
        stale = now - self.last_read > BUTTON_STALE
        self.last_read = now
        if not self.armed:
            pi = _ask(ser, "PI,B,0", one_line=True)
            if pi and pi[0].split(",")[-1].strip() == "1":
                self.armed = True
            return None
        if self.down and now - self.since > BUTTON_AUTO_UP:
            self._up(ser)
            self.log("warn", f"Pen check: no second press in {BUTTON_AUTO_UP:.0f} s, pen raised and motors released.")
            return "up"
        if not pressed or stale:
            return None
        self.armed = False
        if self.down:
            self._up(ser)
            self.log("info", "Pen check: pen up, motors released.")
            return "up"
        with self.runner.lock:              # atomic with JobRunner.reserve: a job and the pen check never overlap
            if self.runner.busy():
                return None
            self.down, self.since = True, now
        servo = self.s.machine()["servo"]
        for line in ("EM,1,1", f"SC,4,{int(servo['pen_up'])}", f"SC,5,{int(servo['pen_down'])}", "SP,0,300"):
            _ask(ser, line)
        self.log("info", "Pen check: motors locked, pen down. Press the button again to raise it.")
        return "down"

    def _up(self, ser):
        _ask(ser, "SP,1,300")
        time.sleep(0.35)
        _ask(ser, "EM,0,0")
        self.down = False

    def run(self):
        extra = {"exclusive": True} if os.name == "posix" else {}
        while True:
            time.sleep(self.interval)
            try:
                if self.runner.busy() or not self.lock.acquire(timeout=0.1):
                    continue
                try:
                    if self.runner.busy():
                        continue
                    port = find_port()
                    if not port:
                        self.down = False            # unplugged or off: the board comes back with the pen up
                        continue
                    with serial.Serial(port, 9600, timeout=0.5, **extra) as ser:
                        self.poll(ser, time.monotonic())
                finally:
                    self.lock.release()
            except Exception as ex:  # noqa: BLE001
                if self.down:
                    self.log("error", f"Pen check: lost the plotter with the pen down ({ex}). Switch it off and on.")
                self.down = False
                time.sleep(2)
