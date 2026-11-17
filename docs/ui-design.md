# Plotter web UI: design

One workspace in the spirit of LightBurn: the bed is the centre of attention, everything else is a panel around
it. Stdlib Python server, plain ES modules, no build step, no CDN. Research behind the rules: `ui-research.md`.

## Layout

```
+----------------------------------------------------------------------------------+
| Plotter  [Simulator|Hardware]  [link: live]   [state pill]        [Stop] [theme] |
+------------+-----------------------------------------------+---------------------+
| Library    |                                               | Status              |
|  upload,   |          bed (368 x 227.5 mm)                 |  state, next action |
|  convert,  |          paper, inset canvas,                 |  progress, ETA      |
|  files     |          design (drag / resize handles),      |  layer, pen, up/dn  |
| Layers     |          drawn strokes, carriage dot,         |  x/y mm, speed, prs |
|  pen,speed,|          start point S                        | Run                 |
|  pressure  |          pan: drag empty space / wheel zoom   |  Preview, Simulate, |
| Tools      |                                               |  Start, Pause,      |
|  sheets,   |                                               |  Continue, Stop     |
|  pen test  |                                               | Placement (x,y,w)   |
+------------+-----------------------------------------------+---------------------+
| Console  [Info|Debug] [search] [copy] [auto-scroll]                               |
+----------------------------------------------------------------------------------+
```

Phone (< 800 px): one column, bottom tab bar (Bed, Library, Layers, Run, Console); the state pill and Stop stay
in the top bar on every tab. Dark and light via `prefers-color-scheme` with a manual toggle.

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

## Changes to `plot_svg.py` (opt-in flags; without them the CLI behaves as before)

| Flag | Why the UI needs it |
|---|---|
| `--control` | Line protocol on stdin: `CONTINUE`, `PAUSE` (at the next stroke end; pen up, motors on), `STOP` (same cleanup as Ctrl-C, checked before every SM and SP), `RETURN` (at the stroke end: travel back to S). End of input = `STOP`. The confirmation word is read through the same reader. In `--sim` the swap pause waits too. |
| `--sim-speed F` | The virtual clock also sleeps `dt / F` real seconds, so a simulated run can be watched and paused. |
| `--preview-json FILE` | Dry run writes the plotted strokes (machine mm, layer, pressure), the plot order and the summary. |
| `--layers FILE` | Per ink layer `{key, enabled, speed, pressure, pen}`, in layer order; `key` must match. Same ceilings. Layer pressure is the default for strokes without their own `data-pressure`. Disabled layers are dropped after placement, so the others do not move. |
| markers under `--track` | `__PROGRESS__`, `__LAYER__`, `__PAUSE__` (`kind: swap|operator`), `__RESUMED__`, `__RESULT__`. |

Also changed (safety, independent of the UI): the far-edge check uses the physical travel on its own (before, a
paper larger than the bed widened it), and the cleanup ignores a second Ctrl-C/SIGTERM.

## Server modules (`tools/plotweb/`)

| Module | Role |
|---|---|
| `config.py` | paths, limits (from `plot_svg.py`), machine/pens loading, bed geometry |
| `auth.py` | password hash, session cookie, login throttle (unchanged behaviour) |
| `events.py` | `EventLog`: sequence numbers, console history ring, live ring, condition variable |
| `runner.py` | `JobRunner`: reservation, subprocess, output parsing, state machine, control commands, stop escalation |
| `plotjob.py` | settings validation, `plot_svg.py` argv, plan ids, plain-language problems |
| `library.py` | manifest, uploads and conversion, per-file saved settings and geometry |
| `converters.py` | registry of upload converters (svg, raster, mesh) with a parameter schema |
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
| POST `/api/text_art` | `{text, size, bend, effect, font, bold, add}` | `{svg}` or `{file}` |
| GET `/api/converters`, `/api/tools`, `/api/pens` | | schemas, pen profiles |
| POST `/api/preview` | `{id, settings}` | `{ok, plan_id, problems, settings, summary, placement, order, strokes, file_layers}`; saves the settings |
| POST `/api/job/start` | `{id, settings, plan_id, mode: plot|trace|simulate, confirm: {parked}}` | `{job}`, or 400/409 with the reason |
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
| `reset` | `epoch, seq` | client re-reads `/api/state` and `/api/log` |
| `ping` | every 15 s without other events | no |

Job: `{id, kind: plot|pentest, mode: plot|trace|simulate, sim, state, message, meta: {file, name, plan_id},
progress: {done, strokes, layer, fraction, elapsed_s, eta_s, est_done_s, est_total_s}, layer: {index, label, pen,
speed, pressure}, pos, pen, pause: {id, kind, label, pen, layer_index, resuming} | null, result}`.

States: `starting, running, pausing, paused, stopping, returning, complete, stopped, interrupted, error`.

## Frontend modules (`tools/web/js/`)

`api.js` (fetch with plain errors, SSE with resume), `store.js` (one state object, subscriptions, local
preferences), `ui.js` (DOM helpers, toast, prompt, schema forms), `bed.js` (canvas 2D bed view, pan/zoom,
drag/resize, progress, carriage), `workspace.js` (open drawing, settings, automatic previews, placement form,
layers), `run.js` (status panel, run buttons, confirmation, pen-swap prompt), `library.js`, `tools.js`,
`console.js`, `app.js` (boot, login, events, tabs, theme, shortcuts). Styles: `tools/web/app.css`.

Keyboard: `?` help, `P` preview, `Space` pause/continue, `Shift+S` stop (works with dialogs open), arrows nudge
the drawing 1 mm (Shift 10 mm) with the bed focused, `+`/`-`/`0` zoom/fit, `Esc` closes dialogs (never stops).

## Tests

`tests/test_plotweb.py` starts the server in simulator mode and drives upload, preview, placement, validation,
plan binding, a two-layer run with pen-swap pause, reload snapshot, stale continue, operator pause, stop, return,
the event stream (replay and reset), per-pen sheets and records, the pen test and the one-job rule.
`tests/test_plot_svg_flags.py` covers the new `plot_svg.py` flags. Browser flows were exercised headless against
a `--sim` server (not part of the suite).
