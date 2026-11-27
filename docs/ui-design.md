# Plotter web UI: design

One workspace in the spirit of LightBurn: the bed is the centre of attention, everything else is a panel around
it. Stdlib Python server, plain ES modules, no build step, no CDN. Research behind the rules: `ui-research.md`.

## Design language (2026-11-27)

The page respects the operator's time: it does not explain what they already know, and it never hides what is
about to happen to the machine. Every new control follows these rules.

1. **Labels are 1 to 3 words**: a verb for an action (Preview, Set S here), a short noun for a setting (Copies,
   Hatch spacing). The explanation goes in the control's `title`, which `chrome.js` shows as a tooltip on hover,
   keyboard focus or a long press, with the shortcut beside it.
2. **No explanatory paragraphs in panels.** Visible text is live state ("Carriage: position unknown") or a
   safety warning (sport mode, a stale plan, a partial plot). A section's explanation is its heading's tooltip.
3. **Units sit inside the field** (`.with-unit`), ranges in the tooltip, defaults as the placeholder.
4. **The control fits the choice**: on/off a checkbox or a pressed button, one of a few segmented buttons, one of
   many a drop-down, a number a field.
5. **One primary button per area**: Plot, Send to Plot, Save.
6. **No control panel scrolls at 1600 x 900.** What does not fit moves out, by how often it is used:
   used constantly: the toolbar; watched while working: a panel tab; set once: a dialog; rare: the menu bar
   only; acting on an object: the options bar or the right-click menu. Lists (library, console, history) may
   scroll; forms may not.
7. **One editor at a time**: a layer, a recipe step, a pen test, a job section opens on its own and closes the
   others (`details name=...`, or the open row in the layer list).
8. **One word per thing**: a *drawing* is a file in the Library, an *object* is a drawing placed on the bed,
   then *layer*, *pen*, *project*. The *drawable area* is the dashed rectangle (never "canvas" in the page);
   *S* is the start point the carriage is parked at by hand; *Home* is only switch homing.
9. **Every control is reachable from the menu bar.** A menu item presses the existing control (so it is
   disabled under the same conditions) and `reveal()` opens whatever holds it: workspace, tab, section, dialog.
10. **Ids are the wiring.** Moving a control keeps its id and `data-act`; `tests/test_web_ui.py` checks that
    every id and `data-act` a script uses exists, and that no inline style breaks the content security policy.

## Workspaces: Prepare and Plot (decided and built 2026-11-18)

Two top-level tabs in the top bar (keys `1` and `2`, or `#prepare` / `#plot` in the address), the way a slicer
splits preparing a model from printing it. The layout below is the Plot workspace.

- **Prepare** turns an input (photo, drawing, mesh, text, an SVG that needs work) into a plot-ready SVG.
  Each conversion method is a plugin: a Python module that declares its parameters as a schema (like
  `tools.py`), so the form builds itself and adding a method needs no frontend code. A recipe is a chain of
  steps (adjust -> convert -> fill -> simplify -> assign pens). The preview shows the source and the result
  side by side or overlaid, rendered as ink: pen colour and width, hatch fills visible, size in mm, stroke
  count, pen-down length and a time estimate. The result goes to the library with its recipe, so it can be
  reopened and re-tuned instead of redone.
- **Plot** places one or more prepared items on the bed (the document model), sets the per-layer pen, speed
  and pressure, previews, and runs. It is the only workspace that drives the machine.
- **Prepare is optional.** A finished SVG goes straight to Plot. Plot still shows the same ink preview (fills
  as they will be hatched, colours, the pen per layer, the real size), so nothing has to pass through Prepare
  just to be seen correctly.
- **One renderer for both.** Prepare and Plot draw the ink preview with the same code from the same plan data,
  so what Prepare shows is what Plot plots.
- **Stop and the state pill stay in the top bar in both workspaces.** A running job keeps running while you
  work in Prepare. The Plot tab shows a dot while a job runs (pulsing when it waits for you), and a pen change
  also raises a toast in Prepare.

How Prepare is built:

- **Recipe** = `{source, steps: [{step, on, params}]}`. Stages run in order: `adjust` (image -> image, Pillow,
  any number), `method` (exactly one: trace styles, SVG as is, 3D wireframe, or a text generator that needs no
  source), `clean` (on the drawn polylines, any number; refused for drawings with filled shapes, which they
  would lose). Fill and pens are plot settings, not steps: they are saved with the result and Plot can still
  change them.
- **Plugins** live in `tools/plotweb/prepare.py` (`STEPS`): id, title, stage, accepted source kinds, a params
  schema and a function. The UI builds every form from `/api/prepare/steps`, so a new step needs no frontend
  code. Schema types: `number` (optionally `slider`), `select`, `bool`, `text` (optionally `list: fonts`),
  `textarea`.
- **Preview** = the method output dry-run by `plot_svg.py` at natural size at the canvas top-left: the same
  strokes, summary and placement a Plot preview gets. Too big for the canvas is reported as the scale Plot
  will shrink it to, not as a problem. Views: Result, Compare (source after adjustments beside the result),
  Overlay (source under the strokes, with a fade slider), Mask (what the trace counts as ink).
- **Cache.** Sources and every stage result are kept under `results/web/prepare/` for two days, keyed by a
  hash of the source and the steps up to that stage; dry runs are also keyed by `machine.json` and
  `plot_svg.py`. Changing a late step does not redo the early ones.
- **Saving** stores the SVG, a copy of the source as the original, the recipe and the plot settings (fill,
  pen per layer with that pen's speed and pressure). Library "Prepare" reopens any drawing: its own recipe,
  or one rebuilt from an older upload's converter settings. "Update" keeps the drawing's id and its place on
  the bed; "Save as new" keeps the old one.
- The browser keeps the recipe being edited (`localStorage`), so a reload resumes the work.

## The bed document (built 2026-11-18)

The Plot workspace edits a **document**: several library drawings on the bed, each an object
`{id, file, x, y, w, h, rot, fx, fy, g}`. `(x, y, w, h)` is the object's box in machine mm before rotation; it turns
`rot` degrees clockwise about the box centre and `fx`/`fy` mirror it inside the box. The drawing's geometry box
(its strokes at natural size) is stretched to fill the box, so width and height scale independently. `g` is the
drawing's geometry hash when it was placed: if Prepare's Update changes the drawing, the page keeps the width and
takes the new proportions instead of stretching it.

- **One plan for the whole bed.** `document.py` composes the objects into one bed-sized SVG in machine mm (each
  drawing wrapped in a `<g transform="matrix(...)">`) and `plot_svg.py --absolute` plans it. Layers, pen changes,
  time, the canvas check and the plan id work as they did for one file. Object order is file order, so within a
  pen the earlier object plots first.
- **Instant editing.** Each drawing's natural strokes come once from `/api/files/natural`; the bed draws them
  through each object's transform while dragging. A fresh preview replaces that with the planner's exact strokes.
- **Undo** is client-side (200 steps). The working document is saved on the server with every preview (and with
  `POST /api/doc` when there is nothing to preview), so reloads and other devices see the same bed; a `doc` event
  tells the other pages, which take the change as an undoable step.
- **Path planning** is part of the job settings (`optimize`, `dedupe`) and so of the plan id. The resume point
  (`start_at`) is sent with each preview and start but never saved with the bed, and any change to the bed drops it.
  A preview returns a `bed_key` (hash of the objects and settings without the resume point) and a plot job keeps it
  with its `start_at`: after a stopped plot, Run offers to resume only on the same bed. The scrubber under the bed
  toolbar walks through the plan order (Play, Travel, Start here).
- **Layers** are listed in plot order with a name, pen, speed, pressure and passes, and arrows to change the pen
  order. **Filled shapes** lists each fill colour with its mode (job default, hatch with spacing, angle,
  cross-hatch and back-and-forth, contour rings, or outline only); `fills` is part of the job settings. A layer
  seen for the first time takes the pen its colour had last time (`results/web/pen_map.json`).
- **Canvas:** rulers on the top and left; guides dragged out of them (never plotted, saved with the bed as
  `settings.guides`, not part of the plan or the resume key); a moved selection snaps its edges and centre to the
  canvas, the paper, guides and other objects, with a line showing the match (Alt: no snapping); right-click opens
  a menu (right-drag still pans); the status line shows the pointer, the selection's bounds and the zoom; `Z`
  zooms to the selection.
- **Text and arrays:** the bed's Text tool places single-line (Hershey) text where you click
  (`POST /api/text_single`, through the library); Arrange, Array copies the selection in rows and columns. Prepare
  has a Single-line text method with the same fonts (`tools/hershey.py`, data and licence in `tools/hershey/`).
- **Shapes** (`shapes.py`, needs shapely; the buttons hide without it): union, subtract (the others from the
  first in the list), intersect, and offset of the selected objects in machine mm. Closed strokes are areas
  (even-odd within an object); open strokes only take part in an outward offset. The result is a new library
  drawing placed exactly where it was made; the booleans replace their inputs (undoable).
- **Projects** are named copies of the document under `results/web/projects/`. Opening one replaces the bed
  (undoable); drawings no longer in the library are left out with a note.
- **Options bar and Objects tab** (the Arrange panel until 2026-11-27): the objects list (plot order, earlier/later), X/Y/W/H of the selection at a 9-point reference
  point with keep-proportions, a turn angle (one object), turn 90, mirror, align (to each other, or to the canvas
  for one), spread, to-canvas-anchor, fit, duplicate, remove. On the bed: click, Shift/Ctrl-click, box select (left
  to right encloses, right to left touches), drag to move (snaps to 0.5 mm, Alt for none), side handles stretch,
  corner handles keep proportions (Shift: free), the round handle turns in 15 degree steps (Shift: free). Right or
  middle drag pans; a finger on empty bed pans.

## Manual control and machine settings (built 2026-11-18; split into the Control tab and the Machine dialog 2026-11-27)

- **Control** is a job of kind `machine`: `tools/jog.py --control` keeps the motors on and takes `MOVE`, `GOTO`,
  `HOME`, `PEN UP|DOWN`, `TICK`, `SPEED`, `SETS`, `QUIT [HERE]` and `STOP` on stdin. It starts like a plot (parked
  at S, pen up, confirmed, `TAKE` as the first line on hardware), one job at a time, and Stop works as for any job.
  Moves are clamped to the travel minus the edge margin (S itself always counts as reachable). Set S here makes
  the tracked position S again, for putting the pen exactly on the paper corner by jogging. The position stays
  counted, never read back.
- **Calibrate** draws two ticks a test length apart (through the same session); the operator measures them and
  saves; steps/mm scale by commanded / measured, refused when more than 20% off.
- **Paper** sets the corner, size and margin. **Pens** creates, edits, renames and deletes profiles; recorded
  test results are kept. The servo values are shown, never written: they are tuned by hand with a pen mounted.
- `machinecfg.py` writes `machine.json` and `pens.json` atomically, keeps the previous file under
  `results/web/backups/`, refuses while a job runs, and emits a `config` event; pages reload `/api/me` and preview
  again (a calibration changes every plan id).

## Layout

Plot workspace (2026-11-27), in the manner of LightBurn and Photoshop:

```
+--------------------------------------------------------------------------------------------+
| Pen plotter  File Edit View Arrange Tools Machine Help  [Prepare|Plot]   Sim  live  Idle  Stop |
+--------------------------------------------------------------------------------------------+
| Import Projects | Undo Redo History | Preview Simulate Frame [Plot] v |      Pen tests  Machine |
+-------------+----+-----------------------------------------------------+---------------------+
| Library     | Sel| title, paths/length, pages            View Ink - Fit + | Status|Layers|Obj|Job|
| Control     | Txt| options bar: ref X Y W H lock rot | turn mirror |      |                     |
| Write       | Shp|   Align v  Distribute v  Object v  Path v  Colour v  Del |  state, next step,  |
|  (tabs)     | Nod| scrub bar (after a preview)                          |  progress, plan,    |
|             | Trm| bed: rulers, paper, drawable area, objects, S        |  recent jobs        |
+-------------+----+-----------------------------------------------------+---------------------+
|                  | console (collapsed until opened)                     |                     |
+--------------------------------------------------------------------------------------------+
```

While a job runs, Pause, Continue, Return, Stop and the speed slider replace Preview..Plot in the toolbar, and
the right panel switches to Status. Dialogs: Machine (Connection, Motion, Paper, Pen depth, Pens, Backup),
Projects, Pen tests (one test at a time), Preferences (General, Keyboard), Help (Quick start, Shortcuts, About).

Phone (< 900 px): one column, bottom tab bar (Bed, Library, Control, Status, Layers, Job, Console); the toolbar
scrolls sideways, the tool strip hides, the state pill and Stop stay in the top bar. Prepare on a phone stacks
the preview, the recipe and the result. Dark and light via `prefers-color-scheme` with a manual toggle.

## Safety model (UI never weakens `plot_svg.py`)

- A run starts only from a **plan** the operator previewed. `POST /api/preview` resolves the settings, hashes the
  SVG bytes, `machine.json` and the resolved settings into a `plan_id`, and stages copies of exactly those inputs
  (`results/web/plans/<plan_id>/`). `POST /api/job/start` recomputes the plan id from the current inputs and refuses
  if it differs, runs a fresh preflight dry run on the staged copies, and launches `plot_svg.py` on the same staged
  copies. A plan can start one real run; the next run needs a new preview (the UI does this automatically).
- Plot and trace need `confirm: {parked: true}`, sent only after the operator ticks three checks in the dialog
  (parked at S with the motors off; pen up and the first pen in; paper taped, path clear, power switch in reach).
  Simulate is exempt. On hardware the server also does a read-only `V` check, serialised with other checks.
- The carriage is always parked at the paper's top-left (anchor `top-left`, S = paper corner). Placement is
  `--x0/--y0/--width`, so `plot_svg.py` keeps every check (S on paper, drawing in canvas, physical travel, speed
  and pressure ceilings, servo range).
- Server limits mirror `plot_svg.py` (drawing (0, 40], pen-up (0, 200] mm/s, pressure [0, 0.9], paper must fit the
  bed); `plot_svg.py` validates again. The UI shows each limit next to its field.
- One job at a time. A job is **reserved** (`starting`) before any check, so Stop cancels it before launch and a
  second start is refused.
- Stop is honest: no e-stop on firmware 2.0.1. `STOP` is honoured before the next SM/SP is sent (the move in
  flight, at most `CHUNK_SECONDS` = 3 s nominal, still runs out), then pen up and motors off. If the process
  ignores STOP for 30 s the server interrupts it, then kills it and sends pen-up/motors-off itself; the job stays
  reserved until that is done. "Return to start" finishes the current stroke and travels back to S. The UI says
  "switch the plotter off for an instant stop".
- Position is the commanded one. After a run the server records `at_start`, `moved` (with the estimated point) or
  `unknown`; simulated runs never change the record of the real carriage.
- `--sim` server mode: every job runs `plot_svg.py --sim`, the serial port is never opened.

## Changes to `plot_svg.py` (new behaviour is opt-in by flag, with the exceptions listed below the table)

| Flag | Why the UI needs it |
|---|---|
| `--control` | Line protocol on stdin: `CONTINUE`, `PAUSE` (at the next stroke end; pen up, motors on), `STOP` (same cleanup as Ctrl-C, checked before every SM and SP), `RETURN` (at the stroke end: travel back to S). End of input = `STOP`. The confirmation word is read through the same reader. In `--sim` the swap pause waits too. |
| `--sim-speed F` | The virtual clock also sleeps `dt / F` real seconds, so a simulated run can be watched and paused. |
| `--preview-json FILE` | Dry run writes the plotted strokes (machine mm, layer, pressure), the plot order and the summary. |
| `--optimize` | Less pen-up travel (`pathplan.py`): within each pen and each fill/outline run, the nearest stroke next; open strokes may be reversed, closed ones start at the nearest vertex. Pen changes stay where they are. The summary keeps the file-order travel and time for the readout. On by default in the UI. |
| `--dedupe` | Skip strokes that exactly repeat an earlier one of the same ink and pressure (any direction, any start on a closed path). |
| `--start-at N` | Skip the first N strokes of the final plot order: resume a stopped plot. The summary says where it starts and the whole count. |
| `--fills FILE` | Per fill colour `{mode: hatch|contour|off, spacing, angle, cross, zigzag}`; colours without an entry follow `--fill`. Hatch spacing is exact here (the `--fill` default scales by darkness). The summary lists every fill colour (`fill_inks`) so the UI can offer them before any is filled. |
| `--absolute` | The SVG's own millimetres are machine millimetres: no anchoring to the canvas, `--x0/--y0/--width` ignored. The bed document is planned this way. |
| `--layers FILE` | Per ink layer `{key, enabled, speed, pressure, pen, passes, rank, name}` (passes 1..5: each stroke again at once, an open one back over itself; rank: pen order), in layer order; `key` must match. Same ceilings. Layer pressure is the default for strokes without their own `data-pressure`. Disabled layers are dropped after placement, so the others do not move. |
| markers under `--track` | `__PROGRESS__`, `__LAYER__`, `__PAUSE__` (`kind: swap|operator`), `__RESUMED__`, `__RESULT__`. |

Also changed (safety, independent of the UI): the far-edge check uses the physical travel on its own (before, a
paper larger than the bed widened it), and the cleanup ignores a second Ctrl-C/SIGTERM. Smaller output changes that also
apply without the flags: extra layer log lines, more fields in the `__PAUSE__` payload and the saved JSON, multi-layer
time estimates in plot order, and an end of input at the confirmation prompt counting as a refusal.

## Server modules (`tools/plotweb/`)

| Module | Role |
|---|---|
| `config.py` | paths, limits (from `plot_svg.py`), machine/pens loading, bed geometry |
| `auth.py` | password hash, session cookie, login throttle (unchanged behaviour) |
| `events.py` | `EventLog`: sequence numbers, console history ring, live ring, condition variable |
| `runner.py` | `JobRunner`: reservation, subprocess, output parsing, state machine, control commands, stop escalation |
| `plotjob.py` | settings validation, `plot_svg.py` argv, plan ids, plain-language problems |
| `library.py` | manifest, uploads and conversion, per-file saved settings and geometry |
| `converters.py` | registry of upload converters (svg, raster, mesh) with a parameter schema; the Library's direct SVG upload |
| `prepare.py` | Prepare: step plugins (`STEPS`), recipe checks, the per-stage cache (`Workshop`) |
| `machinecfg.py` | calibration, paper and pen-profile edits of the config files, with backups |
| `document.py` | the bed document: object validation and transforms, composing one SVG, the working copy and projects (`DocStore`) |
| `tools.py` | registry of tools: stroke/fill/pressure sheets, result recording, pen height test |
| `hardware.py` | the only direct EBB access: read-only `V` check, last-resort pen-up/motors-off |
| `api.py` | HTTP routes, orchestration (preview, start, tools), SSE |
| `../plot_server.py` | entry point (kept for the systemd unit), `--sim`, `--sim-speed`, `--data` |

Adding a converter or a tool = one registry entry (id, title, help, params schema, function or argv builder).
The UI builds forms from the schema (`/api/converters`, `/api/tools`). Tools of kind `machine` always need a
confirmation and run through the job runner; sheets and records are scoped to one pen.

## HTTP API

JSON in and out; errors are `{ok: false, error}` with a plain message. Everything needs the session except `/`,
static files, `/api/me`, `/api/login`, `/api/logout` and `/api/job/stop` (stopping is never refused).

| Method, path | Body / query | Result |
|---|---|---|
| GET `/api/me` | | `{auth, auth_required, sim, machine, limits, pens, epoch}` |
| GET `/api/state` | | `{epoch, seq, job, position, busy, files_rev, sim}` |
| GET `/api/log?since=N` | | `{epoch, seq, events}`: console history |
| GET `/api/events?since=N&epoch=E` | | `text/event-stream`, `id: epoch:seq`; `reset` when it cannot replay |
| GET `/api/files` | | `{files, rev}` |
| GET `/api/files/svg?id=`, `orig?id=`, `mask?id=` | | the file (sandboxed CSP) |
| POST `/api/files/upload` | raw body; `X-Filename`, `X-Params` (URL-encoded JSON) | `{file}` |
| POST `/api/files/convert_preview` | same | `{svg, stats, mask}`; nothing stored |
| POST `/api/files/delete` | `{id}` | refused while that file is plotting |
| POST `/api/text_art`, `/api/text_page` | `{text, ..., add}` | `{svg}` or `{file}`; the UI now uses the Prepare text methods |
| GET `/api/converters`, `/api/tools`, `/api/pens` | | schemas, pen profiles |
| GET `/api/prepare/steps` | | `{steps, kinds, max_bytes}`: the step plugins and their schemas |
| POST `/api/prepare/source` | raw body; `X-Filename` | `{source: {id, name, kind, px, bytes}}`; id = content hash |
| POST `/api/prepare/run` | `{recipe, settings: {fill, fill_angle}}` | `{key, method, image, mask, stats, summary, placement, order, strokes, layers, fit, problems}` |
| POST `/api/prepare/save` | `{recipe, settings: {fill, fill_angle, layers: [{key, pen}]}, name, replace?}` | `{file}`; refused for a drawing being plotted |
| POST `/api/prepare/from_library` | `{file}` | `{source, recipe, name, replace, settings}` |
| GET `/api/prepare/file?kind=png|mask|svg&key=`, `?kind=source&id=` | | stage images, the SVG (`download=1`), the source (sandboxed CSP) |
| POST `/api/preview` | `{doc: {name, objects, settings}, client}` | `{ok, plan_id, problems, settings, summary, placement, order, strokes, file_layers, objects}`; saves the bed |
| POST `/api/preview` | `{id, settings}` (one file, the old form; kept for tools and tests) | as above, without `objects`; saves the file's settings |
| POST `/api/job/start` | `{doc, plan_id, mode: plot|trace|simulate, confirm: {parked}}` (or `{id, settings, ...}`) | `{job}`, or 400/409 with the reason |
| POST `/api/shapes` | `{op: union|subtract|intersect|offset, objects, distance?, join?}` | `{file, x, y, w, h}` |
| POST `/api/text_single` | `{text, font, xheight, align, width?}` | `{file}`: single-line text added to the library |
| GET `/api/files/natural?id=` | | `{geom: {raw_x, raw_y, raw_w, raw_h, svg_hash}, layers, strokes}`: the drawing at natural size in its own mm |
| GET `/api/doc` | | `{doc: {rev, name, objects, settings}, projects}` |
| POST `/api/doc` | `{name, objects, settings, client}` | `{doc}`: replaces the working bed |
| POST `/api/machine/start` | `{mode: plot|simulate, confirm: {parked}}` | `{job}`: manual control (`jog.py`) |
| POST `/api/machine/send` | `{job, cmd: move|goto|home|pen|tick|speed|sets|quit, args}` | refused unless control is running and ready |
| POST `/api/machine/calibrate` | `{axis, commanded, measured}` | `{old, new, me}` |
| POST `/api/machine/paper` | `{paper: {x0, y0, width, height, margin_mm}}` | `{me}` |
| POST `/api/pens/save`, `/api/pens/delete` | `{name, pen, rename_from?}` / `{name}` | `{me}` |
| POST `/api/projects/save`, `open`, `delete` | `{name}` / `{slug}` | `{projects, doc}`; open also says how many drawings were missing |
| GET `/api/job/preview` | | strokes and order of the current/last plot job (for a reloaded page) |
| POST `/api/job/pause`, `/api/job/return` | `{job}` | refused for traces and stale job ids |
| POST `/api/job/continue` | `{job, pause}` | only for that exact pause id |
| POST `/api/job/stop` | `{job?}` | always accepted |
| POST `/api/tools/run` | `{tool, params, confirm?: {ok}}` | `{file}` (sheet), `{output}` (record), `{job}` (machine) |
| POST `/api/pentest` | `{job, cmd: toggle|sweep|quit}` | one command at a time |
| GET `/api/plotter` | | read-only plotter check when idle |

`settings` = `{x, y, w}` (drawing bbox in machine mm; `w` sets the scale), `speed`, `travel_speed`, `paper`
(`{width, height}` or null), `layers: [{key, enabled, pen, speed, pressure}]`.

## Events (SSE `data:` JSON with `seq`, `t`, `type`)

| type | fields | replayed after reload |
|---|---|---|
| `log` | `level` (debug, info, warn, error), `src` (server, plot, tool, http), `text` | yes, from the history ring |
| `state` | `{job, position, busy}` | no: `/api/state` is the snapshot |
| `pos` | `x, y, pen` (commanded) | no |
| `files` | `rev` | no |
| `doc` | `rev, client` (the page that changed the bed) | no |
| `config` | | no: the page reloads `/api/me` |
| `reset` | `epoch, seq` | client re-reads `/api/state` and `/api/log` |
| `ping` | every 15 s without other events | no |

Job: `{id, kind: plot|pentest, mode: plot|trace|simulate, sim, state, message, meta: {file, name, plan_id},
progress: {done, strokes, layer, fraction, elapsed_s, eta_s, est_done_s, est_total_s}, layer: {index, label, pen,
speed, pressure}, pos, pen, pause: {id, kind, label, pen, layer_index, resuming} | null, result}`.

States: `starting, running, pausing, paused, stopping, returning, complete, stopped, interrupted, error`.

## Frontend modules (`tools/web/js/`)

`api.js` (fetch with plain errors, SSE with resume), `store.js` (one state object, subscriptions, local
preferences), `ui.js` (DOM helpers, toast, prompt, schema forms), `bed.js` (canvas 2D bed view, pan/zoom,
selection, move/resize/turn handles, box select, progress, carriage), `geom.js` (object transforms, bounds,
align/scale/turn/mirror maths, the canvas rectangle), `doc.js` (the bed document: edits with undo/redo, selection,
clipboard, add/arrange, projects, server sync and previews), `ink.js` (the ink renderer both workspaces use: pen colour, pen width
`width_mm` in `pens.json` or 0.5 mm, Ink/Lines toggle), `ws.js` (the two workspaces), `prepare.js` (source, recipe
editor, preview views, pens, save), `workspace.js` (options bar, tool strip, objects list, job settings, layers,
projects), `machine.js` (the Control tab and the Machine dialog: control, calibration, paper, pen depth, pens), `chrome.js` (tooltips, drop-down buttons, tab sets, `reveal()`), `run.js` (status panel, run buttons, confirmation, pen-swap prompt), `library.js`, `tools.js`,
`console.js`, `app.js` (boot, login, events, tabs, theme, shortcuts). Styles: `tools/web/app.css`.

Keyboard: `?` help, `1`/`2` Prepare/Plot, `Ctrl+Enter` send to Plot (Prepare), `P` preview, `Space` pause/continue, `Shift+S` stop (works with dialogs open), arrows nudge
the selection 1 mm (Shift 10 mm, Alt 0.1 mm) with the bed focused, `Ctrl+Z`/`Ctrl+Shift+Z`/`Ctrl+Y` undo/redo,
`Ctrl+A/C/X/V/D` select all, copy, cut, paste, duplicate, `Delete` removes from the bed, `T` travel moves, `Z` zoom to selection, `+`/`-`/`0` zoom/fit, `Esc` closes dialogs (never stops).

## Tests

`tests/test_plotweb.py` starts the server in simulator mode and drives upload, preview, placement, validation,
plan binding, a two-layer run with pen-swap pause, reload snapshot, stale continue, operator pause, stop, return,
the event stream (replay and reset), per-pen sheets and records, the pen test and the one-job rule.
`tests/test_plot_svg_flags.py` covers the new `plot_svg.py` flags. Browser flows were exercised headless against
a `--sim` server (not part of the suite).
