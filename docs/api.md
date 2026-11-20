# Scripting the plotter

The web server's own HTTP API, as used by the page. Everything the page does goes through it, so a script can
do the same: upload, place on the bed, preview, start, watch, stop. JSON in and out unless noted. The full
route list is in `tools/plotweb/api.py` (`ROUTES_GET`, `ROUTES_POST`); the design notes are in `ui-design.md`,
"HTTP API".

`tools/plotter_client.py` does the whole sequence and is the reference for this page:

```
python tools/plotter_client.py http://robotlab:8080 drawing.svg --width 120 --simulate --yes
python tools/plotter_client.py http://robotlab:8080 drawing.svg --x 80 --y 40 --plot
```

## Rules a script must keep

- **A run starts only from a preview.** `POST /api/preview` returns a `plan_id`; `POST /api/job/start` refuses
  it if the drawing, its settings or the calibration changed since, and every plan id is good for one start.
- **The carriage position is not measured.** For `plot` and `trace` the caller promises, with
  `"confirm": {"parked": true}`, that the carriage was parked by hand at S (the paper's top-left) with the pen up,
  the same promise the page asks for. Never send it without checking the machine.
- **Stop always works:** `POST /api/job/stop` `{}` is accepted even without a login.
- One job at a time. While a job runs, settings, pens and the machine controls answer 409.
- Errors come back as `{"error": "text"}` with 400 (bad input), 404, 409 (busy or out of date) or 413.

## Login

Only when `config/web.json` sets a password (robotlab does not: the tailnet is the boundary).
`POST /api/login` `{"password": "..."}` sets a session cookie; send it back on every call.

## Upload

`POST /api/files/upload`, the file's bytes as the body (not JSON), with headers:

| Header | |
|---|---|
| `X-Filename` | URL-encoded file name; the extension picks the converter (svg, png/jpg, pdf, stl, dxf, plt, txt...) |
| `X-Params` | URL-encoded JSON of converter settings; `{}` takes the defaults (`GET /api/converters` lists them) |

-> `{"ok": true, "file": {"id", "name", "kind"}}`. `GET /api/files` lists the library.

## The bed

An object is one drawing placed on the bed, in machine mm (origin at the bed's top-left):

```json
{"id": "o1", "file": "<file id>", "x": 80, "y": 40, "w": 120, "h": 90, "rot": 0, "fx": false, "fy": false}
```

`w`/`h` is the placed size; keep the drawing's aspect (`GET /api/files/natural?id=<file id>` -> `geom.raw_w`,
`geom.raw_h`, its natural size in mm). `GET /api/me` -> `machine.canvas` `[x0, y0, x1, y1]` is the area a drawing
must stay inside, and `limits` the speed ranges.

Optional per object: `ink` (`"#rrggbb"`, every line in that colour), `locked`, `guide` (shown, never plotted),
`seam` (`[u, v]`, fractions of the box: closed shapes start at their point nearest it).

`POST /api/doc` `{"objects": [...], "settings": {...}, "name": "..."}` puts a bed on the server without
previewing (the page then shows it); `GET /api/doc` reads it back.

## Preview

`POST /api/preview` `{"doc": {"objects": [...], "settings": {...}}}` ->

| Field | |
|---|---|
| `ok`, `problems` | plottable, or the reasons in words (off the canvas, speed out of range...) |
| `plan_id` | what `job/start` needs |
| `summary` | `strokes`, `drawing_mm`, `travel_mm`, `est_seconds_all`, `bbox_machine_mm`, `layers` (one per pen) |
| `settings` | every setting as the planner will use it, defaults filled in |

Common settings (all optional): `speed`, `travel_speed` (mm/s), `optimize`, `copies`, `finish`
(`start`, `clear`, `stay`), `fill` (hatch spacing mm), `layers` (per pen: `key`, `enabled`, `pen`, `speed`,
`pressure`, `passes`), `origin` (`here` or `pos:<saved position>`) with `origin_anchor` (`tl` ... `br`). The
Job panel writes the same keys; `GET /api/doc` after setting something in the page shows its spelling.

## Run and watch

`POST /api/job/start` `{"doc": <the same doc>, "plan_id": "...", "mode": "simulate" | "trace" | "plot",
"confirm": {"parked": true}}`. `simulate` never touches the hardware.

`GET /api/state` -> `{"job": {"id", "state", "message", "progress", "pause", ...}, "position", "busy"}`.
`state` is one of `starting`, `running`, `paused`, `complete`, `stopped`, `interrupted`, `error`.
For pushed updates instead of polling, `GET /api/events` is a server-sent event stream (`state`, `log`, `pos`, `doc`,
`files`, `config` events).

A **pen change** (or a copy, or a pause) leaves the job `paused` with `pause.id`. After swapping the pen:
`POST /api/job/continue` `{"job": "<job id>", "pause": "<pause id>"}`; the ids make an old prompt harmless.
Also `POST /api/job/pause` `{"job"}`, `/api/job/return` `{"job"}` (back to S), `/api/job/speed`
`{"job", "factor"}`, `/api/job/stop` `{}`.

## Other things scripts use

| | |
|---|---|
| `GET /api/log/download` | the log as text |
| `GET /api/diagnostics` | versions, settings, last jobs and log as one JSON file |
| `GET /api/about` | versions of the app, Python, libraries and the firmware |
| `GET /api/history` | finished jobs |
| `POST /api/projects/save` `{"name", "template"}`, `/api/projects/open` `{"slug"}` | named beds |
| `POST /api/text_single` | single-line text into the library (the Text tool's request) |

## Inkscape

`tools/inkscape/` is an Inkscape extension (Extensions, Export, Send to the pen plotter) built on the upload
call above: it sends the drawing or the selection to the library and can put it on the bed. Install by copying
both files into Inkscape's user extensions folder.
