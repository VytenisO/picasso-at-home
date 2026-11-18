# Web UI feature list

Standing checklist for the plotter web UI (`tools/web`, `tools/plotweb`). Each row is a feature with its current state
and priority. Update the row when a feature lands or a decision changes; do not delete rows, mark them.

Coverage was checked against the feature set of a mature commercial laser tool (LightBurn 2.2 reference docs, its
tags index and its legacy feature comparison, read 2026-11-18). That tool was used only as a list of things a
complete UI should cover. Every description here is our own, and the design follows `ui-design.md`, not theirs.
Laser ideas are translated to pen terms (power -> pressure, frame -> pen-up trace, material library -> pen and
paper library, focus test -> pen-height test, interval -> hatch spacing, passes -> repeat strokes).

**Status:** `HAVE` works today Â· `PART` partly there (the note says what is missing) Â· `TODO` to build Â·
`SKIP` not needed (reason given).
**Priority** (for `PART`/`TODO`): `P0` first, before everything else Â· `P1` core, the UI feels incomplete without it Â· `P2` strong QoL Â· `P3` later or
niche.

## Machine facts that shape the list

- EBB firmware 2.0.1: no e-stop, no position readback, no limit switches, no homing. Position is the commanded
  position from a hand-placed start point. Any jog or click-to-move must update that tracked position and clamp to
  travel; anything that breaks tracking (raw console commands, a manual push) must mark the position unknown.
- Servo lift with variable height = pressure. No Z axis, no laser power, no air, no rotary.
- `plot_svg.py` stays the only executor. A richer editor composes its scene into one flattened SVG (plus layer
  settings) and goes through the existing preview -> plan id -> confirm -> start path. The safety model in
  `ui-design.md` does not change.

## Build order (proposed)

0. **Firmware.** (Crash homing was planned here and dropped on 2026-11-18 after the user watched it run on the
   machine; the start corner stays a hand park.) Upgrade the EBB from 2.0.1 to stock 3.x (prebuilt image, no
   compiler needed) for `ES` (instant stop), `QS` (step
   counter readback), `QM` (motion idle) and `HM` (absolute move). The driver detects them from `V` and falls back
   to timed waits on 2.0.1, so the UI does not wait on the upgrade. The pen-depth pot patch (`firmware/`) comes
   after the stock upgrade is proven.
1. **Workspace split** (done 2026-11-18). Prepare and Plot as top-level tabs. Move image conversion out of the Library panel into
   Prepare, with the plugin interface and the shared ink preview. The Plot workspace keeps the current bed, layers,
   tools and run panels. Everything below is placed in one of the two.
2. **Document model** (done 2026-11-18). Several objects on the bed, each with its own transform (move, non-uniform scale, rotate,
   mirror). Add selection, undo/redo, a numeric transform panel with a 9-point anchor, duplicate/delete/copy/paste,
   align and move to page anchors, and saving the project.
3. **Path planning** (done 2026-11-18). Travel optimisation (nearest neighbour, path reversal, best start point on closed paths) and
   duplicate-line removal, with a before/after time readout. A preview scrubber that shows travel moves and order,
   and start from stroke N.
4. **Machine panel** (done 2026-11-18, simulator only). Jog pad, go to X/Y, pen up/down, set start here, a "position known / unknown" indicator, and
   a calibration wizard. Then a pen and paper library editor.
5. **Layer modes** (done 2026-11-18). Line / hatch fill / contour fill per layer, with angle, spacing, crosshatch and back-and-forth
   options, and passes. SVG fills stop being ignored. Import colour -> pen mapping. Layer reorder and rename.
6. **Canvas QoL** (done 2026-11-18). Rulers, snapping to objects and guides, non-plotting guide layer, context menu, status bar with
   selection bounds.
7. **Image and text** (done 2026-11-18). Image adjust with side-by-side preview, centre-line trace, speck filter, crop/mask, and
   single-line fonts. Then a click-to-place text tool, booleans, offset and arrays.

---

## 1. Workspace and layout

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Two workspaces, Prepare and Plot, as top-level tabs (see `ui-design.md`) | HAVE | | user decision 2026-11-18. Prepare is optional: a finished SVG goes straight to Plot |
| Prepare: conversion methods as plugins with a parameter schema; forms built from it | HAVE | | `prepare.py` `STEPS`; each trace style is its own method with only its own settings |
| Prepare: recipe of chained steps (adjust, method, clean up; fill and pens as plot settings), saved with the result | HAVE | | reopen from the Library and re-tune; update in place or save as new |
| Ink preview: pen colour and width, hatch fills, real size, stroke count, pen-down length, time | HAVE | | `ink.js`, shared by Prepare and Plot; Ink/Lines toggle. Pen widths default to 0.5 mm until `pens.json` has `width_mm` |
| Prepare: recipe presets (named recipes to reuse on new sources) | TODO | P2 | today: Replace the source keeps the recipe |
| Prepare: undo for recipe edits | TODO | P3 | |
| Bed view drawn to machine size with paper, inset canvas and start point S | HAVE | | `bed.js` |
| Three-panel layout with console row | HAVE | | `index.html`, `app.css` |
| Phone layout with bottom tab bar | HAVE | | below 900 px; Prepare stacks preview, recipe and result |
| Dark/light/system theme | HAVE | | `app.js` |
| Collapse side panels to maximise the bed | TODO | P2 | one key toggles all panels |
| Reset panel layout | TODO | P3 | only once panels can be resized or moved |
| Resizable panels | TODO | P3 | |
| Top menu bar (File, Edit, Arrange, Tools, Machine, Help) | TODO | P2 | home for the commands below; the panels stay |
| Right-click context menu on bed and objects | HAVE | | objects: edit, turn, mirror, order, fit, zoom; empty bed: paste, select all, guides, fit |
| Tooltips that show the shortcut | PART | P2 | some titles exist; make it consistent |
| Context help (`?` on a control opens its help) | TODO | P3 | |
| Simple / advanced mode (hide advanced options) | TODO | P3 | |
| Language switch, right-to-left layout | SKIP | | one user |
| Second editor window | SKIP | | a second browser tab does it |

## 2. View, zoom and canvas aids

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Wheel zoom at cursor, pinch zoom, drag to pan | HAVE | | `bed.js` |
| Zoom in/out/fit buttons and `+ - 0` keys | HAVE | | |
| Zoom to selection | HAVE | | Z, or the context menu |
| Pan with Space+drag or middle button | PART | P3 | middle or right drag, a finger on empty bed; Space stays pause/continue |
| Cursor X/Y read-out | HAVE | | |
| Grid | PART | P2 | fixed 10 mm; make spacing a setting and add a toggle |
| Rulers on the top and left edges | HAVE | | adaptive ticks, the pointer and the selection's extent marked |
| Draggable guide lines from the rulers (never plotted, lockable, snappable) | PART | P3 | drag out of a ruler, move, drop back on a ruler to remove, right-click to add or clear; saved with the bed; not lockable |
| Automatic alignment hints while dragging (edges/centres line up) | HAVE | | a line shows what the dragged edge or centre snapped to |
| Wireframe vs filled view | TODO | P2 | matters once hatch fills exist |
| Show pen width (stroke drawn at the pen's real width) | TODO | P2 | pen library has the width; good forecast of dark areas |
| Show path direction and start points | PART | P2 | the scrubber shows the order stroke by stroke with the pen position; no arrows |
| Show travel (pen-up) moves | HAVE | | Travel toggle on the scrubber bar, T |
| Fast low-detail rendering for huge drawings | TODO | P3 | |
| Camera image of the bed as background | SKIP | | no camera; see section 15 |

## 3. Files, projects and library

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Upload by picker or drag-and-drop (12 MB) | HAVE | | `library.js` |
| SVG import (text outlined, fills ignored) | HAVE | | fills become lines: the job default hatch or each colour's own mode |
| Raster image import (png, jpg, webp, bmp, gif, tif) | HAVE | | converted on upload |
| 3D mesh import (stl, obj) as hidden-line wireframe | HAVE | | `mesh_to_svg.py` |
| PDF import | TODO | P2 | vector pages |
| DXF import | TODO | P3 | |
| HPGL/PLT import | TODO | P3 | classic plotter format |
| AI, G-code, Gerber import | SKIP | | rare for this use |
| Import report (what was skipped or changed) | PART | P2 | errors listed; add a "converted N elements, ignored M" summary |
| Paste SVG text or image from the clipboard | TODO | P2 | |
| Library list with thumbnails, kind, pen tag, date | HAVE | | |
| Rename library file | TODO | P1 | |
| Search, sort and filter library | TODO | P2 | |
| Folders or tags in library | TODO | P3 | |
| Re-convert with new parameters (saved params editable) | TODO | P1 | today a new upload is needed |
| Replace source keeping placement and settings | TODO | P2 | |
| Download original | HAVE | | "Save" button |
| Delete with confirmation; refused while plotting | HAVE | | |
| Trash / undo delete | TODO | P2 | delete is permanent today |
| Project file: scene of several objects with layers and settings | HAVE | | the bed document (`document.py`): objects with their own transform, composed into one SVG in machine mm and planned with `plot_svg.py --absolute` |
| New / open / save / save as project | HAVE | | Library tab, Projects: save the bed under a name, open (undoable), delete, clear the bed |
| Recent projects | PART | P3 | projects are listed newest first; no separate recent list |
| Templates (project that opens as a copy, keeps layer/pen setup) | TODO | P3 | |
| Autosave and recovery after reload or crash | HAVE | | the bed is saved on the server with every change and reloads on every device |
| Warn on leaving with unsaved changes | TODO | P1 | browser leave prompt |
| Project notes (free text shown on open) | TODO | P3 | pen and paper reminders |
| Export flattened plot SVG | TODO | P1 | exactly what will be plotted, in machine mm |
| Export preview as PNG | TODO | P3 | |
| Export compiled job (EBB command stream) and run it later | TODO | P3 | |
| Print to paper printer | SKIP | | |

## 4. Selection

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Click select | HAVE | |  |
| Box select: enclosing (drag right) and crossing (drag left) | HAVE | | a mouse or pen on empty bed; a finger pans |
| Shift add, Ctrl toggle | HAVE | | Shift and Ctrl both toggle |
| Select all, deselect with Esc or empty click | HAVE | | Ctrl+A; Esc only deselects, it never stops a job |
| Select all on a layer (pen) | TODO | P1 | |
| Tab cycles through objects | TODO | P3 | |
| Invert selection | TODO | P3 | |
| Select open paths, select shapes smaller than N | TODO | P3 | cleanup helpers |
| Selection drawn with marching dashes, groups distinct | TODO | P2 | |

## 5. Edit and transform

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Move by drag, arrow-key nudge 1 mm / Shift 10 mm | HAVE | | |
| Nudge distances as a setting | TODO | P3 | |
| Uniform scale by corner handles | HAVE | | aspect locked |
| Non-uniform scale (edge handles, unlock aspect) | HAVE | | edge handles stretch one way; corners keep proportions unless Shift |
| Rotate (handle, 15 degree snap with Shift, rotate 90 keys) | HAVE | | turn handle snaps to 15 degrees (Shift: free), Turn left/right buttons, typed angle |
| Flip horizontal / vertical | HAVE | | Mirror H / Mirror V about the selection's centre |
| Mirror about a drawn line | TODO | P3 | |
| Shear | TODO | P3 | |
| Numeric X, Y, W, H, rotation with 9-point anchor and aspect lock | HAVE | | X/Y/W/H are the selection's bounds at the chosen reference point; a group scales about it |
| Unit-aware and arithmetic input (`100/3`, `2in`) | TODO | P2 | |
| Undo / redo, unlimited, Ctrl+Z / Ctrl+Shift+Z | HAVE | | 200 steps per page; typing and arrow nudges coalesce into one step |
| Undo history list | TODO | P3 | |
| Copy / cut / paste / paste in place / duplicate / delete | PART | P3 | copy, cut, paste (offset each time), duplicate, delete; no paste in place |
| Group / ungroup | TODO | P2 | |
| Lock object (no move/resize/delete) | TODO | P2 | |
| Make same width / height | TODO | P3 | |
| Two-point rotate/scale (match two points to two targets) | TODO | P3 | scale a traced photo to true size |
| Warp (4-corner) and deform (grid) | TODO | P3 | |
| Edit locked while a job runs | HAVE | | |

## 6. Arrange

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Quick placement: canvas top-left, centre, fit canvas | HAVE | | `workspace.js` |
| Move to any page anchor (corners, edge midpoints, centre) | HAVE | | "To canvas ..." uses the 9-point reference |
| Align left/right/top/bottom/centres | HAVE | | to each other with several selected, to the canvas with one |
| Distribute by edges or centres | PART | P3 | Spread H/V gives equal gaps between edges; not by centres |
| Move together (abut) and dock to edge | TODO | P3 | |
| Draw order: forward/back/front/back, per-object priority | PART | P2 | objects list order (earlier/later) is the plot order within each pen; path planning (step 3) may reorder strokes |
| Grid array (rows, columns, spacing, stagger, alternate mirror) | PART | P3 | Arrange, Array: columns, rows, gaps; no stagger or alternate mirror |
| Circular array | TODO | P3 | |
| Copy along path | TODO | P3 | |
| Quick pack small items on a sheet | TODO | P3 | bounding-box packing only |
| Shape-aware nesting | SKIP | | material saving; paper is not the constraint |
| Several pages in one project | TODO | P3 | sheet sequence with paper-change holds; live mode already has New page |

## 7. Snapping

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Grid snap while dragging (0.5 mm), Shift disables | HAVE | | |
| Grid snap spacing and toggle as a setting | TODO | P2 | |
| Snap to object nodes, midpoints, centres, intersections | PART | P3 | object edges and centres while moving; no nodes, no snapping while resizing |
| Snap to paper/canvas edges and centre | HAVE | | moving snaps edges and centre to canvas, paper, guides and other objects within 7 px; Alt: none |
| Snap distance setting, modifier to disable | TODO | P3 | |

## 8. Create and edit geometry

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Polyline tool (click nodes, Shift for 45 degree steps, close on start) | TODO | P2 | |
| Bezier drawing (drag for handles) | TODO | P3 | |
| Rectangle (corner radius), ellipse, polygon, star | TODO | P2 | parametric until converted |
| Shape properties stay editable (sides, radius) | TODO | P3 | |
| Freehand drawing on the bed | PART | P2 | the writing pad plots live; add "draw into the scene" |
| Node edit: move, add, delete, corner/smooth, break at node, delete segment | TODO | P3 | large job; useful for cleanup |
| Trim at intersections (click to delete a piece) | TODO | P3 | |
| Join touching paths | PART | P1 | Prepare clean-up step "Join touching strokes" (tolerance); add a "join selected" action in Plot |
| Close nearly closed paths within a tolerance | TODO | P3 | |
| Remove duplicate and overlapping lines | PART | P2 | Job, Path planning: exact repeats of the same pen are skipped (any direction or start); partly overlapping lines are not |
| Simplify paths (tolerance, before/after node count) | PART | P2 | Prepare clean-up step "Simplify" with the point count in Result; the planner's own `--simplify 0.05` stays fixed |
| Smooth / fit arcs | TODO | P3 | |
| Break apart into separate paths | TODO | P2 | |
| Reverse path direction | TODO | P2 | |
| Set start point of a closed path | TODO | P2 | seam placement |
| Offset (in, out, both; round/miter/bevel corners) | HAVE | | Arrange, Offset: outward or inward, round, sharp or cut corners; lines get a band (shapely; python3-shapely on robotlab) |
| Booleans: union/weld, subtract, intersect | HAVE | | Arrange: union, subtract (others from the first), intersect; the result replaces the selection as a new library drawing |
| Cut shapes with a closed shape | TODO | P3 | |
| Fillet corners | TODO | P3 | |
| Convex outline around a selection | TODO | P3 | |
| Measure tool (length, size, area, path count) | TODO | P2 | length feeds time estimates |
| Clip art library (save selection, drag back in) | TODO | P3 | signatures, stamps, borders |
| Tabs, slot resizing, path-from-cut, tangent circles | SKIP | | cutting concepts or niche geometry |

## 9. Layers and plot settings

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Layers from colour and Inkscape layer | HAVE | | `plot_svg.py` |
| Per layer: enable, pen profile, speed, pressure | HAVE | | |
| Pen-swap pause between layers naming the pen | HAVE | | |
| Show/hide a layer in the view without dropping it from the plot | TODO | P2 | today "enabled" does both |
| Reorder layers (pen order) | HAVE | | Layers: arrows change the pen order; pen changes follow it |
| Rename, recolour layers | PART | P3 | rename (shown at the pen change and in Run); no recolour |
| Move objects to another layer (colour palette) | TODO | P1 | |
| Merge layers that share a pen (one swap fewer) | TODO | P2 | |
| Import colour -> pen mapping rules (remembered) | HAVE | | a colour takes the pen it had last time on a layer seen for the first time (`results/web/pen_map.json`) |
| Non-plotting guide layer | PART | P3 | guide lines; no guide shapes |
| Passes per layer (repeat strokes) | HAVE | | 1 to 5; each stroke again right away, an open one back over itself |
| Pressure step per pass | TODO | P3 | brush and felt tips |
| Layer mode: line | HAVE | | the only mode today |
| Layer mode: hatch fill (angle, spacing in mm, crosshatch, back-and-forth or one direction) | HAVE | | Layers, Filled shapes: per fill colour; spacing exact (the job default still scales by darkness) |
| Layer mode: contour fill (inward offsets, spacing) | HAVE | | `fill.contour_rings`: exact distance transform on a capped grid, then marching squares; stdlib only |
| Fill grouping: all shapes together / per group / per shape | TODO | P2 | travel and pen lifts |
| Hatch overscan (start/end outside, clipped) | TODO | P3 | hides blobs at hatch ends |
| Skip empty gaps in fills without lifting | TODO | P3 | |
| Angle increment per pass | TODO | P3 | |
| Several settings on one geometry (outline + hatch, sub-layers) | TODO | P2 | |
| Pen-width compensation (offset outlines by half the line width) | TODO | P3 | |
| Dashed / dotted line mode | TODO | P3 | pen taps at intervals |
| Dwell at stroke start/end | TODO | P3 | ink flow for dip pens; `--pen-delay` exists in the CLI |
| Overdraw closed shapes past the start point | TODO | P3 | closes the seam on felt tips |
| Lead-in/lead-out off the shape | TODO | P3 | |
| Copy/paste layer settings | TODO | P2 | |
| Per-colour default settings for new layers | TODO | P3 | |
| Speed units shown clearly (mm/s) | HAVE | | limits shown next to fields |
| Laser-only settings (power min/max, PWM, air, frequency, Z focus) | SKIP | | no equivalent |

## 10. Path planning and preview

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Preview (dry run) with plain-language problems | HAVE | | `plotjob.py` |
| Stats: size, time, strokes, metres, pens, swaps, speeds | HAVE | | |
| Plan binding: start only the previewed plan | HAVE | | |
| Order by layer (pen) | HAVE | | |
| Reduce travel (nearest neighbour or better) | HAVE | | `pathplan.py`, greedy nearest next with a cell grid; on by default (Job, Path planning). No 2-opt yet |
| Allow path reversal | HAVE | | open strokes are reversed when their far end is nearer |
| Best start point on closed paths (prefer corners) | PART | P3 | closed paths start at the vertex nearest the pen; no preference for corners |
| Order by group (finish one object before the next) | TODO | P2 | |
| Order by priority, pin some objects first | TODO | P2 | |
| Inner shapes first | TODO | P3 | |
| Sweep direction (e.g. top to bottom so the hand never touches wet ink) | TODO | P2 | |
| Same approach direction (hide backlash) | TODO | P3 | |
| Ordering panel with before/after travel and time | HAVE | | Run shows pen-up metres and time against the file order |
| Scrub through the plot with a slider; play at 0.1x to 40x | PART | P3 | slider over the plan order with Play (about 12 s for the whole plan); no speed choice |
| Travel moves shown in preview | HAVE | | Travel toggle (T) and while scrubbing, dashed |
| Pressure shown as line shade | TODO | P3 | |
| Legend by pen | TODO | P2 | |
| Estimates calibrated against real runs (time scale factor) | TODO | P2 | |

## 11. Run control

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Simulate (watchable, pausable) | HAVE | | |
| Trace frame (pen-up bounding box, back to S) | HAVE | | |
| Frame as convex hull or per object | TODO | P3 | |
| Repeat frame until stopped | TODO | P3 | while lining up paper |
| Start plot with three-item checklist | HAVE | | |
| Editable checklist items | TODO | P3 | |
| Pause at stroke end, continue, return to start, stop | HAVE | | |
| Honest stop and kill escalation | HAVE | | `runner.py`. Instant stop with `ES` after the firmware upgrade |
| Progress, elapsed, ETA, current layer/pen/speed/pressure | HAVE | | |
| Stale-connection warning | HAVE | | |
| Start from stroke N (resume an interrupted or stopped job) | HAVE | | Start here on the scrubber; after a stopped plot of the same bed, Run offers Resume there (the stroke cut short is drawn whole) |
| Plot only selected objects or layers | PART | P1 | layers via enable; add "plot selection" |
| Repeat job N times with a paper-change hold between | TODO | P2 | cards, batches |
| Live speed override during a run | TODO | P3 | applies from the next chunk |
| Finish position: S, a named spot, or stay | PART | P2 | always returns to S |
| Job history (what ran, when, result, duration) | TODO | P2 | results/ has logs; surface them |
| Job queue | SKIP | | one machine, one operator; repeat N covers batches |
| Done / pen-swap notifications (browser, sound, ntfy push) | TODO | P2 | long plots run unattended between swaps |
| Delays before the first stroke and after the last | TODO | P3 | |

## 12. Machine control

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Read-only plotter check (`V`) before hardware starts | HAVE | | `/api/plotter` also exists but the UI never calls it; show it |
| Connection status in the top bar (simulator/hardware, link) | HAVE | | |
| Plotter connection doctor (list ports, test `V`, likely causes) | TODO | P2 | |
| Port auto-detect / picker | PART | P3 | auto-detect in `ebb_check.py`; no UI |
| Position record: at start / moved / unknown | HAVE | | |
| Jog pad: step size, speed, pen up, clamped to travel | HAVE | | Machine tab, Control: a machine job (`jog.py`); steps 0.1/1/10/50 mm, speed, arrow keys on the bed; clamped to the travel minus its edge margin |
| Go to X/Y | HAVE | |  |
| Click on the bed to move there (arms for a few seconds) | HAVE | | Click to go arms one click (no timeout) |
| Move to selection anchor (check placement) | TODO | P2 | |
| Pen up / pen down buttons | HAVE | | in machine control |
| Short "pen touch" at layer pressure | TODO | P3 | time-limited |
| Motors off (release to park by hand) | HAVE | | Finish here / Finish at S switch the motors off; Stop too |
| Set start here / re-zero here | HAVE | | Set S here: the carriage becomes S (pen over the paper corner) |
| Saved positions (pen-swap spot, parking, paper corner) | TODO | P3 | |
| Start-from options: S, current position, saved origin, with 9-point job anchor | TODO | P2 | |
| Live position shown on the bed during jogs | HAVE | | the carriage marker and the position pill follow every move |
| Macros: saved command sequences as buttons | TODO | P3 | must mark position unknown if they move |
| Raw EBB command line in the console | TODO | P3 | warn; mark position unknown after motion commands |
| Live writing (font or own handwriting), new page, finish | HAVE | | `live.js`, `live_plot.py` |
| Live writing: pressure field | TODO | P2 | the server accepts it already |
| Live writing: pause and return | TODO | P3 | |
| Replot a saved live session | TODO | P3 | |
| Writing pad (point or stroke mode, pad size, palm rejection), sport mode | HAVE | | `pad.js`, `mirror.py` |
| Crash homing: pen up, slow move past full travel into the upper-left stops, then travel to S | SKIP | | dropped 2026-11-18 after a supervised run on the machine (X, Y, then a full home with a dot): driving into the stops with the motor skipping for up to 30 s puts undue stress on the motors and the assembly. Code was in d80fe69, reverted |
| Home button and "position unknown -> home" prompt | SKIP | | goes with crash homing |
| Home one axis at a time | SKIP | | goes with crash homing |
| Keep motors powered after a job (option) | TODO | P2 | tracked position stays valid; cost is warm motors |
| Switch homing (two microswitches on spare EBB inputs) | TODO | P3 | no grinding; check `PI` on 2.0.1; RB2 is taken by the pen-depth pot patch |
| Limit switches during plots, alarm unlock, Z axis, rotary, dual head | SKIP | | not on this hardware |

## 13. Machine settings and calibration

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Settings page for the machine (travel, axis signs, steps/mm, servo, margins, paper) | PART | P2 | paper corner, size and margin, and steps/mm through calibration; travel and axis signs read-only; servo never (set by hand) |
| Units mm / inch for display | TODO | P3 | mm is the native unit |
| Calibration wizard: plot a known length, type the measured one, store steps/mm | HAVE | | Machine tab: draw ticks a test length apart, measure, save; refused when more than 20% off; machine.json backed up first |
| Paper edge calibration in the UI | PART | P2 | paper corner typed in, or jog to the corner and Set S here; no guided measurement |
| Squareness and backlash test patterns | TODO | P3 | |
| Lost-steps test in the UI | TODO | P3 | `lost_steps_test.py` CLI only |
| Speed and acceleration limits shown and editable within ceilings | PART | P2 | shown; editable only in files |
| Simulator time scale and estimate tuning | PART | P3 | `--sim-speed` flag only |
| Backup / restore all settings (machine, pens, macros, positions) as one file | TODO | P2 | |
| Lifetime stats (jobs, pen-down metres; pen wear) | TODO | P3 | |
| Firmware version shown | PART | P1 | read by the check; not displayed. Needed once 3.x features are used |
| EBB firmware upgrade 2.0.1 -> stock 3.x | TODO | P0 | prebuilt image from Evil Mad Scientist; confirm the bootloader update path for this v2.3 board first (a bad flash can need bootloader recovery); note the `V` output; re-check servo values, timing and calibration after |
| Driver uses `ES`, `QS`, `QM`, `HM` when `V` reports them, timed waits otherwise | TODO | P0 | `wcb_setup.py` already detects missing commands |
| Pen-depth pot firmware patch | PART | P3 | written in `firmware/`, not compiled or flashed; needs MPLAB X + C18; after the stock upgrade |
| Several machine profiles | SKIP | | one machine |

## 14. Pens, paper and tests

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Pen profiles (name, colour, speed, pressure) | HAVE | | read-only, `config/pens.json` |
| Pen library editor: create, edit, duplicate, delete | PART | P3 | create, edit, rename, delete; no duplicate |
| Pen fields: line width, best hatch spacing, dwell, notes | PART | P3 | line width, colour, speed, pressure, notes; hatch spacing and dwell are not fields |
| Paper profiles (size, type, notes), paired with pens | TODO | P2 | today one paper plus a per-job size override |
| Link a layer to a pen profile (retune updates the job) vs copy values | PART | P2 | choosing a pen copies its values today |
| Export / import pen library | TODO | P3 | |
| Stroke and circle test sheet | HAVE | | |
| Infill (hatch spacing) test sheet | HAVE | | |
| Pressure test sheet | HAVE | | |
| Record results into the pen profile | HAVE | | |
| Pen height test (toggle, sweep) | HAVE | | |
| Two-parameter grid test (e.g. speed x pressure) with value labels | TODO | P2 | one sheet instead of several runs |
| Place a test sheet anywhere on the paper | PART | P2 | placed like any drawing; add "fit in the free corner" |
| Tone ramp test (hatch spacing -> perceived grey) | TODO | P2 | feeds the image pipeline's tone mapping |
| Test presets | TODO | P3 | |

## 15. Images and tracing

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Convert styles: contour, line art, halftone, one-line | HAVE | | `image_to_svg.py` |
| Parameters: inks, levels, width, threshold, density, blur, single pen | HAVE | | |
| Live conversion preview with mask and stats | HAVE | | |
| Side-by-side or overlay original vs result, synced zoom | PART | P3 | Prepare: Compare, Overlay with fade, Mask. Zoom is not synced between the panes |
| Brightness, contrast, gamma before converting | HAVE | | Prepare adjust step "Tone" |
| Sharpen / denoise | HAVE | | Prepare adjust step |
| Invert (light ink on dark paper) | HAVE | | Prepare adjust step |
| Crop region | PART | P3 | Prepare crop step by edge percentages; no drag-a-box crop yet |
| Rotate and mirror the image | HAVE | | Prepare adjust step |
| Mask by a vector shape | TODO | P3 | |
| Centre-line trace (single stroke down a line) | TODO | P1 | one pass per drawn line instead of a double outline |
| Edge/sketch trace for photos of drawings | TODO | P2 | |
| Ignore specks under N px | HAVE | | trace setting, plus the "Remove short strokes" clean-up step in mm |
| Smoothness / simplify tolerance | HAVE | | trace setting and trace resolution, plus the "Simplify" clean-up step in mm |
| Brightness window (lower and upper cutoff) | TODO | P3 | |
| Transparent PNG handled as background | HAVE | | Prepare flattens transparency onto white paper |
| Dither styles drawn as stipple dots (error diffusion, ordered) | TODO | P2 | the image research names stipple/TSP as a tone option |
| Variable pressure by tone | TODO | P3 | servo is coarse; hatch density is the main tone tool |
| Hatch angle per tone level / crosshatch for darks | PART | P2 | in halftone; expose angles |
| Image presets (named parameter sets) | TODO | P2 | |
| Save the processed (pre-vector) image | TODO | P3 | debugging the pipeline |
| Batch convert many images | TODO | P3 | |
| Mesh view parameters (yaw, pitch, width) | HAVE | | add max height (CLI only) |
| 3D model to depth map | SKIP | | depth engraving concept |
| Rasterise vectors | SKIP | | output is vectors |

## 16. Text

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Text in a font: wrap to canvas width, size, bold, italic, missing glyphs reported | HAVE | | |
| Text art: arc, wave, rise, circle, twist | HAVE | | |
| Bundled fonts plus system fonts | HAVE | | |
| Own handwriting in live writing | HAVE | | captured with the separate handwriting page |
| Single-line (stroke) fonts | HAVE | | four Hershey fonts (sans, sans double, script, serif) in `tools/hershey/`; Prepare method Single-line text and the bed's Text tool |
| Click-to-place text object on the bed, edit in place | PART | P3 | Text on the bed toolbar: click, type, placed there (via the library); no edit in place |
| Letter spacing, line spacing, alignment (left/centre/right, top/middle/bottom) | PART | P3 | single-line text: line spacing and left/centre/right; no letter spacing |
| Weld overlapping letters (outline fonts) | TODO | P3 | |
| Text on a path | TODO | P3 | |
| Fit text to a width (squeeze) | TODO | P3 | |
| Font browser with favourites and search | TODO | P3 | |
| Own handwriting as a text font outside live writing | TODO | P2 | use the samples in the text panel |
| Serial numbers, dates, CSV merge (variable text) | TODO | P3 | name tags, numbered cards |
| QR / bar code generator | TODO | P3 | |
| Right-to-left text | SKIP | | |

## 17. Console, logs and diagnostics

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Console with level filter, search, auto-scroll pause, copy, clear | HAVE | | `console.js` |
| History replay on reload, SSE resume | HAVE | | |
| Download full log | TODO | P3 | |
| Diagnostic bundle (versions, config, last log) | TODO | P3 | |
| About: app, server and firmware versions | TODO | P3 | |
| Usage telemetry | SKIP | | none |

## 18. Settings and shortcuts

| Feature | Status | Pri | Notes |
|---|---|---|---|
| UI preferences in local storage (theme, tab, console) | HAVE | | |
| Preferences page (units, grid, nudge, snapping, defaults) | TODO | P2 | |
| Shortcuts: `?` `P` Space `Shift+S` `+ - 0` arrows Esc | HAVE | | |
| Editing shortcuts (undo, redo, copy, paste, duplicate, delete, select all, rotate, flip, group) | TODO | P1 | with the document model |
| Shortcut list in the help dialog kept in sync | PART | P2 | written by hand; generate from one table |
| Rebind shortcuts | TODO | P3 | |
| First-run setup wizard (connect, park, calibrate, pen height) | TODO | P3 | |

## 19. Integration

| Feature | Status | Pri | Notes |
|---|---|---|---|
| HTTP API used by the UI | HAVE | | `ui-design.md` |
| Documented API for scripts (upload, preview, start) | PART | P3 | table exists in `ui-design.md` |
| Inkscape extension: send the current drawing to the plotter | TODO | P3 | |
| Login | SKIP | | decided: the tailnet is the boundary |

## 20. Registration and large work

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Register on existing marks (two points: shift, rotate, scale) | TODO | P3 | jog to two marks; second pass on re-inserted paper. Position is software-tracked, so accuracy is limited by hand parking |
| Tiled plotting larger than the bed (plot, shift paper, register) | TODO | P3 | |
| Centre finder for round objects | TODO | P3 | |
| Overhead webcam overlay, lens and alignment calibration | SKIP | | no camera; revisit if one is added (plot a registration pattern with the pen, detect it, compute the mapping) |
| Feeder, rotary, cylinder and taper correction, galvo marking | SKIP | | not this hardware |
