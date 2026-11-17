# picasso-at-home

Pen plotting on an Evil Mad Scientist WaterColorBot v2, driven directly over EBB serial from plain Python. No RoboPaint, no Inkscape extension, no Node.

Status: SVG plotting works end to end on real hardware. The photo-to-pen-paths conversion pipeline is the next phase; the survey of methods is in `research/image-conversion.md`.

## Hardware

- WaterColorBot v2: EiBotBoard (EBB), two independent steppers, servo pen lift, used here with a pen.
- Tested on **EBB firmware 2.0.1**. This firmware has no `QM`, `QS` or `ES`, so there is **no hardware e-stop**: to abort immediately, cut the plotter's power. The tools provide a software e-stop (`Ctrl-C`, or the web UI button) that stops sending commands, raises the pen and releases the motors, but an already-sent move still finishes. The tools use only `V`, `EM`, `SC`, `SP`, `SM` and `CS`.
- The EBB has no limit switches. You place the carriage by hand at the start point before each run.

## Setup

```
pip install -r requirements.txt
python3 tools/ebb_check.py          # finds the EBB (04d8:fd92) and prints its firmware version, read-only
python3 tools/wcb_setup.py          # guided servo + axis + steps/mm calibration; writes results/*.json
```

On Linux your user needs access to the serial port (group `uucp` or `dialout`). Put the calibration result in `config/machine.json` (steps per mm, axis directions, servo pen-up/pen-down values, travel).

## Web interface over Tailscale

A small stdlib web server exposes the plotter on the tailnet: upload SVGs (or raster images, converted to pen paths), preview, dry-run, trace and plot from a browser. Tailscale handles the private network; a shared password can guard the UI (on by default), and real trace/plot always requires a typed confirmation plus a clean preflight dry run. Set `PLOTTER_WEB_AUTH=off` in `~/.config/plotter-web.env` to drop the login gate on a trusted tailnet; the physical-motion gates stay.

On this machine Tailscale is already up (`robotlab`, `100.75.242.85`), so only the server is needed.

```
# one-off (binds to the tailscale IP, port 8080)
/usr/bin/python3 tools/plot_server.py

# persistent (user systemd unit)
mkdir -p ~/.config
printf 'PLOTTER_WEB_PASSWORD=choose-a-long-random-password\n' > ~/.config/plotter-web.env
chmod 600 ~/.config/plotter-web.env
systemctl --user enable --now plotter-web     # unit: config/plotter-web.service
```

Open `http://100.75.242.85:8080/` from any device on the tailnet (or use the MagicDNS name `http://robotlab:8080/`). If no password is configured, the server generates a random one and prints it to the console/journal on first start. Override with `PLOTTER_WEB_PASSWORD`, `PLOTTER_WEB_HOST` and `PLOTTER_WEB_PORT`. For HTTPS, put `tailscale serve 8080` in front of it.

**Serial permission:** uploads, previews and dry runs work without it, but real trace/plot need access to the EBB. `robotlab` has been added to `dialout` in `/etc/group`, but the `systemd --user` manager on this machine started before that change and still holds the old group list, so the UI shows "plotter not ready". Log out and back in (or reboot), then `systemctl --user restart plotter-web`. Alternatively, with sudo, add a udev rule: `SUBSYSTEM=="tty", ATTRS{idVendor}=="04d8", ATTRS{idProduct}=="fd92", MODE="0666"` in `/etc/udev/rules.d/99-ebb.rules`, then `sudo udevadm control --reload-rules && sudo udevadm trigger`. The server does a read-only EBB check before starting any job and refuses to move the machine if that check fails.

### E-STOP and paper placement

**E-STOP.** The red button in the header (or `Ctrl-C` in the terminal) aborts the running job, raises the pen and releases the motors, and marks the job stopped. It stays reachable even if the web session has expired. Firmware 2.0.1 has no `ES`/`QM`/`QS`, so a move already sent still finishes — **cutting power is the only instant stop**. After an e-stop the carriage position is unknown: place it by hand before any new run.

**Paper placement.** The bed is larger than the paper, with empty space around it. `config/machine.json` defines the paper rectangle (`paper.x0`, `paper.y0`, `width`, `height`; tune these to your actual sheet) and a safe inset (`paper.margin_mm`, 15 mm by default). Only the resulting **canvas** is used: raster uploads are auto-fitted to it and dry runs refuse any drawing that leaves it, so the whole sheet is never used. The UI draws the paper, a dashed canvas rectangle, a dashed centre cross and an orange dot for the carriage start, and warns when the carriage is off the canvas.

You choose **where the carriage sits** with the anchor: `top-left` (the sheet's corner), `top-center` (the middle of the sheet's top edge — easiest to eyeball, and the default in the UI), or `center` (the middle of the sheet). The anchor is a point on the **paper**; the drawing is centred on the corresponding point of the **canvas** and inset by the margin automatically. So you can place the carriage at the sheet's top edge (Y=6 mm here) without having to find the exact corner or measure the inset yourself. Start X/Y are filled in automatically from the anchor and paper size, but can be overridden by hand. A dry run refuses to proceed if the carriage or the drawing would be off the paper, so a carriage left in the empty strip is caught before any motion. CLI equivalents: `--paper-x0/-y0/-width/-height`, `--start-x/--start-y` and `--anchor {top-left,top-center,center}`.

The UI writes uploads to `input/` (gitignored) and logs to `results/`. Actions call `tools/plot_svg.py` as a subprocess, so all the safety checks and limits there still apply. Only one plot job runs at a time.

### Drawing styles and ink layers

Selecting a raster shows a **live preview** before you upload: the actual pen paths on the left and the dark-shape mask on the right. Change style, cutoff, density, blur, width or ink layers and the preview refreshes automatically; nothing is saved until you press *Upload this image with these settings*. SVG uploads still go straight in.

Raster uploads offer three styles (`tools/image_to_svg.py`, Pillow only):

- **Contours** — grayscale → blur → marching squares → simplified polylines (the original method). A higher cutoff traces more nested outlines.
- **Halftone dots** — a grid of round dots whose size follows local darkness; Density controls dot fineness.
- **One-line drawing** — a single continuous serpentine line that packs tightly inside the dark shapes. **Dark-shape cutoff** decides which parts count as the subject (a black/white mask of them is shown next to the preview), and **Density** sets the number of passes. Raise the cutoff to include more of the image, lower it to keep only the biggest dark shapes.

**Tone levels** are a pre-processing step before routing: with `--levels 2`–`6` the grayscale is posterised into that many discrete values first, so a photo becomes nested bands of tone (2 values is plain black/white). The right-hand preview image shows the posterised tones when enabled, otherwise the binary dark-shape mask. `--max-width-mm` / `--max-height-mm` fit the output inside the reachable canvas.

Any style can be split into **1–4 ink layers** (Black; Black+Blue; Black+Blue+Red; CMYK-ish). Each ink becomes a labelled SVG layer with a stroke colour. The UI shows a swatch per layer, and **Plot** draws the layers one at a time, stopping with the pen up at each change until you swap pens by hand and press *Continue*. Trace and dry run ignore colours. Manually uploaded SVGs get the same treatment: `stroke`/`style` colours and Inkscape layer groups (`inkscape:groupmode="layer"`) are honoured, inherited from parent groups.

Use the converter standalone too:

```
python3 tools/image_to_svg.py photo.jpg --out photo.svg --style one-line \
    --threshold 150 --density 80 --inks 2 --mask photo_mask.png

python3 tools/plot_svg.py photo.svg                 # dry run reports the ink layers and planned pen swaps
python3 tools/plot_svg.py photo.svg --mode plot     # pauses at each pen change; type CONTINUE to resume
```

This is still a quick way to get a photo onto paper, not a replacement for a dedicated converter such as [DrawingBots' Image to SVG](https://drawingbots.com/tools/image-to-svg/).

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
- `tools/plot_server.py` + `tools/web/index.html` the tailnet web interface (stdlib only)
- `tools/image_to_svg.py` basic raster → pen-path SVG converter (Pillow)
- `config/machine.json` calibration baseline (40 steps/mm at 1/16 microstepping on the author's unit; measure your own)
- `config/plotter-web.service` optional user systemd unit for the web server
- `research/image-conversion.md` survey of image-to-pen-path methods, papers and repos

## License

MIT, see `LICENSE`.
