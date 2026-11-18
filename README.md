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

A small stdlib web server exposes the plotter on the tailnet: upload SVGs (or raster images, converted to pen paths), preview, simulate, trace and plot from a browser. Tailscale handles the private network; a shared password can guard the UI (on by default), and real trace/plot always requires a current preview, a park confirmation and a clean preflight dry run. Set `PLOTTER_WEB_AUTH=off` in `~/.config/plotter-web.env` to drop the login gate on a trusted tailnet; the physical-motion gates stay.

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

**Serial permission:** uploads, previews and simulations work without it, but real trace/plot need access to the EBB. `robotlab` has been added to `dialout` in `/etc/group`, but the `systemd --user` manager on this machine started before that change and still holds the old group list, so the UI shows "plotter not ready". Log out and back in (or reboot), then `systemctl --user restart plotter-web`. Alternatively, with sudo, add a udev rule: `SUBSYSTEM=="tty", ATTRS{idVendor}=="04d8", ATTRS{idProduct}=="fd92", MODE="0666"` in `/etc/udev/rules.d/99-ebb.rules`, then `sudo udevadm control --reload-rules && sudo udevadm trigger`. The server does a read-only EBB check before starting any job and refuses to move the machine if that check fails.

### The workspace

One page: the **bed** in the middle (bed, paper, the dashed inset canvas, the start point **S**, the drawing, plotted progress and the carriage), the **Library / Layers / Tools** on the left, **Status / Run / Placement** on the right and the **console** at the bottom. On a phone the same panels are tabs at the bottom. Dark and light follow the system; the Theme button overrides it. Press `?` for help and shortcuts.

1. Add a drawing (SVG, image or 3D model). Images and models show a conversion preview first.
2. Move it on the bed (drag it, drag a corner to resize, arrow keys, or type X, Y, width). It must stay inside the canvas.
3. In **Layers**, choose the pen, speed and pressure per ink layer, or switch a layer off. Switching a layer off does not move the others.
4. **Preview** runs `plot_svg.py` as a dry run and lists any problem in plain words; it runs automatically after every change. **Simulate** runs the plan on the virtual machine (nothing moves) and animates it on the bed.
5. Park the carriage by hand at **S** (the paper's top-left) with the motors off and the pen up, press **Start plot**, tick the three checks, start. Between layers the plot pauses and asks for the next pen; press Continue when it is in.

**Stopping.** Firmware 2.0.1 has no `ES`/`QM`/`QS`, so there is no emergency stop. **Stop** (always in the top bar while a job runs, `Shift+S`, reachable even with an expired session) lets the move already sent finish (normally within 3 s), raises the pen and releases the motors; the carriage stays where it stopped and must be slid back to S by hand. **Pause** waits for the end of the current stroke, lifts the pen and keeps the motors on. **Return to start** finishes the current stroke and travels back to S. **Cutting power is the only instant stop.** The carriage position shown is the commanded one; there is no readback.

**Safety model.** The server never sends motion itself; every run is `plot_svg.py` with all its checks (speed ceilings 40 mm/s drawing and 200 mm/s pen up, pressure 0 to 0.9, carriage on the paper, drawing inside the canvas and the physical travel). A run starts only from a preview the operator saw: the preview's plan id covers the drawing, machine.json and every setting, and the run uses copies of exactly those inputs. Starting needs the park confirmation and a fresh preflight; one job runs at a time. The one exception is the pen-height tool: it runs `pen_test.py`, which moves only the servo, behind a generic safety confirmation with no preview or park check.

**Placement.** The UI always parks the carriage at the paper's top-left (anchor `top-left`) and expresses placement as `--x0/--y0/--width`. The paper rectangle and margin come from `config/machine.json`; a job can use a different sheet size from the same corner. The CLI keeps `--anchor`, `--start-x/-y` and `--region-*` for other set-ups.

**Server.** `tools/plot_server.py` is a thin entry point over `tools/plotweb/` (job runner, event stream, previews, converter and tool registries). Run it without hardware with `--sim` (every job is `plot_svg.py --sim`, the serial port is never opened). Design, API and event contract: `docs/ui-design.md`. Tests: `python3 -m unittest discover -s tests` (simulator only).

### Drawing styles and ink layers

The bed shows the plotted strokes exactly as `plot_svg.py` will draw them (from its `--preview-json` dry run), and during a run the strokes already drawn and the carriage position (`--track`).

Selecting a raster shows a **live preview** before you upload: the actual pen paths and, below them, the dark-shape mask. Change style, cutoff, density, blur, width or ink layers and the preview refreshes automatically; nothing is saved until you press *Add to library*. SVG uploads still go straight in.

Raster uploads offer three styles (`tools/image_to_svg.py`, Pillow only):

- **Contours** — grayscale → blur → marching squares → simplified polylines (the original method). A higher cutoff traces more nested outlines.
- **Halftone dots** — a grid of round dots whose size follows local darkness; Density controls dot fineness.
- **Line art (centreline)** — for technical drawings / scanned line art: threshold the dark strokes, thin them to a one-pixel skeleton (Zhang–Suen, pure Python) and plot each as a single stroke. Use a high cutoff (200–230), blur 0–0.5 and Continuous tone levels. This avoids the double outlines that contour tracing produces on thin lines, and needs only one pen even when a colour version of the scan shows more detail.
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

Supported SVG: `path` (no arcs), `line`, `polyline`, `polygon`, `rect`, `circle`, `ellipse`, plus `translate`/`scale`/`rotate`/`skew`/`matrix` transforms on elements and groups. Strokes are drawn in file order; pre-sort with something like `vpype linesort` for multi-stroke art.

### Technical drawings (vector SVG, no thresholding)

SVGs are ingested **as vectors** — no rasterisation or thresholding. On upload, `tools/svg_vector.py` converts any `<text>`/`<tspan>` into real glyph outline paths using fontTools and the machine's installed fonts (pick a font in the Upload card; `fc-match` resolves aliases like Arial). The prepared SVG is what you see in the large preview and what gets plotted, so text won't look different on paper. Filled regions are drawn as outlines unless you set a hatch spacing (Fill in the placement panel, or `plot_svg.py --fill 0.8`): shapes with a fill colour are then filled with parallel lines, closer for darker colours, with a cross pass for near-black, and even-odd or nonzero holes respected. Shapes without a fill colour stay outlines, and the fill uses the pen of its colour like any other ink. Photos can be shaded with the raster style "Shading by hatching".

```
python3 tools/svg_vector.py technical.svg technical_plot.svg --font "DejaVu Sans"
python3 tools/plot_svg.py technical_plot.svg            # dry run
```

`fonttools` is in `requirements.txt`. Unknown elements are listed in the dry-run output; use `--ignore-unsupported` to skip them. Arc commands in path data and patterns/clips are not supported yet, so flatten those in the source tool if needed.

**3D meshes:** upload an **STL or OBJ** model to get an orthographic line drawing with hidden lines removed. `tools/mesh_to_svg.py` casts a ray from each sampled point on a mesh edge toward the camera; if a triangle blocks it, the point is hidden. Coplanar triangulation diagonals are dropped so only real silhouette and crease edges are drawn. Tune yaw, pitch and output size in the Upload card and watch the live preview. This is a pure-Python take on the approach used by [fogleman/ln](https://github.com/fogleman/ln) (which is Go; no Go toolchain is installed here). Meshes are capped at 1500 triangles.

**Text art:** the *Text art* card makes word-art from any string — pick a font, size, bold, and a bend style (plain, arc, wave, rising, circle, twist) with a bend amount. It generates real glyph outlines (nothing rasterised), previews them live, and can add the result straight to your uploads or download the SVG. From the CLI: `python3 -c "import sys; sys.path.insert(0,'tools'); from svg_vector import text_art_svg; open('art.svg','w').write(text_art_svg('HELLO',20,'DejaVu Sans','bold','normal','arc',40)[0])"`.

## Safety

Slow, bounded moves only (no single move over 3000 steps), but nothing here can stop a move already sent. Keep the power switch in reach, run a trace first, and watch the first plot.

## Layout

- `tools/` driver, calibration and check scripts (stdlib + pyserial)
- `tools/plot_server.py`, `tools/plotweb/` and `tools/web/` the tailnet web interface (stdlib server, plain ES modules)
- `tools/image_to_svg.py` basic raster → pen-path SVG converter (Pillow)
- `config/machine.json` calibration baseline (40 steps/mm at 1/16 microstepping on the author's unit; measure your own)
- `config/plotter-web.service` optional user systemd unit for the web server
- `research/image-conversion.md` survey of image-to-pen-path methods, papers and repos

## License

MIT, see `LICENSE`.

## Text in standard fonts

The web UI's Library tab has a **Text in a font** panel: paragraphs wrapped to the paper, any installed font plus the bundled Comic Neue (`fonts/`, SIL OFL; Comic Sans itself is proprietary and not included). Letters are drawn as outlines. The same engine writes live: `python3 tools/live_plot.py --font "Comic Neue"` (add `--sim` to try it, `--list-fonts` to list fonts). Without `--font`, `live_plot.py` writes in your own handwriting from the captured samples.
