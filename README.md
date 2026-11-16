# picasso-at-home

Pen plotting on an Evil Mad Scientist WaterColorBot v2, driven directly over EBB serial from plain Python. No RoboPaint, no Inkscape extension, no Node.

Status: SVG plotting works end to end on real hardware. The photo-to-pen-paths conversion pipeline is the next phase; the survey of methods is in `research/image-conversion.md`.

## Hardware

- WaterColorBot v2: EiBotBoard (EBB), two independent steppers, servo pen lift, used here with a pen.
- Tested on **EBB firmware 2.0.1**. This firmware has no `QM`, `QS` or `ES`, so there is **no e-stop**: to abort immediately, cut the plotter's power. The tools use only `V`, `EM`, `SC`, `SP`, `SM` and `CS`.
- The EBB has no limit switches. You place the carriage by hand at the start point before each run.

## Setup

```
pip install -r requirements.txt
python3 tools/ebb_check.py          # finds the EBB (04d8:fd92) and prints its firmware version, read-only
python3 tools/wcb_setup.py          # guided servo + axis + steps/mm calibration; writes results/*.json
```

On Linux your user needs access to the serial port (group `uucp` or `dialout`). Put the calibration result in `config/machine.json` (steps per mm, axis directions, servo pen-up/pen-down values, travel).

## Plotting an SVG

```
python3 tools/plot_svg.py drawing.svg                 # dry run: report only, nothing is sent
python3 tools/plot_svg.py drawing.svg --mode trace    # pen-up trace of the bounding box
python3 tools/plot_svg.py drawing.svg --mode plot     # the real plot (you type PLOT to confirm)
```

Always go dry, then trace, then plot. Before trace or plot: motors off, carriage slid to the start point S (`--start-x/--start-y` tell the script where S is, in mm from the machine's top-left), pen up over the paper. Useful options: `--width`, `--x0/--y0`, `--speed`, `--travel-speed`, `--pen-up/--pen-down`. Add `--sim` to run any mode on a virtual machine with a virtual clock.

Supported SVG: `path` (no arcs), `line`, `polyline`, `polygon`, `rect`, `circle`. Transforms and other elements are refused rather than guessed. Strokes are drawn in file order; pre-sort with something like `vpype linesort` for multi-stroke art.

## Safety

Slow, bounded moves only (no single move over 3000 steps), but nothing here can stop a move already sent. Keep the power switch in reach, run a trace first, and watch the first plot.

## Layout

- `tools/` driver, calibration and check scripts (stdlib + pyserial)
- `config/machine.json` calibration baseline (40 steps/mm at 1/16 microstepping on the author's unit; measure your own)
- `research/image-conversion.md` survey of image-to-pen-path methods, papers and repos

## License

MIT, see `LICENSE`.
