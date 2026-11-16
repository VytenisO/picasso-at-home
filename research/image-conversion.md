# Image conversion for pen: methods, papers, repos

Scope: pen/pencil only (single colour, no paint). Output is a set of polylines, in mm, in paper space. Written 2026-11-16 from web search; most papers were read at abstract level only, nothing here has been run. Items marked (prior knowledge) come from memory, not from a source fetched this session.

## TL;DR

- No single algorithm wins. Plotter work splits into two jobs: **structure** (edges, contours, features) and **tone** (shading via line density). The best-looking plots combine both.
- Candidate pipeline to try first: flow-aligned edge lines (FDoG/XDoG or learned) for structure + curvature-following hatching or stipple/TSP for tone, then path optimisation and a physical-pen forward model.
- Cheapest way to get started: run existing tools (`vpype` + `hatched`, `linedraw`, `DrawingBotV3`, `StippleGen`) on a fixed test set, plot a few, and judge on paper. Then build our own converter around the best look.
- The research-grade upgrade: treat the plotter as a differentiable renderer (pen width, overlap, darkness) and optimise strokes against the source image.

## 1. Classical tone methods

| Method | Idea | Strength | Weakness | Source |
|---|---|---|---|---|
| Weighted Voronoi stippling | Lloyd relaxation of points with density = image darkness | Excellent tone, no line artefacts | Many pen-lifts unless chained; dots are slow to draw | Secord, NPAR 2002 |
| Capacity-constrained stippling | Each point gets equal Voronoi "capacity"; better blue noise | Cleaner point spacing than plain Lloyd | Slower | Balzer, Schlömer, Deussen, SIGGRAPH 2009 |
| TSP art | Stipple, then solve TSP to get one continuous line | Zero pen lifts, one stroke | Long crossing-free lines look scribbly; needs a good TSP solver | Kaplan & Bosch, Bridges 2005; Evil Mad Scientist StippleGen |
| Contrast-aware / structure-aware stippling | Stipples respect edges | Sharper features | Regular dots visible in dark areas | Li & Mould |
| Hatching and cross-hatching | Parallel lines, spacing/direction set by tone, layers added for darker regions | Classic pen-and-ink look, easy to plot | Looks mechanical without orientation | Winkenbach & Salesin, SIGGRAPH 94; `hatched` repo |
| Orientable textures | Hatch direction follows image or 3D shape flow | Conveys form | Needs a good direction field | Salisbury et al., SIGGRAPH 97 |
| Squiggle / wave modulation | One scanline whose amplitude/frequency follows tone | Beautiful for portraits, one line per scanline | Loses fine detail across scanlines | SquiggleCam, plotterfun/DrawingBots |
| Flow fields | Stream lines along a vector field (image gradient, noise) | Organic, continuous | Tone control is indirect | vpype-flow-imager |
| Hilbert / space-filling curve | Image-weighted traversal of a fractal curve | Single stroke, no self-crossing | Strong grid look | Continuous-line image stylization based on Hilbert curve (Eurographics) |
| Spiral, circle packing, Delaunay, differential growth, reaction-diffusion | Tone mapped to geometry density | Distinctive aesthetics | Detail-poor | listed in DrawingBots' 41 algorithms |
| Greedy string art | Pick the pin pair whose line best reduces error | Strong tone from straight lines only | Needs a frame, not a free-form drawing | Bridges 2022 greedy string art |

## 2. Classical structure (line) methods

- **Difference of Gaussians family** (prior knowledge for details):
  - DoG, XDoG (Winnemöller, Kyprianidis, Olsen, Computers & Graphics 2012), Flow-based DoG / Coherent Line Drawing (Kang, Lee, Chui, NPAR 2007).
  - FDoG filters along the local edge tangent flow, so lines come out smooth and connected instead of noisy. Probably the strongest classical line extractor for portraits.
- **Canny + contour tracing:** the baseline everything gets compared against. Noisy, many broken fragments; needs linking and pruning.
- **Pencil drawing production:** Lu, Xu, Jia, NPAR 2012 (best paper). Combines a sketch layer (line integral convolution of gradients) with a tone layer (pencil texture strokes). Project page has results; I found no official repo.
- **Raster to vector:** to turn a binary line image into plot paths you need centerline extraction (skeletonisation + graph tracing), not outline tracing. `potrace` gives outlines (two lines per stroke), so use a centerline tool or a skeleton + prune step (prior knowledge).

## 3. Learned line-drawing generators

- **Informative Drawings** (Chan, Durand, Isola, arXiv 2203.12691; `carolineec/informative-drawings`). Unpaired GAN-style translation with adversarial, CLIP, appearance and geometry losses. Lines carry semantics and depth cues, which pure edge filters miss.
- **APDrawingGAN** (CVPR 2019). Face-specific artistic portrait drawings; hierarchical GAN with region-wise discriminators. Used in a portrait-robot paper (PMC10300977).
- **Anime2Sketch** (`Mukosame/Anime2Sketch`) and **ArtLine** (`vijishmadhavan/ArtLine`): clean line art from illustrations and portraits.
- **Learning to Trace** (Adobe Research): CNN tracing of high-res photos into expressive line drawings.
- **ControlNet lineart** (`lllyasviel/ControlNet` models on Hugging Face): lineart preprocessors/models, usable to generate a clean line image from a photo.
- **Caveat for all of these:** output is raster, so you still need the centerline-and-trace step, and the lines may be too dense or too thin for a real pen.

## 4. Optimisation-based stroke methods

These treat the drawing as a parameter set and optimise it against the target. Closest to "make it good" research.

- **CLIPasso** (Vinker et al., SIGGRAPH 2022, arXiv 2202.05822). Bezier strokes optimised via a differentiable rasteriser with a CLIP perceptual loss; the stroke count sets the abstraction level. Good for an abstract/minimal style and for a few-stroke "artist's sketch".
- **Physically Embodied Deep Image Optimisation** (Mihai & Hare, arXiv 2201.08142). Differentiable rasteriser optimises strokes against a deep-feature loss, then exports G-code for pens/pencils. Nearly our exact pipeline.
- **Image-driven Robot Drawing with Rapid Lognormal Movements** (arXiv 2507.03166, RO-MAN 2025). Sigma-lognormal human-motion model + DiffVG; adds a minimum-time smoothing loss so strokes are physically drawable.
- **Hertzmann, painterly rendering with curved strokes of multiple sizes** (SIGGRAPH 98). Coarse-to-fine layers of curved strokes aligned to gradient normals. Pen version: single colour, layered strokes of decreasing size. Also the origin of "strokes follow edge-perpendicular flow".
- **Learning to Paint** (arXiv 1903.04411) and **Paint Transformer** (arXiv 2108.03798, ICCV 2021). Learn to place strokes. Colour-oriented; would need retraining on pen strokes.
- **Greedy/e-David-style**: e-David (Konstanz) tries ~540 candidate strokes at each location and keeps the best, then repeats with camera feedback (Lindemeier 2013). Gives a model-free approach that works even when the machine is imprecise.

## 5. Robotic portrait systems (inspiration, not code)

- Patrick Tresset's **Paul**: camera in, salient-line recovery, observational drawing with visual feedback. The style is a hand-tuned artistic signature; works as a design reference for how a portrait should look.
- **Artistic Robotic Arm: Drawing Portraits under 80 Seconds** (PMC10300977): image to portrait in under 80s on a robot arm.
- **TSP-Bot** (arXiv 2210.07592): stipple + TSP + bounded-curvature Bezier smoothing. The smoothing step matters for our machine (see section 7).
- **StippleDiffusion** (arXiv 2605.15816): diffusion + ControlNet for capacity-constrained stippling; differentiable, accepts variable point budget. Brand new, no code link found.

## 6. Repos and tools (existing, runnable)

| Tool | What | Notes |
|---|---|---|
| [`vpype`](https://github.com/abey79/vpype) | Plotter vector pipeline: `linemerge`, `linesort`, `linesimplify`, layout | Use for post-processing regardless of generator |
| [`plottertools/hatched`](https://github.com/plottertools/hatched) | Hatching from image; vpype plugin; OpenCV + scikit-image + Shapely | Good baseline |
| vpype-flow-imager | Flow-line rendering of images | vpype plugin |
| [`abey79/occult`](https://github.com/abey79/occult) | Hidden-line removal | For layered overlap |
| [`LingDong-/linedraw`](https://github.com/LingDong-/linedraw) | Sketchy vector line drawings from images | Small, readable Python; BrachioGraph-era |
| [`SonarSonic/DrawingBotV3`](https://github.com/SonarSonic/DrawingBotV3) | GUI, many modes, path optimisation | Free edition has 3 modules; Premium has 50 |
| PlotterForge | 50 path modules incl. Voronoi stippling, can drive GRBL | Needs NVIDIA GPU on Windows |
| drawingbots.com image-to-svg | Browser, 41 algorithms (plotterfun-based) | Fast way to preview styles |
| StippleGen (Evil Mad Scientist) | Processing; Secord stippling + TSP | The classic TSP-art tool |
| [`msurguy/SquiggleCam`](https://github.com/msurguy/SquiggleCam) | Squiggle algorithm in browser | MIT |
| [`kalyaninagaraj/tsp-art`](https://github.com/kalyaninagaraj/tsp-art) | TSP art implementation | Reference |
| [`carolineec/informative-drawings`](https://github.com/carolineec/informative-drawings) | Learned line drawings | PyTorch |
| `Mukosame/Anime2Sketch`, `vijishmadhavan/ArtLine` | Learned line art | Illustration/portrait |
| `saxi`, `svg2gcode`, `abey79/vsketch` | Output/driver and generative-code tooling | Not converters |

Not verified: licences, maintenance status, and star counts of all repos above.

## 7. Constraints specific to this machine

These shape which algorithm is feasible, so test them before choosing:
- **Mechanics:** string/belt-driven carriage and a servo lift, so expect some slop and a slow pen-up/down. Fewer pen lifts and fewer sharp turns are better. TSP-Bot's bounded-curvature smoothing and the lognormal paper's minimum-time smoothing are both relevant.
- **Pen lift cost:** each lift is a servo move plus delay. Stipple (thousands of dots) is only viable if chained via TSP or drawn as tiny strokes.
- **Resolution:** line width of a pen is about 0.3-0.7 mm; spacing finer than ~1 mm will just fill in. Calibrate real tone-vs-density first.
- **Time:** an 8 x 11 in drawing must fit in a sensible plot time; set a path-length and lift budget.

## 8. Outside-the-box ideas worth a spike

1. **Forward-model loop:** simulate pen (width, alpha, overlap) in a differentiable renderer and optimise strokes against the photo. Mihai & Hare + DiffVG is the template.
2. **Closed-loop correction:** photograph the paper mid-plot and add strokes where tone is missing (e-David, Paul).
3. **Region-aware direction fields:** per-region hatch direction from segmentation (SAM) or monocular depth (e.g. MiDaS), so hatching follows surface shape. (prior knowledge)
4. **Depth/semantic contour lines:** use a depth map for occluding contours and suggestive contours, ignoring texture edges. (prior knowledge)
5. **Generate a better source first:** run a photo through a ControlNet/diffusion line-art step, then vectorise. Lets you simplify a cluttered photo before drawing.
6. **Learn stroke ordering:** order strokes the way a human would (outline first, then shading), via a learned or heuristic scheme. Makes partial plots look good too.
7. **Single-stroke variants:** TSP art, Hilbert, or a spiral gives a one-line "signature" drawing; pick when mechanical wear is a concern.
8. **Fourier epicycles:** draw the closed contour as a Fourier series. Novelty, but the path is guaranteed smooth. (prior knowledge)
9. **Hybrid per-region style:** edges for the subject, stipple for background, hatching for shadows; choose per segmented region.

## 9. Evaluation plan

- Fixed test set: portrait, landscape, high-contrast object, low-contrast object, line-art/logo.
- Metrics: physical plots judged by eye is the real test. Add proxy metrics in simulation: SSIM/LPIPS vs the blurred source, line-length, pen-lift count, plot-time estimate.
- Process: run existing tools first, plot 3-4 styles at A5, record results in `FINDINGS.md`, then decide what to build.

## Sources

- Secord, Weighted Voronoi Stippling: cs.ubc.ca/labs/imager/tr/2002/secord2002b/secord.2002b.pdf
- Kaplan & Bosch, TSP Art: cs.uwaterloo.ca/~csk/publications/Papers/kaplan_bosch_2005.pdf
- Balzer et al. 2009: graphics.uni-konstanz.de/publikationen/Balzer2009CapacityconstrainedPoint
- Winkenbach & Salesin 1994: grail.cs.washington.edu/projects/cg-illus
- Salisbury et al. 1997: grail.cs.washington.edu/projects/orient
- Kang et al. Coherent Line Drawing: cs.princeton.edu/courses/archive/spr17/cos426/papers/Kang09.pdf
- Lu, Xu, Jia pencil drawing: cse.cuhk.edu.hk/~leojia/projects/pencilsketch/pencil_drawing.htm
- Hertzmann 1998: mrl.cs.nyu.edu/publications/painterly98/
- CLIPasso: arxiv.org/abs/2202.05822
- Mihai & Hare: arxiv.org/abs/2201.08142
- Rapid Lognormal Movements: arxiv.org/abs/2507.03166
- TSP-Bot: arxiv.org/abs/2210.07592
- StippleDiffusion: arxiv.org/abs/2605.15816
- Informative Drawings: arxiv.org/abs/2203.12691
- Learning to Paint: arxiv.org/abs/1903.04411; Paint Transformer: arxiv.org/abs/2108.03798
- Greedy string art: archive.bridgesmathart.org/2022/bridges2022-63.html
- Hilbert continuous-line stylization: diglib.eg.org/items/3dc00dda-1d94-4941-8599-c48a54bc52e8/full
- e-David: en.wikipedia.org/wiki/EDavid_(robot); Lindemeier 2013 (graphics.uni-konstanz.de)
- Paul (Tresset): research.gold.ac.uk/id/eprint/9255
- Portrait robot: ncbi.nlm.nih.gov/pmc/articles/PMC10300977/
- Tool surveys: plotcaster.com/learn/pen-plotter-software/, drawingbots.com/tools/image-to-svg/
