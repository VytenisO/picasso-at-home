#!/usr/bin/env python3
"""Run a compiled job (an EBB command stream from `plot_svg.py --sim --mode plot --emit job.ebb`, or the web
page's Export compiled job) on the plotter, without the planner.

    python tools/run_compiled.py job.ebb --check      # read it and say what it would do; touches nothing
    python tools/run_compiled.py job.ebb              # plot: asks you to type PLOT first

The file was made for one calibration: it is refused when config/machine.json's steps/mm, axes or servo values
differ from the ones it was compiled with (compile it again). The carriage must be parked by hand at the start
point the file names, pen up, exactly as for a normal run. Ctrl+C stops: ES (on firmware 3.x), pen up, motors
off. Pen changes and copies wait for Enter.
"""
import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
EBB_VID, EBB_PID = 0x04D8, 0xFD92


def read_job(path):
    head, items = None, []
    for ln in pathlib.Path(path).read_text().splitlines():
        if ln.startswith("#HEAD "):
            head = json.loads(ln[6:])
        elif ln.startswith("#WAIT "):
            items.append(("WAIT", float(ln[6:])))
        elif ln.startswith("#PAUSE "):
            items.append(("PAUSE", json.loads(ln[7:])))
        elif ln.strip() and not ln.startswith("#"):
            items.append(("CMD", ln.strip()))
    if not head or head.get("format") != "picasso-ebb-1":
        raise SystemExit(f"{path} is not a compiled job (no picasso-ebb-1 header)")
    return head, items


def check_config(head, cfg):
    """-> list of differences between the calibration the job was made for and the machine's now."""
    diff = []
    for key in ("steps_per_mm", "axis", "servo"):
        if json.dumps(head.get(key), sort_keys=True) != json.dumps(cfg.get(key), sort_keys=True):
            diff.append(f"{key}: job {head.get(key)} vs machine {cfg.get(key)}")
    return diff


def summary(head, items):
    cmds = [v for k, v in items if k == "CMD"]
    secs = sum(v for k, v in items if k == "WAIT")
    pauses = [v for k, v in items if k == "PAUSE"]
    return (f"{len(cmds)} commands ({sum(1 for c in cmds if c.startswith('SM,'))} moves), about {secs / 60:.1f} min, "
            f"{len(pauses)} pause(s); start at S = {head['start_mm']} mm; made {head.get('made')} from {head.get('svg')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job")
    ap.add_argument("--check", action="store_true", help="read the file and compare the calibration; send nothing")
    ap.add_argument("--port")
    ap.add_argument("--config", default=str(ROOT / "config" / "machine.json"))
    a = ap.parse_args()
    head, items = read_job(a.job)
    cfg = json.loads(pathlib.Path(a.config).read_text())
    print(summary(head, items))
    diff = check_config(head, cfg)
    if diff:
        print("Made for a different calibration, compile it again:\n  " + "\n  ".join(diff))
        return 2
    if a.check:
        print("Calibration matches. Nothing sent (--check).")
        return 0

    import serial
    from serial.tools import list_ports
    port = a.port or next((p.device for p in list_ports.comports() if (p.vid, p.pid) == (EBB_VID, EBB_PID)), None)
    if not port:
        raise SystemExit("No EBB (04d8:fd92) found. Plug in/power the plotter.")
    print(f"\nCheck: carriage parked by hand at S {head['start_mm']} mm, pen UP over the paper, power switch in reach.")
    if input("Type PLOT to start: ").strip() != "PLOT":
        raise SystemExit("not confirmed, nothing sent")
    ser = serial.Serial(port, 9600, timeout=3)

    def send(line):
        ser.write(line.encode() + b"\r")
        out = []
        while True:
            r = ser.readline().decode(errors="replace").strip()
            if not r:
                break
            out.append(r)
            if r == "OK" or r.startswith("!"):
                break
        if "OK" not in out:
            raise RuntimeError(f"{line!r} -> {out}")

    ser.write(b"V\r")
    ver = ser.readline().decode(errors="replace").strip()
    has_es = "Version 3." in ver
    print(f"Firmware: {ver or 'no answer'}")
    n = sum(1 for k, _ in items if k == "CMD")
    done, status = 0, "complete"
    try:
        for kind, v in items:
            if kind == "CMD":
                send(v)
                done += 1
                if done % 50 == 0:
                    print(f"\r{done}/{n}", end="", flush=True)
            elif kind == "WAIT":
                time.sleep(v)
            else:
                what = f"put in the pen for {v.get('label') or v.get('color')}" if v.get("kind") == "swap" else \
                    f"new paper for copy {v.get('copy', 0) + 1}" if v.get("kind") == "paper" else "paused"
                input(f"\nPause: {what}. Press Enter to continue (Ctrl+C stops)... ")
    except KeyboardInterrupt:
        status = "stopped"
    except Exception as ex:   # noqa: BLE001
        status = f"error: {ex!r}"
    finally:
        if status != "complete":
            print(f"\n{status}: stopping, pen up, motors off. The position is now unknown: park by hand again.")
            for line in (["ES"] if has_es else []) + ["SP,1,300", "EM,0,0"]:
                try:
                    send(line)
                except Exception:   # noqa: BLE001
                    pass
        ser.close()
    print(f"\n{status}: {done}/{n} commands sent.")
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
