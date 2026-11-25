# Technical drawing -> centreline strokes

Goal: turn scanned or rendered line drawings (HAER measured drawings, NASA manual figures,
declassified three-views) into one smooth, crisp pen stroke per drawn line. Straight lines
dead straight, arcs smooth, corners sharp, clean T/X/L junctions, no skeleton spurs.

## Test case and results (2026-11-24)

Source: HAER TX-116 sheet 2, Space Shuttle Main Engine (Library of Congress, public domain),
400 dpi master TIFF, 14400 x 9600 px. Test patch: "Horizontal engine removal" panel,
2430 x 2940 px crop.

| Method | Result |
|---|---|
| `tools/image_to_svg.py --style lineart` (baseline) | Unusable: the 500 px working-size cap erases thin lines; wheels and circles vanish, outlines break up. |
| `tools/trace_centerline.py` v1 | Clean at drawing scale; small gaps at some junctions and in lettering. Causes: thinning staircase corners read as branch points, and threshold 128 split thin anti-aliased joints. |
| `tools/trace_centerline.py` v2 | Fixed by topology-preserving removal of redundant skeleton pixels and threshold 170. Lettering intact, circles single loops, straight lines one segment. 344 strokes, ~5 k points, ~20 s for the patch. |
| Involution (Gerstner et al. 2026), pretrained weights, CPU | Ran on the same panel at half resolution (1215 x 1470; its centreline net pads to a power of two, so full size would be 4096^2). 4.3 min. Geometry is about as faithful as ours and sometimes better through dense crossings, but corners come out slightly rounded and the output is fragmented: 1188 strokes against our 344, so about 3.5x the pen lifts. Lettering is comparable. Not worth adopting as is; its junction handling could be used as a reference. |
| PolyVector Fields, Deep Vectorization | Not run locally (no C++ toolchain, Docker or CUDA here). Run on a lab GPU machine instead, see below. |

Line weights (v3): each edge's drawn width is twice the median distance-transform value along
the middle of its skeleton. On the test patch the widths cluster at about 4, 6, 8 and 10 px
(0.25-0.63 mm at 400 dpi). `--pens 3` (length-weighted 1-D k-means on log width) gave
0.23 / 0.37 / 0.55 mm classes that match the drawing: silhouettes heavy, wheels and booms
medium, interior detail thin. Edges only join through junctions within their own class.
`--splits 5,7` sets the boundaries by hand; `--split-files` writes one SVG per pen.
Known limit: an edge that changes weight without a junction gets one class.

`trace_centerline.py` pipeline: threshold and despeckle, Zhang-Suen thinning, redundant-pixel
cleanup, skeleton graph (end points, junction clusters, edges), spur pruning (2x stroke width),
junction snapping (edges end at the junction centre), RDP simplification, gentle-bend rounding,
joining edges straight through junctions, greedy stroke ordering. numpy, scipy, Pillow only.

## Four-drawing comparison on a GPU machine (2026-11-25)

Inputs: four HAER sheets (LoC, public domain) cropped and halved to 200 dpi: TX-116 sheet 3
(launch stack elevation, 1912 x 3412), TX-116-A sheet 3 (orbiter isometric, 4050 x 2587),
CO-89 sheet 3 (Titan I silo section, 2310 x 4031), SD-50 sheet 3 (Minuteman II exploded
view, 4014 x 1838). Machine: RTX 4090, 24 cores. All three paper methods ran in Docker or a
venv with pretrained weights. Deep Vectorization needed fixes for current torch (in-place
aliasing in refinement, missing Adam state) and an SVG writer; PolyVector needed a new
Dockerfile (its own uses gcc 5 and OpenCV 2.4, which don't build). Ours: `--pens 3`, local CPU.

Strokes for the whole crop, and run time:

| Drawing | ours | Involution | PolyVector | Deep Vectorization |
|---|---|---|---|---|
| stack | 1704, 137 s | 2338, 1119 s | not finished after 2 h | 2361, 1133 s |
| orbiter | 3042, 52 s | 2220, 1944 s | out of memory | 3971, 1751 s |
| titan | 8573, 106 s | 4818, 1481 s | out of memory | 6384, 3323 s |
| minuteman | 3049, 25 s | 1486, 292 s | 2630, 2309 s | 3297, 1459 s |

PolyVector's orbiter and titan runs died of OOM (exit 137) while running alongside other
jobs, and were queued again one at a time.

Findings from zoomed comparisons:
- Involution is the best overall. It keeps close double lines (orbiter ribs, truss members),
  traces faint grey lines, and on hatching gives the fewest strokes. Corners are slightly
  rounded, and arrowheads become small loops. Its graph stage is single-threaded CPU and
  takes most of the time.
- PolyVector gives the most faithful curves on the one sheet it finished (minuteman): smooth
  arcs, double outlines kept. It is very slow and uses about 12 GB of RAM per sheet.
- Deep Vectorization produces straight segments only: curves come out faceted and lines are
  broken into short pieces. In solid fills it emits a criss-cross of lines. Not useful here.
- Ours is 10-30x faster and clean on line work, but it has three weaknesses:
  1. Solid fills: it draws the medial axis of a filled area (the stack's black tile area)
     instead of its outline and the white lines inside.
  2. Grey lines: threshold 170 drops light construction lines.
  3. Close parallel lines (1-2 px apart at 200 dpi) merge into one.
  Stippled hatch (titan concrete) gives many dot strokes for every method.

Involution is shortlisted as an alternative.

## v4: shading as a second pass, junction placement (2026-11-25)

First attempt (dropped): find shaded and solid areas before tracing and fill them. Too
eager: it decided on fill from tone and width alone, so a thick line could become a fill.

Current design, a second pass checked against the line trace:
1. Trace as before (threshold, thin, skeleton graph, per-edge widths).
2. Candidates: ink areas wider than about two typical lines (opening with a disk of the
   typical width, at least 400 px).
3. For each candidate, model what our pens would draw there: a stroke along every
   skeleton edge no wider than the widest pen (--max-pen, 1 mm on paper), at its local
   width capped at that pen. It stays a line (heavier pen through the weight classes) if
   those strokes cover at least 60 % of it AND it is long for its width (skeleton length
   >= 4x median width). Otherwise it is a fill: a closed path in a "fills" layer, grey
   level as the colour, for the plotter's hatch fill.
4. In a grey fill (most pixels lighter than 40) the lines drawn on the shade are kept:
   inside it, ink is only what is darker than 0.6x its grey level. A black fill is
   removed from the line ink entirely.

Results: the orbiter, Titan and Minuteman crops get no fills; their thick beams, callout
circles and dark details stay lines. The shuttle nose and wing tile bands become fills
with the tile grid traced on top. Stack crop at 400 dpi: 93 wide areas, 40 filled, 53
kept as lines.

Junction placement: each junction moves to the least-squares meeting point of its edges'
lines, each fitted just past the distorted zone around the skeleton's Y. Before this,
every T was pulled towards its stem, so the brick tiles came out as wobbly hexagons; now
straight lines stay straight and T corners are square. A larger --trim did not help. The
tile grid also needs 400 dpi (cells are 5-8 px at 200 dpi).

Speed: thinning now limits each pass to the box around the pixels the last pass removed.
The result is identical, and it is 3x faster on images with wide ink areas, which need
one pass per pixel of half-width.

Still open: light grey lines (threshold), small circles come out as octagons at eps 1 px.

Web UI (2026-11-25): the shading finder is its own module, `tools/find_fills.py` (analyze, then
classify / decide / apply), so the slow pass (thinning, stroke model) runs once per image,
threshold and widest pen, and the thresholds re-decide instantly. Prepare has a Technical drawing
method with a Shading view (areas outlined by decision, click to flip, hover for the numbers),
and slow work runs as background tasks with progress and learned time estimates
(`tools/plotweb/tasks.py`). 26 MP stack sheet: measuring 1 min 25 s, tracing 1 min 01 s.

## Survey summary

Off-the-shelf, Windows-friendly:
- AutoTrace `-centerline` (GPL-2.0, maintained, Windows installers): the only stock centreline
  tracer; known to wobble and leave gaps at junctions. Worth running as a comparison.
- Skeleton graph libraries: scikit-image `skeletonize`, OpenCV `ximgproc.thinning`, sknw, skan.
- OpenCV `ximgproc.EdgeDrawing` (EDLines, EDCircles): sub-pixel line and ellipse detection,
  useful to snap fitted primitives. Edge-based, so run it on the skeleton.
- vpype (MIT): `linemerge`, `linesimplify`, `linesort`, `reloop`, plus `deduplicate` plugin.
- Not suitable (outline tracers): potrace, vtracer, vpype-vectrace.

Research code worth trying:
1. Gerstner, Magne, Sorkine-Hornung, "Stroke Vectorization via Involution Prediction"
   (VMV 2026). MIT, TorchScript weights, Windows-capable. Learned centreline + junction
   pairing, outputs a planar polyline graph.
   https://github.com/verven/InvolutionStrokeVectorization
2. Bessmeltsev and Solomon, "Vectorization of Line Drawings via PolyVector Fields" (TOG 2019).
   MIT, C++, CPU; Windows build is fiddly (OpenCV 2.4), Docker available.
   https://github.com/bmpix/PolyVectorization
3. Egiazarian et al., "Deep Vectorization of Technical Drawings" (ECCV 2020). The only one
   trained on technical drawings; archived, Linux/CUDA only.
   https://github.com/Vahe1994/Deep-Vectorization-of-Technical-Drawings

Ideas to borrow for the next version of our tracer:
- Fit primitives per edge: split at corners (turning angle), then total-least-squares line,
  else circle/arc (Kasa/Taubin), else smoothing spline; prefer fewer primitives under a
  tolerance tied to stroke width (Favreau 2016 "fidelity vs simplicity").
- Regularise CAD-style: snap near-horizontal/vertical, merge collinear pieces across gaps,
  make near-parallel lines parallel, merge concentric arcs (Egiazarian refinement step).
- Junctions: re-extend branches along fitted tangents and meet at a least-squares
  intersection, rather than at the pixel-cluster centre.
- Stroke width per edge from the distance transform: heavy lines could go to a separate,
  thicker pen layer, as the Jezero maps do with index contours.
- Text: either keep traced lettering or mask it and reset it in a Hershey single-stroke font.

Sources: subagent surveys 2026-11-24 (repo pages and READMEs fetched; arxiv.org was
unreachable from this machine, so paper claims rest on project pages).
