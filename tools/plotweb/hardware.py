"""The only places the server itself talks to the EBB. Both are skipped entirely in simulator mode.

  precheck()   read-only 'V' query: is the plotter there and answering?
  last_resort_safe()  after a job process had to be killed: pen up and motors off, best effort
"""
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
