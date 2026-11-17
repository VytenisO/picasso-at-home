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

- Every motion run (plot, trace, simulate is exempt) goes: preflight dry run (server, must have no PROBLEM) ->
  confirmation dialog listing what will happen -> operator ticks "carriage is parked by hand at the paper's
  top-left (x, y mm), pen up, motors off, power switch in reach" -> types nothing else, presses Start.
  The server re-checks: fresh preflight, `confirm` token, plotter reachable (read-only `V`), no job running.
- The carriage is always parked at the paper's top-left (anchor `top-left`, S = paper corner). Placement on
  the canvas is expressed as `--x0/--y0/--width`, so `plot_svg.py` keeps all its checks (S on paper, drawing in
  canvas, speed and pressure ceilings, servo range).
- Server limits mirror `plot_svg.py`: drawing speed (0, 40], travel (0, 200], pressure [0, 0.9].
  `plot_svg.py` validates again; the UI shows the limits next to each field.
- Stop is honest: no e-stop on firmware 2.0.1. Stop = finish the move already sent (bounded by
  `CHUNK_SECONDS` = 3 s), pen up, motors off. "Stop and return" = finish the current stroke, pen up, travel back
  to S, motors off (position stays valid). The UI says "switch the plotter off for an instant stop".
- After an interrupted or failed run the UI marks the position "not at S" and the next start requires the
  re-park confirmation again (it is required every time anyway).
- `--sim` server mode: every job runs `plot_svg.py --sim`, the serial port is never opened.

## Changes to `plot_svg.py` (opt-in flags only; CLI behaviour without them is unchanged)

| Flag | Why the UI needs it |
|---|---|
| `--control` | Line protocol on stdin: `CONTINUE`, `PAUSE` (at next stroke end, pen up, motors stay on), `STOP` (raise the same KeyboardInterrupt cleanup at the next move chunk), `RETURN` (stop at stroke end and travel back to S). EOF = `STOP`. In `--sim` the swap pause waits too. Cross-platform, unlike SIGINT. |
| `--sim-speed F` | Virtual clock also sleeps `dt / F` real seconds, so a simulated run can be watched and paused. |
| `--preview-json FILE` | Dry run writes the plotted strokes (machine mm, per-stroke layer, pressure) and the summary (problems, layers, estimate). The canvas draws exactly what will be plotted. |
| `--layers FILE` | Per-layer `{enabled, speed, pressure, pen}`; same ceilings. Layer pressure is the default for strokes without their own `data-pressure`. |
| markers under `--track` | `__PROGRESS__` (stroke index, layer, estimated done/total seconds, elapsed), `__LAYER__`, richer `__PAUSE__` (`kind: swap|operator`, layer, pen). |

## Server modules (`tools/plotweb/`)

| Module | Role |
|---|---|
| `config.py` | paths, `web.json` (auth secrets, defaults), machine/pens loading |
| `auth.py` | password hash, session cookie, login throttle (as before) |
| `events.py` | `EventLog`: sequence-numbered ring buffer + condition variable for SSE subscribers |
| `runner.py` | `JobRunner`: one subprocess at a time, chunked stdout reader, marker parsing, state machine, control commands |
| `plotjob.py` | option validation, `plot_svg.py` argv, preflight/preview |
| `library.py` | manifest, uploads, per-file saved settings |
| `converters.py` | registry of upload converters (svg, raster, mesh) with a parameter schema |
| `tools.py` | registry of tools (stroke sheet, fill sheet, pressure sheet, pen test, paper trace) with a parameter schema |
| `api.py` | HTTP routes, JSON helpers, SSE |
| `../plot_server.py` | entry point (kept for the systemd unit) |

Adding a converter or a tool = one registry entry (id, title, help, params schema, function or argv builder).
The UI builds forms from the schema (`/api/converters`, `/api/tools`).

## HTTP API (JSON; all except `/`, static files, `/api/me`, `/api/login`, `/api/job/stop` need the session)

| Method, path | Body / query | Result |
|---|---|---|
| GET `/api/me` | | `{auth, sim, server, machine:{bed, paper, canvas, limits}}` |
| GET `/api/state` | | snapshot: `{seq, job, files_rev}` |
| GET `/api/events?since=N` | | `text/event-stream`; replays buffered events after N, then live |
| GET `/api/files` | | `{files:[entry]}` |
| POST `/api/upload` | raw body, `X-Filename`, `X-Params` (JSON) | `{file}` |
| POST `/api/convert_preview` | same | `{svg, stats}` (nothing stored) |
| POST `/api/files/delete` | `{id}` | |
| GET `/api/files/svg?id=` / `orig?id=` | | file (sandboxed CSP) |
| POST `/api/preview` | `{id, settings}` | `{ok, problems[], summary, strokes, placement}`; saves settings |
| POST `/api/job/start` | `{id, settings, mode: plot|trace|simulate, confirm}` | `{job}` or 409 with reasons |
| POST `/api/job/continue` `/pause` `/stop` `/return` | | |
| GET `/api/converters`, `/api/tools`, `/api/pens` | | schemas, pen profiles |
| POST `/api/tools/run` | `{tool, params, confirm?}` | `{file}` for sheet generators, `{job}` for machine tools |
| POST `/api/pentest` | `{cmd: toggle|sweep|quit}` | sends to a running pen-test job |

`settings` = `{x, y, w}` (design bbox, machine mm; `w` sets the scale), `speed`, `travel_speed`, `paper`
(optional size override), `layers: [{enabled, pen, speed, pressure}]`.

## Events (SSE `data:` JSON, each with `seq`, `t`, `type`)

| type | fields | buffered |
|---|---|---|
| `log` | `level` (debug, info, warn, error), `src` (plot, server, tool), `text` | yes |
| `state` | full job snapshot (below) | latest only |
| `pos` | `x, y, pen` | no (latest in snapshot) |
| `files` | `rev` | latest only |

Job snapshot: `{id, kind, mode, file, state, sim, started, finished, progress:{stroke, strokes, fraction,
elapsed_s, eta_s, layer, layers}, layer:{index, label, pen, speed, pressure}, pos, pen, pause:{kind, layer,
label, pen, message} | null, result:{status, message, at_start}, preview_ref}`.

States: `idle, running, pausing, paused, stopping, complete, interrupted, error`.

## Frontend modules (`tools/web/js/`)

`api.js` (fetch, errors, SSE with reconnect and `since`), `store.js` (single state object + subscribe),
`bed.js` (canvas 2D: bed/paper/canvas/design/strokes/carriage, pan/zoom, drag/resize), `status.js`,
`run.js` (preview, confirm dialog, start/pause/continue/stop, pen-swap prompt), `layers.js`, `library.js`
(upload, conversion form from schema, file list), `tools.js`, `console.js`, `forms.js` (schema -> form),
`app.js` (wiring, tabs, theme, keyboard shortcuts). Styles in `tools/web/app.css`.

Keyboard: `?` help, `P` preview, `Space` pause/continue, `Esc` closes dialogs (never stops a job),
`Shift+S` stop (asks nothing: stopping is always safe), arrows nudge the design 1 mm (Shift 10 mm),
`+`/`-`/`0` zoom/fit.

## Tests

`tests/test_plotweb.py` (stdlib `unittest`): starts the server in `--sim` on a free port with temp dirs and
drives upload, preview, simulate with `--sim-speed`, pen-swap pause/continue, operator pause, stop, reload
snapshot, event replay, validation errors, tool sheet generation. `tests/test_plot_svg_control.py`: control
protocol and new flags directly.
