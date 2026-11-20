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

- EBB firmware 3.1.7 since 2026-11-19 (was 2.0.1). The driver still treats it like 2.0.1 until it uses `ES`/`QS`/`QM`: no e-stop, no position readback, no limit switches, no homing. Position is the commanded
  position from a hand-placed start point. Any jog or click-to-move must update that tracked position and clamp to
  travel; anything that breaks tracking (raw console commands, a manual push) must mark the position unknown.
- Servo lift with variable height = pressure. No Z axis, no laser power, no air, no rotary.
- `plot_svg.py` stays the only executor. A richer editor composes its scene into one flattened SVG (plus layer
  settings) and goes through the existing preview -> plan id -> confirm -> start path. The safety model in
  `ui-design.md` does not change.

## Build order (proposed)

0. **Firmware** (stock 3.1.7 flashed 2026-11-19; driver use of the new commands still open). (Crash homing was planned here and dropped on 2026-11-18 after the user watched it run on the
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
| Prepare: recipe presets (named recipes to reuse on new sources) | HAVE |  | Prepare, Presets: save the recipe (steps, their settings, fill) under a name, apply it to a later source of the same kind, delete; kept on the server for every browser |
| Prepare: undo for recipe edits | HAVE |  | Prepare toolbar Undo/Redo and Ctrl+Z / Ctrl+Shift+Z: steps, settings and name |
| Bed view drawn to machine size with paper, inset canvas and start point S | HAVE | | `bed.js` |
| Three-panel layout with console row | HAVE | | `index.html`, `app.css` |
| Phone layout with bottom tab bar | HAVE | | below 900 px; Prepare stacks preview, recipe and result |
| Dark/light/system theme | HAVE | | `app.js` |
| Collapse side panels to maximise the bed | HAVE |  | View, Hide side panels, or the backslash key; remembered in this browser |
| Reset panel layout | TODO | P3 | only once panels can be resized or moved |
| Resizable panels | TODO | P3 | |
| Top menu bar (File, Edit, Arrange, Tools, Machine, Help) | HAVE |  | `menubar.js`: each item presses the existing control, so it is disabled under the same conditions; hidden on narrow screens |
| Right-click context menu on bed and objects | HAVE | | objects: edit, turn, mirror, order, fit, zoom; empty bed: paste, select all, guides, fit |
| Tooltips that show the shortcut | HAVE |  | every control a key presses gets the key in its tooltip, from the shortcut table (shortcuts.js) |
| Context help (`?` on a control opens its help) | HAVE |  | Help, What's this? (Shift+F1): the next click on any control, disabled ones too, shows its explanation |
| Simple / advanced mode (hide advanced options) | HAVE |  | Preferences: Simple mode hides planning fine-tuning, line tools, macros, the EBB command line and backups |
| Language switch, right-to-left layout | SKIP | | one user |
| Second editor window | SKIP | | a second browser tab does it |

## 2. View, zoom and canvas aids

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Wheel zoom at cursor, pinch zoom, drag to pan | HAVE | | `bed.js` |
| Zoom in/out/fit buttons and `+ - 0` keys | HAVE | | |
| Zoom to selection | HAVE | | Z, or the context menu |
| Pan with Space+drag or middle button | HAVE |  | Space+drag when no job is running, middle or right drag, a finger on empty bed |
| Cursor X/Y read-out | HAVE | | |
| Grid | HAVE |  | View: off, 1, 2, 5, 10 or 20 mm (skipped when finer than 4 px on screen) |
| Rulers on the top and left edges | HAVE | | adaptive ticks, the pointer and the selection's extent marked |
| Draggable guide lines from the rulers (never plotted, lockable, snappable) | HAVE |  | drag out of a ruler, move, drop back to remove, right-click to add or clear; saved with the bed; View, Lock the guides |
| Automatic alignment hints while dragging (edges/centres line up) | HAVE | | a line shows what the dragged edge or centre snapped to |
| Wireframe vs filled view | HAVE |  | Ink (pen colour and width) or Lines (thin lines) on the bed and in Prepare |
| Show pen width (stroke drawn at the pen's real width) | HAVE |  | Ink view draws each layer at its pen's width_mm |
| Show path direction and start points | HAVE |  | View: a dot at each stroke's start and an arrowhead halfway along, in plot order, after a preview (up to 4000 strokes); the scrubber limits it to the strokes so far |
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
| PDF import | HAVE |  | Prepare: a PDF is a source; Use a page as it is takes page N in vectors through pdftocairo (poppler-utils, present on robotlab); pictures on the page are left out |
| DXF import | HAVE |  | Prepare (or drop in the Library): ASCII DXF with lines, polylines with bulges, circles, arcs, ellipses, splines; $INSUNITS; colour numbers become pens |
| HPGL/PLT import | HAVE |  | Prepare: HPGL (.plt, .hpgl) with IN, SP, PU, PD, PA, PR, CI; each pen its own colour |
| AI, G-code, Gerber import | SKIP | | rare for this use |
| Import report (what was skipped or changed) | HAVE |  | Prepare result, Imported: shapes that plot, text converted, clones (<use>) expanded, elements left out (pictures, embedded HTML). <use> is now expanded for every SVG, and unplottable elements are taken out instead of failing the plan |
| Paste SVG text or image from the clipboard | HAVE |  | Ctrl+V: SVG text or an SVG file goes into the library and onto the bed, an image to Prepare; otherwise the bed pastes copied objects |
| Library list with thumbnails, kind, pen tag, date | HAVE | | |
| Rename library file | HAVE | | Library: Rename; the name is cleaned the way uploads are, the extension kept |
| Search, sort and filter library | HAVE |  | Library: name search, kind filter, newest, oldest or A-Z; remembered in this browser |
| Folders or tags in library | HAVE |  | Library: Tags per drawing; filter by tag, search finds tags |
| Re-convert with new parameters (saved params editable) | HAVE | | Library: Prepare reopens a prepared drawing's recipe with its settings; save replaces it |
| Replace source keeping placement and settings | HAVE |  | Library: Replace loads a new SVG into an SVG, shape or text drawing under the same id; bed objects keep their place and width and take the new proportions. Prepared drawings: replace the source in Prepare |
| Download original | HAVE | | "Save" button |
| Delete with confirmation; refused while plotting | HAVE | | |
| Trash / undo delete | HAVE |  | Delete moves to the Trash (30 days, then gone); Restore, Delete for good, Empty the trash. The drawing's bed objects leave as an undoable edit |
| Project file: scene of several objects with layers and settings | HAVE | | the bed document (`document.py`): objects with their own transform, composed into one SVG in machine mm and planned with `plot_svg.py --absolute` |
| New / open / save / save as project | HAVE | | Library tab, Projects: save the bed under a name, open (undoable), delete, clear the bed |
| Recent projects | HAVE |  | the five opened last come first, marked recent |
| Templates (project that opens as a copy, keeps layer/pen setup) | HAVE |  | Projects: Save as a template; opening one gives a fresh unnamed copy with its objects, pens and settings |
| Autosave and recovery after reload or crash | HAVE | | the bed is saved on the server with every change and reloads on every device |
| Warn on leaving with unsaved changes | HAVE | | no prompt needed: the bed is saved on the server with every change, and a change still waiting for its preview is sent as the page closes; Prepare keeps its recipe in the browser |
| Project notes (free text shown on open) | HAVE |  | Projects: notes saved with the bed, shown when the project opens and in the list |
| Export flattened plot SVG | HAVE | | Run: Export plot SVG. Every stroke after fills, in plot order, machine mm, one Inkscape layer per pen |
| Export preview as PNG | HAVE |  | Projects: Export the plan as PNG, every stroke in its pen's colour on white, 8 px/mm |
| Export compiled job (EBB command stream) and run it later | TODO | P3 | |
| Print to paper printer | SKIP | | |

## 4. Selection

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Click select | HAVE | |  |
| Box select: enclosing (drag right) and crossing (drag left) | HAVE | | a mouse or pen on empty bed; a finger pans |
| Shift add, Ctrl toggle | HAVE | | Shift and Ctrl both toggle |
| Select all, deselect with Esc or empty click | HAVE | | Ctrl+A; Esc only deselects, it never stops a job |
| Select all on a layer (pen) | HAVE | | Layers: Select on a layer card selects the objects that draw in it |
| Tab cycles through objects | HAVE |  | Tab / Shift+Tab with the bed focused; a group is one step |
| Invert selection | HAVE |  | Ctrl+I, or the bed's right-click menu |
| Select open paths, select shapes smaller than N | HAVE |  | right-click menu: Select objects with open lines, Select small objects (size asked) |
| Selection drawn with marching dashes, groups distinct | HAVE |  | the selection outline's dashes move; groups get a dotted box when selected or hovered |

## 5. Edit and transform

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Move by drag, arrow-key nudge 1 mm / Shift 10 mm | HAVE | | |
| Nudge distances as a setting | HAVE |  | View, Nudge: 0.1 to 10 mm; Shift ten times, Alt a tenth |
| Uniform scale by corner handles | HAVE | | aspect locked |
| Non-uniform scale (edge handles, unlock aspect) | HAVE | | edge handles stretch one way; corners keep proportions unless Shift |
| Rotate (handle, 15 degree snap with Shift, rotate 90 keys) | HAVE | | turn handle snaps to 15 degrees (Shift: free), Turn left/right buttons, typed angle |
| Flip horizontal / vertical | HAVE | | Mirror H / Mirror V about the selection's centre |
| Mirror about a drawn line | HAVE |  | Line tools, Mirror about a line: two clicks on the bed |
| Shear | HAVE |  | Line tools, Shear: a slant angle, about the selection's middle |
| Numeric X, Y, W, H, rotation with 9-point anchor and aspect lock | HAVE | | X/Y/W/H are the selection's bounds at the chosen reference point; a group scales about it |
| Unit-aware and arithmetic input (`100/3`, `2in`) | HAVE |  | Arrange X, Y, width, height, turn: + - * / and brackets, units mm, cm, in or ", pt, px; a decimal comma works |
| Undo / redo, unlimited, Ctrl+Z / Ctrl+Shift+Z | HAVE | | 200 steps per page; typing and arrow nudges coalesce into one step |
| Undo history list | HAVE |  | bed toolbar, History: each step with what it did and when; pick one to go back to just before it |
| Copy / cut / paste / paste in place / duplicate / delete | HAVE |  | plus Paste in place (Ctrl+Shift+V) |
| Group / ungroup | HAVE |  | Arrange Group/Ungroup, Ctrl+G and Ctrl+Shift+G: selecting one member selects the group; copies get their own group |
| Lock object (no move/resize/delete) | HAVE |  | Arrange Lock/Unlock: no handles, no drag, nudge, transform, shape operation or remove; colour and plot settings still change |
| Make same width / height | HAVE |  | Arrange, Same width / Same height: as the first object selected, around each one's centre |
| Two-point rotate/scale (match two points to two targets) | HAVE |  | Line tools, True size from two points: click two points, type their real distance; the drawing scales about the first. Full two-target matching is not there |
| Warp (4-corner) and deform (grid) | PART | P3 | Line tools, Taper / warp: each edge of the box as a percentage (bilinear); no free corner dragging or grid deform |
| Edit locked while a job runs | HAVE | | |

## 6. Arrange

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Quick placement: canvas top-left, centre, fit canvas | HAVE | | `workspace.js` |
| Move to any page anchor (corners, edge midpoints, centre) | HAVE | | "To canvas ..." uses the 9-point reference |
| Align left/right/top/bottom/centres | HAVE | | to each other with several selected, to the canvas with one |
| Distribute by edges or centres | HAVE |  | Spread H/V (equal gaps) and Centres H/V (equal steps between centres) |
| Move together (abut) and dock to edge | HAVE |  | Together H/V close the gaps; docking to an edge is Align with one object (to the canvas) |
| Draw order: forward/back/front/back, per-object priority | HAVE |  | Arrange list arrows; the list order is the plot order within each pen with planning off, or with Finish each object before the next |
| Grid array (rows, columns, spacing, stagger, alternate mirror) | HAVE |  | Arrange, Array: columns, rows, gaps, stagger of odd rows, mirror every other copy |
| Circular array | HAVE |  | Arrange, Circular array: copies in all, radius, turn with the circle or stay upright |
| Copy along path | HAVE |  | Line tools, Copy along a path: the first object selected is the path (its longest line); the rest are copied evenly along it, turned with it |
| Quick pack small items on a sheet | HAVE |  | Line tools, Pack onto the canvas: rows from the canvas's top-left, tallest first, 3 mm apart (bounding boxes) |
| Shape-aware nesting | SKIP | | material saving; paper is not the constraint |
| Several pages in one project | TODO | P3 | sheet sequence with paper-change holds; live mode already has New page |

## 7. Snapping

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Grid snap while dragging (0.5 mm), Shift disables | HAVE | | |
| Grid snap spacing and toggle as a setting | HAVE |  | View: moves and resizes snap to the grid step instead of 0.5 mm |
| Snap to object nodes, midpoints, centres, intersections | PART | P3 | object edges and centres while moving and while resizing an unturned box by an edge; no nodes or intersections |
| Snap to paper/canvas edges and centre | HAVE | | moving snaps edges and centre to canvas, paper, guides and other objects within 7 px; Alt: none |
| Snap distance setting, modifier to disable | HAVE |  | View, Snap distance (off, 4, 7, 12 px); Alt while dragging turns it off |

## 8. Create and edit geometry

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Polyline tool (click nodes, Shift for 45 degree steps, close on start) | HAVE |  | Bed, Shape, Line by clicks: points on the 0.5 mm grid, Shift for 45 degree steps, click the first point to close, double-click or Enter to finish, Backspace drops a point, Esc cancels |
| Bezier drawing (drag for handles) | TODO | P3 | |
| Rectangle (corner radius), ellipse, polygon, star | HAVE |  | Bed, Shape: size, corner radius, sides or points and inner radius, placed by a click; each becomes a library drawing (not editable as parameters afterwards) |
| Shape properties stay editable (sides, radius) | TODO | P3 | |
| Freehand drawing on the bed | HAVE |  | Bed, Shape, Freehand: drag to draw one line, smoothed to 0.3 mm, kept as a drawing; the writing pad still plots live |
| Node edit: move, add, delete, corner/smooth, break at node, delete segment | TODO | P3 | large job; useful for cleanup |
| Trim at intersections (click to delete a piece) | HAVE |  | Line tools, Trim: lines are split where they cross; the clicked piece goes |
| Join touching paths | HAVE | | Prepare clean-up step, and Arrange: Join lines (ends within a set distance, same colour, never at a junction of three or more) |
| Close nearly closed paths within a tolerance | HAVE |  | Line tools, Close nearly closed lines |
| Remove duplicate and overlapping lines | HAVE |  | Job, Path planning: exact repeats are skipped; Remove overlapping lines leaves out the parts of a stroke that run along an earlier one of the same pen (0.1 mm) |
| Simplify paths (tolerance, before/after node count) | HAVE |  | Prepare clean-up step Simplify with the point count in Result; Job, Path planning: Simplify (mm) for the plot itself, with the points in the plan |
| Smooth / fit arcs | PART | P3 | Line tools, Smooth (Chaikin corner cutting); no arc fitting |
| Break apart into separate paths | HAVE |  | Arrange, Break apart: every stroke becomes its own object (and library drawing) where it was; one undo step; at most 64 objects on the bed |
| Reverse path direction | HAVE |  | Arrange, Reverse: each stroke from its other end, last stroke first; only shows with Shorter pen-up travel off, since planning picks direction itself |
| Set start point of a closed path | HAVE |  | right-click an object: Start closed shapes here (moves with the object); otherwise the nearest vertex or a corner |
| Offset (in, out, both; round/miter/bevel corners) | HAVE | | Arrange, Offset: outward or inward, round, sharp or cut corners; lines get a band (shapely; python3-shapely on robotlab) |
| Booleans: union/weld, subtract, intersect | HAVE | | Arrange: union, subtract (others from the first), intersect; the result replaces the selection as a new library drawing |
| Cut shapes with a closed shape | HAVE |  | Line tools: the first object selected cuts the others (keep outside or keep inside); the cutter stays |
| Fillet corners | HAVE |  | Line tools, Round the corners: closed shapes, convex and concave corners, a set radius |
| Convex outline around a selection | HAVE |  | Line tools, Convex outline around: added as a new object |
| Measure tool (length, size, area, path count) | HAVE |  | Arrange: path count and line length of the selection (from the drawings' geometry), size on the bed label and in the status line; no area |
| Clip art library (save selection, drag back in) | HAVE |  | right-click: Save the selection as clip art (a library drawing of kind clip art); library drawings can be dragged onto the bed |
| Tabs, slot resizing, path-from-cut, tangent circles | SKIP | | cutting concepts or niche geometry |

## 9. Layers and plot settings

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Layers from colour and Inkscape layer | HAVE | | `plot_svg.py` |
| Per layer: enable, pen profile, speed, pressure | HAVE | | |
| Pen-swap pause between layers naming the pen | HAVE | | |
| Show/hide a layer in the view without dropping it from the plot | HAVE |  | Layers: Hide/Show on each card, this browser only; enable still decides what plots |
| Reorder layers (pen order) | HAVE | | Layers: arrows change the pen order; pen changes follow it |
| Rename, recolour layers | PART | P3 | rename (shown at the pen change and in Run); no recolour |
| Move objects to another layer (colour palette) | HAVE | | Arrange, Colour: the bed's ink colours as swatches, or any colour; Own colours undoes it. The object's lines are recoloured when the bed is composed |
| Merge layers that share a pen (one swap fewer) | HAVE |  | layers set to the same pen plot one after the other with no pen-change pause, and the swap count says so; put them next to each other in the pen order |
| Import colour -> pen mapping rules (remembered) | HAVE | | a colour takes the pen it had last time on a layer seen for the first time (`results/web/pen_map.json`) |
| Non-plotting guide layer | PART | P3 | guide lines; no guide shapes |
| Passes per layer (repeat strokes) | HAVE | | 1 to 5; each stroke again right away, an open one back over itself |
| Pressure step per pass | HAVE |  | Layers, More: Per pass, each extra pass lighter or heavier by this much |
| Layer mode: line | HAVE | | the only mode today |
| Layer mode: hatch fill (angle, spacing in mm, crosshatch, back-and-forth or one direction) | HAVE | | Layers, Filled shapes: per fill colour; spacing exact (the job default still scales by darkness) |
| Layer mode: contour fill (inward offsets, spacing) | HAVE | | `fill.contour_rings`: exact distance transform on a capped grid, then marching squares; stdlib only |
| Fill grouping: all shapes together / per group / per shape | HAVE |  | Fills: Fill order, all of an ink together, each object, or each shape finished before the next |
| Hatch overscan (start/end outside, clipped) | HAVE |  | as an inset instead: Filled shapes, Inset stops fill lines short of the edge, so the blob at each end stays inside (drawing past the edge would mark the paper) |
| Skip empty gaps in fills without lifting | HAVE |  | Filled shapes, Bridge: fill lines whose ends are this close are drawn as one stroke, pen down across |
| Angle increment per pass | HAVE |  | Filled shapes (hatch): Passes and Turn per pass |
| Several settings on one geometry (outline + hatch, sub-layers) | HAVE |  | Layers, Filled shapes: Own layer puts a colour's hatch or contour fill on a layer of its own, so the fill and the outline take different pens even in the same colour |
| Pen-width compensation (offset outlines by half the line width) | HAVE |  | Line tools, Allow for the pen width: closed shapes move inward by half the width (the layer's pen width offered) |
| Dashed / dotted line mode | HAVE |  | Layers, More: Dash drawn / Dash gap (a short dash and a long gap make dots) |
| Dwell at stroke start/end | HAVE |  | Layers, More: Dwell (ms), counted in the time |
| Overdraw closed shapes past the start point | HAVE |  | Layers, More: Overdraw (mm) |
| Lead-in/lead-out off the shape | SKIP |  | on paper a lead-in is a stray mark; Overdraw closes seams and Approach takes up backlash instead |
| Copy/paste layer settings | HAVE |  | Layers: Copy and Paste on each card (pen, speed, pressure, passes), per browser |
| Per-colour default settings for new layers | HAVE |  | a colour seen before takes the pen it had (pen memory), and with it that pen's speed and pressure |
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
| Best start point on closed paths (prefer corners) | HAVE |  | Job, Path planning: Start closed shapes at a corner; otherwise the vertex nearest the pen |
| Order by group (finish one object before the next) | HAVE |  | Job, Path planning: Finish each object before the next (within each pen), nearest stroke next inside an object; a group counts as its objects |
| Order by priority, pin some objects first | HAVE |  | with Finish each object before the next, the Arrange list order is the priority: move an object up to plot it first |
| Inner shapes first | HAVE |  | Job, Path planning: Inner shapes first (by containment, within each pen) |
| Sweep direction (e.g. top to bottom so the hand never touches wet ink) | HAVE |  | Job, Path planning, Sweep: top to bottom or left to right in 20 mm bands within each pen, nearest stroke next inside a band |
| Same approach direction (hide backlash) | HAVE |  | Job, Path planning: Approach (mm), every stroke start reached from up-left |
| Ordering panel with before/after travel and time | HAVE | | Run shows pen-up metres and time against the file order |
| Scrub through the plot with a slider; play at 0.1x to 40x | HAVE |  | slider over the plan order; Play with a speed: the whole plan in 12 s, or 0.1x to 40x the estimated plotting time |
| Travel moves shown in preview | HAVE | | Travel toggle (T) and while scrubbing, dashed |
| Pressure shown as line shade | HAVE |  | strokes at a lighter pen pressure are drawn lighter on the bed and in Prepare |
| Legend by pen | HAVE |  | Layers: Add a pen legend, single-line text of each enabled layer with its pen and speed, at the canvas's bottom-left |
| Estimates calibrated against real runs (time scale factor) | HAVE |  | the median of real over estimated time of the last 10 finished real plots (3 or more) shows next to the estimate when it is more than 5% off |

## 11. Run control

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Simulate (watchable, pausable) | HAVE | | |
| Trace frame (pen-up bounding box, back to S) | HAVE | | |
| Frame as convex hull or per object | HAVE |  | Job, Trace frame: the drawing's box, its convex outline, or each object's box |
| Repeat frame until stopped | HAVE |  | Job: Trace again and again until Return to start (up to 50 rounds), then back to S |
| Start plot with three-item checklist | HAVE | | |
| Editable checklist items | HAVE |  | Run, Start checklist: your own items, each must be ticked in the start dialog (per browser) |
| Pause at stroke end, continue, return to start, stop | HAVE | | |
| Honest stop and kill escalation | HAVE | | `runner.py`. Instant stop with `ES` after the firmware upgrade |
| Progress, elapsed, ETA, current layer/pen/speed/pressure | HAVE | | |
| Stale-connection warning | HAVE | | |
| Start from stroke N (resume an interrupted or stopped job) | HAVE | | Start here on the scrubber; after a stopped plot of the same bed, Run offers Resume there (the stroke cut short is drawn whole) |
| Plot only selected objects or layers | HAVE | | layers via enable; Run: Plot only the selection (the rest fade on the bed, Plot all objects returns) |
| Repeat job N times with a paper-change hold between | HAVE |  | Job, Copies (1-50): back at S after each copy, a paper-change pause, the pen swapped back when it differs; the time shows all copies; a resume point applies to the first copy only |
| Live speed override during a run | HAVE |  | Run: a Speed slider (25-150%) while a plot runs, from the next move, never above 40 mm/s |
| Finish position: S, a named spot, or stay | HAVE |  | Job, When done: back to S, clear of the paper (the bed's right edge, level with S), or stay at the last stroke; no named spots yet |
| Job history (what ran, when, result, duration) | HAVE |  | Run, Recent jobs: the last 100 jobs (results/web/history.jsonl keeps 500) with result, time, duration, strokes done |
| Job queue | SKIP | | one machine, one operator; repeat N covers batches |
| Done / pen-swap notifications (browser, sound, ntfy push) | HAVE |  | Run: Notify me (browser notification) and Sound, per browser, at pen changes, paper changes and the end; ntfy push when config/web.json has "ntfy": "<topic URL>" (real jobs only) |
| Delays before the first stroke and after the last | HAVE |  | Job, Path planning: Delay before / Delay after (s) |

## 12. Machine control

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Read-only plotter check (`V`) before hardware starts | HAVE | | `/api/plotter` also exists but the UI never calls it; show it |
| Connection status in the top bar (simulator/hardware, link) | HAVE | | |
| Plotter connection doctor (list ports, test `V`, likely causes) | HAVE |  | Machine, Diagnose the connection: serial ports with USB ids, the board (or its bootloader mode), the server user's groups and port access, the board's answer, and what to do |
| Port auto-detect / picker | HAVE |  | Machine, Plotter: found by its USB id, or a port picked from the list (kept on the server, used by every job) |
| Position record: at start / moved / unknown | HAVE | | |
| Jog pad: step size, speed, pen up, clamped to travel | HAVE | | Machine tab, Control: a machine job (`jog.py`); steps 0.1/1/10/50 mm, speed, arrow keys on the bed; clamped to the travel minus its edge margin |
| Go to X/Y | HAVE | |  |
| Click on the bed to move there (arms for a few seconds) | HAVE | | Click to go arms one click (no timeout) |
| Move to selection anchor (check placement) | HAVE |  | Machine control: To the selection moves pen up to the selection's reference point (Arrange's 9-point anchor) |
| Pen up / pen down buttons | HAVE | | in machine control |
| Short "pen touch" at layer pressure | HAVE |  | Machine control: Touch, pen down for under a second at a pressure, then up |
| Motors off (release to park by hand) | HAVE | | Finish here / Finish at S switch the motors off; Stop too |
| Set start here / re-zero here | HAVE | | Set S here: the carriage becomes S (pen over the paper corner) |
| Saved positions (pen-swap spot, parking, paper corner) | HAVE |  | Machine control, Saved positions: save where the carriage is under a name, Go back to it |
| Start-from options: S, current position, saved origin, with 9-point job anchor | HAVE |  | Job, Start from: as placed, the carriage's position now, or a saved position, with one of nine anchor points of the job's box |
| Live position shown on the bed during jogs | HAVE | | the carriage marker and the position pill follow every move |
| Macros: saved command sequences as buttons | HAVE |  | Machine control, Macros: GOTO, MOVE, PEN, HOME, TICK, SPEED, WAIT lines run as tracked moves, so the position stays known |
| Raw EBB command line in the console | HAVE |  | Machine control, EBB command line: one command at a time; bootloader, reset, flash, configuration and servo setup refused; any command that can move or free the carriage makes the position unknown |
| Live writing (font or own handwriting), new page, finish | HAVE | | `live.js`, `live_plot.py` |
| Live writing: pressure field | HAVE |  | Tools, Live writing: Pressure 0-0.9 (empty = the configured pen-down) |
| Live writing: pause and return | HAVE |  | Live writing: Pause (lines typed meanwhile wait for Continue) and Return to S (the next line carries on where the writing was) |
| Replot a saved live session | HAVE |  | Live writing: Save the page as a drawing, into the Library where it was written, to plot again from the bed |
| Writing pad (point or stroke mode, pad size, palm rejection), sport mode | HAVE | | `pad.js`, `mirror.py` |
| Crash homing: pen up, slow move past full travel into the upper-left stops, then travel to S | SKIP | | dropped 2026-11-18 after a supervised run on the machine (X, Y, then a full home with a dot): driving into the stops with the motor skipping for up to 30 s puts undue stress on the motors and the assembly. Code was in d80fe69, reverted |
| Home button and "position unknown -> home" prompt | SKIP | | goes with crash homing |
| Home one axis at a time | SKIP | | goes with crash homing |
| Keep motors powered after a job (option) | HAVE |  | Job: Keep the motors on after a plot that ends normally; Machine control's Finish switches them off |
| Switch homing (two microswitches on spare EBB inputs) | HAVE |  | Machine control, Home with switches: shown when machine.json has a homing block {x: {pin: B3}, y: {pin: B4}, active, speed}; creeps to each switch reading PI, finds it again slowly, then goes to S. Tested on the simulator only: no switches are fitted |
| Limit switches during plots, alarm unlock, Z axis, rotary, dual head | SKIP | | not on this hardware |

## 13. Machine settings and calibration

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Settings page for the machine (travel, axis signs, steps/mm, servo, margins, paper) | HAVE |  | Machine, Machine settings: travel, axis directions (asks first), speed limits and defaults; paper and margin; steps/mm through calibration; servo only by the pen height test, by design |
| Units mm / inch for display | HAVE |  | View, Show lengths in mm or inches (status line, selection, measure, plan size); typed values take either |
| Calibration wizard: plot a known length, type the measured one, store steps/mm | HAVE | | Machine tab: draw ticks a test length apart, measure, save; refused when more than 20% off; machine.json backed up first |
| Paper edge calibration in the UI | HAVE |  | Machine, Paper, Measure the sheet with the pen: jog to the top-left and bottom-right corners |
| Squareness and backlash test patterns | HAVE |  | Tools: Squareness sheet (square with diagonals), Backlash sheet (lines drawn towards each other) |
| Lost-steps test in the UI | HAVE |  | Tools: Lost-steps sheet and Lost-steps result (the drift turned into steps per metre) |
| Speed and acceleration limits shown and editable within ceilings | HAVE |  | Machine settings: drawing and pen-up limits and defaults, never above the driver's ceilings; acceleration is the firmware's |
| Simulator time scale and estimate tuning | HAVE |  | Job, Simulate at: 1x to 400x (per browser); estimates are scaled by past real runs |
| Backup / restore all settings (machine, pens, macros, positions) as one file | HAVE |  | Machine, Backup: one JSON of machine settings, pens, colour-to-pen memory and saved projects; restore checks the machine values (steps/mm, travel, axes, servo range) and keeps each replaced file. No macros or saved positions exist yet |
| Lifetime stats (jobs, pen-down metres; pen wear) | HAVE |  | Machine, Lifetime: real jobs, hours, strokes, metres of line per pen (from each job's plan, by the share of strokes per layer) |
| Firmware version shown | HAVE | | Machine tab, Plotter: version, port and time of the last answer; Check the plotter asks again (nothing moves) |
| EBB firmware upgrade 2.0.1 -> stock 3.x | HAVE | | 3.1.7 flashed 2026-11-19 with `BL` + mphidflash on robotlab, verified; re-check pen servo values and a short plot |
| Pen positions put back after every EBB power-on or reset (3.x starts the servo at its own pen-up, 22565, past the top of this lift) | PART | P0 | `hardware.ServoGuard` deployed on robotlab 2026-11-19 (75fcd71; the board's QL marker read 1 after the restart). To do: switch the plotter off and on while watching the pen: at most a second of stall, then a slow move to pen-up and a "Plotter switched on or reset" line in the log |
| Driver uses `ES`, `QS`, `QM`, `HM` when `V` reports them, timed waits otherwise | PART | P0 | `plot_svg.Plotter` (plot, jog, live, calibration): firmware 3.x found from `V`; Stop cuts a wait short and sends `ES`, then `QS` reads the position back (counters cleared at S with `CS`); 2.0.1 behaves as before. Tested on fake boards only: try a Stop mid-stroke on the machine. `QM` and `HM` not used: the timed model stays the pacing, and the step counters give the truth after a stop |
| Pen-depth pot firmware patch | PART | P3 | written in `firmware/`, not compiled or flashed; needs MPLAB X + C18; after the stock upgrade |
| Several machine profiles | SKIP | | one machine |

## 14. Pens, paper and tests

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Pen profiles (name, colour, speed, pressure) | HAVE | | read-only, `config/pens.json` |
| Pen library editor: create, edit, duplicate, delete | HAVE |  | Machine, Pens: New, Edit, Duplicate, Delete |
| Pen fields: line width, best hatch spacing, dwell, notes | HAVE |  | line width, colour, speed, pressure, best hatch spacing, dwell, notes |
| Paper profiles (size, type, notes), paired with pens | PART | P3 | Job, Paper: standard sheet sizes (A4, A5, A6, Letter, postcard, portrait A4/A5) or custom; no paper types, notes or pen pairing |
| Link a layer to a pen profile (retune updates the job) vs copy values | HAVE |  | choosing a pen leaves the layer's speed and pressure empty: they follow the pen profile (shown as placeholders), so retuning the pen changes every job using it; a value typed on the layer wins |
| Export / import pen library | HAVE |  | Machine, Pens: Export the pens (JSON), Import pens (merged in, the old file kept) |
| Stroke and circle test sheet | HAVE | | |
| Infill (hatch spacing) test sheet | HAVE | | |
| Pressure test sheet | HAVE | | |
| Record results into the pen profile | HAVE | | |
| Pen height test (toggle, sweep) | HAVE | | |
| Two-parameter grid test (e.g. speed x pressure) with value labels | HAVE |  | Tools, Speed x pressure grid: one cell per speed and pressure (patch, line, circle) at its own settings (data-speed, data-pressure), labelled, one run |
| Place a test sheet anywhere on the paper | HAVE |  | placed like any drawing; with drawings already on the bed it goes into the first free canvas corner |
| Tone ramp test (hatch spacing -> perceived grey) | HAVE |  | Tools, Tone ramp: patches hatched 0.3 to 3 mm apart, labelled |
| Test presets | HAVE |  | Tools: each test's settings saved under a name and loaded back (per browser) |

## 15. Images and tracing

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Convert styles: contour, line art, halftone, one-line | HAVE | | `image_to_svg.py` |
| Parameters: inks, levels, width, threshold, density, blur, single pen | HAVE | | |
| Live conversion preview with mask and stats | HAVE | | |
| Side-by-side or overlay original vs result, synced zoom | HAVE |  | Prepare: Compare and Mask follow the result's zoom and pan; Overlay with fade |
| Brightness, contrast, gamma before converting | HAVE | | Prepare adjust step "Tone" |
| Sharpen / denoise | HAVE | | Prepare adjust step |
| Invert (light ink on dark paper) | HAVE | | Prepare adjust step |
| Crop region | HAVE |  | Prepare: drag a box on the picture in Compare, or the Crop step by edge percentages |
| Rotate and mirror the image | HAVE | | Prepare adjust step |
| Mask by a vector shape | HAVE |  | Prepare adjust step Mask to a shape: ellipse or rounded rectangle, inset, soft edge |
| Centre-line trace (single stroke down a line) | HAVE | | Prepare method "Line art (centre lines)": Zhang-Suen thinning in `image_to_svg.py` |
| Edge/sketch trace for photos of drawings | HAVE |  | Prepare method Photo of a drawing (centre lines): ink is what is darker than its neighbourhood by a set sensitivity, so uneven light drops out; then thinned to one stroke per line |
| Ignore specks under N px | HAVE | | trace setting, plus the "Remove short strokes" clean-up step in mm |
| Smoothness / simplify tolerance | HAVE | | trace setting and trace resolution, plus the "Simplify" clean-up step in mm |
| Brightness window (lower and upper cutoff) | HAVE |  | Prepare adjust step Brightness window |
| Transparent PNG handled as background | HAVE | | Prepare flattens transparency onto white paper |
| Dither styles drawn as stipple dots (error diffusion, ordered) | HAVE |  | Prepare method Stipple dots: Floyd-Steinberg (scattered) or 4x4 Bayer (regular), one pen tick per dot; density sets the cell size |
| Variable pressure by tone | HAVE |  | Shading by hatching: Lighter touch for lighter tones (pen pressure per pass, darkest at full pen-down) |
| Hatch angle per tone level / crosshatch for darks | HAVE |  | Shading by hatching: Hatch angle turns all passes (45 degrees plus it for the lightest, the darker ones cross); tone levels set the passes |
| Image presets (named parameter sets) | HAVE |  | the Prepare presets: an image recipe saved with its adjustments, method and clean-up |
| Save the processed (pre-vector) image | HAVE |  | Prepare result: Download the adjusted image |
| Batch convert many images | HAVE |  | Prepare: Apply to more files, the recipe on up to 50 files of the same kind, each saved to the library |
| Mesh view parameters (yaw, pitch, width) | HAVE | | add max height (CLI only) |
| Turtletoy code (paste a turtletoy.net script, it draws) | HAVE | | Prepare, "Turtletoy code": runs in a worker with no network access (CSP plus removed globals), 30 s and 500k-point limits, slider comments become controls, clipped to the 200 x 200 canvas, size in mm; the result is an SVG source. Pen opacity ignored |
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
| Click-to-place text object on the bed, edit in place | HAVE |  | Text on the bed toolbar: click, type, placed; double-click placed text to edit it (the drawing changes in place, every copy follows) |
| Letter spacing, line spacing, alignment (left/centre/right, top/middle/bottom) | HAVE |  | Text: letter spacing, left/centre/right and a top/middle/bottom anchor at the click; line spacing 3x the letter height |
| Weld overlapping letters (outline fonts) | HAVE |  | Prepare clean-up step Weld overlapping outlines (letter counters stay open) |
| Text on a path | HAVE |  | Line tools, Text along a path: single-line letters along the first selected object's longest line |
| Fit text to a width (squeeze) | HAVE |  | Text: Fit to width squeezes or stretches to an exact width |
| Font browser with favourites and search | HAVE |  | every font field has Browse: search the server's fonts, star favourites (listed first) |
| Own handwriting as a text font outside live writing | SKIP |  | handwriting capture and synthesis moved to the separate handwriting-synth project (2026-11-19) |
| Serial numbers, dates, CSV merge (variable text) | HAVE |  | Text, Several from a list: one text per line, {n} counting from Start, {date}, {1} {2}... columns, stacked down from the click |
| QR / bar code generator | HAVE |  | Shape tool: QR code (versions 1-10, level M) and Code 128 bar code, filled with lines at the pen width; both verified with a decoder |
| Right-to-left text | SKIP | | |

## 17. Console, logs and diagnostics

| Feature | Status | Pri | Notes |
|---|---|---|---|
| Console with level filter, search, auto-scroll pause, copy, clear | HAVE | | `console.js` |
| History replay on reload, SSE resume | HAVE | | |
| Download full log | HAVE |  | Console: Download, the whole log as text |
| Diagnostic bundle (versions, config, last log) | HAVE |  | Help: Download a diagnostic bundle (versions, machine, pens, plotter answer, job, last jobs, lifetime, log) |
| About: app, server and firmware versions | HAVE |  | Help shows the app version (git), Python, pyserial, shapely, Pillow and firmware |
| Usage telemetry | SKIP | | none |

## 18. Settings and shortcuts

| Feature | Status | Pri | Notes |
|---|---|---|---|
| UI preferences in local storage (theme, tab, console) | HAVE | | |
| Preferences page (units, grid, nudge, snapping, defaults) | HAVE |  | Edit, Preferences: units, start page, grid, nudge, snapping, guides, simple mode, keys |
| Shortcuts: `?` `P` Space `Shift+S` `+ - 0` arrows Esc | HAVE | | |
| Editing shortcuts (undo, redo, copy, paste, duplicate, delete, select all, rotate, flip, group) | HAVE |  | Ctrl+Z/Y/A/D/C/X/V, Delete, Esc, R/Shift+R turn, H/V mirror, Ctrl+G/Ctrl+Shift+G group |
| Shortcut list in the help dialog kept in sync | HAVE |  | the help dialog's list is built from the same table as the tooltips |
| Rebind shortcuts | HAVE |  | Preferences, Keyboard: click a key and press the new one; the old key is then free |
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
