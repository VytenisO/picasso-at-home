#!/usr/bin/env python3
"""Cycle the pen servo through its range so the pen can be set by hand (SC/SP only).

  pen_test.py            interactive: Enter toggles pen up/down, s sweeps up->down->up, q quits
  pen_test.py --sim      same on a virtual machine, nothing sent

Only the servo moves: the motors are never enabled or released, so the carriage stays free if it was
free (motors off) and stays put if it was locked. Servo values come from ../config/machine.json
(--pen-up/--pen-down override). Use it to set the pen holder height by hand until a pen-down just
touches the paper and pen-up clears it; then the config values only need fine adjustment.
Quitting raises the pen. SC values are volatile and are restored on exit.
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import plot_svg as ps  # noqa: E402

LO, HI = 7500, 22000   # same servo limits as plot_svg.py
STEPS = 10             # positions in a sweep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="virtual machine, nothing sent")
    ap.add_argument("--port")
    ap.add_argument("--config", default=str(ps.ROOT / "config" / "machine.json"))
    ap.add_argument("--pen-up", type=int, help="override servo pen-up value")
    ap.add_argument("--pen-down", type=int, help="override servo pen-down value")
    ap.add_argument("--delay", type=int, default=250, help="ms per servo move")
    a = ap.parse_args()
    a.track, a.start_x, a.start_y = False, 0.0, 0.0   # Plotter fields; unused here

    cfg = json.loads(pathlib.Path(a.config).read_text())
    up = a.pen_up or cfg["servo"]["pen_up"]
    down = a.pen_down or cfg["servo"]["pen_down"]
    for name, v in (("pen_up", up), ("pen_down", down)):
        if not LO <= v <= HI:
            sys.exit(f"servo {name}={v} outside {LO}..{HI}")
    p = ps.Plotter(cfg, a)

    def go(value, delay):
        p.cmd(f"SC,5,{value}")
        p.cmd(f"SP,0,{delay}")
        p.sleep(delay / 1000 + 0.05)

    print(f"pen up {up}, pen down {down}. Enter = toggle, s = sweep, q = quit; anything else is ignored.")
    try:
        input("Nothing has moved yet. Press Enter to raise the pen and start (Ctrl-C to cancel): ")
    except (KeyboardInterrupt, EOFError):
        print()
        return 0
    p.cmd(f"SC,4,{up}")
    p.cmd(f"SC,5,{down}")
    p.cmd(f"SP,1,{a.delay}")
    is_up = True
    try:
        while True:
            key = input(f"[{'UP' if is_up else 'DOWN'}] > ").strip().lower()
            if key == "q":
                break
            if key == "s":
                vals = [up + (down - up) * i // STEPS for i in range(STEPS + 1)]
                for v in vals + vals[-2::-1]:
                    print(f"  {v}")
                    go(v, a.delay)
                p.cmd(f"SC,5,{down}")
                p.cmd(f"SP,1,{a.delay}")
                is_up = True
            elif key == "":
                p.cmd(f"SP,{1 if not is_up else 0},{a.delay}")
                is_up = not is_up
            else:
                print("  unknown input, ignored (Enter, s or q)")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        try:
            p.cmd(f"SC,5,{down}")
            p.cmd(f"SP,1,{a.delay}")
        except Exception as ex:   # noqa: BLE001
            print(f"shutdown problem: {ex!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
